"""
Ingestion layer. Three things live here:

1. resolve_authoritative_supplier_note: the actual fix for the
   out-of-order-timestamp failure mode. Multiple SupplierCreditNote
   records for one return_line_id get ordered and replayed by
   sequence_number, never by received_at (ADR-002).
2. parse_warehouse_record / parse_supplier_note: tolerant parsing of raw
   input at the system boundary, a malformed record becomes a structured
   failure, not a crash.
3. process_return: the pipeline entry point. Groups warehouse records and
   supplier notes by return_line_id, resolves each line's authoritative
   note, and reconciles each line (engine.py). One bad line item does not
   sink the rest of the batch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from pydantic import ValidationError

from reconciliation.engine import UNRESOLVED_BUCKET, LineItemDecision, reconcile_line_item
from reconciliation.rules import ConflictType, RuleOutcome, Winner
from reconciliation.schemas import (
    BatchRegistryEntry,
    Disposition,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)



# 1. Multi-note sequence resolution



@dataclass(frozen=True)
class SupplierNoteResolution:
    return_line_id: str
    authoritative_note: Optional[SupplierCreditNote]
    all_notes_by_sequence: tuple[SupplierCreditNote, ...]
    superseded_note_ids: tuple[str, ...]
    naive_arrival_order_choice: Optional[SupplierCreditNote]
    anomalies: tuple[str, ...] = ()


def resolve_authoritative_supplier_note(
    notes: Sequence[SupplierCreditNote],
) -> SupplierNoteResolution:
    """Order and replay supplier notes by sequence_number, never by
    received_at. Also runs three integrity checks that ADR-002 named but
    deferred: duplicate sequence numbers with conflicting content,
    sequence_number/generated_at inconsistency, and dangling
    supersedes_note_id references. None of these block a decision, they're
    recorded as anomalies in the resolution for the audit trail.
    """
    if not notes:
        return SupplierNoteResolution(
            return_line_id="",
            authoritative_note=None,
            all_notes_by_sequence=(),
            superseded_note_ids=(),
            naive_arrival_order_choice=None,
            anomalies=("no supplier notes provided",),
        )

    return_line_id = notes[0].return_line_id
    anomalies: list[str] = []

    by_sequence = tuple(sorted(notes, key=lambda n: n.sequence_number))

    # duplicate sequence_number with genuinely conflicting content
    seen: dict[int, SupplierCreditNote] = {}
    for note in by_sequence:
        prior = seen.get(note.sequence_number)
        if prior is not None and prior.note_id != note.note_id:
            prior_content = (prior.eligible_for_credit, prior.credit_quantity, prior.restock_required)
            this_content = (note.eligible_for_credit, note.credit_quantity, note.restock_required)
            if prior_content != this_content:
                anomalies.append(
                    f"duplicate sequence_number={note.sequence_number} with conflicting "
                    f"content between {prior.note_id!r} and {note.note_id!r}"
                )
        seen[note.sequence_number] = note

    # sequence_number vs generated_at consistency, the ADR-002 follow-up
    for earlier, later in zip(by_sequence, by_sequence[1:]):
        if later.sequence_number > earlier.sequence_number and later.generated_at < earlier.generated_at:
            anomalies.append(
                f"sequence_number increases ({earlier.sequence_number} -> "
                f"{later.sequence_number}) but generated_at goes backwards "
                f"({earlier.generated_at} -> {later.generated_at}); supplier's own "
                "sequence counter may be unreliable here"
            )

    # supersedes_note_id referencing a note not present in this batch
    known_ids = {n.note_id for n in by_sequence}
    for note in by_sequence:
        if note.supersedes_note_id and note.supersedes_note_id not in known_ids:
            anomalies.append(
                f"note {note.note_id!r} claims to supersede "
                f"{note.supersedes_note_id!r}, which is not present in this batch"
            )

    # authority: highest sequence_number, unless that's a genuine content tie,
    # in which case this is not resolved rather than guessed (same philosophy
    # as Rule 2's ambiguous batch-code tie)
    top_sequence = by_sequence[-1].sequence_number
    top_notes = [n for n in by_sequence if n.sequence_number == top_sequence]
    if len(top_notes) > 1:
        contents = {(n.eligible_for_credit, n.credit_quantity, n.restock_required) for n in top_notes}
        if len(contents) > 1:
            anomalies.append(
                f"multiple notes at the highest sequence_number ({top_sequence}) with "
                "conflicting content; no reliable authority, left unresolved rather "
                "than guessed"
            )
            authoritative: Optional[SupplierCreditNote] = None
        else:
            authoritative = top_notes[0]
    else:
        authoritative = top_notes[0]

    by_arrival = sorted(notes, key=lambda n: n.received_at)
    naive_choice = by_arrival[-1]
    if authoritative is not None and naive_choice.note_id != authoritative.note_id:
        anomalies.append(
            f"arrival order disagreement: {naive_choice.note_id!r} arrived last "
            f"(received_at={naive_choice.received_at}) and would have been chosen by "
            f"a naive arrival-order implementation; {authoritative.note_id!r} has the "
            f"higher sequence_number ({authoritative.sequence_number}) and is the one "
            "actually used"
        )

    superseded = tuple(
        n.note_id
        for n in by_sequence
        if authoritative is None or n.note_id != authoritative.note_id
    )

    return SupplierNoteResolution(
        return_line_id=return_line_id,
        authoritative_note=authoritative,
        all_notes_by_sequence=by_sequence,
        superseded_note_ids=superseded,
        naive_arrival_order_choice=naive_choice,
        anomalies=tuple(anomalies),
    )



# 2. Tolerant parsing at the system boundary



@dataclass(frozen=True)
class ParseOutcome:
    success: bool
    record: Optional[Any]
    raw_payload: dict
    errors: tuple[str, ...] = ()


def parse_warehouse_record(raw: dict) -> ParseOutcome:
    """A malformed warehouse payload becomes a structured failure, never a
    raised exception, the caller decides what a parse failure means for
    routing (process_return routes it to quarantine).
    """
    try:
        return ParseOutcome(success=True, record=WarehouseInspectionRecord(**raw), raw_payload=raw)
    except ValidationError as exc:
        return ParseOutcome(
            success=False,
            record=None,
            raw_payload=raw,
            errors=tuple(f"{err['loc']}: {err['msg']}" for err in exc.errors()),
        )


def parse_supplier_note(raw: dict) -> ParseOutcome:
    try:
        return ParseOutcome(success=True, record=SupplierCreditNote(**raw), raw_payload=raw)
    except ValidationError as exc:
        return ParseOutcome(
            success=False,
            record=None,
            raw_payload=raw,
            errors=tuple(f"{err['loc']}: {err['msg']}" for err in exc.errors()),
        )



# 3. Pipeline entry point

def _sequencing_rule_outcome(resolution: SupplierNoteResolution) -> RuleOutcome:
    """Turns a SupplierNoteResolution into an audit-trail entry. Without
    this, the out-of-order-timestamp handling is computed correctly but
    invisible, resolve_authoritative_supplier_note's reasoning never
    reached LineItemDecision. This is what actually shows, per line item,
    which notes existed, which one was authoritative, and whether arrival
    order would have chosen wrong.
    """
    if len(resolution.all_notes_by_sequence) <= 1:
        return RuleOutcome(
            conflict_type=ConflictType.SUPPLIER_NOTE_SEQUENCING,
            conflict_detected=False,
            winner=Winner.AGREEMENT,
            resolved_value=resolution.authoritative_note.note_id if resolution.authoritative_note else None,
            confidence=1.0,
            reasoning="Only one supplier note received for this line; no ordering to resolve.",
            reason_code="SEQUENCING_SINGLE_NOTE",
        )

    notes_considered = ", ".join(
        f"{n.note_id} (sequence_number={n.sequence_number}, received_at={n.received_at.isoformat()})"
        for n in resolution.all_notes_by_sequence
    )
    out_of_order = (
        resolution.naive_arrival_order_choice is not None
        and resolution.naive_arrival_order_choice.note_id != resolution.authoritative_note.note_id
    )

    reasoning_parts = [f"Notes considered: {notes_considered}."]
    if out_of_order:
        reasoning_parts.append(
            f"Arrival order would have chosen {resolution.naive_arrival_order_choice.note_id!r} "
            "(it arrived last), sequence_number correctly identified "
            f"{resolution.authoritative_note.note_id!r} as authoritative instead; "
            "received_at represents transport order only, sequence_number is authoritative."
        )
    else:
        reasoning_parts.append(
            "Arrival order and sequence_number agree on the same note; no reordering was needed."
        )
    if resolution.anomalies:
        reasoning_parts.append("Anomalies noted: " + "; ".join(resolution.anomalies) + ".")

    return RuleOutcome(
        conflict_type=ConflictType.SUPPLIER_NOTE_SEQUENCING,
        conflict_detected=out_of_order or bool(resolution.anomalies),
        winner=Winner.SUPPLIER,
        resolved_value=resolution.authoritative_note.note_id,
        confidence=1.0 if not resolution.anomalies else 0.75,
        reasoning=" ".join(reasoning_parts),
        evidence_discarded=resolution.superseded_note_ids,
        reason_code="SEQUENCING_ARRIVAL_ORDER_OVERRIDDEN" if out_of_order else "SEQUENCING_ORDER_CONFIRMED",
    )


def validate_registry_integrity(registry: Sequence[BatchRegistryEntry]) -> tuple[str, ...]:
    """Checks the registry as a whole, not just one entry at a time.

    BatchRegistryEntry's own validator (schemas.py) already rejects a
    self-contradictory single entry. This catches the cross-entry
    problem that validator can't see: the same batch_code appearing more
    than once with genuinely conflicting data. This is a registry data
    quality problem, not a warehouse/supplier dispute, there's no rule to
    arbitrate it, it's surfaced as a warning for whoever loaded the
    registry to fix, not resolved automatically.
    """
    warnings: list[str] = []
    seen: dict[str, BatchRegistryEntry] = {}
    for entry in registry:
        prior = seen.get(entry.batch_code)
        if prior is not None and prior != entry:
            warnings.append(
                f"duplicate batch_code {entry.batch_code!r} appears more than once in "
                f"the registry with conflicting data (sku {prior.sku!r} vs {entry.sku!r}, "
                f"manufactured_date {prior.manufactured_date} vs {entry.manufactured_date}, "
                f"best_before_date {prior.best_before_date} vs {entry.best_before_date})"
            )
        seen[entry.batch_code] = entry
    return tuple(warnings)


def process_return(
    warehouse_records: Sequence[WarehouseInspectionRecord],
    supplier_notes: Sequence[SupplierCreditNote],
    registry: Sequence[BatchRegistryEntry],
) -> tuple[LineItemDecision, ...]:
    """One warehouse record and one-or-more supplier notes per
    return_line_id, resolved and reconciled, per line, independently. A
    bad line doesn't take the rest of the shipment down with it.

    At most one warehouse record per return_line_id is still the expected
    shape (that failure mode isn't the one named in the brief, the
    supplier side is). But it's no longer a silent assumption: if more
    than one is provided for the same line and they genuinely disagree,
    that line is quarantined with a named reason rather than silently
    keeping whichever one happened to be last in the input. Duplicate
    submissions with identical content are harmless and pass through, the
    same principle already applied to duplicate supplier notes.
    """
    warehouse_by_line: dict[str, WarehouseInspectionRecord] = {}
    conflicting_warehouse_lines: set[str] = set()
    for warehouse in warehouse_records:
        existing = warehouse_by_line.get(warehouse.return_line_id)
        if existing is not None and existing != warehouse:
            conflicting_warehouse_lines.add(warehouse.return_line_id)
        warehouse_by_line[warehouse.return_line_id] = warehouse

    notes_by_line: dict[str, list[SupplierCreditNote]] = {}
    for note in supplier_notes:
        notes_by_line.setdefault(note.return_line_id, []).append(note)

    all_line_ids = sorted(set(warehouse_by_line) | set(notes_by_line))

    decisions = []
    for return_line_id in all_line_ids:
        try:
            if return_line_id in conflicting_warehouse_lines:
                decisions.append(_quarantine_for_missing_evidence(
                    return_line_id,
                    warehouse_by_line[return_line_id].sku,
                    "Multiple warehouse inspection records exist for this return "
                    "line with genuinely conflicting content; no reliable way to "
                    "determine which inspection is authoritative.",
                    reason_code="CONFLICTING_WAREHOUSE_RECORDS",
                    conflict_type=ConflictType.IDENTITY_MISMATCH,
                ))
                continue
            decisions.append(_process_one_line(return_line_id, warehouse_by_line, notes_by_line, registry))
        except Exception as exc:  # noqa: BLE001 - a batch-level bug on one line must not sink the rest
            known_warehouse = warehouse_by_line.get(return_line_id)
            known_quantity = known_warehouse.inspected_quantity if known_warehouse else 0
            decisions.append(_quarantine_for_internal_error(return_line_id, exc, physical_quantity=known_quantity))
    return tuple(decisions)


def verify_determinism(
    warehouse_records: Sequence[WarehouseInspectionRecord],
    supplier_notes: Sequence[SupplierCreditNote],
    registry: Sequence[BatchRegistryEntry],
) -> bool:
    """Runs process_return twice against identical input and confirms the
    two results are equal. This is a real check, not a demo, it's the
    actual property the deterministic-engine architecture (ADR-001)
    claims. LineItemDecision and RuleOutcome are both frozen dataclasses,
    so == here is genuine structural equality across every field, not a
    shallow identity check.
    """
    first = process_return(warehouse_records, supplier_notes, registry)
    second = process_return(warehouse_records, supplier_notes, registry)
    return first == second


def _process_one_line(
    return_line_id: str,
    warehouse_by_line: dict[str, WarehouseInspectionRecord],
    notes_by_line: dict[str, list[SupplierCreditNote]],
    registry: Sequence[BatchRegistryEntry],
) -> LineItemDecision:
    warehouse = warehouse_by_line.get(return_line_id)
    notes = notes_by_line.get(return_line_id, [])

    if warehouse is None:
        sku = notes[0].sku if notes else "UNKNOWN"
        return _quarantine_for_missing_evidence(
            return_line_id,
            sku,
            "No warehouse inspection record exists for this return line; supplier "
            "evidence alone is not enough to determine physical disposition.",
            reason_code="MISSING_WAREHOUSE_EVIDENCE",
        )

    if not notes:
        return _quarantine_for_missing_evidence(
            return_line_id,
            warehouse.sku,
            "No supplier credit note exists for this return line; warehouse "
            "evidence alone is not enough to determine financial disposition.",
            reason_code="MISSING_SUPPLIER_EVIDENCE",
            physical_quantity=warehouse.inspected_quantity,
        )

    resolution = resolve_authoritative_supplier_note(notes)
    if resolution.authoritative_note is None:
        return _quarantine_for_missing_evidence(
            return_line_id,
            warehouse.sku,
            "Supplier notes exist but no single authoritative note could be "
            "determined: " + "; ".join(resolution.anomalies),
            reason_code="SEQUENCING_UNRESOLVED_CONFLICT",
            physical_quantity=warehouse.inspected_quantity,
        )

    if warehouse.sku != resolution.authoritative_note.sku:
        return _quarantine_for_missing_evidence(
            return_line_id,
            warehouse.sku,
            f"Warehouse record claims SKU {warehouse.sku!r}; supplier note claims "
            f"SKU {resolution.authoritative_note.sku!r}. These records disagree on "
            "what item this even is, not a value to arbitrate between, this line "
            "cannot be safely reconciled until the pairing itself is corrected.",
            reason_code="SKU_MISMATCH",
            conflict_type=ConflictType.IDENTITY_MISMATCH,
            physical_quantity=warehouse.inspected_quantity,
        )

    decision = reconcile_line_item(warehouse, resolution.authoritative_note, registry)
    sequencing_outcome = _sequencing_rule_outcome(resolution)
    return replace(decision, rule_outcomes=(sequencing_outcome,) + decision.rule_outcomes)


def _quarantine_for_missing_evidence(
    return_line_id: str,
    sku: str,
    reason: str,
    reason_code: str,
    physical_quantity: int = 0,
    conflict_type: ConflictType = ConflictType.MISSING_COUNTERPART,
) -> LineItemDecision:
    return LineItemDecision(
        return_line_id=return_line_id,
        sku=sku,
        disposition=Disposition.QUARANTINE,
        temporal_bucket=UNRESOLVED_BUCKET,
        resolved_batch_code=None,
        eligible_for_credit=None,
        physical_quantity=physical_quantity,
        creditable_quantity=0,
        overall_confidence=0.0,
        rule_outcomes=(
            RuleOutcome(
                conflict_type=conflict_type,
                conflict_detected=True,
                winner=Winner.UNRESOLVED,
                resolved_value=None,
                confidence=0.0,
                reasoning=reason,
                triggers_quarantine=True,
                reason_code=reason_code,
            ),
        ),
    )


def _quarantine_for_internal_error(
    return_line_id: str, exc: Exception, physical_quantity: int = 0
) -> LineItemDecision:
    return LineItemDecision(
        return_line_id=return_line_id,
        sku="UNKNOWN",
        disposition=Disposition.QUARANTINE,
        temporal_bucket=UNRESOLVED_BUCKET,
        resolved_batch_code=None,
        eligible_for_credit=None,
        physical_quantity=physical_quantity,
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
                    f"Unhandled internal error processing return_line_id="
                    f"{return_line_id!r}: {exc!r}. Routed to quarantine rather than "
                    "sinking the rest of the batch."
                ),
                triggers_quarantine=True,
                reason_code="INTERNAL_ERROR",
            ),
        ),
    )