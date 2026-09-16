"""
Offline checks for the update-safety work and the two new staff commands.

Run with:  python3 tests_update_safety.py

Nothing here touches Discord. It drives the real Database class against a
throwaway copy of the economy, so the results reflect the actual code the bot
runs.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import Database
import stockhistory
from cogs.persistence import compare_fingerprints, LAST_RUN_KEY

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "economy.db"
PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


async def test_checkpointing(workdir):
    print("\n[1] WAL never hides data from a copy of economy.db")
    path = os.path.join(workdir, "eco.db")
    shutil.copy2(SOURCE, path)

    db = Database(path)
    await db.connect()
    gid = (await db.get_all_guild_ids())[0]

    # Simulate a burst of play.
    for i in range(60):
        await db.log_transaction(900000 + i, gid, "test_play", 1000, "simulated activity")

    wal_before = db.wal_size()
    folded = await db.checkpoint()
    check("checkpoint() reports what it flushed", folded >= 0, f"{folded} bytes")

    # THE REAL TEST: copy ONLY economy.db, exactly like a host file download,
    # and confirm the copy contains everything.
    copy_path = os.path.join(workdir, "copied-by-hand.db")
    shutil.copy2(path, copy_path)

    live = await db.economy_fingerprint()
    await db.close()

    conn = sqlite3.connect(copy_path)
    copied_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    copied_supply = conn.execute("SELECT COALESCE(SUM(cash+bank+savings),0) FROM users").fetchone()[0]
    conn.close()

    check("a plain copy of economy.db has every transaction",
          copied_tx == live["transactions"], f"{copied_tx} vs {live['transactions']}")
    check("a plain copy of economy.db has the full money supply",
          copied_supply == live["money_supply"], f"{copied_supply} vs {live['money_supply']}")
    check("nothing was left stranded in the WAL", db.wal_size() == 0, f"was {wal_before} before flush")


async def test_loss_detection(workdir):
    print("\n[2] Uploading an OLD database is detected, not absorbed silently")
    good = os.path.join(workdir, "good.db")
    shutil.copy2(SOURCE, good)

    db = Database(good)
    await db.connect()
    gid = (await db.get_all_guild_ids())[0]
    before = await db.economy_fingerprint()
    before["saved_at"] = int(time.time())
    await db.set_meta(LAST_RUN_KEY, json.dumps(before))
    await db.checkpoint()

    # Keep a copy of this exact state, then carry on playing.
    stale = os.path.join(workdir, "stale.db")
    shutil.copy2(good, stale)

    for i in range(25):
        await db.log_transaction(800000 + i, gid, "test_play", 500, "after the snapshot")
    await db.record_stock_trade(800001, gid, 1, "buy", 100, 5000, 50.0, False)
    await db.checkpoint()
    newer = await db.economy_fingerprint()
    await db.close()

    # Now the mistake: restart on the STALE file.
    db2 = Database(stale)
    await db2.connect()
    stored = json.loads(await db2.get_meta(LAST_RUN_KEY))
    # The stale file's own record is from before the extra play, so simulate
    # the real case: the bot last ran on the newer economy.
    result = compare_fingerprints(newer, await db2.economy_fingerprint())
    await db2.close()

    check("restoring an older database is flagged as data loss",
          result["verdict"] == "lost", f"verdict={result['verdict']}")
    lost_keys = {e["key"] for e in result["losses"]}
    check("it names the missing transactions", "transactions" in lost_keys)
    check("it names the missing trades", "stock_trades" in lost_keys)

    # And the healthy case must stay quiet.
    db3 = Database(good)
    await db3.connect()
    same = compare_fingerprints(newer, await db3.economy_fingerprint())
    await db3.close()
    check("an intact database is NOT flagged", same["verdict"] != "lost", f"verdict={same['verdict']}")

    # Ordinary spending must not look like loss.
    spent = dict(newer)
    spent["money_supply"] -= 50_000
    spent["cash"] -= 50_000
    drift = compare_fingerprints(newer, spent)
    check("normal spending is a warning, not data loss", drift["verdict"] == "warn")


async def test_transactions_lookup(workdir):
    print("\n[3] Admin transaction lookup returns the right player's rows")
    path = os.path.join(workdir, "tx.db")
    shutil.copy2(SOURCE, path)
    db = Database(path)
    await db.connect()
    gid = (await db.get_all_guild_ids())[0]

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT user_id, COUNT(*) c FROM transactions WHERE guild_id=? "
        "GROUP BY user_id ORDER BY c DESC LIMIT 1", (gid,)
    ).fetchone()
    conn.close()

    if not row:
        check("a player with transactions exists", False)
        await db.close()
        return

    user_id, count = row
    rows = await db.get_transactions(user_id, gid, limit=50)
    check("lookup returns that player's history", len(rows) == min(50, count), f"{len(rows)} rows")
    check("rows are newest first",
          all(rows[i][3] >= rows[i + 1][3] for i in range(len(rows) - 1)))
    check("an untouched user id returns nothing",
          await db.get_transactions(1, gid, limit=10) == [])
    await db.close()


async def test_stock_history(workdir):
    print("\n[4] /stock history analysis")
    path = os.path.join(workdir, "sh.db")
    shutil.copy2(SOURCE, path)
    db = Database(path)
    await db.connect()
    gid = (await db.get_all_guild_ids())[0]

    conn = sqlite3.connect(path)
    traders = conn.execute(
        "SELECT user_id, COUNT(*) c FROM stock_trades WHERE guild_id=? "
        "GROUP BY user_id ORDER BY c DESC", (gid,)
    ).fetchall()
    conn.close()

    listings = await db.list_businesses(gid, include_delisted=True)
    businesses = {
        b[0]: {"name": b[1], "owner_id": b[2], "price": b[3], "is_own": False} for b in listings
    }

    for user_id, count in traders[:5]:
        trades = await db.get_user_stock_trades(user_id, gid)
        holdings = {r[0]: r[2] for r in await db.get_portfolio(user_id, gid)}
        events = await db.get_stock_events_since(gid, min(t[8] for t in trades))
        report = stockhistory.analyse(trades, events, businesses, holdings)
        check(f"real player {user_id} analysed without error",
              report["summary"]["orders"] == count,
              f"{count} orders, {len(report['findings'])} flags, verdict '{report['verdict']['title']}'")

    # --- a deliberately obvious cheat, to prove the flags actually fire ---
    now = int(time.time())
    biz = {7: {"name": "Shady Motors", "owner_id": 42, "price": 20.0, "is_own": True}}
    cheat_trades = [
        # (id, business_id, name, side, shares, amount, price, at_cap, ts)
        (5, 7, "Shady Motors", "sell", 1000, 30000, 30.0, 0, now - 3000),
        (4, 7, "Shady Motors", "buy", 400, 4000, 10.0, 1, now - 4000),
        (3, 7, "Shady Motors", "buy", 400, 4000, 10.0, 1, now - 4200),
        (2, 7, "Shady Motors", "buy", 400, 4000, 10.0, 1, now - 4400),
        (1, 7, "Shady Motors", "buy", 200, 2000, 10.0, 0, now - 4600),
    ]
    cheat_events = [(7, 30.0, "Admin adjustment: new contract", 99, now - 3600)]
    report = stockhistory.analyse(cheat_trades, cheat_events, biz, {}, now=now)
    kinds = {f["kind"] for f in report["findings"]}

    check("catches buying right before a price jump", "event-timing" in kinds)
    check("catches an instant flip for profit", "flip" in kinds)
    check("catches repeated maximum-size orders", "cap-burst" in kinds)
    check("catches selling their own business's stock", "self-dealing" in kinds)
    check("calls it a likely exploit", report["worst"] == stockhistory.HIGH,
          report["verdict"]["title"])
    check("realised profit is correct", report["summary"]["realised"] == 20000,
          f"got {report[chr(39)+chr(39)]}" if False else f"got {report['summary']['realised']}")

    # --- an honest long-term investor must come back clean ---
    clean_trades = [
        (2, 7, "Shady Motors", "sell", 100, 1200, 12.0, 0, now - 100000),
        (1, 7, "Shady Motors", "buy", 100, 1000, 10.0, 0, now - 900000),
    ]
    clean = stockhistory.analyse(
        clean_trades, [], {7: {"name": "Shady Motors", "owner_id": None, "price": 12.0}}, {}, now=now
    )
    check("an ordinary investor is not flagged", clean["findings"] == [],
          f"{len(clean['findings'])} flags")
    check("their profit is still reported", clean["summary"]["realised"] == 200)

    await db.close()


async def main():
    if not os.path.exists(SOURCE):
        print(f"Database not found: {SOURCE}")
        return 1
    with tempfile.TemporaryDirectory() as workdir:
        await test_checkpointing(workdir)
        await test_loss_detection(workdir)
        await test_transactions_lookup(workdir)
        await test_stock_history(workdir)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
