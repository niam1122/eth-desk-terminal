"""Fair probability that a 15-minute window settles Up.

The default model is intentionally small and replaceable. Implement
`probability_up` and pass the object to the engine. The engine does not
care which class it is; it only calls that method with a `PricingInput`.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Protocol

from eth_desk.types import PricingInput


class FairValueModel(Protocol):
    name: str

    def probability_up(self, data: PricingInput) -> Decimal:
        """Return P(settlement price >= window open price), between 0 and 1."""


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
