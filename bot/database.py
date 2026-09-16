"""
Async SQLite database layer for the BeamNG RP Economy Bot.
All access goes through the Database class so cogs never touch SQL directly
outside of this module (keeps queries auditable and easy to change later).

Money-moving helpers (try_spend_cash, move_funds, transfer_cash) perform the
balance check and the update in a single statement / transaction, so two
commands racing each other can never overdraw an account.
"""

import os
import time
import shutil
import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from typing import Optional, List, Tuple

# Where the SQLite file lives. Free hosts wipe the app folder on every redeploy,
# so DATABASE_PATH lets you point the database at a mounted persistent disk
# (for example /data/economy.db) without editing any code.
DB_PATH = os.getenv("DATABASE_PATH", "economy.db")

# Whitelist of user balance columns that move_funds may operate on.
BALANCE_COLUMNS = ("cash", "bank", "savings")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    cash        INTEGER NOT NULL DEFAULT 0,
    bank        INTEGER NOT NULL DEFAULT 0,
    savings     INTEGER NOT NULL DEFAULT 0,
    credit_score INTEGER NOT NULL DEFAULT 650,
    job         TEXT,
    clocked_in_at INTEGER,
    last_daily  INTEGER DEFAULT 0,
    last_work   INTEGER DEFAULT 0,
    last_stock_sell INTEGER NOT NULL DEFAULT 0,
    last_stock_buy INTEGER NOT NULL DEFAULT 0,
    daily_streak INTEGER NOT NULL DEFAULT 0,
    wanted_level INTEGER NOT NULL DEFAULT 0,
    -- When the account row was first created (0 for accounts that predate
    -- this column). The reconciliation audit uses it to explain the starting
    -- balance minted for each new account, and to spot brand-new payers.
    created_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);

-- Cooldowns keyed on something other than the user alone (e.g. one player's
-- /paycompany cooldown PER business). Claimed with the same atomic
-- conditional write as users.last_work / last_stock_sell.
CREATE TABLE IF NOT EXISTS keyed_cooldowns (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    key         TEXT NOT NULL,
    last_ts     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, key)
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT,
    purchase_price INTEGER NOT NULL,
    condition   INTEGER NOT NULL DEFAULT 100,
    insured     INTEGER NOT NULL DEFAULT 0,
    plate       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    reason      TEXT,
    issued_by   INTEGER,
    paid        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    principal   INTEGER NOT NULL,
    remaining   INTEGER NOT NULL,
    interest_rate REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER,
    -- How long the borrower asked for, the deadline that came with it, and
    -- when it was actually settled (so "paid late" is a fact, not a guess).
    term_days   INTEGER,
    due_at      INTEGER,
    closed_at   INTEGER
);

CREATE TABLE IF NOT EXISTS loan_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    requested_at INTEGER,
    decided_by  INTEGER,
    decided_at  INTEGER,
    note        TEXT,
    -- The term the borrower asked for and the interest that term carries.
    term_days   INTEGER,
    interest_rate REAL,
    -- Where the request was posted for staff, so the Approve/Deny buttons on
    -- that message still work after the bot restarts.
    channel_id  INTEGER,
    message_id  INTEGER
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    type        TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    description TEXT,
    timestamp   INTEGER
);

CREATE TABLE IF NOT EXISTS treasury (
    guild_id    INTEGER PRIMARY KEY,
    balance     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS businesses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    name            TEXT NOT NULL,
    owner_id        INTEGER,
    stock_price     REAL NOT NULL,
    prev_price      REAL NOT NULL,
    total_shares    INTEGER NOT NULL,
    available_shares INTEGER NOT NULL,
    created_at      INTEGER,
    delisted        INTEGER NOT NULL DEFAULT 0,
    -- Optional link to a real registered business (companies.id). When set (or
    -- when the names match), the market tick prices this listing off that
    -- business's revenue instead of drifting randomly.
    company_id      INTEGER,
    -- The linked business's all-time revenue as of the last market tick, so a
    -- tick can tell how much was earned during the period just ended.
    -- NULL = never priced off revenue yet (the next tick sets it and skips).
    revenue_baseline INTEGER,
    -- Bumped on every price write. Price updates carry the version they were
    -- computed from and refuse to apply if it has moved on (optimistic lock),
    -- so a trade and a tick can never silently overwrite each other.
    price_version   INTEGER NOT NULL DEFAULT 0,
    -- The price at the start of the current tick window. The circuit breaker
    -- measures how far the price has travelled from here; the market tick
    -- resets it. NULL = not opened yet (treated as the current price).
    window_open_price REAL,
    -- Fractional instant revenue-driven move already spent in this window
    -- (0.03 = 3%). Capped at config.STOCK_REVENUE_INSTANT_PERIOD_MAX; the tick
    -- resets it to 0.
    window_revenue_move REAL NOT NULL DEFAULT 0,
    -- 1 while the circuit breaker has trading halted for the rest of the
    -- window. The tick clears it.
    halted          INTEGER NOT NULL DEFAULT 0,
    -- When a revenue payment last moved this listing's price. Insiders may
    -- not sell for STOCK_INSIDER_SELL_LOCKOUT_MINUTES after it.
    last_revenue_event INTEGER NOT NULL DEFAULT 0
);

-- Every filled /stock buy or sell order. Drives the rolling buy-volume cap and
-- the reconciliation audit's pattern checks (the transactions table only has
-- a free-text description, which is not something to parse numbers out of).
CREATE TABLE IF NOT EXISTS stock_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    business_id INTEGER NOT NULL,
    side        TEXT NOT NULL,            -- 'buy' or 'sell'
    shares      INTEGER NOT NULL,
    amount      INTEGER NOT NULL,         -- cash paid (buy) or received (sell)
    price       REAL NOT NULL,            -- pre-trade price
    at_cap      INTEGER NOT NULL DEFAULT 0, -- 1 if the order was the max allowed size
    timestamp   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_trades_user
    ON stock_trades (guild_id, user_id, business_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_stock_trades_biz
    ON stock_trades (guild_id, business_id, timestamp);

CREATE TABLE IF NOT EXISTS stock_holdings (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    business_id INTEGER NOT NULL,
    shares      INTEGER NOT NULL DEFAULT 0,
    cost_basis  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, business_id)
);

CREATE TABLE IF NOT EXISTS stock_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    business_id     INTEGER NOT NULL,
    change_percent  REAL NOT NULL,
    reason          TEXT,
    actor_id        INTEGER,
    timestamp       INTEGER
);

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    owner_id      INTEGER,
    balance       INTEGER NOT NULL DEFAULT 0,
    revenue_total INTEGER NOT NULL DEFAULT 0,
    tax_paid      INTEGER NOT NULL DEFAULT 0,
    deposits_total INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER
);

-- Employees of a real company. The owner is NOT stored here; they live in
-- companies.owner_id, so a business always has exactly one owner and staff
-- rows never contradict it.
CREATE TABLE IF NOT EXISTS company_staff (
    company_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    position    TEXT NOT NULL,
    hired_at    INTEGER,
    hired_by    INTEGER,
    PRIMARY KEY (company_id, user_id)
);

CREATE TABLE IF NOT EXISTS company_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    description TEXT,
    actor_id    INTEGER,
    timestamp   INTEGER
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id            INTEGER PRIMARY KEY,
    market_channel_id   INTEGER,
    log_channel_id      INTEGER,
    -- Comma-separated channel IDs the stock commands and the public business
    -- information commands are restricted to. Empty/NULL = unrestricted.
    stock_channel_ids     TEXT,
    business_channel_ids  TEXT,
    -- Comma-separated role IDs allowed to use the bot at all.
    -- Empty/NULL = fall back to config.WHITELIST_ROLE_IDS.
    whitelist_role_ids    TEXT
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        # Second connection used ONLY for explicit multi-statement transactions
        # (see transaction() below), plus the lock that serialises them.
        self._tx_conn: Optional[aiosqlite.Connection] = None
        self._tx_lock = asyncio.Lock()

    async def connect(self):
        # Create the folder first: on hosts with a mounted disk the path is
        # something like /data/economy.db and /data may be empty.
        parent = Path(self.path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.path)
        # WAL survives an abrupt kill far better than the default journal, and
        # free hosts restart containers without warning. busy_timeout stops a
        # momentarily locked database from raising instead of waiting.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # THE UPDATE-SAFETY PRAGMA.
        #
        # In WAL mode a committed write lands in economy.db-wal first and only
        # moves into economy.db at a checkpoint. SQLite's default is to wait
        # for 1000 pages (~4 MB) of WAL — which on a server this size can be
        # HOURS of play. Anyone who then copies economy.db out of the host's
        # file manager (exactly what a bot update does) gets a file that is
        # missing every write still sitting in the WAL: a few players' balances
        # and the newest stock trades silently revert.
        #
        # 64 pages (~256 KB) makes SQLite fold the WAL back into economy.db
        # constantly, so the main file is never more than a few seconds stale.
        # cogs/persistence.py additionally forces a full checkpoint on a timer.
        await self._conn.execute("PRAGMA wal_autocheckpoint=64")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate_legacy_columns()

        # isolation_level=None puts the driver in manual mode: it never opens a
        # transaction behind our back, so BEGIN/COMMIT/ROLLBACK below are the
        # only thing that starts or ends one on this connection.
        self._tx_conn = await aiosqlite.connect(self.path, isolation_level=None)
        await self._tx_conn.execute("PRAGMA busy_timeout=10000")
        await self._tx_conn.execute("PRAGMA foreign_keys=ON")
        await self._tx_conn.execute("PRAGMA synchronous=FULL")

    # -- explicit transactions ----------------------------------------------
    @asynccontextmanager
    async def transaction(self):
        """Runs a block of statements as ONE database transaction.

            async with db.transaction() as tx:
                if not await db.try_spend_cash(uid, gid, cost, tx=tx):
                    raise SomeError(...)      # -> ROLLBACK, nothing charged
                await db.upsert_holding(..., tx=tx)
            # -> COMMIT: either every write landed or none did

        Every money/stock helper that takes a `tx` argument runs on the
        transaction when given one, and behaves exactly as before (single
        auto-committed statement) when not.

        Why a separate connection: the bot shares one aiosqlite connection for
        everything, and each helper calls commit(). A BEGIN on that connection
        would be committed by the next unrelated /work or /daily that finished
        while our trade was still half done — and a ROLLBACK would then undo
        THEIR payout too. Transactions therefore run on a dedicated connection
        that nothing else touches; SQLite's WAL mode lets the two coexist, and
        the main connection simply waits (busy_timeout) for the few
        milliseconds a trade holds the write lock.

        One transaction at a time (self._tx_lock): they are short, and
        serialising them means two transactions can never deadlock on the
        database's own write lock.
        """
        async with self._tx_lock:
            conn = self._tx_conn
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    await conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            else:
                await conn.execute("COMMIT")

    async def _exec(self, sql: str, params=(), tx=None):
        """Runs one statement. On the shared connection it auto-commits (the
        existing behaviour of every helper); inside a transaction() block it
        runs on the transaction and leaves the commit to the block."""
        if tx is not None:
            return await tx.execute(sql, params)
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur

    async def _fetchone(self, sql: str, params=(), tx=None):
        conn = tx if tx is not None else self._conn
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    async def _fetchall(self, sql: str, params=(), tx=None):
        conn = tx if tx is not None else self._conn
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

    async def _migrate_legacy_columns(self):
        """Adds columns introduced after a database already existed.
        CREATE TABLE IF NOT EXISTS only helps brand-new databases, so any
        bot instance upgraded from an earlier version needs these ALTERs."""
        for ddl in (
            "ALTER TABLE users ADD COLUMN savings INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN credit_score INTEGER NOT NULL DEFAULT 650",
            "ALTER TABLE users ADD COLUMN daily_streak INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN last_stock_sell INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE guild_settings ADD COLUMN log_channel_id INTEGER",
            "ALTER TABLE companies ADD COLUMN deposits_total INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE guild_settings ADD COLUMN stock_channel_ids TEXT",
            "ALTER TABLE guild_settings ADD COLUMN business_channel_ids TEXT",
            "ALTER TABLE guild_settings ADD COLUMN whitelist_role_ids TEXT",
            "ALTER TABLE businesses ADD COLUMN company_id INTEGER",
            "ALTER TABLE businesses ADD COLUMN revenue_baseline INTEGER",
            "ALTER TABLE loans ADD COLUMN term_days INTEGER",
            "ALTER TABLE loans ADD COLUMN due_at INTEGER",
            "ALTER TABLE loans ADD COLUMN closed_at INTEGER",
            "ALTER TABLE loan_requests ADD COLUMN term_days INTEGER",
            "ALTER TABLE loan_requests ADD COLUMN interest_rate REAL",
            "ALTER TABLE loan_requests ADD COLUMN channel_id INTEGER",
            "ALTER TABLE loan_requests ADD COLUMN message_id INTEGER",
            # Anti-abuse / race-safety columns (see the schema comments).
            "ALTER TABLE users ADD COLUMN last_stock_buy INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE businesses ADD COLUMN price_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE businesses ADD COLUMN window_open_price REAL",
            "ALTER TABLE businesses ADD COLUMN window_revenue_move REAL NOT NULL DEFAULT 0",
            "ALTER TABLE businesses ADD COLUMN halted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE businesses ADD COLUMN last_revenue_event INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await self._conn.execute(ddl)
                await self._conn.commit()
            except aiosqlite.OperationalError:
                pass  # column already exists

        # Listings that predate the circuit breaker open their first window at
        # the price they have now; otherwise the breaker would have nothing to
        # measure from until the next tick.
        await self._conn.execute(
            "UPDATE businesses SET window_open_price=stock_price WHERE window_open_price IS NULL"
        )
        await self._conn.commit()

    async def close(self):
        if self._tx_conn:
            try:
                await self._tx_conn.close()
            except Exception:
                pass
            self._tx_conn = None
        if self._conn:
            try:
                # Fold the write-ahead log back into the main file so a host
                # that only keeps economy.db still has every transaction.
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self._conn.close()
            self._conn = None

    async def checkpoint(self) -> int:
        """Flushes the write-ahead log into the main database file.

        Returns how many bytes of WAL were folded in, so the caller can tell
        whether economy.db was actually out of date.

        TRUNCATE checkpoints require nobody anywhere have an open read on the
        database, including another statement mid-flight on this very
        connection (a cog that's still iterating a query). That collision is
        routine — this fires every 30s, and other cogs run their own timers on
        the same schedule — so it's treated as ordinary contention, not an
        error: retry briefly, and if something is still reading, fall back to
        a PASSIVE checkpoint, which folds in everything it safely can without
        requiring exclusive access and never raises for that reason.
        """
        if not self._conn:
            return 0
        before = self.wal_size()
        for attempt in range(3):
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                break
            except aiosqlite.OperationalError as exc:
                if "lock" not in str(exc).lower():
                    raise
                if attempt == 2:
                    await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                else:
                    await asyncio.sleep(0.2)
        return max(0, before - self.wal_size())

    def wal_size(self) -> int:
        """Bytes currently sitting in economy.db-wal, i.e. committed writes
        that are NOT yet inside economy.db itself. Anything above ~0 means a
        plain copy of economy.db would lose data."""
        try:
            return os.path.getsize(self.path + "-wal")
        except OSError:
            return 0

    # -- economy fingerprint (update safety) --------------------------------
    async def economy_fingerprint(self) -> dict:
        """A small set of whole-economy totals used to prove that a database
        survived a bot update intact.

        Taken before an update and compared after it, any number that went
        DOWN means data was lost in the copy — which is precisely the failure
        that used to eat stock portfolios and the odd player's balance.
        """
        await self.checkpoint()
        queries = {
            "players": "SELECT COUNT(*) FROM users",
            "money_supply": "SELECT COALESCE(SUM(cash+bank+savings),0) FROM users",
            "cash": "SELECT COALESCE(SUM(cash),0) FROM users",
            "bank": "SELECT COALESCE(SUM(bank),0) FROM users",
            "savings": "SELECT COALESCE(SUM(savings),0) FROM users",
            "shareholders": "SELECT COUNT(*) FROM stock_holdings WHERE shares > 0",
            "shares_held": "SELECT COALESCE(SUM(shares),0) FROM stock_holdings",
            "portfolio_cost": "SELECT COALESCE(SUM(cost_basis),0) FROM stock_holdings",
            "listings": "SELECT COUNT(*) FROM businesses WHERE delisted=0",
            "businesses": "SELECT COUNT(*) FROM companies",
            "business_funds": "SELECT COALESCE(SUM(balance),0) FROM companies",
            "treasury": "SELECT COALESCE(SUM(balance),0) FROM treasury",
            "vehicles": "SELECT COUNT(*) FROM vehicles",
            "active_loans": "SELECT COUNT(*) FROM loans WHERE active=1",
            "transactions": "SELECT COUNT(*) FROM transactions",
            "stock_trades": "SELECT COUNT(*) FROM stock_trades",
            "last_transaction_id": "SELECT COALESCE(MAX(id),0) FROM transactions",
            "last_trade_id": "SELECT COALESCE(MAX(id),0) FROM stock_trades",
        }
        out = {}
        for key, sql in queries.items():
            row = await self._fetchone(sql)
            out[key] = int(row[0] if row else 0)
        return out

    async def snapshot(self, destination: Optional[str] = None) -> str:
        """Writes a consistent copy of the database to `destination` and returns
        the path. Safe to call while the bot is running."""
        if destination is None:
            destination = os.path.join(tempfile.gettempdir(), "economy-backup.db")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        await self.checkpoint()
        await self._conn.commit()
        # VACUUM INTO makes a compact, crash-consistent copy (SQLite 3.27+).
        if os.path.exists(destination):
            os.remove(destination)
        try:
            await self._conn.execute("VACUUM INTO ?", (destination,))
        except aiosqlite.OperationalError:
            shutil.copy2(self.path, destination)
        return destination

    # -- internal helpers ---------------------------------------------------
    async def _ensure_user(self, user_id: int, guild_id: int, tx=None):
        row = await self._fetchone(
            "SELECT 1 FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id), tx=tx
        )
        if not row:
            from config import STARTING_CASH, STARTING_BANK
            # OR IGNORE: two commands firing at once can both find no row and
            # both try to insert it, which otherwise crashes the second one.
            await self._exec(
                "INSERT OR IGNORE INTO users (user_id, guild_id, cash, bank, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, guild_id, STARTING_CASH, STARTING_BANK, int(time.time())),
                tx=tx,
            )

    async def _ensure_treasury(self, guild_id: int):
        cur = await self._conn.execute("SELECT 1 FROM treasury WHERE guild_id=?", (guild_id,))
        if not await cur.fetchone():
            await self._conn.execute("INSERT INTO treasury (guild_id, balance) VALUES (?, 0)", (guild_id,))
            await self._conn.commit()

    async def log_transaction(
        self, user_id: int, guild_id: int, ttype: str, amount: int, description: str = "", tx=None
    ):
        await self._exec(
            "INSERT INTO transactions (user_id, guild_id, type, amount, description, timestamp) VALUES (?,?,?,?,?,?)",
            (user_id, guild_id, ttype, amount, description, int(time.time())),
            tx=tx,
        )

    # -- atomic money movement --------------------------------------------
    async def try_spend_cash(self, user_id: int, guild_id: int, amount: int, tx=None) -> bool:
        """Deducts `amount` cash only if the user can afford it. Returns False otherwise.
        The check and the update happen in one statement, so it is race-safe."""
        if amount <= 0:
            return False
        await self._ensure_user(user_id, guild_id, tx=tx)
        cur = await self._exec(
            "UPDATE users SET cash = cash - ? WHERE user_id=? AND guild_id=? AND cash >= ?",
            (amount, user_id, guild_id, amount),
            tx=tx,
        )
        return cur.rowcount > 0

    async def try_spend_from(self, user_id: int, guild_id: int, column: str, amount: int) -> bool:
        """Deducts `amount` from one named balance (cash/bank/savings) only if it
        covers it. Single statement, so it is race-safe the same way
        try_spend_cash is."""
        if column not in BALANCE_COLUMNS:
            raise ValueError(f"Invalid balance column: {column}")
        if amount <= 0:
            return False
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            f"UPDATE users SET {column} = {column} - ? "
            f"WHERE user_id=? AND guild_id=? AND {column} >= ?",
            (amount, user_id, guild_id, amount),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def move_funds(self, user_id: int, guild_id: int, from_col: str, to_col: str, amount: int) -> bool:
        """Moves `amount` between two of the user's own balances (cash/bank/savings).
        Returns False if the source balance is insufficient."""
        if from_col not in BALANCE_COLUMNS or to_col not in BALANCE_COLUMNS or from_col == to_col:
            raise ValueError(f"Invalid balance columns: {from_col} -> {to_col}")
        if amount <= 0:
            return False
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            f"UPDATE users SET {from_col} = {from_col} - ?, {to_col} = {to_col} + ? "
            f"WHERE user_id=? AND guild_id=? AND {from_col} >= ?",
            (amount, amount, user_id, guild_id, amount),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def transfer_cash(self, from_id: int, to_id: int, guild_id: int, amount: int) -> bool:
        """Moves cash from one player to another in a single transaction.
        Returns False (and changes nothing) if the sender cannot afford it."""
        if amount <= 0 or from_id == to_id:
            return False
        await self._ensure_user(from_id, guild_id)
        await self._ensure_user(to_id, guild_id)
        try:
            cur = await self._conn.execute(
                "UPDATE users SET cash = cash - ? WHERE user_id=? AND guild_id=? AND cash >= ?",
                (amount, from_id, guild_id, amount),
            )
            if cur.rowcount == 0:
                await self._conn.rollback()
                return False
            await self._conn.execute(
                "UPDATE users SET cash = cash + ? WHERE user_id=? AND guild_id=?",
                (amount, to_id, guild_id),
            )
            await self._conn.commit()
            return True
        except Exception:
            await self._conn.rollback()
            raise

    async def pay_savings_interest(self, guild_id: int, rate: float) -> Tuple[int, int]:
        """Credits interest to every savings holder in a guild and logs each payout.
        Returns (accounts_paid, total_paid)."""
        holders = await self.get_savings_holders(guild_id)
        paid, total = 0, 0
        now = int(time.time())
        for user_id, savings in holders:
            interest = int(savings * rate)
            if interest <= 0:
                continue
            await self._conn.execute(
                "UPDATE users SET savings = savings + ? WHERE user_id=? AND guild_id=?",
                (interest, user_id, guild_id),
            )
            await self._conn.execute(
                "INSERT INTO transactions (user_id, guild_id, type, amount, description, timestamp) VALUES (?,?,?,?,?,?)",
                (user_id, guild_id, "savings_interest", interest, f"{rate * 100:.2f}% savings interest", now),
            )
            paid += 1
            total += interest
        await self._conn.commit()
        return paid, total

    # -- bot metadata (key/value) -------------------------------------------
    async def get_meta(self, key: str, default=None):
        cur = await self._conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_meta(self, key: str, value):
        await self._conn.execute(
            "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self._conn.commit()

    # -- balances -------------------------------------------------------
    async def get_balance(self, user_id: int, guild_id: int) -> Tuple[int, int]:
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT cash, bank FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        )
        row = await cur.fetchone()
        return row[0], row[1]

    async def get_full_balance(self, user_id: int, guild_id: int):
        """Returns (cash, bank, savings, credit_score) for the financial-profile embed."""
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT cash, bank, savings, credit_score FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        return row

    async def add_savings(self, user_id: int, guild_id: int, amount: int):
        await self._ensure_user(user_id, guild_id)
        await self._conn.execute(
            "UPDATE users SET savings = savings + ? WHERE user_id=? AND guild_id=?", (amount, user_id, guild_id)
        )
        await self._conn.commit()

    async def get_savings(self, user_id: int, guild_id: int) -> int:
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT savings FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def get_savings_holders(self, guild_id: int):
        """Returns (user_id, savings) for every user in a guild with a positive savings balance."""
        cur = await self._conn.execute(
            "SELECT user_id, savings FROM users WHERE guild_id=? AND savings > 0", (guild_id,)
        )
        return await cur.fetchall()

    async def get_all_guild_ids(self):
        cur = await self._conn.execute("SELECT DISTINCT guild_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_credit_score(self, user_id: int, guild_id: int) -> int:
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT credit_score FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def adjust_credit_score(self, user_id: int, guild_id: int, delta: int, min_score: int, max_score: int) -> int:
        await self._ensure_user(user_id, guild_id)
        current = await self.get_credit_score(user_id, guild_id)
        new_score = max(min_score, min(max_score, current + delta))
        await self._conn.execute(
            "UPDATE users SET credit_score=? WHERE user_id=? AND guild_id=?", (new_score, user_id, guild_id)
        )
        await self._conn.commit()
        return new_score

    async def add_cash(self, user_id: int, guild_id: int, amount: int, tx=None):
        await self._ensure_user(user_id, guild_id, tx=tx)
        await self._exec(
            "UPDATE users SET cash = cash + ? WHERE user_id=? AND guild_id=?", (amount, user_id, guild_id),
            tx=tx,
        )

    async def add_bank(self, user_id: int, guild_id: int, amount: int):
        await self._ensure_user(user_id, guild_id)
        await self._conn.execute(
            "UPDATE users SET bank = bank + ? WHERE user_id=? AND guild_id=?", (amount, user_id, guild_id)
        )
        await self._conn.commit()

    async def set_balance(self, user_id: int, guild_id: int, cash: int, bank: int):
        await self._ensure_user(user_id, guild_id)
        await self._conn.execute(
            "UPDATE users SET cash=?, bank=? WHERE user_id=? AND guild_id=?",
            (cash, bank, user_id, guild_id),
        )
        await self._conn.commit()

    async def leaderboard(self, guild_id: int, limit: int = 10) -> List[Tuple[int, int, int, int]]:
        """Returns (user_id, cash, bank, savings) rows ranked by liquid net worth."""
        cur = await self._conn.execute(
            "SELECT user_id, cash, bank, savings FROM users WHERE guild_id=? "
            "ORDER BY (cash+bank+savings) DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()

    async def get_transactions(self, user_id: int, guild_id: int, limit: int = 10):
        cur = await self._conn.execute(
            "SELECT type, amount, description, timestamp FROM transactions "
            "WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT ?",
            (user_id, guild_id, limit),
        )
        return await cur.fetchall()

    # -- cooldowns / daily / work ----------------------------------------
    async def get_cooldowns(self, user_id: int, guild_id: int):
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT last_daily, last_work FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        return await cur.fetchone()

    async def set_last_daily(self, user_id: int, guild_id: int, ts: int):
        await self._conn.execute(
            "UPDATE users SET last_daily=? WHERE user_id=? AND guild_id=?", (ts, user_id, guild_id)
        )
        await self._conn.commit()

    async def get_daily_state(self, user_id: int, guild_id: int) -> Tuple[int, int]:
        """Returns (last_daily_timestamp, current_streak)."""
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT last_daily, daily_streak FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        return (row[0] or 0, row[1] or 0)

    async def set_daily_claimed(self, user_id: int, guild_id: int, ts: int, streak: int):
        await self._conn.execute(
            "UPDATE users SET last_daily=?, daily_streak=? WHERE user_id=? AND guild_id=?",
            (ts, streak, user_id, guild_id),
        )
        await self._conn.commit()

    async def set_last_work(self, user_id: int, guild_id: int, ts: int):
        await self._conn.execute(
            "UPDATE users SET last_work=? WHERE user_id=? AND guild_id=?", (ts, user_id, guild_id)
        )
        await self._conn.commit()

    async def try_consume_work_cooldown(self, user_id: int, guild_id: int, now: int, cooldown_secs: int) -> bool:
        """Claims the /work cooldown atomically: stamps last_work only if the
        cooldown has actually elapsed. Returns False if it hasn't. Reading the
        cooldown and writing it back separately let simultaneous /work commands
        all see the same stale timestamp and all pay out."""
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "UPDATE users SET last_work=? WHERE user_id=? AND guild_id=? "
            "AND ? - COALESCE(last_work, 0) >= ?",
            (now, user_id, guild_id, now, cooldown_secs),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def try_consume_daily(
        self, user_id: int, guild_id: int, now: int, cooldown_secs: int, grace_secs: int
    ) -> Optional[int]:
        """Claims the /daily reward atomically. Stamps last_daily and advances
        (or resets) the streak in ONE statement, so simultaneous /daily commands
        can't all read the same stale timestamp and all get paid. Returns the new
        streak on success, or None if the cooldown hasn't elapsed yet."""
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "UPDATE users SET last_daily=?, "
            "daily_streak = CASE WHEN ? - COALESCE(last_daily, 0) <= ? "
            "THEN COALESCE(daily_streak, 0) + 1 ELSE 1 END "
            "WHERE user_id=? AND guild_id=? AND ? - COALESCE(last_daily, 0) >= ?",
            (now, now, grace_secs, user_id, guild_id, now, cooldown_secs),
        )
        await self._conn.commit()
        if cur.rowcount <= 0:
            return None
        cur = await self._conn.execute(
            "SELECT daily_streak FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        return (row[0] if row else 1) or 1

    # -- stock sell cooldown ----------------------------------------------
    async def get_last_stock_sell(self, user_id: int, guild_id: int, tx=None) -> int:
        await self._ensure_user(user_id, guild_id, tx=tx)
        row = await self._fetchone(
            "SELECT last_stock_sell FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id), tx=tx,
        )
        return (row[0] if row else 0) or 0

    async def try_consume_stock_sell_cooldown(
        self, user_id: int, guild_id: int, now: int, cooldown_secs: int, tx=None
    ) -> bool:
        """Claims the /stock sell cooldown atomically, same pattern as /work:
        the elapsed-time check and the write are a single statement, so burst
        sells can't all slip past the cooldown together."""
        await self._ensure_user(user_id, guild_id, tx=tx)
        if cooldown_secs <= 0:
            return True
        cur = await self._exec(
            "UPDATE users SET last_stock_sell=? WHERE user_id=? AND guild_id=? "
            "AND ? - COALESCE(last_stock_sell, 0) >= ?",
            (now, user_id, guild_id, now, cooldown_secs),
            tx=tx,
        )
        return cur.rowcount > 0

    # -- stock buy cooldown -----------------------------------------------
    async def get_last_stock_buy(self, user_id: int, guild_id: int, tx=None) -> int:
        await self._ensure_user(user_id, guild_id, tx=tx)
        row = await self._fetchone(
            "SELECT last_stock_buy FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id), tx=tx,
        )
        return (row[0] if row else 0) or 0

    async def try_consume_stock_buy_cooldown(
        self, user_id: int, guild_id: int, now: int, cooldown_secs: int, tx=None
    ) -> bool:
        """The /stock buy twin of try_consume_stock_sell_cooldown."""
        await self._ensure_user(user_id, guild_id, tx=tx)
        if cooldown_secs <= 0:
            return True
        cur = await self._exec(
            "UPDATE users SET last_stock_buy=? WHERE user_id=? AND guild_id=? "
            "AND ? - COALESCE(last_stock_buy, 0) >= ?",
            (now, user_id, guild_id, now, cooldown_secs),
            tx=tx,
        )
        return cur.rowcount > 0

    # -- keyed cooldowns (per user, per target) ----------------------------
    async def get_keyed_cooldown(self, user_id: int, guild_id: int, key: str) -> int:
        row = await self._fetchone(
            "SELECT last_ts FROM keyed_cooldowns WHERE user_id=? AND guild_id=? AND key=?",
            (user_id, guild_id, key),
        )
        return (row[0] if row else 0) or 0

    async def try_consume_keyed_cooldown(
        self, user_id: int, guild_id: int, key: str, now: int, cooldown_secs: int
    ) -> bool:
        """Claims a cooldown keyed on (user, something) — e.g. one player's
        /paycompany cooldown for ONE business — atomically. Same idea as the
        users.last_* columns: the elapsed-time check lives in the WHERE of the
        write, so simultaneous claims can't all succeed. A fresh row always
        wins (no previous claim); an existing row is only re-stamped if the
        cooldown has elapsed, and rowcount tells us which happened."""
        if cooldown_secs <= 0:
            return True
        cur = await self._exec(
            "INSERT INTO keyed_cooldowns (user_id, guild_id, key, last_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, guild_id, key) DO UPDATE SET last_ts=excluded.last_ts "
            "WHERE excluded.last_ts - keyed_cooldowns.last_ts >= ?",
            (user_id, guild_id, key, now, cooldown_secs),
        )
        return cur.rowcount > 0

    async def restore_keyed_cooldown(self, user_id: int, guild_id: int, key: str, previous: int):
        """Hands a claimed keyed cooldown back when the action didn't go through."""
        await self._exec(
            "UPDATE keyed_cooldowns SET last_ts=? WHERE user_id=? AND guild_id=? AND key=?",
            (previous, user_id, guild_id, key),
        )

    async def restore_stock_sell_cooldown(self, user_id: int, guild_id: int, previous: int):
        """Puts the previous timestamp back when a claimed sell doesn't go
        through, so a failed order doesn't cost the player 30 minutes."""
        await self._conn.execute(
            "UPDATE users SET last_stock_sell=? WHERE user_id=? AND guild_id=?",
            (previous, user_id, guild_id),
        )
        await self._conn.commit()

    # -- jobs -------------------------------------------------------------
    async def set_job(self, user_id: int, guild_id: int, job: Optional[str]):
        await self._ensure_user(user_id, guild_id)
        await self._conn.execute(
            "UPDATE users SET job=?, clocked_in_at=NULL WHERE user_id=? AND guild_id=?",
            (job, user_id, guild_id),
        )
        await self._conn.commit()

    async def get_job(self, user_id: int, guild_id: int):
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT job, clocked_in_at FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        return await cur.fetchone()

    # /clockin and /clockout were removed, along with their helpers. The
    # clocked_in_at column is kept (and always cleared by set_job) so existing
    # databases still open without a migration.

    # -- vehicles -----------------------------------------------------------
    async def add_vehicle(self, owner_id: int, guild_id: int, name: str, category: str, price: int, plate: str):
        await self._conn.execute(
            "INSERT INTO vehicles (owner_id, guild_id, name, category, purchase_price, condition, insured, plate) "
            "VALUES (?,?,?,?,?,100,0,?)",
            (owner_id, guild_id, name, category, price, plate),
        )
        await self._conn.commit()

    async def get_garage(self, owner_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, name, category, purchase_price, condition, insured, plate FROM vehicles "
            "WHERE owner_id=? AND guild_id=?",
            (owner_id, guild_id),
        )
        return await cur.fetchall()

    async def get_vehicle(self, vehicle_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, owner_id, name, category, purchase_price, condition, insured, plate FROM vehicles "
            "WHERE id=? AND guild_id=?",
            (vehicle_id, guild_id),
        )
        return await cur.fetchone()

    async def remove_vehicle(self, vehicle_id: int):
        await self._conn.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))
        await self._conn.commit()

    async def get_vehicle_value(self, owner_id: int, guild_id: int) -> int:
        """Condition-adjusted total value of everything in a player's garage."""
        cur = await self._conn.execute(
            "SELECT COALESCE(SUM(purchase_price * condition / 100), 0) FROM vehicles "
            "WHERE owner_id=? AND guild_id=?",
            (owner_id, guild_id),
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] else 0

    async def set_vehicle_condition(self, vehicle_id: int, condition: int):
        condition = max(0, min(100, condition))
        await self._conn.execute("UPDATE vehicles SET condition=? WHERE id=?", (condition, vehicle_id))
        await self._conn.commit()

    # -- fines --------------------------------------------------------------
    async def add_fine(self, user_id: int, guild_id: int, amount: int, reason: str, issued_by: int) -> int:
        cur = await self._conn.execute(
            "INSERT INTO fines (user_id, guild_id, amount, reason, issued_by, paid, created_at) "
            "VALUES (?,?,?,?,?,0,?)",
            (user_id, guild_id, amount, reason, issued_by, int(time.time())),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_unpaid_fines(self, user_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, amount, reason, issued_by FROM fines WHERE user_id=? AND guild_id=? AND paid=0",
            (user_id, guild_id),
        )
        return await cur.fetchall()

    async def get_fine(self, fine_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, user_id, amount, paid FROM fines WHERE id=? AND guild_id=?", (fine_id, guild_id)
        )
        return await cur.fetchone()

    async def mark_fine_paid(self, fine_id: int):
        await self._conn.execute("UPDATE fines SET paid=1 WHERE id=?", (fine_id,))
        await self._conn.commit()

    # -- wanted level ---------------------------------------------------------
    async def set_wanted(self, user_id: int, guild_id: int, level: int):
        await self._ensure_user(user_id, guild_id)
        level = max(0, min(5, level))
        await self._conn.execute(
            "UPDATE users SET wanted_level=? WHERE user_id=? AND guild_id=?", (level, user_id, guild_id)
        )
        await self._conn.commit()

    async def get_wanted(self, user_id: int, guild_id: int) -> int:
        await self._ensure_user(user_id, guild_id)
        cur = await self._conn.execute(
            "SELECT wanted_level FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    # -- loans ----------------------------------------------------------------
    LOAN_COLS = "id, principal, remaining, interest_rate, due_at, term_days"

    async def get_active_loan(self, user_id: int, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.LOAN_COLS} FROM loans WHERE user_id=? AND guild_id=? AND active=1",
            (user_id, guild_id),
        )
        return await cur.fetchone()

    async def create_loan(
        self, user_id: int, guild_id: int, principal: int, rate: float, term_days: int = None
    ) -> int:
        now = int(time.time())
        due_at = now + int(term_days) * 86400 if term_days else None
        cur = await self._conn.execute(
            "INSERT INTO loans (user_id, guild_id, principal, remaining, interest_rate, active, "
            "created_at, term_days, due_at) VALUES (?,?,?,?,?,1,?,?,?)",
            (user_id, guild_id, principal, int(principal * (1 + rate)), rate, now, term_days, due_at),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_overdue_loans(self, guild_id: int, now: int):
        """Active loans past their due date, worst overdue first."""
        cur = await self._conn.execute(
            "SELECT id, user_id, principal, remaining, due_at FROM loans "
            "WHERE guild_id=? AND active=1 AND due_at IS NOT NULL AND due_at < ? "
            "ORDER BY due_at",
            (guild_id, now),
        )
        return await cur.fetchall()

    async def get_loan_history(self, user_id: int, guild_id: int):
        """(settled loans, of which were settled late) — the borrower's track
        record, so staff reviewing a request can see whether they pay up."""
        cur = await self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN due_at IS NOT NULL AND closed_at IS NOT NULL "
            "AND closed_at > due_at THEN 1 ELSE 0 END), 0) "
            "FROM loans WHERE user_id=? AND guild_id=? AND active=0",
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        return (row[0] or 0, row[1] or 0) if row else (0, 0)

    async def pay_loan(self, loan_id: int, amount: int) -> int:
        """Reduces remaining balance, closes loan if paid off. Returns new remaining."""
        cur = await self._conn.execute("SELECT remaining FROM loans WHERE id=?", (loan_id,))
        row = await cur.fetchone()
        remaining = max(0, row[0] - amount)
        active = 0 if remaining == 0 else 1
        closed_at = None if active else int(time.time())
        await self._conn.execute(
            "UPDATE loans SET remaining=?, active=?, closed_at=COALESCE(?, closed_at) WHERE id=?",
            (remaining, active, closed_at, loan_id),
        )
        await self._conn.commit()
        return remaining

    # -- loan requests (staff approval queue) ----------------------------------
    async def create_loan_request(
        self, user_id: int, guild_id: int, amount: int,
        term_days: int = None, interest_rate: float = None,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO loan_requests (user_id, guild_id, amount, status, requested_at, "
            "term_days, interest_rate) VALUES (?,?,?,'pending',?,?,?)",
            (user_id, guild_id, amount, int(time.time()), term_days, interest_rate),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def set_loan_request_message(self, request_id: int, channel_id: int, message_id: int):
        """Remembers where a request was posted, so the Approve/Deny buttons on
        that message can find their request again after a restart."""
        await self._conn.execute(
            "UPDATE loan_requests SET channel_id=?, message_id=? WHERE id=?",
            (channel_id, message_id, request_id),
        )
        await self._conn.commit()

    async def get_loan_request_location(self, request_id: int):
        """(channel_id, message_id) of the posted request, or (None, None)."""
        cur = await self._conn.execute(
            "SELECT channel_id, message_id FROM loan_requests WHERE id=?", (request_id,)
        )
        row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def get_loan_request_by_message(self, message_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.LOAN_REQUEST_COLS} FROM loan_requests WHERE message_id=?",
            (message_id,),
        )
        return await cur.fetchone()

    async def get_pending_loan_request_for_user(self, user_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, amount FROM loan_requests WHERE user_id=? AND guild_id=? AND status='pending'",
            (user_id, guild_id),
        )
        return await cur.fetchone()

    LOAN_REQUEST_COLS = "id, user_id, amount, status, term_days, interest_rate, guild_id"

    async def get_loan_request(self, request_id: int, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.LOAN_REQUEST_COLS} FROM loan_requests WHERE id=? AND guild_id=?",
            (request_id, guild_id),
        )
        return await cur.fetchone()

    async def get_pending_loan_requests(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, user_id, amount, requested_at, term_days, interest_rate FROM loan_requests "
            "WHERE guild_id=? AND status='pending' ORDER BY requested_at",
            (guild_id,),
        )
        return await cur.fetchall()

    async def set_loan_request_status(self, request_id: int, status: str, decided_by: int, note: str = None):
        await self._conn.execute(
            "UPDATE loan_requests SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?",
            (status, decided_by, int(time.time()), note, request_id),
        )
        await self._conn.commit()

    # -- treasury ---------------------------------------------------------------
    async def add_treasury(self, guild_id: int, amount: int):
        await self._ensure_treasury(guild_id)
        await self._conn.execute(
            "UPDATE treasury SET balance = balance + ? WHERE guild_id=?", (amount, guild_id)
        )
        await self._conn.commit()

    async def get_treasury(self, guild_id: int) -> int:
        await self._ensure_treasury(guild_id)
        cur = await self._conn.execute("SELECT balance FROM treasury WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

    # -- stock market: businesses ------------------------------------------------
    async def create_business(
        self, guild_id: int, name: str, owner_id, price: float, total_shares: int
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO businesses (guild_id, name, owner_id, stock_price, prev_price, "
            "total_shares, available_shares, created_at, delisted, window_open_price) "
            "VALUES (?,?,?,?,?,?,?,?,0,?)",
            (guild_id, name, owner_id, price, price, total_shares, total_shares, int(time.time()), price),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_business_by_id(self, business_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted "
            "FROM businesses WHERE id=? AND guild_id=?",
            (business_id, guild_id),
        )
        return await cur.fetchone()

    async def get_business_by_name(self, name: str, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted "
            "FROM businesses WHERE guild_id=? AND LOWER(name)=LOWER(?) AND delisted=0",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def get_businesses_by_owner(self, owner_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted "
            "FROM businesses WHERE guild_id=? AND owner_id=? AND delisted=0",
            (guild_id, owner_id),
        )
        return await cur.fetchall()

    async def list_businesses(self, guild_id: int, include_delisted: bool = False):
        if include_delisted:
            cur = await self._conn.execute(
                "SELECT id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted "
                "FROM businesses WHERE guild_id=? ORDER BY name", (guild_id,)
            )
        else:
            cur = await self._conn.execute(
                "SELECT id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted "
                "FROM businesses WHERE guild_id=? AND delisted=0 ORDER BY name", (guild_id,)
            )
        return await cur.fetchall()

    # Everything a trade, tick or revenue event needs to know about a listing,
    # in one row. Callers MUST fetch this AFTER taking the business lock and
    # use the returned price_version when writing the price back.
    TRADE_STATE_COLS = (
        "id, name, owner_id, stock_price, prev_price, total_shares, available_shares, delisted, "
        "price_version, COALESCE(window_open_price, stock_price), COALESCE(window_revenue_move, 0), "
        "COALESCE(halted, 0), COALESCE(last_revenue_event, 0)"
    )

    async def get_business_trade_state(self, business_id: int, tx=None):
        """(id, name, owner_id, price, prev_price, total, available, delisted,
        price_version, window_open_price, window_revenue_move, halted,
        last_revenue_event) — read fresh, for use under the business lock."""
        return await self._fetchone(
            f"SELECT {self.TRADE_STATE_COLS} FROM businesses WHERE id=?", (business_id,), tx=tx
        )

    async def set_business_price(
        self, business_id: int, new_price: float, min_price: float,
        expected_version: Optional[int] = None, tx=None,
    ):
        """Writes a new price in ONE conditional statement and returns it — or
        None if the write was refused.

        The old read-then-write let a trade and a market tick (or a revenue
        event) that landed close together each compute a price from the same
        stale value and the second one silently overwrite the first. Now the
        UPDATE itself copies the current price into prev_price, and when the
        caller passes the price_version it read, the write only applies if
        that version is still current (optimistic lock). Every write bumps the
        version, so a stale caller gets None and must re-read and recompute.
        """
        new_price = max(min_price, round(new_price, 2))
        if expected_version is None:
            cur = await self._exec(
                "UPDATE businesses SET prev_price=stock_price, stock_price=?, "
                "price_version=price_version+1 WHERE id=?",
                (new_price, business_id), tx=tx,
            )
        else:
            cur = await self._exec(
                "UPDATE businesses SET prev_price=stock_price, stock_price=?, "
                "price_version=price_version+1 WHERE id=? AND price_version=?",
                (new_price, business_id, expected_version), tx=tx,
            )
        return new_price if cur.rowcount > 0 else None

    async def adjust_available_shares(self, business_id: int, delta: int, tx=None) -> bool:
        """Moves shares between the open market and players' hands. The
        floor/ceiling check is part of the statement, so concurrent buys can
        never push available_shares below zero (more shares in circulation
        than exist) and concurrent sells can never push it above
        total_shares. Returns False (and changes nothing) if it would."""
        cur = await self._exec(
            "UPDATE businesses SET available_shares = available_shares + ? "
            "WHERE id=? AND available_shares + (?) BETWEEN 0 AND total_shares",
            (delta, business_id, delta), tx=tx,
        )
        return cur.rowcount > 0

    # -- tick window bookkeeping (revenue budget, circuit breaker) ---------
    async def open_tick_window(self, business_id: int, tx=None):
        """Called by the market tick after it has written the tick price:
        the new price becomes the window's opening price, the instant-revenue
        budget refills and any circuit-breaker halt is lifted."""
        await self._exec(
            "UPDATE businesses SET window_open_price=stock_price, window_revenue_move=0, halted=0 "
            "WHERE id=?",
            (business_id,), tx=tx,
        )

    async def spend_window_revenue_move(
        self, business_id: int, move: float, period_max: float, now: int, tx=None
    ) -> bool:
        """Charges an instant revenue move against the listing's per-window
        budget and stamps last_revenue_event. Conditional, so two payments
        landing together can't both fit into the last sliver of budget.
        `period_max=None` means no budget is in force (still stamps)."""
        if period_max is None:
            cur = await self._exec(
                "UPDATE businesses SET window_revenue_move = COALESCE(window_revenue_move, 0) + ?, "
                "last_revenue_event=? WHERE id=?",
                (move, now, business_id), tx=tx,
            )
        else:
            cur = await self._exec(
                "UPDATE businesses SET window_revenue_move = COALESCE(window_revenue_move, 0) + ?, "
                "last_revenue_event=? "
                "WHERE id=? AND ABS(COALESCE(window_revenue_move, 0) + ?) <= ? + 1e-9",
                (move, now, business_id, move, period_max), tx=tx,
            )
        return cur.rowcount > 0

    async def set_business_halted(self, business_id: int, halted: bool, tx=None):
        await self._exec(
            "UPDATE businesses SET halted=? WHERE id=?", (1 if halted else 0, business_id), tx=tx
        )

    # -- who counts as an insider on a listing -----------------------------
    async def is_stock_insider(
        self, user_id: int, guild_id: int, business_id: int, holder_threshold: float,
        withdraw_positions, tx=None,
    ) -> bool:
        """True if `user_id` is the listing's owner, owns the registered
        business behind it, holds a withdraw-capable position there, or holds
        at least `holder_threshold` of the total shares."""
        row = await self._fetchone(
            "SELECT owner_id, total_shares FROM businesses WHERE id=? AND guild_id=?",
            (business_id, guild_id), tx=tx,
        )
        if not row:
            return False
        owner_id, total_shares = row
        if owner_id == user_id:
            return True

        holding = await self._fetchone(
            "SELECT shares FROM stock_holdings WHERE user_id=? AND guild_id=? AND business_id=?",
            (user_id, guild_id, business_id), tx=tx,
        )
        if holding and total_shares and holding[0] >= holder_threshold * total_shares:
            return True

        company = await self._fetchone(
            "SELECT c.id, c.owner_id "
            f"FROM businesses b {self._REVENUE_LINK_JOIN} "
            "WHERE b.guild_id=? AND b.id=?",
            (guild_id, business_id), tx=tx,
        )
        if not company:
            return False
        company_id, company_owner = company
        if company_owner == user_id:
            return True
        position = await self._fetchone(
            "SELECT position FROM company_staff WHERE company_id=? AND user_id=?",
            (company_id, user_id), tx=tx,
        )
        if position and any(p.lower() == position[0].lower() for p in withdraw_positions):
            return True
        return False

    async def set_business_owner(self, business_id: int, owner_id):
        await self._conn.execute("UPDATE businesses SET owner_id=? WHERE id=?", (owner_id, business_id))
        await self._conn.commit()

    async def delist_business(self, business_id: int, tx=None):
        await self._exec("UPDATE businesses SET delisted=1 WHERE id=?", (business_id,), tx=tx)

    # -- stock market: link to a real business, for revenue-driven pricing -----
    # businesses.company_id has three meanings:
    #   a real id  -> explicitly linked to that registered business
    #   NULL       -> automatic: linked to a business of the same name, if one exists
    #   NO_LINK    -> explicitly unlinked; never auto-match, price on drift alone
    NO_LINK = -1

    async def set_business_company_link(self, business_id: int, company_id) -> None:
        """Ties a listing to a registered business. Pass None to go back to
        automatic name matching, or Database.NO_LINK to switch revenue pricing
        off for this listing entirely.

        Resets the revenue baseline so the next tick starts measuring fresh
        rather than treating the new company's whole history as one period."""
        await self._conn.execute(
            "UPDATE businesses SET company_id=?, revenue_baseline=NULL WHERE id=?",
            (company_id, business_id),
        )
        await self._conn.commit()

    async def get_business_company_link(self, business_id: int):
        """(company_id, company_name) for an explicitly linked listing, else None."""
        cur = await self._conn.execute(
            "SELECT c.id, c.name FROM businesses b JOIN companies c ON c.id = b.company_id "
            "WHERE b.id=?",
            (business_id,),
        )
        return await cur.fetchone()

    # A listing is matched to a real business either by an explicit link
    # (businesses.company_id, set with /eco-admin business link) or, failing
    # that, by an exact case-insensitive name match in the same guild.
    _REVENUE_LINK_JOIN = (
        "JOIN companies c ON c.guild_id = b.guild_id AND ("
        "  b.company_id = c.id"
        "  OR (b.company_id IS NULL AND LOWER(c.name) = LOWER(b.name))"
        ")"
    )

    async def get_revenue_links(self, guild_id: int):
        """Every listed business in the guild that is tied to a real business.

        Returns (business_id, company_id, company_name, revenue_total,
        revenue_baseline) — the baseline being that company's all-time revenue
        the last time this listing was priced, or None if it never has been.
        """
        cur = await self._conn.execute(
            "SELECT b.id, c.id, c.name, c.revenue_total, b.revenue_baseline "
            f"FROM businesses b {self._REVENUE_LINK_JOIN} "
            "WHERE b.guild_id=? AND b.delisted=0",
            (guild_id,),
        )
        return await cur.fetchall()

    async def get_company_for_listing(self, business_id: int, guild_id: int):
        """The registered business behind one listing, if any.
        Returns (company_id, name, revenue_total, explicit_link)."""
        cur = await self._conn.execute(
            "SELECT c.id, c.name, c.revenue_total, (b.company_id IS NOT NULL) "
            f"FROM businesses b {self._REVENUE_LINK_JOIN} "
            "WHERE b.guild_id=? AND b.id=?",
            (guild_id, business_id),
        )
        return await cur.fetchone()

    async def get_listing_for_company(self, company_id: int, guild_id: int, tx=None):
        """The live stock listing tied to one registered business, if any.
        Returns (business_id, name, stock_price, total_shares)."""
        return await self._fetchone(
            "SELECT b.id, b.name, b.stock_price, b.total_shares "
            f"FROM businesses b {self._REVENUE_LINK_JOIN} "
            "WHERE b.guild_id=? AND b.delisted=0 AND c.id=?",
            (guild_id, company_id), tx=tx,
        )

    async def set_business_revenue_baseline(self, business_id: int, baseline: int, tx=None) -> None:
        await self._exec(
            "UPDATE businesses SET revenue_baseline=? WHERE id=?", (baseline, business_id), tx=tx
        )

    async def log_stock_event(
        self, guild_id: int, business_id: int, change_percent: float, reason: str, actor_id, tx=None
    ):
        await self._exec(
            "INSERT INTO stock_events (guild_id, business_id, change_percent, reason, actor_id, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            (guild_id, business_id, change_percent, reason, actor_id, int(time.time())),
            tx=tx,
        )

    async def get_recent_stock_events(self, guild_id: int, business_id=None, limit: int = 10):
        if business_id is not None:
            cur = await self._conn.execute(
                "SELECT business_id, change_percent, reason, actor_id, timestamp FROM stock_events "
                "WHERE guild_id=? AND business_id=? ORDER BY timestamp DESC LIMIT ?",
                (guild_id, business_id, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT business_id, change_percent, reason, actor_id, timestamp FROM stock_events "
                "WHERE guild_id=? ORDER BY timestamp DESC LIMIT ?",
                (guild_id, limit),
            )
        return await cur.fetchall()

    # -- stock market: holdings ---------------------------------------------------
    async def get_holding(self, user_id: int, guild_id: int, business_id: int, tx=None):
        return await self._fetchone(
            "SELECT shares, cost_basis FROM stock_holdings WHERE user_id=? AND guild_id=? AND business_id=?",
            (user_id, guild_id, business_id), tx=tx,
        )

    async def get_cash(self, user_id: int, guild_id: int, tx=None) -> int:
        """Cash on hand, readable inside a transaction (get_balance may INSERT
        the account row, which must not happen on the shared connection while
        a transaction holds the write lock)."""
        row = await self._fetchone(
            "SELECT cash FROM users WHERE user_id=? AND guild_id=?", (user_id, guild_id), tx=tx
        )
        return int(row[0]) if row else 0

    async def get_company_revenue_total(self, company_id: int, tx=None) -> int:
        row = await self._fetchone(
            "SELECT COALESCE(revenue_total, 0) FROM companies WHERE id=?", (company_id,), tx=tx
        )
        return int(row[0]) if row else 0

    async def get_portfolio(self, user_id: int, guild_id: int):
        cur = await self._conn.execute(
            "SELECT h.business_id, b.name, h.shares, h.cost_basis, b.stock_price, b.delisted "
            "FROM stock_holdings h JOIN businesses b ON h.business_id = b.id "
            "WHERE h.user_id=? AND h.guild_id=? AND h.shares > 0",
            (user_id, guild_id),
        )
        return await cur.fetchall()

    async def get_all_holders(self, business_id: int, tx=None):
        return await self._fetchall(
            "SELECT user_id, shares FROM stock_holdings WHERE business_id=? AND shares > 0", (business_id,), tx=tx
        )

    async def upsert_holding(
        self, user_id: int, guild_id: int, business_id: int, share_delta: int, cost_delta: int, tx=None
    ):
        """Adds shares (and cost basis) to a holding in ONE statement.

        The previous read-then-write let two simultaneous buys of the same
        stock both read "100 shares", both write "150", and lose one buyer's
        shares. The UPSERT does the addition inside the database, against the
        row as it is at that instant, so nothing can be lost."""
        # Only ever called to ADD shares (buys, founder stakes); removals go
        # through try_sell_shares. The MAX(0, ...) on the insert path mirrors
        # what the old code did for a brand-new holding.
        await self._exec(
            "INSERT INTO stock_holdings (user_id, guild_id, business_id, shares, cost_basis) "
            "VALUES (?,?,?,?,MAX(0, ?)) "
            "ON CONFLICT(user_id, guild_id, business_id) DO UPDATE SET "
            "shares = shares + excluded.shares, "
            "cost_basis = MAX(0, cost_basis + excluded.cost_basis)",
            (user_id, guild_id, business_id, share_delta, cost_delta),
            tx=tx,
        )

    async def try_sell_shares(
        self, user_id: int, guild_id: int, business_id: int, shares: int, cost_delta: int, tx=None
    ) -> bool:
        """Race-safe share decrement. The ownership check and the write happen in
        one statement, so two simultaneous sells can never both succeed against
        the same shares (and the balance can never go negative)."""
        if shares <= 0:
            return False
        cur = await self._exec(
            "UPDATE stock_holdings SET shares = shares - ?, cost_basis = MAX(0, cost_basis + ?) "
            "WHERE user_id=? AND guild_id=? AND business_id=? AND shares >= ?",
            (shares, cost_delta, user_id, guild_id, business_id, shares),
            tx=tx,
        )
        return cur.rowcount > 0

    # -- stock market: trade records --------------------------------------------
    async def record_stock_trade(
        self, user_id: int, guild_id: int, business_id: int, side: str, shares: int,
        amount: int, price: float, at_cap: bool, tx=None,
    ):
        await self._exec(
            "INSERT INTO stock_trades (user_id, guild_id, business_id, side, shares, amount, price, at_cap, timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, guild_id, business_id, side, shares, amount, price, 1 if at_cap else 0, int(time.time())),
            tx=tx,
        )

    async def get_user_stock_trades(self, user_id: int, guild_id: int, limit: int = 200):
        """Every /stock buy and sell this player has filled, newest first.
        Returns [(trade_id, business_id, business_name, side, shares, amount,
        price, at_cap, timestamp)]."""
        return await self._fetchall(
            "SELECT t.id, t.business_id, b.name, t.side, t.shares, t.amount, t.price, "
            "t.at_cap, t.timestamp FROM stock_trades t "
            "LEFT JOIN businesses b ON b.id = t.business_id "
            "WHERE t.user_id=? AND t.guild_id=? ORDER BY t.timestamp DESC, t.id DESC LIMIT ?",
            (user_id, guild_id, limit),
        )

    async def get_stock_events_since(self, guild_id: int, since: int):
        """Every recorded price event since `since`, oldest first. Used to
        check whether a player's trades landed suspiciously close to one."""
        return await self._fetchall(
            "SELECT business_id, change_percent, reason, actor_id, timestamp FROM stock_events "
            "WHERE guild_id=? AND timestamp >= ? ORDER BY timestamp ASC",
            (guild_id, since),
        )

    async def get_user_buy_volume(
        self, user_id: int, guild_id: int, business_id: int, since: int, tx=None
    ) -> int:
        """Shares of one business this player has bought since `since`."""
        row = await self._fetchone(
            "SELECT COALESCE(SUM(shares), 0) FROM stock_trades "
            "WHERE user_id=? AND guild_id=? AND business_id=? AND side='buy' AND timestamp >= ?",
            (user_id, guild_id, business_id, since), tx=tx,
        )
        return int(row[0] if row else 0)

    async def clear_holding(self, user_id: int, guild_id: int, business_id: int, tx=None):
        await self._exec(
            "DELETE FROM stock_holdings WHERE user_id=? AND guild_id=? AND business_id=?",
            (user_id, guild_id, business_id), tx=tx,
        )

    # -- real companies (not stock-market businesses) --------------------------
    COMPANY_COLS = "id, name, owner_id, balance, revenue_total, tax_paid, created_at"

    async def create_company(self, guild_id: int, name: str, owner_id: int) -> int:
        cur = await self._conn.execute(
            "INSERT INTO companies (guild_id, name, owner_id, balance, revenue_total, tax_paid, created_at) "
            "VALUES (?,?,?,0,0,0,?)",
            (guild_id, name, owner_id, int(time.time())),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_company_by_name(self, name: str, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.COMPANY_COLS} FROM companies WHERE guild_id=? AND LOWER(name)=LOWER(?)",
            (guild_id, name),
        )
        return await cur.fetchone()

    async def list_companies(self, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.COMPANY_COLS} FROM companies WHERE guild_id=? ORDER BY name", (guild_id,)
        )
        return await cur.fetchall()

    async def get_companies_by_owner(self, owner_id: int, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.COMPANY_COLS} FROM companies WHERE guild_id=? AND owner_id=? ORDER BY name",
            (guild_id, owner_id),
        )
        return await cur.fetchall()

    async def add_company_revenue(
        self, company_id: int, guild_id: int, amount: int, description: str, actor_id, tx=None
    ):
        """Credits money the company received: bumps balance + all-time revenue and
        writes a permanent ledger entry so staff can audit every dollar ever earned."""
        conn = tx if tx is not None else self._conn
        await conn.execute(
            "UPDATE companies SET balance = balance + ?, revenue_total = revenue_total + ? WHERE id=?",
            (amount, amount, company_id),
        )
        await conn.execute(
            "INSERT INTO company_ledger (company_id, guild_id, amount, description, actor_id, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            (company_id, guild_id, amount, description, actor_id, int(time.time())),
        )
        if tx is None:
            await self._conn.commit()

    async def company_deposit(
        self, company_id: int, guild_id: int, amount: int, description: str, actor_id
    ) -> None:
        """Credits the owner's OWN money into the business account.

        Deliberately different from add_company_revenue(): it does NOT touch
        revenue_total, so a deposit is never taxed (it is already-taxed personal
        money), and it never feeds anything that moves a stock price. It is
        tracked separately in deposits_total purely so staff can see it in
        /eco-admin company info."""
        await self._conn.execute(
            "UPDATE companies SET balance = balance + ?, deposits_total = deposits_total + ? WHERE id=?",
            (amount, amount, company_id),
        )
        await self._conn.execute(
            "INSERT INTO company_ledger (company_id, guild_id, amount, description, actor_id, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            (company_id, guild_id, amount, description, actor_id, int(time.time())),
        )
        await self._conn.commit()

    async def get_company_deposits(self, company_id: int) -> int:
        cur = await self._conn.execute(
            "SELECT COALESCE(deposits_total, 0) FROM companies WHERE id=?", (company_id,)
        )
        row = await cur.fetchone()
        return (row[0] if row else 0) or 0

    async def get_company_by_id(self, company_id: int, guild_id: int):
        cur = await self._conn.execute(
            f"SELECT {self.COMPANY_COLS} FROM companies WHERE id=? AND guild_id=?",
            (company_id, guild_id),
        )
        return await cur.fetchone()

    async def delete_company(self, company_id: int, guild_id: int) -> dict:
        """Permanently removes ONE registered business: the company row, its
        staff roster and its whole ledger, in a single transaction — so a failure
        halfway through can never leave employees attached to a business that no
        longer exists.

        This deliberately does NOT touch the stock market. If a listing was
        priced off this business, it is only unlinked (company_id and the
        revenue baseline are cleared) and carries on trading on random drift.
        Delisting a stock and paying its shareholders out is a separate,
        explicit action: /eco-admin business delist.

        Returns the number of rows removed per table.
        """
        deleted = {}
        try:
            # Unlink first, while the company row still exists.
            await self._conn.execute(
                "UPDATE businesses SET company_id=NULL, revenue_baseline=NULL "
                "WHERE guild_id=? AND company_id=?",
                (guild_id, company_id),
            )
            cur = await self._conn.execute(
                "DELETE FROM company_staff WHERE company_id=? AND guild_id=?",
                (company_id, guild_id),
            )
            deleted["company_staff"] = max(0, cur.rowcount or 0)
            cur = await self._conn.execute(
                "DELETE FROM company_ledger WHERE company_id=? AND guild_id=?",
                (company_id, guild_id),
            )
            deleted["company_ledger"] = max(0, cur.rowcount or 0)
            cur = await self._conn.execute(
                "DELETE FROM companies WHERE id=? AND guild_id=?", (company_id, guild_id)
            )
            deleted["companies"] = max(0, cur.rowcount or 0)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return deleted

    async def set_company_owner(self, company_id: int, owner_id) -> None:
        await self._conn.execute("UPDATE companies SET owner_id=? WHERE id=?", (owner_id, company_id))
        await self._conn.commit()

    # -- company staff ------------------------------------------------------
    async def set_company_staff(
        self, company_id: int, guild_id: int, user_id: int, position: str, hired_by: int
    ) -> None:
        """Hires a new employee or changes an existing one's position."""
        await self._conn.execute(
            "INSERT INTO company_staff (company_id, guild_id, user_id, position, hired_at, hired_by) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(company_id, user_id) DO UPDATE SET position=excluded.position",
            (company_id, guild_id, user_id, position, int(time.time()), hired_by),
        )
        await self._conn.commit()

    async def remove_company_staff(self, company_id: int, user_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM company_staff WHERE company_id=? AND user_id=?", (company_id, user_id)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_company_staff(self, company_id: int):
        """Every employee: [(user_id, position, hired_at), ...]"""
        cur = await self._conn.execute(
            "SELECT user_id, position, hired_at FROM company_staff WHERE company_id=? "
            "ORDER BY hired_at",
            (company_id,),
        )
        return await cur.fetchall()

    async def count_company_staff(self, company_id: int) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM company_staff WHERE company_id=?", (company_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def get_company_staff_position(self, company_id: int, user_id: int) -> Optional[str]:
        cur = await self._conn.execute(
            "SELECT position FROM company_staff WHERE company_id=? AND user_id=?",
            (company_id, user_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def get_companies_for_staff(self, user_id: int, guild_id: int):
        """Companies a player works at (not ones they own):
        [(company_id, name, position), ...]"""
        cur = await self._conn.execute(
            "SELECT c.id, c.name, s.position FROM company_staff s "
            "JOIN companies c ON c.id = s.company_id "
            "WHERE s.user_id=? AND s.guild_id=? ORDER BY c.name",
            (user_id, guild_id),
        )
        return await cur.fetchall()

    async def company_withdraw(self, company_id: int, amount: int) -> bool:
        """Race-safe deduction from a company's balance. Returns False if insufficient."""
        cur = await self._conn.execute(
            "UPDATE companies SET balance = balance - ? WHERE id=? AND balance >= ?",
            (amount, company_id, amount),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def pay_company_tax(self, company_id: int, guild_id: int, amount: int, actor_id: int) -> bool:
        """Deducts a tax payment from the company balance, records it as paid, and
        writes a ledger entry. Returns False if the balance was insufficient."""
        cur = await self._conn.execute(
            "UPDATE companies SET balance = balance - ?, tax_paid = tax_paid + ? "
            "WHERE id=? AND balance >= ?",
            (amount, amount, company_id, amount),
        )
        if cur.rowcount == 0:
            await self._conn.rollback()
            return False
        await self._conn.execute(
            "INSERT INTO company_ledger (company_id, guild_id, amount, description, actor_id, timestamp) "
            "VALUES (?,?,?, 'Tax payment', ?, ?)",
            (company_id, guild_id, -amount, actor_id, int(time.time())),
        )
        await self._conn.commit()
        return True

    async def get_company_ledger(self, company_id: int, limit: int = 25):
        cur = await self._conn.execute(
            "SELECT amount, description, actor_id, timestamp FROM company_ledger "
            "WHERE company_id=? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (company_id, limit),
        )
        return await cur.fetchall()

    # -- guild settings (market announcement channel) ------------------------------
    async def set_market_channel(self, guild_id: int, channel_id):
        await self._conn.execute(
            "INSERT INTO guild_settings (guild_id, market_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET market_channel_id=excluded.market_channel_id",
            (guild_id, channel_id),
        )
        await self._conn.commit()

    async def get_market_channel(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT market_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_log_channel(self, guild_id: int, channel_id):
        await self._conn.execute(
            "INSERT INTO guild_settings (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
            (guild_id, channel_id),
        )
        await self._conn.commit()

    async def get_log_channel(self, guild_id: int):
        cur = await self._conn.execute(
            "SELECT log_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    # -- guild settings (channel restrictions) --------------------------------
    # Stored as a comma-separated list of channel IDs in one column per area,
    # which keeps this to a plain guild_settings row instead of another table.
    # whitelist_role_ids holds role IDs rather than channel IDs, but the
    # storage shape (a comma-separated ID list on the guild row) is identical,
    # so it shares these two accessors.
    CHANNEL_LOCK_COLUMNS = ("stock_channel_ids", "business_channel_ids", "whitelist_role_ids")

    async def get_channel_lock(self, guild_id: int, column: str) -> List[int]:
        """Channel IDs the given area is restricted to. Empty list = unrestricted."""
        if column not in self.CHANNEL_LOCK_COLUMNS:
            raise ValueError(f"Unknown channel lock column: {column}")
        cur = await self._conn.execute(
            f"SELECT {column} FROM guild_settings WHERE guild_id=?", (guild_id,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return []
        ids = []
        for part in str(row[0]).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    async def set_channel_lock(self, guild_id: int, column: str, channel_ids) -> List[int]:
        """Replaces the list for an area. Pass an empty list to unrestrict it.
        Returns the list that was stored."""
        if column not in self.CHANNEL_LOCK_COLUMNS:
            raise ValueError(f"Unknown channel lock column: {column}")
        # De-duplicate while preserving the order staff added them in.
        unique = list(dict.fromkeys(int(c) for c in channel_ids))
        value = ",".join(str(c) for c in unique) if unique else None
        await self._conn.execute(
            f"INSERT INTO guild_settings (guild_id, {column}) VALUES (?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column}",
            (guild_id, value),
        )
        await self._conn.commit()
        return unique

    # -- full economy reset ---------------------------------------------------
    # Every table that holds per-server economy data. guild_settings is
    # deliberately NOT in this list: a reset should not also wipe the log
    # channel, the market channel and the channel restrictions staff set up.
    RESET_TABLES = (
        "users",
        "vehicles",
        "fines",
        "loans",
        "loan_requests",
        "transactions",
        "treasury",
        "businesses",
        "stock_holdings",
        "stock_events",
        "companies",
        "company_staff",
        "company_ledger",
        "stock_trades",
        "keyed_cooldowns",
    )

    # -- reconciliation audit queries (see audit.py) --------------------------
    async def get_money_supply(self, guild_id: int) -> dict:
        """Every dollar that exists in the server right now, by where it sits."""
        users = await self._fetchone(
            "SELECT COUNT(*), COALESCE(SUM(cash),0), COALESCE(SUM(bank),0), COALESCE(SUM(savings),0) "
            "FROM users WHERE guild_id=?", (guild_id,)
        )
        companies = await self._fetchone(
            "SELECT COALESCE(SUM(balance),0) FROM companies WHERE guild_id=?", (guild_id,)
        )
        treasury = await self._fetchone(
            "SELECT COALESCE(balance,0) FROM treasury WHERE guild_id=?", (guild_id,)
        )
        parts = {
            "users": users[0],
            "cash": users[1],
            "bank": users[2],
            "savings": users[3],
            "companies": companies[0] if companies else 0,
            "treasury": treasury[0] if treasury else 0,
        }
        parts["total"] = parts["cash"] + parts["bank"] + parts["savings"] + parts["companies"] + parts["treasury"]
        return parts

    async def get_max_transaction_id(self, guild_id: int) -> int:
        row = await self._fetchone(
            "SELECT COALESCE(MAX(id),0) FROM transactions WHERE guild_id=?", (guild_id,)
        )
        return int(row[0] if row else 0)

    async def sum_transactions_by_type(self, guild_id: int, after_id: int, up_to_id: int):
        """[(type, count, sum_amount)] for transactions with after_id < id <= up_to_id."""
        return await self._fetchall(
            "SELECT type, COUNT(*), COALESCE(SUM(amount),0) FROM transactions "
            "WHERE guild_id=? AND id > ? AND id <= ? GROUP BY type",
            (guild_id, after_id, up_to_id),
        )

    async def count_users_created_between(self, guild_id: int, start: int, end: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM users WHERE guild_id=? AND created_at > ? AND created_at <= ?",
            (guild_id, start, end),
        )
        return int(row[0] if row else 0)

    async def find_cap_trade_bursts(
        self, guild_id: int, since: int, min_count: int, window_secs: int
    ):
        """Players who filled `min_count`+ orders at the per-order cap on one
        stock inside `window_secs`. Returns [(user_id, business_id, name,
        count, first_ts, last_ts)]; a burst is a run of at-cap trades whose
        first and last fall within the window."""
        rows = await self._fetchall(
            "SELECT t.user_id, t.business_id, b.name, t.side, t.timestamp FROM stock_trades t "
            "JOIN businesses b ON b.id = t.business_id "
            "WHERE t.guild_id=? AND t.at_cap=1 AND t.timestamp >= ? "
            "ORDER BY t.user_id, t.business_id, t.timestamp",
            (guild_id, since),
        )
        findings = []
        by_key = {}
        for user_id, business_id, name, side, ts in rows:
            by_key.setdefault((user_id, business_id, name), []).append(ts)
        for (user_id, business_id, name), stamps in by_key.items():
            # Sliding window over the sorted timestamps.
            start = 0
            best = None
            for end in range(len(stamps)):
                while stamps[end] - stamps[start] > window_secs:
                    start += 1
                count = end - start + 1
                if count >= min_count and (best is None or count > best[0]):
                    best = (count, stamps[start], stamps[end])
            if best:
                findings.append((user_id, business_id, name, best[0], best[1], best[2]))
        return findings

    async def find_large_insider_sells(self, guild_id: int, since: int, min_pct: float):
        """Sells since `since` of at least min_pct of a listing's total shares
        by that listing's owner or the linked company's owner. Returns
        [(user_id, business_id, business_name, shares, amount, ts, company_id)]."""
        return await self._fetchall(
            "SELECT t.user_id, t.business_id, b.name, t.shares, t.amount, t.timestamp, c.id "
            "FROM stock_trades t JOIN businesses b ON b.id = t.business_id "
            f"LEFT {self._REVENUE_LINK_JOIN} "
            "WHERE t.guild_id=? AND t.side='sell' AND t.timestamp >= ? "
            "AND t.shares >= ? * b.total_shares "
            "AND (t.user_id = b.owner_id OR t.user_id = c.owner_id)",
            (guild_id, since, min_pct),
        )

    async def get_company_payers_between(self, company_id: int, start: int, end: int):
        """[(actor_id, payments, total)] for positive ledger entries in a window."""
        return await self._fetchall(
            "SELECT actor_id, COUNT(*), COALESCE(SUM(amount),0) FROM company_ledger "
            "WHERE company_id=? AND amount > 0 AND actor_id IS NOT NULL "
            "AND timestamp > ? AND timestamp <= ? GROUP BY actor_id",
            (company_id, start, end),
        )

    async def get_account_activity(self, user_id: int, guild_id: int):
        """(transaction_count, first_seen_ts) — how 'real' an account looks."""
        row = await self._fetchone(
            "SELECT COUNT(*), COALESCE(MIN(timestamp), 0) FROM transactions WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        created = await self._fetchone(
            "SELECT COALESCE(created_at, 0) FROM users WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        first_seen = row[1] if row else 0
        if created and created[0]:
            first_seen = min(first_seen, created[0]) if first_seen else created[0]
        return (int(row[0]) if row else 0, int(first_seen or 0))

    async def count_guild_economy(self, guild_id: int) -> dict:
        """Row counts per table, used to show exactly what a reset is about to
        destroy before anyone confirms it."""
        counts = {}
        for table in self.RESET_TABLES:
            cur = await self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE guild_id=?", (guild_id,)
            )
            row = await cur.fetchone()
            counts[table] = row[0] if row else 0
        return counts

    async def reset_guild_economy(self, guild_id: int, reset_settings: bool = False) -> dict:
        """Erases every economy record for one server in a single transaction:
        either all of it goes or none of it does, so an interrupted reset can
        never leave half an economy behind (companies deleted, say, while their
        shareholders still hold shares in them).

        Returns the number of rows deleted per table.
        """
        deleted = {}
        try:
            for table in self.RESET_TABLES:
                cur = await self._conn.execute(
                    f"DELETE FROM {table} WHERE guild_id=?", (guild_id,)
                )
                deleted[table] = max(0, cur.rowcount or 0)
            if reset_settings:
                cur = await self._conn.execute(
                    "DELETE FROM guild_settings WHERE guild_id=?", (guild_id,)
                )
                deleted["guild_settings"] = max(0, cur.rowcount or 0)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return deleted

    async def get_all_guild_ids_with_businesses(self):
        cur = await self._conn.execute("SELECT DISTINCT guild_id FROM businesses WHERE delisted=0")
        rows = await cur.fetchall()
        return [r[0] for r in rows]
