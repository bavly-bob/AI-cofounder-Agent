"""MOCKED delivery: a payment grants access to the digital product exactly once; a refund revokes it.

Access is a MOCKED license key (a random token held in memory). Nothing is
emailed, uploaded or downloaded, and no buyer data is stored: a grant is keyed by
``order_id`` only.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from decision_log import utc_timestamp


@dataclass
class Grant:
    order_id: str
    license_key: str
    granted_by_event: str
    granted_at: str
    active: bool = True
    revoked_by_event: str | None = None
    revoked_at: str | None = None


@dataclass
class DeliveryService:
    """MOCKED delivery store (in memory)."""

    logger: object = None
    grants: dict = field(default_factory=dict)
    deliveries: int = 0   # how many license keys were ever issued

    def _log(self, action: str, **extra) -> None:
        if self.logger is not None:
            self.logger.log("delivery", action, sources=[], model=None, cost=0,
                            approved_by=None, mocked=True, **extra)

    def grant(self, order_id: str, event_id: str) -> dict:
        """Issue a MOCKED license key for ``order_id``, at most once per order."""
        existing = self.grants.get(order_id)
        if existing is not None:
            self._log("grant_skipped_already_issued", order_id=order_id, event_id=event_id,
                      outcome="skipped", active=existing.active)
            return {"status": "skipped", "order_id": order_id, "reason": "access was already issued once"}
        key = "MOCK-LIC-" + secrets.token_hex(6).upper()
        self.grants[order_id] = Grant(order_id, key, event_id, utc_timestamp())
        self.deliveries += 1
        self._log("grant_access", order_id=order_id, event_id=event_id, outcome="granted")
        return {"status": "granted", "order_id": order_id, "license_key": key, "mocked": True}

    def revoke(self, order_id: str, event_id: str) -> dict:
        grant = self.grants.get(order_id)
        if grant is None or not grant.active:
            self._log("revoke_skipped", order_id=order_id, event_id=event_id, outcome="skipped",
                      reason="no active access")
            return {"status": "skipped", "order_id": order_id, "reason": "no active access"}
        grant.active = False
        grant.revoked_by_event = event_id
        grant.revoked_at = utc_timestamp()
        self._log("revoke_access", order_id=order_id, event_id=event_id, outcome="revoked")
        return {"status": "revoked", "order_id": order_id, "mocked": True}

    def has_access(self, order_id: str) -> bool:
        grant = self.grants.get(order_id)
        return bool(grant and grant.active)
