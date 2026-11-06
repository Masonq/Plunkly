import os, secrets, logging, pathlib, hmac, hashlib, json, sqlite3, time
from urllib.parse import parse_qsl
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


DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/var/lib/plunkly"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = ROOT / ".data"; DATA_DIR.mkdir(exist_ok=True)
DB = DATA_DIR / "plunkly.db"


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS saves (uid INTEGER PRIMARY KEY, name TEXT, state TEXT NOT NULL, ts INTEGER NOT NULL, updated INTEGER NOT NULL)")
    return c


def tg_user(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


@app.post("/api/load")
async def api_load(request: Request):
    body = await request.json()
    u = tg_user(body.get("initData", ""))
    if not u or "id" not in u:
        raise HTTPException(status_code=401)
    with db() as c:
        row = c.execute("SELECT state, ts FROM saves WHERE uid=?", (int(u["id"]),)).fetchone()
    return JSONResponse({"state": json.loads(row[0]) if row else None, "ts": row[1] if row else 0}, headers=NOCACHE)


@app.post("/api/save")
async def api_save(request: Request):
    raw = await request.body()
    if len(raw) > 200_000:
        raise HTTPException(status_code=413)
    body = json.loads(raw or b"{}")
    u = tg_user(body.get("initData", ""))
    if not u or "id" not in u:
        raise HTTPException(status_code=401)
    state = body.get("state")
    ts = int(body.get("ts") or 0)
    if not isinstance(state, dict):
        raise HTTPException(status_code=400)
    with db() as c:
        cur = c.execute("SELECT ts FROM saves WHERE uid=?", (int(u["id"]),)).fetchone()
        if cur and cur[0] > ts:
            return {"ok": True, "stale": True}
        c.execute("INSERT INTO saves(uid,name,state,ts,updated) VALUES(?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,state=excluded.state,ts=excluded.ts,updated=excluded.updated",
                  (int(u["id"]), (u.get("first_name") or "")[:64], json.dumps(state, separators=(",", ":")), ts, int(time.time())))
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    try:
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM saves").fetchone()[0]
    except Exception:
        n = -1
    return JSONResponse({"ok": True, "version": VERSION, "saves": n}, headers=NOCACHE)


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


WELCOME_RU = (
    "<b>Добро пожаловать в Plunkly!</b>\n\n"
    "Твоя маленькая планета летит по космосу. Встречай соседей, торгуй, устраивай набеги и заключай союзы.\n\n"
    "Нажми <b>Играть</b>, чтобы открыть свою планету."
)


@app.post("/tg/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(status_code=403)
    upd = await request.json()
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    ru = ((msg.get("from") or {}).get("language_code") or "").lower().startswith("ru")
    if chat:
        await tg("sendMessage", chat_id=chat, text=WELCOME_RU if ru else WELCOME, parse_mode="HTML",
                 reply_markup={"inline_keyboard": [[{"text": "Играть в Plunkly" if ru else "Play Plunkly", "web_app": {"url": play_url()}}]]})
        if ru:
            await tg("setChatMenuButton", chat_id=chat, menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": play_url()}})
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
        await tg("setMyShortDescription", short_description="Маленькие миры. Большие встречи. Космическая игра в Telegram.", language_code="ru")
        await tg("setMyDescription", description="Твоя маленькая планета летит по общему космосу. Встречай соседей, торгуй, устраивай набеги, улучшай снаряжение капитана и заключай союзы.", language_code="ru")
        await tg("setMyCommands", commands=[{"command": "start", "description": "Открыть свою планету"}], language_code="ru")
    except Exception as e:
        log.exception("bot setup failed: %s", e)
