"""Give the owner's account max gear/levels: queues a 'dev_max' grant that the game applies on next open."""
import sqlite3, sys, time
db = sqlite3.connect("/var/lib/plunkly/plunkly.db")
db.execute("CREATE TABLE IF NOT EXISTS grants (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, sku TEXT, charge TEXT UNIQUE, claimed INTEGER DEFAULT 0, ts INTEGER)")
name = (sys.argv[1] if len(sys.argv) > 1 else "max").lower()
rows = db.execute("SELECT uid, name FROM saves WHERE LOWER(name) LIKE ? ORDER BY updated DESC", (name + "%",)).fetchall()
if not rows:
    rows = db.execute("SELECT uid, name FROM saves ORDER BY updated DESC LIMIT 1").fetchall()
if not rows:
    print("no players yet - open the game once first"); sys.exit(1)
uid, nm = rows[0]
db.execute("INSERT INTO grants(uid, sku, charge, claimed, ts) VALUES(?, 'dev_max', ?, 0, ?)", (uid, f"dev-{uid}-{int(time.time())}", int(time.time())))
db.commit()
print("granted max pack to", nm, uid)
