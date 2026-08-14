"""
Turns LineItemDecision objects into an actual audit trail: a
JSON-serializable log for persistence, and a readable console report for
the demo. No new decisions get made here, everything rendered was already
computed by rules.py and ingestion.py, this module's only job is making
it visible.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from enum import Enum
from typing import Any, Sequence

from reconciliation.engine import LineItemDecision
from reconciliation.rules import RuleOutcome


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_serialize_value(v) for v in value]
    return value


def rule_outcome_to_dict(outcome: RuleOutcome) -> dict:
    return {
        "conflict_type": outcome.conflict_type.value,
        "conflict_detected": outcome.conflict_detected,
        "winner": outcome.winner.value,
        "resolved_value": _serialize_value(outcome.resolved_value),
        "confidence": outcome.confidence,
        "reasoning": outcome.reasoning,
        "reason_code": outcome.reason_code,
        "evidence_discarded": list(outcome.evidence_discarded),
        "triggers_quarantine": outcome.triggers_quarantine,
        "detail": outcome.detail,
    }


def decision_to_dict(decision: LineItemDecision) -> dict:
    return {
        "return_line_id": decision.return_line_id,
        "sku": decision.sku,
        "disposition": decision.disposition.value,
        "temporal_bucket": decision.temporal_bucket,
        "resolved_batch_code": decision.resolved_batch_code,
        "eligible_for_credit": decision.eligible_for_credit,
        "physical_quantity": decision.physical_quantity,
        "creditable_quantity": decision.creditable_quantity,
        "requires_human_review": decision.disposition.value == "quarantine",
        "rule_outcomes": [rule_outcome_to_dict(o) for o in decision.rule_outcomes],
    }


def write_audit_log(decisions: Sequence[LineItemDecision], path: str) -> None:
    """Persists the full shipment's decisions as a JSON array. This is the
    literal audit trail artifact, every rule outcome, every discarded
    piece of evidence, every confidence score, for every line item.
    """
    payload = [decision_to_dict(d) for d in decisions]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def render_decision_report(decision: LineItemDecision) -> str:
    """Human-readable, one line item. This is what the demo video's
    console output should look like: which source won each conflict, and
    why, in plain language, not just a JSON blob.
    """
    lines = [
        f"Return line {decision.return_line_id} (SKU {decision.sku})",
        f"  Disposition: {decision.disposition.value.upper()}",
        f"  Temporal bucket: {decision.temporal_bucket}",
        f"  Resolved batch code: {decision.resolved_batch_code or 'unresolved'}",
        f"  Eligible for credit: {decision.eligible_for_credit}",
        f"  Physical quantity: {decision.physical_quantity}",
        f"  Creditable quantity: {decision.creditable_quantity}"
        + (
            f"  ({decision.physical_quantity - decision.creditable_quantity} uncredited)"
            if decision.creditable_quantity < decision.physical_quantity
            else ""
        ),
        "  Rule-by-rule reasoning:",
    ]
    for outcome in decision.rule_outcomes:
        flag = " [CONFLICT]" if outcome.conflict_detected else " [agreement]"
        lines.append(
            f"    - {outcome.conflict_type.value}{flag}: "
            f"winner={outcome.winner.value}, confidence={outcome.confidence:.2f}, "
            f"reason={outcome.reason_code or 'n/a'}"
        )
        lines.append(f"      {outcome.reasoning}")
        candidates = outcome.detail.get("candidates") if outcome.detail else None
        if candidates:
            lines.append(f"      candidates considered (threshold {outcome.detail['threshold']:.0%}):")
            for candidate in candidates:
                lines.append(f"        {candidate['batch_code']} -> {candidate['confidence']:.1%}")
        if outcome.evidence_discarded:
            lines.append(f"      discarded: {', '.join(outcome.evidence_discarded)}")
    return "\n".join(lines)


def render_shipment_report(decisions: Sequence[LineItemDecision]) -> str:
    """All line items in a shipment, with a one-line summary up top."""
    counts = Counter(d.disposition.value for d in decisions)
    summary = f"Processed {len(decisions)} return line(s): " + ", ".join(
        f"{count} {disposition}" for disposition, count in counts.items()
    )
    body = "\n\n".join(render_decision_report(d) for d in decisions)
    return f"{summary}\n\n{body}"


# ---------------------------------------------------------------------------
# Boxed terminal report, presentation only, same data as the plain renderer
# above, no second source of truth, this just formats it differently for
# the demo video. Plain bracketed tags rather than checkmark/warning
# glyphs, on purpose: those glyphs render at inconsistent widths across
# terminals and fonts, exactly the thing that would misalign the box
# edges on camera without ever looking wrong on the machine that built it.
# ---------------------------------------------------------------------------

_BOX_WIDTH = 66  # interior width, excluding the two border characters


def _box_top() -> str:
    return "\u2554" + "\u2550" * _BOX_WIDTH + "\u2557"


def _box_bottom() -> str:
    return "\u255a" + "\u2550" * _BOX_WIDTH + "\u255d"


def _box_divider() -> str:
    return "\u2560" + "\u2550" * _BOX_WIDTH + "\u2563"


def _box_line(text: str = "") -> str:
    # truncate rather than let a long value break alignment, degrade
    # gracefully instead of failing, same principle as everywhere else
    truncated = text[: _BOX_WIDTH - 2]
    return "\u2551 " + truncated.ljust(_BOX_WIDTH - 2) + " \u2551"


def _box_centered(text: str) -> str:
    return "\u2551" + text.center(_BOX_WIDTH) + "\u2551"


def render_decision_box(decision: LineItemDecision) -> str:
    """A boxed terminal report for one line item. Presentation layer only,
    every value here comes straight from the decision already rendered by
    render_decision_report, this just formats it for visual impact.
    """
    has_conflicts = any(o.conflict_detected for o in decision.rule_outcomes)

    lines = [
        _box_top(),
        _box_centered("RETURNS RECONCILIATION AGENT"),
        _box_divider(),
        _box_line(f"Return: {decision.return_line_id}    SKU: {decision.sku}"),
        _box_divider(),
        _box_line("CONFLICTS DETECTED" if has_conflicts else "NO CONFLICTS"),
        _box_line(),
    ]
    for outcome in decision.rule_outcomes:
        status = "[CONFLICT]" if outcome.conflict_detected else "[ok]"
        label = outcome.conflict_type.value.replace("_", " ")
        lines.append(_box_line(f"{label:<28} {status}"))

    lines.append(_box_divider())
    lines.append(_box_line("FINAL DECISION"))
    lines.append(_box_line())
    lines.append(_box_line(f"ROUTE:        {decision.disposition.value.upper()}"))
    lines.append(_box_line(f"BATCH:        {decision.resolved_batch_code or 'unresolved'}"))
    lines.append(_box_line(f"BEST BEFORE:  {decision.temporal_bucket}"))
    lines.append(_box_line(f"PHYSICAL QTY: {decision.physical_quantity}"))
    lines.append(_box_line(f"CREDIT QTY:   {decision.creditable_quantity}"))
    lines.append(_box_bottom())
    return "\n".join(lines)


def render_shipment_box(decisions: Sequence[LineItemDecision]) -> str:
    """All line items, boxed, stacked. For a shipment with more than a
    handful of lines the plain render_shipment_report is more scannable,
    this is meant for walking through one or a few line items on camera.
    """
    return "\n\n".join(render_decision_box(d) for d in decisions)