"""
Database tables that belong to the web dashboard, not to the economy.

WHY THIS IS A SEPARATE MODULE
-----------------------------
The economy's schema in database.py is the thing staff back up, restore and
compare fingerprints against. Bolting website tables into it would mean every
restore and every update-safety check had to reason about browser sessions,
which have nothing to do with anyone's money. So the website's tables are
created here, on the same connection, and the economy's schema is left exactly
as it was. Delete every row in this file's tables and the economy is untouched:
players would simply have to run /weblogin again.

WHAT LIVES HERE
---------------
  web_login_codes   short-lived codes /weblogin hands out, redeemed once
  web_sessions      browser sessions, one per successful redeem
  web_orders        idempotency records, so a double-clicked Buy button or a
                    retried request cannot place the same order twice
  web_price_samples price history for the dashboard's charts

SECURITY NOTE
-------------
Login codes and session tokens are stored HASHED (SHA-256), never in plain
text. The bot has no use for the original value after it has handed it to the
player, and a database that leaks — via a backup posted to a Discord channel,
say — then leaks no working credentials.
"""

import hashlib
import logging
import secrets
import time

import config

log = logging.getLogger("beamng-eco-bot.webdb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_login_codes (
    code_hash   TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user
    ON web_sessions(user_id, guild_id);

CREATE TABLE IF NOT EXISTS web_orders (
    idem_key    TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    status      INTEGER NOT NULL,
    response    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_orders_created
    ON web_orders(created_at);

CREATE TABLE IF NOT EXISTS web_price_samples (
    business_id INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    price       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_price_samples
    ON web_price_samples(business_id, ts);
"""

# How long a /weblogin code is valid before the player has to ask for another.
LOGIN_CODE_TTL_SECONDS = 10 * 60

# How long an idempotency record is remembered. Long enough to cover any retry
# a browser or a Base44 backend function would make, short enough that the
# table stays small.
ORDER_MEMORY_SECONDS = 24 * 3600


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def ensure_schema(db):
    """Creates the website tables. Safe to call on every start."""
    await db._conn.executescript(SCHEMA)
    await db._conn.commit()


# --------------------------------------------------------------------------- #
# Login codes
# --------------------------------------------------------------------------- #
async def create_login_code(db, user_id: int, guild_id: int) -> tuple:
    """Issues a fresh sign-in code for a player and returns (code, expires_at).

    Any unused code the player already has is deleted first, so the code the
    bot just showed them is always the only one that works — otherwise a code
    read over someone's shoulder an hour ago would still be live.
    """
    now = int(time.time())
    await db._exec(
        "DELETE FROM web_login_codes WHERE user_id=? AND guild_id=? AND used_at IS NULL",
        (user_id, guild_id),
    )
    # Six groups of four from an unambiguous alphabet: no O/0 or I/1, because
    # players retype these by hand from Discord into a browser.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
    expires_at = now + LOGIN_CODE_TTL_SECONDS
    await db._exec(
        "INSERT INTO web_login_codes (code_hash, user_id, guild_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (_hash(code), user_id, guild_id, now, expires_at),
    )
    return code, expires_at


async def redeem_login_code(db, code: str):
    """Turns a valid code into a session token.

    Returns (token, user_id, guild_id, expires_at), or None if the code is
    unknown, expired or already used. The code is marked used in the same step,
    so a code is worth exactly one session.
    """
    now = int(time.time())
    code = (code or "").strip().upper()
    row = await db._fetchone(
        "SELECT user_id, guild_id, expires_at, used_at FROM web_login_codes WHERE code_hash=?",
        (_hash(code),),
    )
    if not row:
        return None
    user_id, guild_id, expires_at, used_at = row
    if used_at is not None or now > int(expires_at):
        return None

    await db._exec(
        "UPDATE web_login_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
        (now, _hash(code)),
    )

    token = secrets.token_urlsafe(32)
    session_expires = now + int(getattr(config, "WEB_SESSION_DAYS", 30)) * 86400
    await db._exec(
        "INSERT INTO web_sessions (token_hash, user_id, guild_id, created_at, expires_at, last_seen) "
        "VALUES (?,?,?,?,?,?)",
        (_hash(token), user_id, guild_id, now, session_expires, now),
    )
    return token, int(user_id), int(guild_id), session_expires


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
async def session_for_token(db, token: str):
    """Returns (user_id, guild_id, expires_at) for a live session, else None."""
    if not token:
        return None
    now = int(time.time())
    row = await db._fetchone(
        "SELECT user_id, guild_id, expires_at FROM web_sessions WHERE token_hash=?",
        (_hash(token),),
    )
    if not row:
        return None
    user_id, guild_id, expires_at = row
    if now > int(expires_at):
        await db._exec("DELETE FROM web_sessions WHERE token_hash=?", (_hash(token),))
        return None
    # last_seen is only for staff answering "when was this account last used on
    # the site?", so a minute of granularity is plenty — the WHERE clause means
    # a page doing four reads a second writes at most once a minute.
    await db._exec(
        "UPDATE web_sessions SET last_seen=? WHERE token_hash=? AND last_seen < ?",
        (now, _hash(token), now - 60),
    )
    return int(user_id), int(guild_id), int(expires_at)


async def revoke_token(db, token: str) -> bool:
    cur = await db._exec("DELETE FROM web_sessions WHERE token_hash=?", (_hash(token),))
    return bool(getattr(cur, "rowcount", 0))


async def revoke_all_sessions(db, user_id: int, guild_id: int) -> int:
    """Signs a player out of every browser. Used by /weblogout."""
    cur = await db._exec(
        "DELETE FROM web_sessions WHERE user_id=? AND guild_id=?", (user_id, guild_id)
    )
    return int(getattr(cur, "rowcount", 0) or 0)


async def count_sessions(db, user_id: int, guild_id: int) -> int:
    row = await db._fetchone(
        "SELECT COUNT(*) FROM web_sessions WHERE user_id=? AND guild_id=? AND expires_at > ?",
        (user_id, guild_id, int(time.time())),
    )
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# Order idempotency
#
# A browser Buy button is the easiest thing in the world to double-click, and a
# flaky mobile connection retries POSTs on its own. Discord never had this
# problem because an interaction token can only be used once. Here the site
# sends an idempotency key with every order; if we have already answered that
# key, we replay the stored answer instead of trading again.
# --------------------------------------------------------------------------- #
async def remembered_order(db, idem_key: str):
    """Returns (status, response_json) for an order already processed, else None."""
    if not idem_key:
        return None
    row = await db._fetchone(
        "SELECT status, response FROM web_orders WHERE idem_key=?", (idem_key,)
    )
    if not row:
        return None
    return int(row[0]), row[1]


async def remember_order(db, idem_key: str, user_id: int, guild_id: int, status: int, response: str):
    if not idem_key:
        return
    await db._exec(
        "INSERT OR IGNORE INTO web_orders (idem_key, user_id, guild_id, created_at, status, response) "
        "VALUES (?,?,?,?,?,?)",
        (idem_key, user_id, guild_id, int(time.time()), status, response),
    )


async def prune_orders(db):
    await db._exec(
        "DELETE FROM web_orders WHERE created_at < ?",
        (int(time.time()) - ORDER_MEMORY_SECONDS,),
    )


# --------------------------------------------------------------------------- #
# Price history for the charts
# --------------------------------------------------------------------------- #
async def record_samples(db, guild_id: int, rows):
    """rows: iterable of (business_id, price). One timestamp for the batch, so
    every line on a multi-stock chart shares the same x-axis points."""
    now = int(time.time())
    payload = [(int(biz_id), int(guild_id), now, float(price)) for biz_id, price in rows]
    if not payload:
        return 0
    await db._conn.executemany(
        "INSERT INTO web_price_samples (business_id, guild_id, ts, price) VALUES (?,?,?,?)",
        payload,
    )
    await db._conn.commit()
    return len(payload)


async def price_history(db, business_id: int, since_ts: int, limit: int = 2000):
    """Oldest-first samples for one listing, ready to plot."""
    return await db._fetchall(
        "SELECT ts, price FROM web_price_samples WHERE business_id=? AND ts >= ? "
        "ORDER BY ts ASC LIMIT ?",
        (business_id, since_ts, limit),
    )


async def prune_samples(db):
    """Drops samples older than WEB_PRICE_HISTORY_DAYS."""
    days = int(getattr(config, "WEB_PRICE_HISTORY_DAYS", 14))
    cutoff = int(time.time()) - days * 86400
    await db._exec("DELETE FROM web_price_samples WHERE ts < ?", (cutoff,))
