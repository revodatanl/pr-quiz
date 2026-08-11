"""Order intake tying stock reservations to price quotes.

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
