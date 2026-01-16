import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
import re
import html
import threading
import asyncio

from flask import Flask, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
import firebase_admin
from firebase_admin import credentials, firestore

# Load environment variables from .env
load_dotenv()

# Logger setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Firebase Initialization ---
firebase_key_raw = os.getenv("FIREBASE_KEY")

if not firebase_key_raw:
    print("Error: FIREBASE_KEY not found in .env file.")
    # don't exit here; allow web health checks to work even if envs are missing
else:
    try:
        service_account_info = json.loads(firebase_key_raw.strip())
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase initialized successfully.")
    except Exception as e:
        print(f"Failed to initialize Firebase: {e}")
        print("Check if your FIREBASE_KEY in .env is a valid JSON and wrapped in single quotes.")
        db = None

# --- Configuration & States ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
UNIQUE_STRING = os.getenv("UNIQUE_STRING")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

WAITING_FOR_ADMIN_PASS = 1
WAITING_FOR_SCREENSHOT = 2

# --- Helper Functions ---

async def get_admin_id():
    """Fetches the registered admin chat ID from Firestore."""
    if not db:
        return None
    admin_ref = db.collection("config").document("admin_user").get()
    if admin_ref.exists:
        return admin_ref.to_dict().get("chat_id")
    return None

def parse_price(price_str):
    """Converts '$17.00' to 170.0 (price * 10)."""
    try:
        clean_price = float(price_str.replace('$', '').replace(',', '').strip())
        return clean_price * 10
    except (ValueError, AttributeError):
        return 0.0

def get_template_caption(doc_data):
    name = html.escape(doc_data.get('name', 'N/A'))
    price = parse_price(doc_data.get('price', '$0'))
    desc = html.escape(doc_data.get('description', 'No description'))

    return (
        f"<pre>Name: {name}</pre>\n"
        f"<pre>Price: {price:.2f} Birr</pre>\n"
        f"<pre>Description: {desc}</pre>\n"
    )

# --- Admin Registration Flow ---

async def start_admin_reg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please enter the Admin Password:")
    return WAITING_FOR_ADMIN_PASS

async def verify_admin_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == ADMIN_PASSWORD:
        chat_id = update.effective_chat.id
        if db:
            db.collection("config").document("admin_user").set({"chat_id": chat_id})
        await update.message.reply_text("✅ Registration Successful. You are now the bot admin.")
    else:
        await update.message.reply_text("❌ Incorrect password.")
    return ConversationHandler.END

# --- Daily Scheduled Task ---

def fix_drive_link(link):
    """Converts a standard Google Drive sharing link into a direct download link."""
    if not link or "drive.google.com" not in link:
        return link

    match = re.search(r'd/([^/]+)', link)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?id={file_id}"
    return link

async def check_pending_templates(context: ContextTypes.DEFAULT_TYPE):
    admin_id = await get_admin_id()
    if not admin_id:
        logging.warning("Scheduled task ran but no admin is registered yet.")
        return

    if not db:
        logging.warning("Firestore not initialized; cannot check templates.")
        return

    templates_ref = db.collection("templates").where(
        filter=firestore.FieldFilter("status", "==", "pending")
    ).stream()

    for doc in templates_ref:  # Change 'async for' to 'for'
        data = doc.to_dict()
        doc_id = doc.id
        caption = get_template_caption(data)
        photo_url = fix_drive_link(data.get('image_drive_link'))

        keyboard_rows = [
            [
                InlineKeyboardButton("Accept", callback_data=f"adm_acc_{doc_id}"),
                InlineKeyboardButton("Reject", callback_data=f"adm_rej_{doc_id}")
            ]
        ]

        preview = data.get('preview_link')
        if preview and str(preview).startswith("http"):
            keyboard_rows.append([InlineKeyboardButton("Preview", url=preview)])

        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_url,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard_rows),
                parse_mode='HTML'
            )
            db.collection("templates").document(doc_id).update({"status": "waiting"})
            logging.info(f"Post {doc_id} moved to 'waiting' status.")
        except Exception as e:
            logging.error(f"Error sending pending post {doc_id}: {e}")

# --- Admin Callbacks (Template Approval) ---

async def handle_admin_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    action, doc_id = parts[1], parts[2]
    doc_ref = db.collection("templates").document(doc_id)
    doc_snapshot = doc_ref.get()

    if not doc_snapshot.exists:
        await query.edit_message_caption(caption="❌ Error: Template no longer exists in Firestore.")
        return

    doc_data = doc_snapshot.to_dict()

    if action == "acc":
        doc_ref.update({"status": "accepted"})
        bot_info = await context.bot.get_me()
        buy_url = f"https://t.me/{bot_info.username}?start={doc_id}"

        channel_keyboard = [
            [
                InlineKeyboardButton("Preview", url=doc_data.get('preview_link')),
                InlineKeyboardButton("Buy", url=buy_url)
            ]
        ]

        await context.bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=doc_data.get('image_drive_link'),
            caption=get_template_caption(doc_data),
            reply_markup=InlineKeyboardMarkup(channel_keyboard),
            parse_mode='HTML'
        )
        await query.edit_message_caption(caption="✅ Template Posted to Channel.")

    elif action == "rej":
        doc_ref.update({"status": "rejected"})
        await query.edit_message_caption(caption="❌ Template Rejected.")

# --- Purchase Flow ---

async def start_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        doc_id = context.args[0]
        doc_ref = db.collection("templates").document(doc_id).get()

        if doc_ref.exists:
            data = doc_ref.to_dict()
            price = parse_price(data.get('price', '$0'))
            context.user_data['buying_id'] = doc_id

            payment_msg = (
                f"Please transfer the required amount to the following account:\n\n"
                f"**Account:** `1000649561382`\n"
                f"**Name:** `Jemal Hussen Hassen`\n"
                f"**Bank:** `CBE`\n"
                f"**Amount:** {price:.2f} Birr\n\n"
                f"Please upload a screenshot of your payment below."
            )
            await update.message.reply_text(payment_msg, parse_mode='Markdown')
            return WAITING_FOR_SCREENSHOT

    await update.message.reply_text("Welcome! Browse our channel to find templates to purchase.")
    return ConversationHandler.END

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = await get_admin_id()
    doc_id = context.user_data.get('buying_id')
    photo_file_id = update.message.photo[-1].file_id
    user_handle = update.effective_user.mention_markdown()

    if not admin_id:
        await update.message.reply_text("Admin is not available. Please try again later.")
        return ConversationHandler.END

    keyboard = [[
        InlineKeyboardButton("Accept Payment", callback_data=f"pay_acc_{doc_id}_{update.effective_chat.id}"),
        InlineKeyboardButton("Reject Payment", callback_data=f"pay_rej_{doc_id}_{update.effective_chat.id}")
    ]]

    await context.bot.send_photo(
        chat_id=admin_id,
        photo=photo_file_id,
        caption=f"Payment for: `{doc_id}`\nFrom: {user_handle}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    await update.message.reply_text("Screenshot received! The admin will verify it shortly.")
    return ConversationHandler.END

# --- Payment Verification Callbacks ---

async def handle_payment_verification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    action, doc_id, user_chat_id = parts[1], parts[2], parts[3]

    doc_ref = db.collection("templates").document(doc_id).get()
    data = doc_ref.to_dict()

    if action == "acc":
        download_link = data.get('zip_drive_link') or data.get('website_zip') or "Link not found."
        await context.bot.send_message(
            chat_id=user_chat_id,
            text=f"✅ Payment Verified! Here is your download link:\n\n{download_link}"
        )
        await query.edit_message_caption(caption="✅ Payment Accepted. Download link sent.")
    else:
        await context.bot.send_message(
            chat_id=user_chat_id,
            text="❌ Payment verification failed. Please re-upload a valid payment screenshot."
        )
        await query.edit_message_caption(caption="❌ Payment Rejected. User notified.")

# --- Build Application and Register Handlers (module-level) ---

application = None
if BOT_TOKEN:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Job Queue: Checks every 86400 seconds (kept from original)
    job_queue = application.job_queue
    job_queue.run_repeating(check_pending_templates, interval=86400, first=5)

    admin_reg_handler = ConversationHandler(
        entry_points=[CommandHandler(UNIQUE_STRING, start_admin_reg)],
        states={WAITING_FOR_ADMIN_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_admin_pass)]},
        fallbacks=[],
    )

    purchase_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_purchase)],
        states={WAITING_FOR_SCREENSHOT: [MessageHandler(filters.PHOTO, handle_screenshot)]},
        fallbacks=[],
    )

    application.add_handler(admin_reg_handler)
    application.add_handler(purchase_handler)
    application.add_handler(CallbackQueryHandler(handle_admin_approval, pattern="^adm_"))
    application.add_handler(CallbackQueryHandler(handle_payment_verification, pattern="^pay_"))
else:
    logging.warning("BOT_TOKEN not found; Telegram application not initialized.")

# --- Background bot runner ---

def _run_bot():
    if not application:
        logging.warning("Application not initialized; bot will not start.")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _runner():
        try:
            await application.initialize()
            await application.start()
            # Start polling (this is a coroutine and must be awaited)
            await application.updater.start_polling()
            # keep running until the process exits
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Exception in bot runner")
        finally:
            try:
                await application.updater.stop_polling()
            except Exception:
                pass
            try:
                await application.stop()
                await application.shutdown()
            except Exception:
                pass

    try:
        loop.run_until_complete(_runner())
    finally:
        loop.close()


def start_bot_in_thread():
    t = threading.Thread(target=_run_bot, daemon=True)
    t.start()
    logging.info("Bot thread started (daemon).")

# --- Flask app for Render / health checks ---
app = Flask(__name__)

@app.route("/")
def health():
    return jsonify({"status": "ok"}), 200

# optional small endpoint to check bot state
@app.route("/status")
def status():
    bot_running = application is not None
    return jsonify({"bot_initialized": bool(bot_running)}), 200

# Start the bot thread when module is imported (so gunicorn workers start it)
start_bot_in_thread()

# Local dev fallback
if __name__ == '__main__':
    # when running directly, run Flask dev server (not for production)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
