import os
import re
import json
import asyncio
import httpx

GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip() or None

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODEL = "gemini-flash-latest"          # stable alias — avoids 404 from retired versions

HTTP_CLIENT = httpx.AsyncClient(timeout=120)  # shared client — reuses connections across requests

QUIZ_N = 5
FLASHCARD_N = 8


def gemini_error_message(e: httpx.HTTPStatusError) -> str:
    code = e.response.status_code
    if code in (401, 403):
        return f"Gemini rejected the API key ({code}). Check GEMINI_API_KEY."
    if code == 429:
        return f"Gemini quota/rate limit hit ({code}). Please try again in a moment."
    if code == 503:
        return f"Gemini is temporarily overloaded ({code}). Please try again in a moment."
    return f"Gemini error ({code}). Please try again in a moment."


async def gemini_text(prompt: str, temperature: float = 0.4) -> str:
    url = f"{GEMINI}/{TEXT_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature}}
    for attempt in range(5):
        r = await HTTP_CLIENT.post(url, json=body)
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
