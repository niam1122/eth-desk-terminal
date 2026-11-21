"""Fair probability that a 15-minute window settles Up.

These markets resolve Up when the Chainlink ETH/USD 60-second TWAP at the
window end is greater than or equal to the same TWAP at the window start.
A tie is Up. The published TWAP matches the plain average of the 60
consecutive 1-second Chainlink prices (RTDS topic `crypto_prices_chainlink`,
symbol `eth/usd`) on either of two alignments that both fit the boundaries
to 1e-12: the seconds ending 62s through 3s before the boundary, and those
ending 61s through 2s before it. The alignment jitters by a second, so the
model blends them. The two endpoints of the blend carry half weight
(`0.5/60`); every interior second carries `1/60`.

`TwapSettlementModel` is the default. Seconds of that window that have not
printed yet are driftless Brownian motion with the covariance
`min(tau_i, tau_j)`. Seconds that have printed are plugged in at their
price. With `m` and `s` the mean and standard deviation of the closing
TWAP, P(Up) = Φ((m − O) / s). Far from expiry, before any of those seconds
have printed, this is close to Φ(ln(S/O) / (σ √(T − 40s))).

`ZeroDriftLognormalModel` is the plain spot-close formula. It stays
available for comparison and for anything that still wants it.

The engine only calls `probability_up`. Pass either model in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol

from eth_desk.types import PricingInput

# Blended settlement window, in seconds before the boundary.
N_TWAP = 60
FIRST_OFFSET = 62
LAST_OFFSET = 2
# Variance of a uniform average over a 60-second lookback is T − 2/3·60
# when the lookback ends at the boundary. The closed form uses that.
FAR_EXPIRY_LOOKBACK_SECONDS = 40


class FairValueModel(Protocol):
    name: str

    def probability_up(self, data: PricingInput) -> Decimal:
        """Return P(settlement price >= the price to beat), between 0 and 1."""


def _unix_seconds(timestamp: int) -> int:
    value = int(timestamp)
    if value >= 1_000_000_000_000:
        return value // 1000
    return value


def settlement_seconds(window_end_unix: int) -> list[int]:
    """Unix seconds in the blended settlement window, oldest first."""

    end = _unix_seconds(window_end_unix)
    return list(range(end - FIRST_OFFSET, end - LAST_OFFSET + 1))


def settlement_weights(window_end_unix: int) -> dict[int, float]:
    """Half weight on the two endpoints, full weight on the seconds in both alignments."""

    seconds = settlement_seconds(window_end_unix)
    unit = 1.0 / N_TWAP
    weights = {second: unit for second in seconds}
    weights[seconds[0]] = 0.5 * unit
    weights[seconds[-1]] = 0.5 * unit
    return weights


def settlement_twap(
    prices: Mapping[int, float],
    window_end_unix: int,
    max_missing: int = 5,
) -> float | None:
    """Blended settlement TWAP. Gaps are forward-filled from the previous second.

    The second before the window (`end − 63`) may be present in `prices` and
    is used only as the anchor for a gap at the first second. Returns None
    when the anchor is missing as well, or when more than `max_missing`
    seconds are absent. Chainlink's own handling of a skipped second is not
    published; forward-fill is the reconstruction used here.
    """

    weights = settlement_weights(window_end_unix)
    seconds = sorted(weights)
    last = _price_at(prices, seconds[0] - 1)
    total = 0.0
    missing = 0
    for second in seconds:
        price = _price_at(prices, second)
        if price is None:
            missing += 1
            if last is None or missing > max_missing:
                return None
            price = last
        else:
            last = price
        total += weights[second] * price
    return total


def observed_settlement_seconds(
    points: Sequence[tuple[int, Decimal]],
    window_end_unix: int,
) -> tuple[tuple[int, Decimal], ...]:
    """1-second prints the TWAP model can treat as already observed.

    Keeps the blended window `[end−62, end−2]` and the anchor second
    `end−63`. Timestamps are unix seconds, or unix milliseconds when the
    value is at least 10**12. A second printed more than once keeps the
    last point in `points`.
    """

    end = _unix_seconds(window_end_unix)
    wanted = set(settlement_seconds(end))
    wanted.add(end - FIRST_OFFSET - 1)
    latest: dict[int, Decimal] = {}
    for timestamp, price in points:
        second = _unix_seconds(timestamp)
        if second in wanted:
            latest[second] = price
    return tuple(sorted(latest.items()))


def ewma_sigma_per_sqrt_second(
    log_returns: Sequence[float],
    dt_seconds: float,
    halflife_observations: float = 60.0,
) -> float:
    """Per-square-root-second volatility from an EWMA of squared log returns.

    `halflife_observations` is counted in samples of `log_returns`. The
    default, 60, is a 1-hour half-life when `log_returns` are 1-minute
    returns and `dt_seconds` is 60. The newest return has weight 1. The
    oldest has weight `0.5 ** ((n−1) / halflife)`.
    """

    if dt_seconds <= 0 or halflife_observations <= 0:
        raise ValueError("dt and half-life must be positive")
    returns = [float(value) for value in log_returns if value == value]
    if not returns:
        raise ValueError("no returns")
    decay = 0.5 ** (1.0 / float(halflife_observations))
    weight = decay ** (len(returns) - 1)
    weighted_square = 0.0
    weight_sum = 0.0
    for value in returns:
        weighted_square += weight * value * value
        weight_sum += weight
        weight /= decay
    return math.sqrt(weighted_square / weight_sum / float(dt_seconds))


def far_from_expiry_approximation(
    spot: float,
    open_price: float,
    seconds_remaining: float,
    sigma: float,
) -> float:
    """Φ(ln(S/O) / (σ √(T − 40s))).

    This is the closed form while the settlement window is still entirely
    in the future. Forty seconds is two thirds of the 60-second lookback.
    The exact blend ends about two seconds before the boundary, so its
    variance is closer to T − 42; the two agree once T is many minutes.
    """

    if spot <= 0 or open_price <= 0:
        raise ValueError("prices must be positive")
    if sigma <= 0:
        return 1.0 if spot >= open_price else 0.0
    horizon = float(seconds_remaining) - FAR_EXPIRY_LOOKBACK_SECONDS
    if horizon <= 0:
        raise ValueError("approximation needs more than 40 seconds remaining")
    z = math.log(spot / open_price) / (sigma * math.sqrt(horizon))
    return _normal_cdf(z)


def probability_up_twap(
    spot: float,
    open_price: float,
    now: float,
    window_end: int,
    sigma: float,
    observed: Mapping[int, float] | None = None,
) -> float:
    """P(closing TWAP >= open TWAP) for driftless Brownian motion.

    `now` is the unix time of `spot` (fractional seconds allowed). `observed`
    maps unix seconds to prices already printed. A missing second at or
    before `now` is forward-filled from the previous second, then from the
    anchor at `end−63`, and only then replaced with `spot`.
    """

    if spot <= 0 or open_price <= 0:
        raise ValueError("prices must be positive")
    if sigma < 0:
        raise ValueError("volatility must be non-negative")
    mean, taus, weights = _twap_moments(spot, now, _unix_seconds(window_end), observed or {})
    if not taus or sigma == 0:
        return 1.0 if mean >= open_price else 0.0
    scale = spot * sigma * math.sqrt(_brownian_time_variance(taus, weights))
    if scale <= 0:
        return 1.0 if mean >= open_price else 0.0
    return _normal_cdf((mean - open_price) / scale)


class TwapSettlementModel:
    """P(Up) from the blended 60-second Chainlink TWAP. This is the default."""

    name = "twap_settlement"

    def probability_up(self, data: PricingInput) -> Decimal:
        if data.open_price <= 0 or data.current_price <= 0:
            raise ValueError("prices must be positive")
        if data.volatility_per_sqrt_second < 0:
            raise ValueError("volatility must be non-negative")
        now, window_end = _clock(data)
        observed = _observed_map(data.observed_seconds)
        probability = probability_up_twap(
            float(data.current_price),
            float(data.open_price),
            now,
            window_end,
            float(data.volatility_per_sqrt_second),
            observed,
        )
        return _as_probability(probability)


class ZeroDriftLognormalModel:
    """P(S_T >= S_open) for a zero-drift lognormal price.

    `volatility_per_sqrt_second` is the diffusion coefficient estimated from
    recent prints of the settlement stream. At expiry (or with zero
    volatility) the probability collapses to the settlement rule: 1 when the
    current print is already at or above the open, otherwise 0.
    """

    name = "zero_drift_lognormal"

    def probability_up(self, data: PricingInput) -> Decimal:
        if data.open_price <= 0 or data.current_price <= 0:
            raise ValueError("prices must be positive")
        if data.seconds_remaining <= 0 or data.volatility_per_sqrt_second <= 0:
            return Decimal("1") if data.current_price >= data.open_price else Decimal("0")
        sigma_t = data.volatility_per_sqrt_second * data.seconds_remaining.sqrt()
        if sigma_t <= 0:
            return Decimal("1") if data.current_price >= data.open_price else Decimal("0")
        z = float((data.current_price / data.open_price).ln() / sigma_t)
        if z > 12:
            z = 12.0
        elif z < -12:
            z = -12.0
        probability = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        if probability < 0:
            probability = 0.0
        elif probability > 1:
            probability = 1.0
        return Decimal(f"{probability:.8f}")


def _clock(data: PricingInput) -> tuple[float, int]:
    if data.window_end_unix is None:
        if data.observed_seconds:
            raise ValueError("window_end_unix is required when observed_seconds is set")
        window_end = 1_000_000_000
        return window_end - float(data.seconds_remaining), window_end
    window_end = _unix_seconds(data.window_end_unix)
    if data.as_of_unix is None:
        return window_end - float(data.seconds_remaining), window_end
    return float(_unix_seconds(data.as_of_unix)), window_end


def _observed_map(points: Sequence[tuple[int, Decimal]]) -> dict[int, float]:
    observed: dict[int, float] = {}
    for timestamp, price in points:
        value = float(price)
        if value <= 0:
            raise ValueError("observed prices must be positive")
        observed[_unix_seconds(timestamp)] = value
    return observed


def _price_at(prices: Mapping[int, float], second: int) -> float | None:
    if second not in prices:
        return None
    return float(prices[second])


def _twap_moments(
    spot: float,
    now: float,
    window_end: int,
    observed: Mapping[int, float],
) -> tuple[float, list[float], list[float]]:
    weights = settlement_weights(window_end)
    seconds = sorted(weights)
    last = _price_at(observed, seconds[0] - 1)
    mean = 0.0
    taus: list[float] = []
    future_weights: list[float] = []
    for second in seconds:
        weight = weights[second]
        if second <= now:
            if second in observed:
                last = float(observed[second])
            price = spot if last is None else last
            mean += weight * price
        else:
            mean += weight * spot
            taus.append(second - now)
            future_weights.append(weight)
    return mean, taus, future_weights


def _brownian_time_variance(taus: Sequence[float], weights: Sequence[float]) -> float:
    """`wᵀ min(τ_i, τ_j) w` for a standard Brownian motion."""

    total = 0.0
    for tau_i, weight_i in zip(taus, weights):
        for tau_j, weight_j in zip(taus, weights):
            earlier = tau_i if tau_i < tau_j else tau_j
            total += weight_i * weight_j * earlier
    return total


def _normal_cdf(z: float) -> float:
    if z > 12:
        z = 12.0
    elif z < -12:
        z = -12.0
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _as_probability(probability: float) -> Decimal:
    if probability <= 0:
        return Decimal("0")
    if probability >= 1:
        return Decimal("1")
    text = f"{probability:.8f}"
    if text == "0.00000000":
        return Decimal("0")
    if text == "1.00000000":
        return Decimal("1")
    return Decimal(text)
