"""Order intake, reduced to status bookkeeping.

Quiz fixture sandbox code; never imported by the application.
"""

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

    def add_line(self, sku, quantity):
        if self.status != "draft":
            raise OrderError(f"order {self.order_id}: cannot edit a {self.status} order")
        if quantity <= 0:
            raise OrderError("line quantity must be positive")
        self.lines.append((sku, quantity))

    def advance(self, status):
        """Move to `status`; stock and pricing are no longer handled here."""
        if status not in VALID_STATUSES:
            raise OrderError(f"unknown status: {status}")
        self.status = status
