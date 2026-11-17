"""Agents, one module per role. Importing this package registers the steps:
Phase 1: 1.1 opportunities (researcher), 1.2 idea_scoring (scorer), 1.3 market (market_analyst),
1.4 interviews and 1.5 outreach (interviewer).
Phase 2: 2.1 lean_canvas (strategist), 2.2 experiment (experimenter), 2.3 landing_page (copywriter),
2.4 demand_results (analyst)."""
from agents import interviewer, market_analyst, researcher, scorer  # noqa: F401  (registers the steps)
from agents import analyst, copywriter, experimenter, strategist  # noqa: F401  (registers the Phase 2 steps)
