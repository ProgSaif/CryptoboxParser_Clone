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
def parse_and_format_message(text):
    """
    Extract only valid messages:
    🎁 CODE
    👥 CODE
    Ignore extra lines like '- Sent via TeleFeed'
    Replace emoji with clickable 🧧 link.
    """

    if not text:
        return None

    # Split lines, remove empty ones
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    # Only check first real line
    first_line = lines[0]

    # Remove zero-width characters and HTML tags
    cleaned = re.sub(r'[\u200b\u200c\u200d\uFEFF]', '', first_line)
    cleaned = re.sub(r'<[^>]+>', '', cleaned).strip()

    # Match 🎁 or 👥 + CODE
    m = re.match(r'^(🎁|👥)\s*([A-Z0-9]+)$', cleaned)
    if not m:
        return None

    code = m.group(2)

    # New emoji
    emoji = "🧧"

    # Clickable link
    link = f'<a href="https://t.me/BinanceRedPacket_Hub">{emoji}</a>'

    # Final formatted output
    formatted = (
        f"{link} <code>{html.escape(code)}</code>\n\n"
        f"#Binance #RedPacket"
    )

    return formatted


# ----------------------
# Handle incoming messages
# ----------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def new_message_handler(event):
    global is_forwarding, last_forward_time

    try:
        raw_text = event.message.message or ""

        parsed = parse_and_format_message(raw_text)
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
