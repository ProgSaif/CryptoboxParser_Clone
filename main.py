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

# Load .env in dev; Railway uses env vars via dashboard
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
# Flask keep-alive (Railway treats this as web process)
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
        logging.critical(f"Missing required env var: {name}")
        raise RuntimeError(f"Missing required env var: {name}")
    if val is None:
        return None
    try:
        return cast(val)
    except Exception:
        logging.critical(f"Invalid cast for env var: {name}")
        raise

API_ID = get_env("API_ID", required=True, cast=int)
API_HASH = get_env("API_HASH", required=True, cast=str)
BOT_TOKEN = get_env("BOT_TOKEN", required=True, cast=str)

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
# Telethon client and queue
# ----------------------
client = TelegramClient('bot_session', API_ID, API_HASH)
message_queue = deque()
is_forwarding = False
last_forward_time = 0.0
queue_task_running = False  # Ensure only one queue processor runs

# ----------------------
# Parsing / formatting
# ----------------------
def strip_html_tags(text):
    return re.sub(r'<[^>]+>', '', text)

def parse_and_format_message(text):
    """
    Parses raw message and returns HTML string with claim code monospace.
    """
    if not text:
        return None

    cleaned = strip_html_tags(text)
    cleaned = re.sub(r'[\u200b\u200c\u200d\uFEFF]', '', cleaned)
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    m1 = re.search(r'±\s*([0-9]*\.?[0-9]+)\s*([A-Za-z0-9_]+)', lines[0])
    if not m1:
        return None
    amount = m1.group(1).strip()
    token = html.escape(m1.group(2).strip())  # escape token

    m2 = re.search(r'Claimed:\s*(\d+)\s*/\s*(\d+)', lines[1], flags=re.IGNORECASE)
    if not m2:
        return None
    claimed = m2.group(1)
    total = m2.group(2)

    m3 = re.search(r'🎁\s*([A-Z0-9]+)', lines[2])
    if not m3:
        return None
    code = html.escape(m3.group(1).strip())

    # Only claim code is monospace
    formatted_message = (
        f"🎁 <code>{code}</code>\n"
        f"💰 Amount: {amount} {token}\n"
        f"🧧 Progress: {claimed} / {total}\n"
        f"#Binance #RedPacket"
    )
    return formatted_message

# ----------------------
# Handlers / forwarding
# ----------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def new_message_handler(event):
    global is_forwarding, last_forward_time, queue_task_running

    try:
        raw_text = event.message.message or ""

        # Skip messages with hyperlinks
        if "http://" in raw_text.lower() or "https://" in raw_text.lower() or "<a href=" in raw_text.lower():
            logging.info("Skipped message: contains hyperlink.")
            return

        parsed = parse_and_format_message(raw_text)
        if not parsed:
            logging.debug("Skipped message: format not matched.")
            return

        now = time.time()
        if (not is_forwarding) or (now - last_forward_time > RATE_LIMIT):
            await forward_to_targets(parsed)
            last_forward_time = now
            is_forwarding = True
        else:
            message_queue.append(parsed)
            logging.info(f"Message queued (size={len(message_queue)})")

        # Start background queue processor if not already running
        if not queue_task_running:
            queue_task_running = True
            client.loop.create_task(process_queue())

    except Exception:
        logging.exception("Error in new_message_handler:")

async def forward_to_targets(formatted_html):
    for ch in TARGET_CHANNELS:
        try:
            await client.send_message(
                entity=ch,
                message=formatted_html,
                parse_mode='html',
                link_preview=False
            )
            logging.info(f"Forwarded message to {ch}")
            await asyncio.sleep(1)
        except Exception:
            logging.exception(f"Failed to send to {ch}:")
            await asyncio.sleep(2)

async def process_queue():
    global is_forwarding, queue_task_running
    while message_queue:
        await asyncio.sleep(QUEUE_DELAY)
        msg = message_queue.popleft()
        await forward_to_targets(msg)
    is_forwarding = False
    queue_task_running = False

# ----------------------
# Connection / raw events
# ----------------------
@client.on(events.Raw)
async def handle_raw(event):
    if isinstance(event, types.UpdateConnectionState):
        try:
            if event.state == types.ConnectionState.disconnected:
                logging.warning("Detected disconnection, attempting reconnect...")
                await asyncio.sleep(5)
                await client.connect()
        except Exception:
            logging.exception("Error handling raw update")

# ----------------------
# Start bot
# ----------------------
async def run_bot():
    await client.start(bot_token=BOT_TOKEN)
    logging.info("Bot started and connected.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    keep_alive(PORT)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
    except Exception:
        logging.exception("Fatal error in run_bot")
