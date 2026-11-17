# Sale to cash (MOCKED)

Key facts: this is a local, in-memory **simulation**. There is no payment provider, no sandbox, no SDK, no network call, no card input, no buyer data and no money. Every event carries `"mocked": true` and `"livemode": false`, and anything else is rejected. Amounts are integer cents. It runs as a standalone script (`scripts/payment_test.py`) and is not yet connected to the Phase 1-2 pipeline.

## Modules

| Module | What it does |
|---|---|
| `payments.py` | `MockPaymentProvider`: `create_order` (product and price only), `simulate_payment` (emits `payment.succeeded` with `event_id`, `order_id`, `amount`, `fee`, `currency`; MOCKED fee 2.9% + 30 cents), `replay` (re-sends an event), `refund` (founder-gated "spend" action; emits `refund.succeeded`; the fee is not returned), `mock_payouts` (net = paid - fee - refunded per order). `validate_event` refuses non-MOCKED events. |
| `webhooks.py` | `handle_event(event)`: validates, ignores and logs a replayed `event_id`, records payments and refunds, and calls delivery. |
| `delivery.py` | `DeliveryService.grant` issues a MOCKED license key at most once per order; `revoke` turns it off; `has_access` tells. Nothing is emailed or downloaded. |
| `refunds.py` | `request_refund`: checks that the order is paid, then asks the founder through `approval.guarded_action("spend", ...)`. Only an explicit Yes runs the executor; the refund event then goes through the webhook handler, which revokes access. |
| `reconcile.py` | `reconcile(orders, payments, refunds, payouts)`: one row per order (paid, fee, refunded, expected payout, payout, matched or MISMATCH) and a list of mismatches: payment amount, currency, refund larger than payment, payout missing, payout amount, payment or payout without an order. |

## The scenario

`python scripts/payment_test.py` (writes under `runs/<run_id>/`) or `python scripts/payment_test.py --demo` (writes `outputs/payment_test_log.md` and appends to the root `decision_log.json`):

1. 3 orders of digital products are paid (MOCKED events); each grants access once.
2. The first payment event is replayed: it is ignored and logged, and no second key is issued.
3. A refund of the second order is requested: the terminal asks the founder (default No). Yes: access is revoked. No: nothing is refunded and access stays.
4. A payout mismatch of -1.50 USD is planted on the third order; reconciliation must report it.

The report's first line is "MOCKED — no real payments". The refund answer is a real founder decision: the script uses only the terminal approver, and no flag answers for the founder. The committed log is produced by the founder-run demo.

## Tests

`tests/test_sale_to_cash.py`: events are MOCKED and complete; a duplicate event delivers once; a non-MOCKED event is rejected; an approved refund revokes access; a refund without approval is not executed; the provider cannot refund by itself; reconciliation finds the planted mismatch and matches after a refund; every log entry has the required keys; the script runs; no payment SDK anywhere.

## What production would need

- A real provider in **test mode first**, on accounts the founder opens and owns.
- Signed webhooks, with the signature checked before an event is processed.
- Credentials handled only by the founder, never stored by this system.
- Persistent storage for processed event ids and access grants (today they are in memory).
- Refunds still only through a founder-approved "spend" action.
