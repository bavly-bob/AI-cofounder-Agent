"""M5 privacy: the decision log never keeps emails, phone numbers, keys or card numbers.

Fake secrets are built from pieces so that this file itself never contains one
(scripts/scan_secrets.py scans every tracked file).
"""
import json

import decision_log
from decision_log import DecisionLogger, scrub, scrub_text

FAKE_OPENAI = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_GH = "ghp_" + "a" * 30
CARD = "4242 4242 " + "4242 4242"          # the well-known test card number (passes Luhn)
EMAIL = "sara.ahmed" + "@" + "example.com"


def test_scrub_text_removes_personal_data_and_keys():
    text = (f"Contact {EMAIL} or +20 10 1234 5678 or 01012345678 or (555) 123-4567. "
            f"key {FAKE_OPENAI}, aws {FAKE_AWS}, gh {FAKE_GH}, card {CARD}, "
            "api_key=abcd1234efgh password: hunter22 Authorization: Bearer abcdefghijklmnop123")
    out = scrub_text(text)
    for secret in (EMAIL, "1234 5678", "01012345678", "123-4567", FAKE_OPENAI, FAKE_AWS, FAKE_GH, CARD,
                   "abcd1234efgh", "hunter22", "abcdefghijklmnop123"):
        assert secret not in out, secret
    for label in ("[REDACTED_EMAIL]", "[REDACTED_PHONE]", "[REDACTED_KEY]", "[REDACTED_CARD]", "[REDACTED_SECRET]"):
        assert label in out


def test_scrub_keeps_ordinary_text_numbers_dates_and_hashes():
    text = ("Target: signups >= 100 in a 7-day window; actual 12 (12% of the target). "
            "Run 20261117T090100Z-abc123 at 2026-11-17T09:01:00Z, cost $0.00, 1,200 users (ASSUMPTION). "
            "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 "
            "https://www.reddit.com/r/smallbusiness/ order 1234567890123 is not Luhn-valid")
    assert scrub_text(text) == text


def test_scrub_walks_nested_values_and_cuts_long_prompts():
    data = {"prompt": "word " * 1000 + EMAIL, "nested": {"list": [EMAIL, 3, None, True]}, "n": 12.5}
    out = scrub(data)
    assert out["n"] == 12.5 and out["nested"]["list"][1:] == [3, None, True]
    assert out["nested"]["list"][0] == "[REDACTED_EMAIL]"
    assert len(out["prompt"]) < 2100 and "truncated" in out["prompt"]


def test_logger_scrubs_extras_but_keeps_required_keys(tmp_path):
    logger = DecisionLogger(tmp_path / "decision_log.json", run_id="r1")
    entry = logger.log("researcher", "llm_call", sources=["https://example.com/a"], model="qwen3:8b",
                       prompt=f"Email the founder at {EMAIL}, call +20 10 1234 5678",
                       response=f"use {FAKE_OPENAI}", error=f"card {CARD} rejected")
    stored = json.loads((tmp_path / "decision_log.json").read_text(encoding="utf-8"))[0]
    raw = (tmp_path / "decision_log.json").read_text(encoding="utf-8")
    for secret in (EMAIL, "1234 5678", FAKE_OPENAI, CARD):
        assert secret not in raw
    assert stored == entry
    assert (stored["agent"], stored["action"], stored["sources"], stored["model"], stored["cost"],
            stored["approved_by"]) == ("researcher", "llm_call", ["https://example.com/a"], "qwen3:8b", 0, None)
    assert "[REDACTED_EMAIL]" in stored["prompt"] and "[REDACTED_PHONE]" in stored["prompt"]


def test_module_logger_also_scrubs(tmp_path):
    decision_log.configure(tmp_path / "log.json", run_id="r2", reset=True)
    try:
        decision_log.log("brief_parser", "llm_call", text=f"my email is {EMAIL}")
        assert EMAIL not in (tmp_path / "log.json").read_text(encoding="utf-8")
    finally:
        decision_log._default = None
