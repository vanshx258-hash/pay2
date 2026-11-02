import os
import sqlite3
import logging
import threading
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import qrcode

# ================== CONFIG ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "8586849798"))
PORT = int(os.environ.get("PORT", 10000))

# Tera Group ID (Sahi format me)
SMS_GROUP_ID = int(os.environ.get("SMS_GROUP_ID", "-1005351620091")) 

(ASK_UPI_ID,) = range(1)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== PORT SERVER ==================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-type', 'text/html'); self.end_headers()
        self.wfile.write(b"Payment Bot is Running!")
    def log_message(self, format, *args): pass

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

# ================== DATABASE ==================
def init_db():
    conn = sqlite3.connect("payment_data.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        status TEXT DEFAULT 'PENDING',
        txn_id TEXT,
        time TEXT
    )""")
    conn.commit()
    conn.close()
init_db()

def db_execute(query, params=()):
    conn = sqlite3.connect("payment_data.db")
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    res = c.fetchall()
    conn.close()
    return res

def get_setting(key, default=None):
    res = db_execute("SELECT value FROM settings WHERE key=?", (key,))
    return res[0][0] if res else default

def set_setting(key, value):
    db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

# ================== BOT HANDLERS ==================
bot_app = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("SET UPI ID", callback_data="set_upi")],
        [InlineKeyboardButton("PAY 1 RS", callback_data="pay_1")]
    ]
    upi = get_setting("upi_id", "Not Set")
    await update.message.reply_text(f"Welcome!\n\nCurrent UPI: {upi}\n\nSelect an option:", reply_markup=InlineKeyboardMarkup(kb))

# --- SET UPI ---
async def set_upi_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Apni UPI ID daal (jaise: name@upi):")
    return ASK_UPI_ID

async def save_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi_id = update.message.text.strip()
    set_setting("upi_id", upi_id)
    await update.message.reply_text(f"UPI ID set to: {upi_id}")
    return ConversationHandler.END

# --- PAYMENT ---
async def pay_1rs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    upi_id = get_setting("upi_id")
    if not upi_id or upi_id == "Not Set":
        return await query.edit_message_text("Pehle UPI ID set kar!")
    
    amount = 1.00
    user_id = query.from_user.id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Create QR Code
    upi_url = f"upi://pay?pa={upi_id}&pn=Owner&am={amount}&cu=INR"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    
    # Save pending payment
    db_execute("INSERT INTO payments (user_id, amount, status, time) VALUES (?,?,?,?)", (user_id, amount, "PENDING", now))
    
    await query.edit_message_text(f"Pay Rs {amount} to:\n`{upi_id}`\n\nAfter paying, click 'Refresh Status'", parse_mode="Markdown")
    await context.bot.send_photo(chat_id=query.message.chat_id, photo=InputFile(bio, filename="qr.png"))
    
    kb = [[InlineKeyboardButton("REFRESH STATUS", callback_data="check_status")]]
    await context.bot.send_message(chat_id=query.message.chat_id, text="Waiting for payment...", reply_markup=InlineKeyboardMarkup(kb))

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    res = db_execute("SELECT status, txn_id FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    if res and res[0][0] == "VERIFIED":
        await query.edit_message_text(f"Payment Auto Verified!\nTxn ID: {res[0][1]}")
    else:
        await query.edit_message_text("Payment not received yet. Please wait or pay again.")

# ================== GROUP SMS READER ==================
async def group_sms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    text = update.message.text
    logger.info(f"Group Message Received: {text}")
    
    # Regex to find amount (Rs.1.00 or Rs 1)
    amount_match = re.search(r'(?:Rs\.?|INR)\s*([0-9]+(?:\.[0-9]{1,2})?)', text)
    # Regex to find Txn ID
    txn_match = re.search(r'(?:Txn ID|Txn|Ref)\s*:?\s*([A-Za-z0-9]+)', text)
    
    if amount_match:
        amount = float(amount_match.group(1))
        txn_id = txn_match.group(1) if txn_match else "N/A"
        
        logger.info(f"Amount detected: {amount}, Txn: {txn_id}")
        
        # Find pending payment
        pending = db_execute("SELECT id, user_id FROM payments WHERE amount=? AND status='PENDING' ORDER BY id ASC LIMIT 1", (amount,))
        
        if pending:
            payment_id, user_id = pending[0]
            db_execute("UPDATE payments SET status='VERIFIED', txn_id=? WHERE id=?", (txn_id, payment_id))
            
            # Notify user
            try:
                await bot_app.bot.send_message(
                    chat_id=user_id, 
                    text=f"Payment Auto Verified!\nAmount: Rs {amount}\nTxn ID: {txn_id}"
                )
                logger.info(f"User {user_id} notified about payment {txn_id}")
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")

# ================== MAIN ==================
def main():
    global bot_app
    threading.Thread(target=run_server, daemon=True).start()
    
    bot_app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_upi_start, pattern="^set_upi$")],
        states={ASK_UPI_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_upi)]},
        fallbacks=[]
    )
    
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(conv_handler)
    bot_app.add_handler(CallbackQueryHandler(pay_1rs, pattern="^pay_1$"))
    bot_app.add_handler(CallbackQueryHandler(check_status, pattern="^check_status$"))
    
    # Group Message Handler (Using Chat ID Filter)
    bot_app.add_handler(MessageHandler(filters.TEXT & filters.Chat(chat_id=SMS_GROUP_ID), group_sms_handler))
    
    logger.info(f"Payment Bot Started! Monitoring Group: {SMS_GROUP_ID}")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
