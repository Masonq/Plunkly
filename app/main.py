import os, secrets, logging, pathlib
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET", "") or secrets.token_hex(16)
VERSION = os.environ.get("APP_VERSION", "dev")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
log = logging.getLogger("plunkly")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return r


app.mount("/assets", CachedStatic(directory=WEB / "assets"), name="assets")
INDEX = (WEB / "index.html").read_text(encoding="utf-8").replace("__V__", VERSION)
NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX, headers=NOCACHE)


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "version": VERSION}, headers=NOCACHE)


def play_url():
    return f"{BASE_URL}/?v={VERSION}"


async def tg(method, **data):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/{method}", json=data)
        return r.json()


WELCOME = (
    "<b>Welcome to Plunkly!</b>\n\n"
    "Your tiny planet is drifting through space. Meet neighbours, trade, raid and build alliances.\n\n"
    "Tap <b>Play</b> to open your planet."
)


@app.post("/tg/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(status_code=403)
    upd = await request.json()
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat:
        await tg("sendMessage", chat_id=chat, text=WELCOME, parse_mode="HTML",
                 reply_markup={"inline_keyboard": [[{"text": "Play Plunkly", "web_app": {"url": play_url()}}]]})
    return {"ok": True}


@app.on_event("startup")
async def setup_bot():
    if not BOT_TOKEN or not BASE_URL:
        log.warning("BOT_TOKEN or BASE_URL missing, bot setup skipped")
        return
    try:
        await tg("setWebhook", url=f"{BASE_URL}/tg/webhook/{SECRET}", secret_token=SECRET,
                 allowed_updates=["message"], drop_pending_updates=False)
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Play", "web_app": {"url": play_url()}})
        await tg("setMyCommands", commands=[{"command": "start", "description": "Open your planet"}])
        await tg("setMyShortDescription", short_description="Tiny worlds. Big meetups. A space game in Telegram.")
        await tg("setMyDescription", description="Your tiny planet drifts through a shared space. Meet neighbours, trade, raid, upgrade your captain's gear and build alliances.")
    except Exception as e:
        log.exception("bot setup failed: %s", e)
