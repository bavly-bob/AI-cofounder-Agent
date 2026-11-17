"""MOCKED payment provider: a local simulation, no real payments.

Nothing here talks to a payment provider, a sandbox or the network, and no
payment SDK is used. There is no card input and no credential storage: an
"order" is a product name and a price, nothing more. Every event and payout this
module creates carries ``"mocked": True`` and ``"livemode": False``.

Amounts and fees are integers in minor units (cents) to avoid float rounding.

The provider never refunds by itself: ``MockPaymentProvider.refund`` is a
founder-gated "spend" action and only runs inside an approved
``approval.guarded_action("spend", ...)`` (see ``refunds.py``).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from approval import requires_founder_approval

MOCKED_LABEL = "MOCKED"
EVENT_TYPES = ("payment.succeeded", "refund.succeeded")
EVENT_KEYS = ("event_id", "type", "order_id", "amount", "fee", "currency")

# MOCKED fee schedule (2.9% + 30 cents), only so the numbers look like a real statement.
FEE_RATE_PER_MILLE = 29
FEE_FIXED_CENTS = 30


def mock_fee(amount_cents: int) -> int:
    """MOCKED processing fee for one payment, in cents."""
    return (amount_cents * FEE_RATE_PER_MILLE + 500) // 1000 + FEE_FIXED_CENTS


def fmt_money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d} {currency}"


@dataclass
class Order:
    order_id: str
    product: str
    amount: int          # cents
    currency: str
    status: str = "created"   # created | paid | refunded
    fee: int = 0
    refunded_amount: int = 0

    def as_dict(self) -> dict:
        return {"order_id": self.order_id, "product": self.product, "amount": self.amount,
                "currency": self.currency, "status": self.status, "fee": self.fee,
                "refunded_amount": self.refunded_amount, "mocked": True}


@dataclass
class MockPaymentProvider:
    """MOCKED provider: creates fake orders, fake events and fake payouts in memory."""

    currency: str = "USD"
    logger: object = None
    orders: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    _seq: dict = field(default_factory=dict)

    # -- helpers ----------------------------------------------------------
    def _next(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}_mock_{self._seq[prefix]:04d}"

    def _log(self, action: str, approved_by=None, **extra) -> None:
        if self.logger is not None:
            self.logger.log("payments", action, sources=[], model=None, cost=0,
                            approved_by=approved_by, mocked=True, **extra)

    def _event(self, event_type: str, order: Order, amount: int, fee: int,
               approved_by=None) -> dict:
        event = {
            "event_id": self._next("evt"),
            "type": event_type,
            "order_id": order.order_id,
            "amount": amount,
            "fee": fee,
            "currency": order.currency,
            "mocked": True,
            "livemode": False,
            "label": f"{MOCKED_LABEL}: simulated event, no real money moved",
        }
        self.events.append(event)
        self._log(f"emit_{event_type.replace('.', '_')}", event_id=event["event_id"],
                  order_id=order.order_id, amount=amount, fee=fee, currency=order.currency,
                  approved_by=approved_by)
        return copy.deepcopy(event)

    # -- public API -------------------------------------------------------
    def create_order(self, product: str, amount: int) -> Order:
        """MOCKED checkout: records a product and a price. No buyer data, no card data."""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("amount must be a positive integer number of cents")
        order = Order(self._next("ord"), str(product), amount, self.currency)
        self.orders[order.order_id] = order
        self._log("create_order", order_id=order.order_id, product=order.product,
                  amount=amount, currency=self.currency)
        return order

    def simulate_payment(self, order_id: str) -> dict:
        """MOCKED: pretend the buyer paid; returns a ``payment.succeeded`` event."""
        order = self.orders[order_id]
        if order.status != "created":
            raise ValueError(f"order {order_id} is already {order.status}")
        order.status = "paid"
        order.fee = mock_fee(order.amount)
        return self._event("payment.succeeded", order, order.amount, order.fee)

    def replay(self, event: dict) -> dict:
        """MOCKED: re-deliver an already sent event (same ``event_id``), as providers sometimes do."""
        self._log("replay_event", event_id=event["event_id"], order_id=event["order_id"])
        return copy.deepcopy(event)

    @requires_founder_approval("spend")
    def refund(self, order_id: str) -> dict:
        """MOCKED refund: founder-gated. Returns a ``refund.succeeded`` event; no money moves."""
        order = self.orders[order_id]
        if order.status != "paid":
            raise ValueError(f"order {order_id} is {order.status}; only a paid order can be refunded")
        order.status = "refunded"
        order.refunded_amount = order.amount
        # MOCKED policy: the processing fee is not returned on a refund.
        # Only reachable inside an approved guarded_action("spend"), so the founder approved it.
        return self._event("refund.succeeded", order, order.amount, 0, approved_by="founder")

    def mock_payouts(self) -> list:
        """MOCKED payouts: one line per order, net = paid - fee - refunded (what the provider would settle)."""
        payouts = []
        for order in self.orders.values():
            if order.status == "created":
                continue
            net = order.amount - order.fee - order.refunded_amount
            payouts.append({"payout_id": self._next("po"), "order_id": order.order_id,
                            "amount": net, "currency": order.currency, "mocked": True})
        self._log("create_mock_payouts", count=len(payouts))
        return payouts


def validate_event(event) -> None:
    """Raise ValueError unless ``event`` is a well-formed MOCKED event. Real (live) events are refused."""
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    missing = [k for k in EVENT_KEYS if k not in event]
    if missing:
        raise ValueError(f"event is missing {missing}")
    if event.get("mocked") is not True or event.get("livemode") not in (False, None):
        raise ValueError("only MOCKED events are accepted: this system handles no real payments")
    if event["type"] not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event['type']!r}")
    for key in ("amount", "fee"):
        value = event[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"'{key}' must be a non-negative integer number of cents")
