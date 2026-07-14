"""Deterministic fixture-PR content generator for quiz E2E testing.

Writes a fixed set of files under fixtures/sandbox/ (or --root). Output is
byte-identical on every run, so fixture branches regenerate idempotently.
Sizes map to quiz-pipeline behaviors:
- small:  docs-only, ~12 lines    -> difficulty judge should rate near 0.2, N=1
- medium: ~300 Python lines       -> judged difficulty visibly swings N
- large:  ~4400 lines over 8 files -> skips the judge (>=4000 lines), N=20,
          one diff chunk per file, MAX_CHUNKS cap exercised
"""
import argparse
from pathlib import Path

NOTES_MD = """# Sandbox notes

This folder only exists on `fixture/*` branches. It carries generated
content used to exercise the PR quiz merge gate end to end.

## Why a docs-only fixture?

A pure-markdown change is the easiest possible diff to review. The
difficulty judge should rate it near the 0.2 minimum, and with only a
dozen changed lines the quiz stays at a single question.

Regenerate with `just fixture-prs small`; clean up with `just fixture-clean`.
"""

INVENTORY_PY = '''"""In-memory stock ledger with lot-level reservations.

Quiz fixture sandbox code; never imported by the application.
"""


class LedgerError(Exception):
    """Raised when an operation would corrupt stock state."""


class Lot:
    """A received batch of a single SKU."""

    def __init__(self, lot_id, sku, quantity, received_on):
        if quantity <= 0:
            raise LedgerError(f"lot {lot_id}: quantity must be positive")
        self.lot_id = lot_id
        self.sku = sku
        self.quantity = quantity
        self.reserved = 0
        self.received_on = received_on

    @property
    def available(self):
        return self.quantity - self.reserved

    def reserve(self, amount):
        if amount > self.available:
            raise LedgerError(f"lot {self.lot_id}: cannot reserve {amount}")
        self.reserved += amount

    def release(self, amount):
        if amount > self.reserved:
            raise LedgerError(f"lot {self.lot_id}: cannot release {amount}")
        self.reserved -= amount

    def consume(self, amount):
        if amount > self.reserved:
            raise LedgerError(f"lot {self.lot_id}: consume exceeds reservation")
        self.reserved -= amount
        self.quantity -= amount


class StockLedger:
    """Tracks lots per SKU and reserves stock first-in-first-out."""

    def __init__(self, reorder_point=10):
        self.reorder_point = reorder_point
        self._lots = {}
        self._by_sku = {}

    def receive(self, lot_id, sku, quantity, received_on):
        if lot_id in self._lots:
            raise LedgerError(f"lot {lot_id} already received")
        lot = Lot(lot_id, sku, quantity, received_on)
        self._lots[lot_id] = lot
        self._by_sku.setdefault(sku, []).append(lot)
        self._by_sku[sku].sort(key=lambda l: l.received_on)
        return lot

    def on_hand(self, sku):
        return sum(lot.quantity for lot in self._by_sku.get(sku, []))

    def available(self, sku):
        return sum(lot.available for lot in self._by_sku.get(sku, []))

    def reserve(self, sku, amount):
        """Reserve `amount` across the oldest lots; returns (lot_id, taken) pairs."""
        if amount <= 0:
            raise LedgerError("reservation amount must be positive")
        if amount > self.available(sku):
            raise LedgerError(f"{sku}: insufficient stock for {amount}")
        plan = []
        remaining = amount
        for lot in self._by_sku[sku]:
            take = min(lot.available, remaining)
            if take == 0:
                continue
            lot.reserve(take)
            plan.append((lot.lot_id, take))
            remaining -= take
            if remaining == 0:
                break
        return plan

    def release(self, plan):
        for lot_id, amount in plan:
            self._lots[lot_id].release(amount)

    def consume(self, plan):
        for lot_id, amount in plan:
            self._lots[lot_id].consume(amount)

    def reorder_suggestions(self):
        """SKUs whose unreserved stock fell to or below the reorder point."""
        return sorted(
            sku for sku in self._by_sku if self.available(sku) <= self.reorder_point
        )
'''

PRICING_PY = '''"""Tiered price book with quantity breaks and channel discounts.

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
'''

ORDERS_PY = '''"""Order intake tying stock reservations to price quotes.

Quiz fixture sandbox code; never imported by the application.
"""
from inventory import LedgerError, StockLedger
from pricing import PriceBook, PricingError

VALID_STATUSES = ("draft", "reserved", "invoiced", "cancelled")


class OrderError(Exception):
    """Raised when an order transition is not allowed."""


class Order:
    """A customer order moving draft -> reserved -> invoiced (or cancelled)."""

    def __init__(self, order_id, channel="web"):
        self.order_id = order_id
        self.channel = channel
        self.status = "draft"
        self.lines = []
        self.reservations = {}
        self.totals = None

    def add_line(self, sku, quantity):
        if self.status != "draft":
            raise OrderError(f"order {self.order_id}: cannot edit a {self.status} order")
        if quantity <= 0:
            raise OrderError("line quantity must be positive")
        self.lines.append((sku, quantity))


class OrderDesk:
    """Coordinates a StockLedger and a PriceBook for order processing."""

    def __init__(self, ledger, price_book):
        self.ledger = ledger
        self.price_book = price_book
        self._orders = {}

    def open_order(self, order_id, channel="web"):
        if order_id in self._orders:
            raise OrderError(f"order {order_id} already exists")
        order = Order(order_id, channel)
        self._orders[order_id] = order
        return order

    def reserve(self, order_id):
        """Reserve stock for every line; rolls back on partial failure."""
        order = self._require(order_id, "draft")
        if not order.lines:
            raise OrderError(f"order {order_id}: no lines to reserve")
        done = {}
        try:
            for sku, quantity in order.lines:
                done[sku] = self.ledger.reserve(sku, quantity)
        except LedgerError:
            for plan in done.values():
                self.ledger.release(plan)
            raise
        order.reservations = done
        order.status = "reserved"
        return order

    def invoice(self, order_id):
        """Consume reserved stock and price the order."""
        order = self._require(order_id, "reserved")
        try:
            totals, grand = self.price_book.quote_lines(order.lines, order.channel)
        except PricingError:
            self.cancel(order_id)
            raise
        for plan in order.reservations.values():
            self.ledger.consume(plan)
        order.totals = (totals, grand)
        order.status = "invoiced"
        return grand

    def cancel(self, order_id):
        order = self._orders.get(order_id)
        if order is None or order.status in ("invoiced", "cancelled"):
            raise OrderError(f"order {order_id}: cannot cancel")
        for plan in order.reservations.values():
            self.ledger.release(plan)
        order.reservations = {}
        order.status = "cancelled"

    def _require(self, order_id, status):
        order = self._orders.get(order_id)
        if order is None:
            raise OrderError(f"unknown order: {order_id}")
        if order.status != status:
            raise OrderError(f"order {order_id} is {order.status}, expected {status}")
        return order


def build_demo_desk():
    """Small wired-up desk used by the fixture docs."""
    ledger = StockLedger(reorder_point=25)
    ledger.receive("L1", "SKU-RED", 120, "2026-01-05")
    ledger.receive("L2", "SKU-RED", 80, "2026-02-11")
    ledger.receive("L3", "SKU-BLUE", 200, "2026-01-20")
    book = PriceBook()
    book.set_tiers("SKU-RED", [(1, "4.90"), (50, "4.40"), (200, "3.95")])
    book.set_tiers("SKU-BLUE", [(1, "2.10"), (100, "1.85")])
    return OrderDesk(ledger, book)
'''

_LABELS = ("alpha", "beta", "gamma", "delta")


def _dataset_module(letter, offset):
    rows = "\n".join(
        f'    ("{letter}{i:04d}", {i * 7 + offset}, {(i * 31 + offset) % 997}, "{_LABELS[i % 4]}"),'
        for i in range(540)
    )
    return f'''"""Deterministic lookup table "{letter}" for the large quiz fixture."""

ROWS = [
{rows}
]


def row_count():
    return len(ROWS)


def value_sum():
    return sum(row[1] for row in ROWS)


def find(key):
    for row in ROWS:
        if row[0] == key:
            return row
    return None
'''


def small():
    return {"NOTES.md": NOTES_MD}


def medium():
    return {"inventory.py": INVENTORY_PY, "orders.py": ORDERS_PY, "pricing.py": PRICING_PY}


def large():
    return {
        f"dataset_{letter}.py": _dataset_module(letter, offset=i * 17)
        for i, letter in enumerate("abcdefgh")
    }


FIXTURES = {"small": small, "medium": medium, "large": large}


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic fixture-PR content")
    parser.add_argument("size", choices=sorted(FIXTURES))
    parser.add_argument("--root", default="fixtures/sandbox")
    args = parser.parse_args()

    root = Path(args.root)
    files = FIXTURES[args.size]()
    for rel, content in sorted(files.items()):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="\n") as fh:
            fh.write(content)
    lines = sum(content.count("\n") for content in files.values())
    print(f"{args.size}: {len(files)} files, {lines} lines under {root}")


if __name__ == "__main__":
    main()
