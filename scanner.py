"""
scanner.py — scanner par étapes : checks gratuits d'abord, appels lourds seulement pour les vrais candidats.
Plus de SteamSpy (estimations + 1 req/s). Uniquement des données officielles Steam.
"""
import asyncio, os
import aiohttp
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

import database as db
import scoring

MAX_REVIEWS        = int(os.getenv("MAX_REVIEWS", "0"))         # 'dead'  : <= N reviews
DYING_MAX_REVIEWS  = int(os.getenv("DYING_MAX_REVIEWS", "10"))  # 'dying' : <= N reviews
MIN_AGE_DAYS       = int(os.getenv("MIN_AGE_DAYS", "180"))      # sorti depuis au moins X jours
MIN_INACTIVE_DAYS  = int(os.getenv("MIN_INACTIVE_DAYS", "365")) # aucune review/news depuis X jours
RECHECK_ALIVE_DAYS = int(os.getenv("RECHECK_ALIVE_DAYS", "7"))  # ne pas re-tester un jeu vivant avant X jours
NOTIFY_MIN_SCORE   = int(os.getenv("NOTIFY_MIN_SCORE", "40"))   # score mini pour la notif Discord

DISCORD_DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"
PLAYERS_URL  = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
REVIEWS_URL  = "https://store.steampowered.com/appreviews/{appid}"
DETAILS_URL  = "https://store.steampowered.com/api/appdetails"
NEWS_URL     = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"

CONCURRENCY_STEAM = 8


def _now():
    return datetime.now(timezone.utc)


def _parse_date(s: str) -> Optional[datetime]:
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _days_since(ts: Optional[float]) -> Optional[int]:
    return None if not ts else int((_now().timestamp() - ts) // 86400)


class Scanner:
    def __init__(self, broadcast_fn: Callable[[dict], Awaitable[None]] = None):
        self.is_running = False
        self._stop_flag = False
        self._broadcast = broadcast_fn or (lambda d: asyncio.sleep(0))
        self.known: dict = {}
        self.scan_id: Optional[int] = None
        self.progress = 0
        self.total = 0
        self.current_game = ""
        self.dead_count = 0
        self.new_dead_count = 0
        self.started_at: Optional[str] = None
        self.failed = 0
        self.pending = []
        self.ok = 0
        self.abort_reason = ""
        self.last_err = ""

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running, "scan_id": self.scan_id,
            "progress": self.progress, "total": self.total,
            "current_game": self.current_game, "dead_count": self.dead_count,
            "new_dead_count": self.new_dead_count, "started_at": self.started_at, "failed": self.failed, "last_err": self.last_err,
            "pct": round(self.progress / self.total * 100) if self.total else 0,
        }

    def stop(self):
        self._stop_flag = True

    async def _broadcast_progress(self):
        await self._broadcast({"type": "scan_progress", **self.get_status()})

    # ── HTTP avec retry/backoff (avant : un 429 = jeu classé "alive" par erreur) ──
    async def _get_json(self, session, url, params=None, timeout=12, retries=4):
        for attempt in range(retries):
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout),
                                       headers={"User-Agent": "SideQuestScanner/1.0 (+https://side-quest.pro)"}) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    self.last_err = f"HTTP {r.status} from {url.split('/')[2]}"
                    if r.status in (403, 429, 500, 502, 503):
                        try:
                            wait = float(r.headers.get("Retry-After", 2 ** attempt * 2))
                        except ValueError:
                            wait = 2 ** attempt * 2
                        await asyncio.sleep(min(wait, 60))
                        continue
                    return None
            except (asyncio.TimeoutError, aiohttp.ClientError):
                self.last_err = "timeout / network error"
                await asyncio.sleep(2 ** attempt)
            except ValueError:
                self.last_err = "invalid JSON"
                return None
        return None

    @staticmethod
    def _extract_steam_appid(game: dict) -> Optional[str]:
        for sku in game.get("third_party_skus", []):
            if sku.get("distributor") == "steam":
                return sku.get("id")
        return None

    # ── Étape 1 : joueurs (officiel). None = échec/inconnu → on ne conclut RIEN ──
    async def _players(self, session, appid) -> Optional[int]:
        data = await self._get_json(session, PLAYERS_URL, {"appid": appid})
        if data is None:
            return None                                     # vrai échec réseau
        resp = data.get("response", {})
        return resp.get("player_count") if resp.get("result") == 1 else -1   # -1 = Steam répond mais sans stats pour cette appli

    # ── Étape 2 : reviews réelles + date de la dernière review ──
    async def _reviews(self, session, appid):
        data = await self._get_json(session, REVIEWS_URL.format(appid=appid), {
            "json": 1, "language": "all", "purchase_type": "all",
            "filter": "recent", "num_per_page": 1})
        if not data or data.get("success") != 1:
            return None, None
        total = data.get("query_summary", {}).get("total_reviews")
        revs = data.get("reviews") or []
        return total, (revs[0].get("timestamp_created") if revs else None)

    async def _news_ts(self, session, appid) -> Optional[int]:
        data = await self._get_json(session, NEWS_URL, {"appid": appid, "count": 1, "maxlength": 1})
        items = (data or {}).get("appnews", {}).get("newsitems") or []
        return items[0].get("date") if items else None

    async def _details(self, session, appid) -> Optional[dict]:
        data = await self._get_json(session, DETAILS_URL, {"appids": appid, "l": "english", "cc": "us"})
        app = (data or {}).get(str(appid), {})
        return app.get("data") if app.get("success") else {}  # {} = page retirée ; None = échec réseau

    async def _evaluate(self, session, game_raw, steam_sem, details_sem, notifier):
        appid = self._extract_steam_appid(game_raw)
        if not appid:
            return None
        name, did = game_raw.get("name", "Unknown"), game_raw.get("id", "")
        self.current_game = name
        sc = self.sc
        k = self.known.get(did)
        if k:
            if k["status"] == "ignored" or k.get("pinned"):
                return None
            if k["status"] == "unknown" and k["last_checked"]:
                try:
                    if (_now() - datetime.fromisoformat(k["last_checked"]).replace(tzinfo=timezone.utc)).days < 30:
                        return None
                except ValueError:
                    pass
            if k["status"] == "alive" and k["last_checked"] and (k["current_players"] or 0) > 20 and (k["total_reviews"] is not None or not sc["fetch_reviews_all"]):
                try:
                    if (_now() - datetime.fromisoformat(k["last_checked"]).replace(tzinfo=timezone.utc)).days < sc["recheck_alive_days"]:
                        return None
                except ValueError:
                    pass
        base = {"discord_id": did, "name": name, "steam_appid": appid, "icon": game_raw.get("icon"),
                "last_checked": datetime.utcnow().isoformat()}

        async with steam_sem:
            players = await self._players(session, appid)
        if players is None:
            self.failed += 1
            return None                                     # échec → on garde l'ancien état
        if players < 0:                                     # appli fantôme / retirée : ce n'est pas une erreur
            await db.upsert_game({**base, "status": "unknown", "score": 0, "notes": "no player data from Steam"})
            return "unknown", False
        streak = (k["zero_streak"] or 0) + 1 if (players == 0 and k) else (1 if players == 0 else 0)
        base.update(current_players=players, zero_streak=streak)

        if players > sc["max_players"]:                                     # vivant → fin, 1 seul appel utilisé
            if sc["fetch_reviews_all"]:
                async with steam_sem:
                    rv, lr = await self._reviews(session, appid)
                if rv is not None:
                    base.update(total_reviews=rv, last_review_ts=lr)
            await db.upsert_game({**base, "status": "alive", "score": 0})
            return "alive", False

        async with steam_sem:
            reviews, last_review = await self._reviews(session, appid)
        if reviews is None:
            self.failed += 1
            return None
        base.update(total_reviews=reviews, last_review_ts=last_review)
        if reviews > sc["dying_max_reviews"]:
            await db.upsert_game({**base, "status": "alive", "score": 0})
            return "alive", False

        async with details_sem:                             # appdetails = limité (~200/5min) → survivants only
            info = await self._details(session, appid)
            await asyncio.sleep(1.6)
        if info is None:
            self.failed += 1
            return None
        if not info:                                        # page retirée : rien à acheter
            await db.upsert_game({**base, "status": "unknown", "score": 0, "notes": "page retirée / indisponible"})
            return "unknown", False
        if info.get("type") != "game":
            await db.upsert_game({**base, "status": "ignored", "app_type": info.get("type"), "score": 0})
            return None
        ex = [t.strip().lower() for t in sc["exclude_publishers"].split(",") if t.strip()]
        who = " ".join((info.get("developers") or []) + (info.get("publishers") or [])).lower()
        if any(t in who for t in ex):
            await db.upsert_game({**base, "status": "unknown", "score": 0, "notes": "excluded publisher"})
            return "unknown", False
        rel = info.get("release_date") or {}
        released = _parse_date(rel.get("date", "")) if not rel.get("coming_soon") else None
        age = (_now() - released).days if released else None
        async with steam_sem:
            news_ts = await self._news_ts(session, appid) if sc["use_news"] else None
        acts = [t for t in (last_review, news_ts) if t]
        inactive = _days_since(max(acts)) if acts else None

        devs = [x.strip().lower() for x in info.get("developers") or []]
        pubs = [x.strip().lower() for x in info.get("publishers") or []]
        sup = info.get("support_info") or {}
        email = (sup.get("email") or "").strip()
        email = email if "@" in email and " " not in email else ""
        url = (sup.get("url") or info.get("website") or "").strip()
        url = url if url.startswith(("http://", "https://")) else ""
        contact = email or url
        price = (info.get("price_overview") or {}).get("final")
        early = any(g.get("description") == "Early Access" for g in info.get("genres") or [])
        old_enough = (not sc["use_age"]) or age is None or age >= sc["min_age_days"]
        quiet = (not sc["use_inactivity"]) or inactive is None or inactive >= sc["min_inactive_days"]

        if rel.get("coming_soon") or not old_enough:
            status = "alive"                                # pas sorti / trop récent → pas "mort"
        elif reviews <= sc["max_reviews"] and quiet and streak >= sc["min_zero_streak"]:
            status = "dead"
        elif quiet or (inactive is not None and inactive >= 180):
            status = "dying"
        else:
            status = "alive"
        rec = {**base, "status": status, "app_type": "game", "last_news_ts": news_ts,
               "release_date": rel.get("date"),
               "developers": ", ".join(info.get("developers") or []),
               "publishers": ", ".join(info.get("publishers") or []),
               "contact": contact, "email": email, "website": url, "self_pub": int(bool(set(devs) & set(pubs))),
               "is_free": int(bool(info.get("is_free"))), "price": price, "early_access": int(early)}
        score = scoring.compute(rec, self.cfg) if status != "alive" else 0
        await db.upsert_game({**rec, "score": score})
        is_new = status == "dead" and (not k or k["status"] != "dead")
        if is_new and notifier and score >= sc["notify_min_score"] and (contact or not sc["notify_require_contact"]):
            self.pending.append(dict(name=name, discord_id=did, steam_appid=appid, current_players=players,
                                     total_reviews=reviews, inactive_days=inactive, score=score, contact=contact))
        return status, is_new

    async def _process_game(self, session, game_raw, steam_sem, details_sem, notifier):
        if self._stop_flag:
            return
        try:
            res = await self._evaluate(session, game_raw, steam_sem, details_sem, notifier)
        except Exception as e:
            print(f"[!] {game_raw.get('name')}: {e}")
            self.failed += 1
            res = None
        if res:
            self.ok += 1
        if self.failed >= 12 and self.ok == 0 and not self._stop_flag:
            self._stop_flag, self.abort_reason = True, self.last_err or "no answer from Steam"
        self.progress += 1
        if res and res[0] == "dead":
            self.dead_count += 1
        if res and res[1]:
            self.new_dead_count += 1
        await self._broadcast_progress()

    async def diagnose(self) -> list:
        """Teste chaque service une fois, DEPUIS le serveur (Render), pour voir qui bloque."""
        import time
        checks = [("Discord game list", DISCORD_DETECTABLE_URL, None),
                  ("Steam players (CS2)", PLAYERS_URL, {"appid": 730}),
                  ("Steam reviews (CS2)", REVIEWS_URL.format(appid=730), {"json": 1, "num_per_page": 1}),
                  ("Steam appdetails (CS2)", DETAILS_URL, {"appids": 730, "filters": "basic"}),
                  ("Steam news (CS2)", NEWS_URL, {"appid": 730, "count": 1})]
        out = []
        async with aiohttp.ClientSession() as session:
            for name, url, params in checks:
                t = time.perf_counter()
                try:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12),
                                           headers={"User-Agent": "SideQuestScanner/1.0 (+https://side-quest.pro)"}) as r:
                        body = (await r.content.read(110)).decode("utf-8", "replace").replace("\n", " ")
                        out.append({"name": name, "ok": r.status == 200, "status": r.status,
                                    "ms": int((time.perf_counter() - t) * 1000), "body": body})
                except Exception as e:
                    out.append({"name": name, "ok": False, "status": 0,
                                "ms": int((time.perf_counter() - t) * 1000), "body": type(e).__name__})
        return out

    async def run(self, notifier=None):
        if self.is_running:
            return
        self.is_running, self._stop_flag = True, False
        self.progress = self.total = self.dead_count = self.new_dead_count = 0
        self.failed, self.pending = 0, []
        self.ok, self.abort_reason, self.last_err = 0, "", ""
        self.started_at = datetime.utcnow().isoformat()
        self.current_game = ""
        self.scan_id = await db.create_scan()
        await self._broadcast({"type": "scan_started", "scan_id": self.scan_id})
        try:
            async with db.get_db() as conn:
                cur = await conn.execute(
                    "SELECT discord_id, status, last_checked, current_players, zero_streak, total_reviews, pinned FROM games")
                self.known = {r["discord_id"]: dict(r) for r in await cur.fetchall()}

            self.cfg = await db.get_criteria()
            self.sc = await db.get_scanner_cfg()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50)) as session:
                self.current_game = "Fetching Discord games..."
                await self._broadcast_progress()
                async with session.get(DISCORD_DETECTABLE_URL, headers={"User-Agent": "Mozilla/5.0"}) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Discord game list unavailable (HTTP {r.status})")
                    discord_games = await r.json()
                steam_games = [g for g in discord_games if self._extract_steam_appid(g)]
                if not steam_games:
                    raise RuntimeError("Discord returned no Steam games")
                self.total = len(steam_games)
                await db.update_scan(self.scan_id, total_games=self.total)
                await self._broadcast_progress()

                steam_sem, details_sem = asyncio.Semaphore(max(1, self.sc["concurrency"])), asyncio.Semaphore(1)
                await asyncio.gather(*[self._process_game(session, g, steam_sem, details_sem, notifier)
                                       for g in steam_games], return_exceptions=True)

            if self.abort_reason:
                raise RuntimeError("Scan aborted, Steam is not answering: " + self.abort_reason)
            finish = "stopped" if self._stop_flag else "completed"
            await db.update_scan(self.scan_id, finished_at=datetime.utcnow().isoformat(),
                                 total_processed=self.progress, total_dead=self.dead_count,
                                 new_dead=self.new_dead_count, status=finish)
            await self._broadcast({"type": "scan_complete", "scan_id": self.scan_id,
                                   "total_processed": self.progress, "total_dead": self.dead_count,
                                   "new_dead": self.new_dead_count, "failed": self.failed, "status": finish})
            if notifier:
                top = sorted(self.pending, key=lambda a: -a["score"])[:self.sc["notify_max_per_scan"]]
                if top:
                    await notifier.notify_batch(top)
                await notifier.notify_scan_complete(total=self.progress, dead=self.dead_count,
                                                    new_dead=self.new_dead_count)
        except Exception as e:
            print(f"[scan failed] {e}", flush=True)
            await db.update_scan(self.scan_id, status="failed", finished_at=datetime.utcnow().isoformat())
            await self._broadcast({"type": "scan_error", "error": str(e)})
        finally:
            self.is_running = False
