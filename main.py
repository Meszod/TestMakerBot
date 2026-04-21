import asyncio
import json
import re
import os
import uuid
import logging
import tempfile
import aiosqlite
import pdfplumber
import pytesseract
import httpx

from docx import Document
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN      = "8550766106:AAEeRmfijVT91MNTn9Ku4gMwauBijO-a47Q"
GROQ_API_KEY   = "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"
GROQ_MODEL     = "llama-3.3-70b-versatile"
ADMIN_USERNAME = "mesz0d"
BOT_USERNAME   = "testmakerAI_Robot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ============================================================
# GROQ
# ============================================================
async def groq_chat(prompt: str, max_tokens: int = 4000, temperature: float = 0.1) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=body
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

# ============================================================
# FSM
# ============================================================
class UserState(StatesGroup):
    waiting_file = State()
    waiting_text = State()
    waiting_ai   = State()

# ============================================================
# DATABASE
# ============================================================
DB = "quiz_bot.db"

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            full_name TEXT,
            correct   INTEGER DEFAULT 0,
            total     INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS shared_tests (
            share_id   TEXT PRIMARY KEY,
            owner_id   INTEGER,
            title      TEXT,
            tests_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS shared_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            share_id   TEXT,
            user_id    INTEGER,
            score      INTEGER,
            total      INTEGER,
            done_at    TEXT DEFAULT (datetime('now'))
        );
        """)
        await db.commit()

async def upsert_user(user_id, username, full_name):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, full_name=excluded.full_name
        """, (user_id, username or "", full_name or ""))
        await db.commit()

async def update_stats(user_id, correct: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            UPDATE users SET correct=correct+?, total=total+1 WHERE user_id=?
        """, (correct, user_id))
        await db.commit()

async def get_stats(user_id):
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT correct, total FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            return await cur.fetchone() or (0, 0)

async def save_shared_test(share_id, owner_id, title, tests):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT OR REPLACE INTO shared_tests (share_id, owner_id, title, tests_json)
            VALUES (?, ?, ?, ?)
        """, (share_id, owner_id, title, json.dumps(tests, ensure_ascii=False)))
        await db.commit()

async def get_shared_test(share_id):
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT title, tests_json, owner_id FROM shared_tests WHERE share_id=?",
            (share_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], json.loads(row[1]), row[2]
    return None, None, None

async def save_shared_result(share_id, user_id, score, total):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT INTO shared_results (share_id, user_id, score, total)
            VALUES (?, ?, ?, ?)
        """, (share_id, user_id, score, total))
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id, username, full_name, correct, total FROM users"
        ) as cur:
            return await cur.fetchall()

# ============================================================
# SESSION
# ============================================================
sessions: dict = {}

# ============================================================
# FILE TEXT EXTRACTION
# ============================================================
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md",
               ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

def extract_text(path: str) -> str:
    ext  = os.path.splitext(path)[1].lower()
    text = ""
    try:
        if ext == ".pdf":
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if not t or len(t.strip()) < 20:
                        img = page.to_image(resolution=300).original
                        t   = pytesseract.image_to_string(img, lang="uzb+rus+eng")
                    text += (t or "") + "\n"
        elif ext == ".docx":
            doc  = Document(path)
            text = "\n".join(p.text for p in doc.paragraphs)
        elif ext in (".txt", ".md"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"):
            img  = Image.open(path)
            text = pytesseract.image_to_string(img, lang="uzb+rus+eng")
    except Exception as e:
        log.warning(f"extract_text ({ext}): {e}")
    return text.strip()

# ============================================================
# QUESTION PARSER
# ============================================================
def parse_questions(text: str) -> list:
    questions = []
    blocks    = re.split(r'\n(?=\d{1,3}[\.\)]\s)', text)
    for b in blocks:
        q = re.search(r'\d{1,3}[\.\)]\s*(.+)', b)
        if not q:
            continue
        opts = re.findall(r'(?:^|\n)\s*([A-Da-d][\.\)]\s*.+)', b, re.MULTILINE)
        opts = [re.sub(r'^[A-Da-d][\.\)]\s*', '', o).strip() for o in opts]
        correct_match = re.search(
            r"(?:tog.ri|javob|answer|correct)[:\s]*([A-Da-d])", b, re.IGNORECASE
        )
        correct_idx = 0
        if correct_match:
            correct_idx = "abcd".index(correct_match.group(1).lower())
        if q and len(opts) >= 2:
            questions.append({
                "question": q.group(1).strip(),
                "options":  opts[:4],
                "correct":  correct_idx
            })
    return questions

# ============================================================
# AI HELPERS
# ============================================================
def clean_json(raw: str) -> str:
    raw = re.sub(r"```json|```", "", raw).strip()
    m   = re.search(r'\[.*\]', raw, re.DOTALL)
    return m.group(0) if m else raw

async def ai_fix(data: list) -> list:
    prompt = f"""Sen test savollarini tekshiruvchi yordamchisan.
Quyidagi JSON ni qabul qil, har bir savolda:
- question (string)
- options (4 ta string massiv)
- correct (0-3 orasida int)
bo'lishiga ishonch hosil qil va to'g'ri javob indeksini aniqlash.
Faqat JSON array qaytар, boshqa hech narsa yozma.

{json.dumps(data, ensure_ascii=False, indent=2)}"""
    try:
        raw   = await groq_chat(prompt, max_tokens=4000, temperature=0.1)
        fixed = json.loads(clean_json(raw))
        if isinstance(fixed, list) and fixed:
            return fixed
    except Exception as e:
        log.warning(f"ai_fix: {e}")
    return data

async def ai_extract(text: str) -> list:
    prompt = f"""Quyidagi matndagi test savollarini topib JSON formatda chiqar.
Format: [{{"question":"...","options":["A","B","C","D"],"correct":0}}]
correct — to'g'ri javobning 0-dan boshlanadigan indeksi.
Faqat JSON qaytар:

{text[:3000]}"""
    try:
        raw  = await groq_chat(prompt, max_tokens=3000, temperature=0.1)
        data = json.loads(clean_json(raw))
        if isinstance(data, list):
            return data
    except Exception as e:
        log.warning(f"ai_extract: {e}")
    return []

async def ai_generate(topic: str, count: int = 10) -> list:
    prompt = f""""{topic}" mavzusida {count} ta test savolini JSON formatda yaratgin.
Format: [{{"question":"...","options":["A variant","B variant","C variant","D variant"],"correct":0}}]
correct — to'g'ri javob indeksi (0-3).
Faqat JSON qaytар, boshqa hech narsa yozma."""
    try:
        raw  = await groq_chat(prompt, max_tokens=4000, temperature=0.7)
        data = json.loads(clean_json(raw))
        if isinstance(data, list):
            return data
    except Exception as e:
        log.warning(f"ai_generate: {e}")
    return []

# ============================================================
# KEYBOARDS
# ============================================================
LETTERS = ["A", "B", "C", "D"]

def answer_kb(options: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=f"{LETTERS[i]}) {opt[:60]}", callback_data=f"ans_{i}")
    b.adjust(1)
    return b.as_markup()

def share_kb(share_id: str) -> InlineKeyboardMarkup:
    link = f"https://t.me/{BOT_USERNAME}?start=test_{share_id}"
    b    = InlineKeyboardBuilder()
    b.button(text="📤 Do'stlarimga ulashish",
             url=f"https://t.me/share/url?url={link}&text=Bu%20testni%20sinab%20ko'ring!")
    b.button(text="🔗 Linkni ko'rish", callback_data=f"showlink_{share_id}")
    b.button(text="🏠 Asosiy menyu",   callback_data="home")
    b.adjust(1)
    return b.as_markup()

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📄 Fayl yuklash (PDF/DOCX/TXT/Rasm)", callback_data="mode_file")
    b.button(text="✍️ Matn kiritish",                    callback_data="mode_text")
    b.button(text="🤖 AI bilan test yaratish",           callback_data="mode_ai")
    b.button(text="📊 Mening statistikam",               callback_data="my_stats")
    b.adjust(1)
    return b.as_markup()

def home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Asosiy menyu", callback_data="home")
    b.adjust(1)
    return b.as_markup()

# ============================================================
# QUIZ CORE
# ============================================================
async def start_quiz(chat_id: int, uid: int, tests: list,
                     title: str = "Test", share_id: str = None):
    if not tests:
        await bot.send_message(chat_id, "❌ Savollar topilmadi.")
        return
    sessions[uid] = {
        "tests":    tests,
        "index":    0,
        "score":    0,
        "title":    title,
        "share_id": share_id,
    }
    await send_question(chat_id, uid)

async def send_question(chat_id: int, uid: int):
    d     = sessions[uid]
    idx   = d["index"]
    total = len(d["tests"])
    q     = d["tests"][idx]
    await bot.send_message(
        chat_id,
        f"📌 <b>Savol {idx+1}/{total}</b>\n\n{q['question']}",
        reply_markup=answer_kb(q["options"]),
        parse_mode="HTML"
    )

async def process_text(message: Message, text: str, title: str = "Test"):
    uid    = message.from_user.id
    status = await message.answer("🔍 Savollar aniqlanmoqda...")

    parsed = parse_questions(text)

    if not parsed:
        await status.edit_text("🧠 AI yordamida savollar izlanmoqda...")
        parsed = await ai_extract(text)

    if not parsed:
        await status.edit_text(
            "❌ Savollar topilmadi.\n\n"
            "Iltimos, quyidagi formatda yuboring:\n"
            "<code>1. Savol matni\nA) variant\nB) variant\nC) variant\nD) variant</code>",
            parse_mode="HTML"
        )
        return

    await status.edit_text(f"✅ {len(parsed)} ta savol topildi.\n🧠 AI tekshirmoqda...")
    tests = await ai_fix(parsed)

    share_id = str(uuid.uuid4())[:8]
    await save_shared_test(share_id, uid, title, tests)
    await status.edit_text(f"🚀 Test boshlanmoqda! ({len(tests)} ta savol)")
    await start_quiz(message.chat.id, uid, tests, title, share_id)

# ============================================================
# /start
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid  = message.from_user.id
    args = message.text.split(maxsplit=1)[1] if " " in message.text else ""
    await upsert_user(uid, message.from_user.username, message.from_user.full_name)

    if args.startswith("test_"):
        share_id            = args[5:]
        title, tests, owner = await get_shared_test(share_id)
        if tests:
            await message.answer(
                f"🎯 <b>{title}</b>\n📝 {len(tests)} ta savol\n\nTest boshlanmoqda...",
                parse_mode="HTML"
            )
            await start_quiz(message.chat.id, uid, tests, title, share_id)
            return
        else:
            await message.answer("❌ Bu test topilmadi yoki o'chirilgan.")

    name = message.from_user.first_name or "Do'stim"
    await message.answer(
        f"👋 Salom, <b>{name}</b>!\n\n"
        f"🤖 Men — <b>Quiz Bot</b>.\n"
        f"PDF, DOCX, TXT yoki rasmdan test tuzaman!\n\n"
        f"Quyidan tanlang 👇",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )

# ============================================================
# MENU CALLBACKS
# ============================================================
@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    name = c.from_user.first_name or "Do'stim"
    await c.message.edit_text(
        f"👋 Salom, <b>{name}</b>! Nima qilmoqchisiz?",
        reply_markup=main_menu_kb(), parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "mode_file")
async def cb_mode_file(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_file)
    await c.message.edit_text(
        "📎 <b>Faylni yuboring:</b>\n\n"
        "• PDF (skanerlangan ham bo'ladi)\n"
        "• DOCX (Word hujjati)\n"
        "• TXT / MD\n"
        "• Rasm (JPG, PNG, ...)\n\n"
        "Faylni shu chatga tashlang 👇",
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "mode_text")
async def cb_mode_text(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_text)
    await c.message.edit_text(
        "✍️ <b>Savollarni matn ko'rinishida yuboring.</b>\n\n"
        "Namuna:\n"
        "<code>1. Python nima?\n"
        "A) Dasturlash tili\n"
        "B) Operatsion tizim\n"
        "C) Ma'lumotlar bazasi\n"
        "D) Tarmoq protokoli</code>",
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "mode_ai")
async def cb_mode_ai(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_ai)
    await c.message.edit_text(
        "🤖 <b>Qaysi mavzuda test yaratay?</b>\n\n"
        "Misol: <i>Python dasturlash</i> yoki <i>O'zbekiston tarixi</i>\n\n"
        "Mavzuni yozing 👇",
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "my_stats")
async def cb_stats(c: CallbackQuery):
    uid            = c.from_user.id
    correct, total = await get_stats(uid)
    pct            = round(correct / total * 100) if total else 0
    bar            = "🟩" * (pct // 10) + "⬜" * (10 - pct // 10)
    await c.message.edit_text(
        f"📊 <b>Sizning statistikangiz</b>\n\n"
        f"✅ To'g'ri: <b>{correct}</b>\n"
        f"❌ Noto'g'ri: <b>{total - correct}</b>\n"
        f"📝 Jami: <b>{total}</b>\n"
        f"🎯 Natija: <b>{pct}%</b>\n\n"
        f"{bar}",
        reply_markup=home_kb(), parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data.startswith("showlink_"))
async def cb_show_link(c: CallbackQuery):
    share_id = c.data.split("showlink_")[1]
    link     = f"https://t.me/{BOT_USERNAME}?start=test_{share_id}"
    await c.message.answer(
        f"🔗 <b>Test linki:</b>\n\n<code>{link}</code>\n\n"
        f"Do'stlaringizga yuboring — ular ham xuddi shu testni ishlaydi!",
        parse_mode="HTML"
    )
    await c.answer()

# ============================================================
# FILE HANDLER  ✅ TUZATILDI — tempfile ishlatiladi
# ============================================================
@dp.message(UserState.waiting_file, F.document | F.photo)
async def handle_file(message: Message, state: FSMContext):
    await state.clear()

    if message.photo:
        file_obj  = message.photo[-1]
        file_name = "photo.jpg"
    else:
        file_obj  = message.document
        file_name = message.document.file_name or "file.txt"

    ext = os.path.splitext(file_name.lower())[1]
    if ext not in ALLOWED_EXT:
        return await message.answer(
            f"❌ Bu format qo'llab-quvvatlanmaydi.\n"
            f"Qabul qilinadi: {', '.join(sorted(ALLOWED_EXT))}"
        )

    tg_file = await bot.get_file(file_obj.file_id)

    # ✅ Windows va Linux ikkalasida ham ishlaydigan yo'l
    tmp_dir = tempfile.gettempdir()
    path    = os.path.join(tmp_dir, f"qb_{message.from_user.id}{ext}")

    # ✅ Papka mavjudligini tekshirib, keyin yuklab olish
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        await bot.download_file(tg_file.file_path, path)
    except Exception as e:
        log.error(f"Fayl yuklab olishda xatolik: {e}")
        return await message.answer(
            "❌ Faylni yuklab olishda xatolik yuz berdi. Qayta urinib ko'ring."
        )

    await message.answer("📖 Fayl o'qilmoqda...")
    text = extract_text(path)

    # ✅ Faylni o'chirishda xatolik bo'lsa ham davom etadi
    try:
        os.remove(path)
    except Exception:
        pass

    if not text:
        return await message.answer(
            "❌ Fayldan matn o'qib bo'lmadi.\n"
            "Boshqa format sinab ko'ring yoki /start bosing."
        )

    await process_text(message, text, title=file_name)

# ============================================================
# TEXT HANDLER
# ============================================================
@dp.message(UserState.waiting_text, F.text)
async def handle_text(message: Message, state: FSMContext):
    await state.clear()
    await process_text(message, message.text, title="Matn testi")

# ============================================================
# AI GENERATE HANDLER
# ============================================================
@dp.message(UserState.waiting_ai, F.text)
async def handle_ai(message: Message, state: FSMContext):
    await state.clear()
    topic  = message.text.strip()
    status = await message.answer(
        f"🤖 <b>{topic}</b> mavzusida test yaratilmoqda...", parse_mode="HTML"
    )
    tests = await ai_generate(topic, count=10)
    if not tests:
        return await status.edit_text(
            "❌ AI test yarata olmadi. Qayta urining yoki boshqa mavzu kiriting."
        )
    share_id = str(uuid.uuid4())[:8]
    await save_shared_test(share_id, message.from_user.id, topic, tests)
    await status.edit_text(f"✅ {len(tests)} ta savol yaratildi! 🚀")
    await start_quiz(message.chat.id, message.from_user.id, tests, topic, share_id)

# ============================================================
# ANSWER CALLBACK
# ============================================================
@dp.callback_query(F.data.startswith("ans_"))
async def handle_answer(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in sessions:
        return await c.answer("⚠️ Session topilmadi. /start bosing.", show_alert=True)

    d   = sessions[uid]
    q   = d["tests"][d["index"]]
    sel = int(c.data.split("_")[1])

    if sel == q["correct"]:
        d["score"] += 1
        result_line = "\n\n✅ <b>To'g'ri!</b>"
        await update_stats(uid, 1)
    else:
        correct_text = q["options"][q["correct"]]
        result_line  = f"\n\n❌ <b>Noto'g'ri!</b>\n💡 To'g'ri javob: <b>{correct_text}</b>"
        await update_stats(uid, 0)

    await c.message.edit_text(
        c.message.text + result_line,
        parse_mode="HTML"
    )

    d["index"] += 1

    if d["index"] < len(d["tests"]):
        await send_question(c.message.chat.id, uid)
    else:
        score = d["score"]
        total = len(d["tests"])
        pct   = round(score / total * 100)

        if pct == 100:   medal = "🏆 Mukammal natija!"
        elif pct >= 80:  medal = "🥇 A'lo!"
        elif pct >= 60:  medal = "🥈 Yaxshi!"
        elif pct >= 40:  medal = "🥉 O'rtacha"
        else:            medal = "📚 Ko'proq o'qing!"

        share_id = d.get("share_id")
        if share_id:
            await save_shared_result(share_id, uid, score, total)

        await c.message.answer(
            f"🏁 <b>{d['title']} — Yakuniy natija</b>\n\n"
            f"✅ To'g'ri: <b>{score}</b>\n"
            f"❌ Noto'g'ri: <b>{total - score}</b>\n"
            f"📊 Natija: <b>{score}/{total}</b> ({pct}%)\n\n"
            f"{medal}",
            reply_markup=share_kb(share_id) if share_id else home_kb(),
            parse_mode="HTML"
        )
        del sessions[uid]

    await c.answer()

# ============================================================
# COMMANDS
# ============================================================
@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "/start — Asosiy menyu\n"
        "/stat  — Statistikam\n"
        "/help  — Yordam\n"
        "/dev   — Dasturchi\n\n"
        "<b>Qo'llab-quvvatlanadigan fayllar:</b>\n"
        "PDF, DOCX, TXT, MD, JPG, PNG, BMP, TIFF, WEBP\n\n"
        "<b>Matn formati:</b>\n"
        "<code>1. Savol?\n"
        "A) Variant 1\nB) Variant 2\nC) Variant 3\nD) Variant 4</code>",
        parse_mode="HTML"
    )

@dp.message(Command("stat"))
async def cmd_stat(message: Message):
    correct, total = await get_stats(message.from_user.id)
    pct = round(correct / total * 100) if total else 0
    bar = "🟩" * (pct // 10) + "⬜" * (10 - pct // 10)
    await message.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"✅ To'g'ri: {correct}\n❌ Noto'g'ri: {total-correct}\n"
        f"📝 Jami: {total}\n🎯 {pct}%\n{bar}",
        parse_mode="HTML"
    )

@dp.message(Command("dev"))
async def cmd_dev(message: Message):
    b = InlineKeyboardBuilder()
    b.button(text="👨‍💻 Dasturchi", url=f"https://t.me/{ADMIN_USERNAME}")
    await message.answer(
        "🛠 <b>Yaratuvchi:</b> Sultonboyev Muhammad\n"
        f"📬 @{ADMIN_USERNAME}",
        reply_markup=b.as_markup(), parse_mode="HTML"
    )

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    users = await get_all_users()
    lines = [f"👥 Jami: <b>{len(users)}</b> foydalanuvchi\n"]
    for uid, uname, fname, cor, tot in users[:30]:
        pct = round(cor / tot * 100) if tot else 0
        lines.append(f"• {fname} (@{uname}) — {cor}/{tot} ({pct}%)")
    await message.answer("\n".join(lines), parse_mode="HTML")

# ============================================================
# FALLBACKS
# ============================================================
@dp.message(F.document | F.photo)
async def fallback_file(message: Message, state: FSMContext):
    await state.set_state(UserState.waiting_file)
    await handle_file(message, state)

@dp.message(F.text)
async def fallback_text(message: Message):
    if message.text.startswith("/"):
        return
    await message.answer(
        "Iltimos, avval rejim tanlang 👇",
        reply_markup=main_menu_kb()
    )

# ============================================================
# RUN
# ============================================================
async def main():
    await init_db()
    log.info("✅ Bot ishga tushdi!")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
