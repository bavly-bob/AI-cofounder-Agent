"""MOCKED reconciliation: matches orders <-> payments <-> fees <-> payouts and reports every mismatch.

Inputs are the merchant's own records (orders, plus the payment and refund
events received by the webhook handler) and the MOCKED provider payouts. For each
order the expected payout is ``payment.amount - payment.fee - refund.amount``.
All amounts are integer cents. Nothing here moves money.
"""
from __future__ import annotations


def _as_dict(order) -> dict:
    return order.as_dict() if hasattr(order, "as_dict") else dict(order)


def reconcile(orders, payments: dict, refunds: dict, payouts: list, logger=None) -> dict:
    """Return ``{"ok", "rows", "mismatches", "totals", "mocked"}``.

    ``orders``: dict or list of orders (``Order`` or dict with order_id/amount/currency).
    ``payments`` / ``refunds``: order_id -> event. ``payouts``: list of payout dicts.
    """
    order_list = [_as_dict(o) for o in (orders.values() if isinstance(orders, dict) else orders)]
    by_order = {o["order_id"]: o for o in order_list}
    payouts_by_order: dict = {}
    for p in payouts:
        payouts_by_order.setdefault(p["order_id"], []).append(p)

    rows, mismatches = [], []

    def flag(kind, order_id, detail):
        mismatches.append({"type": kind, "order_id": order_id, "detail": detail})

    for oid, order in by_order.items():
        pay = payments.get(oid)
        ref = refunds.get(oid)
        lines = payouts_by_order.get(oid, [])
        paid = pay["amount"] if pay else 0
        fee = pay["fee"] if pay else 0
        refunded = ref["amount"] if ref else 0
        expected = paid - fee - refunded
        paid_out = sum(p["amount"] for p in lines)
        if pay is None:
            if lines:
                flag("payout_without_payment", oid, f"payout {paid_out} cents but no payment recorded")
            rows.append({"order_id": oid, "order_amount": order["amount"], "paid": 0, "fee": 0,
                         "refunded": refunded, "expected_payout": 0, "payout": paid_out,
                         "status": "unpaid" if not lines else "MISMATCH"})
            if ref is not None:
                flag("refund_without_payment", oid, "refund recorded for an unpaid order")
            continue
        row_ok = True
        if pay["amount"] != order["amount"]:
            flag("payment_amount_mismatch", oid, f"order {order['amount']} vs payment {pay['amount']} cents")
            row_ok = False
        if pay.get("currency") != order.get("currency"):
            flag("currency_mismatch", oid, f"order {order.get('currency')} vs payment {pay.get('currency')}")
            row_ok = False
        if refunded > paid:
            flag("refund_exceeds_payment", oid, f"refunded {refunded} > paid {paid} cents")
            row_ok = False
        if not lines:
            flag("payout_missing", oid, f"expected payout {expected} cents, none found")
            row_ok = False
        elif paid_out != expected:
            flag("payout_amount_mismatch", oid,
                 f"expected {expected} cents (paid {paid} - fee {fee} - refunded {refunded}), payout {paid_out}")
            row_ok = False
        rows.append({"order_id": oid, "order_amount": order["amount"], "paid": paid, "fee": fee,
                     "refunded": refunded, "expected_payout": expected, "payout": paid_out,
                     "status": "matched" if row_ok else "MISMATCH"})

    for oid in payments:
        if oid not in by_order:
            flag("payment_without_order", oid, "payment event for an unknown order")
    for oid in payouts_by_order:
        if oid not in by_order:
            flag("payout_without_order", oid, "payout for an unknown order")

    totals = {
        "gross": sum(r["paid"] for r in rows),
        "fees": sum(r["fee"] for r in rows),
        "refunds": sum(r["refunded"] for r in rows),
        "expected_payouts": sum(r["expected_payout"] for r in rows),
        "payouts": sum(p["amount"] for p in payouts),
    }
    report = {"ok": not mismatches, "rows": rows, "mismatches": mismatches, "totals": totals,
              "mocked": True}
    if logger is not None:
        logger.log("reconcile", "reconcile_orders_payments_fees_payouts", sources=[], model=None,
                   cost=0, approved_by=None, mocked=True,
                   outcome="matched" if report["ok"] else "mismatch",
                   orders=len(rows), mismatches=len(mismatches),
                   mismatch_types=sorted({m["type"] for m in mismatches}))
    return report
