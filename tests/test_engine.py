"""
Integration tests for reconcile_line_item (engine.py). This is where the
five rules from rules.py actually combine, unit-correct rules can still
produce a wrong decision if the aggregation logic mis-assembles them,
that's what this file is for.

Does NOT test the brief's full "both failure modes at once" scenario,
that needs multiple, possibly out-of-order supplier notes, which
reconcile_line_item doesn't accept yet. That test belongs to the
ingestion milestone, next.
"""

from datetime import date, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from reconciliation.engine import UNRESOLVED_BUCKET, reconcile_line_item
from reconciliation.rules import ConflictType
from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    DamageType,
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
# Straightforward end-to-end cases
# ---------------------------------------------------------------------------

class TestReconcileLineItemGoldenCases:
    def test_clean_sellable_agreement_restocks(self):
        decision = reconcile_line_item(
            wh(condition_grade=ConditionGrade.SELLABLE, claimed_batch_code="BC-2026-0817-A"),
            sn(claimed_batch_code="BC-2026-0817-A", restock_required=True),
            REGISTRY,
        )
        assert decision.disposition == Disposition.RESTOCK
        assert decision.temporal_bucket == "2027-08"
        assert decision.resolved_batch_code == "BC-2026-0817-A"

    def test_major_damage_scraps_regardless_of_supplier_restock_claim(self):
        decision = reconcile_line_item(
            wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, claimed_batch_code="BC-2026-0817-A",
               inspector_has_photo_evidence=True),
            sn(claimed_batch_code="BC-2026-0817-A", restock_required=True),
            REGISTRY,
        )
        assert decision.disposition == Disposition.SCRAP

    def test_garbled_batch_code_alone_repairs_cleanly(self):
        decision = reconcile_line_item(
            wh(raw_scanner_output="BC-2O26-O817-A", claimed_batch_code=None,
               condition_grade=ConditionGrade.SELLABLE),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert decision.resolved_batch_code == "BC-2026-0817-A"
        assert decision.disposition == Disposition.RESTOCK  # batch resolved, condition fine

    def test_garbled_batch_code_unrepairable_quarantines_and_does_not_guess(self):
        decision = reconcile_line_item(
            wh(raw_scanner_output="###UNREADABLE###", claimed_batch_code=None,
               condition_grade=ConditionGrade.SELLABLE),
            sn(claimed_batch_code="ALSO-NOT-IN-REGISTRY"),
            REGISTRY,
        )
        assert decision.disposition == Disposition.QUARANTINE
        assert decision.temporal_bucket == UNRESOLVED_BUCKET
        assert decision.resolved_batch_code is None

    def test_unknown_condition_quarantines_even_with_everything_else_clean(self):
        decision = reconcile_line_item(
            wh(condition_grade=ConditionGrade.UNKNOWN, claimed_batch_code="BC-2026-0817-A"),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert decision.disposition == Disposition.QUARANTINE

    def test_multiple_conflicts_at_once_resolve_independently_and_correctly(self):
        # condition conflict + quantity overclaim + eligibility override + a
        # garbled batch code, all on the same line item, all at once
        decision = reconcile_line_item(
            wh(
                condition_grade=ConditionGrade.MAJOR_DAMAGE,
                inspector_has_photo_evidence=True,
                inspected_quantity=4,
                raw_scanner_output="BC-2O26-O817-A",
                claimed_batch_code=None,
            ),
            sn(
                restock_required=True,       # conflicts with MAJOR_DAMAGE
                credit_quantity=9,             # over the physical count of 4
                eligible_for_credit=False,     # conflicts with MAJOR_DAMAGE + photo evidence
                claimed_batch_code="BC-2026-0817-A",
            ),
            REGISTRY,
        )
        assert decision.disposition == Disposition.SCRAP          # condition rule
        assert decision.physical_quantity == 4                     # warehouse's actual count
        assert decision.creditable_quantity == 4                    # quantity rule capped it
        assert decision.eligible_for_credit is True                 # eligibility rule overrode it
        assert decision.resolved_batch_code == "BC-2026-0817-A"    # batch rule repaired it
        assert decision.temporal_bucket == "2027-08"                # best-before followed the batch
        assert len(decision.rule_outcomes) == 5  # reconcile_line_item's 5, sequencing is ingestion's job


# ---------------------------------------------------------------------------
# ADR-006: the engine must never propagate a crash
# ---------------------------------------------------------------------------

class TestEngineNeverCrashes:
    def test_internal_error_in_a_rule_falls_back_to_quarantine(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("simulated bug in a rule")

        monkeypatch.setattr("reconciliation.engine.resolve_condition", boom)

        decision = reconcile_line_item(wh(), sn(), REGISTRY)

        assert decision.disposition == Disposition.QUARANTINE
        assert decision.temporal_bucket == UNRESOLVED_BUCKET
        assert len(decision.rule_outcomes) == 1
        assert decision.rule_outcomes[0].conflict_type == ConflictType.INTERNAL_ERROR
        assert decision.rule_outcomes[0].triggers_quarantine is True


# ---------------------------------------------------------------------------
# Property-based invariants
# ---------------------------------------------------------------------------

_warehouse_strategy = st.builds(
    WarehouseInspectionRecord,
    record_id=st.text(min_size=1, max_size=8),
    return_line_id=st.just("RL-1"),
    sku=st.just("SKU-123"),
    condition_grade=st.sampled_from(list(ConditionGrade)),
    damage_type=st.sampled_from(list(DamageType)),
    inspected_quantity=st.integers(min_value=0, max_value=1000),
    raw_scanner_output=st.one_of(st.none(), st.text(max_size=20)),
    claimed_batch_code=st.one_of(st.none(), st.text(max_size=20)),
    batch_scan_confidence=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)),
    best_before_date=st.none(),
    inspector_id=st.none(),
    inspector_has_photo_evidence=st.booleans(),
    inspected_at=st.datetimes(),
)

_supplier_strategy = st.builds(
    SupplierCreditNote,
    note_id=st.text(min_size=1, max_size=8),
    return_line_id=st.just("RL-1"),
    sku=st.just("SKU-123"),
    sequence_number=st.integers(min_value=0, max_value=1000),
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


class TestEngineInvariants:
    @given(warehouse=_warehouse_strategy, supplier=_supplier_strategy)
    @settings(max_examples=200)
    def test_never_raises(self, warehouse, supplier):
        reconcile_line_item(warehouse, supplier, REGISTRY)

    @given(warehouse=_warehouse_strategy, supplier=_supplier_strategy)
    @settings(max_examples=200)
    def test_disposition_always_one_of_three_valid_values(self, warehouse, supplier):
        decision = reconcile_line_item(warehouse, supplier, REGISTRY)
        assert decision.disposition in (
            Disposition.SCRAP,
            Disposition.RESTOCK,
            Disposition.QUARANTINE,
        )

    @given(warehouse=_warehouse_strategy, supplier=_supplier_strategy)
    @settings(max_examples=200)
    def test_idempotent(self, warehouse, supplier):
        first = reconcile_line_item(warehouse, supplier, REGISTRY)
        second = reconcile_line_item(warehouse, supplier, REGISTRY)
        assert first == second

    @given(warehouse=_warehouse_strategy, supplier=_supplier_strategy)
    @settings(max_examples=200)
    def test_creditable_quantity_never_exceeds_physical_quantity(self, warehouse, supplier):
        decision = reconcile_line_item(warehouse, supplier, REGISTRY)
        assert decision.creditable_quantity <= decision.physical_quantity