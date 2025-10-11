import os
import time
import threading
import requests
from datetime import datetime, timezone
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from dotenv import load_dotenv
from flask import Flask

load_dotenv()

# === CONFIG ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
    "https://solana.public-rpc.com",
]
TOKEN_LIST_URL = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"

SEEN = set()

app = Flask(__name__)

# === TELEGRAM ALERT ===
def send_telegram_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# === ON-CHAIN DATA ===
def get_token_data(mint_address: str):
    for rpc in RPC_URLS:
        try:
            client = Client(rpc)
            mint = Pubkey.from_string(mint_address)

            supply_resp = client.get_token_supply(mint)
            supply_value = float(supply_resp.value.ui_amount) if hasattr(supply_resp, "value") else 0.0

            largest_resp = client.get_token_largest_accounts(mint)
            accounts = getattr(largest_resp, "value", [])
            if not accounts:
                continue

            top10_balances = [float(a.ui_amount) for a in accounts[:10] if hasattr(a, "ui_amount")]
            top10_sum = sum(top10_balances)
            top10pct = (top10_sum / supply_value * 100) if supply_value > 0 else 0.0
            holders = len(accounts)

            return supply_value, holders, top10pct
        except Exception as e:
            print(f"On-chain fetch error from {rpc}: {e}")
            time.sleep(1)
    return 0.0, 0, 0.0

# === DEXSCREENER PER TOKEN ===
def get_token_market_data(mint_address: str):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
        resp = requests.get(url, timeout=10).json()
        pairs = resp.get("pairs", [])
        if not pairs:
            return 0.0, 0.0, 0.0, "", "N/A", "N/A"
        pair = max(pairs, key=lambda x: x.get("volume", {}).get("h24", 0))
        fdv = float(pair.get("fdv", 0) or 0)
        volume24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        liquidityUsd = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        link = pair.get("url", "")
        socials = pair.get("info", {}).get("socials", [])
        twitter = next((s["url"] for s in socials if s["type"] == "twitter"), "N/A")
        telegram = next((s["url"] for s in socials if s["type"] == "telegram"), "N/A")
        return fdv, volume24h, liquidityUsd, link, twitter, telegram
    except Exception as e:
        print(f"DexScreener fetch error: {e}")
        return 0.0, 0.0, 0.0, "", "N/A", "N/A"

# === ALERT FORMAT ===
def format_alert(symbol, ca, fdv, volume, liquidity, top10pct, holders, pair_age, link, twitter, telegram):
    text = (
        f"🔥 *New Raydium Graduate Detected:* ${symbol}\n\n"
        f"💰 *Market Cap:* ${fdv:,.0f}\n"
        f"📊 *Volume (24h):* ${volume:,.0f}\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"🏦 *Top 10 Wallets:* {top10pct:.2f}%\n"
        f"👥 *Holders:* {holders}\n"
        f"🕒 *Graduated:* {pair_age:.0f} minutes ago\n"
        f"🔗 [DexScreener]({link})\n"
        f"🐦 [Twitter]({twitter}) | 💬 [Telegram]({telegram})\n"
        f"🧾 *CA:* `{ca}`"
    )
    return text

# === FETCH RECENT TOKENS ===
def get_recent_tokens():
    try:
        r = requests.get(TOKEN_LIST_URL, timeout=10).json()
        tokens = r.get("tokens", [])
        now = datetime.now(timezone.utc)
        recent_tokens = []
        for t in tokens:
            ca = t.get("address")
            symbol = t.get("symbol")
            ts = t.get("extensions", {}).get("listedAt")
            if not ca or not ts or ca in SEEN:
                continue
            age = (now - datetime.fromtimestamp(ts, timezone.utc)).total_seconds() / 60
            if age > 59:
                continue
            SEEN.add(ca)
            recent_tokens.append({"ca": ca, "symbol": symbol, "age": age})
        return recent_tokens
    except Exception as e:
        print(f"Error fetching recent tokens: {e}")
        return []

# === MAIN WORKER THREAD ===
def worker():
    while True:
        try:
            recent_tokens = get_recent_tokens()
            for t in recent_tokens:
                ca = t["ca"]
                symbol = t["symbol"]
                age = t["age"]

                supply, holders, top10pct = get_token_data(ca)
                fdv, volume, liquidity, link, twitter, telegram = get_token_market_data(ca)

                # --- Filters ---
                if not (80_000 <= fdv <= 300_000):
                    continue
                if volume < 200_000:
                    continue
                if top10pct > 25:
                    continue
                if holders < 250:
                    continue

                msg = format_alert(symbol, ca, fdv, volume, liquidity, top10pct, holders, age, link, twitter, telegram)
                send_telegram_message(msg)

        except Exception as e:
            print(f"Worker loop error: {e}")
        time.sleep(60)

# === FLASK ENDPOINT ===
@app.route("/")
def home():
    return "🚀 Raydium Graduate Token Watcher is running!"

if __name__ == "__main__":
    # Start background worker
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
