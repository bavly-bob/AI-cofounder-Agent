"""Smoke tests for the MOCKED sale-to-cash flow. No network, no Ollama, no money.

The approve-everything approver below exists only in tests/ (never in app code).
"""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from approval import ApprovalRequired, ApprovalResult, Approver, DenyAllApprover, FOUNDER
from decision_log import DecisionLogger, REQUIRED_KEYS, validate_entry
from delivery import DeliveryService
from payments import MockPaymentProvider
from reconcile import reconcile
from refunds import request_refund
from webhooks import WebhookHandler

ROOT = Path(__file__).resolve().parent.parent
PAYMENT_SDKS = ("stripe", "paypal", "paymob", "braintree", "square", "adyen", "razorpay")


class ScriptedYesApprover(Approver):
    """TEST ONLY: approves every request, as if the founder typed y."""

    def __init__(self):
        self.calls = []

    def request(self, kind, summary, payload):
        self.calls.append((kind, summary))
        return ApprovalResult(True, FOUNDER, "scripted test approval")


class ScriptedNoApprover(Approver):
    """TEST ONLY: the founder explicitly rejects."""

    def request(self, kind, summary, payload):
        return ApprovalResult(False, FOUNDER, "scripted test rejection")


@pytest.fixture
def world(tmp_path):
    logger = DecisionLogger(tmp_path / "decision_log.json", run_id="test")
    provider = MockPaymentProvider(logger=logger)
    delivery = DeliveryService(logger=logger)
    handler = WebhookHandler(delivery, logger=logger)
    return SimpleNamespace(logger=logger, provider=provider, delivery=delivery, handler=handler)


def _paid_order(w, amount=1900):
    order = w.provider.create_order("Template (digital download)", amount)
    w.handler.handle_event(w.provider.simulate_payment(order.order_id))
    return order


def test_events_are_mocked_and_complete(world):
    order = world.provider.create_order("Template", 1900)
    event = world.provider.simulate_payment(order.order_id)
    for key in ("event_id", "order_id", "amount", "fee", "currency"):
        assert key in event
    assert event["type"] == "payment.succeeded"
    assert event["mocked"] is True and event["livemode"] is False and "MOCKED" in event["label"]


def test_duplicate_event_delivers_once(world):
    order = world.provider.create_order("Template", 1900)
    event = world.provider.simulate_payment(order.order_id)
    first = world.handler.handle_event(event)
    second = world.handler.handle_event(world.provider.replay(event))
    assert first["status"] == "processed" and first["delivery"]["status"] == "granted"
    assert second["status"] == "ignored_duplicate"
    assert world.delivery.deliveries == 1
    assert world.delivery.has_access(order.order_id)
    actions = [e["action"] for e in world.logger.entries()]
    assert actions.count("grant_access") == 1
    assert "ignore_duplicate_event" in actions


def test_non_mocked_event_is_rejected(world):
    order = world.provider.create_order("Template", 1900)
    event = dict(world.provider.simulate_payment(order.order_id), mocked=False, livemode=True)
    assert world.handler.handle_event(event)["status"] == "rejected"
    assert world.delivery.deliveries == 0


def test_refund_with_founder_approval_revokes_access(world):
    order = _paid_order(world)
    approver = ScriptedYesApprover()
    ctx = SimpleNamespace(approver=approver, logger=world.logger)
    result = request_refund(order.order_id, "test", ctx, world.provider, world.handler)
    assert result["executed"] is True and result["status"] == "refunded"
    assert approver.calls and approver.calls[0][0] == "spend"
    assert not world.delivery.has_access(order.order_id)
    assert world.provider.orders[order.order_id].status == "refunded"
    granted = [e for e in world.logger.entries() if e["action"] == "execute_spend"]
    assert granted and granted[0]["approved_by"] == "founder"


@pytest.mark.parametrize("approver", [None, DenyAllApprover(), ScriptedNoApprover()])
def test_refund_without_approval_is_not_executed(world, approver):
    order = _paid_order(world)
    ctx = SimpleNamespace(approver=approver, logger=world.logger)
    result = request_refund(order.order_id, "test", ctx, world.provider, world.handler)
    assert result["executed"] is False
    assert world.delivery.has_access(order.order_id)
    assert world.provider.orders[order.order_id].status == "paid"
    assert order.order_id not in world.handler.refunds
    assert not any(e["action"] == "execute_spend" for e in world.logger.entries())


def test_provider_cannot_refund_by_itself(world):
    order = _paid_order(world)
    with pytest.raises(ApprovalRequired):
        world.provider.refund(order.order_id)
    assert world.delivery.has_access(order.order_id)


def test_reconciliation_finds_planted_mismatch(world):
    orders = [_paid_order(world, a) for a in (1900, 1900, 2900)]
    clean = world.provider.mock_payouts()
    assert reconcile(world.provider.orders, world.handler.payments, world.handler.refunds, clean)["ok"]
    planted = [dict(p) for p in clean]
    planted[2]["amount"] -= 150
    report = reconcile(world.provider.orders, world.handler.payments, world.handler.refunds, planted,
                       logger=world.logger)
    assert not report["ok"]
    assert [(m["type"], m["order_id"]) for m in report["mismatches"]] == [
        ("payout_amount_mismatch", orders[2].order_id)]


def test_reconciliation_after_refund_matches(world):
    order = _paid_order(world)
    ctx = SimpleNamespace(approver=ScriptedYesApprover(), logger=world.logger)
    request_refund(order.order_id, "test", ctx, world.provider, world.handler)
    report = reconcile(world.provider.orders, world.handler.payments, world.handler.refunds,
                       world.provider.mock_payouts())
    assert report["ok"], report["mismatches"]
    assert report["rows"][0]["expected_payout"] == -report["rows"][0]["fee"]


def test_every_log_entry_has_required_keys(world):
    _paid_order(world)
    entries = world.logger.entries()
    assert entries
    for entry in entries:
        validate_entry(entry)
        assert all(k in entry for k in REQUIRED_KEYS)


@pytest.mark.parametrize("approver,executed", [(ScriptedNoApprover(), False), (ScriptedYesApprover(), True)])
def test_payment_test_script_non_demo(tmp_path, approver, executed):
    import scripts.payment_test as pt

    out = []
    code = pt.main([], approver=approver, base_dir=tmp_path, echo=out.append)
    assert code == 0
    logs = list((tmp_path / "runs").glob("*/payment_test_log.md"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert text.splitlines()[0] == "MOCKED — no real payments"
    assert "PLANTED" in text and ("revoked" in text) == executed
    assert not (tmp_path / "outputs").exists() and not (tmp_path / "decision_log.json").exists()
    entries = json.loads(logs[0].with_name("decision_log.json").read_text(encoding="utf-8"))
    for entry in entries:
        validate_entry(entry)
    assert any(e["action"] == "ignore_duplicate_event" for e in entries)
    assert all("MOCKED" in line for line in out if line.startswith("["))


def _import_names(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0].lower() for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0].lower())
    return names


def test_no_payment_sdk_anywhere():
    for req in ROOT.glob("requirements*.txt"):
        for line in req.read_text(encoding="utf-8").splitlines():
            pkg = re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower()
            assert not any(sdk in pkg for sdk in PAYMENT_SDKS), f"{req.name}: {line}"
    skip = {".venv", "venv", ".git", "runs", "__pycache__", "node_modules"}
    for py in ROOT.rglob("*.py"):
        if skip & set(py.relative_to(ROOT).parts):
            continue
        bad = {n for n in _import_names(py) if any(sdk in n for sdk in PAYMENT_SDKS)}
        assert not bad, f"{py.relative_to(ROOT)} imports {bad}"
