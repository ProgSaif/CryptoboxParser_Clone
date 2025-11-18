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

# Load .env
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
# Flask keep-alive
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
# Env helper
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

# Temp storage for pairing
last_message_data = {
    "amount": None,
    "token": None,
    "remaining": None
}

# ----------------------
# Parse Message 1 (text)
# ----------------------
def parse_message_one(text):
    """
    Looks like:
    💰 ± 2.00 BTTC
    🧧 Remaining: 9488
    """

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    # Extract amount + token
    m1 = re.search(r'±\s*([0-9]*\.?[0-9]+)\s*([A-Za-z0-9_]+)', lines[0])
    if not m1:
        return None
    amount = m1.group(1).strip()
    token = m1.group(2).strip()

    # Extract remaining
    m2 = re.search(r'Remaining:\s*(\d+)', lines[1], flags=re.IGNORECASE)
    if not m2:
        return None
    remaining = m2.group(1)

    return amount, token, remaining

# ----------------------
# Extract Claim URL from Message 2 (button link)
# ----------------------
async def extract_claim_url(event):
    """
    Message 2 has a real external URL in event.message.entities (URL preview)
    or attached web link.
    """
    msg = event.message

    # entity type: MessageEntityTextUrl or MessageEntityUrl
    if msg.entities:
        for e in msg.entities:
            if isinstance(e, types.MessageEntityTextUrl):
                return e.url
            if isinstance(e, types.MessageEntityUrl):
                # URL inside message text
                full_text = msg.raw_text
                return full_text[e.offset:e.offset + e.length]

    # Possibly via msg.media.webpage
    if msg.media and hasattr(msg.media, "webpage"):
        if hasattr(msg.media.webpage, "url") and msg.media.webpage.url:
            return msg.media.webpage.url

    return None

# ----------------------
# Build final formatted output
# ----------------------
def build_final_message(a, t, r, claim_url):
    token_link = f'<a href="https://t.me/BinanceRedPacket_Hub">{t}</a>'
    claim_link = f'<a href="{claim_url}">Claim</a>'

    formatted = (
        f"💰 ± {a} {token_link}\n"
        f"🧧 Remaining: {r}\n\n"
        f"🎁 {claim_link}"
    )
    return formatted

# ----------------------
# Forward logic
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
# Main Handler
# ----------------------
@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    global last_message_data, is_forwarding, last_forward_time

    text = event.message.message or ""

    # Detect type of message: Message 1 or Message 2
    # -----------------------------------------------------

    # ---------- MESSAGE 1: No URL, text only ----------
    if ("±" in text) and ("Remaining" in text) and ("Claim" not in text):
        parsed = parse_message_one(text)
        if parsed:
            a, t, r = parsed
            last_message_data["amount"] = a
            last_message_data["token"] = t
            last_message_data["remaining"] = r
            logging.info("Message 1 parsed successfully.")
        return

    # ---------- MESSAGE 2: Claim button ----------
    if "Claim" in text:
        claim_url = await extract_claim_url(event)

        if not claim_url:
            logging.info("Message 2 received but no claim URL found — skipping.")
            return

        a = last_message_data["amount"]
        t = last_message_data["token"]
        r = last_message_data["remaining"]

        # Only forward if pair is complete
        if not (a and t and r):
            logging.info("Incomplete pair detected — discarding.")
            last_message_data = {"amount": None, "token": None, "remaining": None}
            return

        final_msg = build_final_message(a, t, r, claim_url)

        # Reset buffer after building
        last_message_data = {"amount": None, "token": None, "remaining": None}

        # Rate limiting & queue
        now = time.time()
        if (not is_forwarding) or (now - last_forward_time > RATE_LIMIT):
            await forward_to_targets(final_msg)
            last_forward_time = now
            is_forwarding = True
            client.loop.create_task(process_queue())
        else:
            message_queue.append(final_msg)
            logging.info(f"Queued message (size={len(message_queue)})")
        return

# ----------------------
# Reconnect Handler
# ----------------------
@client.on(events.Raw)
async def raw_handler(event):
    if isinstance(event, types.UpdateConnectionState):
        if event.state == types.ConnectionState.disconnected:
            logging.warning("Disconnected. Reconnecting...")
            await asyncio.sleep(5)
            try:
                await client.connect()
            except:
                logging.exception("Reconnect failed")

# ----------------------
# Start Bot
# ----------------------
async def run_bot():
    await client.start(bot_token=BOT_TOKEN)
    logging.info("Bot started.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    keep_alive(PORT)
    asyncio.run(run_bot())
