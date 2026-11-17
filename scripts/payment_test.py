"""MOCKED sale-to-cash scenario: no real payments, no provider, no SDK, no money.

Scenario: 3 orders are paid (MOCKED events), 1 webhook is delivered twice, 1
refund is requested and needs the founder's approval at the terminal (default
No), and 1 payout mismatch is planted on purpose so reconciliation must find it.

    python scripts/payment_test.py          # writes runs/<run_id>/payment_test_log.md + decision_log.json
    python scripts/payment_test.py --demo   # writes outputs/payment_test_log.md, appends to decision_log.json

The refund approval is a real founder decision: this script only ever uses
``CLIApprover``. There is no flag that approves for the founder.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from approval import CLIApprover  # noqa: E402
from decision_log import DecisionLogger, new_run_id, utc_timestamp  # noqa: E402
from delivery import DeliveryService  # noqa: E402
from payments import MockPaymentProvider, fmt_money  # noqa: E402
from reconcile import reconcile  # noqa: E402
from refunds import request_refund  # noqa: E402
from webhooks import WebhookHandler  # noqa: E402

FIRST_LINE = "MOCKED — no real payments"
PRODUCTS = [  # MOCKED catalogue: digital products only
    ("Client-onboarding template (digital download)", 1900),
    ("Client-onboarding template (digital download)", 1900),
    ("Template bundle + checklist (digital download)", 2900),
]
PLANTED_DELTA = -150  # cents: the planted payout mismatch


def run_scenario(approver, logger, echo=print) -> dict:
    """Run the MOCKED scenario and return everything the report needs."""
    provider = MockPaymentProvider(logger=logger)
    delivery = DeliveryService(logger=logger)
    handler = WebhookHandler(delivery, logger=logger)
    ctx = SimpleNamespace(approver=approver, logger=logger)
    logger.log("payment_test", "start_mocked_sale_to_cash_scenario", sources=[], model=None,
               cost=0, approved_by=None, mocked=True)

    echo(f"[MOCKED] creating {len(PRODUCTS)} orders (no buyer data, no card data)")
    orders = [provider.create_order(name, price) for name, price in PRODUCTS]
    events, webhook_results = [], []
    for order in orders:
        event = provider.simulate_payment(order.order_id)
        events.append(event)
        res = handler.handle_event(event)
        webhook_results.append((event["event_id"], event["type"], order.order_id, res["status"]))
        echo(f"[MOCKED] {event['event_id']} payment.succeeded {order.order_id} "
             f"{fmt_money(event['amount'])} -> {res['status']}, access {res['delivery']['status']}")

    dup = provider.replay(events[0])
    dup_res = handler.handle_event(dup)
    webhook_results.append((dup["event_id"], dup["type"] + " (replay)", dup["order_id"], dup_res["status"]))
    echo(f"[MOCKED] duplicate webhook {dup['event_id']} -> {dup_res['status']}")

    refund_order = orders[1].order_id
    echo(f"[MOCKED] refund requested for {refund_order}: founder approval required")
    refund = request_refund(refund_order, "buyer asked for a refund within 14 days (MOCKED request)",
                            ctx, provider, handler)
    if refund["executed"]:
        webhook_results.append((refund["event_id"], "refund.succeeded", refund_order,
                                refund["webhook"]["status"]))
        echo(f"[MOCKED] refund executed after founder approval -> access "
             f"{refund['webhook']['delivery']['status']}")
    else:
        echo(f"[MOCKED] refund NOT executed ({refund.get('decision') or refund['status']}): access kept")

    payouts = provider.mock_payouts()
    planted = orders[2].order_id
    for p in payouts:
        if p["order_id"] == planted:
            p["amount"] += PLANTED_DELTA
            p["planted_mismatch"] = True
    logger.log("payment_test", "plant_payout_mismatch", sources=[], model=None, cost=0,
               approved_by=None, mocked=True, order_id=planted, delta=PLANTED_DELTA)
    echo(f"[MOCKED] planted a payout mismatch of {fmt_money(PLANTED_DELTA)} on {planted}")

    report = reconcile(provider.orders, handler.payments, handler.refunds, payouts, logger=logger)
    found = any(m["order_id"] == planted and m["type"] == "payout_amount_mismatch"
                for m in report["mismatches"])
    echo(f"[MOCKED] reconciliation: {len(report['mismatches'])} mismatch(es); planted mismatch "
         f"{'FOUND' if found else 'NOT FOUND'}")
    logger.log("payment_test", "finish_mocked_sale_to_cash_scenario", sources=[], model=None,
               cost=0, approved_by=None, mocked=True,
               outcome="planted_mismatch_found" if found else "planted_mismatch_missed")
    return {"provider": provider, "delivery": delivery, "handler": handler, "orders": orders,
            "webhooks": webhook_results, "duplicate": dup_res, "refund": refund,
            "refund_order": refund_order, "payouts": payouts, "planted_order": planted,
            "reconcile": report, "planted_found": found}


def render_log(r: dict, run_id: str, demo: bool) -> str:
    refund = r["refund"]
    if refund["executed"]:
        refund_line = "approved by the founder at the CLI prompt; MOCKED refund executed; access revoked"
    elif refund.get("decision") == "rejected":
        refund_line = "rejected by the founder at the CLI prompt; not executed; access kept"
    else:
        refund_line = f"not executed ({refund.get('decision') or refund['status']}); access kept"
    lines = [
        FIRST_LINE,
        "",
        "# Payment test log (MOCKED sale-to-cash)",
        "",
        f"- Run: `{run_id}` ({'demo run' if demo else 'local run'}), generated {utc_timestamp()} "
        "by `scripts/payment_test.py`",
        "- MOCKED: no payment provider, no sandbox, no SDK, no network, no card data, no money moved.",
        "- Amounts in USD; fees use a MOCKED schedule (2.9% + 0.30).",
        f"- Refund: {refund_line}.",
        f"- Duplicate webhook: {r['duplicate']['status']} (delivered once).",
        f"- Planted payout mismatch on `{r['planted_order']}`: "
        f"{'FOUND by reconciliation' if r['planted_found'] else 'NOT FOUND (test failed)'}.",
        "",
        "## Orders and access",
        "",
        "| Order | Product | Amount | Status | Access |",
        "|---|---|---|---|---|",
    ]
    for o in r["orders"]:
        grant = r["delivery"].grants.get(o.order_id)
        access = "none" if grant is None else ("active (MOCK license key)" if grant.active else "revoked")
        lines.append(f"| {o.order_id} | {o.product} | {fmt_money(o.amount)} | {o.status} | {access} |")
    lines += ["", "## Webhook events (MOCKED)", "", "| Event | Type | Order | Result |", "|---|---|---|---|"]
    for eid, etype, oid, status in r["webhooks"]:
        lines.append(f"| {eid} | {etype} | {oid} | {status} |")
    lines += ["", f"MOCK license keys issued: {r['delivery'].deliveries} (one per paid order).", "",
              "## Reconciliation (orders, payments, fees, MOCKED payouts)", "",
              "| Order | Paid | Fee | Refunded | Expected payout | Payout | Status |",
              "|---|---|---|---|---|---|---|"]
    for row in r["reconcile"]["rows"]:
        lines.append(f"| {row['order_id']} | {fmt_money(row['paid'])} | {fmt_money(row['fee'])} | "
                     f"{fmt_money(row['refunded'])} | {fmt_money(row['expected_payout'])} | "
                     f"{fmt_money(row['payout'])} | {row['status']} |")
    t = r["reconcile"]["totals"]
    lines += ["", f"Totals: gross {fmt_money(t['gross'])}, fees {fmt_money(t['fees'])}, refunds "
              f"{fmt_money(t['refunds'])}, expected payouts {fmt_money(t['expected_payouts'])}, "
              f"payouts {fmt_money(t['payouts'])}.", "", "Mismatches:", ""]
    if r["reconcile"]["mismatches"]:
        for m in r["reconcile"]["mismatches"]:
            planted = " (PLANTED on purpose)" if m["order_id"] == r["planted_order"] else ""
            lines.append(f"- `{m['type']}` on {m['order_id']}: {m['detail']}{planted}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main(argv=None, approver=None, base_dir=None, echo=print) -> int:
    parser = argparse.ArgumentParser(description="MOCKED sale-to-cash scenario (no real payments).")
    parser.add_argument("--demo", action="store_true",
                        help="write outputs/payment_test_log.md and append to the root decision_log.json")
    args = parser.parse_args(argv)
    base = Path(base_dir) if base_dir else ROOT
    run_id = new_run_id()
    if args.demo:
        out_path, log_path = base / "outputs" / "payment_test_log.md", base / "decision_log.json"
    else:
        out_path = base / "runs" / run_id / "payment_test_log.md"
        log_path = base / "runs" / run_id / "decision_log.json"
    logger = DecisionLogger(log_path, run_id=run_id, reset=False)
    approver = approver or CLIApprover()

    echo(f"MOCKED - no real payments. MOCKED sale-to-cash scenario, run {run_id}")
    try:
        results = run_scenario(approver, logger, echo=echo)
    except Exception as e:  # never crash in front of a user
        echo(f"[MOCKED] scenario failed: {e}")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_log(results, run_id, args.demo), encoding="utf-8")
    logger.log("payment_test", "write_payment_test_log", sources=[], model=None, cost=0,
               approved_by=None, mocked=True, path=out_path.relative_to(base).as_posix())
    echo(f"[MOCKED] wrote {out_path}")
    echo(f"[MOCKED] decision log: {log_path}")
    return 0 if results["planted_found"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
