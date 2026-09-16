"""
The JSON API the web dashboard trades through.

Mounted by keepalive.py at /api, so every route below lives under /api/v1/...
on whatever URL your host gives the bot.

THE ONE RULE THIS MODULE FOLLOWS
--------------------------------
A website order calls the SAME method a slash command calls:
Stocks._execute_buy / _execute_sell. Not a copy of it, not a simplified
version of it. Every cooldown, order cap, rolling volume cap, circuit breaker,
insider rule and price-impact calculation therefore applies identically, and
anything added to those methods later applies to the website automatically
without anyone remembering to update this file. If you find yourself about to
write trading logic in here, that is the bug.

TWO LAYERS OF AUTHENTICATION
----------------------------
  X-API-Key       proves the CALLER is your website. Required on every route
                  except /health. This is WEB_API_KEY, set as an environment
                  variable on your host.
  Authorization:  proves WHICH PLAYER the call is for. A session token from
  Bearer <token>  /weblogin, required on anything player-specific.

Market data needs only the API key, so your site can show the market to a
visitor who has not signed in. Portfolio and orders need both.

The recommended Base44 setup calls this from a backend function, where the API
key stays server-side and CORS never applies. If you call it straight from the
browser instead, the key is visible to anyone who opens devtools — narrow
WEB_API_CORS_ORIGINS and understand that the key is then public.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque

from aiohttp import web

import config

log = logging.getLogger("beamng-eco-bot.webapi")

API_VERSION = "v1"


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def api_key() -> str:
    """The shared secret, environment variable first.

    config.py is committed and copied around; the environment variable is not.
    So the environment always wins, and a key left in config.py is only ever a
    fallback for someone testing locally.
    """
    return (os.getenv("WEB_API_KEY") or getattr(config, "WEB_API_KEY", "") or "").strip()


def dashboard_url() -> str:
    return (os.getenv("WEB_DASHBOARD_URL") or getattr(config, "WEB_DASHBOARD_URL", "") or "").strip()


def enabled() -> bool:
    """The API only starts when it is switched on AND has a key.

    Starting a trading API with no key because someone forgot to set one is the
    kind of default that empties an economy, so an unkeyed API stays off and
    says why.
    """
    if not getattr(config, "WEB_API_ENABLED", False):
        return False
    if not api_key():
        log.warning(
            "WEB_API_ENABLED is on but no WEB_API_KEY is set — the web API stays OFF. "
            "Set WEB_API_KEY in your host's environment variables."
        )
        return False
    return True


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def response(self) -> web.Response:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return web.json_response(body, status=self.status)


# --------------------------------------------------------------------------- #
# Rate limiting: a sliding window per player, in memory.
#
# In memory is the right call here: the limits exist to stop a script hammering
# a small host, and the bot restarting resets them. Writing them to the
# database would put a write on every single read request, which is exactly
# what the limit is trying to prevent.
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)

    def check(self, key: str, limit: int):
        if limit <= 0:
            return
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(60 - (now - window[0])))
            raise ApiError(
                429, "rate_limited",
                "Too many requests. Slow down and try again shortly.",
                retry_after=retry_after,
            )
        window.append(now)

    def sweep(self):
        """Drops idle buckets so a busy week doesn't grow this forever."""
        now = time.monotonic()
        for key in list(self._hits):
            window = self._hits[key]
            while window and now - window[0] > 60:
                window.popleft()
            if not window:
                del self._hits[key]


# --------------------------------------------------------------------------- #
# Serialisers — one place where the JSON shape of a stock is decided, so the
# market list, the single-stock view and the portfolio can never disagree.
# --------------------------------------------------------------------------- #
def pct_change(price: float, prev: float) -> float:
    if not prev:
        return 0.0
    return round((price - prev) / prev * 100, 2)


def serialise_listing(row) -> dict:
    biz_id, name, owner_id, price, prev_price, total, available, delisted = row
    return {
        "id": int(biz_id),
        "name": name,
        "owner_id": str(owner_id) if owner_id else None,
        "price": round(float(price), 2),
        "previous_price": round(float(prev_price or price), 2),
        "change_pct": pct_change(float(price), float(prev_price or 0)),
        "total_shares": int(total),
        "available_shares": int(available),
        "market_cap": round(float(price) * int(total), 2),
        "delisted": bool(delisted),
    }


def serialise_holding(row) -> dict:
    biz_id, name, shares, cost_basis, price, delisted = row
    shares = int(shares)
    cost_basis = int(cost_basis)
    value = round(float(price) * shares, 2)
    return {
        "business_id": int(biz_id),
        "name": name,
        "shares": shares,
        "cost_basis": cost_basis,
        "average_cost": round(cost_basis / shares, 2) if shares else 0.0,
        "price": round(float(price), 2),
        "value": value,
        "profit": round(value - cost_basis, 2),
        "profit_pct": round((value - cost_basis) / cost_basis * 100, 2) if cost_basis else 0.0,
        "delisted": bool(delisted),
    }


# --------------------------------------------------------------------------- #
# The application
# --------------------------------------------------------------------------- #
def create_app(bot) -> web.Application:
    import webdb

    app = web.Application()
    limiter = RateLimiter()
    app["bot"] = bot
    app["limiter"] = limiter

    db = bot.db

    # ---------------- auth helpers ---------------------------------------- #
    def require_api_key(request):
        supplied = request.headers.get("X-API-Key", "")
        expected = api_key()
        # Compare every byte regardless of where the first difference is, so
        # response timing can't be used to guess the key character by character.
        if not supplied or not _constant_time_equals(supplied, expected):
            raise ApiError(401, "bad_api_key", "Missing or invalid X-API-Key header.")

    async def require_session(request):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            raise ApiError(401, "no_session", "Missing session token. Sign in with /weblogin in Discord.")
        session = await webdb.session_for_token(db, token)
        if not session:
            raise ApiError(
                401, "session_expired",
                "Your session has expired. Run /weblogin in Discord to get a new code.",
            )
        return session  # (user_id, guild_id, expires_at)

    def guild_scope(request) -> int:
        """Which server the request is about, for routes with no session."""
        raw = request.query.get("guild_id") or os.getenv("GUILD_ID") or ""
        if not str(raw).isdigit():
            raise ApiError(400, "bad_guild", "guild_id is missing or not numeric.")
        return int(raw)

    # ---------------- middleware ------------------------------------------ #
    @web.middleware
    async def error_middleware(request, handler):
        try:
            return await handler(request)
        except ApiError as exc:
            return exc.response()
        except web.HTTPException:
            raise
        except Exception:
            # A traceback in the console, a flat sentence to the browser: the
            # site must never render a stack trace at a player.
            log.exception("Unhandled error in %s %s", request.method, request.path)
            return ApiError(500, "server_error", "Something went wrong handling that request.").response()

    @web.middleware
    async def cors_middleware(request, handler):
        origins = getattr(config, "WEB_API_CORS_ORIGINS", "*") or "*"
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = origins
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization, Idempotency-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
        return response

    app.middlewares.append(error_middleware)
    app.middlewares.append(cors_middleware)

    # ---------------- routes: health -------------------------------------- #
    async def health(request):
        """No API key needed: this is what you curl to prove the API is up."""
        return web.json_response({
            "status": "ok",
            "api_version": API_VERSION,
            "bot": str(bot.user) if bot.user else None,
            "ready": bot.is_ready(),
        })

    # ---------------- routes: auth ---------------------------------------- #
    async def redeem(request):
        require_api_key(request)
        body = await _json_body(request)
        code = str(body.get("code", "")).strip()
        if not code:
            raise ApiError(400, "no_code", "No sign-in code supplied.")

        # Rate limited by code shape rather than by player, because nobody is
        # signed in yet. Ten attempts a minute from one caller is plenty for a
        # human retyping a code and far too few to brute-force one.
        limiter.check(f"redeem:{request.remote}", 10)

        result = await webdb.redeem_login_code(db, code)
        if not result:
            raise ApiError(
                401, "bad_code",
                "That code is invalid, expired or already used. Run /weblogin in Discord for a new one.",
            )
        token, user_id, guild_id, expires_at = result
        return web.json_response({
            "token": token,
            "expires_at": expires_at,
            "user": await _describe_user(bot, user_id, guild_id),
        })

    async def me(request):
        require_api_key(request)
        user_id, guild_id, expires_at = await require_session(request)
        limiter.check(f"read:{user_id}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))
        cash, bank = await db.get_balance(user_id, guild_id)
        return web.json_response({
            "user": await _describe_user(bot, user_id, guild_id),
            "session_expires_at": expires_at,
            "cash": int(cash),
            "bank": int(bank),
        })

    async def logout(request):
        require_api_key(request)
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        revoked = await webdb.revoke_token(db, token) if token else False
        return web.json_response({"ok": True, "revoked": bool(revoked)})

    # ---------------- routes: market data --------------------------------- #
    async def market(request):
        require_api_key(request)
        guild_id = guild_scope(request)
        limiter.check(f"market:{request.remote}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))

        rows = await db.list_businesses(guild_id)
        listings = [serialise_listing(r) for r in rows]
        listings.sort(key=lambda item: item["change_pct"], reverse=True)

        stocks_cog = bot.get_cog("Stocks")
        next_tick = await stocks_cog.next_tick_timestamp() if stocks_cog else None
        return web.json_response({
            "listings": listings,
            "next_tick_at": next_tick,
            "tick_minutes": int(getattr(config, "STOCK_TICK_MINUTES", 0) or 0),
            "server_time": int(time.time()),
        })

    async def stock_detail(request):
        require_api_key(request)
        guild_id = guild_scope(request)
        business_id = _int_param(request, "business_id")
        limiter.check(f"market:{request.remote}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))

        row = await db.get_business_by_id(business_id, guild_id)
        if not row:
            raise ApiError(404, "not_found", "No listed business with that ID.")

        state = await db.get_business_trade_state(business_id)
        halted = bool(state[11]) if state else False
        stocks_cog = bot.get_cog("Stocks")
        payload = serialise_listing(row)
        payload["halted"] = halted
        payload["resumes_at"] = (
            await stocks_cog.next_tick_timestamp() if (halted and stocks_cog) else None
        )
        # (business_id, change_percent, reason, actor_id, timestamp)
        events = await db.get_recent_stock_events(guild_id, business_id, 10)
        payload["events"] = [
            {
                "ts": int(ev[4]),
                "change_pct": round(float(ev[1] or 0), 2),
                "reason": ev[2],
                "actor_id": str(ev[3]) if ev[3] else None,
            }
            for ev in (events or [])
        ]
        return web.json_response(payload)

    async def stock_history(request):
        require_api_key(request)
        guild_scope(request)
        business_id = _int_param(request, "business_id")
        hours = min(int(request.query.get("hours", 24) or 24), 24 * int(getattr(config, "WEB_PRICE_HISTORY_DAYS", 14)))
        limiter.check(f"market:{request.remote}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))

        since = int(time.time()) - hours * 3600
        rows = await webdb.price_history(db, business_id, since)
        return web.json_response({
            "business_id": business_id,
            "hours": hours,
            "points": [{"t": int(ts), "p": round(float(price), 2)} for ts, price in rows],
        })

    # ---------------- routes: player -------------------------------------- #
    async def portfolio(request):
        require_api_key(request)
        user_id, guild_id, _ = await require_session(request)
        limiter.check(f"read:{user_id}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))

        rows = await db.get_portfolio(user_id, guild_id)
        holdings = [serialise_holding(r) for r in rows]
        cash, bank = await db.get_balance(user_id, guild_id)
        invested = sum(h["cost_basis"] for h in holdings)
        value = sum(h["value"] for h in holdings)
        return web.json_response({
            "cash": int(cash),
            "bank": int(bank),
            "holdings": holdings,
            "invested": invested,
            "market_value": round(value, 2),
            "profit": round(value - invested, 2),
            "net_worth": round(int(cash) + int(bank) + value, 2),
        })

    async def cooldowns(request):
        """Lets the site grey out the Buy/Sell buttons instead of letting a
        player click one and be told no."""
        require_api_key(request)
        user_id, guild_id, _ = await require_session(request)
        limiter.check(f"read:{user_id}", int(getattr(config, "WEB_API_READ_RATE_LIMIT", 120)))

        now = int(time.time())
        last_buy = int(await db.get_last_stock_buy(user_id, guild_id) or 0)
        last_sell = int(await db.get_last_stock_sell(user_id, guild_id) or 0)
        buy_cd = int(float(getattr(config, "STOCK_BUY_COOLDOWN_MINUTES", 0)) * 60)
        sell_cd = int(float(getattr(config, "STOCK_SELL_COOLDOWN_MINUTES", 0)) * 60)
        return web.json_response({
            "buy_ready_at": last_buy + buy_cd if last_buy else now,
            "sell_ready_at": last_sell + sell_cd if last_sell else now,
            "server_time": now,
        })

    # ---------------- routes: orders -------------------------------------- #
    async def place_order(request, side: str):
        require_api_key(request)
        user_id, guild_id, _ = await require_session(request)
        limiter.check(f"trade:{user_id}", int(getattr(config, "WEB_API_TRADE_RATE_LIMIT", 20)))

        body = await _json_body(request)
        business_id = body.get("business_id")
        shares = body.get("shares")
        if not isinstance(business_id, int) or not isinstance(shares, int):
            raise ApiError(400, "bad_request", "business_id and shares must both be whole numbers.")
        if shares < 1:
            raise ApiError(400, "bad_request", "shares must be at least 1.")

        idem_key = (request.headers.get("Idempotency-Key") or body.get("idempotency_key") or "").strip()
        if idem_key:
            # Namespaced by player so one player's key can never replay another
            # player's order back at them.
            idem_key = f"{user_id}:{idem_key}"
            remembered = await webdb.remembered_order(db, idem_key)
            if remembered:
                status, payload = remembered
                return web.json_response(json.loads(payload), status=status, headers={"Idempotent-Replay": "true"})

        stocks_cog = bot.get_cog("Stocks")
        if stocks_cog is None:
            raise ApiError(503, "unavailable", "The market is not loaded right now. Try again in a moment.")

        from cogs.stocks import OrderRejected

        try:
            if side == "buy":
                filled = await stocks_cog._execute_buy(user_id, guild_id, business_id, shares)
            else:
                filled = await stocks_cog._execute_sell(user_id, guild_id, business_id, shares)
        except OrderRejected as exc:
            # A refusal is a normal outcome, not a server fault. 409 plus the
            # machine-readable code the OrderRejected already carries, so the
            # page can show a countdown for a cooldown and a "max X shares"
            # hint for a cap rather than just printing a sentence.
            payload = {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}
            if idem_key:
                await webdb.remember_order(db, idem_key, user_id, guild_id, 409, json.dumps(payload))
            return web.json_response(payload, status=409)

        cash, _bank = await db.get_balance(user_id, guild_id)
        payload = {
            "ok": True,
            "side": side,
            "business_id": business_id,
            "name": filled["name"],
            "shares": filled["shares"],
            "price": round(float(filled["exec_price"]), 2),
            "new_price": round(float(filled["new_price"]), 2),
            "halted": bool(filled["halted"]),
            "cash": int(cash),
        }
        payload["total"] = int(filled["cost"]) if side == "buy" else int(filled["proceeds"])
        if idem_key:
            await webdb.remember_order(db, idem_key, user_id, guild_id, 200, json.dumps(payload))
        return web.json_response(payload)

    async def buy(request):
        return await place_order(request, "buy")

    async def sell(request):
        return await place_order(request, "sell")

    # ---------------- wiring ---------------------------------------------- #
    base = f"/{API_VERSION}"
    app.router.add_get(f"{base}/health", health)
    app.router.add_post(f"{base}/auth/redeem", redeem)
    app.router.add_get(f"{base}/auth/me", me)
    app.router.add_post(f"{base}/auth/logout", logout)
    app.router.add_get(f"{base}/market", market)
    app.router.add_get(f"{base}/market/{{business_id}}", stock_detail)
    app.router.add_get(f"{base}/market/{{business_id}}/history", stock_history)
    app.router.add_get(f"{base}/portfolio", portfolio)
    app.router.add_get(f"{base}/cooldowns", cooldowns)
    app.router.add_post(f"{base}/orders/buy", buy)
    app.router.add_post(f"{base}/orders/sell", sell)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204))

    async def housekeeping(app_):
        """Trims the rate-limit buckets and the idempotency table hourly."""
        try:
            while True:
                await asyncio.sleep(3600)
                limiter.sweep()
                try:
                    await webdb.prune_orders(db)
                except Exception:
                    log.exception("Could not prune old web orders")
        except asyncio.CancelledError:
            pass

    async def _start_housekeeping(app_):
        app_["housekeeping"] = asyncio.create_task(housekeeping(app_))

    async def _stop_housekeeping(app_):
        task = app_.get("housekeeping")
        if task:
            task.cancel()

    app.on_startup.append(_start_housekeeping)
    app.on_cleanup.append(_stop_housekeeping)

    log.info("Web API ready with %d routes.", len(app.router.routes()))
    return app


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _constant_time_equals(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def _json_body(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "bad_json", "Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "Request body must be a JSON object.")
    return body


def _int_param(request, name: str) -> int:
    raw = request.match_info.get(name, "")
    if not str(raw).isdigit():
        raise ApiError(400, "bad_request", f"{name} must be a whole number.")
    return int(raw)


async def _describe_user(bot, user_id: int, guild_id: int) -> dict:
    """Name and avatar for the header of the site.

    Falls back to the raw ID rather than failing: a player who has left the
    server should still be able to see their own portfolio.
    """
    name, avatar = str(user_id), None
    try:
        guild = bot.get_guild(guild_id)
        member = (guild.get_member(user_id) if guild else None) or bot.get_user(user_id)
        # Only fall back to a REST lookup when the gateway is actually up.
        # Calling it while the bot is starting or reconnecting leaves the
        # request waiting on a connection that isn't there, and the site hangs
        # on its sign-in call rather than failing fast.
        if member is None and bot.is_ready():
            member = await asyncio.wait_for(bot.fetch_user(user_id), timeout=5)
        if member is not None:
            name = getattr(member, "display_name", None) or str(member)
            avatar = member.display_avatar.url if getattr(member, "display_avatar", None) else None
    except Exception:
        pass
    return {"id": str(user_id), "guild_id": str(guild_id), "name": name, "avatar": avatar}
