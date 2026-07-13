import os
import re
import sys
import json
import asyncio
import logging
import httpx
from io import BytesIO
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    MenuButtonWebApp,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if not TELEGRAM_TOKEN or not GEMINI_KEY:
    missing = [name for name, val in
               (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("GEMINI_API_KEY", GEMINI_KEY)) if not val]
    sys.exit(f"ERROR: missing required environment variable(s): {', '.join(missing)}")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODEL = "gemini-flash-latest"          # stable alias — avoids 404 from retired versions

QUIZ_N = 5
FLASHCARD_N = 8
user_mode = {}         # chat_id -> mode
quiz_state = {}        # chat_id -> {"questions":[...], "idx":int, "score":int}
flashcard_state = {}   # chat_id -> {"cards":[...], "idx":int}


# ---------------- Gemini ----------------
async def gemini_text(prompt: str, temperature: float = 0.4) -> str:
    url = f"{GEMINI}/{TEXT_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature}}
    async with httpx.AsyncClient(timeout=120) as c:
        for attempt in range(5):
            r = await c.post(url, json=body)
            if r.status_code == 503 and attempt < 4:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            break
    data = r.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


async def gemini_quiz(source: str) -> list:
    """Ask Gemini for quiz questions as strict JSON, return a list of dicts."""
    prompt = (
        f"You are writing an exam-quality quiz for a pharmacy/medical student. "
        f"Create up to {QUIZ_N} multiple-choice questions based ONLY on the text "
        "below — if the text doesn't contain enough distinct facts for that many "
        "non-overlapping questions, create fewer rather than padding or repeating. "
        "Every question must be answerable strictly from the text, with exactly "
        "one clearly correct option among the four — the other three must be "
        "plausible but unambiguously wrong. Do not invent facts, numbers, or "
        "mechanisms not stated in the text. Double-check that the 'answer' index "
        "actually points to the correct option before responding.\n\n"
        "Return STRICT JSON only — no markdown, no code fences, no commentary, "
        "no trailing commas. Schema:\n"
        '[{"q":"question text","options":["opt A","opt B","opt C","opt D"],'
        '"answer":0,"explain":"one line why, quoting or referencing the text"}]\n'
        '"answer" is the 0-based index of the correct option.\n\n'
        f"TEXT:\n{source}"
    )
    raw = await gemini_text(prompt, temperature=0.25)
    # strip accidental code fences
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    # grab the JSON array
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    data = json.loads(raw)
    clean = []
    for item in data:
        opts = item.get("options", [])
        ans = int(item.get("answer", 0))
        if len(opts) == 4 and 0 <= ans <= 3 and item.get("q"):
            clean.append({"q": item["q"], "options": opts, "answer": ans,
                          "explain": item.get("explain", "")})
    return clean


async def gemini_flashcards(source: str) -> list:
    """Ask Gemini for flashcards as strict JSON, return a list of dicts."""
    prompt = (
        f"You are writing a study flashcard deck for a pharmacy/medical student. "
        f"Create up to {FLASHCARD_N} flashcards based ONLY on the text below — if "
        "the text doesn't support that many distinct, non-overlapping facts, "
        "create fewer rather than padding or repeating the same idea twice. "
        "Each card must test exactly one fact — never combine multiple facts on "
        "one card. The 'front' is a single term or question; the 'back' is the "
        "precise answer, stated only using information present in the text. "
        "Do not invent facts, numbers, or mechanisms not stated in the text.\n\n"
        "Return STRICT JSON only — no markdown, no code fences, no commentary, "
        "no trailing commas. Schema:\n"
        '[{"front":"term or question","back":"concise answer or definition"}]\n'
        "Keep each side under 200 characters.\n\n"
        f"TEXT:\n{source}"
    )
    raw = await gemini_text(prompt, temperature=0.25)
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    data = json.loads(raw)
    clean = []
    for item in data:
        front, back = item.get("front"), item.get("back")
        if front and back:
            clean.append({"front": front, "back": back})
    return clean


# ---------------- File extraction ----------------
async def extract_file_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    doc = update.message.document
    if not doc:
        return None
    name = (doc.file_name or "").lower()
    tg_file = await ctx.bot.get_file(doc.file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    data = buf.getvalue()
    try:
        if name.endswith((".txt", ".md")):
            return data.decode("utf-8", errors="ignore")
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        if name.endswith(".docx"):
            import docx
            d = docx.Document(BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
    except Exception as e:
        logging.warning(f"extract failed: {e}")
    return None


# ---------------- Guard / UI ----------------
def allowed(update: Update) -> bool:
    return not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS


async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    await update.effective_message.reply_text(f"Not authorized. Your ID: {update.effective_user.id}")
    return False


def menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton("📝 Quiz"), KeyboardButton("🗂 Flashcards")],
            [KeyboardButton("📄 Summary"), KeyboardButton("💬 Text")]]
    if WEBAPP_URL:
        rows.insert(0, [KeyboardButton("✨ Open App", web_app=WebAppInfo(url=WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


MODE_MSG = {
    "quiz": "📝 Quiz mode. Paste text or upload a .txt / .pdf / .docx — I'll build an interactive quiz.",
    "flashcards": "🗂 Flashcard mode. Paste text or upload a file — I'll build a flashcard deck.",
    "summary": "📄 Summary mode. Paste text or upload a file to summarize.",
    "text": "💬 Text mode. Ask me anything.",
}
LETTERS = ["A", "B", "C", "D"]


# ---------------- Quiz flow ----------------
async def send_question(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    st = quiz_state[chat_id]
    q = st["questions"][st["idx"]]
    n = st["idx"] + 1
    total = len(st["questions"])
    buttons = [[InlineKeyboardButton(f"{LETTERS[i]}) {opt}", callback_data=f"ans:{i}")]
               for i, opt in enumerate(q["options"])]
    await ctx.bot.send_message(
        chat_id,
        f"*Question {n}/{total}*\n\n{q['q']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    st = quiz_state.get(chat_id)
    if not st:
        await query.edit_message_text("This quiz has ended. Send new text to start another. 📝")
        return

    chosen = int(query.data.split(":")[1])
    q = st["questions"][st["idx"]]
    correct = q["answer"]
    right = chosen == correct

    if right:
        st["score"] += 1
        head = f"✅ Correct! {LETTERS[correct]}) {q['options'][correct]}"
    else:
        head = (f"❌ You chose {LETTERS[chosen]}. "
                f"Correct: {LETTERS[correct]}) {q['options'][correct]}")
    body = f"\n_{q['explain']}_" if q.get("explain") else ""
    await query.edit_message_text(f"{head}{body}", parse_mode="Markdown")

    st["idx"] += 1
    if st["idx"] < len(st["questions"]):
        await send_question(chat_id, ctx)
    else:
        score, total = st["score"], len(st["questions"])
        pct = round(100 * score / total)
        if pct == 100:
            note = "Perfect! 🏆"
        elif pct >= 70:
            note = "Great work! 💪"
        elif pct >= 40:
            note = "Good effort — review and retry. 📚"
        else:
            note = "Keep studying — you'll get there. 🌱"
        await ctx.bot.send_message(
            chat_id,
            f"🎯 *Quiz complete!*\nScore: {score}/{total} ({pct}%)\n{note}\n\n"
            "Send more text or a file to make another quiz.",
            parse_mode="Markdown",
        )
        quiz_state.pop(chat_id, None)


async def start_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, "typing")
    try:
        questions = await gemini_quiz(source)
    except json.JSONDecodeError:
        await update.message.reply_text("Couldn't build a clean quiz from that. Try clearer or shorter text.")
        return
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"Gemini error ({e.response.status_code}). Check key/quota.")
        return
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if not questions:
        await update.message.reply_text("No usable questions came back — try more detailed text.")
        return
    quiz_state[chat_id] = {"questions": questions, "idx": 0, "score": 0}
    await update.message.reply_text(f"📝 Quiz ready — {len(questions)} questions. Tap your answers!")
    await send_question(chat_id, ctx)


# ---------------- Flashcard flow ----------------
async def send_flashcard(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    st = flashcard_state[chat_id]
    card = st["cards"][st["idx"]]
    n = st["idx"] + 1
    total = len(st["cards"])
    buttons = [[InlineKeyboardButton("👁 Show answer", callback_data="flip")]]
    await ctx.bot.send_message(
        chat_id,
        f"*Card {n}/{total}*\n\n{card['front']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_flip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    st = flashcard_state.get(chat_id)
    if not st:
        await query.edit_message_text("This flashcard deck has ended. Send new text to start another. 🗂")
        return
    card = st["cards"][st["idx"]]
    n = st["idx"] + 1
    total = len(st["cards"])
    is_last = n == total
    buttons = [[InlineKeyboardButton("🎉 Done" if is_last else "➡️ Next", callback_data="nextcard")]]
    await query.edit_message_text(
        f"*Card {n}/{total}*\n\n{card['front']}\n\n💡 {card['back']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_next_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    st = flashcard_state.get(chat_id)
    if not st:
        return
    st["idx"] += 1
    if st["idx"] < len(st["cards"]):
        await send_flashcard(chat_id, ctx)
    else:
        await ctx.bot.send_message(
            chat_id,
            "🎉 *Flashcard deck complete!*\nSend more text or a file to make another deck.",
            parse_mode="Markdown",
        )
        flashcard_state.pop(chat_id, None)


async def start_flashcards(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, "typing")
    try:
        cards = await gemini_flashcards(source)
    except json.JSONDecodeError:
        await update.message.reply_text("Couldn't build a clean deck from that. Try clearer or shorter text.")
        return
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(f"Gemini error ({e.response.status_code}). Try again in a moment.")
        return
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if not cards:
        await update.message.reply_text("No usable flashcards came back — try more detailed text.")
        return
    flashcard_state[chat_id] = {"cards": cards, "idx": 0}
    await update.message.reply_text(f"🗂 Flashcards ready — {len(cards)} cards. Tap to reveal!")
    await send_flashcard(chat_id, ctx)


# ---------------- Other modes ----------------
async def send_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def do_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    prompt = (
        "Summarize the text below for a pharmacy/medical student: a 2-sentence "
        "overview, then 5-8 key bullet points, then a 'High-yield:' line with the "
        "single most important takeaway. Use ONLY information present in the "
        "text — do not add outside facts, context, or knowledge not stated there. "
        "If the text is ambiguous, incomplete, or too short to summarize "
        "meaningfully, say so plainly instead of filling gaps with guesses.\n\n"
        "TEXT:\n" + source
    )
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        out = await gemini_text(prompt, temperature=0.2)
    except httpx.HTTPStatusError as e:
        out = f"Gemini is temporarily overloaded ({e.response.status_code}). Please try again in a moment."
    except Exception as e:
        out = f"Error: {e}"
    await send_long(update, out)


async def route_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    mode = user_mode.get(update.effective_chat.id, "quiz")
    if mode in ("quiz", "flashcards", "summary") and len(source.strip()) < 20:
        await update.message.reply_text("Please send more text (a few sentences at least).")
        return
    if len(source) > 20000:
        source = source[:20000]
        await update.message.reply_text("ℹ️ Text was long — using the first part only.")
    if mode == "quiz":
        await start_quiz(update, ctx, source)
    elif mode == "flashcards":
        await start_flashcards(update, ctx, source)
    elif mode == "summary":
        await do_summary(update, ctx, source)
    else:
        prompt = (
            "You are a knowledgeable pharmacy/medical study assistant. Answer "
            "the question below clearly and accurately. If you are not "
            "confident about a fact, say so explicitly rather than guessing. "
            "Keep the answer focused and well-structured.\n\n"
            f"QUESTION:\n{source}"
        )
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        try:
            out = await gemini_text(prompt, temperature=0.4)
        except httpx.HTTPStatusError as e:
            out = f"Gemini is temporarily overloaded ({e.response.status_code}). Please try again in a moment."
        except Exception as e:
            out = f"Error: {e}"
        await send_long(update, out)


# ---------------- Handlers ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_mode[update.effective_chat.id] = "quiz"
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"Hi {name}! ✦ I turn your notes into study material.\n\n"
        "📝 *Quiz* — interactive multiple-choice (tap answers)\n"
        "🗂 *Flashcards* — tap to reveal each answer\n"
        "📄 *Summary* — key points\n"
        "💬 *Text* — ask anything\n\n"
        "Pick a mode, then paste text or upload a file.",
        parse_mode="Markdown",
        reply_markup=menu(),
    )
    if WEBAPP_URL:
        await ctx.bot.set_chat_menu_button(
            chat_id=update.effective_chat.id,
            menu_button=MenuButtonWebApp(text="Skills", web_app=WebAppInfo(url=WEBAPP_URL)))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text("📎 Reading your file…")
    text = await extract_file_text(update, ctx)
    if not text or not text.strip():
        await update.message.reply_text("Couldn't read that file. Supported: .txt, .md, .pdf, .docx.")
        return
    await route_content(update, ctx, text)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    t = update.message.text
    labels = {"📝 Quiz": "quiz", "🗂 Flashcards": "flashcards", "📄 Summary": "summary", "💬 Text": "text"}
    if t in labels:
        user_mode[update.effective_chat.id] = labels[t]
        quiz_state.pop(update.effective_chat.id, None)       # cancel any running quiz
        flashcard_state.pop(update.effective_chat.id, None)  # cancel any running deck
        await update.message.reply_text(MODE_MSG[labels[t]])
        return
    await route_content(update, ctx, t)


async def models_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List models this API key can actually use — handy for debugging 404s."""
    if not await guard(update):
        return
    url = f"{GEMINI}?key={GEMINI_KEY}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        names = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                names.append(m["name"].replace("models/", ""))
        if names:
            await update.message.reply_text("Models your key supports:\n" + "\n".join(names[:40]))
        else:
            await update.message.reply_text("No generateContent models returned for this key.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't list models: {e}")


async def quiz_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_mode[update.effective_chat.id] = "quiz"
    src = " ".join(ctx.args)
    if src:
        await route_content(update, ctx, src)
    else:
        await update.message.reply_text(MODE_MSG["quiz"])


async def flashcards_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_mode[update.effective_chat.id] = "flashcards"
    src = " ".join(ctx.args)
    if src:
        await route_content(update, ctx, src)
    else:
        await update.message.reply_text(MODE_MSG["flashcards"])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("flashcards", flashcards_cmd))
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^ans:"))
    app.add_handler(CallbackQueryHandler(on_flip, pattern=r"^flip$"))
    app.add_handler(CallbackQueryHandler(on_next_card, pattern=r"^nextcard$"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
