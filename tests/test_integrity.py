"""
Tests for the six gaps named in review, before this file existed they
were named limitations, this is what actually closes each one:

1. Large-batch / no-cross-item-state-leakage  -> TestLargeBatch
2. No git history                              -> not a code problem, N/A here
3. warehouse.sku == supplier.sku unchecked    -> TestSkuMismatch
4. Multiple warehouse records silently last-wins -> TestConflictingWarehouseRecords
5. reason_code not validated against a closed set -> TestReasonCodeClosedSet
6. Registry treated as ground truth, no self-check -> TestRegistryIntegrity
"""

from datetime import date, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from reconciliation.ingestion import process_return, validate_registry_integrity
from reconciliation.rules import KNOWN_REASON_CODES, ConflictType
from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    Disposition,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)


def wh(**overrides):
    base = dict(
        record_id="WH-1", return_line_id="RL-1", sku="SKU-123",
        condition_grade=ConditionGrade.SELLABLE, inspected_quantity=10,
        inspected_at=datetime(2026, 1, 1),
    )
    base.update(overrides)
    return WarehouseInspectionRecord(**base)


def sn(**overrides):
    base = dict(
        note_id="SN-1", return_line_id="RL-1", sku="SKU-123",
        sequence_number=1, generated_at=datetime(2026, 1, 1), received_at=datetime(2026, 1, 1),
        eligible_for_credit=True, credit_quantity=10, restock_required=True,
    )
    base.update(overrides)
    return SupplierCreditNote(**base)


REGISTRY = [
    BatchRegistryEntry(
        batch_code="BC-2026-0817-A", sku="SKU-123",
        manufactured_date=date(2026, 8, 17), best_before_date=date(2027, 8, 17),
    ),
]



# 3. SKU mismatch between warehouse and supplier

class TestSkuMismatch:
    def test_mismatched_sku_quarantines_with_named_reason(self):
        decisions = process_return(
            [wh(sku="SKU-123")],
            [sn(sku="SKU-999")],  # different SKU, same return_line_id
            REGISTRY,
        )
        assert len(decisions) == 1
        assert decisions[0].disposition == Disposition.QUARANTINE
        outcome = decisions[0].rule_outcomes[0]
        assert outcome.reason_code == "SKU_MISMATCH"
        assert outcome.conflict_type == ConflictType.IDENTITY_MISMATCH

    def test_matching_sku_proceeds_normally(self):
        decisions = process_return(
            [wh(sku="SKU-123", claimed_batch_code="BC-2026-0817-A")],
            [sn(sku="SKU-123", claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert decisions[0].disposition == Disposition.RESTOCK



# 4. Multiple warehouse records for one return line

class TestConflictingWarehouseRecords:
    def test_conflicting_duplicate_warehouse_records_quarantine(self):
        decisions = process_return(
            [
                wh(record_id="WH-1", condition_grade=ConditionGrade.SELLABLE),
                wh(record_id="WH-2", condition_grade=ConditionGrade.MAJOR_DAMAGE),  # same line, disagrees
            ],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert len(decisions) == 1
        assert decisions[0].disposition == Disposition.QUARANTINE
        assert decisions[0].rule_outcomes[0].reason_code == "CONFLICTING_WAREHOUSE_RECORDS"

    def test_identical_duplicate_warehouse_records_are_harmless(self):
        # a re-delivered identical record shouldn't be treated as a conflict
        identical_kwargs = dict(condition_grade=ConditionGrade.SELLABLE, claimed_batch_code="BC-2026-0817-A")
        decisions = process_return(
            [wh(record_id="WH-1", **identical_kwargs), wh(record_id="WH-1", **identical_kwargs)],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert decisions[0].disposition == Disposition.RESTOCK



# 5. reason_code closed set

_warehouse_strategy = st.builds(
    WarehouseInspectionRecord,
    record_id=st.text(min_size=1, max_size=8),
    return_line_id=st.just("RL-1"),
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
    return_line_id=st.just("RL-1"),
    sku=st.sampled_from(["SKU-123", "SKU-999"]),  # sometimes mismatched, on purpose
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

class TestReasonCodeClosedSet:
    @given(warehouse=_warehouse_strategy, notes=st.lists(_supplier_strategy, min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_every_emitted_reason_code_is_declared(self, warehouse, notes):
        decisions = process_return([warehouse], notes, REGISTRY)
        for decision in decisions:
            for outcome in decision.rule_outcomes:
                assert outcome.reason_code in KNOWN_REASON_CODES, (
                    f"undeclared reason_code {outcome.reason_code!r} was emitted, "
                    "add it to KNOWN_REASON_CODES in rules.py or fix the typo"
                )



# 6. Registry integrity

class TestRegistryIntegrity:
    def test_valid_entry_constructs(self):
        entry = BatchRegistryEntry(
            batch_code="BC-1", sku="SKU-1",
            manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
        )
        assert entry.batch_code == "BC-1"

    def test_best_before_on_or_before_manufactured_is_rejected(self):
        with pytest.raises(ValidationError):
            BatchRegistryEntry(
                batch_code="BC-BAD", sku="SKU-1",
                manufactured_date=date(2026, 6, 1), best_before_date=date(2026, 1, 1),
            )

    def test_best_before_equal_to_manufactured_is_rejected(self):
        with pytest.raises(ValidationError):
            BatchRegistryEntry(
                batch_code="BC-BAD", sku="SKU-1",
                manufactured_date=date(2026, 6, 1), best_before_date=date(2026, 6, 1),
            )



# 1. Large batch, no cross-item state leakage

class TestLargeBatch:
    def test_five_hundred_independent_lines_no_cross_contamination(self):
        n = 500
        registry = [
            BatchRegistryEntry(
                batch_code=f"BC-{i:04d}", sku=f"SKU-{i:04d}",
                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
            )
            for i in range(n)
        ]
        warehouse_records = [
            wh(record_id=f"WH-{i}", return_line_id=f"RL-{i}", sku=f"SKU-{i:04d}",
               claimed_batch_code=f"BC-{i:04d}", condition_grade=ConditionGrade.SELLABLE)
            for i in range(n)
        ]
        supplier_notes = [
            sn(note_id=f"SN-{i}", return_line_id=f"RL-{i}", sku=f"SKU-{i:04d}",
               claimed_batch_code=f"BC-{i:04d}")
            for i in range(n)
        ]

        decisions = process_return(warehouse_records, supplier_notes, registry)

        assert len(decisions) == n
        by_line = {d.return_line_id: d for d in decisions}
        for i in range(n):
            decision = by_line[f"RL-{i}"]
            # each item's resolved batch must be its OWN batch, never another
            # item's, this is the actual "no cross-item state leakage" check
            assert decision.resolved_batch_code == f"BC-{i:04d}", (
                f"RL-{i} resolved to {decision.resolved_batch_code!r}, "
                f"expected its own batch BC-{i:04d}, possible cross-item leakage"
            )
            assert decision.disposition == Disposition.RESTOCK

    def test_large_batch_with_one_bad_line_does_not_corrupt_the_rest(self):
        n = 200
        registry = [
            BatchRegistryEntry(
                batch_code=f"BC-{i:04d}", sku=f"SKU-{i:04d}",
                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
            )
            for i in range(n)
        ]
        warehouse_records = [
            wh(record_id=f"WH-{i}", return_line_id=f"RL-{i}", sku=f"SKU-{i:04d}",
               claimed_batch_code=f"BC-{i:04d}", condition_grade=ConditionGrade.SELLABLE)
            for i in range(n)
        ]
        # line RL-50 gets a deliberate SKU mismatch, everything else clean
        supplier_notes = [
            sn(
                note_id=f"SN-{i}", return_line_id=f"RL-{i}",
                sku=("SKU-WRONG" if i == 50 else f"SKU-{i:04d}"),
                claimed_batch_code=f"BC-{i:04d}",
            )
            for i in range(n)
        ]

        decisions = process_return(warehouse_records, supplier_notes, registry)
        by_line = {d.return_line_id: d for d in decisions}

        assert by_line["RL-50"].disposition == Disposition.QUARANTINE
        assert by_line["RL-50"].rule_outcomes[0].reason_code == "SKU_MISMATCH"
        # every other line is unaffected
        for i in range(n):
            if i == 50:
                continue
            assert by_line[f"RL-{i}"].disposition == Disposition.RESTOCK
            assert by_line[f"RL-{i}"].resolved_batch_code == f"BC-{i:04d}"



# Cross-entry registry integrity (validate_registry_integrity)

class TestCrossEntryRegistryIntegrity:
    def test_clean_registry_has_no_warnings(self):
        assert validate_registry_integrity(REGISTRY) == ()

    def test_duplicate_batch_code_with_conflicting_data_is_flagged(self):
        conflicting_registry = [
            BatchRegistryEntry(
                batch_code="BC-DUP", sku="SKU-1",
                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
            ),
            BatchRegistryEntry(
                batch_code="BC-DUP", sku="SKU-1",  # same code, different dates
                manufactured_date=date(2026, 2, 1), best_before_date=date(2027, 2, 1),
            ),
        ]
        warnings = validate_registry_integrity(conflicting_registry)
        assert len(warnings) == 1
        assert "BC-DUP" in warnings[0]

    def test_duplicate_batch_code_with_identical_data_is_not_flagged(self):
        # a re-listed identical entry is harmless, same principle as
        # identical duplicate warehouse/supplier records elsewhere
        entry_kwargs = dict(
            batch_code="BC-SAME", sku="SKU-1",
            manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
        )
        registry = [BatchRegistryEntry(**entry_kwargs), BatchRegistryEntry(**entry_kwargs)]
        assert validate_registry_integrity(registry) == ()

    def test_multiple_distinct_duplicates_each_get_their_own_warning(self):
        registry = [
            BatchRegistryEntry(batch_code="BC-A", sku="SKU-1",
                                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1)),
            BatchRegistryEntry(batch_code="BC-A", sku="SKU-2",
                                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1)),
            BatchRegistryEntry(batch_code="BC-B", sku="SKU-1",
                                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1)),
            BatchRegistryEntry(batch_code="BC-B", sku="SKU-3",
                                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1)),
        ]
        warnings = validate_registry_integrity(registry)
        assert len(warnings) == 2



# Aggregate confidence (LineItemDecision.overall_confidence)

class TestOverallConfidence:
    def test_clean_agreement_is_full_confidence(self):
        decisions = process_return(
            [wh(condition_grade=ConditionGrade.SELLABLE, claimed_batch_code="BC-2026-0817-A")],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert decisions[0].overall_confidence == 1.0

    def test_unknown_condition_drags_confidence_to_zero(self):
        decisions = process_return(
            [wh(condition_grade=ConditionGrade.UNKNOWN, claimed_batch_code="BC-2026-0817-A")],
            [sn(claimed_batch_code="BC-2026-0817-A")],
            REGISTRY,
        )
        assert decisions[0].overall_confidence == 0.0

    def test_confidence_is_the_minimum_not_average_or_batch_only(self):
        # condition confidence is reduced (no photo evidence backing a
        # MAJOR_DAMAGE claim, 0.75), batch confidence is full (1.0);
        # overall must reflect the WEAKER of the two, not an average,
        # and not just whichever rule happens to run last
        decisions = process_return(
            [wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, inspector_has_photo_evidence=False,
                claimed_batch_code="BC-2026-0817-A")],
            [sn(claimed_batch_code="BC-2026-0817-A", restock_required=True)],
            REGISTRY,
        )
        assert decisions[0].overall_confidence == 0.75

    def test_missing_evidence_quarantine_has_zero_confidence(self):
        decisions = process_return([wh(claimed_batch_code="BC-2026-0817-A")], [], REGISTRY)
        assert decisions[0].overall_confidence == 0.0