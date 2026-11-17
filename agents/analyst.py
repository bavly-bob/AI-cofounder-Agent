"""Step 2.4 demand_results (analyst): sign-ups vs the pre-registered target, conversion, days elapsed.

Evidence, in priority order:
1. ``--waitlist path.csv``: a real export. Rows in the window are counted; duplicates and
   invalid emails are dropped. Emails are NEVER written anywhere: only aggregate counts.
2. ``--signups N --visitors V``: founder-reported.
3. ``--mock-signups N --mock-visitors V``: MOCKED, demonstration only (labelled everywhere).
4. Nothing: no evidence; the Phase 2 gate says STOP ("demand test not run yet").
No model is used: the numbers and the plain interpretation are computed in Python (no spin).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.phase2_common import MOCKED_EVIDENCE, load_phase2
from steps import StepResult, register_step

ROLE = "analyst"
MAX_CSV_BYTES = 5_000_000
MAX_ROWS = 100_000
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_DATE_COLS = ("date", "time", "created", "signed", "joined", "subscribed", "timestamp")


def _parse_time(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def count_waitlist(path, window_start=None, window_days: int = 14) -> dict:
    """Aggregate counts from a waitlist CSV. The returned dict never contains an email address."""
    raw = Path(path).read_bytes()[:MAX_CSV_BYTES].decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(raw))
    rows = [r for _, r in zip(range(MAX_ROWS + 1), reader)]
    counts = {"rows": 0, "valid": 0, "invalid_emails": 0, "duplicates": 0, "outside_window": 0, "undated": 0,
              "counted": 0, "truncated": len(rows) > MAX_ROWS}
    if not rows:
        return counts
    header = [h.strip().lower() for h in rows[0]]
    has_header = any("mail" in h for h in header)
    email_col = next((i for i, h in enumerate(header) if "mail" in h), 0) if has_header else 0
    date_col = next((i for i, h in enumerate(header) if any(k in h for k in _DATE_COLS)), None) if has_header else None
    body = rows[1:MAX_ROWS + 1] if has_header else rows[:MAX_ROWS]
    end = window_start + timedelta(days=window_days) if window_start else None
    seen = set()
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        counts["rows"] += 1
        email = row[email_col].strip().lower() if email_col < len(row) else ""
        if not _EMAIL.match(email):
            counts["invalid_emails"] += 1
            continue
        if email in seen:
            counts["duplicates"] += 1
            continue
        seen.add(email)
        counts["valid"] += 1
        when = _parse_time(row[date_col]) if date_col is not None and date_col < len(row) else None
        if when is None:
            counts["undated"] += 1
        elif window_start and not (window_start <= when <= end):
            counts["outside_window"] += 1
            continue
        counts["counted"] += 1
    seen.clear()
    return counts


def gather_evidence(ctx, target_record) -> dict:
    """The best available evidence, as aggregate numbers only."""
    o = ctx.options
    approved_at = _parse_time((target_record or {}).get("approved_at"))
    window = int(((target_record or {}).get("target") or {}).get("window_days") or load_phase2()["window_days"])
    days = max(0, (datetime.now(timezone.utc) - approved_at).days) if approved_at else None
    ev = {"type": "none", "signups": None, "visitors": None, "conversion": None, "days_elapsed": days,
          "window_days": window, "mocked": False, "details": {}}
    if o.get("waitlist_path"):
        try:
            counts = count_waitlist(o["waitlist_path"], approved_at, window)
        except OSError as e:
            ctx.logger.log(ROLE, "waitlist_unreadable", error=type(e).__name__)
            counts = None
        if counts is not None:
            ev.update(type="waitlist_csv", signups=counts["counted"], details=counts)
            if o.get("visitors") is not None:
                ev["visitors"] = o["visitors"]
            ctx.logger.log(ROLE, "count_waitlist", **{k: v for k, v in counts.items()},
                           note="aggregate counts only; no email address is kept or logged")
    if ev["type"] == "none" and o.get("signups") is not None:
        ev.update(type="founder_reported", signups=int(o["signups"]), visitors=o.get("visitors"))
    if ev["type"] == "none" and o.get("mock_signups") is not None:
        ev.update(type="mocked", signups=int(o["mock_signups"]), visitors=o.get("mock_visitors"), mocked=True)
    if ev["signups"] is not None and ev["visitors"]:
        ev["conversion"] = round(ev["signups"] / ev["visitors"], 4)
    return ev


def interpret(ev: dict, target: dict | None) -> list:
    """Plain sentences, no spin."""
    if ev["signups"] is None:
        return ["No sign-up data was supplied, so the demand test has not been run yet. Nothing can be "
                "concluded about demand."]
    if not target:
        return ["There is no approved target to compare with, so the numbers cannot be judged."]
    share = ev["signups"] / target["signups"] if target["signups"] else 0
    out = [f"{ev['signups']} sign-ups against a target of {target['signups']} ({share:.0%} of the target)."]
    if ev["conversion"] is None:
        out.append("Conversion could not be measured because no visitor count was given.")
    else:
        word = "meets" if ev["conversion"] >= target["min_conversion"] else "is below"
        out.append(f"Conversion was {ev['conversion']:.1%}, which {word} the minimum of "
                   f"{target['min_conversion']:.0%} set before the test.")
    if share >= 1:
        out.append("The sign-up number meets the target that was set in advance.")
    elif share >= 0.5:
        out.append("This is below the target that was set in advance. It is a miss, not a partial success.")
    else:
        out.append("This is less than half of the target that was set in advance: the evidence of demand is weak.")
    if ev.get("days_elapsed") is not None and ev["days_elapsed"] < ev["window_days"]:
        out.append(f"The test window has not ended (day {ev['days_elapsed']} of {ev['window_days']}), so this is "
                   "an interim result.")
    if ev["mocked"]:
        out.insert(0, f"{MOCKED_EVIDENCE}: these numbers were typed in for a demo. They say nothing about real "
                      "demand.")
    return out


@register_step
class DemandResultsStep:
    phase, step, name, agent = 2, 4, "demand_results", ROLE
    repeat_from = (2, 2)   # a Phase 2 REPEAT designs a new experiment (changed channel or message)

    def default_gates(self) -> list:
        from gates import Phase2DemandGate
        return [Phase2DemandGate()]

    def run(self, ctx) -> StepResult:
        record = ctx.options.get("experiment_target")
        target = (record or {}).get("target")
        ev = gather_evidence(ctx, record)
        label = {"waitlist_csv": "waitlist export (CSV): aggregate counts only, no email kept",
                 "founder_reported": "founder-reported (typed in by the founder; not verified by the system)",
                 "mocked": MOCKED_EVIDENCE, "none": "none: no data supplied"}[ev["type"]]
        lines = ["# Phase 2, step 4: demand results", ""]
        if ev["mocked"]:
            lines += [f"**{MOCKED_EVIDENCE}.** The numbers below were typed in with `--mock-signups`. They are not "
                      "real sign-ups, and a GO on them is not a real validation.", ""]
        lines += [f"- Evidence type: {label}"]
        if target:
            lines += [f"- Pre-registered target: >= {target['signups']} sign-ups in {target['window_days']} days, "
                      f"conversion >= {target['min_conversion']:.0%} (approved {record.get('approved_at')}, "
                      f"SHA-256 `{record.get('sha256')}`)"]
        else:
            lines += ["- Pre-registered target: none approved"]
        lines += [f"- Sign-ups: {ev['signups'] if ev['signups'] is not None else 'no data'}",
                  f"- Visitors: {ev['visitors'] if ev['visitors'] is not None else 'no data'}",
                  "- Conversion: " + (f"{ev['conversion']:.1%}" if ev["conversion"] is not None else "not measured"),
                  f"- Days elapsed since the target was approved: "
                  f"{ev['days_elapsed'] if ev['days_elapsed'] is not None else 'unknown'} of {ev['window_days']}"]
        if ev["type"] == "waitlist_csv":
            d = ev["details"]
            lines += [f"- CSV rows: {d['rows']}; invalid emails dropped: {d['invalid_emails']}; duplicates dropped: "
                      f"{d['duplicates']}; outside the window: {d['outside_window']}; undated (counted): "
                      f"{d['undated']}"]
        lines += ["", "## Plain interpretation", "", *[f"- {s}" for s in interpret(ev, target)]]
        data = {"evidence": {k: v for k, v in ev.items() if k != "details"}, "counts": ev["details"]}
        return StepResult(self.phase, self.step, self.name, self.agent, "\n".join(lines), [], data,
                          None, 0, None, "MOCKED" if ev["mocked"] else "WORKING")
