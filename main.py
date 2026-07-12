import os
import re
import json
import base64
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
    InputFile,
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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash-image"

MEDICAL_RE = re.compile(
    r"\b(anatomy|organ|heart|liver|kidney|brain|lung|receptor|pharmacolog|drug|"
    r"dose|mechanism|disease|clinical|patient|medcoling|infographic)\b", re.IGNORECASE)
IMG_DISCLAIMER = ("\n\n⚠️ Illustrative only — AI-generated, not anatomically exact. "
                  "Not for clinical or study reference.")

QUIZ_N = 5
user_mode = {}         # chat_id -> mode
quiz_state = {}        # chat_id -> {"questions":[...], "idx":int, "score":int}


# ---------------- Gemini ----------------
async def gemini_text(prompt: str, temperature: float = 0.4) -> str:
    url = f"{GEMINI}/{TEXT_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature}}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


async def gemini_quiz(source: str) -> list:
    """Ask Gemini for quiz questions as strict JSON, return a list of dicts."""
    prompt = (
        f"Create {QUIZ_N} multiple-choice questions based ONLY on the text below, "
        "for a pharmacy/medical student. Return STRICT JSON only — no markdown, no "
        "code fences, no commentary. Schema:\n"
        '[{"q":"question text","options":["opt A","opt B","opt C","opt D"],'
        '"answer":0,"explain":"one line why"}]\n'
        '"answer" is the 0-based index of the correct option. '
        "Do not invent facts not in the text.\n\n"
        f"TEXT:\n{source}"
    )
    raw = await gemini_text(prompt, temperature=0.5)
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


async def gemini_image(prompt: str) -> bytes | None:
    url = f"{GEMINI}/{IMAGE_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]}}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    for part in data["candidates"][0]["content"]["parts"]:
        inline = part.get("inline_data") or part.get("inlineData")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    return None


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
    rows = [[KeyboardButton("📝 Quiz"), KeyboardButton("📄 Summary")],
            [KeyboardButton("💬 Text"), KeyboardButton("🎨 Image")]]
    if WEBAPP_URL:
        rows.insert(0, [KeyboardButton("✨ Open App", web_app=WebAppInfo(url=WEBAPP_URL))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


MODE_MSG = {
    "quiz": "📝 Quiz mode. Paste text or upload a .txt / .pdf / .docx — I'll build an interactive quiz.",
    "summary": "📄 Summary mode. Paste text or upload a file to summarize.",
    "text": "💬 Text mode. Ask me anything.",
    "image": "🎨 Image mode. Describe the picture you want.",
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


# ---------------- Other modes ----------------
async def send_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def do_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    prompt = ("Summarize the text for a pharmacy/medical student: a 2-sentence overview, "
              "then 5-8 key bullet points, then a 'High-yield:' line with the top takeaway. "
              "Stay faithful to the text.\n\nTEXT:\n" + source)
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        out = await gemini_text(prompt, temperature=0.3)
    except Exception as e:
        out = f"Error: {e}"
    await send_long(update, out)


async def make_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str):
    if not prompt.strip():
        await update.message.reply_text("Describe the image you want.")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    try:
        img = await gemini_image(prompt)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (402, 429):
            await update.message.reply_text(
                "🎨 Image generation needs billing enabled on your Google key. Use another mode instead.")
        else:
            await update.message.reply_text(f"Image error ({e.response.status_code}).")
        return
    except Exception as e:
        await update.message.reply_text(f"Image error: {e}")
        return
    if not img:
        await update.message.reply_text("No image came back — try rephrasing.")
        return
    caption = "🎨 " + prompt[:800]
    if MEDICAL_RE.search(prompt):
        caption += IMG_DISCLAIMER
    bio = BytesIO(img); bio.name = "image.png"
    await update.message.reply_photo(photo=InputFile(bio), caption=caption[:1024])


async def route_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE, source: str):
    mode = user_mode.get(update.effective_chat.id, "quiz")
    if mode in ("quiz", "summary") and len(source.strip()) < 20:
        await update.message.reply_text("Please send more text (a few sentences at least).")
        return
    if len(source) > 20000:
        source = source[:20000]
        await update.message.reply_text("ℹ️ Text was long — using the first part only.")
    if mode == "quiz":
        await start_quiz(update, ctx, source)
    elif mode == "summary":
        await do_summary(update, ctx, source)
    elif mode == "image":
        await make_image(update, ctx, source)
    else:
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        try:
            out = await gemini_text(source, temperature=0.6)
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
        "📄 *Summary* — key points\n"
        "💬 *Text* — ask anything\n"
        "🎨 *Image* — generate a picture\n\n"
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
    labels = {"📝 Quiz": "quiz", "📄 Summary": "summary", "💬 Text": "text", "🎨 Image": "image"}
    if t in labels:
        user_mode[update.effective_chat.id] = labels[t]
        quiz_state.pop(update.effective_chat.id, None)  # cancel any running quiz
        await update.message.reply_text(MODE_MSG[labels[t]])
        return
    await route_content(update, ctx, t)


async def quiz_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    user_mode[update.effective_chat.id] = "quiz"
    src = " ".join(ctx.args)
    if src:
        await route_content(update, ctx, src)
    else:
        await update.message.reply_text(MODE_MSG["quiz"])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^ans:"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
