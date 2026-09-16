"""
Shared shorthand-amount parsing used by EVERY command that takes an amount.

Players can type any of these and the bot understands them identically:

    500        ->        500
    1,500      ->      1,500
    $2500      ->      2,500
    10k        ->     10,000
    20K        ->     20,000
    1.5k       ->      1,500
    2m         ->  2,000,000
    0.5b       -> 500,000,000
    all / max  -> the caller's whole available balance (where supported)
    half       -> half of the available balance (where supported)

Every parser returns a `(value, error)` pair instead of raising, so a cog can
do:

    amount, error = parse_amount(raw, available=cash)
    if error:
        await send_error(interaction, error)
        return

Keeping this in one module means a new command only has to call one helper to
get identical shorthand support and identical error wording.
"""

import math
from typing import Optional, Tuple

# Suffix multipliers. Case-insensitive.
SUFFIXES = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "t": 1_000_000_000_000,
}

# Words that mean "everything I have".
ALL_WORDS = {"all", "max", "everything", "full"}
HALF_WORDS = {"half", "1/2"}

# Shown in every error message so players learn the shorthand.
EXAMPLES = "`500`, `1500`, `10k`, `1.5k`, `2m`"


def _strip(raw: str) -> str:
    """Removes the decoration players habitually type around numbers."""
    text = (raw or "").strip().lower()
    for junk in (",", "_", " ", "$", "'"):
        text = text.replace(junk, "")
    return text


def to_number(raw: str) -> Optional[float]:
    """'10k' -> 10000.0. Returns None when the text isn't a number at all."""
    text = _strip(raw)
    if not text:
        return None

    multiplier = 1
    if text[-1] in SUFFIXES:
        multiplier = SUFFIXES[text[-1]]
        text = text[:-1]
        # "k" on its own, or "1.5kk", is not a number.
        if not text:
            return None

    # Reject "inf" / "nan" / "1e999", which float() would happily accept.
    if not all(c.isdigit() or c in ".-+" for c in text):
        return None

    try:
        value = float(text)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value * multiplier


def parse_amount(
    raw: str,
    *,
    minimum: int = 1,
    maximum: int = 1_000_000_000_000,
    available: Optional[int] = None,
    allow_all: bool = True,
    noun: str = "amount",
) -> Tuple[Optional[int], Optional[str]]:
    """Parses a money amount. Returns (value, None) or (None, error_message).

    `available` enables `all` / `half`; pass the balance the command is
    spending from. Fractions of a dollar are rounded down, so a player can
    never conjure a cent out of rounding.
    """
    text = _strip(raw)

    if available is not None and allow_all:
        if text in ALL_WORDS:
            value = int(available)
            if value < minimum:
                return None, f"You don't have enough for that — your balance is ${int(available):,}."
            return min(value, maximum), None
        if text in HALF_WORDS:
            value = int(available) // 2
            if value < minimum:
                return None, f"You don't have enough for that — your balance is ${int(available):,}."
            return min(value, maximum), None

    number = to_number(raw)
    if number is None:
        hint = f" You can also type `all`." if (available is not None and allow_all) else ""
        return None, f"`{raw}` isn't a valid {noun}. Try {EXAMPLES}.{hint}"

    # Floor, with a nudge so 1.5k never lands on 1499.9999999.
    value = math.floor(number + 1e-9)

    if value < minimum:
        return None, f"The smallest {noun} is ${minimum:,}."
    if value > maximum:
        return None, f"The largest {noun} is ${maximum:,}."
    return value, None


def parse_count(
    raw: str,
    *,
    minimum: int = 1,
    maximum: int = 1_000_000_000,
    available: Optional[int] = None,
    allow_all: bool = True,
    noun: str = "number",
) -> Tuple[Optional[int], Optional[str]]:
    """Same shorthand for whole-unit counts (shares, quantities). Errors don't
    carry a dollar sign."""
    text = _strip(raw)

    if available is not None and allow_all:
        if text in ALL_WORDS:
            value = int(available)
            if value < minimum:
                return None, f"You don't have enough — you have {int(available):,}."
            return min(value, maximum), None
        if text in HALF_WORDS:
            value = int(available) // 2
            if value < minimum:
                return None, f"You don't have enough — you have {int(available):,}."
            return min(value, maximum), None

    number = to_number(raw)
    if number is None:
        hint = " You can also type `all`." if (available is not None and allow_all) else ""
        return None, f"`{raw}` isn't a valid {noun}. Try {EXAMPLES}.{hint}"

    value = math.floor(number + 1e-9)
    if value < minimum:
        return None, f"The smallest {noun} is {minimum:,}."
    if value > maximum:
        return None, f"The largest {noun} is {maximum:,}."
    return value, None


def parse_price(
    raw: str,
    *,
    minimum: float = 0.01,
    maximum: float = 1_000_000_000.0,
    noun: str = "price",
) -> Tuple[Optional[float], Optional[str]]:
    """Shorthand for values that keep their cents, e.g. a share price.
    `1.5k` -> 1500.00, `12.50` -> 12.50."""
    number = to_number(raw)
    if number is None:
        return None, f"`{raw}` isn't a valid {noun}. Try {EXAMPLES} or `12.50`."
    value = round(number, 2)
    if value < minimum:
        return None, f"The smallest {noun} is ${minimum:,.2f}."
    if value > maximum:
        return None, f"The largest {noun} is ${maximum:,.2f}."
    return value, None


def parse_percent(
    raw: str,
    *,
    minimum: float = -100.0,
    maximum: float = 1000.0,
    noun: str = "percentage",
) -> Tuple[Optional[float], Optional[str]]:
    """Accepts `15`, `+15`, `-20`, `15%`."""
    number = to_number((raw or "").replace("%", ""))
    if number is None:
        return None, f"`{raw}` isn't a valid {noun}. Try `15`, `-20` or `7.5`."
    value = round(number, 2)
    if value < minimum or value > maximum:
        return None, f"The {noun} must be between {minimum:g} and {maximum:g}."
    return value, None
