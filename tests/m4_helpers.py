"""Offline test steps for the M4 tests (STUBBED: no model, no network)."""
from __future__ import annotations

from brief import FounderBrief
from gates import EvidenceThresholdGate, FounderApprovalGate, StubPassThroughGate
from steps import StepResult

PUBLISHED = []   # every call of the test executor; it must stay empty unless the founder approved


def counting_publish(payload) -> dict:
    """MOCKED test executor: records the call, publishes nothing."""
    PUBLISHED.append(payload)
    return {"status": "MOCKED", "kind": "publish"}


class StubDiscover:
    phase, step, name, agent = 1, 1, "opportunities", "test_discover"

    def run(self, ctx):
        return StepResult(1, 1, self.name, self.agent, "# STUBBED test step", [], {}, None, 0, 5, "STUBBED")

    def default_gates(self):
        return [StubPassThroughGate()]


class StubValidate:
    phase, step, name, agent = 2, 1, "validation", "test_validate"

    def run(self, ctx):
        value = ctx.options.get("metric")
        metrics = {} if value is None else {"signups": {"value": float(value), "source": "MOCKED: test",
                                                        "mocked": True}}
        return StepResult(2, 1, self.name, self.agent, "# STUBBED test step (MOCKED metric)", [],
                          {"metrics": metrics}, None, 0, 7, "STUBBED")

    def default_gates(self):
        return [EvidenceThresholdGate("signups", 100, ">=", "MOCKED test window"),
                FounderApprovalGate("publish", "Publish the test offer page (MOCKED)", counting_publish)]


def test_steps():
    return [StubDiscover(), StubValidate()]


test_steps.__test__ = False


def brief() -> FounderBrief:
    return FounderBrief(idea="A printable weekly meal-planning template pack for busy parents",
                        skills=["design"], budget_usd=100, time_per_week_hours=5,
                        target_market="busy parents")
