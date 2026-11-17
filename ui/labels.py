"""Text shown on every page: what is MOCKED or STUBBED, phase names, outcome colours."""

MOCKED_BANNER = (
    "**MOCKED / STUBBED in this app:** "
    "Phases 3-6 are STUBBED (see ARCHITECTURE.md). "
    "Publish, send, spend and sign executors are MOCKED: nothing leaves this machine. "
    "Payments, delivery, refunds and reconciliation are MOCKED (no real money). "
    "Mock evidence numbers are MOCKED — demo only, not a real validation. "
    "Steps that are still placeholders say STUBBED on their output."
)

PHASES = {
    1: "Discover",
    2: "Validate",
    3: "Build",
    4: "Commercial setup",
    5: "Sell, deliver, get paid",
    6: "Operate and learn",
}
LIVE_PHASES = (1, 2)

OUTCOME_COLOUR = {"go": "#1b7f3b", "repeat": "#b26a00", "pivot": "#8a4fbf", "stop": "#c62828"}

STATUS_TEXT = {
    "running": "Running",
    "awaiting_approval": "Waiting for the founder",
    "stopped": "Stopped",
    "finished": "Finished",
}

PROJECTOR_CSS = """
<style>
html, body, [class*="css"] { font-size: 19px; }
.stMarkdown p, .stMarkdown li { font-size: 1.05rem; line-height: 1.5; }
.badge { display: inline-block; padding: 0.15rem 0.7rem; border-radius: 0.6rem; color: #fff;
         font-weight: 700; letter-spacing: 0.03em; }
.mocked { color: #c62828; font-weight: 700; }
</style>
"""


def badge(outcome: str | None) -> str:
    word = (outcome or "n/a").lower()
    colour = OUTCOME_COLOUR.get(word, "#555")
    return f'<span class="badge" style="background:{colour}">{word.upper()}</span>'
