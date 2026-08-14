"""
Tests for ingestion.py. Three sections matching the module's three
concerns: sequence resolution, parsing, and the pipeline. The pipeline
section includes the scenario deferred from the rule engine milestone,
garbled batch code and out-of-order supplier notes on the same line,
at once, this is the brief's actual test case.
"""

from datetime import date, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from reconciliation.engine import UNRESOLVED_BUCKET
from reconciliation.ingestion import (
    parse_supplier_note,
    parse_warehouse_record,
    process_return,
    resolve_authoritative_supplier_note,
)
from reconciliation.rules import ConflictType
from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    Disposition,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)


def wh(**overrides):
    base = dict(
        record_id="WH-1",
        return_line_id="RL-1",
        sku="SKU-123",
        condition_grade=ConditionGrade.SELLABLE,
        inspected_quantity=10,
        inspected_at=datetime(2026, 1, 1),
    )
    base.update(overrides)
    return WarehouseInspectionRecord(**base)


def sn(**overrides):
    base = dict(
        note_id="SN-1",
        return_line_id="RL-1",
        sku="SKU-123",
        sequence_number=1,
        generated_at=datetime(2026, 1, 1),
        received_at=datetime(2026, 1, 1),
        eligible_for_credit=True,
        credit_quantity=10,
        restock_required=True,
    )
    base.update(overrides)
    return SupplierCreditNote(**base)


REGISTRY = [
    BatchRegistryEntry(
        batch_code="BC-2026-0817-A",
        sku="SKU-123",
        manufactured_date=date(2026, 8, 17),
        best_before_date=date(2027, 8, 17),
    ),
]


# ---------------------------------------------------------------------------
# resolve_authoritative_supplier_note
# ---------------------------------------------------------------------------

class TestResolveAuthoritativeSupplierNote:
    def test_single_note_is_trivially_authoritative(self):
        note = sn()
        resolution = resolve_authoritative_supplier_note([note])
        assert resolution.authoritative_note.note_id == note.note_id
        assert resolution.anomalies == ()

    def test_out_of_order_arrival_sequence_number_wins(self):
        # SN-2 is the real correction (higher sequence_number) but arrives
        # chronologically BEFORE SN-1, this is the named failure mode
        earlier_by_sequence = sn(
            note_id="SN-1", sequence_number=1,
            generated_at=datetime(2026, 1, 1, 8, 0), received_at=datetime(2026, 1, 1, 8, 5),
            credit_quantity=10,
        )
        later_by_sequence = sn(
            note_id="SN-2", sequence_number=2,
            generated_at=datetime(2026, 1, 2, 8, 0), received_at=datetime(2026, 1, 1, 8, 0),
            credit_quantity=3,
        )
        resolution = resolve_authoritative_supplier_note([earlier_by_sequence, later_by_sequence])

        assert resolution.authoritative_note.note_id == "SN-2"
        assert resolution.authoritative_note.credit_quantity == 3
        assert resolution.naive_arrival_order_choice.note_id == "SN-1"
        assert any("arrival order disagreement" in a for a in resolution.anomalies)

    def test_duplicate_sequence_identical_content_is_harmless(self):
        note_a = sn(note_id="SN-A", sequence_number=1, credit_quantity=10)
        note_b = sn(note_id="SN-B", sequence_number=1, credit_quantity=10)  # re-delivery, same content
        resolution = resolve_authoritative_supplier_note([note_a, note_b])
        assert resolution.authoritative_note is not None
        assert not any("conflicting" in a for a in resolution.anomalies)

    def test_duplicate_sequence_conflicting_content_is_unresolved(self):
        note_a = sn(note_id="SN-A", sequence_number=1, eligible_for_credit=True, credit_quantity=10)
        note_b = sn(note_id="SN-B", sequence_number=1, eligible_for_credit=False, credit_quantity=2)
        resolution = resolve_authoritative_supplier_note([note_a, note_b])
        assert resolution.authoritative_note is None
        assert any("conflicting content" in a for a in resolution.anomalies)

    def test_sequence_generated_at_inconsistency_is_flagged_but_does_not_block(self):
        earlier = sn(note_id="SN-1", sequence_number=1, generated_at=datetime(2026, 1, 5))
        later_but_backwards = sn(note_id="SN-2", sequence_number=2, generated_at=datetime(2026, 1, 1))
        resolution = resolve_authoritative_supplier_note([earlier, later_but_backwards])
        assert resolution.authoritative_note is not None  # flagged, not blocked
        assert any("generated_at goes backwards" in a for a in resolution.anomalies)

    def test_dangling_supersedes_reference_is_flagged(self):
        note = sn(note_id="SN-2", sequence_number=2, supersedes_note_id="SN-NOT-IN-BATCH")
        resolution = resolve_authoritative_supplier_note([note])
        assert any("not present in this batch" in a for a in resolution.anomalies)

    def test_empty_notes_list(self):
        resolution = resolve_authoritative_supplier_note([])
        assert resolution.authoritative_note is None
        assert resolution.anomalies == ("no supplier notes provided",)


# ---------------------------------------------------------------------------
# Tolerant parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_valid_warehouse_payload_parses(self):
        outcome = parse_warehouse_record(
            {
                "record_id": "WH-1", "return_line_id": "RL-1", "sku": "SKU-123",
                "condition_grade": "sellable", "inspected_quantity": 10,
                "inspected_at": "2026-01-01T09:00:00",
            }
        )
        assert outcome.success is True
        assert outcome.record.sku == "SKU-123"

    def test_malformed_warehouse_payload_does_not_raise(self):
        outcome = parse_warehouse_record({"record_id": "WH-BAD", "sku": "SKU-1"})  # missing required fields
        assert outcome.success is False
        assert outcome.record is None
        assert len(outcome.errors) > 0

    def test_malformed_supplier_payload_does_not_raise(self):
        outcome = parse_supplier_note({"note_id": "SN-BAD"})
        assert outcome.success is False
        assert len(outcome.errors) > 0


# ---------------------------------------------------------------------------
# process_return: the pipeline, including the combined failure mode
# ---------------------------------------------------------------------------

class TestProcessReturn:
    def test_clean_single_line_resolves_normally(self):
        decisions = process_return(
            [wh(claimed_batch_code="BC-2026-0817-A")],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert len(decisions) == 1
        assert decisions[0].disposition == Disposition.RESTOCK

    def test_both_failure_modes_at_once_same_line(self):
        """The brief's actual test case: a warehouse scanner that returns a
        garbled batch code, and supplier credit notes that arrive out of
        order, on the same return line, at the same time. Must still
        produce a defensible decision, no crash, no default to one source.
        """
        warehouse = wh(
            raw_scanner_output="BC-2O26-O817-A",  # garbled (letter O for zero)
            claimed_batch_code=None,
            condition_grade=ConditionGrade.SELLABLE,
        )
        notes = [
            sn(  # the earlier note, with a wrong batch code, arrives LAST
                note_id="SN-A", sequence_number=1,
                received_at=datetime(2026, 1, 1, 9, 0),
                claimed_batch_code="WRONG-CODE",
            ),
            sn(  # the correcting note, with the right code, arrives FIRST
                note_id="SN-B", sequence_number=2,
                received_at=datetime(2026, 1, 1, 8, 0),
                claimed_batch_code="BC-2026-0817-A",
            ),
        ]
        decisions = process_return([warehouse], notes, REGISTRY)
        assert len(decisions) == 1
        decision = decisions[0]

        # proves sequence_number won, not arrival order: if arrival order
        # had won, the resolved batch would be "WRONG-CODE" or unresolved
        assert decision.resolved_batch_code == "BC-2026-0817-A"
        assert decision.disposition == Disposition.RESTOCK
        assert decision.temporal_bucket == "2027-08"

    def test_missing_warehouse_evidence_quarantines(self):
        decisions = process_return([], [sn(claimed_batch_code="BC-2026-0817-A")], REGISTRY)
        assert decisions[0].disposition == Disposition.QUARANTINE
        assert decisions[0].rule_outcomes[0].conflict_type == ConflictType.MISSING_COUNTERPART

    def test_missing_supplier_evidence_quarantines(self):
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], [], REGISTRY)
        assert decisions[0].disposition == Disposition.QUARANTINE
        assert decisions[0].rule_outcomes[0].conflict_type == ConflictType.MISSING_COUNTERPART

    def test_unresolved_note_conflict_quarantines_downstream(self):
        notes = [
            sn(note_id="SN-A", sequence_number=1, eligible_for_credit=True, credit_quantity=10),
            sn(note_id="SN-B", sequence_number=1, eligible_for_credit=False, credit_quantity=2),
        ]
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], notes, REGISTRY)
        assert decisions[0].disposition == Disposition.QUARANTINE
        assert decisions[0].temporal_bucket == UNRESOLVED_BUCKET

    def test_one_bad_line_does_not_sink_the_batch(self):
        line_1_warehouse = wh(record_id="WH-1", return_line_id="RL-1", claimed_batch_code="BC-2026-0817-A")
        line_2_warehouse = wh(record_id="WH-2", return_line_id="RL-2", claimed_batch_code="BC-2026-0817-A")
        line_1_note = sn(note_id="SN-1", return_line_id="RL-1", claimed_batch_code="BC-2026-0817-A")
        # RL-2 has no supplier note at all, should quarantine, not crash the whole call

        decisions = process_return([line_1_warehouse, line_2_warehouse], [line_1_note], REGISTRY)
        by_line = {d.return_line_id: d for d in decisions}

        assert by_line["RL-1"].disposition == Disposition.RESTOCK
        assert by_line["RL-2"].disposition == Disposition.QUARANTINE

    def test_internal_error_on_one_line_falls_back_to_quarantine_not_a_crash(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("simulated bug")

        monkeypatch.setattr("reconciliation.ingestion.resolve_authoritative_supplier_note", boom)

        decisions = process_return(
            [wh(claimed_batch_code="BC-2026-0817-A")],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert decisions[0].disposition == Disposition.QUARANTINE
        assert decisions[0].rule_outcomes[0].conflict_type == ConflictType.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Property-based: process_return over arbitrary groupings
# ---------------------------------------------------------------------------

_line_ids = st.sampled_from(["RL-1", "RL-2", "RL-3"])

_warehouse_strategy = st.builds(
    WarehouseInspectionRecord,
    record_id=st.text(min_size=1, max_size=8),
    return_line_id=_line_ids,
    sku=st.just("SKU-123"),
    condition_grade=st.sampled_from(list(ConditionGrade)),
    inspected_quantity=st.integers(min_value=0, max_value=1000),
    raw_scanner_output=st.one_of(st.none(), st.text(max_size=20)),
    claimed_batch_code=st.one_of(st.none(), st.text(max_size=20)),
    batch_scan_confidence=st.none(),
    best_before_date=st.none(),
    inspector_id=st.none(),
    inspector_has_photo_evidence=st.booleans(),
    inspected_at=st.datetimes(),
)

_supplier_strategy = st.builds(
    SupplierCreditNote,
    note_id=st.text(min_size=1, max_size=8),
    return_line_id=_line_ids,
    sku=st.just("SKU-123"),
    sequence_number=st.integers(min_value=0, max_value=20),
    generated_at=st.datetimes(),
    received_at=st.datetimes(),
    claimed_batch_code=st.one_of(st.none(), st.text(max_size=20)),
    claimed_best_before_date=st.none(),
    eligible_for_credit=st.booleans(),
    credit_quantity=st.integers(min_value=0, max_value=1000),
    restock_required=st.booleans(),
    credit_amount=st.none(),
    supersedes_note_id=st.none(),
)


class TestProcessReturnInvariants:
    @given(
        warehouses=st.lists(_warehouse_strategy, min_size=0, max_size=4),
        notes=st.lists(_supplier_strategy, min_size=0, max_size=6),
    )
    @settings(max_examples=150)
    def test_never_raises_for_arbitrary_groupings(self, warehouses, notes):
        process_return(warehouses, notes, REGISTRY)

    @given(
        warehouses=st.lists(_warehouse_strategy, min_size=0, max_size=4),
        notes=st.lists(_supplier_strategy, min_size=0, max_size=6),
    )
    @settings(max_examples=150)
    def test_one_decision_per_line_id_observed(self, warehouses, notes):
        decisions = process_return(warehouses, notes, REGISTRY)
        expected_line_ids = {w.return_line_id for w in warehouses} | {n.return_line_id for n in notes}
        assert {d.return_line_id for d in decisions} == expected_line_ids


# ---------------------------------------------------------------------------
# reason_code: the sequencing outcome and the three missing-evidence
# variants, this is new surface, no prior test checked any of these strings
# ---------------------------------------------------------------------------

class TestReasonCodes:
    def test_single_note_reason_code(self):
        decisions = process_return(
            [wh(claimed_batch_code="BC-2026-0817-A")], [sn(claimed_batch_code="BC-2026-0817-A")], REGISTRY
        )
        seq_outcome = next(o for o in decisions[0].rule_outcomes if o.conflict_type == ConflictType.SUPPLIER_NOTE_SEQUENCING)
        assert seq_outcome.reason_code == "SEQUENCING_SINGLE_NOTE"

    def test_arrival_order_overridden_reason_code(self):
        notes = [
            sn(note_id="SN-1", sequence_number=1, received_at=datetime(2026, 1, 1, 9, 0)),
            sn(note_id="SN-2", sequence_number=2, received_at=datetime(2026, 1, 1, 8, 0)),
        ]
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], notes, REGISTRY)
        seq_outcome = next(o for o in decisions[0].rule_outcomes if o.conflict_type == ConflictType.SUPPLIER_NOTE_SEQUENCING)
        assert seq_outcome.reason_code == "SEQUENCING_ARRIVAL_ORDER_OVERRIDDEN"

    def test_order_confirmed_reason_code_when_sequence_and_arrival_agree(self):
        notes = [
            sn(note_id="SN-1", sequence_number=1, received_at=datetime(2026, 1, 1, 8, 0)),
            sn(note_id="SN-2", sequence_number=2, received_at=datetime(2026, 1, 1, 9, 0)),
        ]
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], notes, REGISTRY)
        seq_outcome = next(o for o in decisions[0].rule_outcomes if o.conflict_type == ConflictType.SUPPLIER_NOTE_SEQUENCING)
        assert seq_outcome.reason_code == "SEQUENCING_ORDER_CONFIRMED"

    def test_missing_warehouse_evidence_reason_code(self):
        decisions = process_return([], [sn(claimed_batch_code="BC-2026-0817-A")], REGISTRY)
        assert decisions[0].rule_outcomes[0].reason_code == "MISSING_WAREHOUSE_EVIDENCE"

    def test_missing_supplier_evidence_reason_code(self):
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], [], REGISTRY)
        assert decisions[0].rule_outcomes[0].reason_code == "MISSING_SUPPLIER_EVIDENCE"

    def test_unresolved_sequencing_conflict_reason_code(self):
        notes = [
            sn(note_id="SN-A", sequence_number=1, eligible_for_credit=True, credit_quantity=10),
            sn(note_id="SN-B", sequence_number=1, eligible_for_credit=False, credit_quantity=2),
        ]
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], notes, REGISTRY)
        assert decisions[0].rule_outcomes[0].reason_code == "SEQUENCING_UNRESOLVED_CONFLICT"

    def test_internal_error_reason_code(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("simulated bug")

        monkeypatch.setattr("reconciliation.ingestion.resolve_authoritative_supplier_note", boom)
        decisions = process_return(
            [wh(claimed_batch_code="BC-2026-0817-A")], [sn(claimed_batch_code="BC-2026-0817-A")], REGISTRY
        )
        assert decisions[0].rule_outcomes[0].reason_code == "INTERNAL_ERROR"