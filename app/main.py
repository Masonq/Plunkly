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
    gcols = {r[1] for r in c.execute("PRAGMA table_info(grants)")}
    if "qty" not in gcols:
        c.execute("ALTER TABLE grants ADD COLUMN qty INTEGER DEFAULT 1")
    if "data" not in gcols:
        c.execute("ALTER TABLE grants ADD COLUMN data TEXT")
    c.execute("CREATE TABLE IF NOT EXISTS wboss (day TEXT PRIMARY KEY, hp INTEGER, max INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS wdmg (day TEXT, uid INTEGER, name TEXT, dmg INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0, PRIMARY KEY(day, uid))")
    c.execute("CREATE TABLE IF NOT EXISTS dau (day TEXT, uid INTEGER, PRIMARY KEY(day, uid))")
    c.execute("CREATE TABLE IF NOT EXISTS notified (uid INTEGER, kind TEXT, target INTEGER, sent INTEGER, PRIMARY KEY(uid,kind))")
    c.execute("CREATE TABLE IF NOT EXISTS guilds (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, emblem INTEGER, owner INTEGER, created INTEGER, week INTEGER DEFAULT 0, boss_hp INTEGER DEFAULT 0, boss_max INTEGER DEFAULT 0, killed INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS gmembers (uid INTEGER PRIMARY KEY, gid INTEGER, name TEXT, joined INTEGER, week INTEGER DEFAULT 0, dmg INTEGER DEFAULT 0, hits_day TEXT DEFAULT '', hits INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS gfeed (id INTEGER PRIMARY KEY AUTOINCREMENT, gid INTEGER, uid INTEGER, name TEXT, text TEXT, ts INTEGER)")
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
        c.execute("INSERT OR IGNORE INTO dau(day,uid) VALUES(?,?)", (time.strftime("%Y-%m-%d", time.gmtime()), int(u["id"])))
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
            mj = json.loads(meta or "{}") or {}
        except Exception:
            mj = {}
        st = mj.get("st") or {}
        out.append({"uid": uid % 1000, "name": name or "Player", "power": power, "points": points, "st": st, "title": (mj.get("title") or "")[:40]})
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


SKUS = {"b_start": ("Survivor pack", 99), "b_tok": ("500 tokens", 25), "b_parts": ("300 parts", 25), "b_vip": ("VIP 30 days", 150), "starter": ("Starter pack", 99), "deal_caps": ("Deal: 40 star pods", 25), "deal_mcaps": ("Deal: 12 module pods", 25), "deal_core": ("Deal: 25 evolution cores", 25), "deal_eggs": ("Deal: 3 pet eggs", 25), "eggs5": ("5 pet eggs", 60), "caps10": ("10 star pods", 50), "caps50": ("50 star pods", 200), "acc20": ("20 accelerators", 40), "vip30": ("VIP for 30 days", 300), "pass": ("Space Pass premium", 250)}


@app.post("/api/invoice")
async def api_invoice(request: Request):
    body = await request.json()
    u = auth(body)
    sku = body.get("sku")
    if sku not in SKUS:
        raise HTTPException(status_code=400)
    title, price = SKUS[sku]
    qty = max(1, min(1 if (sku in ("pass", "starter", "b_start") or sku.startswith("deal_")) else 20, int(body.get("qty") or 1)))
    label = f"{title} ×{qty}" if qty > 1 else title
    r = await tg("createInvoiceLink", title=f"Бункер 17 · {label}", description=label, payload=f"{u['id']}:{sku}:{qty}:{secrets.token_hex(4)}", currency="XTR", prices=[{"label": label, "amount": price * qty}])
    if not r.get("ok"):
        raise HTTPException(status_code=502)
    return {"link": r["result"]}


@app.post("/api/grants")
async def api_grants(request: Request):
    body = await request.json()
    u = auth(body)
    with db() as c:
        rows = c.execute("SELECT id,sku,COALESCE(qty,1),data FROM grants WHERE uid=? AND claimed=0", (int(u["id"]),)).fetchall()
    return {"list": [{"id": i, "sku": s, "qty": q, "data": d} for i, s, q, d in rows]}


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


# ---------------- guilds ----------------
GMAX = 20
def week_no():
    return int((time.time() - 345600) // 604800)  # weeks starting Monday 00:00 UTC

def boss_max_for(c, gid):
    n = c.execute("SELECT COUNT(*) FROM gmembers WHERE gid=?", (gid,)).fetchone()[0]
    pw = c.execute("SELECT COALESCE(SUM(s.power),0) FROM gmembers m JOIN saves s ON s.uid=m.uid WHERE m.gid=?", (gid,)).fetchone()[0] or 0
    return max(200000, int(pw * 120) + n * 50000)

def guild_roll(c, gid):
    w = week_no()
    g = c.execute("SELECT week FROM guilds WHERE id=?", (gid,)).fetchone()
    if g and g[0] != w:
        mx = boss_max_for(c, gid)
        c.execute("UPDATE guilds SET week=?, boss_hp=?, boss_max=?, killed=0 WHERE id=?", (w, mx, mx, gid))
        c.execute("UPDATE gmembers SET week=?, dmg=0, claimed=0 WHERE gid=?", (w, gid))

def guild_view(c, uid):
    me = c.execute("SELECT gid FROM gmembers WHERE uid=?", (uid,)).fetchone()
    if not me:
        return {"guild": None}
    gid = me[0]
    guild_roll(c, gid)
    g = c.execute("SELECT id,name,emblem,owner,boss_hp,boss_max,killed FROM guilds WHERE id=?", (gid,)).fetchone()
    if not g:
        c.execute("DELETE FROM gmembers WHERE uid=?", (uid,))
        return {"guild": None}
    mem = c.execute("SELECT m.uid,m.name,m.dmg,COALESCE(s.power,0),m.hits_day,m.hits,m.claimed FROM gmembers m LEFT JOIN saves s ON s.uid=m.uid WHERE m.gid=? ORDER BY m.dmg DESC, s.power DESC", (gid,)).fetchall()
    feed = c.execute("SELECT name,text,ts,uid FROM gfeed WHERE gid=? ORDER BY id DESC LIMIT 40", (gid,)).fetchall()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    mine = next((x for x in mem if x[0] == uid), None)
    left = 3 - (mine[5] if mine and mine[4] == today else 0)
    return {"guild": {"id": g[0], "name": g[1], "emblem": g[2], "owner": g[3] == uid, "boss_hp": g[4], "boss_max": g[5], "killed": bool(g[6]),
            "members": [{"name": n or "Player", "dmg": d, "power": p, "me": u == uid, "owner": u == g[3]} for u, n, d, p, _, _, _ in mem],
            "feed": [{"name": n, "text": t, "ts": ts, "me": u == uid} for n, t, ts, u in feed], "hits_left": max(0, left),
            "claim": bool(g[6]) and mine is not None and not mine[6], "week_ends": (week_no() + 1) * 604800 + 345600}}

@app.post("/api/guild/me")
async def g_me(request: Request):
    body = await request.json(); u = auth(body)
    with db() as c:
        return guild_view(c, int(u["id"]))

@app.post("/api/guild/list")
async def g_list(request: Request):
    body = await request.json(); auth(body)
    q = (body.get("q") or "").strip()[:24]
    with db() as c:
        rows = c.execute("SELECT g.id,g.name,g.emblem,COUNT(m.uid),COALESCE(SUM(s.power),0) FROM guilds g LEFT JOIN gmembers m ON m.gid=g.id LEFT JOIN saves s ON s.uid=m.uid WHERE g.name LIKE ? GROUP BY g.id ORDER BY SUM(s.power) DESC LIMIT 30", (f"%{q}%",)).fetchall()
    return {"list": [{"id": i, "name": n, "emblem": e, "count": k, "power": p} for i, n, e, k, p in rows]}

@app.post("/api/guild/create")
async def g_create(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"])
    name = " ".join((body.get("name") or "").split())[:20]
    emb = int(body.get("emblem") or 0) % 6
    if len(name) < 3:
        return {"error": "name"}
    with db() as c:
        if c.execute("SELECT 1 FROM gmembers WHERE uid=?", (uid,)).fetchone():
            return {"error": "member"}
        if c.execute("SELECT 1 FROM guilds WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
            return {"error": "taken"}
        c.execute("INSERT INTO guilds(name,emblem,owner,created,week) VALUES(?,?,?,?,-1)", (name, emb, uid, int(time.time())))
        gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO gmembers(uid,gid,name,joined,week) VALUES(?,?,?,?,?)", (uid, gid, (u.get("first_name") or "")[:32], int(time.time()), week_no()))
        c.execute("INSERT INTO gfeed(gid,uid,name,text,ts) VALUES(?,?,?,?,?)", (gid, 0, "", "created", int(time.time())))
        return guild_view(c, uid)

@app.post("/api/guild/join")
async def g_join(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"]); gid = int(body.get("gid") or 0)
    with db() as c:
        if c.execute("SELECT 1 FROM gmembers WHERE uid=?", (uid,)).fetchone():
            return {"error": "member"}
        if not c.execute("SELECT 1 FROM guilds WHERE id=?", (gid,)).fetchone():
            return {"error": "nf"}
        if c.execute("SELECT COUNT(*) FROM gmembers WHERE gid=?", (gid,)).fetchone()[0] >= GMAX:
            return {"error": "full"}
        c.execute("INSERT INTO gmembers(uid,gid,name,joined,week) VALUES(?,?,?,?,?)", (uid, gid, (u.get("first_name") or "")[:32], int(time.time()), week_no()))
        c.execute("INSERT INTO gfeed(gid,uid,name,text,ts) VALUES(?,?,?,?,?)", (gid, 0, (u.get("first_name") or "")[:32], "joined", int(time.time())))
        return guild_view(c, uid)

@app.post("/api/guild/leave")
async def g_leave(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"])
    with db() as c:
        me = c.execute("SELECT gid FROM gmembers WHERE uid=?", (uid,)).fetchone()
        if me:
            gid = me[0]
            c.execute("DELETE FROM gmembers WHERE uid=?", (uid,))
            c.execute("INSERT INTO gfeed(gid,uid,name,text,ts) VALUES(?,?,?,?,?)", (gid, 0, (u.get("first_name") or "")[:32], "left", int(time.time())))
            rest = c.execute("SELECT uid FROM gmembers WHERE gid=? ORDER BY joined LIMIT 1", (gid,)).fetchone()
            if not rest:
                c.execute("DELETE FROM guilds WHERE id=?", (gid,)); c.execute("DELETE FROM gfeed WHERE gid=?", (gid,))
            else:
                c.execute("UPDATE guilds SET owner=? WHERE id=? AND owner=?", (rest[0], gid, uid))
    return {"guild": None}

@app.post("/api/guild/post")
async def g_post(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"])
    text = " ".join((body.get("text") or "").split())[:200]
    with db() as c:
        me = c.execute("SELECT gid FROM gmembers WHERE uid=?", (uid,)).fetchone()
        if not me or not text:
            return guild_view(c, uid)
        last = c.execute("SELECT ts FROM gfeed WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not last or time.time() - last[0] >= 3:
            c.execute("INSERT INTO gfeed(gid,uid,name,text,ts) VALUES(?,?,?,?,?)", (me[0], uid, (u.get("first_name") or "")[:32], text, int(time.time())))
        return guild_view(c, uid)

@app.post("/api/guild/hit")
async def g_hit(request: Request):
    import random
    body = await request.json(); u = auth(body); uid = int(u["id"])
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with db() as c:
        me = c.execute("SELECT gid,hits_day,hits FROM gmembers WHERE uid=?", (uid,)).fetchone()
        if not me:
            return {"error": "nf"}
        gid, hd, h = me
        guild_roll(c, gid)
        h = h if hd == today else 0
        if h >= 3:
            return {"error": "limit", **guild_view(c, uid)}
        g = c.execute("SELECT boss_hp FROM guilds WHERE id=?", (gid,)).fetchone()
        pw = c.execute("SELECT COALESCE(power,0) FROM saves WHERE uid=?", (uid,)).fetchone()
        dmg = int(max(100, (pw[0] if pw else 0)) * random.uniform(8, 12))
        hp = max(0, g[0] - dmg)
        c.execute("UPDATE guilds SET boss_hp=?, killed=CASE WHEN ?=0 THEN 1 ELSE killed END WHERE id=?", (hp, hp, gid))
        c.execute("UPDATE gmembers SET dmg=dmg+?, hits_day=?, hits=? WHERE uid=?", (dmg, today, h + 1, uid))
        if hp == 0 and g[0] > 0:
            c.execute("INSERT INTO gfeed(gid,uid,name,text,ts) VALUES(?,?,?,?,?)", (gid, 0, (u.get("first_name") or "")[:32], "boss", int(time.time())))
        v = guild_view(c, uid); v["dmg"] = dmg
        return v

@app.post("/api/guild/claim")
async def g_claim(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"])
    with db() as c:
        me = c.execute("SELECT m.gid,m.claimed,g.killed FROM gmembers m JOIN guilds g ON g.id=m.gid WHERE m.uid=?", (uid,)).fetchone()
        if not me or me[1] or not me[2]:
            return {"ok": False}
        c.execute("UPDATE gmembers SET claimed=1 WHERE uid=?", (uid,))
        return {"ok": True}

# ---------------- weekly event ----------------
@app.post("/api/event/top")
async def ev_top(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"]); eid = int(body.get("id") or 0)
    with db() as c:
        rows = c.execute("SELECT uid,name,meta FROM saves").fetchall()
    out = []
    for i, n, m in rows:
        try:
            ev = (json.loads(m or "{}") or {}).get("ev") or {}
        except Exception:
            ev = {}
        if ev.get("id") == eid and ev.get("tok"):
            out.append({"name": n or "Player", "tok": int(ev["tok"]), "me": i == uid})
    out.sort(key=lambda x: -x["tok"])
    rank = next((k + 1 for k, x in enumerate(out) if x["me"]), 0)
    return {"list": out[:30], "rank": rank}


# ---------------- world boss ----------------
def wday(off=0):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + off * 86400))

def wboss_row(c):
    d = wday()
    r = c.execute("SELECT hp,max FROM wboss WHERE day=?", (d,)).fetchone()
    if not r:
        pw = c.execute("SELECT COALESCE(SUM(power),0) FROM saves WHERE updated>?", (int(time.time()) - 3 * 86400,)).fetchone()[0] or 0
        mx = max(5_000_000, int(pw * 300))
        c.execute("INSERT OR IGNORE INTO wboss(day,hp,max) VALUES(?,?,?)", (d, mx, mx))
        r = (mx, mx)
    return d, r[0], r[1]

def wview(c, uid):
    d, hp, mx = wboss_row(c)
    top = c.execute("SELECT uid,name,dmg FROM wdmg WHERE day=? ORDER BY dmg DESC LIMIT 20", (d,)).fetchall()
    me = c.execute("SELECT dmg,hits FROM wdmg WHERE day=? AND uid=?", (d, uid)).fetchone()
    rank = (c.execute("SELECT COUNT(*) FROM wdmg WHERE day=? AND dmg>?", (d, me[0])).fetchone()[0] + 1) if me else 0
    n = c.execute("SELECT COUNT(*) FROM wdmg WHERE day=?", (d,)).fetchone()[0]
    y = wday(-1)
    ym = c.execute("SELECT dmg,claimed FROM wdmg WHERE day=? AND uid=?", (y, uid)).fetchone()
    yrank = (c.execute("SELECT COUNT(*) FROM wdmg WHERE day=? AND dmg>?", (y, ym[0])).fetchone()[0] + 1) if ym else 0
    ykilled = (c.execute("SELECT hp FROM wboss WHERE day=?", (y,)).fetchone() or [1])[0] == 0
    return {"day": d, "hp": hp, "max": mx, "players": n, "top": [{"name": nm or "Player", "dmg": dm, "me": u == uid} for u, nm, dm in top],
            "my": {"dmg": me[0] if me else 0, "hits_left": 3 - (me[1] if me else 0), "rank": rank},
            "prev": {"rank": yrank, "dmg": ym[0] if ym else 0, "claimable": bool(ym) and not ym[1], "killed": ykilled}, "ends": int(time.time()) // 86400 * 86400 + 86400}

@app.post("/api/wboss")
async def wb_get(request: Request):
    body = await request.json(); u = auth(body)
    with db() as c:
        return wview(c, int(u["id"]))

@app.post("/api/wboss/hit")
async def wb_hit(request: Request):
    import random
    body = await request.json(); u = auth(body); uid = int(u["id"])
    with db() as c:
        d, hp, mx = wboss_row(c)
        me = c.execute("SELECT hits FROM wdmg WHERE day=? AND uid=?", (d, uid)).fetchone()
        if me and me[0] >= 3:
            return {"error": "limit", **wview(c, uid)}
        pw = c.execute("SELECT COALESCE(power,0) FROM saves WHERE uid=?", (uid,)).fetchone()
        dmg = int(max(100, (pw[0] if pw else 0)) * random.uniform(20, 30))
        c.execute("INSERT INTO wdmg(day,uid,name,dmg,hits) VALUES(?,?,?,?,1) ON CONFLICT(day,uid) DO UPDATE SET dmg=dmg+excluded.dmg, hits=hits+1, name=excluded.name", (d, uid, (u.get("first_name") or "")[:32], dmg))
        c.execute("UPDATE wboss SET hp=MAX(0,hp-?) WHERE day=?", (dmg, d))
        v = wview(c, uid); v["dmg"] = dmg
        return v

@app.post("/api/wboss/claim")
async def wb_claim(request: Request):
    body = await request.json(); u = auth(body); uid = int(u["id"])
    with db() as c:
        y = wday(-1)
        me = c.execute("SELECT dmg,claimed FROM wdmg WHERE day=? AND uid=?", (y, uid)).fetchone()
        if not me or me[1]:
            return {"ok": False}
        rank = c.execute("SELECT COUNT(*) FROM wdmg WHERE day=? AND dmg>?", (y, me[0])).fetchone()[0] + 1
        killed = (c.execute("SELECT hp FROM wboss WHERE day=?", (y,)).fetchone() or [1])[0] == 0
        c.execute("UPDATE wdmg SET claimed=1 WHERE day=? AND uid=?", (y, uid))
        return {"ok": True, "rank": rank, "killed": killed}

# ---------------- admin ----------------
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "1245509871").split(",") if x.strip().isdigit()}

def admin(body):
    u = auth(body)
    if int(u["id"]) not in ADMIN_IDS:
        raise HTTPException(status_code=403)
    return u

@app.post("/api/admin/stats")
async def adm_stats(request: Request):
    body = await request.json(); admin(body)
    now = int(time.time())
    with db() as c:
        tot = c.execute("SELECT COUNT(*) FROM saves").fetchone()[0]
        days = [time.strftime("%Y-%m-%d", time.gmtime(now - k * 86400)) for k in range(7)]
        dau = [{"day": d, "n": c.execute("SELECT COUNT(*) FROM dau WHERE day=?", (d,)).fetchone()[0]} for d in days]
        new = c.execute("SELECT COUNT(*) FROM saves WHERE uid IN (SELECT uid FROM dau GROUP BY uid HAVING MIN(day)=?)", (days[0],)).fetchone()[0]
        pays = c.execute("SELECT g.sku,COALESCE(g.qty,1),g.ts,s.name FROM grants g LEFT JOIN saves s ON s.uid=g.uid WHERE g.charge IS NOT NULL AND g.charge NOT LIKE 'dev-%' AND g.charge NOT LIKE 'adm-%' ORDER BY g.ts DESC LIMIT 30").fetchall()
        top = c.execute("SELECT uid,name,power,points FROM saves ORDER BY power DESC LIMIT 15").fetchall()
        refs = c.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        guilds = c.execute("SELECT COUNT(*) FROM guilds").fetchone()[0]
    stars = 0; week = 0
    for sku, q, ts, _ in pays:
        p = SKUS.get(sku, ("", 0))[1] * q
        stars += p
        if ts and ts > now - 7 * 86400:
            week += p
    return {"total": tot, "dau": dau, "new_today": new, "refs": refs, "guilds": guilds, "stars_recent": stars, "stars_week": week,
            "pays": [{"sku": s2, "qty": q, "ts": ts, "name": n} for s2, q, ts, n in pays], "top": [{"uid": i, "name": n, "power": p, "points": pt} for i, n, p, pt in top]}

@app.post("/api/admin/find")
async def adm_find(request: Request):
    body = await request.json(); admin(body)
    q = (body.get("q") or "").strip()
    with db() as c:
        if q.isdigit():
            rows = c.execute("SELECT uid,name,power,updated FROM saves WHERE uid=?", (int(q),)).fetchall()
        else:
            rows = c.execute("SELECT uid,name,power,updated FROM saves WHERE name LIKE ? ORDER BY updated DESC LIMIT 20", (f"%{q}%",)).fetchall()
    return {"list": [{"uid": i, "name": n, "power": p, "seen": up} for i, n, p, up in rows]}

@app.post("/api/admin/give")
async def adm_give(request: Request):
    body = await request.json(); admin(body)
    uid = int(body.get("uid") or 0); rw = body.get("rw") or {}
    allowed = {"tokens", "parts", "energy", "water", "food", "coin", "cry", "caps", "mcaps", "acc", "vou", "fuel", "core", "shard", "ess", "egg", "treat"}
    rw = {k: max(0, min(10**12, int(v))) for k, v in rw.items() if k in allowed and str(v).lstrip("-").isdigit()}
    if not uid or not rw:
        return {"ok": False}
    with db() as c:
        c.execute("INSERT INTO grants(uid,sku,charge,claimed,ts,qty,data) VALUES(?,?,?,0,?,1,?)", (uid, "adm", f"adm-{uid}-{secrets.token_hex(4)}", int(time.time()), json.dumps(rw)))
    msg = (body.get("note") or "").strip()[:300]
    if msg:
        await tg("sendMessage", chat_id=uid, text=msg, reply_markup={"inline_keyboard": [[{"text": "Открыть Бункер 17", "web_app": {"url": play_url()}}]]})
    return {"ok": True}

BCAST = {"running": False, "sent": 0, "total": 0}

async def _broadcast(text):
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT uid FROM saves").fetchall()]
    BCAST.update(running=True, sent=0, total=len(ids))
    for uid in ids:
        try:
            await tg("sendMessage", chat_id=uid, text=text, reply_markup={"inline_keyboard": [[{"text": "Играть", "web_app": {"url": play_url()}}]]})
            BCAST["sent"] += 1
        except Exception:
            pass
        await asyncio.sleep(0.06)
    BCAST["running"] = False

@app.post("/api/admin/broadcast")
async def adm_bcast(request: Request):
    body = await request.json(); admin(body)
    text = (body.get("text") or "").strip()[:1000]
    if body.get("status"):
        return BCAST
    if not text or BCAST["running"]:
        return {"ok": False, **BCAST}
    asyncio.create_task(_broadcast(text))
    return {"ok": True}

@app.post("/api/admin/check")
async def adm_check(request: Request):
    body = await request.json(); u = auth(body)
    return {"admin": int(u["id"]) in ADMIN_IDS}


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
    "<b>Bunker 17</b>\n\n"
    "Forty levels underground. One staircase. A screen that always shows a dead world.\n"
    "Your partner was found at the bottom of the stairs. They say he slipped. You don't believe it.\n\n"
    "Tap <b>Play</b> to go down."
)


WELCOME_RU = (
    "<b>Бункер 17</b>\n\n"
    "Сорок уровней под землёй. Одна лестница. Экран, на котором снаружи всегда мёртвый мир.\n"
    "Твоего напарника нашли у подножия лестницы. Говорят, сорвался. Ты в это не веришь.\n\n"
    "Нажми <b>Играть</b>, чтобы спуститься."
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
            parts = sp["invoice_payload"].split(":")
            uid, sku = parts[0], parts[1]
            qty = int(parts[2]) if len(parts) > 3 and parts[2].isdigit() else 1
            with db() as c:
                c.execute("INSERT OR IGNORE INTO grants(uid,sku,charge,claimed,ts,qty) VALUES(?,?,?,0,?,?)", (int(uid), sku, sp.get("telegram_payment_charge_id"), int(time.time()), qty))
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
                 reply_markup={"inline_keyboard": [[{"text": "Играть" if ru else "Play", "web_app": {"url": play_url()}}]]})
        if ru:
            await tg("setChatMenuButton", chat_id=chat, menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": play_url()}})
    return {"ok": True}


NOTE = {
    "storage": ("Bunker 17: all rooms are full, production has stopped. Come and collect.", "Бункер 17: все комнаты заполнены, выработка встала. Спустись и собери ресурсы."),
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
        await tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": play_url()}})
        await tg("setMyName", name="Bunker 17")
        await tg("setMyName", name="Бункер 17", language_code="ru")
        await tg("setMyCommands", commands=[{"command": "start", "description": "Go down into the bunker"}])
        await tg("setMyShortDescription", short_description="40 levels underground. One staircase. One lie.")
        await tg("setMyDescription", description="Bunker 17. Your partner was found dead at the bottom of the stairs. Run the lower levels, earn trust and uncover what the Data Department is hiding.")
        await tg("setMyShortDescription", short_description="40 уровней под землёй. Одна лестница. Одна ложь.", language_code="ru")
        await tg("setMyDescription", description="Бункер 17. Твоего напарника нашли мёртвым у подножия лестницы. Управляй нижними уровнями, завоёвывай доверие и раскрой, что скрывает Отдел данных.", language_code="ru")
        await tg("setMyCommands", commands=[{"command": "start", "description": "Спуститься в бункер"}], language_code="ru")
    except Exception as e:
        log.exception("bot setup failed: %s", e)
