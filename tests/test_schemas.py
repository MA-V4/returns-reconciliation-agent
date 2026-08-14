"""
Unit tests for the domain schemas (Act I / Phase 1 milestone).

These only exercise validation and default behaviour of the pydantic
models themselves, nothing about reconciliation logic yet. The rule
engine gets its own test file once it exists, mapped to DECISION_RULES.md.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    DamageType,
    Disposition,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)


# WarehouseInspectionRecord

def _base_warehouse_kwargs(**overrides):
    kwargs = dict(
        record_id="WH-001",
        return_line_id="RL-001",
        sku="SKU-123",
        condition_grade=ConditionGrade.SELLABLE,
        inspected_quantity=10,
        inspected_at=datetime(2026, 1, 1, 9, 0, 0),
    )
    kwargs.update(overrides)
    return kwargs


def test_warehouse_record_minimal_valid_construction():
    record = WarehouseInspectionRecord(**_base_warehouse_kwargs())
    assert record.condition_grade == ConditionGrade.SELLABLE
    # ADR-007: damage_type defaults to UNKNOWN, not NONE, when unspecified
    assert record.damage_type == DamageType.UNKNOWN
    assert record.claimed_batch_code is None
    assert record.raw_scanner_output is None
    assert record.inspector_has_photo_evidence is False


def test_warehouse_record_condition_grade_is_required():
    # ADR-007: condition_grade must be required, UNKNOWN is a value within
    # it, not a substitute for the field being optional
    kwargs = _base_warehouse_kwargs()
    del kwargs["condition_grade"]
    with pytest.raises(ValidationError):
        WarehouseInspectionRecord(**kwargs)


def test_warehouse_record_condition_grade_unknown_is_a_valid_explicit_value():
    record = WarehouseInspectionRecord(
        **_base_warehouse_kwargs(condition_grade=ConditionGrade.UNKNOWN)
    )
    assert record.condition_grade == ConditionGrade.UNKNOWN


def test_warehouse_record_rejects_negative_quantity():
    with pytest.raises(ValidationError):
        WarehouseInspectionRecord(**_base_warehouse_kwargs(inspected_quantity=-1))


def test_warehouse_record_allows_zero_quantity():
    record = WarehouseInspectionRecord(**_base_warehouse_kwargs(inspected_quantity=0))
    assert record.inspected_quantity == 0


def test_warehouse_record_preserves_garbled_raw_scanner_output():
    # ADR-004 / Rule 2 depend on the garbled string surviving verbatim
    garbled = "B(H*7#N0T-A-R3AL-C0DE"
    record = WarehouseInspectionRecord(
        **_base_warehouse_kwargs(raw_scanner_output=garbled, claimed_batch_code=None)
    )
    assert record.raw_scanner_output == garbled
    assert record.claimed_batch_code is None


def test_warehouse_record_batch_scan_confidence_bounds():
    with pytest.raises(ValidationError):
        WarehouseInspectionRecord(**_base_warehouse_kwargs(batch_scan_confidence=1.5))
    with pytest.raises(ValidationError):
        WarehouseInspectionRecord(**_base_warehouse_kwargs(batch_scan_confidence=-0.1))
    record = WarehouseInspectionRecord(**_base_warehouse_kwargs(batch_scan_confidence=0.9))
    assert record.batch_scan_confidence == 0.9


# SupplierCreditNote

def _base_supplier_kwargs(**overrides):
    kwargs = dict(
        note_id="SN-001",
        return_line_id="RL-001",
        sku="SKU-123",
        sequence_number=1,
        generated_at=datetime(2026, 1, 1, 8, 0, 0),
        received_at=datetime(2026, 1, 1, 8, 5, 0),
        eligible_for_credit=True,
        credit_quantity=10,
        restock_required=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_supplier_note_minimal_valid_construction():
    note = SupplierCreditNote(**_base_supplier_kwargs())
    assert note.sequence_number == 1
    assert note.supersedes_note_id is None


def test_supplier_note_rejects_negative_credit_quantity():
    with pytest.raises(ValidationError):
        SupplierCreditNote(**_base_supplier_kwargs(credit_quantity=-5))


def test_supplier_note_generated_at_and_received_at_are_independent():
    # ADR-002: received_at must exist for the audit trail, but the schema
    # itself must not conflate it with sequence_number or generated_at,
    # ordering discipline is enforced by the ingestion layer, not here
    note = SupplierCreditNote(
        **_base_supplier_kwargs(
            sequence_number=3,
            generated_at=datetime(2026, 1, 3, 8, 0, 0),
            received_at=datetime(2026, 1, 1, 8, 0, 0),  # arrived "before" an earlier note would
        )
    )
    assert note.sequence_number == 3
    assert note.received_at < note.generated_at  # schema permits this on purpose


def test_supplier_note_credit_amount_accepts_decimal():
    note = SupplierCreditNote(**_base_supplier_kwargs(credit_amount=Decimal("123.45")))
    assert note.credit_amount == Decimal("123.45")


def test_supplier_note_supersedes_chain_is_trackable():
    note = SupplierCreditNote(
        **_base_supplier_kwargs(sequence_number=2, supersedes_note_id="SN-001")
    )
    assert note.supersedes_note_id == "SN-001"



# BatchRegistryEntry

def test_batch_registry_entry_valid_construction():
    entry = BatchRegistryEntry(
        batch_code="BC-2026-0817-A",
        sku="SKU-123",
        manufactured_date=date(2026, 8, 17),
        best_before_date=date(2027, 8, 17),
    )
    assert entry.batch_code == "BC-2026-0817-A"


def test_batch_registry_entry_requires_all_fields():
    with pytest.raises(ValidationError):
        BatchRegistryEntry(batch_code="BC-2026-0817-A", sku="SKU-123")



# Disposition

def test_disposition_has_exactly_three_values():
    # ADR-006: the routing function must be total over exactly these three
    assert {d.value for d in Disposition} == {"scrap", "restock", "quarantine"}