"""
Renders a static, self-contained HTML case file from real
LineItemDecision output. This is a read-only observability layer: it
cannot influence reconciliation decisions, every value here is read
from an already-computed LineItemDecision, nothing is decided or
derived here. No server, no build step, no external font or script
dependency, opens correctly with no internet connection.

Design direction: dark, restrained, confident, matching the actual
stated design philosophy of the company this was built for ("decisions,
not dashboards"), not a generic AI-product dashboard skin. One accent
color per disposition, flat, no glow or gradient. Sans-serif throughout,
monospace strictly for literal data (batch codes, IDs, timestamps,
confidence, reason codes). Summary stats are plain numbers with small
labels, no icon cards.
"""

from __future__ import annotations

import html as _html
from typing import Sequence

from reconciliation.engine import LineItemDecision
from reconciliation.rules import RuleOutcome

_CSS = """
:root {
  --bg: #050607;
  --surface: #111417;
  --surface-raised: #15181C;
  --border: #23272C;
  --text: #F2F3F5;
  --text-muted: #868D97;
  --restock: #2FA871;
  --scrap: #E5484D;
  --quarantine: #D89B3C;
  --font-sans: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, monospace;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 56px 24px 90px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: 860px;
  margin: 0 auto;
}

header.masthead h1 {
  font-size: 36px;
  font-weight: 800;
  letter-spacing: -0.02em;
  margin: 0 0 8px;
}

header.masthead .subtitle {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-muted);
}

.stats {
  display: flex;
  flex-wrap: nowrap;
  margin: 40px 0 44px;
  padding: 0;
}

.stat {
  padding: 0 28px 0 0;
  margin-right: 28px;
  border-right: 1px solid var(--border);
  flex-shrink: 0;
}

.stat:last-child { border-right: none; margin-right: 0; }

.stat .stat-value {
  font-family: var(--font-mono);
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
}

.stat .stat-label {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 6px;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.determinism {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-muted);
  padding: 14px 0;
  margin-bottom: 44px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.determinism strong {
  color: var(--text);
  font-weight: 700;
}

.determinism.pass .status-dot { background: var(--restock); }
.determinism.fail .status-dot { background: var(--scrap); }

.status-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-right: 8px;
  position: relative;
  top: -1px;
}

.case {
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 4px;
  margin-bottom: 24px;
}

.case.restock { border-color: var(--restock); }
.case.scrap { border-color: var(--scrap); }
.case.quarantine { border-color: var(--quarantine); }

.case-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
}

.case-header .line-id {
  font-family: var(--font-mono);
  font-size: 17px;
  font-weight: 700;
}

.case-header .sku {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-muted);
  margin-top: 3px;
}

.disposition {
  font-family: var(--font-mono);
  font-size: 24px;
  font-weight: 700;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.disposition.restock { color: var(--restock); }
.disposition.scrap { color: var(--scrap); }
.disposition.quarantine { color: var(--quarantine); }

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  padding: 18px 24px;
  border-bottom: 1px solid var(--border);
}

.facts > div {
  padding: 6px 14px 6px 0;
}

.facts .fact-label {
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.facts .fact-value {
  font-family: var(--font-mono);
  font-size: 14px;
}

.ledger {
  padding: 4px 24px 20px;
}

.rule-row {
  padding: 16px 0;
  border-top: 1px solid var(--border);
}

.rule-row:first-child { border-top: none; }

.rule-row .rule-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  font-family: var(--font-mono);
  font-size: 12px;
  margin-bottom: 7px;
}

.rule-row .rule-head > * {
  margin-right: 14px;
  margin-bottom: 4px;
}

.rule-row .rule-name {
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text);
}

.status-tag {
  padding: 1px 8px;
  border-radius: 3px;
  font-weight: 700;
  letter-spacing: 0.04em;
  font-size: 11px;
}

.status-tag.conflict { color: var(--bg); background: var(--scrap); font-weight: 800; }
.status-tag.agreement { color: var(--text-muted); border: 1px solid var(--border); }

.rule-row .meta { color: var(--text-muted); }
.rule-row .reason-code { color: var(--text-muted); }

.rule-row .reasoning {
  font-family: var(--font-sans);
  font-size: 14px;
  color: var(--text);
  margin: 4px 0 0;
}

.rule-row .discarded {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 8px;
}

table.candidates {
  border-collapse: collapse;
  margin-top: 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  width: 100%;
  max-width: 480px;
}

table.candidates th {
  text-align: left;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  padding: 5px 12px 5px 0;
  font-weight: 500;
}

table.candidates td {
  padding: 6px 12px 6px 0;
  border-bottom: 1px solid var(--border);
}

table.candidates tr:last-child td { border-bottom: none; }

table.candidates .selected { color: var(--restock); font-weight: 700; }

footer.principle {
  margin-top: 48px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.7;
}

@media (max-width: 480px) {
  .case-header { flex-direction: column; }
  .disposition { margin-top: 10px; }
  .stat { margin-right: 20px; padding-right: 20px; }
}
"""


def _escape(value) -> str:
    if value is None:
        return "unresolved"
    return _html.escape(str(value))


def _disposition_class(disposition_value: str) -> str:
    return {"restock": "restock", "scrap": "scrap", "quarantine": "quarantine"}.get(
        disposition_value, "quarantine"
    )


def _candidate_table(detail: dict, resolved_value) -> str:
    candidates = detail.get("candidates") if detail else None
    if not candidates:
        return ""
    threshold = detail.get("threshold", 0.0)
    rows = []
    for candidate in candidates:
        code = candidate.get("batch_code")
        confidence = candidate.get("confidence", 0.0)
        selected = code == resolved_value
        label = "SELECTED" if selected else "rejected"
        row_class = ' class="selected"' if selected else ""
        rows.append(
            f"<tr><td{row_class}>{_escape(code)}</td>"
            f"<td{row_class}>{confidence:.1%}</td>"
            f"<td{row_class}>{label}</td></tr>"
        )
    return (
        '<table class="candidates">'
        f"<tr><th>Candidate</th><th>Similarity</th><th>Status (threshold {threshold:.0%})</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _rule_row(outcome: RuleOutcome, resolved_value) -> str:
    status_class = "conflict" if outcome.conflict_detected else "agreement"
    status_label = "CONFLICT" if outcome.conflict_detected else "agreement"
    label = outcome.conflict_type.value.replace("_", " ")

    parts = [
        '<div class="rule-row">',
        '<div class="rule-head">',
        f'<span class="rule-name">{_escape(label)}</span>',
        f'<span class="status-tag {status_class}">{status_label}</span>',
        f'<span class="meta">winner={_escape(outcome.winner.value)}</span>',
        f'<span class="meta">confidence={outcome.confidence:.2f}</span>',
        f'<span class="reason-code">{_escape(outcome.reason_code or "n/a")}</span>',
        "</div>",
        f'<p class="reasoning">{_escape(outcome.reasoning)}</p>',
    ]
    if outcome.evidence_discarded:
        parts.append(
            f'<div class="discarded">discarded: {_escape(", ".join(outcome.evidence_discarded))}</div>'
        )
    candidate_html = _candidate_table(outcome.detail, resolved_value)
    if candidate_html:
        parts.append(candidate_html)
    parts.append("</div>")
    return "".join(parts)


def _case_card(decision: LineItemDecision) -> str:
    disposition_value = decision.disposition.value
    uncredited = decision.physical_quantity - decision.creditable_quantity
    credit_note = f" ({uncredited} uncredited)" if uncredited > 0 else ""

    facts = [
        ("Best before", decision.temporal_bucket),
        ("Batch code", decision.resolved_batch_code),
        ("Physical qty", decision.physical_quantity),
        ("Creditable qty", f"{decision.creditable_quantity}{credit_note}"),
        ("Credit eligible", decision.eligible_for_credit),
    ]
    facts_html = "".join(
        f'<div><div class="fact-label">{_escape(label)}</div>'
        f'<div class="fact-value">{_escape(value)}</div></div>'
        for label, value in facts
    )

    rules_html = "".join(
        _rule_row(outcome, decision.resolved_batch_code) for outcome in decision.rule_outcomes
    )

    return (
        f'<section class="case {_disposition_class(disposition_value)}">'
        '<div class="case-header">'
        "<div>"
        f'<div class="line-id">{_escape(decision.return_line_id)}</div>'
        f'<div class="sku">SKU {_escape(decision.sku)}</div>'
        "</div>"
        f'<span class="disposition {_disposition_class(disposition_value)}">{_escape(disposition_value)}</span>'
        "</div>"
        f'<div class="facts">{facts_html}</div>'
        f'<div class="ledger">{rules_html}</div>'
        "</section>"
    )


def render_html_report(
    decisions: Sequence[LineItemDecision], determinism_verified: bool
) -> str:
    """Builds the full page. Pure rendering, every value comes from an
    already-computed LineItemDecision (or the already-computed boolean
    result of verify_determinism), nothing is decided here.
    """
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.disposition.value] = counts.get(decision.disposition.value, 0) + 1

    conflict_count = sum(
        1 for d in decisions for o in d.rule_outcomes if o.conflict_detected
    )

    stats = [(str(len(decisions)), "return lines"), (str(conflict_count), "conflicts resolved")]
    for label in ("restock", "scrap", "quarantine"):
        if label in counts:
            stats.append((str(counts[label]), label))

    stats_html = "".join(
        f'<div class="stat"><div class="stat-value">{_escape(value)}</div>'
        f'<div class="stat-label">{_escape(label)}</div></div>'
        for value, label in stats
    )

    determinism_class = "pass" if determinism_verified else "fail"
    determinism_text = (
        "<strong>Verified deterministic.</strong> process_return executed twice against "
        "identical input, results compared field by field, outcome identical."
        if determinism_verified
        else "<strong>Non-deterministic result detected.</strong> Two runs against identical "
        "input produced different decisions."
    )

    cases_html = "".join(_case_card(d) for d in decisions)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Returns Reconciliation</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
<header class="masthead">
<h1>Returns Reconciliation</h1>
<div class="subtitle">Generated directly from LineItemDecision output. Read-only, cannot influence any decision above.</div>
</header>
<div class="stats">{stats_html}</div>
<div class="determinism {determinism_class}">
<span class="status-dot"></span>{determinism_text}
</div>
{cases_html}
<footer class="principle">
The decision engine is deterministic by design (ADR-001): reproducible on rerun,
testable per rule, explainable by citing the rule that fired. This page is a
read-only observability layer over that engine's output. It renders decisions,
it does not make them.
</footer>
</div>
</body>
</html>"""


def write_html_report(
    decisions: Sequence[LineItemDecision], determinism_verified: bool, path: str
) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html_report(decisions, determinism_verified))