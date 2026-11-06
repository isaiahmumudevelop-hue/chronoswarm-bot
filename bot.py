import asyncio
import logging
import os
import sqlite3
import aiohttp
import edge_tts
import base64
import glob
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import yt_dlp
from pypdf import PdfReader

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from aiohttp import web

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DB_PATH = "chronoswarm.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="UTC")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

user_states = {}

# ==========================================
# 1. MAIN MENU KEYBOARD
# ==========================================
def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="🧠 New Task"), KeyboardButton(text="✅ Finish Task")],
        [KeyboardButton(text="🔄 Inject Plan"), KeyboardButton(text="📋 Status")],
        [KeyboardButton(text="📍 Share Location"), KeyboardButton(text="🔍 Find Place")],
        [KeyboardButton(text="🌤️ Weather"), KeyboardButton(text="🌐 Web Search")],
        [KeyboardButton(text="🎨 Gen Image"), KeyboardButton(text="👁️ Analyze Image")],
        [KeyboardButton(text="🎬 Analyze Video"), KeyboardButton(text="📄 Analyze PDF")],
        [KeyboardButton(text="📥 Download Video"), KeyboardButton(text="🎵 Download Music")],
        [KeyboardButton(text="💬 Send Message"), KeyboardButton(text="📇 My Contacts")],
        [KeyboardButton(text="👥 Add Contact"), KeyboardButton(text="🚀 Promote Content")],
        [KeyboardButton(text="🗣️ Voice Settings"), KeyboardButton(text="🆔 My Chat ID")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ==========================================
# 2. DATABASE HELPERS
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, goal TEXT, status TEXT DEFAULT 'active', created_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS agents (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, role TEXT, status TEXT DEFAULT 'active', ttl_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS relays (id INTEGER PRIMARY KEY AUTOINCREMENT, target_chat_id INTEGER, message TEXT, send_at TEXT, sent BOOLEAN DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS user_locations (chat_id INTEGER PRIMARY KEY, lat REAL, lon REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS contacts (owner_id INTEGER, contact_id INTEGER, name TEXT, UNIQUE(owner_id, contact_id))")
    c.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, is_verified BOOLEAN DEFAULT 0, last_contact INTEGER, last_active TEXT, voice_persona TEXT DEFAULT 'en-US-AriaNeural', referred_by INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS promos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, media_type TEXT, file_id TEXT, caption TEXT, link TEXT, views INTEGER DEFAULT 0, created_at TEXT)")
    
    try: c.execute("ALTER TABLE users ADD COLUMN voice_persona TEXT DEFAULT 'en-US-AriaNeural'")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    except sqlite3.OperationalError: pass
        
    conn.commit()
    conn.close()

def db_execute(query: str, params: tuple = ()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    conn.close()

def db_fetch(query: str, params: tuple = ()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    row = c.fetchone()
    conn.close()
    return row

def db_fetchall(query: str, params: tuple = ()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

def update_last_active(chat_id: int):
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    db_execute("UPDATE users SET last_active = ? WHERE chat_id = ?", (now, chat_id))

def get_online_status(target_id: int) -> str:
    row = db_fetch("SELECT last_active FROM users WHERE chat_id = ?", (target_id,))
    if not row or not row[0]: return "⚪ Unknown"
    try:
        diff = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
        return "🟢 Online" if diff < 300 else "🔴 Offline"
    except: return "⚪ Unknown"

async def check_scheduled_relays():
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, target_chat_id, message FROM relays WHERE sent = 0 AND send_at <= ?", (now,))
    pending = c.fetchall()
    for relay_id, target_id, msg in pending:
        try:
            await bot.send_message(target_id, f"⏰ **Scheduled Relay:**\n\n{msg}")
            c.execute("UPDATE relays SET sent = 1 WHERE id = ?", (relay_id,))
        except Exception as e:
            logging.error(f"Failed to send relay {relay_id}: {e}")
    conn.commit()
    conn.close()

# ==========================================
# 3. HELPER FUNCTIONS (AI, Voice, Maps)
# ==========================================
async def ask_ai(prompt: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "system", "content": "Be concise."}, {"role": "user", "content": prompt}],
        "max_tokens": 500
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['choices'][0]['message']['content']
                return f"AI Error: Status {resp.status}"
    except Exception as e: return f"AI failed: {str(e)}"

async def analyze_image(file_path: str, prompt: str) -> str:
    with open(file_path, "rb") as f:
        base64_image = base64.b64encode(f.read()).decode('utf-8')
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
        "max_tokens": 500
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=45) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['choices'][0]['message']['content']
                return f"Vision API Error: Status {resp.status}"
    except Exception as e: return f"Vision failed: {str(e)}"

async def transcribe_audio(file_path: str) -> str:
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    data = aiohttp.FormData()
    try:
        with open(file_path, 'rb') as f:
            file_bytes = f.read()
        data.add_field('file', file_bytes, filename=os.path.basename(file_path), content_type='audio/mpeg')
        data.add_field('model', 'whisper-large-v3')
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=60) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get('text', '')
                return f"Error transcribing audio. Status: {resp.status}"
    except Exception as e: return f"STT failed: {str(e)}"

async def generate_voice(text: str, voice: str = "en-US-AriaNeural") -> str:
    filename = f"receipt_{datetime.now().strftime('%H%M%S')}.mp3"
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(filename)
        return filename
    except Exception as e: 
        logging.error(f"TTS Error: {e}")
        return None

async def get_weather(lat: float, lon: float) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&timezone=auto"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cw = data.get("current_weather", {})
                    return f"🌤️ **Current Weather:**\n• Temp: {cw.get('temperature')}°C\n• Wind: {cw.get('windspeed')} km/h"
    except: pass
    return "❌ Weather unavailable."

async def search_places(query: str, lat: float = None, lon: float = None) -> str:
    clean_q = query.lower().replace("near me", "").replace("nearby", "").strip()
    if not clean_q: return "❌ Specify what to find."
    headers = {"User-Agent": "ChronoSwarmBot/1.0"}
    url = f"https://nominatim.openstreetmap.org/search?q={clean_q}&format=json&limit=5"
    if lat and lon:
        delta = 0.2
        url += f"&viewbox={lon-delta},{lat+delta},{lon+delta},{lat-delta}&bounded=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if not data: return "❌ No places found."
                    res = []
                    for p in data: res.append(f"📍 **{p.get('name', 'Unknown')}**\n{p.get('display_name')}\n[Map](https://www.openstreetmap.org/?mlat={p['lat']}&mlon={p['lon']})")
                    return "\n\n".join(res)
    except: pass
    return "❌ Search error."

# ==========================================
# 4. HANDLERS
# ==========================================
@dp.message(CommandStart())
async def start_handler(message: Message):
    chat_id = message.chat.id
    update_last_active(chat_id)
    
    # REFERRAL TRACKING
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = int(args[1].replace("ref_", ""))
        if referrer_id != chat_id:
            user = db_fetch("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
            if user and user[0] is None:
                db_execute("UPDATE users SET referred_by = ? WHERE chat_id = ?", (referrer_id, chat_id))
                try:
                    await bot.send_message(referrer_id, "🎉 **New Referral!** Someone just joined using your link. Keep sharing!")
                except: pass

    user = db_fetch("SELECT is_verified FROM users WHERE chat_id = ?", (chat_id,))
    if user and user[0] == 1:
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{chat_id}"
        await message.answer(
            f"🤖 **ChronoSwarm v9.0**\nWelcome back!\n\n"
            f"🔗 **Your Invite Link:**\n`{ref_link}`\n"
            f"Share this to grow the network!", 
            reply_markup=get_main_keyboard()
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ I am a Human", callback_data="verify_human")]])
        await message.answer("🛡️ **Security Check**\nPlease verify you are human.", reply_markup=kb)

@dp.callback_query(F.data == "verify_human")
async def verify_callback(callback: CallbackQuery):
    update_last_active(callback.message.chat.id)
    db_execute("UPDATE users SET is_verified = 1 WHERE chat_id = ?", (callback.message.chat.id,))
    await callback.message.edit_text("✅ Verification successful!")
    await callback.message.answer("Use the menu below:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("contact_"))
async def select_contact_callback(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[1])
    chat_id = callback.message.chat.id
    db_execute("UPDATE users SET last_contact = ? WHERE chat_id = ?", (target_id, chat_id))
    name = db_fetch("SELECT name FROM contacts WHERE owner_id = ? AND contact_id = ?", (chat_id, target_id))[0]
    await callback.message.edit_text(f"✅ Switched to **{name}** (`{target_id}`)\nStatus: {get_online_status(target_id)}")
    await callback.message.answer("Tap 💬 Send Message to type.", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("voice_"))
async def set_voice_callback(callback: CallbackQuery):
    voice = callback.data.split("_")[1]
    db_execute("UPDATE users SET voice_persona = ? WHERE chat_id = ?", (voice, callback.message.chat.id))
    await callback.message.edit_text(f"✅ Voice updated successfully!")
    await callback.answer()

@dp.message(F.location)
async def location_handler(message: Message):
    update_last_active(message.chat.id)
    lat, lon = message.location.latitude, message.location.longitude
    db_execute("INSERT OR REPLACE INTO user_locations (chat_id, lat, lon) VALUES (?, ?, ?)", (message.chat.id, lat, lon))
    await message.answer(f"📍 Location Saved!", reply_markup=get_main_keyboard())

@dp.message(F.document)
async def document_handler(message: Message):
    chat_id = message.chat.id
    update_last_active(chat_id)
    
    if not message.document.file_name.lower().endswith('.pdf'):
        await message.answer("❌ Please send a PDF file.", reply_markup=get_main_keyboard())
        return

    file_path = f"doc_{chat_id}.pdf"
    await message.answer("📄 Downloading and reading PDF...")
    try:
        await bot.download(message.document, destination=file_path)
        
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages[:10]:
            text += page.extract_text() or ""

        if not text.strip():
            await message.answer("❌ Could not extract text from this PDF. It might be a scanned image or empty.", reply_markup=get_main_keyboard())
            return

        await message.answer("🧠 Analyzing document with AI (this may take a moment)...")
        prompt = f"Here is the text from a PDF document:\n\n{text[:10000]}\n\nPlease summarize this document and highlight the key points."
        summary = await ask_ai(prompt)
        await message.answer(f"📄 **PDF Analysis:**\n\n{summary}", reply_markup=get_main_keyboard())
        
    except Exception as e:
        await message.answer(f"❌ Error reading PDF: {str(e)[:100]}", reply_markup=get_main_keyboard())
    finally:
        if os.path.exists(file_path): 
            os.remove(file_path)

@dp.message(F.photo)
async def photo_handler(message: Message):
    chat_id = message.chat.id
    update_last_active(chat_id)
    
    if user_states.get(chat_id) == "promo_link":
        user_states.pop(chat_id, None)
        caption = message.caption or "Check this out!"
        file_id = message.photo[-1].file_id
        db_execute("INSERT INTO promos (user_id, media_type, file_id, caption, created_at) VALUES (?, ?, ?, ?, ?)", 
                   (chat_id, "photo", file_id, caption, datetime.now(timezone.utc).isoformat()))
        await message.answer("✅ **Content received!** Broadcasting to the network now...", reply_markup=get_main_keyboard())
        await broadcast_promo(message, "photo", file_id, caption)
        return

    file_path = f"temp_{chat_id}.jpg"
    await bot.download(message.photo[-1], destination=file_path)
    await message.answer("👁️ Analyzing image...")
    prompt = message.caption if message.caption else "What is in this image?"
    result = await analyze_image(file_path, prompt)
    if os.path.exists(file_path): os.remove(file_path)
    await message.answer(result, reply_markup=get_main_keyboard())

@dp.message(F.video)
async def video_handler(message: Message):
    chat_id = message.chat.id
    update_last_active(chat_id)

    if user_states.get(chat_id) == "promo_link":
        user_states.pop(chat_id, None)
        if message.video.file_size and message.video.file_size > 20 * 1024 * 1024:
            await message.answer("❌ Promo video is too large (Max 20MB).", reply_markup=get_main_keyboard())
            return
        caption = message.caption or "Check this out!"
        file_id = message.video.file_id
        db_execute("INSERT INTO promos (user_id, media_type, file_id, caption, created_at) VALUES (?, ?, ?, ?, ?)", 
                   (chat_id, "video", file_id, caption, datetime.now(timezone.utc).isoformat()))
        await message.answer("✅ **Content received!** Broadcasting to the network now...", reply_markup=get_main_keyboard())
        await broadcast_promo(message, "video", file_id, caption)
        return

    if message.video.file_size and message.video.file_size > 20 * 1024 * 1024:
        await message.answer("❌ Video is too large! Telegram limits bots to downloading 20MB files directly.", reply_markup=get_main_keyboard())
        return

    file_path = f"vid_{chat_id}.mp4"
    audio_path = f"aud_{chat_id}.mp3"
    
    await message.answer("🎬 Downloading video...")
    try:
        await bot.download(message.video, destination=file_path)
    except Exception as e:
        await message.answer(f"❌ Failed to download video: {e}", reply_markup=get_main_keyboard())
        return
    
    await message.answer("🎵 Extracting audio and transcribing (this may take a moment)...")
    
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', file_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '4', audio_path, '-y',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()

        if not os.path.exists(audio_path):
            await message.answer("❌ Failed to extract audio. Is ffmpeg installed?")
            return

        if os.path.getsize(audio_path) > 24 * 1024 * 1024:
            await message.answer("❌ Audio track is too large for AI transcription (over 24MB). Try a shorter video.")
            return

        transcript = await transcribe_audio(audio_path)
        if transcript.startswith("Error") or transcript.startswith("STT"):
            await message.answer(f"❌ Transcription failed: {transcript}")
            return

        await message.answer("🧠 Audio transcribed! Generating summary...")
        prompt = f"Here is the transcript of a video: '{transcript}'. Please provide a concise summary of what happens in this video."
        summary = await ask_ai(prompt)

        response_text = f"**📝 Transcript (First 500 chars):**\n_{transcript[:500]}..._\n\n**🧠 AI Summary:**\n{summary}"
        await message.answer(response_text, reply_markup=get_main_keyboard())

    except Exception as e:
        await message.answer(f"❌ Video processing failed: {e}", reply_markup=get_main_keyboard())
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if os.path.exists(audio_path): os.remove(audio_path)

@dp.message(F.voice)
async def voice_handler(message: Message):
    update_last_active(message.chat.id)
    file_path = f"temp_{message.chat.id}.ogg"
    await bot.download(message.voice, destination=file_path)
    await message.answer("🎙️ Transcribing...")
    text = await transcribe_audio(file_path)
    if os.path.exists(file_path): os.remove(file_path)
    
    if text.startswith("Error") or text.startswith("STT"):
        await message.answer(text)
        return
        
    await message.answer(f"📝 **You said:** {text}")
    
    state = user_states.get(message.chat.id)
    if state == "new_task":
        user_states.pop(message.chat.id, None)
        await process_new_task(message, text)
    elif state == "web_search":
        user_states.pop(message.chat.id, None)
        await process_web_search(message, text)
    elif state == "gen_image":
        user_states.pop(message.chat.id, None)
        await process_gen_image(message, text)
    elif state == "dl_music":
        user_states.pop(message.chat.id, None)
        await process_download_music(message, text)
    elif state == "dl_video":
        user_states.pop(message.chat.id, None)
        await process_download_video(message, text)
    else:
        await process_new_task(message, text)

@dp.message(F.text)
async def text_router(message: Message):
    chat_id = message.chat.id
    update_last_active(chat_id)
    if not db_fetch("SELECT is_verified FROM users WHERE chat_id = ?", (chat_id,))[0]:
        await message.answer("🛡️ Please verify first.")
        return

    text = message.text
    state = user_states.get(chat_id)

    # --- BUTTONS ---
    if text == "📍 Share Location":
        await message.answer("Tap 📎 -> Location.", reply_markup=get_main_keyboard())
    elif text == "🆔 My Chat ID":
        await message.answer(f"Your ID: `{chat_id}`", reply_markup=get_main_keyboard())
    elif text == "🔍 Find Place":
        user_states[chat_id] = "find"
        await message.answer("🔍 Type the place (e.g., 'hospital'):")
    elif text == "🌤️ Weather":
        row = db_fetch("SELECT lat, lon FROM user_locations WHERE chat_id = ?", (chat_id,))
        if not row: await message.answer("⚠️ Share location first!", reply_markup=get_main_keyboard())
        else: await message.answer(await get_weather(row[0], row[1]), reply_markup=get_main_keyboard())
    elif text == "🌐 Web Search":
        user_states[chat_id] = "web_search"
        await message.answer("🌐 What do you want to search?")
    elif text == "🎨 Gen Image":
        user_states[chat_id] = "gen_image"
        await message.answer("🎨 Describe the image:")
    elif text == "🎬 Analyze Video":
        await message.answer("🎬 Please send/forward a short video file (under 20MB) for me to analyze.")
    elif text == "📄 Analyze PDF":
        await message.answer("📄 Please send me a PDF document to analyze.")
    elif text == "📥 Download Video":
        user_states[chat_id] = "dl_video"
        await message.answer("📥 Send me a link to a YouTube, TikTok, or Instagram video:")
    elif text == "🎵 Download Music":
        user_states[chat_id] = "dl_music"
        await message.answer("🎵 Send me a song name or a YouTube link:")
    elif text == "🚀 Promote Content":
        user_states[chat_id] = "promo_link"
        await message.answer("🚀 **Viral Promoter Engine**\n\nSend me a YouTube/TikTok link, OR send a photo/video with a caption to broadcast to all users!", reply_markup=get_main_keyboard())
    elif text == "🗣️ Voice Settings":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇸 Aria (US Female)", callback_data="voice_en-US-AriaNeural")],
            [InlineKeyboardButton(text="🇺🇸 Guy (US Male)", callback_data="voice_en-US-GuyNeural")],
            [InlineKeyboardButton(text="🇬🇧 Ryan (UK Male)", callback_data="voice_en-GB-RyanNeural")],
            [InlineKeyboardButton(text="🇦🇺 Natasha (AU Female)", callback_data="voice_en-AU-NatashaNeural")]
        ])
        await message.answer("🗣️ Choose a voice for receipts:", reply_markup=kb)
    elif text == "🧠 New Task":
        user_states[chat_id] = "new_task"
        await message.answer("🧠 Type the goal:")
    elif text == "🔄 Inject Plan":
        user_states[chat_id] = "inject"
        await message.answer("🔄 Type the new instruction:")
    elif text == "👥 Add Contact":
        user_states[chat_id] = "add_contact"
        await message.answer("👥 Type: `12345678 John`")
    elif text == "📇 My Contacts":
        contacts = db_fetchall("SELECT contact_id, name FROM contacts WHERE owner_id = ?", (chat_id,))
        if not contacts: 
            await message.answer("No contacts. Tap 👥 Add Contact.", reply_markup=get_main_keyboard())
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{c[1]} ({get_online_status(c[0])})", callback_data=f"contact_{c[0]}")] for c in contacts])
            await message.answer("📇 **Contacts:**", reply_markup=kb)
    elif text == "💬 Send Message":
        user_states[chat_id] = "msg"
        last = db_fetch("SELECT last_contact FROM users WHERE chat_id = ?", (chat_id,))[0]
        if last: await message.answer(f"💬 Chatting with `{last}`.\nType reply:")
        else: await message.answer("💬 No active chat. Use 📇 My Contacts.")
    elif text == "⏰ Schedule Relay":
        user_states[chat_id] = "relay"
        await message.answer("⏰ Type: `12345678 14:30 Message`")
    elif text == "✅ Finish Task":
        await process_done(message)
    elif text == "📋 Status":
        tasks = db_fetchall("SELECT id, goal, status FROM tasks WHERE chat_id = ? ORDER BY id DESC LIMIT 5", (chat_id,))
        if not tasks: await message.answer("No tasks.", reply_markup=get_main_keyboard())
        else: await message.answer("📋 **Recent Tasks**\n\n" + "\n".join([f"#{t[0]} [{t[2]}] — {t[1][:30]}..." for t in tasks]), reply_markup=get_main_keyboard())

    # --- USER INPUTS BASED ON STATE ---
    elif state == "new_task":
        user_states.pop(chat_id, None)
        await process_new_task(message, text)
    elif state == "find":
        user_states.pop(chat_id, None)
        row = db_fetch("SELECT lat, lon FROM user_locations WHERE chat_id = ?", (chat_id,))
        await message.answer(await search_places(text, row[0] if row else None, row[1] if row else None), disable_web_page_preview=True, reply_markup=get_main_keyboard())
    elif state == "inject":
        user_states.pop(chat_id, None)
        row = db_fetch("SELECT id, goal FROM tasks WHERE chat_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (chat_id,))
        if row: await message.answer(f"🔄 **Injection:**\n\n{await ask_ai(f'Goal: {row[1]}. New: {text}')}", reply_markup=get_main_keyboard())
        else: await message.answer("❌ No active task.", reply_markup=get_main_keyboard())
    elif state == "web_search":
        user_states.pop(chat_id, None)
        await process_web_search(message, text)
    elif state == "gen_image":
        user_states.pop(chat_id, None)
        await process_gen_image(message, text)
    elif state == "dl_video":
        user_states.pop(chat_id, None)
        await process_download_video(message, text)
    elif state == "dl_music":
        user_states.pop(chat_id, None)
        await process_download_music(message, text)
    elif state == "promo_link" and text.startswith("http"):
        user_states.pop(chat_id, None)
        db_execute("INSERT INTO promos (user_id, link, caption, created_at) VALUES (?, ?, ?, ?)", 
                   (chat_id, text, "Check out this link!", datetime.now(timezone.utc).isoformat()))
        await message.answer("✅ **Link received!** Broadcasting to the network now...", reply_markup=get_main_keyboard())
        await broadcast_promo(message, "link", text, "Check out this link!")
    elif state == "add_contact":
        user_states.pop(chat_id, None)
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            db_execute("INSERT OR REPLACE INTO contacts (owner_id, contact_id, name) VALUES (?, ?, ?)", (chat_id, int(parts[0]), parts[1]))
            db_execute("UPDATE users SET last_contact = ? WHERE chat_id = ?", (int(parts[0]), chat_id))
            await message.answer(f"✅ {parts[1]} saved!", reply_markup=get_main_keyboard())
        else: await message.answer("❌ Format: `12345678 John`", reply_markup=get_main_keyboard())
    elif state == "msg":
        user_states.pop(chat_id, None)
        last = db_fetch("SELECT last_contact FROM users WHERE chat_id = ?", (chat_id,))[0]
        if last:
            try:
                await bot.send_message(last, f"📩 **Message** from `{chat_id}`:\n\n{text}")
                await message.answer("✅ Delivered!", reply_markup=get_main_keyboard())
            except Exception as e: await message.answer(f"❌ Failed. Error: {e}", reply_markup=get_main_keyboard())
        else: await message.answer("❌ No contact selected.", reply_markup=get_main_keyboard())
    elif state == "relay":
        user_states.pop(chat_id, None)
        parts = text.split(maxsplit=2)
        try:
            target_id, time_str, msg_text = int(parts[0]), parts[1], parts[2]
            now = datetime.now(timezone.utc)
            send_at = datetime.combine(now.date(), datetime.strptime(time_str, "%H:%M").time(), tzinfo=timezone.utc)
            if send_at < now: send_at += timedelta(days=1)
            db_execute("INSERT INTO relays (target_chat_id, message, send_at) VALUES (?, ?, ?)", (target_id, msg_text, send_at.isoformat()))
            await message.answer(f"✅ Scheduled for {time_str} UTC.", reply_markup=get_main_keyboard())
        except: await message.answer("❌ Format: `12345678 14:30 Message`", reply_markup=get_main_keyboard())
    else:
        await process_new_task(message, text)

# ==========================================
# 5. PROMOTER BROADCAST LOGIC
# ==========================================
async def broadcast_promo(message: Message, media_type: str, file_id: str, caption: str):
    users = db_fetchall("SELECT chat_id FROM users WHERE is_verified = 1")
    count = 0
    promo_msg = f"🔥 **PROMOTED CONTENT** 🔥\n\n{caption}\n\n_Submitted by User {message.chat.id}_"
    
    for (uid,) in users:
        if uid == message.chat.id: continue
        try:
            if media_type == "photo":
                await bot.send_photo(uid, photo=file_id, caption=promo_msg)
            elif media_type == "video":
                await bot.send_video(uid, video=file_id, caption=promo_msg)
            else:
                await bot.send_message(uid, f"{promo_msg}\n{file_id}", disable_web_page_preview=False)
            count += 1
            await asyncio.sleep(0.05) 
        except: pass 
        
    await message.answer(f"📢 **Promotion Complete!** Your content was broadcasted to {count} users.")

# ==========================================
# 6. PROCESSES
# ==========================================
async def process_new_task(message: Message, goal: str):
    chat_id = message.chat.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (chat_id, goal, created_at) VALUES (?, ?, ?)", (chat_id, goal, datetime.now(timezone.utc).isoformat()))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    await message.answer(f"🧠 **Task #{task_id}** Thinking...", reply_markup=get_main_keyboard())
    await message.answer(f"**[Planner]**\n\n{await ask_ai(f'Break down this goal into 3 actionable steps: {goal}')}")

async def process_web_search(message: Message, query: str):
    await message.answer("🌐 Searching the web...", reply_markup=get_main_keyboard())
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    await message.answer("❌ Search failed. Try again.", reply_markup=get_main_keyboard())
                    return
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                results = []
                for result in soup.find_all("div", class_="result")[:3]:
                    title_tag = result.find("a", class_="result__a")
                    snippet_tag = result.find("a", class_="result__snippet")
                    if title_tag:
                        results.append(f"• **{title_tag.get_text()}**\n{snippet_tag.get_text() if snippet_tag else ''}\n[Read More]({title_tag.get('href')})\n")
                if not results:
                    await message.answer("❌ No results found.", reply_markup=get_main_keyboard())
                else:
                    await message.answer("🌐 **Search Results:**\n\n" + "\n".join(results), disable_web_page_preview=True, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Search error: {e}", reply_markup=get_main_keyboard())

async def process_gen_image(message: Message, prompt: str):
    await message.answer("🎨 Generating image...", reply_markup=get_main_keyboard())
    resolutions = [(1024, 1024), (768, 768)]
    
    for attempt, (width, height) in enumerate(resolutions):
        url = f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}?width={width}&height={height}&nologo=true"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    if resp.status == 200:
                        img_bytes = await resp.read()
                        await message.answer_photo(BufferedInputFile(img_bytes, filename="image.jpg"), caption=f"🎨 {prompt}", reply_markup=get_main_keyboard())
                        return
        except Exception as e:
            logging.warning(f"Image gen attempt {attempt+1} error: {e}")
        if attempt < len(resolutions) - 1: await asyncio.sleep(2)
            
    await message.answer("❌ Image generation failed (server overloaded). Try a simpler prompt.", reply_markup=get_main_keyboard())

async def process_download_video(message: Message, url: str):
    await message.answer("📥 Downloading video... Please wait.", reply_markup=get_main_keyboard())
    outtmpl = f"dl_{message.chat.id}_%(id)s.%(ext)s"
    
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': outtmpl,
        'quiet': True,
        'noplaylist': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'mweb']}},
    }
    
    def download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = await asyncio.to_thread(download)
        filename = info['requested_downloads'][0]['filepath']
        
        if os.path.getsize(filename) > 50 * 1024 * 1024:
            await message.answer("❌ Video is too large to send via Telegram (Limit is 50MB).", reply_markup=get_main_keyboard())
        else:
            await message.answer_video(FSInputFile(filename), caption="📥 Here is your video!", reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Download failed. Make sure the URL is valid and public.\nError: {str(e)[:100]}", reply_markup=get_main_keyboard())
    finally:
        for f in glob.glob(f"dl_{message.chat.id}_*"):
            try: os.remove(f)
            except: pass

async def process_download_music(message: Message, query: str):
    await message.answer("🎵 Searching and downloading music...", reply_markup=get_main_keyboard())
    outtmpl = f"mus_{message.chat.id}_%(id)s.%(ext)s"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True,
        'noplaylist': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'mweb']}},
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    
    def download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_q = query if query.startswith("http") else f"ytsearch1:{query}"
            return ydl.extract_info(search_q, download=True)

    try:
        info = await asyncio.to_thread(download)
        base_name = info['requested_downloads'][0]['filepath'].rsplit('.', 1)[0]
        filename = f"{base_name}.mp3"
        
        await message.answer_audio(FSInputFile(filename), title=info.get('title', query), reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"❌ Music download failed: {str(e)[:100]}", reply_markup=get_main_keyboard())
    finally:
        for f in glob.glob(f"mus_{message.chat.id}_*"):
            try: os.remove(f)
            except: pass

async def process_done(message: Message):
    row = db_fetch("SELECT id, goal FROM tasks WHERE chat_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (message.chat.id,))
    if not row: 
        await message.answer("❌ No active task.", reply_markup=get_main_keyboard())
        return
    
    task_id, goal = row
    await message.answer("[Summarizer] Finishing...", reply_markup=get_main_keyboard())
    summary = await ask_ai(f"Summarize this task in 2 sentences: {goal}")
    db_execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    
    await message.answer(f"✅ **Task #{task_id} Completed**\n\n{summary}")
    
    voice_row = db_fetch("SELECT voice_persona FROM users WHERE chat_id = ?", (message.chat.id,))
    voice = voice_row[0] if voice_row else "en-US-AriaNeural"
    
    if (voice_file := await generate_voice(summary, voice)):
        await message.answer_voice(FSInputFile(voice_file), caption="🔊 Receipt")
        os.remove(voice_file)

# ==========================================
# GLOBAL ERROR HANDLER
# ==========================================
@dp.errors()
async def error_handler(event: any):
    try:
        update = event.update
        logging.error(f"Exception while handling update: {update}")
        if update.message:
            await update.message.answer("⚠️ An unexpected error occurred. Please try again.")
    except Exception as e:
        logging.error(f"Failed to handle error: {e}")

# ==========================================
# 7. MAIN
# ==========================================
async def main():
    init_db()
    scheduler.add_job(check_scheduled_relays, 'interval', seconds=30) 
    scheduler.start()
    
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    async def handle(request): return web.Response(text="ChronoSwarm Online!")
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"ChronoSwarm is running on port {port}")
    
    await bot_task

if __name__ == "__main__":
    asyncio.run(main())
