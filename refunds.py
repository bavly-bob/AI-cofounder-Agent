"""Refunds: executed ONLY through ``approval.guarded_action("spend", ...)``.

The system never refunds by itself. ``request_refund`` checks the order, then
asks the founder (via ``ctx.approver``). Only an explicit founder approval runs
the MOCKED executor, which asks the MOCKED provider for a refund event; that
event then goes through the webhook handler, which revokes access. No money
moves anywhere: there is no real provider.
"""
from __future__ import annotations

from approval import guarded_action, requires_founder_approval
from payments import MockPaymentProvider, fmt_money
from webhooks import WebhookHandler


def _make_executor(provider: MockPaymentProvider):
    @requires_founder_approval("spend")
    def mock_execute_refund(payload) -> dict:
        """MOCKED refund executor: produces a simulated refund event, moves no money."""
        event = provider.refund(payload["order_id"])
        return {"status": "MOCKED", "kind": "spend", "event": event,
                "detail": "MOCKED refund: no real money moved"}
    return mock_execute_refund


def request_refund(order_id: str, reason: str, ctx, provider: MockPaymentProvider,
                   handler: WebhookHandler) -> dict:
    """Ask the founder to approve a MOCKED refund. Returns what happened.

    ``ctx`` needs ``.approver`` and ``.logger``. Without an approver nothing is refunded.
    """
    logger = getattr(ctx, "logger", None)

    def _log(action, **extra):
        if logger is not None:
            logger.log("refunds", action, sources=[], model=None, cost=0, approved_by=None,
                       mocked=True, order_id=order_id, **extra)

    order = provider.orders.get(order_id)
    if order is None or order.status != "paid" or order_id not in handler.payments:
        state = "unknown" if order is None else order.status
        _log("refund_refused", outcome="refused", reason=f"order is {state}, not a paid order")
        return {"status": "refused", "order_id": order_id, "executed": False,
                "reason": f"order is {state}, not a paid order"}

    payload = {"order_id": order_id, "amount": order.amount, "currency": order.currency,
               "reason": reason, "mocked": True}
    _log("refund_requested", amount=order.amount, currency=order.currency, reason=reason)
    summary = (f"MOCKED refund of {fmt_money(order.amount, order.currency)} for {order_id} "
               f"({order.product}). Reason: {reason}. No real money moves.")
    result = guarded_action("spend", summary, payload, _make_executor(provider), ctx)

    if not result.executed:
        _log("refund_not_executed", outcome=result.decision, error=result.error)
        return {"status": "not_executed", "order_id": order_id, "executed": False,
                "decision": result.decision, "error": result.error}

    webhook = handler.handle_event(result.result["event"])
    return {"status": "refunded", "order_id": order_id, "executed": True, "decision": "approved",
            "event_id": result.result["event"]["event_id"], "webhook": webhook}
