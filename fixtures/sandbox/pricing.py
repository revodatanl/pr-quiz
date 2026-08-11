"""Tiered price book with quantity breaks and channel discounts.

Quiz fixture sandbox code; never imported by the application.
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

CHANNEL_DISCOUNTS = {
    "web": Decimal("0.00"),
    "partner": Decimal("0.05"),
    "wholesale": Decimal("0.12"),
}


class PricingError(Exception):
    """Raised for unknown SKUs, channels, or malformed tier tables."""


def _validate_tiers(tiers):
    if not tiers:
        raise PricingError("tier table is empty")
    thresholds = [t[0] for t in tiers]
    if thresholds[0] != 1:
        raise PricingError("first tier must start at quantity 1")
    if thresholds != sorted(set(thresholds)):
        raise PricingError("tier thresholds must be strictly increasing")
    for _, unit_price in tiers:
        if unit_price <= 0:
            raise PricingError("unit prices must be positive")


class PriceBook:
    """Maps a SKU to quantity-break tiers: [(min_qty, unit_price), ...]."""

    def __init__(self):
        self._tiers = {}

    def set_tiers(self, sku, tiers):
        parsed = [(int(q), Decimal(str(p))) for q, p in tiers]
        _validate_tiers(parsed)
        self._tiers[sku] = parsed

    def unit_price(self, sku, quantity):
        if quantity < 1:
            raise PricingError("quantity must be at least 1")
        if sku not in self._tiers:
            raise PricingError(f"unknown SKU: {sku}")
        chosen = None
        for min_qty, price in self._tiers[sku]:
            if quantity >= min_qty:
                chosen = price
            else:
                break
        return chosen

    def quote(self, sku, quantity, channel="web"):
        """Line total after tier pricing and channel discount, rounded to cents.

        Rounding happens once, on the line total, with ROUND_HALF_UP; rounding
        per unit would drift on large quantities.
        """
        if channel not in CHANNEL_DISCOUNTS:
            raise PricingError(f"unknown channel: {channel}")
        unit = self.unit_price(sku, quantity)
        gross = unit * quantity
        net = gross * (1 - CHANNEL_DISCOUNTS[channel])
        return net.quantize(CENT, rounding=ROUND_HALF_UP)

    def quote_lines(self, lines, channel="web"):
        """Quote [(sku, quantity), ...]; returns (per-line totals, grand total)."""
        totals = [self.quote(sku, quantity, channel) for sku, quantity in lines]
        grand = sum(totals, Decimal("0.00"))
        return totals, grand
