"""
Unit tests for the five rules (rules.py), one rule per section, named
after DECISION_RULES.md. Each function is tested in isolation, engine
level integration tests live in test_engine.py.
"""

from datetime import date, datetime

import pytest

from reconciliation.rules import (
    BatchRepairResult,
    ConflictType,
    Winner,
    repair_batch_code,
    resolve_batch_code,
    resolve_best_before,
    resolve_condition,
    resolve_eligibility,
    resolve_quantity,
)
from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
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
    BatchRegistryEntry(
        batch_code="BC-2026-0817-B",
        sku="SKU-123",
        manufactured_date=date(2026, 8, 17),
        best_before_date=date(2027, 9, 1),
    ),
]



# Rule 1: resolve_condition


class TestResolveCondition:
    def test_agreement_no_conflict(self):
        outcome = resolve_condition(
            wh(condition_grade=ConditionGrade.SELLABLE), sn(restock_required=True)
        )
        assert outcome.conflict_detected is False
        assert outcome.winner == Winner.AGREEMENT
        assert outcome.resolved_value is True

    def test_conflict_warehouse_wins_with_photo_evidence(self):
        outcome = resolve_condition(
            wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, inspector_has_photo_evidence=True),
            sn(restock_required=True),
        )
        assert outcome.conflict_detected is True
        assert outcome.winner == Winner.WAREHOUSE
        assert outcome.resolved_value is False
        assert outcome.confidence == 1.0

    def test_conflict_warehouse_wins_without_photo_evidence_at_reduced_confidence(self):
        outcome = resolve_condition(
            wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, inspector_has_photo_evidence=False),
            sn(restock_required=True),
        )
        assert outcome.winner == Winner.WAREHOUSE
        assert outcome.confidence < 1.0
        assert outcome.confidence > 0.0

    def test_unknown_condition_is_unresolved_and_triggers_quarantine(self):
        outcome = resolve_condition(
            wh(condition_grade=ConditionGrade.UNKNOWN), sn(restock_required=True)
        )
        assert outcome.winner == Winner.UNRESOLVED
        assert outcome.resolved_value is None
        assert outcome.triggers_quarantine is True



# repair_batch_code


class TestRepairBatchCode:
    def test_exact_match(self):
        result = repair_batch_code("BC-2026-0817-A", REGISTRY)
        assert result.matched_code == "BC-2026-0817-A"
        assert result.confidence == 1.0
        assert result.ambiguous is False

    def test_garbled_but_recoverable(self):
        result = repair_batch_code("BC-2O26-O817-A", REGISTRY)  # letter O for zero
        assert result.matched_code == "BC-2026-0817-A"
        assert result.confidence >= 0.8

    def test_below_threshold_returns_no_match(self):
        result = repair_batch_code("COMPLETELY-UNRELATED-TEXT", REGISTRY, threshold=0.8)
        assert result.matched_code is None

    def test_none_input_returns_no_match(self):
        result = repair_batch_code(None, REGISTRY)
        assert result.matched_code is None
        assert result.confidence == 0.0

    def test_empty_registry_returns_no_match(self):
        result = repair_batch_code("BC-2026-0817-A", [])
        assert result.matched_code is None

    def test_tie_between_equally_good_candidates_is_ambiguous(self):
        tied_registry = [
            BatchRegistryEntry(
                batch_code="AAAA", sku="SKU-1",
                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
            ),
            BatchRegistryEntry(
                batch_code="AAAB", sku="SKU-1",
                manufactured_date=date(2026, 1, 1), best_before_date=date(2027, 1, 1),
            ),
        ]
        # "AAAC" is equidistant (edit distance 1) from both AAAA and AAAB
        result = repair_batch_code("AAAC", tied_registry, threshold=0.5)
        assert result.matched_code is None
        assert result.ambiguous is True



# Rule 2: resolve_batch_code


class TestResolveBatchCode:
    def test_exact_agreement_no_registry_needed(self):
        outcome = resolve_batch_code(
            wh(claimed_batch_code="BC-2026-0817-A"),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert outcome.conflict_detected is False
        assert outcome.winner == Winner.AGREEMENT
        assert outcome.resolved_value == "BC-2026-0817-A"

    def test_garbled_warehouse_repairs_and_corroborates_supplier(self):
        outcome = resolve_batch_code(
            wh(raw_scanner_output="BC-2O26-O817-A", claimed_batch_code=None),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert outcome.winner == Winner.AGREEMENT
        assert outcome.resolved_value == "BC-2026-0817-A"
        assert outcome.triggers_quarantine is False

    def test_only_warehouse_validates_warehouse_wins(self):
        outcome = resolve_batch_code(
            wh(raw_scanner_output="BC-2O26-O817-A", claimed_batch_code=None),
            sn(claimed_batch_code="NOT-IN-REGISTRY"),
            REGISTRY,
        )
        assert outcome.winner == Winner.WAREHOUSE
        assert outcome.resolved_value == "BC-2026-0817-A"

    def test_only_supplier_validates_supplier_wins(self):
        # this is the case that proves there's no fixed source preference
        outcome = resolve_batch_code(
            wh(raw_scanner_output="###UNREADABLE###", claimed_batch_code=None),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert outcome.winner == Winner.SUPPLIER
        assert outcome.resolved_value == "BC-2026-0817-A"

    def test_neither_validates_is_unresolved_and_triggers_quarantine(self):
        outcome = resolve_batch_code(
            wh(raw_scanner_output="###UNREADABLE###", claimed_batch_code=None),
            sn(claimed_batch_code="ALSO-NOT-IN-REGISTRY"),
            REGISTRY,
        )
        assert outcome.winner == Winner.UNRESOLVED
        assert outcome.resolved_value is None
        assert outcome.triggers_quarantine is True



# Rule 3: resolve_best_before


class TestResolveBestBefore:
    def test_resolved_batch_uses_registry_date_not_either_partys_claim(self):
        warehouse = wh(claimed_batch_code="BC-2026-0817-A")
        supplier = sn(claimed_batch_code="BC-2026-0817-A")
        batch_outcome = resolve_batch_code(warehouse, supplier, REGISTRY)
        outcome = resolve_best_before(warehouse, supplier, batch_outcome, REGISTRY)
        assert outcome.winner == Winner.REGISTRY
        assert outcome.resolved_value == date(2027, 8, 17)  # the registry's date, not a guess

    def test_unresolved_batch_leaves_best_before_unresolved(self):
        warehouse = wh(raw_scanner_output="###", claimed_batch_code=None)
        supplier = sn(claimed_batch_code="ALSO-BAD")
        batch_outcome = resolve_batch_code(warehouse, supplier, REGISTRY)
        outcome = resolve_best_before(warehouse, supplier, batch_outcome, REGISTRY)
        assert outcome.resolved_value is None
        assert outcome.triggers_quarantine is True

    def test_agreeing_dates_are_not_flagged_as_a_conflict(self):
        warehouse = wh(claimed_batch_code="BC-2026-0817-A", best_before_date=date(2027, 8, 17))
        supplier = sn(claimed_batch_code="BC-2026-0817-A", claimed_best_before_date=date(2027, 8, 17))
        batch_outcome = resolve_batch_code(warehouse, supplier, REGISTRY)
        outcome = resolve_best_before(warehouse, supplier, batch_outcome, REGISTRY)
        assert outcome.conflict_detected is False

    def test_disagreeing_dates_are_flagged_even_though_registry_still_wins(self):
        # this is the actual bug caught in review: a real disagreement
        # between the two parties used to be silently reported as
        # conflict_detected=False just because the registry lookup
        # itself succeeded. The resolution (registry wins) was always
        # correct, only the conflict reporting was wrong.
        warehouse = wh(claimed_batch_code="BC-2026-0817-A", best_before_date=date(2027, 9, 1))
        supplier = sn(claimed_batch_code="BC-2026-0817-A", claimed_best_before_date=date(2027, 8, 1))
        batch_outcome = resolve_batch_code(warehouse, supplier, REGISTRY)
        outcome = resolve_best_before(warehouse, supplier, batch_outcome, REGISTRY)

        assert outcome.conflict_detected is True
        assert outcome.winner == Winner.REGISTRY  # still the registry, disagreement doesn't change who wins
        assert outcome.resolved_value == date(2027, 8, 17)  # still the actual registry date
        assert len(outcome.evidence_discarded) == 2
        assert any("2027-09-01" in e for e in outcome.evidence_discarded)
        assert any("2027-08-01" in e for e in outcome.evidence_discarded)

    def test_one_side_silent_on_best_before_is_not_treated_as_disagreement(self):
        # only one party stating a date isn't a disagreement, there's
        # nothing to disagree with
        warehouse = wh(claimed_batch_code="BC-2026-0817-A", best_before_date=date(2027, 9, 1))
        supplier = sn(claimed_batch_code="BC-2026-0817-A", claimed_best_before_date=None)
        batch_outcome = resolve_batch_code(warehouse, supplier, REGISTRY)
        outcome = resolve_best_before(warehouse, supplier, batch_outcome, REGISTRY)
        assert outcome.conflict_detected is False



# Rule 4: resolve_quantity


class TestResolveQuantity:
    def test_supplier_overclaim_is_capped(self):
        outcome = resolve_quantity(wh(inspected_quantity=3), sn(credit_quantity=10))
        assert outcome.conflict_detected is True
        assert outcome.resolved_value == 3

    def test_supplier_exact_match_passes_through_clean(self):
        outcome = resolve_quantity(wh(inspected_quantity=10), sn(credit_quantity=10))
        assert outcome.conflict_detected is False
        assert outcome.resolved_value == 10

    def test_supplier_underclaim_is_flagged_not_silently_accepted(self):
        # problem caught in review: this used to be treated as "no conflict"
        outcome = resolve_quantity(wh(inspected_quantity=10), sn(credit_quantity=7))
        assert outcome.conflict_detected is True
        assert outcome.resolved_value == 7



# Rule 5: resolve_eligibility


class TestResolveEligibility:
    def test_supplier_already_eligible_no_conflict(self):
        outcome = resolve_eligibility(wh(), sn(eligible_for_credit=True))
        assert outcome.conflict_detected is False
        assert outcome.resolved_value is True

    def test_unknown_condition_disputed_is_the_genuine_tossup(self):
        outcome = resolve_eligibility(
            wh(condition_grade=ConditionGrade.UNKNOWN), sn(eligible_for_credit=False)
        )
        assert outcome.winner == Winner.UNRESOLVED
        assert outcome.triggers_quarantine is True

    def test_major_damage_with_photo_overrides_at_full_confidence(self):
        outcome = resolve_eligibility(
            wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, inspector_has_photo_evidence=True),
            sn(eligible_for_credit=False),
        )
        assert outcome.winner == Winner.WAREHOUSE
        assert outcome.resolved_value is True
        assert outcome.confidence == 1.0

    def test_major_damage_without_photo_still_overrides_at_reduced_confidence(self):
        # this is the case that used to be a contradiction with Rule 1, now fixed
        outcome = resolve_eligibility(
            wh(condition_grade=ConditionGrade.MAJOR_DAMAGE, inspector_has_photo_evidence=False),
            sn(eligible_for_credit=False),
        )
        assert outcome.winner == Winner.WAREHOUSE
        assert outcome.resolved_value is True
        assert 0.0 < outcome.confidence < 1.0

    def test_sellable_ineligible_supplier_stands(self):
        outcome = resolve_eligibility(
            wh(condition_grade=ConditionGrade.SELLABLE), sn(eligible_for_credit=False)
        )
        assert outcome.winner == Winner.SUPPLIER
        assert outcome.resolved_value is False



# reason_code: spot-checked per rule, not exhaustively per branch, decision
# correctness for each branch is already covered above, this only confirms
# the metadata is actually threaded through


class TestReasonCodes:
    def test_condition_codes(self):
        assert resolve_condition(wh(condition_grade=ConditionGrade.UNKNOWN), sn()).reason_code == "CONDITION_UNKNOWN_UNRESOLVED"
        assert resolve_condition(wh(condition_grade=ConditionGrade.SELLABLE), sn(restock_required=True)).reason_code == "CONDITION_AGREEMENT"
        assert resolve_condition(wh(condition_grade=ConditionGrade.MAJOR_DAMAGE), sn(restock_required=True)).reason_code == "CONDITION_WAREHOUSE_OVERRIDE"

    def test_batch_code_codes(self):
        assert resolve_batch_code(
            wh(claimed_batch_code="BC-2026-0817-A"), sn(claimed_batch_code="BC-2026-0817-A"), REGISTRY
        ).reason_code == "BATCH_EXACT_AGREEMENT"
        assert resolve_batch_code(
            wh(raw_scanner_output="BC-2O26-O817-A", claimed_batch_code=None),
            sn(claimed_batch_code="BC-2026-0817-A"), REGISTRY,
        ).reason_code == "BATCH_REPAIRED_AND_CORROBORATED"
        assert resolve_batch_code(
            wh(raw_scanner_output="###", claimed_batch_code=None),
            sn(claimed_batch_code="ALSO-BAD"), REGISTRY,
        ).reason_code == "BATCH_UNRESOLVED"

    def test_quantity_codes(self):
        assert resolve_quantity(wh(inspected_quantity=3), sn(credit_quantity=10)).reason_code == "QUANTITY_CAPPED_TO_PHYSICAL_COUNT"
        assert resolve_quantity(wh(inspected_quantity=10), sn(credit_quantity=7)).reason_code == "QUANTITY_UNDERCLAIM_FLAGGED"
        assert resolve_quantity(wh(inspected_quantity=10), sn(credit_quantity=10)).reason_code == "QUANTITY_EXACT_MATCH"

    def test_eligibility_codes(self):
        assert resolve_eligibility(wh(condition_grade=ConditionGrade.UNKNOWN), sn(eligible_for_credit=False)).reason_code == "ELIGIBILITY_UNKNOWN_CONDITION_UNRESOLVED"
        assert resolve_eligibility(wh(condition_grade=ConditionGrade.MAJOR_DAMAGE), sn(eligible_for_credit=False)).reason_code == "ELIGIBILITY_WAREHOUSE_DAMAGE_OVERRIDE"
        assert resolve_eligibility(wh(condition_grade=ConditionGrade.SELLABLE), sn(eligible_for_credit=False)).reason_code == "ELIGIBILITY_SUPPLIER_STANDS"

    def test_best_before_codes(self):
        warehouse_ok = wh(claimed_batch_code="BC-2026-0817-A")
        supplier_ok = sn(claimed_batch_code="BC-2026-0817-A")
        batch_outcome = resolve_batch_code(warehouse_ok, supplier_ok, REGISTRY)
        assert resolve_best_before(warehouse_ok, supplier_ok, batch_outcome, REGISTRY).reason_code == "BEST_BEFORE_FROM_REGISTRY"

        warehouse_bad = wh(raw_scanner_output="###", claimed_batch_code=None)
        supplier_bad = sn(claimed_batch_code="BAD")
        unresolved_batch = resolve_batch_code(warehouse_bad, supplier_bad, REGISTRY)
        assert resolve_best_before(warehouse_bad, supplier_bad, unresolved_batch, REGISTRY).reason_code == "BEST_BEFORE_UNRESOLVED_BATCH"



# Rule 2's candidate list (detail), the actual fix for "audit trail claims
# candidates were considered but doesn't show them"


class TestBatchRepairCandidateList:
    def test_all_candidates_present_and_sorted_descending_by_confidence(self):
        result = repair_batch_code("BC-2O26-O817-A", REGISTRY)  # closer to -A than -B
        assert len(result.all_candidates) == len(REGISTRY)
        scores = [score for _, score in result.all_candidates]
        assert scores == sorted(scores, reverse=True)

    def test_threshold_is_reported_alongside_candidates(self):
        result = repair_batch_code("BC-2026-0817-A", REGISTRY, threshold=0.8)
        assert result.threshold == 0.8

    def test_no_candidate_text_still_returns_empty_list_not_a_crash(self):
        result = repair_batch_code(None, REGISTRY)
        assert result.all_candidates == ()

    def test_resolve_batch_code_populates_detail_when_repair_was_attempted(self):
        outcome = resolve_batch_code(
            wh(raw_scanner_output="BC-2O26-O817-A", claimed_batch_code=None),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert "candidates" in outcome.detail
        assert "threshold" in outcome.detail
        assert len(outcome.detail["candidates"]) == len(REGISTRY)
        assert outcome.detail["candidates"][0]["batch_code"] == "BC-2026-0817-A"

    def test_resolve_batch_code_detail_empty_when_no_repair_needed(self):
        # exact string agreement never calls repair_batch_code at all
        outcome = resolve_batch_code(
            wh(claimed_batch_code="BC-2026-0817-A"),
            sn(claimed_batch_code="BC-2026-0817-A"),
            REGISTRY,
        )
        assert outcome.detail == {}