from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from eth_desk.types import FeeSchedule

# Polymarket rounds taker fees to 5 decimal places (0.00001 USDC).
FEE_QUANTUM = Decimal("0.00001")


class MissingFeeSchedule(ValueError):
    """The market says fees are on, but the schedule did not include a rate."""


def taker_fee(shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    """USDC taker fee for buying `shares` at `price`.

    The rate and exponent always come from the market's fee schedule.
    With exponent 1 this is the published formula `C × feeRate × p × (1 − p)`.
    The general form used here is `C × rate × (p × (1 − p)) ^ exponent`.

    Amounts below 0.00001 USDC are charged as zero, matching the fee docs.
    `rebate_rate` is not applied: this terminal takes liquidity, and the
    rebate on the schedule is the maker rebate, not a taker discount.
    """

    if shares <= 0 or price <= 0:
        return Decimal("0")
    if not schedule.fees_enabled:
        return Decimal("0")
    if schedule.rate < 0 or schedule.exponent < 0:
        raise MissingFeeSchedule("fee schedule rate and exponent must be non-negative")

    probability_term = price * (Decimal("1") - price)
    if probability_term < 0:
        probability_term = Decimal("0")
    raw = shares * schedule.rate * (probability_term ** schedule.exponent)
    if raw < FEE_QUANTUM:
        return Decimal("0")
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
