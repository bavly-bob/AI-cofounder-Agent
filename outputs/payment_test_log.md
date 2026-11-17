MOCKED — no real payments

# Payment test log (MOCKED sale-to-cash)

- Run: `20261117T210804Z-c94949` (demo run), generated 2026-11-17T21:08:09Z by `scripts/payment_test.py`
- MOCKED: no payment provider, no sandbox, no SDK, no network, no card data, no money moved.
- Amounts in USD; fees use a MOCKED schedule (2.9% + 0.30).
- Refund: approved by the founder at the CLI prompt; MOCKED refund executed; access revoked.
- Duplicate webhook: ignored_duplicate (delivered once).
- Planted payout mismatch on `ord_mock_0003`: FOUND by reconciliation.

## Orders and access

| Order | Product | Amount | Status | Access |
|---|---|---|---|---|
| ord_mock_0001 | Client-onboarding template (digital download) | 19.00 USD | paid | active (MOCK license key) |
| ord_mock_0002 | Client-onboarding template (digital download) | 19.00 USD | refunded | revoked |
| ord_mock_0003 | Template bundle + checklist (digital download) | 29.00 USD | paid | active (MOCK license key) |

## Webhook events (MOCKED)

| Event | Type | Order | Result |
|---|---|---|---|
| evt_mock_0001 | payment.succeeded | ord_mock_0001 | processed |
| evt_mock_0002 | payment.succeeded | ord_mock_0002 | processed |
| evt_mock_0003 | payment.succeeded | ord_mock_0003 | processed |
| evt_mock_0001 | payment.succeeded (replay) | ord_mock_0001 | ignored_duplicate |
| evt_mock_0004 | refund.succeeded | ord_mock_0002 | processed |

MOCK license keys issued: 3 (one per paid order).

## Reconciliation (orders, payments, fees, MOCKED payouts)

| Order | Paid | Fee | Refunded | Expected payout | Payout | Status |
|---|---|---|---|---|---|---|
| ord_mock_0001 | 19.00 USD | 0.85 USD | 0.00 USD | 18.15 USD | 18.15 USD | matched |
| ord_mock_0002 | 19.00 USD | 0.85 USD | 19.00 USD | -0.85 USD | -0.85 USD | matched |
| ord_mock_0003 | 29.00 USD | 1.14 USD | 0.00 USD | 27.86 USD | 26.36 USD | MISMATCH |

Totals: gross 67.00 USD, fees 2.84 USD, refunds 19.00 USD, expected payouts 45.16 USD, payouts 43.66 USD.

Mismatches:

- `payout_amount_mismatch` on ord_mock_0003: expected 2786 cents (paid 2900 - fee 114 - refunded 0), payout 2636 (PLANTED on purpose)
