import os
import time
import requests
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# === CONFIG ===
app = Flask(__name__)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-api.projectserum.com",
]
DEXSCREENER_URL = "https://api.dexscreener.io/latest/dex/search?q=solana"
SEEN = set()


# === TELEGRAM ===
def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass


# === ON-CHAIN DATA ===
def get_token_data(ca):
    for rpc in RPC_URLS:
        try:
            client = Client(rpc)
            mint = Pubkey.from_string(ca)
            supply_resp = client.get_token_supply(mint)
            supply_value = float(supply_resp.value.ui_amount)

            largest_resp = client.get_token_largest_accounts(mint)
            accounts = largest_resp.value or []
            top10 = sum(float(a.ui_amount) for a in accounts[:10])
            holders = len(accounts)
            top10pct = (top10 / supply_value) * 100 if supply_value > 0 else 0
            return supply_value, holders, top10pct
        except:
            time.sleep(0.5)
    return 0.0, 0, 0.0


# === ALERT MESSAGE ===
def format_alert(symbol, ca, mcap, volume, liquidity, top10pct, holders, age, dex, link):
    return (
        f"🚀 *New Pump.fun Graduate Found*\n\n"
        f"💎 *Token:* ${symbol}\n"
        f"💰 *Market Cap:* ${mcap:,.0f}\n"
        f"📊 *Volume (24h):* ${volume:,.0f}\n"
        f"💧 *Liquidity:* ${liquidity:,.0f}\n"
        f"🏦 *Top 10 Wallets:* {top10pct:.1f}% of supply\n"
        f"👥 *Holders:* {holders}\n"
        f"🕒 *Age:* {age:.0f} min\n"
        f"🧩 *DEX:* {dex.capitalize()}\n\n"
        f"🔗 [View on DexScreener]({link})\n\n"
        f"🧾 *CA:* `{ca}`"
    )


# === FETCH RECENT GRADUATES ===
def get_recent_pumpfun_graduates():
    try:
        r = requests.get(DEXSCREENER_URL, timeout=15)
        pairs = r.json().get("pairs", [])
    except:
        return []

    new_tokens = []
    for p in pairs:
        dex = p.get("dexId", "")
        if dex not in ["raydium", "pumpswap"]:
            continue

        ca = p.get("baseToken", {}).get("address", "")
        if not ca or ca in SEEN:
            continue

        age_min = (p.get("age", 0) or 0) / 60
        fdv = float(p.get("fdv", 0) or 0)
        volume = float(p.get("volume", {}).get("h24", 0) or 0)
        liquidity = float(p.get("liquidity", {}).get("usd", 0) or 0)

        if not (80_000 <= fdv <= 300_000 and volume >= 200_000 and age_min <= 60):
            continue

        supply, holders, top10pct = get_token_data(ca)
        if holders < 250 or top10pct > 25:
            continue

        SEEN.add(ca)
        new_tokens.append({
            "symbol": p.get("baseToken", {}).get("symbol", ""),
            "ca": ca,
            "fdv": fdv,
            "volume": volume,
            "liquidity": liquidity,
            "top10pct": top10pct,
            "holders": holders,
            "age": age_min,
            "dex": dex,
            "url": p.get("url", "")
        })
    return new_tokens


# === MAIN LOOP ===
def run_bot():
    while True:
        try:
            tokens = get_recent_pumpfun_graduates()
            for t in tokens:
                msg = format_alert(
                    t["symbol"], t["ca"], t["fdv"], t["volume"],
                    t["liquidity"], t["top10pct"], t["holders"], t["age"], t["dex"], t["url"]
                )
                send_telegram_message(msg)
        except Exception as e:
            send_telegram_message(f"⚠️ Bot Error: {e}")
        time.sleep(90)


@app.route("/")
def home():
    return "Pump.fun Graduate Watcher is running!"


if __name__ == "__main__":
    import threading
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
