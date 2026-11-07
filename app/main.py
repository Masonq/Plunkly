import asyncio
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
    cols = {r[1] for r in c.execute("PRAGMA table_info(saves)")}
    for col, typ in (("power", "INTEGER DEFAULT 0"), ("points", "INTEGER DEFAULT 1000"), ("meta", "TEXT")):
        if col not in cols:
            c.execute(f"ALTER TABLE saves ADD COLUMN {col} {typ}")
    c.execute("CREATE TABLE IF NOT EXISTS grants (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, sku TEXT, charge TEXT UNIQUE, claimed INTEGER DEFAULT 0, ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS notified (uid INTEGER, kind TEXT, target INTEGER, sent INTEGER, PRIMARY KEY(uid,kind))")
    c.execute("CREATE TABLE IF NOT EXISTS refs (friend INTEGER PRIMARY KEY, referrer INTEGER, name TEXT, claimed INTEGER DEFAULT 0, ts INTEGER)")
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
        meta = body.get("meta") or {}
        power = int(meta.get("power") or 0)
        points = int(meta.get("points") or 1000)
        c.execute("INSERT INTO saves(uid,name,state,ts,updated,power,points,meta) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,state=excluded.state,ts=excluded.ts,updated=excluded.updated,power=excluded.power,points=excluded.points,meta=excluded.meta",
                  (int(u["id"]), (u.get("first_name") or "")[:64], json.dumps(state, separators=(",", ":")), ts, int(time.time()), power, points, json.dumps(meta)[:4000]))
    return {"ok": True}


def auth(body):
    u = tg_user(body.get("initData", ""))
    if not u or "id" not in u:
        raise HTTPException(status_code=401)
    return u


@app.post("/api/arena/opponents")
async def api_opp(request: Request):
    body = await request.json()
    u = auth(body)
    mp = int(body.get("power") or 0)
    with db() as c:
        rows = c.execute("SELECT uid,name,power,points,meta FROM saves WHERE uid!=? AND power>0 ORDER BY ABS(power-?) LIMIT 12", (int(u["id"]), mp)).fetchall()
    import random
    random.shuffle(rows)
    out = []
    for uid, name, power, points, meta in rows[:3]:
        try:
            st = (json.loads(meta or "{}") or {}).get("st") or {}
        except Exception:
            st = {}
        out.append({"uid": uid % 1000, "name": name or "Player", "power": power, "points": points, "st": st})
    return {"list": out}


@app.post("/api/top")
async def api_top(request: Request):
    body = await request.json()
    u = auth(body)
    with db() as c:
        rows = c.execute("SELECT uid,name,points,power FROM saves ORDER BY points DESC, power DESC LIMIT 50").fetchall()
        me = c.execute("SELECT points FROM saves WHERE uid=?", (int(u["id"]),)).fetchone()
        rank = (c.execute("SELECT COUNT(*) FROM saves WHERE points>?", (me[0],)).fetchone()[0] + 1) if me else 0
    return {"list": [{"name": n or "Player", "points": p, "power": pw, "me": uid == int(u["id"])} for uid, n, p, pw in rows], "rank": rank}


SKUS = {"caps10": ("10 star pods", 50), "caps50": ("50 star pods", 200), "acc20": ("20 accelerators", 40), "vip30": ("VIP for 30 days", 300), "pass": ("Space Pass premium", 250)}


@app.post("/api/invoice")
async def api_invoice(request: Request):
    body = await request.json()
    u = auth(body)
    sku = body.get("sku")
    if sku not in SKUS:
        raise HTTPException(status_code=400)
    title, price = SKUS[sku]
    r = await tg("createInvoiceLink", title=f"Plunkly · {title}", description=title, payload=f"{u['id']}:{sku}:{secrets.token_hex(4)}", currency="XTR", prices=[{"label": title, "amount": price}])
    if not r.get("ok"):
        raise HTTPException(status_code=502)
    return {"link": r["result"]}


@app.post("/api/grants")
async def api_grants(request: Request):
    body = await request.json()
    u = auth(body)
    with db() as c:
        rows = c.execute("SELECT id,sku FROM grants WHERE uid=? AND claimed=0", (int(u["id"]),)).fetchall()
    return {"list": [{"id": i, "sku": s} for i, s in rows]}


@app.post("/api/grants/ack")
async def api_grants_ack(request: Request):
    body = await request.json()
    u = auth(body)
    ids = [int(x) for x in (body.get("ids") or [])][:50]
    if ids:
        with db() as c:
            c.executemany("UPDATE grants SET claimed=1 WHERE uid=? AND id=?", [(int(u["id"]), i) for i in ids])
    return {"ok": True}


@app.post("/api/friends")
async def api_friends(request: Request):
    body = await request.json()
    u = auth(body)
    with db() as c:
        rows = c.execute("SELECT name,claimed FROM refs WHERE referrer=? ORDER BY ts DESC", (int(u["id"]),)).fetchall()
    return {"count": len(rows), "unclaimed": sum(1 for _, cl in rows if not cl), "names": [n for n, _ in rows[:20] if n]}


@app.post("/api/friends/claim")
async def api_friends_claim(request: Request):
    body = await request.json()
    u = auth(body)
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM refs WHERE referrer=? AND claimed=0", (int(u["id"]),)).fetchone()[0]
        c.execute("UPDATE refs SET claimed=1 WHERE referrer=? AND claimed=0", (int(u["id"]),))
    return {"n": n}


@app.get("/healthz")
async def healthz():
    try:
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM saves").fetchone()[0]
    except Exception:
        n = -1
    try:
        with db() as c:
            dv = c.execute("SELECT COUNT(*), COALESCE(SUM(claimed),0) FROM grants WHERE sku='dev_max'").fetchone()
    except Exception:
        dv = (0, 0)
    return JSONResponse({"ok": True, "version": VERSION, "saves": n, "dev_grants": dv[0], "dev_claimed": dv[1]}, headers=NOCACHE)


def play_url():
    return f"{BASE_URL}/?v={VERSION}"


async def tg(method, **data):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/{method}", json=data)
        return r.json()


WELCOME = (
    "<b>Welcome to Plunkly!</b>\n\n"
    "Defend your tiny planet from space monsters, open star pods for epic gear and battle other captains in the arena.\n\n"
    "Tap <b>Play</b> to open your planet."
)


WELCOME_RU = (
    "<b>Добро пожаловать в Plunkly!</b>\n\n"
    "Защищай свою маленькую планету от космических монстров, открывай звёздные капсулы с крутым снаряжением и сражайся с другими капитанами на арене.\n\n"
    "Нажми <b>Играть</b>, чтобы открыть свою планету."
)


@app.post("/tg/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(status_code=403)
    upd = await request.json()
    pcq = upd.get("pre_checkout_query")
    if pcq:
        ok = (pcq.get("invoice_payload") or "").split(":")[1:2] and (pcq.get("invoice_payload").split(":")[1] in SKUS)
        await tg("answerPreCheckoutQuery", pre_checkout_query_id=pcq["id"], ok=bool(ok), **({} if ok else {"error_message": "Unknown item"}))
        return {"ok": True}
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    frm = msg.get("from") or {}
    ru = (frm.get("language_code") or "").lower().startswith("ru")
    sp = msg.get("successful_payment")
    if sp:
        try:
            uid, sku = sp["invoice_payload"].split(":")[:2]
            with db() as c:
                c.execute("INSERT OR IGNORE INTO grants(uid,sku,charge,claimed,ts) VALUES(?,?,?,0,?)", (int(uid), sku, sp.get("telegram_payment_charge_id"), int(time.time())))
        except Exception as e:
            log.exception("payment store failed: %s", e)
        if chat:
            await tg("sendMessage", chat_id=chat, text="Спасибо! Покупка уже в игре." if ru else "Thank you! Your purchase is waiting in the game.",
                     reply_markup={"inline_keyboard": [[{"text": "Играть" if ru else "Play", "web_app": {"url": play_url()}}]]})
        return {"ok": True}
    text = msg.get("text") or ""
    if text.startswith("/start ref_") and frm.get("id"):
        try:
            ref = int(text.split("ref_", 1)[1].split()[0])
            me = int(frm["id"])
            if ref != me:
                with db() as c:
                    known = c.execute("SELECT 1 FROM saves WHERE uid=?", (me,)).fetchone() or c.execute("SELECT 1 FROM refs WHERE friend=?", (me,)).fetchone()
                    if not known:
                        c.execute("INSERT OR IGNORE INTO refs(friend,referrer,name,claimed,ts) VALUES(?,?,?,0,?)", (me, ref, (frm.get("first_name") or "")[:64], int(time.time())))
        except Exception:
            pass
    if chat:
        await tg("sendMessage", chat_id=chat, text=WELCOME_RU if ru else WELCOME, parse_mode="HTML",
                 reply_markup={"inline_keyboard": [[{"text": "Играть в Plunkly" if ru else "Play Plunkly", "web_app": {"url": play_url()}}]]})
        if ru:
            await tg("setChatMenuButton", chat_id=chat, menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": play_url()}})
    return {"ok": True}


NOTE = {
    "storage": ("Your planet storage is full — the mine has stopped. Collect your stardust!", "Хранилище планеты заполнено — шахта простаивает. Собери пыль!"),
    "patrol": ("Your captain is back from an 8-hour patrol with full loot.", "Капитан вернулся из патруля с полной добычей. Забери награды!"),
    "capup": ("Your star pod upgrade is done — better drop chances!", "Капсула улучшена — шансы на редкие вещи выросли!"),
    "free": ("Free star pods are waiting in the shop.", "В магазине ждут бесплатные капсулы."),
}


async def notifier():
    await asyncio.sleep(20)
    while True:
        try:
            now = int(time.time() * 1000)
            with db() as c:
                rows = c.execute("SELECT uid, meta, updated FROM saves WHERE updated > ?", (int(time.time()) - 14 * 86400,)).fetchall()
            for uid, meta, updated in rows:
                try:
                    m = json.loads(meta or "{}")
                except Exception:
                    continue
                if m.get("notif") is False or time.time() - updated < 600:
                    continue
                hour = ((now // 60000 + int(m.get("tz") or 0)) // 60) % 24
                if hour < 9 or hour >= 22:
                    continue
                with db() as c:
                    sent24 = c.execute("SELECT COUNT(*) FROM notified WHERE uid=? AND sent>?", (uid, int(time.time()) - 86400)).fetchone()[0]
                    if sent24 >= 3:
                        continue
                    for kind, target in (m.get("n") or {}).items():
                        target = int(target or 0)
                        if kind not in NOTE or not target or target > now or target < now - 6 * 3600 * 1000:
                            continue
                        row = c.execute("SELECT target FROM notified WHERE uid=? AND kind=?", (uid, kind)).fetchone()
                        if row and row[0] == target:
                            continue
                        ru = m.get("lang") == "ru"
                        await tg("sendMessage", chat_id=uid, text=NOTE[kind][1 if ru else 0],
                                 reply_markup={"inline_keyboard": [[{"text": "Играть" if ru else "Play", "web_app": {"url": play_url()}}]]})
                        c.execute("INSERT INTO notified(uid,kind,target,sent) VALUES(?,?,?,?) ON CONFLICT(uid,kind) DO UPDATE SET target=excluded.target, sent=excluded.sent", (uid, kind, target, int(time.time())))
                        break
        except Exception as e:
            log.exception("notifier: %s", e)
        await asyncio.sleep(120)


@app.on_event("startup")
async def setup_bot():
    asyncio.create_task(notifier())
    if not BOT_TOKEN or not BASE_URL:
        log.warning("BOT_TOKEN or BASE_URL missing, bot setup skipped")
        return
    try:
        await tg("setWebhook", url=f"{BASE_URL}/tg/webhook/{SECRET}", secret_token=SECRET,
                 allowed_updates=["message", "pre_checkout_query"], drop_pending_updates=False)
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Play", "web_app": {"url": play_url()}})
        await tg("setMyCommands", commands=[{"command": "start", "description": "Open your planet"}])
        await tg("setMyShortDescription", short_description="Tiny worlds. Big meetups. A space game in Telegram.")
        await tg("setMyDescription", description="Your tiny planet drifts through a shared space. Meet neighbours, trade, raid, upgrade your captain's gear and build alliances.")
        await tg("setMyShortDescription", short_description="Маленькие миры. Большие встречи. Космическая игра в Telegram.", language_code="ru")
        await tg("setMyDescription", description="Твоя маленькая планета летит по общему космосу. Встречай соседей, торгуй, устраивай набеги, улучшай снаряжение капитана и заключай союзы.", language_code="ru")
        await tg("setMyCommands", commands=[{"command": "start", "description": "Открыть свою планету"}], language_code="ru")
    except Exception as e:
        log.exception("bot setup failed: %s", e)
