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
# Flask keep-alive (Railway will treat this as the web process)
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

# Required
API_ID = get_env("API_ID", required=True, cast=int)
API_HASH = get_env("API_HASH", required=True, cast=str)
BOT_TOKEN = get_env("BOT_TOKEN", required=True, cast=str)

# SOURCE_CHANNELS and TARGET_CHANNELS are comma-separated IDs (numeric or @username)
def parse_channel_list(s):
    if not s:
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    out = []
    for p in parts:
        # try int else keep string (username)
        try:
            out.append(int(p))
        except:
            out.append(p)
    return out

SOURCE_CHANNELS = parse_channel_list(get_env("SOURCE_CHANNELS", required=True, cast=str))
TARGET_CHANNELS = parse_channel_list(get_env("TARGET_CHANNELS", required=True, cast=str))

# Optional
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

# ----------------------
# Parsing / formatting
# ----------------------
def strip_html_tags(text):
    # remove any <a ...> or other HTML tags
    return re.sub(r'<[^>]+>', '', text)

def parse_and_format_message(text):
    """
    Accepts raw message text (possibly with Telegram HTML)
    Returns formatted HTML string wrapped for <pre> (monospace)
    or None if the message doesn't match the expected pattern.
    Pattern expected (3 lines):
      1) 💰 ± <amount> <TOKEN>
      2) 🧧 Claimed: <claimed> / <total>
      3) 🎁 <CODE>
    """

    if not text:
        return None

    # remove HTML tags first
    cleaned = strip_html_tags(text)

    # remove zero-width and stray non-printables
    cleaned = re.sub(r'[\u200b\u200c\u200d\uFEFF]', '', cleaned)

    # split into non-empty lines
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    # Line 1: amount and token
    # Accept formats like: "💰 ± 0.00001234 GUN" or "💰 ± 10.00 BTTC"
    m1 = re.search(r'±\s*([0-9]*\.?[0-9]+)\s*([A-Za-z0-9_]+)', lines[0])
    if not m1:
        return None
    amount = m1.group(1).strip()
    token = m1.group(2).strip()

    # Line 2: claimed
    m2 = re.search(r'Claimed:\s*(\d+)\s*/\s*(\d+)', lines[1], flags=re.IGNORECASE)
    if not m2:
        return None
    claimed = m2.group(1)
    total = m2.group(2)

    # Line 3: code
    m3 = re.search(r'🎁\s*([A-Z0-9]+)', lines[2])
    if not m3:
        return None
    code = m3.group(1).strip()

    # Build formatted text. We'll use an HTML <pre> block so Telegram renders monospace and users can tap-to-copy.
    # Escape any HTML entities in the content but keep the <pre> tags.
    
    
    
    body = (
		     f"🎁 Code: <code>{html.escape(code)}</code>\n"
		     f"💰 Amount: {amount} {token}\n"
		     f"🧧 Progress: {claimed} / {total}\n\n"
		     f"#Binance #RedPacketHub"
	  	)
	  	return body

    
    
    
    

# ----------------------
# Handlers / forwarding
# ----------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def new_message_handler(event):
    global is_forwarding, last_forward_time

    try:
        raw_text = event.message.message or ""
        # If the message has any explicit http(s) or inline link entity -> skip
        # Also skip messages that include telegram link tags (just in case)
        if "http://" in raw_text.lower() or "https://" in raw_text.lower() or "<a href=" in raw_text.lower():
            logging.info("Skipped message because it contains hyperlink.")
            return

        parsed = parse_and_format_message(raw_text)
        if not parsed:
            logging.debug("Message format not matched; skipping.")
            return

        now = time.time()
        if (not is_forwarding) or (now - last_forward_time > RATE_LIMIT):
            await forward_to_targets(parsed)
            last_forward_time = now
            is_forwarding = True
            # start background queue processor if there are queued items
            client.loop.create_task(process_queue())
        else:
            message_queue.append(parsed)
            logging.info(f"Message queued (size={len(message_queue)})")

    except Exception as e:
        logging.exception("Error in new_message_handler:")

async def forward_to_targets(formatted_html):
    """
    Send the preformatted HTML (<pre>..</pre>) to all target channels.
    Uses parse_mode='html' so <pre> becomes monospace.
    """
    for ch in TARGET_CHANNELS:
        try:
            await client.send_message(entity=ch, message=formatted_html, parse_mode='html', link_preview=False)
            logging.info(f"Forwarded message to {ch}")
            await asyncio.sleep(1)  # small sleep between sends
        except Exception as e:
            logging.exception(f"Failed to send to {ch}:")
            await asyncio.sleep(2)

async def process_queue():
    global is_forwarding
    while message_queue:
        await asyncio.sleep(QUEUE_DELAY)
        msg = message_queue.popleft()
        await forward_to_targets(msg)
    is_forwarding = False

# ----------------------
# Connection / raw events
# ----------------------
@client.on(events.Raw)
async def handle_raw(event):
    # Attempt basic reconnection on disconnection update
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
    # Start web thread to let Railway treat the service as web and keep it alive.
    keep_alive(PORT)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
    except Exception:
        logging.exception("Fatal error in run_bot")
