"""
Tests for audit.py. Deliberately built on hand-constructed RuleOutcome /
LineItemDecision fixtures rather than running the full pipeline, this
file only tests serialization and rendering, decision correctness is
already covered by test_rules.py, test_engine.py, and test_ingestion.py.
"""

import json
from datetime import date, datetime

from reconciliation.audit import (
    _serialize_value,
    decision_to_dict,
    render_decision_box,
    render_decision_report,
    render_shipment_box,
    render_shipment_report,
    rule_outcome_to_dict,
    write_audit_log,
)
from reconciliation.engine import LineItemDecision
from reconciliation.rules import ConflictType, RuleOutcome, Winner
from reconciliation.schemas import Disposition


def outcome(**overrides):
    base = dict(
        conflict_type=ConflictType.CONDITION,
        conflict_detected=True,
        winner=Winner.WAREHOUSE,
        resolved_value=False,
        confidence=0.9,
        reasoning="Example reasoning text.",
        evidence_discarded=("supplier restock_required=True",),
        triggers_quarantine=False,
    )
    base.update(overrides)
    return RuleOutcome(**base)


def decision(**overrides):
    base = dict(
        return_line_id="RL-1",
        sku="SKU-123",
        disposition=Disposition.SCRAP,
        temporal_bucket="2027-08",
        resolved_batch_code="BC-2026-0817-A",
        eligible_for_credit=True,
        physical_quantity=4,
        creditable_quantity=4,
        rule_outcomes=(outcome(),),
    )
    base.update(overrides)
    return LineItemDecision(**base)


# ---------------------------------------------------------------------------
# _serialize_value
# ---------------------------------------------------------------------------

class TestSerializeValue:
    def test_enum_becomes_its_value(self):
        assert _serialize_value(Winner.WAREHOUSE) == "warehouse"

    def test_date_becomes_isoformat_string(self):
        assert _serialize_value(date(2027, 8, 17)) == "2027-08-17"

    def test_datetime_becomes_isoformat_string(self):
        assert _serialize_value(datetime(2026, 1, 1, 9, 30)) == "2026-01-01T09:30:00"

    def test_tuple_becomes_list_recursively_serialized(self):
        assert _serialize_value((Winner.SUPPLIER, "plain string")) == ["supplier", "plain string"]

    def test_passthrough_for_json_native_types(self):
        assert _serialize_value(True) is True
        assert _serialize_value(4) == 4
        assert _serialize_value(None) is None
        assert _serialize_value("text") == "text"


# ---------------------------------------------------------------------------
# rule_outcome_to_dict
# ---------------------------------------------------------------------------

class TestRuleOutcomeToDict:
    def test_all_fields_present_with_correct_types(self):
        d = rule_outcome_to_dict(outcome())
        assert d["conflict_type"] == "condition"
        assert d["winner"] == "warehouse"
        assert d["conflict_detected"] is True
        assert d["resolved_value"] is False
        assert d["confidence"] == 0.9
        assert d["reasoning"] == "Example reasoning text."
        assert d["evidence_discarded"] == ["supplier restock_required=True"]
        assert isinstance(d["evidence_discarded"], list)  # not a tuple, JSON needs a list

    def test_date_resolved_value_is_serialized(self):
        d = rule_outcome_to_dict(outcome(resolved_value=date(2027, 8, 17)))
        assert d["resolved_value"] == "2027-08-17"

    def test_none_resolved_value_stays_none(self):
        d = rule_outcome_to_dict(outcome(resolved_value=None, winner=Winner.UNRESOLVED))
        assert d["resolved_value"] is None

    def test_empty_evidence_discarded_is_empty_list(self):
        d = rule_outcome_to_dict(outcome(evidence_discarded=()))
        assert d["evidence_discarded"] == []

    def test_reason_code_is_included(self):
        d = rule_outcome_to_dict(outcome(reason_code="CONDITION_WAREHOUSE_OVERRIDE"))
        assert d["reason_code"] == "CONDITION_WAREHOUSE_OVERRIDE"

    def test_detail_is_included_when_present(self):
        detail = {"candidates": [{"batch_code": "BC-1", "confidence": 0.96}], "threshold": 0.8}
        d = rule_outcome_to_dict(outcome(detail=detail))
        assert d["detail"] == detail

    def test_detail_defaults_to_empty_dict(self):
        d = rule_outcome_to_dict(outcome())
        assert d["detail"] == {}


# ---------------------------------------------------------------------------
# decision_to_dict
# ---------------------------------------------------------------------------

class TestDecisionToDict:
    def test_top_level_structure(self):
        d = decision_to_dict(decision())
        assert d["return_line_id"] == "RL-1"
        assert d["sku"] == "SKU-123"
        assert d["disposition"] == "scrap"  # enum serialized to its value
        assert d["temporal_bucket"] == "2027-08"
        assert d["resolved_batch_code"] == "BC-2026-0817-A"
        assert d["eligible_for_credit"] is True
        assert d["physical_quantity"] == 4
        assert d["creditable_quantity"] == 4
        assert d["requires_human_review"] is False

    def test_requires_human_review_true_for_quarantine(self):
        d = decision_to_dict(decision(disposition=Disposition.QUARANTINE))
        assert d["requires_human_review"] is True

    def test_rule_outcomes_is_a_list_of_dicts(self):
        d = decision_to_dict(decision(rule_outcomes=(outcome(), outcome(conflict_type=ConflictType.QUANTITY))))
        assert isinstance(d["rule_outcomes"], list)
        assert len(d["rule_outcomes"]) == 2
        assert d["rule_outcomes"][1]["conflict_type"] == "quantity"


# ---------------------------------------------------------------------------
# write_audit_log
# ---------------------------------------------------------------------------

class TestWriteAuditLog:
    def test_writes_valid_json_that_round_trips(self, tmp_path):
        path = tmp_path / "audit_log.json"
        write_audit_log([decision(), decision(return_line_id="RL-2")], str(path))

        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)  # raises if not valid JSON

        assert len(loaded) == 2
        assert loaded[0]["return_line_id"] == "RL-1"
        assert loaded[1]["return_line_id"] == "RL-2"

    def test_empty_decisions_writes_empty_array(self, tmp_path):
        path = tmp_path / "empty.json"
        write_audit_log([], str(path))
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == []


# ---------------------------------------------------------------------------
# render_decision_report
# ---------------------------------------------------------------------------

class TestRenderDecisionReport:
    def test_contains_key_identifying_fields(self):
        report = render_decision_report(decision())
        assert "RL-1" in report
        assert "SKU-123" in report
        assert "SCRAP" in report  # disposition uppercased
        assert "2027-08" in report

    def test_contains_each_rules_reasoning(self):
        report = render_decision_report(
            decision(rule_outcomes=(outcome(reasoning="Distinctive reasoning marker."),))
        )
        assert "Distinctive reasoning marker." in report

    def test_conflict_flag_shown_for_detected_conflicts(self):
        report = render_decision_report(decision(rule_outcomes=(outcome(conflict_detected=True),)))
        assert "[CONFLICT]" in report

    def test_agreement_flag_shown_when_no_conflict(self):
        report = render_decision_report(decision(rule_outcomes=(outcome(conflict_detected=False),)))
        assert "[agreement]" in report

    def test_discarded_evidence_is_shown_when_present(self):
        report = render_decision_report(
            decision(rule_outcomes=(outcome(evidence_discarded=("supplier credit_quantity=9",)),))
        )
        assert "discarded:" in report
        assert "supplier credit_quantity=9" in report

    def test_no_discarded_line_when_evidence_discarded_is_empty(self):
        report = render_decision_report(decision(rule_outcomes=(outcome(evidence_discarded=()),)))
        assert "discarded:" not in report

    def test_unresolved_batch_code_reads_as_unresolved_not_none(self):
        report = render_decision_report(decision(resolved_batch_code=None))
        assert "unresolved" in report
        assert "None" not in report

    def test_reason_code_shown_on_the_header_line(self):
        report = render_decision_report(
            decision(rule_outcomes=(outcome(reason_code="CONDITION_WAREHOUSE_OVERRIDE"),))
        )
        assert "reason=CONDITION_WAREHOUSE_OVERRIDE" in report

    def test_missing_reason_code_shows_as_not_applicable(self):
        report = render_decision_report(decision(rule_outcomes=(outcome(reason_code=""),)))
        assert "reason=n/a" in report

    def test_candidate_list_shown_when_detail_present(self):
        detail = {
            "candidates": [
                {"batch_code": "BC-2026-0901-C", "confidence": 0.964},
                {"batch_code": "BC-2026-0817-A", "confidence": 0.712},
            ],
            "threshold": 0.8,
        }
        report = render_decision_report(decision(rule_outcomes=(outcome(detail=detail),)))
        assert "candidates considered" in report
        assert "BC-2026-0901-C -> 96.4%" in report
        assert "BC-2026-0817-A -> 71.2%" in report

    def test_no_candidate_block_when_detail_empty(self):
        report = render_decision_report(decision(rule_outcomes=(outcome(detail={}),)))
        assert "candidates considered" not in report


# ---------------------------------------------------------------------------
# Boxed terminal report
# ---------------------------------------------------------------------------

class TestBoxRenderer:
    def test_structural_lines_are_all_the_same_length(self):
        # this is the actual alignment guarantee, not eyeballing the output
        report = render_decision_box(decision())
        structural = [
            line for line in report.splitlines()
            if line.startswith(("\u2554", "\u255a", "\u2560")) or line.startswith("\u2551")
        ]
        lengths = {len(line) for line in structural}
        assert len(lengths) == 1, f"box lines are not aligned, found lengths: {lengths}"

    def test_contains_return_line_id_and_sku(self):
        report = render_decision_box(decision(return_line_id="RL-9", sku="SKU-999"))
        assert "RL-9" in report
        assert "SKU-999" in report

    def test_flags_conflicts_detected_vs_none(self):
        with_conflict = render_decision_box(decision(rule_outcomes=(outcome(conflict_detected=True),)))
        assert "CONFLICTS DETECTED" in with_conflict

        without_conflict = render_decision_box(decision(rule_outcomes=(outcome(conflict_detected=False),)))
        assert "NO CONFLICTS" in without_conflict

    def test_final_decision_fields_present(self):
        report = render_decision_box(
            decision(disposition=Disposition.RESTOCK, resolved_batch_code="BC-1", temporal_bucket="2027-09")
        )
        assert "RESTOCK" in report
        assert "BC-1" in report
        assert "2027-09" in report

    def test_long_value_truncates_without_breaking_alignment(self):
        # this is the degrade-gracefully guarantee: a pathologically long
        # value must not push the box out of alignment
        report = render_decision_box(decision(return_line_id="X" * 200))
        lines = report.splitlines()
        lengths = {len(line) for line in lines}
        assert len(lengths) == 1

    def test_render_shipment_box_stacks_multiple_decisions(self):
        report = render_shipment_box([decision(return_line_id="RL-1"), decision(return_line_id="RL-2")])
        assert "RL-1" in report
        assert "RL-2" in report


# ---------------------------------------------------------------------------
# render_shipment_report
# ---------------------------------------------------------------------------

class TestRenderShipmentReport:
    def test_summary_line_counts_by_disposition(self):
        report = render_shipment_report(
            [decision(disposition=Disposition.SCRAP), decision(disposition=Disposition.RESTOCK, return_line_id="RL-2")]
        )
        first_line = report.splitlines()[0]
        assert "2 return line(s)" in first_line
        assert "1 scrap" in first_line
        assert "1 restock" in first_line

    def test_contains_every_line_items_report(self):
        report = render_shipment_report(
            [decision(return_line_id="RL-1"), decision(return_line_id="RL-2")]
        )
        assert "RL-1" in report
        assert "RL-2" in report

    def test_empty_shipment(self):
        report = render_shipment_report([])
        assert "0 return line(s)" in report