import os
import time
import threading
import requests
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

SEEN = set()
DEXSCREENER_NEW_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/solana"

app = Flask(__name__)

# === TELEGRAM ===
def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url, data=data, timeout=10)
    except:
        pass  # silently ignore errors

# === ON-CHAIN DATA ===
def get_token_data(mint_address):
    for rpc in RPC_URLS:
        try:
            client = Client(rpc)
            mint = Pubkey.from_string(mint_address)

            supply_resp = client.get_token_supply(mint)
            if not hasattr(supply_resp, "value"):
                continue
            supply_value = float(supply_resp.value.ui_amount)

            largest_resp = client.get_token_largest_accounts(mint)
            accounts = getattr(largest_resp, "value", [])
            if not accounts:
                continue

            top10_balances = [float(a.ui_amount) for a in accounts[:10]]
            top10_sum = sum(top10_balances)
            top10pct = (top10_sum / supply_value * 100) if supply_value > 0 else 0.0
            holders = len(accounts)
            return supply_value, holders, top10pct

        except:
            time.sleep(1)

    return 0.0, 0, 0.0

# === ALERT FORMAT ===
def format_alert(symbol, ca, mcap, volume, liquidity, top10pct, holders, age, link, twitter, telegram):
    return (
        f"🔥 *New Raydium Graduate Detected:* ${symbol}\n\n"
        f"💰 *Market Cap:* ${mcap:,.0f}\n"
        f"📊 *Volume (24h):* ${volume:,.0f}\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"🏦 *Top 10 Wallets:* {top10pct:.2f}% of supply\n"
        f"👥 *Holders:* {holders}\n"
        f"🕒 *Listed:* {age:.0f} minutes ago\n"
        f"🧩 *DEX:* Raydium\n\n"
        f"🔗 [View on DexScreener]({link})\n"
        f"🐦 [Twitter]({twitter}) | 💬 [Telegram]({telegram})\n\n"
        f"🧾 *CA:* `{ca}`"
    )

# === GET NEW RAYDIUM TOKENS ===
def get_new_raydium_tokens():
    try:
        r = requests.get(DEXSCREENER_NEW_PAIRS, timeout=15).json()
        pairs = r.get("pairs", [])
        raydium_pairs = [p for p in pairs if p.get("dexId") == "raydium"]
        new_listings = []

        for p in raydium_pairs:
            ca = p.get("baseToken", {}).get("address")
            if not ca or ca in SEEN:
                continue

            age = p.get("age", 0) / 60 if p.get("age") else 0
            if age > 10:
                continue

            SEEN.add(ca)

            symbol = p.get("baseToken", {}).get("symbol", "")
            fdv = float(p.get("fdv", 0) or 0)
            volume24h = float(p.get("volume", {}).get("h24", 0) or 0)
            liquidityUsd = float(p.get("liquidity", {}).get("usd", 0) or 0)
            socials = p.get("info", {}).get("socials", [])
            twitter = next((s["url"] for s in socials if s["type"] == "twitter"), "N/A")
            telegram = next((s["url"] for s in socials if s["type"] == "telegram"), "N/A")
            pair_url = p.get("url", "")

            if liquidityUsd < 10000 or fdv < 50000:
                continue

            new_listings.append({
                "symbol": symbol,
                "ca": ca,
                "fdv": fdv,
                "volume": volume24h,
                "liquidity": liquidityUsd,
                "age": age,
                "pair_url": pair_url,
                "twitter": twitter,
                "telegram": telegram,
            })
        return new_listings
    except:
        return []

# === BACKGROUND BOT THREAD ===
def bot_loop():
    while True:
        new_tokens = get_new_raydium_tokens()
        for t in new_tokens:
            supply, holders, top10pct = get_token_data(t["ca"])
            msg = format_alert(
                t["symbol"], t["ca"], t["fdv"], t["volume"], t["liquidity"],
                top10pct, holders, t["age"], t["pair_url"], t["twitter"], t["telegram"]
            )
            send_telegram_message(msg)
        time.sleep(60)

# === FLASK ROUTE ===
@app.route("/")
def home():
    return "🚀 Raydium Graduate Token Watcher is running!"

# === START THREAD ON RENDER ===
if __name__ == "__main__":
    thread = threading.Thread(target=bot_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
