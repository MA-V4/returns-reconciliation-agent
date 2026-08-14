"""
The five decision rules from DECISION_RULES.md, implemented as pure
functions. Each takes already-parsed source records (and the batch
registry, where relevant) and returns a RuleOutcome, never a bare
value, the reasoning and evidence trail are not an afterthought bolted
on top, they're what each function actually returns.

Deliberately dataclasses, not pydantic models, for the output types here.
Pydantic validates untrusted input at the system boundary (see
schemas.py), these are internally computed results built from data
that's already validated, they don't need re-validation, just structure.

reason_code is a stable, machine-searchable string per specific scenario
within a rule, not just per rule, "warehouse won on condition" and
"warehouse won on eligibility" are different codes even though both are
ConflictType.CONDITION-adjacent wins. detail is an optional, rule-specific
structured payload (currently only Rule 2 populates it, with the ranked
batch-code candidate list), free-form on purpose rather than forcing every
rule into one shared schema for data that's genuinely different per rule.

These functions do not raise for any combination of valid schema
instances, see the docstring on each rule for how the "no evidence"
case is handled. The one place that still wraps in a try/except is the
aggregator in engine.py (ADR-006), as defence in depth against a bug in
this file, not a substitute for these functions being correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from rapidfuzz import fuzz, process

from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)


class ConflictType(str, Enum):
    CONDITION = "condition"
    BATCH_CODE = "batch_code"
    BEST_BEFORE = "best_before"
    QUANTITY = "quantity"
    ELIGIBILITY = "eligibility"
    INTERNAL_ERROR = "internal_error"  # engine.py's fallback only, never raised from here
    MISSING_COUNTERPART = "missing_counterpart"  # ingestion.py: one source never reported at all
    SUPPLIER_NOTE_SEQUENCING = "supplier_note_sequencing"  # ingestion.py: which note was authoritative and why
    IDENTITY_MISMATCH = "identity_mismatch"  # ingestion.py: warehouse and supplier disagree on SKU, or conflicting warehouse records for one line


class Winner(str, Enum):
    WAREHOUSE = "warehouse"
    SUPPLIER = "supplier"
    REGISTRY = "registry"       # Rules 2 and 3: the independent arbiter won, not a party
    AGREEMENT = "agreement"     # no conflict existed
    UNRESOLVED = "unresolved"   # genuine toss-up, quarantine follows


# The closed set of every reason_code this system can emit, across rules.py
# and ingestion.py. A test (test_reason_codes.py) asserts every reason_code
# produced by any real decision is a member of this set, so a typo'd or
# stray code fails a test instead of silently never matching anything.
KNOWN_REASON_CODES = frozenset({
    # Rule 1: condition
    "CONDITION_AGREEMENT",
    "CONDITION_WAREHOUSE_OVERRIDE",
    "CONDITION_UNKNOWN_UNRESOLVED",
    # Rule 2: batch code
    "BATCH_EXACT_AGREEMENT",
    "BATCH_REPAIRED_AND_CORROBORATED",
    "BATCH_WAREHOUSE_REGISTRY_MATCH",
    "BATCH_SUPPLIER_REGISTRY_MATCH",
    "BATCH_UNRESOLVED",
    # Rule 3: best-before
    "BEST_BEFORE_FROM_REGISTRY",
    "BEST_BEFORE_UNRESOLVED_BATCH",
    "BEST_BEFORE_REGISTRY_ENTRY_MISSING",
    # Rule 4: quantity
    "QUANTITY_CAPPED_TO_PHYSICAL_COUNT",
    "QUANTITY_UNDERCLAIM_FLAGGED",
    "QUANTITY_EXACT_MATCH",
    # Rule 5: eligibility
    "ELIGIBILITY_AGREEMENT",
    "ELIGIBILITY_UNKNOWN_CONDITION_UNRESOLVED",
    "ELIGIBILITY_WAREHOUSE_DAMAGE_OVERRIDE",
    "ELIGIBILITY_SUPPLIER_STANDS",
    # ingestion.py: sequencing
    "SEQUENCING_SINGLE_NOTE",
    "SEQUENCING_ARRIVAL_ORDER_OVERRIDDEN",
    "SEQUENCING_ORDER_CONFIRMED",
    "SEQUENCING_UNRESOLVED_CONFLICT",
    # ingestion.py: missing evidence / identity integrity
    "MISSING_WAREHOUSE_EVIDENCE",
    "MISSING_SUPPLIER_EVIDENCE",
    "SKU_MISMATCH",
    "CONFLICTING_WAREHOUSE_RECORDS",
    # engine.py / ingestion.py: internal error fallback
    "INTERNAL_ERROR",
})


@dataclass(frozen=True)
class RuleOutcome:
    """One rule's result, structured for the audit trail from the start."""

    conflict_type: ConflictType
    conflict_detected: bool
    winner: Winner
    resolved_value: Any  # bool | str | int | date | None, varies by rule
    confidence: float
    reasoning: str
    evidence_discarded: tuple[str, ...] = ()
    triggers_quarantine: bool = False
    reason_code: str = ""
    detail: dict = field(default_factory=dict)



# Rule 1: condition / salvageability disagreement


# Explicit assumption, stated here rather than left implicit: MINOR_DAMAGE is
# treated as restockable. There is no fourth "discount" disposition in this
# system, only SCRAP / RESTOCK / QUARANTINE, so MINOR_DAMAGE has to land
# somewhere, and "still sellable, imperfect" is the defensible read of it.
_RESTOCKABLE_BY_CONDITION: dict[ConditionGrade, Optional[bool]] = {
    ConditionGrade.SELLABLE: True,
    ConditionGrade.MINOR_DAMAGE: True,
    ConditionGrade.MAJOR_DAMAGE: False,
    ConditionGrade.DESTROYED: False,
    ConditionGrade.UNKNOWN: None,
}


def resolve_condition(
    warehouse: WarehouseInspectionRecord, supplier: SupplierCreditNote
) -> RuleOutcome:
    """Rule 1. See DECISION_RULES.md."""
    implied = _RESTOCKABLE_BY_CONDITION[warehouse.condition_grade]

    if implied is None:
        return RuleOutcome(
            conflict_type=ConflictType.CONDITION,
            conflict_detected=True,
            winner=Winner.UNRESOLVED,
            resolved_value=None,
            confidence=0.0,
            reasoning=(
                "Warehouse condition_grade is UNKNOWN, the inspector could not "
                "determine physical state. Warehouse has no physical assertion to "
                f"win this rule with, and supplier restock_required="
                f"{supplier.restock_required} is not independently verifiable. "
                "Falls to quarantine."
            ),
            triggers_quarantine=True,
            reason_code="CONDITION_UNKNOWN_UNRESOLVED",
        )

    if implied == supplier.restock_required:
        return RuleOutcome(
            conflict_type=ConflictType.CONDITION,
            conflict_detected=False,
            winner=Winner.AGREEMENT,
            resolved_value=implied,
            confidence=1.0,
            reasoning=(
                f"Warehouse condition_grade={warehouse.condition_grade.value} and "
                f"supplier restock_required={supplier.restock_required} agree, no "
                "conflict to resolve."
            ),
            reason_code="CONDITION_AGREEMENT",
        )

    confidence = 1.0 if warehouse.inspector_has_photo_evidence else 0.75
    evidence_note = (
        ""
        if warehouse.inspector_has_photo_evidence
        else (
            " No photo evidence backs this claim; still the best available "
            "first-hand observation, so it wins, but at reduced confidence."
        )
    )
    return RuleOutcome(
        conflict_type=ConflictType.CONDITION,
        conflict_detected=True,
        winner=Winner.WAREHOUSE,
        resolved_value=implied,
        confidence=confidence,
        reasoning=(
            f"Warehouse recorded condition_grade={warehouse.condition_grade.value} "
            f"(implies restockable={implied}); supplier claims restock_required="
            f"{supplier.restock_required}. Direct physical observation outranks a "
            "financially motivated inference: restocking a damaged unit instead of "
            "crediting it minimises the supplier's credit exposure." + evidence_note
        ),
        evidence_discarded=(f"supplier restock_required={supplier.restock_required}",),
        reason_code="CONDITION_WAREHOUSE_OVERRIDE",
    )



# Rule 2: batch code mismatch, and its repair helper



@dataclass(frozen=True)
class BatchRepairResult:
    matched_code: Optional[str]
    confidence: float
    ambiguous: bool  # multiple registry candidates tied at the top score
    threshold: float
    all_candidates: tuple[tuple[str, float], ...] = ()  # (batch_code, confidence 0-1), ranked


def repair_batch_code(
    candidate_text: Optional[str],
    registry_candidates: Sequence[BatchRegistryEntry],
    threshold: float = 0.8,
) -> BatchRepairResult:
    """Fuzzy-match a possibly garbled batch code against known valid codes.

    Edit-distance based (ADR-004), not embeddings, this is character-level
    scanner corruption, not semantic ambiguity, and edit distance is the
    right tool for that failure mode. A tie at the top score is reported
    as ambiguous rather than arbitrarily broken by list order. The full
    ranked candidate list is returned, not just the winner, so the audit
    trail can show what else was considered and rejected, not only what
    won.
    """
    if not candidate_text or not registry_candidates:
        return BatchRepairResult(
            matched_code=None, confidence=0.0, ambiguous=False, threshold=threshold, all_candidates=()
        )

    choices = [entry.batch_code for entry in registry_candidates]
    results = process.extract(candidate_text, choices, scorer=fuzz.ratio, limit=len(choices))
    if not results:
        return BatchRepairResult(
            matched_code=None, confidence=0.0, ambiguous=False, threshold=threshold, all_candidates=()
        )

    all_candidates = tuple((match, score / 100.0) for match, score, _ in results)

    top_score = results[0][1]  # rapidfuzz returns 0-100
    confidence = top_score / 100.0
    top_matches = {match for match, score, _ in results if score == top_score}

    if confidence < threshold:
        return BatchRepairResult(None, confidence, False, threshold, all_candidates)
    if len(top_matches) > 1:
        return BatchRepairResult(None, confidence, True, threshold, all_candidates)
    return BatchRepairResult(next(iter(top_matches)), confidence, False, threshold, all_candidates)


def _candidate_detail(repair: BatchRepairResult) -> dict:
    return {
        "candidates": [
            {"batch_code": code, "confidence": round(score, 4)} for code, score in repair.all_candidates
        ],
        "threshold": repair.threshold,
    }


def _candidate_summary(repair: BatchRepairResult) -> str:
    if not repair.all_candidates:
        return ""
    ranked = ", ".join(f"{code}={score:.1%}" for code, score in repair.all_candidates)
    return f" Candidates considered: {ranked} (threshold {repair.threshold:.0%})."


def resolve_batch_code(
    warehouse: WarehouseInspectionRecord,
    supplier: SupplierCreditNote,
    registry: Sequence[BatchRegistryEntry],
    threshold: float = 0.8,
) -> RuleOutcome:
    """Rule 2. See DECISION_RULES.md.

    Assumes warehouse.sku == supplier.sku, that's the correlation
    assumption from the schema design (ADR-005). Enforced upstream in
    ingestion.py's _process_one_line (SKU_MISMATCH quarantines the line
    before this function is ever called), not re-checked here, this
    function trusts its caller rather than re-validating on every rule.
    """
    candidates = [entry for entry in registry if entry.sku == warehouse.sku]

    warehouse_claim = warehouse.claimed_batch_code or warehouse.raw_scanner_output
    supplier_claim = supplier.claimed_batch_code

    if warehouse_claim and supplier_claim and warehouse_claim == supplier_claim:
        return RuleOutcome(
            conflict_type=ConflictType.BATCH_CODE,
            conflict_detected=False,
            winner=Winner.AGREEMENT,
            resolved_value=warehouse_claim,
            confidence=1.0,
            reasoning="Warehouse and supplier batch codes agree exactly; no registry lookup needed.",
            reason_code="BATCH_EXACT_AGREEMENT",
        )

    repair = repair_batch_code(warehouse_claim, candidates, threshold)
    detail = _candidate_detail(repair)
    candidate_note = _candidate_summary(repair)
    warehouse_valid = repair.matched_code is not None
    supplier_valid = supplier_claim is not None and any(
        entry.batch_code == supplier_claim for entry in candidates
    )

    if warehouse_valid and supplier_valid and repair.matched_code == supplier_claim:
        return RuleOutcome(
            conflict_type=ConflictType.BATCH_CODE,
            conflict_detected=False,
            winner=Winner.AGREEMENT,
            resolved_value=repair.matched_code,
            confidence=repair.confidence,
            reasoning=(
                f"Warehouse's raw evidence repaired to {repair.matched_code!r} "
                f"(confidence {repair.confidence:.2f}) and corroborated by supplier's claim."
                + candidate_note
            ),
            reason_code="BATCH_REPAIRED_AND_CORROBORATED",
            detail=detail,
        )

    if warehouse_valid and not supplier_valid:
        return RuleOutcome(
            conflict_type=ConflictType.BATCH_CODE,
            conflict_detected=True,
            winner=Winner.WAREHOUSE,
            resolved_value=repair.matched_code,
            confidence=repair.confidence,
            reasoning=(
                f"Warehouse's repaired code {repair.matched_code!r} validates against "
                f"the batch registry for SKU {warehouse.sku!r}; supplier's claimed code "
                f"{supplier_claim!r} does not. The registry decided this, not source "
                "preference." + candidate_note
            ),
            evidence_discarded=(f"supplier claimed_batch_code={supplier_claim!r}",),
            reason_code="BATCH_WAREHOUSE_REGISTRY_MATCH",
            detail=detail,
        )

    if supplier_valid and not warehouse_valid:
        return RuleOutcome(
            conflict_type=ConflictType.BATCH_CODE,
            conflict_detected=True,
            winner=Winner.SUPPLIER,
            resolved_value=supplier_claim,
            confidence=1.0,
            reasoning=(
                f"Supplier's claimed code {supplier_claim!r} validates against the "
                f"batch registry; warehouse's evidence "
                f"({warehouse.raw_scanner_output!r}) does not repair to a confident "
                "registry match. The registry decided this, not source preference."
                + candidate_note
            ),
            evidence_discarded=(f"warehouse raw_scanner_output={warehouse.raw_scanner_output!r}",),
            reason_code="BATCH_SUPPLIER_REGISTRY_MATCH",
            detail=detail,
        )

    ambiguity_note = ", ambiguous tie between multiple candidates" if repair.ambiguous else ""
    return RuleOutcome(
        conflict_type=ConflictType.BATCH_CODE,
        conflict_detected=True,
        winner=Winner.UNRESOLVED,
        resolved_value=None,
        confidence=repair.confidence,
        reasoning=(
            "Neither source's batch claim clears an independent registry match "
            f"(warehouse candidate confidence {repair.confidence:.2f}{ambiguity_note}; "
            f"supplier claim {supplier_claim!r} not found in registry for this SKU). "
            "Batch is left unresolved rather than guessed." + candidate_note
        ),
        evidence_discarded=(
            f"warehouse raw_scanner_output={warehouse.raw_scanner_output!r}",
            f"supplier claimed_batch_code={supplier_claim!r}",
        ),
        triggers_quarantine=True,
        reason_code="BATCH_UNRESOLVED",
        detail=detail,
    )



# Rule 3: best-before / temporal disagreement



def resolve_best_before(
    warehouse: WarehouseInspectionRecord,
    supplier: SupplierCreditNote,
    batch_outcome: RuleOutcome,
    registry: Sequence[BatchRegistryEntry],
) -> RuleOutcome:
    """Rule 3. See DECISION_RULES.md.

    The resolution is entirely downstream of Rule 2, a registry lookup on
    the resolved batch, the registry always wins regardless of what
    either party claimed. But this now actually receives both parties'
    stated dates specifically to detect and report a real disagreement
    between them, previously this function only saw batch_outcome and
    the registry, so a genuine warehouse/supplier disagreement on
    best-before could be silently marked conflict_detected=False just
    because the registry lookup itself succeeded. Caught in review:
    the outcome (registry wins) was always correct, the conflict
    reporting around it wasn't.
    """
    warehouse_date = warehouse.best_before_date
    supplier_date = supplier.claimed_best_before_date
    dates_disagree = (
        warehouse_date is not None
        and supplier_date is not None
        and warehouse_date != supplier_date
    )

    if batch_outcome.resolved_value is None:
        return RuleOutcome(
            conflict_type=ConflictType.BEST_BEFORE,
            conflict_detected=True,
            winner=Winner.UNRESOLVED,
            resolved_value=None,
            confidence=0.0,
            reasoning="Batch unresolved (Rule 2), best-before cannot be looked up; bucket left as unknown, pending review.",
            triggers_quarantine=True,
            reason_code="BEST_BEFORE_UNRESOLVED_BATCH",
        )

    entry = next(
        (e for e in registry if e.batch_code == batch_outcome.resolved_value), None
    )
    if entry is None:
        return RuleOutcome(
            conflict_type=ConflictType.BEST_BEFORE,
            conflict_detected=True,
            winner=Winner.UNRESOLVED,
            resolved_value=None,
            confidence=0.0,
            reasoning=(
                f"Resolved batch code {batch_outcome.resolved_value!r} not found in "
                "registry, unexpected given Rule 2 just validated it there, treated as "
                "a data integrity issue rather than trusted blindly. Best-before left "
                "unresolved."
            ),
            triggers_quarantine=True,
            reason_code="BEST_BEFORE_REGISTRY_ENTRY_MISSING",
        )

    if dates_disagree:
        reasoning = (
            f"Warehouse claimed best-before {warehouse_date}; supplier claimed "
            f"{supplier_date}; these disagree. Best-before is a property of the "
            f"resolved batch, not a value either party states directly, so both "
            f"claims are discarded in favour of the registry entry for "
            f"{entry.batch_code}: {entry.best_before_date}."
        )
        evidence_discarded = (
            f"warehouse best_before_date={warehouse_date}",
            f"supplier claimed_best_before_date={supplier_date}",
        )
    else:
        reasoning = (
            f"Best-before taken from the batch registry entry for {entry.batch_code}, "
            "not from either party's stated date."
        )
        evidence_discarded = ()

    return RuleOutcome(
        conflict_type=ConflictType.BEST_BEFORE,
        conflict_detected=dates_disagree,
        winner=Winner.REGISTRY,
        resolved_value=entry.best_before_date,
        confidence=1.0,
        reasoning=reasoning,
        evidence_discarded=evidence_discarded,
        reason_code="BEST_BEFORE_FROM_REGISTRY",
    )



# Rule 4: quantity dispute



def resolve_quantity(
    warehouse: WarehouseInspectionRecord, supplier: SupplierCreditNote
) -> RuleOutcome:
    """Rule 4. See DECISION_RULES.md.

    resolved_value is the creditable quantity, capped at the physical
    count. Physical quantity and creditable quantity are two different
    numbers, physical quantity always equals what the warehouse actually
    counted and drives routing regardless of any credit dispute; this
    rule only ever affects the financial reconciliation. A supplier
    claiming FEWER units than were physically inspected is a real
    discrepancy too, not agreement, the difference is credit-eligible
    stock the supplier isn't crediting for. Both directions are flagged.
    """
    physical = warehouse.inspected_quantity
    creditable = min(supplier.credit_quantity, physical)
    uncredited = physical - creditable

    if supplier.credit_quantity > physical:
        return RuleOutcome(
            conflict_type=ConflictType.QUANTITY,
            conflict_detected=True,
            winner=Winner.WAREHOUSE,
            resolved_value=creditable,
            confidence=1.0,
            reasoning=(
                f"Supplier claimed credit for {supplier.credit_quantity} units; "
                f"warehouse physically inspected only {physical}. Crediting more units "
                "than were received is a physical impossibility, not a disagreement to "
                f"arbitrate. Capped at the physical count ({physical})."
            ),
            evidence_discarded=(f"supplier credit_quantity={supplier.credit_quantity}",),
            reason_code="QUANTITY_CAPPED_TO_PHYSICAL_COUNT",
        )

    if uncredited > 0:
        return RuleOutcome(
            conflict_type=ConflictType.QUANTITY,
            conflict_detected=True,
            winner=Winner.SUPPLIER,
            resolved_value=creditable,
            confidence=1.0,
            reasoning=(
                f"Supplier claims {creditable} of {physical} physically inspected units "
                f"are credit-eligible, {uncredited} unit(s) are not. This is a financial "
                f"gap, not a physical routing question, all {physical} units are still "
                "physically routed per the condition rule; the uncredited units simply "
                "carry no financial reconciliation."
            ),
            reason_code="QUANTITY_UNDERCLAIM_FLAGGED",
        )

    return RuleOutcome(
        conflict_type=ConflictType.QUANTITY,
        conflict_detected=False,
        winner=Winner.AGREEMENT,
        resolved_value=creditable,
        confidence=1.0,
        reasoning=f"Supplier's claimed quantity matches the physically inspected count exactly ({physical}).",
        reason_code="QUANTITY_EXACT_MATCH",
    )



# Rule 5: eligibility dispute


def resolve_eligibility(
    warehouse: WarehouseInspectionRecord, supplier: SupplierCreditNote
) -> RuleOutcome:
    """Rule 5. See DECISION_RULES.md.

    Three exhaustive cases when supplier marks a unit ineligible, keyed
    off condition_grade, with photo evidence acting purely as a confidence
    modifier on the MAJOR_DAMAGE/DESTROYED case, the same role it plays in
    Rule 1, not a separate override gate.
    """
    if supplier.eligible_for_credit:
        return RuleOutcome(
            conflict_type=ConflictType.ELIGIBILITY,
            conflict_detected=False,
            winner=Winner.AGREEMENT,
            resolved_value=True,
            confidence=1.0,
            reasoning="Supplier already marked eligible; no conflict to resolve.",
            reason_code="ELIGIBILITY_AGREEMENT",
        )

    if warehouse.condition_grade == ConditionGrade.UNKNOWN:
        return RuleOutcome(
            conflict_type=ConflictType.ELIGIBILITY,
            conflict_detected=True,
            winner=Winner.UNRESOLVED,
            resolved_value=None,
            confidence=0.0,
            reasoning=(
                "Supplier marked ineligible, but warehouse condition is UNKNOWN, "
                "there is no physical determination in either direction to reason "
                "from. Genuine toss-up, routed to quarantine rather than guessed."
            ),
            triggers_quarantine=True,
            reason_code="ELIGIBILITY_UNKNOWN_CONDITION_UNRESOLVED",
        )

    if warehouse.condition_grade in (ConditionGrade.MAJOR_DAMAGE, ConditionGrade.DESTROYED):
        confidence = 1.0 if warehouse.inspector_has_photo_evidence else 0.75
        evidence_note = (
            ""
            if warehouse.inspector_has_photo_evidence
            else (
                " No photo evidence backs this claim; still the best available "
                "first-hand observation, honoured at reduced confidence, the same "
                "treatment Rule 1 gives this exact gap."
            )
        )
        return RuleOutcome(
            conflict_type=ConflictType.ELIGIBILITY,
            conflict_detected=True,
            winner=Winner.WAREHOUSE,
            resolved_value=True,
            confidence=confidence,
            reasoning=(
                f"Supplier marked ineligible, but warehouse recorded "
                f"{warehouse.condition_grade.value}. Physical evidence of damage "
                "independently contradicts a 'no credit' claim from a party with a "
                "known incentive to minimise credit. Eligibility overridden to True."
                + evidence_note
            ),
            evidence_discarded=("supplier eligible_for_credit=False",),
            reason_code="ELIGIBILITY_WAREHOUSE_DAMAGE_OVERRIDE",
        )

    return RuleOutcome(
        conflict_type=ConflictType.ELIGIBILITY,
        conflict_detected=False,
        winner=Winner.SUPPLIER,
        resolved_value=False,
        confidence=0.75,
        reasoning=(
            f"Supplier marked ineligible; warehouse condition_grade="
            f"{warehouse.condition_grade.value} does not physically contradict that. "
            "No independent evidence to override, supplier's determination stands."
        ),
        reason_code="ELIGIBILITY_SUPPLIER_STANDS",
    )