import os
import re
import json
import requests
import time
import threading
import asyncio
from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from solana.rpc.api import Client as SolanaClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import Transaction
from base58 import b58decode
from datetime import datetime, timedelta, timezone

load_dotenv()

# --- Config (from env) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")   # group id e.g. -100xxxxxxxx
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.devnet.solana.com")
SOLANA_KEYPAIR_JSON_PATH = os.getenv("SOLANA_KEYPAIR_JSON_PATH")
TRADE_SOL_AMOUNT = float(os.getenv("TRADE_SOL_AMOUNT", "0.01"))

TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "50"))   # e.g. +50%
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))      # -5%
TRAILING_SL_PCT = float(os.getenv("TRAILING_STOP_PCT", "10"))
TRADE_TIMEOUT = int(os.getenv("TRADE_TIMEOUT", "300"))  # seconds (default 5 minutes)
CHECK_INTERVAL = int(os.getenv("PRICE_CHECK_INTERVAL", "30"))

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"

# --- Regex for CA & token symbol ---
CA_REGEX = re.compile(r"(?:CA|Contract)[:>\s]*([A-Za-z0-9]{32,44})")
SYMBOL_REGEX = re.compile(r"\$([A-Za-z0-9_-]{1,20})")

# --- Globals for daily stats ---
trade_logs = []
total_pnl_sol = 0.0
win_count = 0
loss_count = 0

# --- Load wallet (safe) ---
def load_keypair(path):
    if not path:
        raise ValueError("SOLANA_KEYPAIR_JSON_PATH is not set")
    with open(path, "r") as f:
        raw = json.load(f)
        # raw should be a list of bytes values (as in solana keypair JSON)
        b = bytes(raw)
        return Keypair.from_bytes(b)

try:
    keypair = load_keypair(SOLANA_KEYPAIR_JSON_PATH)
    sol_client = SolanaClient(SOLANA_RPC)
except Exception as e:
    keypair = None
    sol_client = SolanaClient(SOLANA_RPC)
    print(f"Warning: could not load keypair on startup: {e}")

# --- Wallet balance ---
def get_wallet_balance():
    try:
        if keypair is None:
            return 0.0
        bal = sol_client.get_balance(keypair.pubkey())
        return bal["result"]["value"] / 1e9
    except Exception:
        return 0.0

# --- Jupiter swap (BUY or SELL) ---
def jupiter_swap(input_mint, output_mint, amount, symbol, action="BUY"):
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": 200
        }
        route = requests.get(JUPITER_QUOTE_API, params=params, timeout=10).json()
        if "data" not in route or not route["data"]:
            return None, f"No route for {symbol} {action}"

        swap_req = {
            "route": route["data"][0],
            "userPublicKey": str(keypair.pubkey()) if keypair else None,
            "wrapUnwrapSOL": True
        }
        swap_tx = requests.post(JUPITER_SWAP_API, json=swap_req, timeout=10).json()
        if "swapTransaction" not in swap_tx:
            return None, f"Swap tx error {swap_tx}"

        tx = Transaction.from_bytes(b58decode(swap_tx["swapTransaction"]))
        resp = sol_client.send_transaction(tx, keypair, opts=TxOpts(skip_preflight=True))
        return resp.get("result"), f"{action} {symbol} | Tx: {resp.get('result')}"
    except Exception as e:
        return None, f"{action} failed {symbol}: {str(e)}"

# --- Monitor trade (async) ---
async def monitor_trade(app: Application, ca: str, symbol: str, entry_price: float, amount_in_lamports: int):
    global total_pnl_sol, win_count, loss_count
    peak_price = entry_price
    start_time = time.time()

    while True:
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            pairs = r.get("pairs", [])
            if not pairs:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"No price for {symbol}, retrying...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            price = float(pairs[0].get("priceUsd", 0))
            change = (price - entry_price) / entry_price * 100 if entry_price else 0
            if price > peak_price:
                peak_price = price

            reason = None
            if change >= TP_PCT:
                reason = f"TP hit {symbol} +{change:.1f}%"
            elif change <= -SL_PCT:
                reason = f"SL hit {symbol} {change:.1f}%"
            elif price <= peak_price * (1 - TRAILING_SL_PCT / 100):
                reason = f"Trailing SL hit {symbol}"
            elif time.time() - start_time >= TRADE_TIMEOUT:
                reason = f"Timeout hit, selling {symbol}"

            if reason:
                txid, msg = jupiter_swap(ca, SOL_MINT, amount_in_lamports, symbol, "SELL")
                exit_price = price
                pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
                profit_in_sol = TRADE_SOL_AMOUNT * pnl_pct / 100
                total_pnl_sol += profit_in_sol
                if pnl_pct > 0:
                    win_count += 1
                else:
                    loss_count += 1

                trade_logs.append({
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "profit_in_sol": profit_in_sol,
                    "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

                sol_bal = get_wallet_balance()
                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(f"Trade closed: {symbol}\nEntry: ${entry_price:.6f} → Exit: ${exit_price:.6f}\n"
                          f"PnL: {pnl_pct:+.2f}% | {profit_in_sol:+.6f} SOL\n"
                          f"Reason: {reason}\nWallet: {sol_bal:.6f} SOL")
                )
                break

        except Exception as e:
            try:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Error monitoring {symbol}: {e}")
            except Exception:
                print(f"Error sending monitoring error message: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# --- Telegram message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "")
    ca_match = CA_REGEX.search(text)
    sym_match = SYMBOL_REGEX.search(text)
    if not ca_match:
        await update.message.reply_text("No contract address found in message.")
        return

    ca = ca_match.group(1)
    symbol = sym_match.group(1).upper() if sym_match else "UNKNOWN"

    lamports = int(TRADE_SOL_AMOUNT * 1e9)
    txid, msg = jupiter_swap(SOL_MINT, ca, lamports, symbol, "BUY")
    await update.message.reply_text(msg)

    if txid:
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            entry_price = float(r.get("pairs", [{}])[0].get("priceUsd", 0) or 0)
            # schedule the monitor_trade coroutine on the bot's loop
            # Use create_task from the Application (thread-safe)
            context.application.create_task(
                monitor_trade(context.application, ca, symbol, entry_price, lamports)
            )
        except Exception as e:
            await update.message.reply_text(f"Could not fetch entry price for {symbol}: {e}")

# --- Daily summary ---
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
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=summary_msg)
    except Exception as e:
        print("Failed to send daily summary:", e)

    trade_logs, total_pnl_sol, win_count, loss_count = [], 0.0, 0, 0

def schedule_daily(app: Application):
    # run in separate thread; schedule message when WAT midnight (UTC+1)
    while True:
        now = datetime.now(timezone.utc) + timedelta(hours=1)  # WAT
        if now.hour == 0 and now.minute == 0:
            try:
                app.create_task(send_daily_summary(app))
            except Exception as e:
                print("Failed to schedule daily summary task:", e)
            time.sleep(60)
        time.sleep(30)

# --- Start Telegram bot in background ---
tg_app: Application = None
tg_thread: threading.Thread = None

def start_telegram_bot():
    global tg_app, tg_thread
    if tg_app is not None:
        return

    if not TELEGRAM_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set; telegram bot will not start.")
        return

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # start schedule thread for daily summary
    schedule_thread = threading.Thread(target=schedule_daily, args=(tg_app,), daemon=True)
    schedule_thread.start()

    print("Starting telegram polling (background thread)...")
    # run polling (blocking) in a dedicated thread
    def run_polling():
        tg_app.run_polling(timeout=10, poll_interval=2, allowed_updates=Update.ALL_TYPES)

    tg_thread = threading.Thread(target=run_polling, daemon=True)
    tg_thread.start()

# --- Flask app for health & quick stats ---
flask_app = Flask(__name__)

@flask_app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "telegram_running": tg_app is not None})

@flask_app.route("/stats")
def stats():
    return jsonify({
        "wallet_balance": get_wallet_balance(),
        "trades_count": len(trade_logs),
        "wins": win_count,
        "losses": loss_count,
        "total_pnl_sol": total_pnl_sol
    })

# optional route to start bot manually (useful for testing)
@flask_app.route("/start-bot", methods=["POST"])
def route_start_bot():
    start_telegram_bot()
    return jsonify({"started": True})

# Start bot automatically when first request arrives (safer under Gunicorn)
@flask_app.before_first_request
def before_first_request():
    start_telegram_bot()

if __name__ == "__main__":
    # local dev
    start_telegram_bot()
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
