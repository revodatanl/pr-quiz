"""In-memory stock ledger with lot-level reservations.

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
