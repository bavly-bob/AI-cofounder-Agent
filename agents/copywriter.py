"""Step 2.3 landing_page (copywriter): one self-contained, honest waitlist page.

The HTML has inline CSS only: no scripts, no trackers, no outside files. The email form is
MOCKED: it has no action and a disabled button, so it submits nowhere, and it shows the
visible label "MOCKED — demo form". The page is checked by ``guardrails.check_landing_page``
before it is saved. "Publishing" goes only through ``guarded_action("publish", ...)`` and its
executor is MOCKED: it writes a copy to ``runs/<run_id>/published_mock/`` and nothing else.
"""
from __future__ import annotations

import html
from pathlib import Path

import guardrails
from agents.common import current_idea, fill, prompt_sections, safe_text, system_prompt
from agents.phase2_common import mocked_banner
from approval import requires_founder_approval
from steps import StepResult, register_step

ROLE = "copywriter"
MOCK_LABEL = "MOCKED — demo form"
STATUS_LINE = "In development — join the waitlist to hear when it's ready"
PAGE_FILE = "phase2_step3_landing_page.html"
SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"}, "headline": {"type": "string"}, "problem": {"type": "string"},
        "how_it_works": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "who_its_for": {"type": "string"},
    },
    "required": ["product_name", "headline", "problem", "how_it_works", "who_its_for"],
}
CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
line-height:1.5;color:#1d2330;background:#f7f8fa}main{max-width:40rem;margin:0 auto;padding:1.5rem 1.25rem}
h1{font-size:1.9rem;line-height:1.2;margin:.5rem 0 1rem}h2{font-size:1.15rem;margin:1.75rem 0 .5rem}
.status{display:inline-block;background:#fff4d6;border:1px solid #e8c766;border-radius:.4rem;padding:.35rem .6rem;
font-size:.95rem}.mock{background:#ffe1e1;border:2px dashed #c0392b;color:#8e1c12;font-weight:700;padding:.4rem .6rem;
border-radius:.4rem;margin-bottom:.75rem}form{background:#fff;border:1px solid #d9dde5;border-radius:.6rem;padding:1rem}
label{display:block;margin:.5rem 0}input[type=email]{width:100%;padding:.6rem;font-size:1rem;border:1px solid #b9c0cc;
border-radius:.4rem}button{margin-top:.75rem;padding:.6rem 1rem;font-size:1rem;border:0;border-radius:.4rem;
background:#8a93a3;color:#fff}.privacy{font-size:.9rem;color:#4a5263}footer{font-size:.8rem;color:#6b7383;
margin-top:2rem}@media (max-width:30rem){h1{font-size:1.5rem}main{padding:1rem}}"""


def build_html(copy: dict, run_id: str, model: str | None) -> str:
    e = lambda x: html.escape(str(x or ""), quote=True)   # noqa: E731
    steps = "".join(f"<li>{e(s)}</li>" for s in copy["how_it_works"])
    return f"""<!DOCTYPE html>
<!-- run_id: {e(run_id)} | model: {e(model or 'none')} | status: WORKING page, MOCKED form -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(copy['product_name'])}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<p class="status">{e(STATUS_LINE)}</p>
<h1>{e(copy['headline'])}</h1>
<h2>The problem</h2>
<p>{e(copy['problem'])}</p>
<h2>How it works</h2>
<ol>{steps}</ol>
<h2>Who it's for</h2>
<p>{e(copy['who_its_for'])}</p>
<h2>Join the waitlist</h2>
<p class="mock">{e(MOCK_LABEL)}: this form is not connected. It sends nothing and stores nothing.</p>
<form>
<label for="email">Email address</label>
<input id="email" name="email" type="email" autocomplete="email" required>
<label><input type="checkbox" name="consent" required> Yes, email me once when it is ready. I can unsubscribe at any time.</label>
<button type="submit" disabled>Join the waitlist (demo)</button>
</form>
<p class="privacy">Privacy note: only your email address is asked for. It would be used for one message when the product is ready and for nothing else: it is never sold or shared, and you can ask for it to be deleted at any time.</p>
<footer>{e(copy['product_name'])} is in development. Nothing is for sale yet.</footer>
</main>
</body>
</html>
"""


def check_page(page: str):
    """guardrails.check_landing_page when present; otherwise check_output on the visible text."""
    check = getattr(guardrails, "check_landing_page", None)
    if callable(check):
        return check(page), "check_landing_page"
    import re
    text = re.sub(r"<style>.*?</style>|<[^>]+>", " ", page, flags=re.S)
    return guardrails.check_output(html.unescape(text)), "check_output (fallback)"


@requires_founder_approval("publish")
def mock_publish_page(payload) -> dict:
    """MOCKED publish: writes a local copy under runs/<run_id>/published_mock/. Nothing leaves this machine."""
    dest = Path(payload["dest_dir"])
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "landing_page.html"
    path.write_text(payload["html"], encoding="utf-8", newline="\n")
    return {"status": "MOCKED", "kind": "publish", "path": f"{dest.name}/{path.name}",   # run-relative
            "detail": "MOCKED publish: a local copy was written; nothing was put online"}


def publish_payload(result, ctx) -> dict:
    run_dir = Path(ctx.options.get("run_dir") or Path(ctx.output_dir))
    return {"what": "landing page (step 2.3)", "html": (result.data or {}).get("html", ""),
            "dest_dir": str(run_dir / "published_mock"), "executor": "MOCKED: local copy only"}


def _fallback_copy(ctx) -> dict:
    opp = ctx.latest(1, 1)
    d = (opp.data if opp else {}) or {}
    return {"product_name": "Waitlist", "headline": "A simpler way to handle this problem",
            "problem": d.get("problem_statement") or current_idea(ctx),
            "how_it_works": ["Join the waitlist with your email.", "Hear once when the first version is ready.",
                             "Decide then whether it fits your work."],
            "who_its_for": d.get("target_user") or ctx.brief.target_market}


@register_step
class LandingPageStep:
    phase, step, name, agent = 2, 3, "landing_page", ROLE

    def default_gates(self) -> list:
        from gates import FounderApprovalGate
        return [FounderApprovalGate(
            "publish", "Publish the waitlist landing page (MOCKED executor: a local copy is written to "
            "runs/<run_id>/published_mock/; nothing is put online)", mock_publish_page, publish_payload,
            name="founder_approval_publish")]

    def run(self, ctx) -> StepResult:
        sections = prompt_sections(ROLE)
        opp, exp = ctx.latest(1, 1), ctx.latest(2, 2)
        d, x = (opp.data if opp else {}) or {}, (exp.data if exp else {}) or {}
        reply = ctx.llm_json(fill(sections["page"], idea=current_idea(ctx),
                                  who=d.get("target_user") or ctx.brief.target_market,
                                  problem=d.get("problem_statement") or current_idea(ctx),
                                  message=x.get("message") or ""),
                             SCHEMA, ROLE, system_prompt(sections), "copywriter.page")
        raw = reply["data"]
        copy = {k: safe_text(ctx, ROLE, raw.get(k), k) for k in ("product_name", "headline", "problem", "who_its_for")}
        copy["how_it_works"] = [s for s in (safe_text(ctx, ROLE, s, "how_it_works")
                                            for s in raw.get("how_it_works", [])[:3]) if s]
        fallback = _fallback_copy(ctx)
        for k, v in fallback.items():
            if not copy.get(k):
                copy[k] = v
        page = build_html(copy, ctx.run_id, reply.get("model"))
        verdict, checker = check_page(page)
        used_fallback = False
        if not verdict.allowed:
            ctx.logger.log("guardrail", "refuse_landing_page", outcome="blocked", category=verdict.category,
                           reason=verdict.reason, checker=checker, agent_refused=ROLE)
            page = build_html(fallback, ctx.run_id, reply.get("model"))
            verdict, checker = check_page(page)
            used_fallback = True
            if not verdict.allowed:
                raise RuntimeError(f"the landing page did not pass {checker} ({verdict.category})")
        ctx.logger.log("guardrail", "check_landing_page", outcome="allowed", checker=checker,
                       fallback_copy=used_fallback)
        lines = ["# Phase 2, step 3: landing page", "", *mocked_banner(ctx),
                 f"- Page: `{PAGE_FILE}` (one self-contained HTML file: inline CSS, no scripts, no trackers, "
                 "mobile friendly)",
                 f"- Status line on the page: \"{STATUS_LINE}\"",
                 f"- Waitlist form: MOCKED. It has no action and a disabled button, so it submits nowhere; it shows "
                 f"the visible label \"{MOCK_LABEL}\". Email only, with a consent checkbox and a privacy note.",
                 f"- Checked by `{checker}`: passed" + (" (with the neutral fallback copy)" if used_fallback else ""),
                 "- Publishing: only after the founder's approval, and the publish executor is MOCKED (a local "
                 "copy in `runs/<run_id>/published_mock/`). Nothing is put online.", "",
                 "## Page text", "", f"- Headline: {copy['headline']}", f"- Problem: {copy['problem']}",
                 "- How it works: " + " / ".join(copy["how_it_works"]), f"- Who it's for: {copy['who_its_for']}"]
        data = {"html": page, "copy": copy, "checker": checker, "fallback_copy": used_fallback,
                "extra_files": {PAGE_FILE: page}, "published": False}
        return StepResult(self.phase, self.step, self.name, self.agent, "\n".join(lines), [], data,
                          reply.get("model"), 0, reply.get("tokens"), "WORKING")
