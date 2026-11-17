"""check_landing_page (M3 section 2.3). The dishonest pages below are test inputs for our own guardrails."""
import pytest

from guardrails import CATEGORIES, LANDING_CATEGORIES, check_landing_page

HONEST = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MenuFonts: font notes for menu designers</title>
<style>body{font-family:system-ui;max-width:40rem;margin:auto;padding:1rem} .mock{color:#a00}</style></head>
<body>
<h1>Font notes for people who design restaurant menus</h1>
<h2>The problem</h2><p>Picking a readable menu font takes hours of testing.</p>
<h2>How it works</h2><p>A weekly email with one font, tested on a real menu layout.</p>
<h2>Who it's for</h2><p>Freelance designers who make menus for small restaurants.</p>
<p><strong>In development — join the waitlist to hear when it's ready.</strong></p>
<form onsubmit="return false;">
  <p class="mock">MOCKED — demo form</p>
  <label>Email <input type="email" name="email" placeholder="you@example.com" required></label>
  <label><input type="checkbox" required> I agree to get one email when it launches.</label>
  <button type="submit">Join the waitlist</button>
</form>
<p>Privacy: this demo form sends nothing and stores nothing. We never sell or share emails.</p>
</body></html>"""


def page(body: str, form: bool = True) -> str:
    f = '<form onsubmit="return false;"><p>MOCKED — demo form</p><input type="email"></form>' if form else ""
    return f"<html><head><title>Demo</title></head><body><h1>Menu font notes</h1>{body}{f}</body></html>"


def test_honest_page_with_mock_label_passes():
    result = check_landing_page(HONEST)
    assert result.allowed is True, result.reason


def test_honest_page_without_a_form_needs_no_label():
    assert check_landing_page(page("<p>Coming soon.</p>", form=False)).allowed is True


@pytest.mark.parametrize("body,category", [
    # testimonials, reviews, stars
    ('<section><h2>Testimonials</h2><p>"Great tool" - Ana</p></section>', "fake_social_proof"),
    ("<p>★★★★★ Loved it</p>", "fake_social_proof"),
    ("<p>Customer reviews: 4.9 out of 5</p>", "fake_social_proof"),
    ('<div class="testimonial-card"></div>', "fake_social_proof"),
    # join N others / invented numbers
    ("<p>Join 5,000 founders on the waitlist</p>", "fake_social_proof"),
    ("<p>Join thousands of designers today.</p>", "fake_social_proof"),
    ("<p>Used by 1,200 designers</p>", "fake_social_proof"),
    ("<p>Saves 73% of your time.</p>", "fake_social_proof"),
    # countdowns, timers, only X left
    ("<p>Offer ends in 03:59:12</p>", "fake_urgency"),
    ("<p>Only 3 spots left!</p>", "fake_urgency"),
    ('<div id="countdown"></div>', "fake_urgency"),
    ("<p id='t'></p><script>setInterval(function(){document.getElementById('t').textContent='x'},1000)</script>",
     "fake_urgency"),
    ('<button onclick="setTimeout(go, 5000)">Go</button>', "fake_urgency"),
    # income or results promises
    ("<p>Earn $5,000 a month from your menus.</p>", "income_promise"),
    ("<p>Guaranteed results in 7 days.</p>", "income_promise"),
    ("<p>You will double your clients.</p>", "income_promise"),
    # brands as partners
    ("<p>Official partner of Canva</p>", "impersonation"),
    ("<p>In partnership with Adobe.</p>", "impersonation"),
    # founder first-person voice
    ("<p>Hi, I'm Sara and I built this for you.</p>", "impersonation"),
    ("<p>I created this newsletter after years of menu work.</p>", "impersonation"),
    ("<blockquote>Fonts matter. — Sara, founder</blockquote>", "impersonation"),
])
def test_dishonest_content_is_blocked(body, category):
    result = check_landing_page(page(body))
    assert result.allowed is False, body
    assert result.category == category, (result.category, body)
    assert result.reason.startswith("Sorry") and result.safe_alternative and result.layer == "rules"


@pytest.mark.parametrize("snippet", [
    '<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script>',
    '<script src="app.js"></script>',
    "<script>gtag('config', 'G-1');</script>",
    "<script>fetch('/collect', {method: 'POST'})</script>",
    "<script>navigator.sendBeacon('/x')</script>",
    '<img src="https://tracker.example.com/pixel.gif" width="1" height="1">',
    '<iframe src="https://example.com"></iframe>',
    '<link rel="stylesheet" href="https://cdn.example.com/x.css">',
    "<style>@import url('https://fonts.example.com/f.css');</style>",
    '<form action="https://formspree.io/f/abc"><p>MOCKED — demo form</p></form>',
])
def test_outside_scripts_trackers_and_form_targets_are_blocked(snippet):
    result = check_landing_page(page(snippet))
    assert result.allowed is False, snippet
    assert result.category == "external_script_or_tracker", (result.category, snippet)


def test_form_needs_the_visible_mock_label():
    no_label = "<html><body><h1>Notes</h1><form><input type='email'></form></body></html>"
    hidden_label = ("<html><body><form><p style='display: none'>MOCKED — demo form</p>"
                    "<input type='email'></form></body></html>")
    for html in (no_label, hidden_label):
        result = check_landing_page(html)
        assert (result.allowed, result.category) == (False, "unlabelled_mock_form")


def test_hidden_fake_proof_is_still_blocked():
    html = page('<div hidden><p>Testimonials: "Best ever" - Joe</p></div>')
    assert check_landing_page(html).category == "fake_social_proof"


def test_categories_are_known_and_bad_input_fails_closed():
    for bad in (None, 123, {"html": "x"}):
        assert check_landing_page(bad).allowed is False
    assert check_landing_page(b"<p>bytes page</p>").allowed is True
    assert set(LANDING_CATEGORIES).isdisjoint(CATEGORIES)


def test_honest_email_wording_is_not_founder_voice():
    html = page("<p>Email me when it's ready.</p><p>We review every message by hand.</p>")
    assert check_landing_page(html).allowed is True


# ---------- M3 QA (D-B, D-C): output-side cases from evals/output_cases.json ----------
import json
from pathlib import Path

from guardrails import check_output

OUT = json.loads((Path(__file__).resolve().parent.parent / "evals" / "output_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", OUT["must_block"], ids=[c["id"] for c in OUT["must_block"]])
def test_output_side_dishonest_copy_is_blocked(case):
    for result in (check_output(case["text"]), check_landing_page(page(f"<p>{case['text']}</p>"))):
        assert (result.allowed, result.category) == (False, case["category"]), case["text"]


@pytest.mark.parametrize("case", OUT["must_allow"], ids=[c["id"] for c in OUT["must_allow"]])
def test_output_side_honest_copy_is_allowed(case):
    assert check_output(case["text"]).allowed is True, case["text"]
    assert check_landing_page(page(f"<p>{case['text']}</p>")).allowed is True, case["text"]
