"""
Tiny HTTP server used to keep the bot alive on free hosting.

Several free tiers (Render, Koyeb, Replit, Cyclic and friends) only keep a
container running if it listens on a port, and they shut it down when no
request arrives for a while. This module answers both problems:

  * it binds whatever port the host hands us in $PORT, so the platform's
    health check passes and the service is not marked as failed, and
  * it exposes "/" and "/health" so a free uptime pinger (UptimeRobot,
    cron-job.org, BetterStack) can hit the URL every few minutes and stop the
    container from being put to sleep.

It starts automatically whenever $PORT is set, which is exactly the case on
web-service style hosts. On a plain worker or VPS nothing is started and
nothing is wasted. Set KEEPALIVE=1 to force it on, KEEPALIVE=0 to force it off.
"""

import os
import logging

from aiohttp import web

try:
    import webapi
except ModuleNotFoundError:
    webapi = None

try:
    import webdb
except ModuleNotFoundError:
    webdb = None

log = logging.getLogger("beamng-eco-bot.keepalive")


def _enabled() -> bool:
    flag = os.getenv("KEEPALIVE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Auto-on only for $PORT, which is how web-service hosts say "you must
    # listen or you are dead". Panel hosts (bot-hosting.net, Pterodactyl) always
    # set SERVER_PORT even for a plain bot, so that one must never auto-start a
    # server nobody asked for — ask for it with KEEPALIVE=1.
    return bool(os.getenv("PORT"))


def _port() -> int:
    for var in ("PORT", "SERVER_PORT"):
        value = os.getenv(var, "").strip()
        if value.isdigit():
            return int(value)
    return 8080


def _api_wanted() -> bool:
    """True only if the web dashboard files are actually installed and switched on."""
    if webapi is None or webdb is None:
        if os.getenv("WEB_API_KEY"):
            log.warning(
                "WEB_API_KEY is set but webapi.py/webdb.py are not installed — "
                "running as a Discord-only bot."
            )
        return False
    try:
        return bool(webapi.enabled())
    except Exception:
        log.exception("Could not read the web API config — running without it.")
        return False


class KeepAliveServer:
    def __init__(self, bot):
        self.bot = bot
        self._runner = None

    async def _status(self, request: web.Request) -> web.Response:
        ready = self.bot.is_ready() and not self.bot.is_closed()
        body = {
            "status": "ok" if ready else "starting",
            "bot": str(self.bot.user) if self.bot.user else None,
            "latency_ms": round(self.bot.latency * 1000) if ready else None,
            "guilds": len(self.bot.guilds),
        }
        # 200 either way: a host that sees a 503 during login may decide the
        # deploy failed and restart us in a loop.
        return web.json_response(body)

    async def start(self):
        # The web dashboard's API is served by this same server, so it has to
        # come up even on a host that wouldn't otherwise need a keep-alive.
        want_api = _api_wanted()
        if not _enabled() and not want_api:
            log.info("Keep-alive server disabled (no PORT set, web API off).")
            return

        port = _port()
        app = web.Application()
        app.router.add_get("/", self._status)
        app.router.add_get("/health", self._status)
        app.router.add_head("/", lambda r: web.Response())

        if want_api:
            # The website tables are created here rather than in database.py so
            # the economy's own schema is left exactly as it was.
            await webdb.ensure_schema(self.bot.db)
            app.add_subapp("/api", webapi.create_app(self.bot))
            log.info("Web dashboard API mounted at /api/v1 (see WEBSITE.md).")
        elif os.getenv("WEB_API_KEY") and webapi is not None:
            log.warning(
                "WEB_API_KEY is set but the web API is off — check WEB_API_ENABLED in config.py."
            )

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", port)
        await site.start()
        log.info("Keep-alive server listening on port %s — point your uptime pinger at it.", port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
