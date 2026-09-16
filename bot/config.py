"""
Central configuration for the BeamNG RP Economy Bot.
Tweak values here without touching command logic.
"""

import os


def loan_request_channel_id():
    """The channel every new loan request is posted to for staff to review.

    Set LOAN_REQUEST_CHANNEL_ID in .env (or in your host's environment
    variables). Returns None when it isn't set or isn't a channel ID, in which
    case requests simply aren't announced and staff use /loanrequests instead.

    Read on every call rather than once at import, because config.py is
    imported before .env is loaded.
    """
    raw = os.getenv("LOAN_REQUEST_CHANNEL_ID", "").strip()
    return int(raw) if raw.isdigit() else None

# ---------------------------------------------------------------------------
# General economy settings
# ---------------------------------------------------------------------------
STARTING_CASH = 0
STARTING_BANK = 15000

# ---------------------------------------------------------------------------
# /daily reward — every value below is adjustable.
# ---------------------------------------------------------------------------
DAILY_ENABLED = True             # set False to switch /daily off entirely
DAILY_REWARD_MIN = 4000           # smallest possible base payout
DAILY_REWARD_MAX = 10000           # largest possible base payout
DAILY_COOLDOWN_HOURS = 48        # how long between claims
DAILY_STREAK_BONUS = 500          # extra cash per consecutive day claimed
DAILY_STREAK_MAX_DAYS = 7        # streak bonus stops growing after this many days
DAILY_STREAK_GRACE_HOURS = 24    # wait longer than this between claims and the streak resets
DAILY_PAY_TO_BANK = False        # True pays into the bank instead of cash

WORK_COOLDOWN_MINUTES = 60
WORK_MIN = 1000
WORK_MAX = 3000

LOAN_INTEREST_RATE = 0.1        # interest on the SHORTEST possible loan (1 day)
LOAN_MAX_MULTIPLIER = 5          # max loan = bank balance * this multiplier (scaled by credit score)
LOAN_MIN_AMOUNT = 200
LOAN_MIN_CREDIT_SCORE = 400     # players below this score are refused new loans

# --- Loan terms -------------------------------------------------------------
# A borrower picks how long they want to take to pay a loan back, and the bank
# charges for the privilege: the longer the term, the steeper the interest.
LOAN_TERM_MIN_DAYS = 1
LOAN_TERM_MAX_DAYS = 21          # hard ceiling — three weeks, and no longer
LOAN_TERM_DEFAULT_DAYS = 7

# Interest at the maximum term. The rate runs from LOAN_INTEREST_RATE (shortest
# term) up to this (the full three weeks), so a player who wants three weeks to
# pay pays 60% interest instead of 10%.
LOAN_MAX_TERM_INTEREST_RATE = 0.60

# How the rate climbs between those two. Above 1.0 the curve is cheap early and
# punishing at the end, so a couple of extra days costs little but stretching a
# loan to the limit is a serious decision. 1 = a straight line.
LOAN_TERM_RATE_CURVE = 2.0

# Post a reminder to the loan channel when loans fall past their due date, so
# staff can chase them in character. Nothing is taken automatically.
LOAN_OVERDUE_ALERTS = True
LOAN_OVERDUE_ALERT_HOURS = 24    # how often that reminder may be posted

# Credit consequences of settling a loan late (the on-time bonus is
# CREDIT_SCORE_LOAN_PAYOFF_BONUS, further down).
LOAN_LATE_PAYOFF_PENALTY = 25

# Loans are no longer self-service. /loanrequest only files a request; a
# staff member has to run /approveloan or /denyloan. Anyone with the
# Administrator permission, Discord's built-in "Moderate Members" permission,
# or "Manage Server" permission always qualifies. You can also list extra
# role names below (e.g. a "Moderator" or "Bank Manager" role) that should be
# allowed to approve/deny loans without needing those permissions.
LOAN_APPROVER_ROLE_NAMES = ["Moderator"]

# Vehicle insurance (/insure, /claim) was removed — it was an unlimited money
# printer (flat premium, payout based on the full purchase price).

# ---------------------------------------------------------------------------
# Police / legal command access
# ---------------------------------------------------------------------------
# Role IDs allowed to use /fine, /wanted and /arrest. Matched by role ID, NOT
# by role name: anyone who can create a role could previously name it "Police"
# and grant themselves the power to fine players up to $100,000.
#
# To fill this in: enable Developer Mode in Discord (User Settings -> Advanced
# -> Developer Mode), then right-click each police role in Server Settings ->
# Roles and choose "Copy Role ID". List every qualifying role ID below.
#
# IMPORTANT: while this list is empty, only members with Discord's
# Administrator permission can use the police commands.
POLICE_ROLE_IDS = [
    1546222191643988038,
    1519314283459117186,
    1508355515963408454,
    1504231581584195624,
    1531265093130784910
]

MECHANIC_ROLE_NAME = "Mechanic"

# ---------------------------------------------------------------------------
# Job roles (Police / EMS / DOT only)
# ---------------------------------------------------------------------------
# Jobs can no longer be self-applied with a command — /jobapply has been
# removed. A player's job is now detected automatically from the Discord
# roles they hold, matched by role ID rather than role name (IDs can't be
# renamed or confused with another role, and several roles can map to the
# same job, e.g. "Police Cadet" and "Police Officer" both counting as "Police").
#
# To fill this in: enable Developer Mode in Discord (User Settings ->
# Advanced -> Developer Mode), then right-click each qualifying role in
# Server Settings -> Roles and choose "Copy Role ID". Paste every ID that
# should count for that job into the matching list below. You can list as
# many role IDs per job as you want.
JOB_ROLE_IDS = {
    "Police": [
        1546222191643988038,
        1519314283459117186,
        1508355515963408454,
        1504231581584195624,
        1531265093130784910
    ],
    "EMS": [
        1504231817782497341,
    ],
    "DOT": [
        1504232284109279444,
    ],
}

# Branding used in the financial-profile embed (title, bank field name).
ECONOMY_BRAND_NAME = "BeamState"

# ---------------------------------------------------------------------------
# Savings & credit
# ---------------------------------------------------------------------------
STARTING_CREDIT_SCORE = 650
MIN_CREDIT_SCORE = 300
MAX_CREDIT_SCORE = 850
CREDIT_SCORE_LOAN_PAYOFF_BONUS = 15   # applied when a loan is fully paid off
CREDIT_SCORE_FINE_PENALTY = 5         # applied each time police issue a fine

SAVINGS_INTEREST_RATE = 0.05    # applied per interest tick
SAVINGS_TICK_HOURS = 24          # how often savings interest is paid out

# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
# A player's job is detected from the Discord roles they hold (see
# JOB_ROLE_IDS above). /clockin and /clockout were removed, so hourly_pay is
# now purely informational — jobs no longer pay out automatically.
JOBS = {
    "Police": {"hourly_pay": 300, "description": "Patrol, issue fines, respond to incidents."},
    "EMS": {"hourly_pay": 280, "description": "Respond to crashes and medical calls."},
    "DOT": {"hourly_pay": 240, "description": "Maintain roads and manage traffic infrastructure."},
}

# ---------------------------------------------------------------------------
# Vehicles
# Staff register a vehicle in a player's garage with /eco-admin givevehicle.
# Categories are intentionally broad so they fit in one Discord choice list
# (25 max).
# ---------------------------------------------------------------------------
VEHICLE_CATEGORIES = [
    "Compact", "Sedan", "Sports", "SUV", "Truck",
    "Van/Bus", "Classic", "Luxury", "Off-Road", "Supercar",
]

# NOTE: /sellvehicle was removed, so this is currently unused. Left in place
# in case a staff-run buyback command is added later.
SELLBACK_DEPRECIATION = 0.55

# ---------------------------------------------------------------------------
# Stock market
# ---------------------------------------------------------------------------
STOCK_MIN_PRICE = 0.50            # price floor, prevents a stock hitting $0
STOCK_TICK_MINUTES = 60           # how often the background market tick runs
STOCK_MAX_RANDOM_DRIFT = 0.04     # max +/- % random drift per tick (0.04 = 4%)
STOCK_TRADE_IMPACT_FACTOR = 0.6   # how strongly buy/sell volume moves the price
MAX_BUSINESS_NAME_LENGTH = 40
DEFAULT_TOTAL_SHARES = 10_000     # suggested default if an admin doesn't specify

# Anti-manipulation cap: the largest single /stock buy or sell order allowed,
# expressed as a fraction of a business's total shares. Stops one player from
# dumping or pumping a stock in a single order; they can still trade the rest
# across multiple orders (each of which will move the price on its own).
STOCK_MAX_TRADE_PCT_OF_SHARES = 0.10

# Cooldown between /stock sell orders, per player, per server. Stops rapid-fire
# dumping and any remaining quick round-trip churn. Set to 0 to disable.
STOCK_SELL_COOLDOWN_MINUTES = 30

# Cooldown between /stock buy orders, per player, per server. The per-order
# cap above limits ONE order; without this a player could fire a dozen
# legal-sized orders in a few seconds and build a huge position before the
# price had moved to reflect it. Deliberately lighter than the sell cooldown —
# buying is what makes the market work. Set to 0 to disable.
STOCK_BUY_COOLDOWN_MINUTES = 15

# Rolling per-player, per-stock volume cap on BUYS: the most shares of one
# business a single player may buy inside any STOCK_BUY_VOLUME_WINDOW_HOURS
# window, as a fraction of that business's total shares. Set to 0 to disable.
STOCK_BUY_DAILY_VOLUME_PCT_OF_SHARES = 0.25
STOCK_BUY_VOLUME_WINDOW_HOURS = 24

# Circuit breaker: if a listing's price has moved more than this (0.25 = 25%)
# away from where it opened the current tick window — from trades, revenue
# events, or both together — /stock buy and /stock sell are refused on it for
# the rest of that window. Trading reopens automatically at the next market
# tick. Set to 0 to disable.
STOCK_CIRCUIT_BREAKER_PCT = 0.50

# Insider lock-out: after a revenue-driven price event on a listing
# (/paycompany or /eco-admin company addrevenue moving its price), the people
# best placed to have engineered it — the listing's owner, the linked
# company's owner and withdraw-capable staff, and anyone holding at least
# STOCK_INSIDER_HOLDER_THRESHOLD of the total shares — may not SELL that stock
# for this many minutes. Set the minutes to 0 to disable. Keep it longer than
# STOCK_TICK_MINUTES, because the tick re-prices the same revenue.
STOCK_INSIDER_SELL_LOCKOUT_MINUTES = 60
STOCK_INSIDER_HOLDER_THRESHOLD = 0.05

# When an admin lists a business with an owner attached, that owner
# automatically receives this fraction of the total shares for free as a
# "founder" stake (the rest goes onto the open market for players to buy).
# Set to 0 to disable and have every share start on the open market instead.
# An admin can override this per-business with the founder_percent option on
# /eco-admin business create.
BUSINESS_FOUNDER_SHARE_PERCENT = 0

# ---------------------------------------------------------------------------
# Revenue-driven stock prices
# ---------------------------------------------------------------------------
# A stock market listing can be tied to a real registered business (the ones
# made with `/eco-admin company create`). When it is, the listing stops being a
# pure random walk: every market tick the price moves on how much revenue that
# business actually earned since the last tick.
#
# How a listing is matched to a business:
#   1. An explicit link made with `/eco-admin business link` (preferred), or
#   2. failing that, an exact (case-insensitive) name match.
# A listing with no match keeps the old random-drift behaviour.
STOCK_REVENUE_LINK_ENABLED = True

# What counts as a "par" performance for one tick, as a fraction of the
# business's market cap (price x total shares). Earn exactly this much revenue
# in a tick and the price holds steady; earn more and it rises, earn less and it
# falls. 0.02 = the business is expected to turn over 2% of its own market cap
# every STOCK_TICK_MINUTES.
#
# This is what keeps the market honest: as a price rises, so does the market
# cap, so the same revenue becomes an underperformance and the price comes back
# down. A business can only hold a high valuation by continuing to earn.
STOCK_REVENUE_EXPECTED_YIELD = 0.02

# How hard a revenue beat/miss pushes the price. The move is
# STOCK_REVENUE_IMPACT x (actual - expected) / expected, so with 0.10 a business
# that earned nothing at all drops 10%, and one that doubled expectations
# gains 10%.
STOCK_REVENUE_IMPACT = 0.10

# Hard caps on how far the revenue component alone may move a price in one
# tick, up and down (0.25 = 25%). Random drift is applied on top of this.
STOCK_REVENUE_MAX_GAIN = 0.25
STOCK_REVENUE_MAX_DROP = 0.15

# Revenue moves are written to the stock history, so players can see *why* a
# stock moved in `/stockinfo`. Moves smaller than this (0.005 = 0.5%) are
# skipped to keep the history readable.
STOCK_REVENUE_EVENT_MIN_CHANGE = 0.005

# Instant reaction: when a business is paid (`/paycompany`) or an admin logs
# income (`/eco-admin company addrevenue`), nudge its stock immediately instead
# of making players wait for the next tick. Small on purpose — the tick above
# is the real pricing mechanism, this is just the market reacting to the news.
STOCK_REVENUE_INSTANT_ENABLED = True
STOCK_REVENUE_INSTANT_FACTOR = 0.5   # x (payment / market cap)
STOCK_REVENUE_INSTANT_MAX = 0.02     # never more than +2% from one payment

# ...and never more than this in TOTAL from all instant reactions inside one
# tick window (0.05 = 5%). The per-payment cap alone could be stacked by paying
# the same business ten times in a row; this is the budget those payments
# share. Refills when the market tick runs. Must be >= STOCK_REVENUE_INSTANT_MAX.
STOCK_REVENUE_INSTANT_PERIOD_MAX = 0.05

# Cooldown between /paycompany payments from the same player to the same
# business. A customer paying once for a service is normal; the same account
# paying the same business every few seconds is either a macro or a pump.
# Set to 0 to disable.
PAYCOMPANY_COOLDOWN_MINUTES = 10

# ---------------------------------------------------------------------------
# Channel restrictions
# ---------------------------------------------------------------------------
# Keep the noisy command families in the channels you've set aside for them.
#
# The easiest way to set these is in Discord itself, with
# `/eco-admin channels add`, `/eco-admin channels remove` and
# `/eco-admin channels show` — those are stored in the database, survive a
# redeploy, and override the two lists below.
#
# The lists here are the fallback for servers that prefer to hard-code it.
# Leave a list empty and that area is unrestricted.
#
# To fill these in: enable Developer Mode in Discord (User Settings ->
# Advanced -> Developer Mode), right-click the channel and "Copy Channel ID".

# Channels where /market, /topmovers, /stockinfo, /portfolio, /stock buy and
# /stock sell may be used.
STOCK_CHANNEL_IDS = [
    # 1234567890123456789,   # #stock-market
]

# Channels where the public business information commands (/company list and
# /company info) may be used. Can be the same channel as the stock one.
BUSINESS_INFO_CHANNEL_IDS = [
    # 1234567890123456789,   # #businesses
]

# When True, economy staff (see ECO_ADMIN_ROLE_IDS below) can use those
# commands in any channel, so they can test something or answer a question
# without having to move. Set to False to apply the restriction to everyone.
CHANNEL_LOCK_STAFF_BYPASS = True

# ---------------------------------------------------------------------------
# Whitelist: who may use the bot at all
# ---------------------------------------------------------------------------
# When WHITELIST_ENABLED is True, only members with one of the roles below can
# run ANY of the bot's commands, and only they get an economy account. Everyone
# else gets a short private reply and no balance is ever created for them.
#
# The easiest way to manage this is in Discord with:
#     /eco-admin whitelist add role:@Whitelisted
# That list is stored per server and survives restarts and redeploys. The list
# below is only a fallback for servers that would rather hard-code it, and is
# ignored as soon as a role has been added in Discord.
#
# IMPORTANT: while WHITELIST_ENABLED is True and no role has been set either
# here or in Discord, nobody but economy staff and Administrators can use the
# bot. Set at least one role before turning this on.
WHITELIST_ENABLED = True

WHITELIST_ROLE_IDS = [
    # 1234567890123456789,   # @Whitelisted
]

# Economy staff (ECO_ADMIN_ROLE_IDS) and Administrators always bypass the
# whitelist, so you can never lock yourself out. Set to False to make the
# whitelist apply to absolutely everyone except the server owner.
WHITELIST_STAFF_BYPASS = True

# The private message a non-whitelisted member gets. Keep it friendly — this is
# usually someone who simply hasn't been given the role yet.
WHITELIST_MESSAGE = (
    "🔒 You need to be whitelisted to use the economy bot.\n"
    "get the whitelist role, then try again."
)

# ---------------------------------------------------------------------------
# Full economy reset (/eco-admin reset-economy)
# ---------------------------------------------------------------------------
# /eco-admin reset-economy erases EVERYTHING for the server — balances, banks,
# savings, credit scores, vehicles, fines, loans, transaction history, the
# treasury, every stock market listing and shareholding, and every registered
# company with its staff and ledger. It cannot be undone.
#
# Because of that it is deliberately NOT available to everyone with an economy
# staff role. The server owner can always run it; list the user IDs of any
# co-owners who should also be able to below. An Administrator who is not
# listed here cannot run it.
#
# To fill this in: enable Developer Mode, right-click the person and choose
# "Copy User ID".
ECONOMY_RESET_USER_IDS = [
    # 1234567890123456789,   # co-owner
]

# The phrase the person has to type into the command's `confirm` option before
# the confirmation button even appears. Case-sensitive.
ECONOMY_RESET_CONFIRM_PHRASE = "RESET EVERYTHING"

# When True, the bot uploads a database backup to your BACKUP_CHANNEL_ID before
# wiping anything, so a reset run by mistake can still be undone by restoring
# that file. Only works if a backup channel is configured.
ECONOMY_RESET_BACKUP_FIRST = True

# ---------------------------------------------------------------------------
# Economy staff roles (/eco-admin access)
# ---------------------------------------------------------------------------
# Role IDs allowed to use the /eco-admin command group (including
# /eco-admin company ...). Matched by role ID so a rename can't grant or lose
# access. To fill this in: enable Developer Mode in Discord (User Settings ->
# Advanced -> Developer Mode), then right-click each staff role in
# Server Settings -> Roles and choose "Copy Role ID".
#
# While this list is empty, only members with Discord's Administrator
# permission can run /eco-admin commands.
ECO_ADMIN_ROLE_IDS = [
    1487852544355995884,
    1511023760864579726,
    1519341406655746199,
    1519341146185400412,
    1520049460753858721,
    1528388626713411715,
    1519341075997786203,
    1500800998661165107,
    1519340988101951498,
    1545453626875711498,
    1528715325363720302,   # testing server
]

# When True, members with the Administrator permission always keep /eco-admin
# access even if they hold none of the roles above. Set to False to make the
# role list the only way in (the server owner is always allowed, so you can
# never lock yourself out).
ECO_ADMIN_ALLOW_ADMINISTRATOR = True

# ---------------------------------------------------------------------------
# Real businesses (companies) — separate from the stock market
# ---------------------------------------------------------------------------
# A company pays this fraction of its all-time revenue in tax. E.g. a company
# that has earned 100,000 total owes 20,000 at the default 0.20 (20%).
# Tax already paid counts against the bill, so the owner is only ever charged
# the outstanding difference via /tax pay.
BUSINESS_TAX_RATE = 0.20

# Owners (and any staff position listed in COMPANY_DEPOSIT_POSITIONS below) can
# put their own personal money INTO the business account with /company deposit.
# A deposit is the owner's own already-taxed money, so it is deliberately NOT
# counted as revenue: it is never taxed, and it never moves the company's stock
# price. Set this to False to switch deposits off entirely.
COMPANY_DEPOSITS_ENABLED = True

# Smallest / largest single deposit.
COMPANY_DEPOSIT_MIN = 1
COMPANY_DEPOSIT_MAX = 1_000_000_000

# Public business information (/company list and /company info) — any player
# can look up any business, in the channel(s) set for the "business" area (see
# BUSINESS_INFO_CHANNEL_IDS above).
#
# Set this to False to keep account balances and owner deposits private to the
# owner, their staff and economy staff. All-time revenue, the owner, the staff
# roster and the tax status stay public either way.
COMPANY_PUBLIC_INFO_SHOW_BALANCE = True

# Whether /company info shows a business's last few ledger entries to everyone.
COMPANY_PUBLIC_INFO_SHOW_LEDGER = True

# ---------------------------------------------------------------------------
# Company staff (owner, COO, managers, employees)
# ---------------------------------------------------------------------------
# Job titles a company owner can hand out, ordered MOST senior first. The order
# is what gives a title its authority: a staff member can only hire, promote,
# demote or fire people into positions strictly BELOW their own. Rename, add or
# remove titles freely — just keep them in seniority order.
#
# Discord allows at most 25 choices in a dropdown, so keep this list to 25.
COMPANY_POSITIONS = [
    "COO",
    "CFO",
    "Senior Manager",
    "Manager",
    "Supervisor",
    "Employee",
]

# Maximum number of employees one company can have on its books.
COMPANY_MAX_EMPLOYEES = 25

# Which positions may pay money INTO the business account (/company deposit).
# The owner always can.
COMPANY_DEPOSIT_POSITIONS = ["COO", "CFO", "Senior Manager", "Manager", "Supervisor", "Employee"]

# Which positions may take money OUT of the business account
# (/company withdraw). Keep this tight — anyone listed here can move company
# money into their own pocket.
COMPANY_WITHDRAW_POSITIONS = ["COO", "CFO"]

# Which positions may settle the business tax bill (/tax pay).
COMPANY_TAX_POSITIONS = ["COO", "CFO"]

# Which positions may hire, promote, demote and fire other staff — always only
# into positions below their own rank.
COMPANY_MANAGE_STAFF_POSITIONS = ["COO", "Senior Manager"]

# When True, a company owner can hand their business to another player
# themselves with /company transfer. When False, only economy staff can do it
# with /eco-admin company setowner.
COMPANY_OWNER_CAN_TRANSFER = True

# ---------------------------------------------------------------------------
# UnbelievaBoat migration
# ---------------------------------------------------------------------------
UNB_API_BASE = "https://unbelievaboat.com/api/v1"
UNB_PAGE_SIZE = 1000              # max users fetched per API request
UNB_REQUEST_DELAY_SECONDS = 1.0   # polite delay between paginated requests

# ---------------------------------------------------------------------------
# Hosting / automatic backups
#
# Free hosts usually wipe the container's filesystem on redeploy, which would
# take economy.db with it. Two independent defences, both optional:
#
#   1. DATABASE_PATH (set in .env or your host's environment variables) points
#      the database at a persistent disk, e.g. DATABASE_PATH=/data/economy.db
#   2. The settings below upload a snapshot of the database to a private
#      Discord channel on a timer. Set BACKUP_CHANNEL_ID in .env to switch it
#      on; staff can also run /backup-now at any time.
# ---------------------------------------------------------------------------
BACKUP_ENABLED = True            # False turns scheduled backups off entirely
BACKUP_INTERVAL_HOURS = 6        # how often a snapshot is uploaded

# ---------------------------------------------------------------------------
# Bot-update safety (cogs/persistence.py)
#
# SQLite in WAL mode writes to economy.db-wal first and only folds it into
# economy.db at a checkpoint. Copying economy.db off the host in between — the
# normal way of carrying the economy across a bot update — therefore loses
# every write still in the WAL: usually just the newest ones, which is why it
# showed up as one or two players' balances reverting and recent stock
# portfolios disappearing rather than an obvious wipe.
#
# Checkpointing this often keeps economy.db on disk within a few seconds of
# reality, so it is always safe to copy. Lower = safer, at the cost of a little
# more disk I/O. There is no reason to raise it much above a minute.
# ---------------------------------------------------------------------------
CHECKPOINT_INTERVAL_SECONDS = 30

# ---------------------------------------------------------------------------
# Economy reconciliation audit (/eco-admin audit, and on a timer)
#
# Two independent checks, posted to the audit log channel when they find
# something:
#
#   1. MONEY SUPPLY DRIFT. All money in the server (cash + bank + savings +
#      business accounts + treasury) is compared with what the transaction
#      log says it should be. Every command that CREATES or DESTROYS money
#      logs a transaction of one of the types in AUDIT_SUPPLY_CHANGING_TYPES;
#      transfers between two pockets (bank deposits, /pay, /paycompany, fines
#      into the treasury...) do not change the total and are ignored. If the
#      total moved by more than the log explains, money appeared or vanished
#      without a record — the signature of a dupe or a lost-update bug.
#
#   2. SUSPICIOUS PATTERNS: a player repeatedly filling orders at the
#      per-order cap, and a business paid by several low-activity or brand-new
#      accounts shortly before its owner sells a large stock position.
# ---------------------------------------------------------------------------
AUDIT_RECONCILE_ENABLED = True
AUDIT_RECONCILE_INTERVAL_HOURS = 6

# Transaction types whose amount adds to / removes from the total money supply.
# Anything NOT listed here is treated as a neutral transfer. If you add a
# command that mints or burns money, log it with a type and add it here.
AUDIT_SUPPLY_CHANGING_TYPES = {
    "daily", "work", "savings_interest", "admin_give", "admin_take",
    "loan_issued", "loan_payment", "stock_buy", "stock_sell",
    "stock_delist_payout", "company_closure_payout", "unb_migration",
}
# Money in the supply that has no transaction behind it and is expected:
# every new account is minted STARTING_CASH + STARTING_BANK. Drift smaller
# than this (in dollars) is reported as noise, not flagged.
AUDIT_DRIFT_TOLERANCE = 0

# Pattern 1: this many orders AT the STOCK_MAX_TRADE_PCT_OF_SHARES cap, on one
# stock, by one player, inside this many minutes.
AUDIT_CAP_TRADE_COUNT = 3
AUDIT_CAP_TRADE_WINDOW_MINUTES = 90

# Pattern 2: a company paid by at least AUDIT_SUSPICIOUS_PAYMENT_COUNT distinct
# "thin" accounts (fewer than AUDIT_LOW_ACTIVITY_TX_COUNT transactions ever, or
# first seen less than AUDIT_NEW_ACCOUNT_AGE_HOURS ago) in the
# AUDIT_PAYMENT_TO_SELL_WINDOW_HOURS before an insider of the linked listing
# sold at least AUDIT_LARGE_SELL_PCT of its total shares.
AUDIT_SUSPICIOUS_PAYMENT_COUNT = 2
AUDIT_LOW_ACTIVITY_TX_COUNT = 5
AUDIT_NEW_ACCOUNT_AGE_HOURS = 72
AUDIT_PAYMENT_TO_SELL_WINDOW_HOURS = 24
AUDIT_LARGE_SELL_PCT = 0.03

# How far back each scheduled audit looks for the two patterns above.
AUDIT_LOOKBACK_HOURS = 24

# ---------------------------------------------------------------------------
# /stock history <player> — the per-player exploit review (stockhistory.py)
#
# The reconciliation audit above sweeps the whole server. These settings tune
# the report staff pull up on ONE player.
# ---------------------------------------------------------------------------
# A trade counts as suspiciously timed if a price move of at least
# HISTORY_EVENT_MIN_MOVE lands within HISTORY_EVENT_WINDOW_MINUTES of it, in
# the direction that made the player money. Tighten the window to cut noise on
# a busy market; widen it to catch slower insiders.
HISTORY_EVENT_WINDOW_MINUTES = 60
HISTORY_EVENT_MIN_MOVE = 0.05        # 0.05 = a 5% move

# How much the player must actually have gained (or losses dodged) before a
# well-timed trade is reported at all. Without a floor the report flags $300 of
# good luck as loudly as a coordinated pump and staff stop trusting it. A
# repeated pattern — three or more orders around one event — is always shown
# regardless of size, because the repetition is the tell.
HISTORY_EVENT_MIN_BENEFIT = 2_500
# Gains at or above this, within 15 minutes of the move, are called a likely
# exploit outright.
HISTORY_EVENT_SERIOUS_BENEFIT = 25_000

# A "fast flip" is buying and selling the same stock inside
# HISTORY_FLIP_MINUTES for a gain of at least HISTORY_FLIP_MIN_GAIN.
HISTORY_FLIP_MINUTES = 60
HISTORY_FLIP_MIN_GAIN = 0.25         # 0.25 = sold for 25% more than it cost

# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------
# The bot can serve a JSON API that a website (Base44 or anything else) uses to
# show the market and place orders, so players trade in a browser instead of
# typing /market and /stock buy in Discord.
#
# The API is served by the keep-alive web server the bot already runs, on the
# same URL your uptime pinger hits:
#
#     https://<your-bot-host>/api/v1/market
#
# Orders placed on the website run through the SAME code as the slash commands,
# so every cooldown, cap, circuit breaker and insider rule applies identically.
# Nothing is bypassed by going through a browser.
#
# Read WEBSITE.md for the full setup, and BASE44_PROMPT.md for the text to
# paste into Base44.

# Master switch. The API also needs WEB_API_KEY below before it will start.
WEB_API_ENABLED = True

# The shared secret your website sends in the X-API-Key header on every call.
#
# SET THIS AS AN ENVIRONMENT VARIABLE (WEB_API_KEY) IN YOUR HOST'S PANEL, not
# here — the same place DISCORD_TOKEN lives. Anything written in this file ends
# up in your repository. Generate one with:
#
#     python -c "import secrets; print(secrets.token_urlsafe(32))"
#
# Leave it empty here and the environment variable wins.
WEB_API_KEY = ""

# Where the dashboard lives. Shown by /dashboard and /weblogin. Once Base44
# gives you the published URL, put it here (or in the WEB_DASHBOARD_URL
# environment variable).
WEB_DASHBOARD_URL = ""

# Which browser origins may call the API directly. Keep "*" while you are
# building; once the site is published, narrow it to your own domain, e.g.
# "https://yourgame.base44.app". Only matters if your site calls the API from
# the browser — the recommended setup calls it from a Base44 backend function,
# where CORS never applies.
WEB_API_CORS_ORIGINS = "*"

# How long a website sign-in lasts before the player has to run /weblogin again.
WEB_SESSION_DAYS = 30

# Requests per minute, per signed-in player. Reads cover the market and
# portfolio pages; trades cover buy and sell. These are generous for a human
# clicking buttons and tight enough to stop a script hammering the host.
WEB_API_READ_RATE_LIMIT = 120
WEB_API_TRADE_RATE_LIMIT = 20

# How often prices are sampled for the dashboard's charts, and how long the
# samples are kept. One listing costs roughly 30 KB a month at these settings.
WEB_PRICE_SAMPLE_SECONDS = 60
WEB_PRICE_HISTORY_DAYS = 14

# Set to False once the dashboard is live to retire the Discord trading
# commands. /market, /topmovers, /stockinfo, /portfolio, /stock buy and
# /stock sell then reply with a link to the website instead of running.
#
# /stock history (the staff exploit check) keeps working either way, so staff
# never lose their tooling.
STOCK_DISCORD_COMMANDS_ENABLED = True
