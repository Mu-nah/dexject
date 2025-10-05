import os, re, json, requests, time, threading
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

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.devnet.solana.com")
SOLANA_KEYPAIR_JSON_PATH = os.getenv("SOLANA_KEYPAIR_JSON_PATH")
TRADE_SOL_AMOUNT = float(os.getenv("TRADE_SOL_AMOUNT", "0.01"))

TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "50"))
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))
TRAILING_SL_PCT = float(os.getenv("TRAILING_STOP_PCT", "10"))
CHECK_INTERVAL = int(os.getenv("PRICE_CHECK_INTERVAL", "30"))
TRADE_TIMEOUT = 300  # 5 minutes

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"

# Regex
CA_REGEX = re.compile(r"(?:CA|Contract)[:>\s]*([A-Za-z0-9]{32,44})")
SYMBOL_REGEX = re.compile(r"\$([A-Za-z0-9_-]{1,20})")

# Globals
trade_logs = []
total_pnl_sol = 0.0
win_count = 0
loss_count = 0

# Load wallet
def load_keypair(path):
    with open(path, "r") as f:
        return Keypair.from_bytes(bytes(json.load(f)))

keypair = load_keypair(SOLANA_KEYPAIR_JSON_PATH)
sol_client = SolanaClient(SOLANA_RPC)

def get_wallet_balance():
    try:
        bal = sol_client.get_balance(keypair.pubkey())
        return bal["result"]["value"] / 1e9
    except:
        return 0.0

# Jupiter trade
def jupiter_swap(input_mint, output_mint, amount, symbol, action="BUY"):
    try:
        params = {"inputMint": input_mint, "outputMint": output_mint,
                  "amount": int(amount), "slippageBps": 200}
        route = requests.get(JUPITER_QUOTE_API, params=params, timeout=10).json()
        if "data" not in route or not route["data"]:
            return None, f"No route for {symbol} {action}"

        swap_req = {"route": route["data"][0],
                    "userPublicKey": str(keypair.pubkey()),
                    "wrapUnwrapSOL": True}
        swap_tx = requests.post(JUPITER_SWAP_API, json=swap_req, timeout=10).json()
        if "swapTransaction" not in swap_tx:
            return None, f"Swap tx error {swap_tx}"

        tx = Transaction.from_bytes(b58decode(swap_tx["swapTransaction"]))
        resp = sol_client.send_transaction(tx, keypair, opts=TxOpts(skip_preflight=True))
        return resp["result"], f"{action} {symbol} | Tx: {resp['result']}"
    except Exception as e:
        return None, f"{action} failed {symbol}: {str(e)}"

# Monitor trade
def monitor_trade(ca, symbol, entry_price, amount_in_lamports):
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
            price = float(pairs[0].get("priceUsd", 0))
            change = (price - entry_price) / entry_price * 100

            if price > peak_price:
                peak_price = price

            reason = None
            if change >= TP_PCT:
                reason = f"TP HIT {symbol} +{change:.1f}%"
            elif change <= -SL_PCT:
                reason = f"SL HIT {symbol} {change:.1f}%"
            elif price <= peak_price * (1 - TRAILING_SL_PCT / 100):
                reason = f"Trailing SL HIT {symbol}"
            elif time.time() - start_time >= TRADE_TIMEOUT:
                reason = f"Timeout expired, selling {symbol}"

            if reason:
                txid, msg = jupiter_swap(ca, SOL_MINT, amount_in_lamports, symbol, "SELL")
                exit_price = price
                pnl_pct = (exit_price - entry_price) / entry_price * 100
                profit_in_sol = TRADE_SOL_AMOUNT * pnl_pct / 100
                total_pnl_sol += profit_in_sol
                if pnl_pct > 0: win_count += 1
                else: loss_count += 1

                trade_logs.append({
                    "symbol": symbol, "entry_price": entry_price,
                    "exit_price": exit_price, "pnl_pct": pnl_pct,
                    "profit_in_sol": profit_in_sol, "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                return
        except Exception as e:
            print("Price check error:", e)
        time.sleep(CHECK_INTERVAL)

# Telegram handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    ca_match = CA_REGEX.search(text)
    sym_match = SYMBOL_REGEX.search(text)
    if not ca_match: return
    ca = ca_match.group(1)
    symbol = sym_match.group(1).upper() if sym_match else "UNKNOWN"

    lamports = int(TRADE_SOL_AMOUNT * 1e9)
    txid, msg = jupiter_swap(SOL_MINT, ca, lamports, symbol, "BUY")
    await update.message.reply_text(msg)

    if txid:
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            entry_price = float(r.get("pairs", [{}])[0].get("priceUsd", 0))
            threading.Thread(target=monitor_trade, args=(ca, symbol, entry_price, lamports), daemon=True).start()
        except Exception as e:
            await update.message.reply_text(f"Could not fetch entry price for {symbol}: {e}")

# Daily summary
async def send_daily_summary(app: Application):
    global total_pnl_sol, win_count, loss_count, trade_logs
    sol_balance = get_wallet_balance()
    summary_msg = (
        f"📊 Daily Summary\n"
        f"Wallet Balance: {sol_balance:.6f} SOL\n"
        f"Trades: {len(trade_logs)}\n"
        f"Wins: {win_count} | Losses: {loss_count}\n"
        f"Total PnL: {total_pnl_sol:.6f} SOL"
    )
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=summary_msg)
    trade_logs, total_pnl_sol, win_count, loss_count = [], 0.0, 0, 0

def schedule_daily(app: Application):
    async def job(): await send_daily_summary(app)
    while True:
        now = datetime.now(timezone.utc) + timedelta(hours=1)  # WAT
        if now.hour == 0 and now.minute == 0:
            app.create_task(job()); time.sleep(60)
        time.sleep(30)

# Bot runner
def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    threading.Thread(target=schedule_daily, args=(app,), daemon=True).start()
    app.run_polling(timeout=10, poll_interval=2, allowed_updates=Update.ALL_TYPES)

# Flask web service (for Render health check)
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Trading bot running ✅"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
