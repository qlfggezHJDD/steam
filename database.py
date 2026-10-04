"""
database.py — SQLite schema + helpers via aiosqlite
"""
import aiosqlite
import os
import json
import time
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

# On Render set DB_PATH=/data/games.db (persistent disk mount)
DB_PATH = os.getenv("DB_PATH", "games.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            discord_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            steam_appid     TEXT,
            icon            TEXT,
            current_players INTEGER,
            players_2weeks  INTEGER,
            owners          TEXT,
            avg_forever     INTEGER,
            status          TEXT DEFAULT 'unchecked',
            last_checked    TEXT,
            first_seen      TEXT DEFAULT (datetime('now')),
            notes           TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT DEFAULT (datetime('now')),
            finished_at     TEXT,
            total_games     INTEGER DEFAULT 0,
            total_processed INTEGER DEFAULT 0,
            total_dead      INTEGER DEFAULT 0,
            new_dead        INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'running'
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            game_discord_id TEXT,
            event_type      TEXT,
            sent_at         TEXT DEFAULT (datetime('now')),
            webhook_msg_id  TEXT
        )""")

        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_games_status ON games(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_games_steam  ON games(steam_appid)")
        # migration auto des nouvelles colonnes (DB existante OK)
        for col, typ in [("total_reviews","INTEGER"),("last_review_ts","INTEGER"),("last_news_ts","INTEGER"),
                         ("app_type","TEXT"),("release_date","TEXT"),("developers","TEXT"),("publishers","TEXT"),
                         ("contact","TEXT"),("email","TEXT"),("website","TEXT"),("is_free","INTEGER"),("price","INTEGER"),("early_access","INTEGER"),
                         ("zero_streak","INTEGER DEFAULT 0"),("score","INTEGER DEFAULT 0"),("pinned","INTEGER DEFAULT 0"),("self_pub","INTEGER"),("outreach","TEXT DEFAULT 'new'"),("outreach_note","TEXT"),("follow_up","TEXT")]:
            try:
                await db.execute(f"ALTER TABLE games ADD COLUMN {col} {typ}")
            except Exception:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_games_score ON games(score)")
        await db.commit()


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        yield db


async def get_stats() -> dict:
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM games")
        total  = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM games WHERE status='dead'")
        dead   = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM games WHERE status='alive'")
        alive  = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT COUNT(*) FROM games
            WHERE status='dead' AND last_checked >= datetime('now', '-1 day')
        """)
        new_dead = (await cursor.fetchone())[0]

        cursor = await db.execute("""
            SELECT id, started_at, finished_at, status, total_processed, total_games, total_dead
            FROM scans ORDER BY id DESC LIMIT 1
        """)
        last_scan = await cursor.fetchone()

        return {
            "total":        total,
            "dead":         dead,
            "alive":        alive,
            "new_dead_24h": new_dead,
            "last_scan":    dict(last_scan) if last_scan else None,
        }


def _n(qp, k):
    try:
        return float(qp[k]) if qp.get(k) not in (None, "") else None
    except ValueError:
        return None


def build_where(qp: dict, default_status: str = "all"):
    c, p = [], []
    st = qp.get("status") or default_status
    sts = [x for x in st.split(",") if x in ("dead", "dying", "alive", "unknown", "unchecked")]
    if sts and st != "all":
        c.append(f"status IN ({','.join('?' * len(sts))})"); p += sts
    else:
        c.append("status != 'ignored'")
    if qp.get("search"):
        c.append("name LIKE ?"); p.append("%" + qp["search"] + "%")
    if qp.get("dev"):
        c.append("(developers LIKE ? OR publishers LIKE ?)"); p += ["%" + qp["dev"] + "%"] * 2
    if qp.get("outreach"):
        c.append("COALESCE(outreach,'new') = ?"); p.append(qp["outreach"])
    rng = {"min_score": "COALESCE(score,0) >= ?", "reviews_min": "COALESCE(total_reviews,0) >= ?",
           "reviews_max": "total_reviews <= ?", "players_min": "current_players >= ?",
           "players_max": "current_players <= ?", "streak_min": "COALESCE(zero_streak,0) >= ?",
           "year_min": "CAST(substr(release_date,-4) AS INTEGER) >= ?",
           "year_max": "CAST(substr(release_date,-4) AS INTEGER) <= ?"}
    for k, sql in rng.items():
        v = _n(qp, k)
        if v is not None:
            c.append(sql); p.append(v)
    v = _n(qp, "inactive_min")
    if v is not None:
        c.append("MAX(COALESCE(last_review_ts,0), COALESCE(last_news_ts,0)) <= ?"); p.append(time.time() - v * 86400)
    v = _n(qp, "price_max")
    if v is not None:
        c.append("(is_free = 1 OR price <= ?)"); p.append(v * 100)
    flags = {"has_contact": "COALESCE(contact,'') != ''",
             "has_email": "(COALESCE(email,'') != '' OR COALESCE(contact,'') LIKE '%@%')",
             "has_site": "(COALESCE(website,'') != '' OR (COALESCE(contact,'') != '' AND contact NOT LIKE '%@%'))",
             "free_only": "is_free = 1", "early": "early_access = 1",
             "self_pub": "(self_pub = 1 OR (self_pub IS NULL AND COALESCE(developers,'') != '' AND LOWER(developers) = LOWER(COALESCE(publishers,''))))"}
    for k, sql in flags.items():
        if str(qp.get(k, "")).lower() in ("1", "true"):
            c.append(sql)
    return "WHERE " + " AND ".join(c), p


async def get_games(qp: dict) -> dict:
    where, params = build_where(qp)
    sort_map = {"score": "COALESCE(score,0) DESC, COALESCE(total_reviews,999999) ASC",
                "current": "COALESCE(current_players, 999999) ASC", "name": "name ASC",
                "last_checked": "last_checked DESC"}
    order = sort_map.get(qp.get("sort"), sort_map["score"])
    page = max(1, int(_n(qp, "page") or 1)); per = max(1, min(200, int(_n(qp, "per_page") or 50)))
    async with get_db() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM games {where}", params)
        total = (await cur.fetchone())[0]
        cur = await db.execute(f"""
            SELECT discord_id, name, steam_appid, icon, current_players, total_reviews, score, contact,
                   release_date, developers, publishers, zero_streak, last_review_ts, last_news_ts,
                   is_free, price, early_access, email, website, self_pub, outreach, outreach_note, follow_up, pinned, status, last_checked, notes
            FROM games {where} ORDER BY {order} LIMIT ? OFFSET ?""", params + [per, (page - 1) * per])
        games = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute("SELECT developers, COUNT(*) FROM games WHERE status IN ('dead','dying') AND COALESCE(developers,'') != '' GROUP BY developers")
        devn = {r[0]: r[1] for r in await cur.fetchall()}
    import scoring
    cfg = await get_criteria()
    for g in games:
        g["dev_n"] = devn.get(g.get("developers"), 0)
        g["bd"] = [[cfg[k]["label"], v] for k, v in scoring.breakdown(g, cfg).items() if v] if g["status"] in ("dead", "dying") else []
    return {"games": games, "total": total, "page": page, "per_page": per}


async def get_scan_history(limit: int = 20) -> list:
    async with get_db() as db:
        cursor = await db.execute("""
            SELECT id, started_at, finished_at, total_games, total_processed,
                   total_dead, new_dead, status
            FROM scans ORDER BY id DESC LIMIT ?
        """, [limit])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def upsert_game(game: dict):
    cols = list(game.keys())
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "discord_id")
    async with get_db() as db:
        await db.execute(f"""
            INSERT INTO games ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})
            ON CONFLICT(discord_id) DO UPDATE SET {updates}
        """, game)
        await db.commit()


async def create_scan() -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO scans (status) VALUES ('running') RETURNING id"
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0]


async def update_scan(scan_id: int, **kwargs):
    if not kwargs:
        return
    sets   = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [scan_id]
    async with get_db() as db:
        await db.execute(f"UPDATE scans SET {sets} WHERE id = ?", values)
        await db.commit()


async def patch_game_status(discord_id: str, status: str, notes: Optional[str] = None):
    async with get_db() as db:
        if notes is not None:
            await db.execute(
                "UPDATE games SET status=?, notes=? WHERE discord_id=?",
                [status, notes, discord_id],
            )
        else:
            await db.execute(
                "UPDATE games SET status=? WHERE discord_id=?",
                [status, discord_id],
            )
        await db.commit()


# ── Critères de score + export ───────────────────────────────────────────────
async def get_criteria() -> dict:
    import scoring
    async with get_db() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='criteria'")
        row = await cur.fetchone()
    return scoring.merge(json.loads(row[0]) if row else {})


async def set_criteria(body: dict) -> dict:
    import scoring
    cfg = scoring.merge(body)
    slim = {k: {a: b for a, b in v.items() if v.get("custom") or a != "label"} for k, v in cfg.items()}
    async with get_db() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('criteria', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [json.dumps(slim)])
        await db.commit()
    return cfg


async def recompute_scores() -> int:
    import scoring
    cfg = await get_criteria()
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM games WHERE status IN ('dead','dying')")
        rows = [dict(r) for r in await cur.fetchall()]
        await db.executemany("UPDATE games SET score=? WHERE discord_id=?",
                             [(scoring.compute(r, cfg), r["discord_id"]) for r in rows])
        await db.commit()
    return len(rows)


async def export_rows(qp: dict) -> list:
    where, params = build_where(qp, "dead,dying")
    async with get_db() as db:
        cur = await db.execute(f"SELECT * FROM games {where} ORDER BY COALESCE(score,0) DESC, COALESCE(total_reviews,999999) ASC", params)
        return [dict(r) for r in await cur.fetchall()]


# ── Config du scanner (modifiable depuis l'UI ; l'env donne les valeurs par défaut) ──
def _e(name, d):
    return int(os.getenv(name, d))

SCAN_DEFAULTS = {
    "max_players": 0, "max_reviews": _e("MAX_REVIEWS", 0), "dying_max_reviews": _e("DYING_MAX_REVIEWS", 10),
    "use_age": True, "min_age_days": _e("MIN_AGE_DAYS", 180),
    "use_inactivity": True, "min_inactive_days": _e("MIN_INACTIVE_DAYS", 365),
    "use_news": True, "recheck_alive_days": _e("RECHECK_ALIVE_DAYS", 7),
    "notify_min_score": _e("NOTIFY_MIN_SCORE", 40), "notify_require_contact": False,
    "exclude_publishers": "", "fetch_reviews_all": True, "min_zero_streak": 1, "concurrency": 8, "notify_max_per_scan": 20, "offer_price": "",
}


def _merge_scan(saved: dict) -> dict:
    out = {}
    for k, d in SCAN_DEFAULTS.items():
        v = (saved or {}).get(k, d)
        if isinstance(d, bool):
            v = bool(v)
        elif isinstance(d, int):
            try:
                v = max(0, min(100000, int(v)))
            except (TypeError, ValueError):
                v = d
        else:
            v = str(v)[:500]
        out[k] = v
    return out


async def get_scanner_cfg() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='scanner'")
        row = await cur.fetchone()
    return _merge_scan(json.loads(row[0]) if row else {})


async def set_scanner_cfg(body: dict) -> dict:
    cfg = _merge_scan(body)
    async with get_db() as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('scanner', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [json.dumps(cfg)])
        await db.commit()
    return cfg


# ── Suivi des offres + verrou manuel ─────────────────────────────────────────
OUTREACH = ("new", "contacted", "replied", "accepted", "declined", "no_answer")


async def set_outreach(did: str, body: dict):
    st = body.get("status") if body.get("status") in OUTREACH else "new"
    async with get_db() as db:
        await db.execute("UPDATE games SET outreach=?, outreach_note=?, follow_up=? WHERE discord_id=?",
                         [st, str(body.get("note", ""))[:1000], str(body.get("follow_up", ""))[:10], did])
        await db.commit()


async def set_pinned(did: str, pinned: bool):
    async with get_db() as db:
        await db.execute("UPDATE games SET pinned=? WHERE discord_id=?", [int(pinned), did])
        await db.commit()


async def funnel() -> dict:
    async with get_db() as db:
        cur = await db.execute("SELECT COALESCE(outreach,'new'), COUNT(*) FROM games WHERE status IN ('dead','dying') GROUP BY 1")
        return {r[0]: r[1] for r in await cur.fetchall()}
