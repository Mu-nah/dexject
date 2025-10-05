# solbot_deployable.py
import os
import re
import json
import requests
import time
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from solana.rpc.api import Client as SolanaClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import Transaction
from base58 import b58decode
from datetime import datetime, timedelta, timezone

load_dotenv()

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# TELEGRAM_CHAT_ID must be the group/chat id where the bot posts results (e.g. -1001234567890)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.devnet.solana.com")
# You can provide either:
#  - SOLANA_KEYPAIR_JSON_PATH pointing to a file inside the container (preferred if you mount it)
#  - OR SOLANA_KEYPAIR_JSON env containing the keypair JSON array (will be written to /tmp/sol_key.json)
SOLANA_KEYPAIR_JSON_PATH = os.getenv("SOLANA_KEYPAIR_JSON_PATH")
SOLANA_KEYPAIR_JSON = os.getenv("SOLANA_KEYPAIR_JSON")
TRADE_SOL_AMOUNT = float(os.getenv("TRADE_SOL_AMOUNT", "0.01"))

TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "50"))   # percent
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))      # percent
TRAILING_SL_PCT = float(os.getenv("TRAILING_STOP_PCT", "10"))
TRADE_TIMEOUT = int(os.getenv("TRADE_TIMEOUT_SEC", "300"))  # seconds
CHECK_INTERVAL = int(os.getenv("PRICE_CHECK_INTERVAL", "30"))

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"

# ---------- REGEX ----------
# We only trigger when both Contract: <addr> and a token symbol $TOKEN are present.
CA_REGEX = re.compile(r"Contract:\s*\n?\s*(\S+)", re.IGNORECASE)
SYMBOL_REGEX = re.compile(r"\$([A-Za-z0-9_-]{1,20})")

# ---------- STATE ----------
trade_logs = []
total_pnl_sol = 0.0
win_count = 0
loss_count = 0

# ---------- KEYPAIR LOADING (works on Render) ----------
def ensure_keypair_file():
    # If path provided and file exists, use it.
    if SOLANA_KEYPAIR_JSON_PATH and os.path.isfile(SOLANA_KEYPAIR_JSON_PATH):
        return SOLANA_KEYPAIR_JSON_PATH

    # If full JSON is provided in env, write it to /tmp and return path
    if SOLANA_KEYPAIR_JSON:
        p = "/tmp/sol_key.json"
        with open(p, "w") as f:
            f.write(SOLANA_KEYPAIR_JSON)
        return p

    # otherwise error (we don't want to continue without wallet)
    raise RuntimeError("No SOLANA_KEYPAIR_JSON_PATH or SOLANA_KEYPAIR_JSON set in environment")

def load_keypair(path):
    with open(path, "r") as f:
        return Keypair.from_bytes(bytes(json.load(f)))

KEYPAIR_PATH = ensure_keypair_file()
keypair = load_keypair(KEYPAIR_PATH)
sol_client = SolanaClient(SOLANA_RPC)

def get_wallet_balance():
    try:
        bal = sol_client.get_balance(keypair.pubkey())
        return bal["result"]["value"] / 1e9
    except Exception:
        return 0.0

# ---------- JUPITER SWAP ----------
def jupiter_swap(input_mint, output_mint, amount, symbol, action="BUY"):
    try:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount), "slippageBps": 200}
        route = requests.get(JUPITER_QUOTE_API, params=params, timeout=12).json()
        if "data" not in route or not route["data"]:
            return None, f"No route for {symbol} {action}"

        swap_req = {"route": route["data"][0], "userPublicKey": str(keypair.pubkey()), "wrapUnwrapSOL": True}
        swap_tx = requests.post(JUPITER_SWAP_API, json=swap_req, timeout=12).json()
        if "swapTransaction" not in swap_tx:
            return None, f"Swap tx error {swap_tx}"

        tx = Transaction.from_bytes(b58decode(swap_tx["swapTransaction"]))
        resp = sol_client.send_transaction(tx, keypair, opts=TxOpts(skip_preflight=True))
        return resp["result"], f"{action} {symbol} | Tx: {resp['result']}"
    except Exception as e:
        return None, f"{action} failed {symbol}: {str(e)}"

# ---------- TRADE MONITOR ----------
def monitor_trade(app: Application, ca, symbol, entry_price, amount_in_lamports):
    global total_pnl_sol, win_count, loss_count
    peak_price = entry_price
    start_time = time.time()

    while True:
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            pairs = r.get("pairs", [])
            if not pairs:
                time.sleep(CHECK_INTERVAL)
                continue
            price = float(pairs[0].get("priceUsd", 0) or 0)
            if price <= 0:
                time.sleep(CHECK_INTERVAL)
                continue

            change = (price - entry_price) / entry_price * 100
            if price > peak_price:
                peak_price = price

            reason = None
            if change >= TP_PCT:
                reason = f"✅ TP HIT {symbol} +{change:.1f}%"
            elif change <= -SL_PCT:
                reason = f"❌ SL HIT {symbol} {change:.1f}%"
            elif price <= peak_price * (1 - TRAILING_SL_PCT / 100):
                reason = f"⚠️ Trailing SL HIT {symbol}"
            elif time.time() - start_time >= TRADE_TIMEOUT:
                reason = f"⌛ Timeout expired, selling {symbol}"

            if reason:
                txid, _ = jupiter_swap(ca, SOL_MINT, amount_in_lamports, symbol, "SELL")
                exit_price = price
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                profit_in_sol = TRADE_SOL_AMOUNT * pnl_pct / 100
                total_pnl_sol += profit_in_sol
                if pnl_pct > 0:
                    win_count += 1
                else:
                    loss_count += 1

                trade_logs.append(
                    {
                        "symbol": symbol,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl_pct": pnl_pct,
                        "profit_in_sol": profit_in_sol,
                        "reason": reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

                exit_msg = (
                    f"✅ Trade closed: ${symbol}\n"
                    f"Entry: ${entry_price:.6f} → Exit: ${exit_price:.6f}\n"
                    f"PnL: {pnl_pct:.2f}% | {profit_in_sol:+.6f} SOL\n"
                    f"Reason: {reason}\n"
                    f"Tx: {txid if txid else 'N/A'}\n"
                    f"Wallet Balance: {get_wallet_balance():.6f} SOL"
                )
                app.create_task(app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=exit_msg))
                break
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL)

# ---------- SIGNAL PROCESSOR ----------
def process_signal_text(text: str, app: Application):
    # ignore n8n footer and any messages without Contract + $SYMBOL
    if not text or "This message was sent automatically with n8n" in text:
        return False

    ca_match = CA_REGEX.search(text)
    sym_match = SYMBOL_REGEX.search(text)
    if not ca_match or not sym_match:
        # require both symbol and contract to reduce false triggers
        return False

    ca_raw = ca_match.group(1).strip()
    ca = re.sub(r"[^A-Za-z0-9]", "", ca_raw)  # sanitize
    symbol = sym_match.group(1).upper()

    # BUY
    lamports = int(TRADE_SOL_AMOUNT * 1e9)
    txid, msg = jupiter_swap(SOL_MINT, ca, lamports, symbol, "BUY")

    # immediate report to chat
    app.create_task(
        app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 Trade attempt: ${symbol}\nCA: {ca}\n{msg}")
    )

    if txid:
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            entry_price = float(r.get("pairs", [{}])[0].get("priceUsd", 0) or 0)
            if entry_price > 0:
                threading.Thread(target=monitor_trade, args=(app, ca, symbol, entry_price, lamports), daemon=True).start()
            else:
                app.create_task(app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚠️ No entry price for ${symbol} (CA:{ca})"))
        except Exception:
            app.create_task(app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⚠️ Could not fetch entry price for ${symbol} (CA:{ca})"))
    return True

# ---------- TELEGRAM HANDLER ----------
async def handle_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ""
    if update.message and update.message.text:
        text = update.message.text
    elif update.channel_post and update.channel_post.text:
        text = update.channel_post.text

    if text:
        process_signal_text(text, context.application)

# ---------- DAILY SUMMARY (12AM WAT) ----------
async def send_daily_summary(app: Application):
    global total_pnl_sol, win_count, loss_count, trade_logs
    sol_balance = get_wallet_balance()
    summary_msg = (
        f"📊 Daily Summary\n"
        f"Wallet Balance: {sol_balance:.6f} SOL\n"
        f"Trades: {len(trade_logs)}\n"
        f"Wins: {win_count} | Losses: {loss_count}\n"
        f"Total PnL: {total_pnl_sol:+.6f} SOL"
    )
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=summary_msg)
    trade_logs, total_pnl_sol, win_count, loss_count = [], 0.0, 0, 0

def schedule_daily(app: Application):
    async def job():
        await send_daily_summary(app)
    while True:
        now = datetime.now(timezone.utc) + timedelta(hours=1)  # WAT
        if now.hour == 0 and now.minute == 0:
            app.create_task(job())
            time.sleep(60)
        time.sleep(30)

# ---------- MAIN ----------
def main():
    # verify required env
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any))

    # start background schedule
    threading.Thread(target=schedule_daily, args=(app,), daemon=True).start()

    # start polling in background (allow channel_post events)
    threading.Thread(
        target=lambda: app.run_polling(allowed_updates=["message", "channel_post"], timeout=10, poll_interval=2),
        daemon=True,
    ).start()

    # Flask health endpoint for Render
    server = Flask(__name__)

    @server.route("/")
    def health():
        return "OK", 200

    port = int(os.getenv("PORT", 5000))
    server.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
