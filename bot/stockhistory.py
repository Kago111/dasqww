"""
Per-player stock activity analysis behind /stock history.

The reconciliation audit in audit.py sweeps the WHOLE server looking for
patterns. This module does the opposite: staff name one player, and it lays out
everything that player has done on the market with the dodgy bits called out.

Everything here is pure functions over rows already fetched from the database,
so the shape of a report is easy to reason about (and to change) without
touching Discord code.

WHAT IT LOOKS FOR
-----------------
  * Insider buying   — bought just before the price jumped
  * Insider selling  — sold just before the price fell
  * Fast flips       — bought and sold the same stock within the hour for a
                       large profit
  * Cap hammering    — repeatedly filling the maximum allowed order size on one
                       stock in a short window (the classic way to walk a price
                       up past the per-order limit)
  * Self-dealing     — trading a stock the player themselves owns

Each finding carries a severity so the report can lead with the worst one and
staff do not have to read the whole thing to know whether it matters.
"""

import time
from collections import defaultdict

import config

# Severity ladder. Higher = worse.
LOW, MEDIUM, HIGH = 1, 2, 3
SEVERITY_EMOJI = {LOW: "\U0001f7e1", MEDIUM: "\U0001f7e0", HIGH: "\U0001f534"}
SEVERITY_NAME = {LOW: "Minor", MEDIUM: "Suspicious", HIGH: "Likely exploit"}


def _cfg(name, default):
    return getattr(config, name, default)


def analyse(trades, events, businesses, holdings, now=None):
    """Builds the report.

    trades     -- [(id, business_id, name, side, shares, amount, price, at_cap, ts)]
                  newest first, as returned by Database.get_user_stock_trades
    events     -- [(business_id, change_percent, reason, actor_id, ts)] oldest first
    businesses -- {business_id: {"name": str, "owner_id": int|None, "price": float}}
    holdings   -- {business_id: shares_still_held}

    Returns a dict the command renders directly.
    """
    now = int(now or time.time())
    chronological = sorted(trades, key=lambda t: (t[8], t[0]))

    summary = _summarise(chronological, businesses, holdings)
    findings = []
    findings += _find_event_timing(chronological, events)
    findings += _find_fast_flips(chronological)
    findings += _find_cap_bursts(chronological)
    findings += _find_self_dealing(chronological, businesses)

    findings.sort(key=lambda f: (-f["severity"], -f["ts"]))
    worst = max((f["severity"] for f in findings), default=0)

    # Any one of these has an innocent explanation. Three different kinds of
    # them in the same player's history does not, so the report escalates
    # rather than leaving staff to spot the pattern themselves.
    distinct_kinds = {f["kind"] for f in findings if f["severity"] >= MEDIUM}
    compounded = worst == MEDIUM and len(distinct_kinds) >= 3
    if compounded:
        worst = HIGH

    return {
        "summary": summary,
        "findings": findings,
        "worst": worst,
        "verdict": _verdict(worst, len(findings), compounded),
        "trades": trades,
        "generated_at": now,
    }


def _verdict(worst, count, compounded=False):
    if worst >= HIGH:
        text = (
            "Several different warning signs at once. Individually each has an innocent "
            "explanation; together they do not."
            if compounded else
            "This player's trades line up with price moves in a way normal play does not. "
            "Worth acting on."
        )
        return {"emoji": "\U0001f534", "title": "Likely exploit", "text": text}
    if worst == MEDIUM:
        return {
            "emoji": "\U0001f7e0",
            "title": "Suspicious",
            "text": "Some of this looks engineered rather than lucky. Read the flags below before deciding.",
        }
    if worst == LOW:
        return {
            "emoji": "\U0001f7e1",
            "title": "Worth a glance",
            "text": "Nothing alarming, but a couple of things stand out. Probably just an active trader.",
        }
    if count == 0:
        return {
            "emoji": "\U0001f7e2",
            "title": "Nothing unusual",
            "text": "No exploit patterns in this player's trading history.",
        }
    return {"emoji": "\U0001f7e2", "title": "Nothing unusual", "text": ""}


# --------------------------------------------------------------------------- #
# Summary: money in, money out, and what the profit actually was.
# --------------------------------------------------------------------------- #
def _summarise(chronological, businesses, holdings):
    spent = sum(t[5] for t in chronological if t[3] == "buy")
    received = sum(t[5] for t in chronological if t[3] == "sell")
    buys = sum(1 for t in chronological if t[3] == "buy")
    sells = sum(1 for t in chronological if t[3] == "sell")

    # Realised profit, matching each sale against the shares it actually sold
    # (oldest first), so a player who bought cheap and sold dear shows the gain
    # on that round trip rather than a meaningless spend-vs-receive figure.
    lots = defaultdict(list)   # business_id -> [[shares, cost_per_share], ...]
    realised = 0
    for _, biz_id, _, side, shares, amount, price, _, _ in chronological:
        if shares <= 0:
            continue
        if side == "buy":
            lots[biz_id].append([shares, amount / shares])
            continue
        proceeds_per_share = amount / shares
        left = shares
        while left > 0 and lots[biz_id]:
            lot = lots[biz_id][0]
            take = min(left, lot[0])
            realised += round(take * (proceeds_per_share - lot[1]))
            lot[0] -= take
            left -= take
            if lot[0] <= 0:
                lots[biz_id].pop(0)
        if left > 0:
            # Sold shares with no purchase on record (bought before trade
            # logging existed, or granted as a founder stake).
            realised += round(left * proceeds_per_share)

    still_held = []
    for biz_id, shares in sorted(holdings.items(), key=lambda kv: -kv[1]):
        if shares <= 0:
            continue
        info = businesses.get(biz_id, {})
        still_held.append({
            "name": info.get("name", f"#{biz_id}"),
            "shares": shares,
            "value": round(shares * info.get("price", 0)),
        })

    return {
        "orders": len(chronological),
        "buys": buys,
        "sells": sells,
        "spent": spent,
        "received": received,
        "realised": realised,
        "still_held": still_held,
        "first_ts": chronological[0][8] if chronological else 0,
        "last_ts": chronological[-1][8] if chronological else 0,
        "stocks_traded": len({t[1] for t in chronological}),
    }


# --------------------------------------------------------------------------- #
# Flag 1: trades timed around recorded price events.
#
# Buying minutes before a listing jumps, or selling minutes before it drops, is
# the single clearest sign someone knew what was coming — whether from a staff
# adjustment or from being told a revenue payment was about to land.
# --------------------------------------------------------------------------- #
def _find_event_timing(chronological, events):
    window = int(_cfg("HISTORY_EVENT_WINDOW_MINUTES", 60)) * 60
    min_move = float(_cfg("HISTORY_EVENT_MIN_MOVE", 0.05))
    # How much the player has to have actually gained before this is worth
    # staff's attention. Without this, a $300 stroke of luck reads the same as
    # a coordinated pump, and the report cries wolf.
    min_benefit = int(_cfg("HISTORY_EVENT_MIN_BENEFIT", 2500))
    serious_benefit = int(_cfg("HISTORY_EVENT_SERIOUS_BENEFIT", 25000))

    by_business = defaultdict(list)
    for biz_id, change, reason, actor_id, ts in events:
        by_business[biz_id].append((ts, change, reason))

    # Group every well-timed trade around the SAME price event into one
    # finding. Three buys in the half hour before a spike is one story, not
    # three, and told once it is far easier to judge.
    grouped = {}
    for trade_id, biz_id, name, side, shares, amount, price, at_cap, ts in chronological:
        for event_ts, change, reason in by_business.get(biz_id, ()):
            gap = event_ts - ts
            if not (0 <= gap <= window):
                continue
            # change_percent is stored as a percentage (15.0 = +15%).
            move = change / 100.0
            if abs(move) < min_move:
                continue
            if not ((side == "buy" and move > 0) or (side == "sell" and move < 0)):
                continue

            key = (biz_id, event_ts, side)
            entry = grouped.setdefault(key, {
                "name": name, "side": side, "change": change, "reason": reason,
                "orders": 0, "shares": 0, "amount": 0, "benefit": 0,
                "closest_gap": gap, "last_ts": ts,
            })
            entry["orders"] += 1
            entry["shares"] += shares
            entry["amount"] += amount
            entry["benefit"] += round(amount * abs(move))
            entry["closest_gap"] = min(entry["closest_gap"], gap)
            entry["last_ts"] = max(entry["last_ts"], ts)

    findings = []
    for entry in grouped.values():
        benefit = entry["benefit"]
        if benefit < min_benefit and entry["orders"] < 3:
            # Too small to mean anything, and not a repeated pattern either.
            continue

        minutes = max(1, entry["closest_gap"] // 60)
        how_often = "" if entry["orders"] == 1 else f" across **{entry['orders']}** orders"

        if entry["side"] == "buy":
            detail = (
                f"Bought **{entry['shares']:,}** {entry['name']}{how_often} in the "
                f"**{minutes} min before** it rose **{entry['change']:+.1f}%** \u2014 "
                f"worth about **${benefit:,}** to them"
            )
        else:
            detail = (
                f"Sold **{entry['shares']:,}** {entry['name']}{how_often} in the "
                f"**{minutes} min before** it fell **{entry['change']:+.1f}%** \u2014 "
                f"dodged about **${benefit:,}** of losses"
            )

        if benefit >= serious_benefit and minutes <= 15:
            severity = HIGH
        elif benefit >= min_benefit:
            severity = MEDIUM
        else:
            severity = LOW

        findings.append({
            "kind": "event-timing",
            "severity": severity,
            "title": "Traded right before a price move",
            "detail": detail,
            "context": f"The move was logged as: {entry['reason'] or 'no reason recorded'}",
            "ts": entry["last_ts"],
            "business": entry["name"],
        })
    return findings


# --------------------------------------------------------------------------- #
# Flag 2: fast flips — in and out of the same stock for a big gain.
# --------------------------------------------------------------------------- #
def _find_fast_flips(chronological):
    window = int(_cfg("HISTORY_FLIP_MINUTES", 60)) * 60
    min_gain = float(_cfg("HISTORY_FLIP_MIN_GAIN", 0.25))

    open_lots = defaultdict(list)   # business_id -> [[shares, cost_per_share, ts], ...]
    findings = []

    for _, biz_id, name, side, shares, amount, price, _, ts in chronological:
        if shares <= 0:
            continue
        if side == "buy":
            open_lots[biz_id].append([shares, amount / shares, ts])
            continue

        proceeds_per_share = amount / shares
        left = shares
        # One sale can close several purchases at once. That is still ONE flip
        # as far as staff are concerned, so it is accumulated and reported as a
        # single line rather than one per purchase.
        flip = {"shares": 0, "profit": 0, "cost": 0, "held": 0, "worst_gain": 0}
        while left > 0 and open_lots[biz_id]:
            lot = open_lots[biz_id][0]
            take = min(left, lot[0])
            held_for = ts - lot[2]
            gain_pct = (proceeds_per_share - lot[1]) / lot[1] if lot[1] else 0
            profit = round(take * (proceeds_per_share - lot[1]))

            if held_for <= window and gain_pct >= min_gain and profit > 0:
                flip["shares"] += take
                flip["profit"] += profit
                flip["cost"] += take * lot[1]
                flip["held"] = max(flip["held"], held_for)
                flip["worst_gain"] = max(flip["worst_gain"], gain_pct)

            lot[0] -= take
            left -= take
            if lot[0] <= 0:
                open_lots[biz_id].pop(0)

        if flip["shares"]:
            minutes = max(1, flip["held"] // 60)
            gain_pct = flip["worst_gain"]
            avg_cost = flip["cost"] / flip["shares"]
            # Doubling your money on a stock you held for under an hour is not
            # a trade, it is a price being moved on purpose.
            severity = HIGH if gain_pct >= 1.0 or (gain_pct >= 0.75 and minutes <= 15) else MEDIUM
            findings.append({
                "kind": "flip",
                "severity": severity,
                "title": "Bought and sold again almost immediately",
                "detail": (
                    f"**{flip['shares']:,}** {name} held for under **{minutes} min** and sold "
                    f"for **+{gain_pct * 100:.0f}%** \u2014 **${flip['profit']:,}** profit"
                ),
                "context": (
                    f"In at ${avg_cost:,.2f}/share, out at ${proceeds_per_share:,.2f}/share"
                ),
                "ts": ts,
                "business": name,
            })
    return findings


# --------------------------------------------------------------------------- #
# Flag 3: repeatedly filling the maximum order size on one stock.
#
# The per-order cap exists to stop one player moving a price in a single trade.
# Firing off cap-sized orders back to back is that same manipulation, just
# spread over a few minutes to get around the limit.
# --------------------------------------------------------------------------- #
def _find_cap_bursts(chronological):
    min_count = int(_cfg("AUDIT_CAP_TRADE_COUNT", 3))
    window = int(_cfg("AUDIT_CAP_TRADE_WINDOW_MINUTES", 90)) * 60

    by_business = defaultdict(list)
    for _, biz_id, name, side, shares, amount, price, at_cap, ts in chronological:
        if at_cap:
            by_business[(biz_id, name)].append((ts, side, shares, amount))

    findings = []
    for (biz_id, name), orders in by_business.items():
        orders.sort()
        start = 0
        best = None
        for end in range(len(orders)):
            while orders[end][0] - orders[start][0] > window:
                start += 1
            count = end - start + 1
            if count >= min_count and (best is None or count > best[0]):
                best = (count, start, end)
        if not best:
            continue

        count, lo, hi = best
        burst = orders[lo:hi + 1]
        minutes = max(1, (burst[-1][0] - burst[0][0]) // 60)
        total_shares = sum(o[2] for o in burst)
        total_amount = sum(o[3] for o in burst)
        sides = {o[1] for o in burst}
        direction = "buy" if sides == {"buy"} else "sell" if sides == {"sell"} else "buy and sell"

        findings.append({
            "kind": "cap-burst",
            "severity": HIGH if count >= min_count * 2 else MEDIUM,
            "title": "Repeatedly filled the maximum order size",
            "detail": (
                f"**{count}** maximum-size {direction} orders on {name} within **{minutes} min** "
                f"\u2014 **{total_shares:,} shares**, **${total_amount:,}**"
            ),
            "context": (
                "Splitting one huge order into back-to-back maximum ones is how the per-order "
                "size limit gets worked around."
            ),
            "ts": burst[-1][0],
            "business": name,
        })
    return findings


# --------------------------------------------------------------------------- #
# Flag 4: trading a stock the player owns.
#
# Not against the rules by itself — an owner holding their own stock is normal
# — but an owner selling into a rise they caused is the thing to watch, so it
# is surfaced quietly rather than as an accusation.
# --------------------------------------------------------------------------- #
def _find_self_dealing(chronological, businesses):
    owned_trades = defaultdict(lambda: {"buy": [0, 0], "sell": [0, 0], "name": ""})
    for _, biz_id, name, side, shares, amount, price, _, ts in chronological:
        info = businesses.get(biz_id) or {}
        if not info.get("is_own"):
            continue
        bucket = owned_trades[biz_id]
        bucket["name"] = name
        bucket[side][0] += shares
        bucket[side][1] += amount
        bucket["ts"] = max(bucket.get("ts", 0), ts)

    findings = []
    for biz_id, bucket in owned_trades.items():
        sold_shares, sold_amount = bucket["sell"]
        if not sold_shares:
            continue
        findings.append({
            "kind": "self-dealing",
            "severity": MEDIUM,
            "title": "Sold shares in their own business",
            "detail": (
                f"Sold **{sold_shares:,}** shares of **{bucket['name']}** \u2014 a business they "
                f"own \u2014 for **${sold_amount:,}**"
            ),
            "context": "Check this was not straight after they moved the price themselves.",
            "ts": bucket.get("ts", 0),
            "business": bucket["name"],
        })
    return findings


def _dedupe(findings):
    """Collapses identical flags raised by several events on the same trade."""
    seen = {}
    for finding in findings:
        key = (finding["kind"], finding["ts"], finding["business"])
        if key not in seen or finding["severity"] > seen[key]["severity"]:
            seen[key] = finding
    return list(seen.values())
