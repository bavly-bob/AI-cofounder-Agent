"""Founder-gated functions cannot be reached undecorated through ``__wrapped__``."""
import pytest

from approval import ApprovalRequired, mock_publish, mock_send, requires_founder_approval
from payments import MockPaymentProvider

GATED = [
    ("mock_publish", mock_publish),
    ("mock_send", mock_send),
    ("MockPaymentProvider.refund", MockPaymentProvider.refund),
]


@pytest.mark.parametrize("name,fn", GATED, ids=[n for n, _ in GATED])
def test_no_wrapped_original_is_exposed(name, fn):
    assert not hasattr(fn, "__wrapped__"), f"{name} exposes the undecorated function"
    assert fn.requires_founder_approval in ("publish", "send", "spend")


@pytest.mark.parametrize("name,fn", GATED[:2], ids=[n for n, _ in GATED[:2]])
def test_direct_call_still_raises(name, fn):
    with pytest.raises(ApprovalRequired):
        fn({})


def test_provider_refund_still_raises_without_approval():
    provider = MockPaymentProvider()
    order = provider.create_order("Template (digital download)", 1900)
    provider.simulate_payment(order.order_id)
    with pytest.raises(ApprovalRequired):
        provider.refund(order.order_id)
    with pytest.raises(AttributeError):
        provider.refund.__wrapped__   # noqa: B018


def test_name_and_doc_are_kept():
    @requires_founder_approval("sign")
    def sign_contract(payload):
        """Signs a contract."""
    assert sign_contract.__name__ == "sign_contract"
    assert sign_contract.__doc__ == "Signs a contract."
    assert not hasattr(sign_contract, "__wrapped__")
