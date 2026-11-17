"""MOCKED webhook handler: turns MOCKED provider events into delivery and bookkeeping.

``handle_event(event)`` is idempotent on ``event_id``: an event that was already
processed is ignored (and the replay is logged), so a duplicate webhook can never
deliver twice. Only MOCKED events are accepted; there is no signature check
because there is no real provider (production would need signed webhooks).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from delivery import DeliveryService
from payments import validate_event


@dataclass
class WebhookHandler:
    delivery: DeliveryService
    logger: object = None
    processed: set = field(default_factory=set)
    payments: dict = field(default_factory=dict)   # order_id -> payment.succeeded event (merchant records)
    refunds: dict = field(default_factory=dict)    # order_id -> refund.succeeded event
    ignored: list = field(default_factory=list)    # event_ids of ignored replays

    def _log(self, action: str, **extra) -> None:
        if self.logger is not None:
            self.logger.log("webhooks", action, sources=[], model=None, cost=0,
                            approved_by=None, mocked=True, **extra)

    def handle_event(self, event) -> dict:
        try:
            validate_event(event)
        except ValueError as e:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            self._log("reject_event", event_id=event_id, outcome="rejected", error=str(e))
            return {"status": "rejected", "event_id": event_id, "error": str(e)}

        event_id = event["event_id"]
        if event_id in self.processed:
            self.ignored.append(event_id)
            self._log("ignore_duplicate_event", event_id=event_id, order_id=event["order_id"],
                      type=event["type"], outcome="ignored_duplicate")
            return {"status": "ignored_duplicate", "event_id": event_id}
        self.processed.add(event_id)
        self._log("receive_event", event_id=event_id, order_id=event["order_id"], type=event["type"],
                  amount=event["amount"], fee=event["fee"], currency=event["currency"])

        order_id = event["order_id"]
        if event["type"] == "payment.succeeded":
            self.payments[order_id] = dict(event)
            result = self.delivery.grant(order_id, event_id)
        else:  # refund.succeeded
            self.refunds[order_id] = dict(event)
            result = self.delivery.revoke(order_id, event_id)
        return {"status": "processed", "event_id": event_id, "delivery": result}


_default_handler: WebhookHandler | None = None


def default_handler() -> WebhookHandler:
    global _default_handler
    if _default_handler is None:
        _default_handler = WebhookHandler(DeliveryService())
    return _default_handler


def handle_event(event, handler: WebhookHandler | None = None) -> dict:
    """Process one MOCKED event (idempotent on ``event_id``)."""
    return (handler or default_handler()).handle_event(event)
