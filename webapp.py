import os
import sys
import json
import hmac
import hashlib
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import entitlements
import gemini_client

TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip() or None
if not TELEGRAM_TOKEN or not gemini_client.GEMINI_KEY or not entitlements.DATABASE_URL:
    missing = [name for name, val in
               (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("GEMINI_API_KEY", gemini_client.GEMINI_KEY),
                ("DATABASE_URL", entitlements.DATABASE_URL)) if not val]
    sys.exit(f"ERROR: missing required environment variable(s): {', '.join(missing)}")
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


def verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram Mini App initData per Telegram's documented HMAC check."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    return json.loads(user_raw)


class GenerateRequest(BaseModel):
    init_data: str
    text: str


def authorize(init_data: str) -> tuple[int | None, JSONResponse | None]:
    user = verify_init_data(init_data)
    if not user:
        return None, JSONResponse({"error": "unauthorized"}, status_code=401)
    user_id = user["id"]
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        return None, JSONResponse({"error": "unauthorized"}, status_code=403)
    return user_id, None


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/quiz")
async def api_quiz(req: GenerateRequest):
    user_id, err = authorize(req.init_data)
    if err:
        return err
    if not entitlements.has_unlimited_access(user_id):
        if not entitlements.try_consume_daily_quiz(user_id):
            return JSONResponse({"error": "quota_exceeded"}, status_code=403)
    try:
        questions = await gemini_client.gemini_quiz(req.text)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_generation",
                              "message": "Couldn't build a clean quiz from that. Try clearer or shorter text."},
                             status_code=422)
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": "gemini_error", "message": gemini_client.gemini_error_message(e)},
                             status_code=502)
    if not questions:
        return JSONResponse({"error": "empty", "message": "No usable questions came back."}, status_code=422)
    return {"questions": questions}


@app.post("/api/flashcards")
async def api_flashcards(req: GenerateRequest):
    user_id, err = authorize(req.init_data)
    if err:
        return err
    try:
        cards = await gemini_client.gemini_flashcards(req.text)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_generation",
                              "message": "Couldn't build a clean deck from that. Try clearer or shorter text."},
                             status_code=422)
    except httpx.HTTPStatusError as e:
        return JSONResponse({"error": "gemini_error", "message": gemini_client.gemini_error_message(e)},
                             status_code=502)
    if not cards:
        return JSONResponse({"error": "empty", "message": "No usable flashcards came back."}, status_code=422)
    return {"cards": cards}
