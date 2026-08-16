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


# 1. AUDIT / UNRESOLVED STATE

# Any evidence or processing failure that cannot be safely resolved
# is explicitly represented rather than silently guessed.
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



# 2. EVIDENCE INGESTION + SAFETY BOUNDARY

# The public entry point receives the warehouse inspection,
# supplier credit note and batch registry as the evidence set.
#
# ADR-006 guarantees that reconciliation never propagates an
# exception. If anything unexpected fails, the item is quarantined
# and the known physical evidence is preserved for audit.
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



# 3. CONFLICT DETECTION + EVIDENCE ARBITRATION

# Each evidence question is resolved independently.
#
# The system does NOT decide that the warehouse or supplier is
# globally trustworthy. Instead, each rule determines which evidence
# is strongest for that particular field.
def _reconcile_line_item(
    warehouse: WarehouseInspectionRecord,
    supplier: SupplierCreditNote,
    registry: Sequence[BatchRegistryEntry],
) -> LineItemDecision:

    
    # 3a. EVIDENCE ARBITRATION: FIVE INDEPENDENT RULES
    
    condition_outcome = resolve_condition(warehouse, supplier)
    batch_outcome = resolve_batch_code(warehouse, supplier, registry)
    best_before_outcome = resolve_best_before(
        warehouse, supplier, batch_outcome, registry
    )
    quantity_outcome = resolve_quantity(warehouse, supplier)
    eligibility_outcome = resolve_eligibility(warehouse, supplier)

    # Collect the result of each evidence decision so that the
    # final outcome remains explainable and auditable.
    rule_outcomes = (
        condition_outcome,
        batch_outcome,
        best_before_outcome,
        quantity_outcome,
        eligibility_outcome,
    )



    # 4. DECISION RULES: PHYSICAL ROUTING

    # Condition and batch identity determine whether the physical
    # stock can be safely routed.
    #
    # If either creates an unresolved safety issue -> QUARANTINE.
    # If condition confirms damage -> SCRAP.
    # Otherwise -> RESTOCK.
    if condition_outcome.triggers_quarantine or batch_outcome.triggers_quarantine:
        disposition = Disposition.QUARANTINE
    elif condition_outcome.resolved_value is False:
        disposition = Disposition.SCRAP
    else:
        disposition = Disposition.RESTOCK



    # 5. TEMPORAL BUCKET / RESOLVED EVIDENCE

    # Best-before is derived from the winning evidence source.
    # If it cannot be resolved safely, it remains explicitly
    # unknown rather than being guessed.
    if best_before_outcome.resolved_value is None:
        temporal_bucket = UNRESOLVED_BUCKET
    else:
        d = best_before_outcome.resolved_value
        temporal_bucket = f"{d.year:04d}-{d.month:02d}"



    # 6. FINAL DECISION + CONFIDENCE + AUDIT TRAIL

    # The final object preserves the individual rule outcomes,
    # resolved values and confidence so the decision can be traced
    # back to the underlying evidence.
    return LineItemDecision(
        return_line_id=warehouse.return_line_id,
        sku=warehouse.sku,
        disposition=disposition,
        temporal_bucket=temporal_bucket,
        resolved_batch_code=batch_outcome.resolved_value,
        eligible_for_credit=eligibility_outcome.resolved_value,
        physical_quantity=warehouse.inspected_quantity,
        creditable_quantity=quantity_outcome.resolved_value,

        # Confidence deliberately follows the weaker of the two
        # rules responsible for physical disposition. This avoids
        # inventing arbitrary weights across unrelated evidence.
        overall_confidence=min(
            condition_outcome.confidence,
            batch_outcome.confidence,
        ),

        # Preserve every rule outcome for explainability and audit.
        rule_outcomes=rule_outcomes,
    )