import os
import time
import requests
from datetime import datetime, timezone
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from dotenv import load_dotenv
from flask import Flask

# === ENV ===
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# === CONFIG ===
RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
    "https://solana.public-rpc.com",
]
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/"
GRADUATES_FEED = "https://api.pump.fun/graduate/recent"
CHECK_INTERVAL = 30  # seconds (real-time watch)
SEEN = set()

# === FLASK KEEPALIVE ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Raydium Graduate Token Analyzer is running!"

# === TELEGRAM ===
def send_telegram(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


# === ONCHAIN ===
def get_token_data(ca):
    for rpc in RPC_URLS:
        try:
            client = Client(rpc)
            mint = Pubkey.from_string(ca)
            supply = client.get_token_supply(mint).value.ui_amount
            largest = client.get_token_largest_accounts(mint).value
            top10_balances = [float(a.ui_amount) for a in largest[:10]]
            top10pct = sum(top10_balances) / supply * 100 if supply > 0 else 0.0
            holders = len(largest)
            return supply, holders, top10pct
        except Exception:
            time.sleep(1)
            continue
    return 0.0, 0, 0.0


# === DEXSCREENER ===
def get_dex_data(ca):
    try:
        r = requests.get(f"{DEXSCREENER_API}{ca}", timeout=10).json()
        pairs = r.get("pairs", [])
        if not pairs:
            return None
        pair = max(pairs, key=lambda x: x.get("volume", {}).get("h24", 0))
        return {
            "symbol": pair.get("baseToken", {}).get("symbol", ""),
            "fdv": float(pair.get("fdv", 0) or 0),
            "volume": float(pair.get("volume", {}).get("h24", 0) or 0),
            "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "url": pair.get("url", ""),
            "dex": pair.get("dexId", "unknown"),
        }
    except Exception:
        return None


# === ALERT ===
def format_alert(symbol, ca, fdv, volume, liquidity, top10, holders, mins, dex, link):
    return (
        f"🔥 *New Raydium Graduate Detected!*\n\n"
        f"💎 *Token:* ${symbol}\n"
        f"💰 *Market Cap:* ${fdv:,.0f}\n"
        f"📊 *Volume (24h):* ${volume:,.0f}\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"🏦 *Top 10 Wallets:* {top10:.2f}%\n"
        f"👥 *Holders:* {holders}\n"
        f"🕒 *Graduated:* {mins:.0f} mins ago\n"
        f"🧩 *DEX:* {dex.capitalize()}\n\n"
        f"🔗 [DexScreener Link]({link})\n"
        f"🧾 *CA:* `{ca}`"
    )


# === FETCH GRADUATES ===
def get_recent_graduates():
    try:
        r = requests.get(GRADUATES_FEED, timeout=15)
        if r.status_code != 200:
            return []
        grads = r.json()
        now = datetime.now(timezone.utc)
        results = []

        for g in grads:
            ca = g.get("mint")
            if not ca or ca in SEEN:
                continue
            ts = g.get("timestamp", 0)
            age_mins = (now - datetime.fromtimestamp(ts, timezone.utc)).total_seconds() / 60
            if age_mins > 60:
                continue
            SEEN.add(ca)
            results.append({"ca": ca, "age": age_mins})
        return results
    except Exception:
        return []


# === MAIN LOOP ===
def monitor_graduates():
    send_telegram("🚀 *Raydium Graduate Analyzer Started...*")
    while True:
        grads = get_recent_graduates()
        for g in grads:
            ca, age = g["ca"], g["age"]
            dex_data = get_dex_data(ca)
            if not dex_data:
                continue

            fdv, volume = dex_data["fdv"], dex_data["volume"]
            if not (80000 <= fdv <= 300000 and volume >= 200000):
                continue

            supply, holders, top10 = get_token_data(ca)
            if holders < 250 or top10 > 25:
                continue

            alert = format_alert(
                dex_data["symbol"], ca, fdv, volume,
                dex_data["liquidity"], top10, holders, age,
                dex_data["dex"], dex_data["url"]
            )
            send_telegram(alert)

        time.sleep(CHECK_INTERVAL)


# === ENTRYPOINT ===
if __name__ == "__main__":
    import threading

    # Run main monitor loop in background thread
    threading.Thread(target=monitor_graduates, daemon=True).start()

    # Flask web app to keep container alive
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
