# main.py
import os
import re
import html
import asyncio
import logging
import time
from collections import deque
from threading import Thread
from flask import Flask
from telethon import TelegramClient, events, types
from dotenv import load_dotenv

# Load .env in dev; Railway provides env vars via the dashboard
load_dotenv()

# ----------------------
# Logging
# ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')]
)

# ----------------------
# Flask keep-alive (Railway Web Process)
# ----------------------
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

def run_web(port):
    app.run(host='0.0.0.0', port=port)

def keep_alive(port):
    Thread(target=run_web, args=(port,), daemon=True).start()

# ----------------------
# Env loader + helpers
# ----------------------
def get_env(name, default=None, required=False, cast=str):
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    if val is None:
        return None
    return cast(val)

API_ID = get_env("API_ID", required=True, cast=int)
API_HASH = get_env("API_HASH", required=True)
BOT_TOKEN = get_env("BOT_TOKEN", required=True)

def parse_channel_list(s):
    if not s:
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except:
            out.append(p)
    return out

SOURCE_CHANNELS = parse_channel_list(get_env("SOURCE_CHANNELS", required=True))
TARGET_CHANNELS = parse_channel_list(get_env("TARGET_CHANNELS", required=True))

QUEUE_DELAY = get_env("QUEUE_DELAY", default="120", cast=int)
RATE_LIMIT = get_env("RATE_LIMIT", default="60", cast=int)
PORT = get_env("PORT", default="8080", cast=int)

# ----------------------
# Telethon client + queue
# ----------------------
client = TelegramClient('bot_session', API_ID, API_HASH)
message_queue = deque()
is_forwarding = False
last_forward_time = 0

# ----------------------
# Helper functions
# ----------------------
def strip_html_tags(text):
    """Remove any <a>, <b>, <i>, etc. from source message"""
    return re.sub(r'<[^>]+>', '', text)

def parse_and_format_message(event_message):
    """
    Parse new format messages:
    💰 ± AMOUNT TOKEN
    🧧 Remaining: N
    🎁 Claim (with Binance hyperlink)

    Only include Claim hyperlink, ignore token hyperlink
    """
    if not event_message.message:
        return None

    # Remove zero-width chars
    text = re.sub(r'[\u200b\u200c\u200d\uFEFF]', '', event_message.message)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    # Line 1: Amount + Token
    m1 = re.search(r'±\s*([0-9]*\.?[0-9]+)\s*([A-Za-z0-9_]+)', lines[0])
    if not m1:
        return None
    amount = m1.group(1)
    token = m1.group(2)

    # Line 2: Remaining
    m2 = re.search(r'Remaining:\s*(\d+)', lines[1], flags=re.IGNORECASE)
    if not m2:
        return None
    remaining = m2.group(1)

    # Line 3: Claim link
    claim_link = None
    if event_message.entities:
        for e in event_message.entities:
            if isinstance(e, types.MessageEntityTextUrl):
                claim_link = e.url
                break
    if not claim_link:
        # Fallback: sometimes URL may be plain text
        urls = re.findall(r'https?://\S+', lines[2])
        if urls:
            claim_link = urls[0]

    if not claim_link:
        logging.info("Skipped message: Claim link not found")
        return None

    # Format output HTML message
    formatted = (
        f"💰 {amount} {token}\n"
        f"🧧 Remaining: {remaining}\n"
        f'🎁 <a href="{claim_link}">Claim</a>\n'
        f"#Binance #RedPacket #Hub"
    )
    return formatted

# ----------------------
# Handle incoming messages
# ----------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def new_message_handler(event):
    global is_forwarding, last_forward_time

    try:
        # Skip any messages containing hyperlinks other than Claim (safety)
        raw_text = event.message.message or ""
        if "<a href=" in raw_text.lower() and not "🎁" in raw_text:
            logging.info("Skipped message due to unrelated hyperlink.")
            return

        parsed = parse_and_format_message(event.message)
        if not parsed:
            return

        now = time.time()
        if (not is_forwarding) or (now - last_forward_time > RATE_LIMIT):
            await forward_to_targets(parsed)
            last_forward_time = now
            is_forwarding = True
            client.loop.create_task(process_queue())
        else:
            message_queue.append(parsed)
            logging.info(f"Queued message (size={len(message_queue)})")

    except Exception:
        logging.exception("Error in handler:")

# ----------------------
# Forward messages
# ----------------------
async def forward_to_targets(html_message):
    for ch in TARGET_CHANNELS:
        try:
            await client.send_message(
                entity=ch,
                message=html_message,
                parse_mode='html',
                link_preview=False
            )
            await asyncio.sleep(1)
        except Exception:
            logging.exception(f"Failed to send to {ch}")
            await asyncio.sleep(2)

async def process_queue():
    global is_forwarding
    while message_queue:
        await asyncio.sleep(QUEUE_DELAY)
        msg = message_queue.popleft()
        await forward_to_targets(msg)
    is_forwarding = False

# ----------------------
# Reconnection handler
# ----------------------
@client.on(events.Raw)
async def handle_raw(event):
    if isinstance(event, types.UpdateConnectionState):
        if event.state == types.ConnectionState.disconnected:
            logging.warning("Disconnected. Reconnecting...")
            await asyncio.sleep(5)
            try:
                await client.connect()
            except:
                logging.exception("Reconnect failed")

# ----------------------
# Start bot
# ----------------------
async def run_bot():
    await client.start(bot_token=BOT_TOKEN)
    logging.info("Bot started.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    keep_alive(PORT)
    asyncio.run(run_bot())
