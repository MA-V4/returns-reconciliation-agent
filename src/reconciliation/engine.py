"""
Aggregates the five rule outcomes (rules.py) into one total decision per
return_line_id. This is the ADR-006 boundary: reconcile_line_item must
never raise and must always return exactly one Disposition, whatever the
input looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from reconciliation.rules import (
    ConflictType,
    RuleOutcome,
    Winner,
    resolve_batch_code,
    resolve_best_before,
    resolve_condition,
    resolve_eligibility,
    resolve_quantity,
)
from reconciliation.schemas import (
    BatchRegistryEntry,
    Disposition,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)

UNRESOLVED_BUCKET = "unknown_pending_review"


@dataclass(frozen=True)
class LineItemDecision:
    return_line_id: str
    sku: str
    disposition: Disposition
    temporal_bucket: str
    resolved_batch_code: Optional[str]
    eligible_for_credit: Optional[bool]
    physical_quantity: int
    creditable_quantity: int
    overall_confidence: float
    rule_outcomes: tuple[RuleOutcome, ...]


def reconcile_line_item(
    warehouse: WarehouseInspectionRecord,
    supplier: SupplierCreditNote,
    registry: Sequence[BatchRegistryEntry],
) -> LineItemDecision:
    """Public entry point. Never raises, see _reconcile_line_item below for
    the actual logic, this wraps it as defence in depth (ADR-006): even a
    bug in this file gets routed to quarantine, not propagated as a crash.
    """
    try:
        return _reconcile_line_item(warehouse, supplier, registry)
    except Exception as exc:  # noqa: BLE001 - intentional, see ADR-006
        return LineItemDecision(
            return_line_id=warehouse.return_line_id,
            sku=warehouse.sku,
            disposition=Disposition.QUARANTINE,
            temporal_bucket=UNRESOLVED_BUCKET,
            resolved_batch_code=None,
            eligible_for_credit=None,
            # warehouse is the original, untouched parameter, still valid
            # even though something failed inside _reconcile_line_item.
            # Caught in review: this used to hardcode 0 here, discarding a
            # physically known fact instead of a genuinely unknown one,
            # exactly backwards for an audit system.
            physical_quantity=warehouse.inspected_quantity,
            creditable_quantity=0,
            overall_confidence=0.0,
            rule_outcomes=(
                RuleOutcome(
                    conflict_type=ConflictType.INTERNAL_ERROR,
                    conflict_detected=True,
                    winner=Winner.UNRESOLVED,
                    resolved_value=None,
                    confidence=0.0,
                    reasoning=(
                        f"Unhandled internal error during reconciliation: {exc!r}. "
                        "Routed to quarantine rather than propagating a crash. "
                        f"Physical quantity ({warehouse.inspected_quantity}) is preserved "
                        "from the warehouse record, it was never in question, only the "
                        "reconciliation logic failed, not the underlying evidence."
                    ),
                    triggers_quarantine=True,
                    reason_code="INTERNAL_ERROR",
                ),
            ),
        )


def _reconcile_line_item(
    warehouse: WarehouseInspectionRecord,
    supplier: SupplierCreditNote,
    registry: Sequence[BatchRegistryEntry],
) -> LineItemDecision:
    """Runs all five rules and aggregates them.

    Physical disposition (scrap/restock/quarantine) is driven only by
    Rule 1 (condition) and Rule 2 (batch resolution), those are the two
    things that determine whether the physical item can be safely routed
    at all. Rule 4 (quantity) and Rule 5 (eligibility) resolve
    independently and drive the financial outcome, not the physical one,
    a legitimately damaged item can still be physically scrapped even if
    a credit dispute over it is unresolved.

    Assumes warehouse.return_line_id == supplier.return_line_id and
    warehouse.sku == supplier.sku, that correlation is the caller's
    responsibility (the ingestion layer), not re-validated here.
    """
    condition_outcome = resolve_condition(warehouse, supplier)
    batch_outcome = resolve_batch_code(warehouse, supplier, registry)
    best_before_outcome = resolve_best_before(warehouse, supplier, batch_outcome, registry)
    quantity_outcome = resolve_quantity(warehouse, supplier)
    eligibility_outcome = resolve_eligibility(warehouse, supplier)

    rule_outcomes = (
        condition_outcome,
        batch_outcome,
        best_before_outcome,
        quantity_outcome,
        eligibility_outcome,
    )

    if condition_outcome.triggers_quarantine or batch_outcome.triggers_quarantine:
        disposition = Disposition.QUARANTINE
    elif condition_outcome.resolved_value is False:
        disposition = Disposition.SCRAP
    else:
        disposition = Disposition.RESTOCK

    if best_before_outcome.resolved_value is None:
        temporal_bucket = UNRESOLVED_BUCKET
    else:
        d = best_before_outcome.resolved_value
        temporal_bucket = f"{d.year:04d}-{d.month:02d}"

    return LineItemDecision(
        return_line_id=warehouse.return_line_id,
        sku=warehouse.sku,
        disposition=disposition,
        temporal_bucket=temporal_bucket,
        resolved_batch_code=batch_outcome.resolved_value,
        eligible_for_credit=eligibility_outcome.resolved_value,
        physical_quantity=warehouse.inspected_quantity,
        creditable_quantity=quantity_outcome.resolved_value,
        # The decision is only as strong as the weaker of the two rules
        # that actually decide physical disposition. Deliberately not a
        # weighted sum across all five rules, invented weights would be
        # less defensible than this, not more (see ADR-008): this number
        # traces to two specific, named confidences, not a formula.
        overall_confidence=min(condition_outcome.confidence, batch_outcome.confidence),
        rule_outcomes=rule_outcomes,
    )