import os, re, json, requests, time, threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ChannelPostHandler,
    filters,
    ContextTypes,
)
from solana.rpc.api import Client as SolanaClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import Transaction
from base58 import b58decode
from datetime import datetime, timezone

load_dotenv()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # where bot posts trade messages
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.devnet.solana.com")
SOLANA_KEYPAIR_JSON_PATH = os.getenv("SOLANA_KEYPAIR_JSON_PATH")
TRADE_SOL_AMOUNT = float(os.getenv("TRADE_SOL_AMOUNT", "0.01"))

# TP/SL configs
TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "50"))
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "5"))
CHECK_INTERVAL = int(os.getenv("PRICE_CHECK_INTERVAL", "30"))
TRAILING_SL_PCT = float(os.getenv("TRAILING_STOP_PCT", "10"))
TRADE_TIMEOUT = int(os.getenv("TRADE_TIMEOUT_SEC", "300"))  # seconds (default 5 minutes)

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/"

# --- Regex (handles Contract inline or on the next line) ---
# Capture the token string following "Contract:" (non-whitespace sequence),
# works whether the address is on the same line or the next line.
CA_REGEX = re.compile(r"Contract:\s*\n?\s*(\S+)", re.IGNORECASE)
SYMBOL_REGEX = re.compile(r"\$([A-Za-z0-9_-]{1,20})")

# --- Globals (simple trade tracking) ---
trade_logs = []
total_pnl_sol = 0.0
win_count = 0
loss_count = 0

# --- Load wallet ---
def load_keypair(path):
    if not path:
        raise RuntimeError("SOLANA_KEYPAIR_JSON_PATH not set")
    with open(path, "r") as f:
        return Keypair.from_bytes(bytes(json.load(f)))

keypair = load_keypair(SOLANA_KEYPAIR_JSON_PATH)
sol_client = SolanaClient(SOLANA_RPC)


def get_wallet_balance():
    try:
        bal = sol_client.get_balance(keypair.pubkey())
        return bal["result"]["value"] / 1e9
    except Exception:
        return 0.0


# --- Jupiter swap (sync; may block briefly) ---
def jupiter_swap(input_mint, output_mint, amount, symbol, action="BUY"):
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": 200,
        }
        route = requests.get(JUPITER_QUOTE_API, params=params, timeout=10).json()
        if "data" not in route or not route["data"]:
            return None, f"No route for {symbol} {action}"

        swap_req = {
            "route": route["data"][0],
            "userPublicKey": str(keypair.pubkey()),
            "wrapUnwrapSOL": True,
        }
        swap_tx = requests.post(JUPITER_SWAP_API, json=swap_req, timeout=10).json()
        if "swapTransaction" not in swap_tx:
            return None, f"Swap tx error {swap_tx}"

        tx = Transaction.from_bytes(b58decode(swap_tx["swapTransaction"]))
        resp = sol_client.send_transaction(tx, keypair, opts=TxOpts(skip_preflight=True))
        return resp["result"], f"{action} {symbol} | Tx: {resp['result']}"
    except Exception as e:
        return None, f"{action} failed {symbol}: {str(e)}"


# --- Monitor trade (runs in a background thread) ---
def monitor_trade(ca, symbol, entry_price, amount_in_lamports, app: Application):
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
                # send result to configured chat
                app.create_task(app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=exit_msg))
                break
        except Exception:
            # silent continue; monitoring loop should be resilient
            pass
        time.sleep(CHECK_INTERVAL)


# --- central signal processor (used by both message and channel_post) ---
def process_signal_text(text: str, app: Application):
    """
    Extract contract + symbol from a block of text and attempt BUY.
    Returns True if we detected a CA and attempted a trade (regardless success).
    """
    if not text:
        return False

    ca_match = CA_REGEX.search(text)
    if not ca_match:
        return False

    raw_ca = ca_match.group(1).strip()
    # sanitize: keep only alphanumeric characters (addresses/pump tokens are alphanumeric)
    ca = re.sub(r"[^A-Za-z0-9]", "", raw_ca)

    # get token symbol if present
    sym_match = SYMBOL_REGEX.search(text)
    symbol = sym_match.group(1).upper() if sym_match else "UNKNOWN"

    # do trade (amount in lamports)
    lamports = int(TRADE_SOL_AMOUNT * 1e9)
    txid, msg = jupiter_swap(SOL_MINT, ca, lamports, symbol, "BUY")

    # send immediate result message into chat
    # msg contains success/failure text from jupiter_swap
    app.create_task(app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 Trade attempt: ${symbol}\nCA: {ca}\n{msg}"))

    if txid:
        # fetch entry price and start monitor thread
        try:
            r = requests.get(f"{DEXSCREENER_TOKEN}{ca}", timeout=10).json()
            entry_price = float(r.get("pairs", [{}])[0].get("priceUsd", 0) or 0)
            if entry_price <= 0:
                # notify that price couldn't be obtained
                app.create_task(
                    app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ Could not fetch entry price for ${symbol} (CA: {ca}). Monitoring aborted.",
                    )
                )
            else:
                threading.Thread(
                    target=monitor_trade,
                    args=(ca, symbol, entry_price, lamports, app),
                    daemon=True,
                ).start()
        except Exception:
            app.create_task(
                app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"⚠️ Could not fetch entry price for ${symbol} (CA: {ca}).",
                )
            )

    return True


# --- Tele handlers (message & channel_post) ---
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message and update.message.text else ""
    process_signal_text(text, context.application)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.channel_post.text if update.channel_post and update.channel_post.text else ""
    process_signal_text(text, context.application)


# --- Main entrypoint ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # handle normal group messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    # handle channel posts (n8n might post as a channel)
    app.add_handler(ChannelPostHandler(filters.TEXT, handle_channel_post))

    # start bot polling in background thread and include channel_post in allowed_updates
    polling_thread = threading.Thread(
        target=lambda: app.run_polling(allowed_updates=["message", "channel_post"], timeout=10, poll_interval=2),
        daemon=True,
    )
    polling_thread.start()

    # Flask health endpoint for Render (keeps port bound)
    server = Flask(__name__)

    @server.route("/")
    def home():
        return "Bot is running!", 200

    port = int(os.getenv("PORT", 5000))
    server.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
