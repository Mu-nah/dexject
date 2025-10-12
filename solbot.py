# solbotC.py
import os
import time
import requests
from datetime import datetime, timezone
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.pubkey import Pubkey

load_dotenv()

# === Configuration ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# DexScreener search endpoint (works)
DEX_SEARCH_URL = "https://api.dexscreener.io/latest/dex/search?q=solana"
DEX_TOKEN_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/"

# Solana RPC endpoints (rotate on failures)
RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
    "https://solana.public-rpc.com",
]

# Monitoring / thresholds
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # seconds
MAX_WATCH_MINUTES = 60
MIN_FDV = 80_000
MAX_FDV = 300_000
MIN_VOLUME_24H = 200_000
MAX_TOP10_PCT = 25.0
MIN_HOLDERS = 250

# Internal state
WATCHLIST = {}  # ca -> {"first_seen_ts": epoch, "alert_sent": bool, "symbol": str, "pair_url": str}
SEEN_FOREVER = set()  # tokens already alerted (keeps them suppressed forever)

app = Flask(__name__)


# === Telegram ===
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        # Silent: we purposely avoid console logs
        pass


# === On-chain helpers ===
def get_onchain_top10_holders(mint_address: str):
    """
    Return (supply, holders_count, top10_pct)
    If RPC calls fail, return (0.0, 0, 0.0)
    """
    for rpc in RPC_URLS:
        try:
            client = Client(rpc)
            mint = Pubkey.from_string(mint_address)

            supply_resp = client.get_token_supply(mint)
            if not hasattr(supply_resp, "value"):
                continue
            supply = float(supply_resp.value.ui_amount or 0.0)

            largest_resp = client.get_token_largest_accounts(mint)
            accounts = getattr(largest_resp, "value", []) or []
            holders = len(accounts)
            top10_sum = 0.0
            for a in accounts[:10]:
                # each a has .ui_amount
                amt = getattr(a, "ui_amount", 0.0)
                top10_sum += float(amt or 0.0)
            top10_pct = (top10_sum / supply * 100.0) if supply > 0 else 0.0
            return supply, holders, top10_pct
        except Exception:
            # try next RPC
            time.sleep(0.2)
            continue
    return 0.0, 0, 0.0


# === DexScreener helpers ===
def fetch_dex_search():
    """
    Fetch the search feed for Solana pairs.
    Returns list of pairs or empty list on failure.
    """
    try:
        r = requests.get(DEX_SEARCH_URL, timeout=15)
        if r.status_code != 200 or not r.text:
            return []
        data = r.json()
        return data.get("pairs", []) or []
    except Exception:
        return []


def fetch_token_pair_by_mint(mint_address: str):
    """
    Fetch token's pairs via DexScreener token endpoint.
    Returns the highest-volume pair dict or None.
    """
    try:
        r = requests.get(f"{DEX_TOKEN_ENDPOINT}{mint_address}", timeout=10)
        if r.status_code != 200 or not r.text:
            return None
        data = r.json()
        pairs = data.get("pairs", []) or []
        if not pairs:
            return None
        pair = max(pairs, key=lambda x: x.get("volume", {}).get("h24", 0) or 0)
        return pair
    except Exception:
        return None


# === Alert formatting ===
def format_alert(pair, ca, fdv, volume, liquidity, top10pct, holders, age_min):
    symbol = pair.get("baseToken", {}).get("symbol", ca[:6])
    dex = pair.get("dexId", "unknown")
    url = pair.get("url", "")
    return (
        f"🔥 *Pump.fun Graduate Detected* 🔥\n\n"
        f"💠 *Token:* ${symbol}\n"
        f"🧾 *CA:* `{ca}`\n"
        f"💰 *Market Cap (FDV):* ${fdv:,.0f}\n"
        f"📈 *24h Volume:* ${volume:,.0f}\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"🏦 *Top 10 wallets:* {top10pct:.2f}%\n"
        f"👥 *Holders:* {holders}\n"
        f"🕒 *Age:* {age_min:.0f} min\n"
        f"🧩 *DEX:* {dex}\n\n"
        f"🔗 [DexScreener]({url})"
    )


# === Core monitoring logic ===
def update_watchlist_from_search():
    """
    Scan DexScreener search results and populate/update WATCHLIST for Pump.fun graduates.
    """
    pairs = fetch_dex_search()
    now = time.time()
    # Build quick lookup of current pairs by mint for efficiency
    for p in pairs:
        try:
            info = p.get("info", {}) or {}
            # Identify pump.fun graduates: sourceId == 'pumpfun' OR info mentions pumpfun
            is_pumpfun = (info.get("sourceId") == "pumpfun") or ("pumpfun" in info.get("header", "") or "pumpfun" in info.get("imageUrl", ""))
            # Also allow if p contains 'labels' or 'tags' referencing pumpfun (optional)
            if not is_pumpfun:
                continue

            base = p.get("baseToken", {}) or {}
            ca = base.get("address")
            if not ca:
                continue
            # compute pair age in minutes: prefer pairCreatedAt (ms) if present
            created_ms = p.get("pairCreatedAt") or 0
            if created_ms:
                age_min = (time.time() * 1000 - created_ms) / 60000.0
                created_ts = created_ms / 1000.0
            else:
                # fallback to 'age' field (seconds)
                age_min = (p.get("age", 0) or 0) / 60.0
                created_ts = time.time() - (p.get("age", 0) or 0)

            if age_min > MAX_WATCH_MINUTES:
                continue

            # Keep or add to WATCHLIST if not already alerted forever
            if ca in SEEN_FOREVER:
                continue

            # Populate watchlist entry if not present
            if ca not in WATCHLIST:
                WATCHLIST[ca] = {
                    "first_seen_ts": created_ts,
                    "alert_sent": False,
                    "symbol": base.get("symbol", ""),
                    "pair_snapshot": p,
                }
            else:
                # update snapshot so subsequent checks use fresh data
                WATCHLIST[ca]["pair_snapshot"] = p

        except Exception:
            # silent
            continue


def evaluate_watchlist():
    """
    For each token in WATCHLIST:
      - compute current age
      - get up-to-date pair data (use snapshot)
      - get fdv/volume/liquidity from pair or token endpoint
      - get on-chain holders/top10
      - if criteria met and alert not sent => send alert and mark SEEN_FOREVER
      - remove entries older than MAX_WATCH_MINUTES
    """
    now = time.time()
    to_remove = []
    for ca, meta in list(WATCHLIST.items()):
        try:
            first_seen = meta.get("first_seen_ts", now)
            age_min = (now - first_seen) / 60.0
            if age_min > MAX_WATCH_MINUTES:
                to_remove.append(ca)
                continue

            # get latest pair (snapshot) and also try token endpoint to be safe
            pair = meta.get("pair_snapshot") or fetch_token_pair_by_mint(ca)
            if not pair:
                # try token endpoint
                pair = fetch_token_pair_by_mint(ca)
                if not pair:
                    continue

            fdv = float(pair.get("fdv", 0) or 0)
            volume24h = float(pair.get("volume", {}).get("h24", 0) or 0)
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)

            # quick metric filter (fdv + volume)
            if not (MIN_FDV <= fdv <= MAX_FDV):
                continue
            if volume24h < MIN_VOLUME_24H:
                continue

            # on-chain metrics
            _, holders, top10pct = get_onchain_top10_holders(ca)
            if holders < MIN_HOLDERS:
                continue
            if top10pct > MAX_TOP10_PCT:
                continue

            # All pass and alert not sent yet
            if not meta.get("alert_sent", False):
                text = format_alert(pair, ca, fdv, volume24h, liquidity, top10pct, holders, age_min)
                send_telegram(text)
                WATCHLIST[ca]["alert_sent"] = True
                SEEN_FOREVER.add(ca)
                # optional: we can remove after alert to save memory
                to_remove.append(ca)

        except Exception:
            # silent
            continue

    # cleanup
    for ca in to_remove:
        WATCHLIST.pop(ca, None)


def monitor_loop():
    # Start without console output; send single "started" Telegram message optionally
    # send_telegram("🚀 Graduate monitor started")  # uncomment if you want a startup message
    while True:
        try:
            update_watchlist_from_search()
            evaluate_watchlist()
        except Exception:
            # silent
            pass
        time.sleep(POLL_INTERVAL)


# === Flask keepalive endpoint ===
@app.route("/")
def index():
    return "Pump.fun -> DexScreener Graduate Watcher (running)"

if __name__ == "__main__":
    # Start monitor thread
    t = Thread(target=monitor_loop, daemon=True)
    t.start()
    # Run Flask app (will keep process alive on Render)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
