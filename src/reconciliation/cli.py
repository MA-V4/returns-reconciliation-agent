"""
CLI entry point. `python -m reconciliation.cli` runs a built-in demo
scenario and prints the full audit report, no setup required. `--input`
points it at a real JSON shipment file instead; `--output` also writes
the full audit log.

Shipment file format:
{
  "warehouse_records": [ {...WarehouseInspectionRecord fields...}, ... ],
  "supplier_notes":    [ {...SupplierCreditNote fields...}, ... ],
  "batch_registry":    [ {...BatchRegistryEntry fields...}, ... ]
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from datetime import date, datetime
from typing import Sequence

from reconciliation.audit import render_shipment_box, render_shipment_report, write_audit_log
from reconciliation.html_report import write_html_report
from reconciliation.ingestion import (
    parse_supplier_note,
    parse_warehouse_record,
    process_return,
    validate_registry_integrity,
    verify_determinism,
)
from reconciliation.schemas import (
    BatchRegistryEntry,
    ConditionGrade,
    SupplierCreditNote,
    WarehouseInspectionRecord,
)


def _load_shipment_file(
    path: str,
) -> tuple[list[WarehouseInspectionRecord], list[SupplierCreditNote], list[BatchRegistryEntry]]:
    """Loads a shipment from JSON, using the tolerant parsers from
    ingestion.py, an unparseable record is skipped and warned about, it
    does not crash the whole load. batch_registry entries are treated as
    curated reference data, not an untrusted external source, and are
    constructed directly, a malformed registry entry is a configuration
    error, not one of the two named runtime failure modes.
    """
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    warehouse_records: list[WarehouseInspectionRecord] = []
    for raw in payload.get("warehouse_records", []):
        outcome = parse_warehouse_record(raw)
        if outcome.success:
            warehouse_records.append(outcome.record)
        else:
            print(
                f"WARNING: skipping unparseable warehouse record "
                f"{raw.get('record_id', '?')!r}: {outcome.errors}",
                file=sys.stderr,
            )

    supplier_notes: list[SupplierCreditNote] = []
    for raw in payload.get("supplier_notes", []):
        outcome = parse_supplier_note(raw)
        if outcome.success:
            supplier_notes.append(outcome.record)
        else:
            print(
                f"WARNING: skipping unparseable supplier note "
                f"{raw.get('note_id', '?')!r}: {outcome.errors}",
                file=sys.stderr,
            )

    registry = [BatchRegistryEntry(**entry) for entry in payload.get("batch_registry", [])]

    return warehouse_records, supplier_notes, registry


def _build_demo_shipment() -> (
    tuple[list[WarehouseInspectionRecord], list[SupplierCreditNote], list[BatchRegistryEntry]]
):
    """Four return lines, one per disposition plus the recovery case:

    RL-1: clean agreement                                    -> RESTOCK
    RL-2: genuine multi-rule disagreement, no data corruption -> SCRAP
    RL-3: both named failure modes at once, resolved anyway   -> RESTOCK
    RL-4: genuinely unresolvable                               -> QUARANTINE
    """
    registry = [
        BatchRegistryEntry(
            batch_code="BC-2026-0817-A", sku="SKU-KETTLE-01",
            manufactured_date=date(2026, 8, 17), best_before_date=date(2027, 8, 17),
        ),
        BatchRegistryEntry(
            batch_code="BC-2026-0901-C", sku="SKU-KETTLE-01",
            manufactured_date=date(2026, 9, 1), best_before_date=date(2027, 9, 1),
        ),
    ]

    warehouse_records = [
        WarehouseInspectionRecord(
            record_id="WH-1", return_line_id="RL-1", sku="SKU-KETTLE-01",
            condition_grade=ConditionGrade.SELLABLE, inspected_quantity=6,
            claimed_batch_code="BC-2026-0817-A", inspector_has_photo_evidence=True,
            inspected_at=datetime(2026, 8, 20, 10, 0),
        ),
        WarehouseInspectionRecord(
            record_id="WH-2", return_line_id="RL-2", sku="SKU-KETTLE-01",
            condition_grade=ConditionGrade.MAJOR_DAMAGE, inspected_quantity=3,
            claimed_batch_code="BC-2026-0817-A", inspector_has_photo_evidence=True,
            inspected_at=datetime(2026, 8, 21, 11, 0),
        ),
        WarehouseInspectionRecord(
            record_id="WH-3", return_line_id="RL-3", sku="SKU-KETTLE-01",
            condition_grade=ConditionGrade.SELLABLE, inspected_quantity=8,
            raw_scanner_output="BC-2O26-O9O1-C", claimed_batch_code=None,  # garbled: letter O for zero
            inspector_has_photo_evidence=False,
            inspected_at=datetime(2026, 9, 3, 14, 0),
        ),
        WarehouseInspectionRecord(
            record_id="WH-4", return_line_id="RL-4", sku="SKU-KETTLE-01",
            condition_grade=ConditionGrade.UNKNOWN, inspected_quantity=2,
            raw_scanner_output="###ILLEGIBLE###", claimed_batch_code=None,
            inspector_has_photo_evidence=False,
            inspected_at=datetime(2026, 9, 5, 16, 0),
        ),
    ]

    supplier_notes = [
        SupplierCreditNote(
            note_id="SN-1", return_line_id="RL-1", sku="SKU-KETTLE-01",
            sequence_number=1, generated_at=datetime(2026, 8, 20, 9, 0), received_at=datetime(2026, 8, 20, 9, 5),
            claimed_batch_code="BC-2026-0817-A", eligible_for_credit=True, credit_quantity=6, restock_required=True,
        ),
        # RL-2: supplier disagrees on condition, eligibility, and quantity, all at once
        SupplierCreditNote(
            note_id="SN-2", return_line_id="RL-2", sku="SKU-KETTLE-01",
            sequence_number=1, generated_at=datetime(2026, 8, 21, 10, 0), received_at=datetime(2026, 8, 21, 10, 5),
            claimed_batch_code="BC-2026-0817-A", eligible_for_credit=False, credit_quantity=9, restock_required=True,
        ),
        # RL-3: the correction (seq 2) arrives BEFORE the original (seq 1)
        SupplierCreditNote(
            note_id="SN-3A", return_line_id="RL-3", sku="SKU-KETTLE-01",
            sequence_number=1, generated_at=datetime(2026, 9, 3, 8, 0), received_at=datetime(2026, 9, 3, 9, 30),
            claimed_batch_code="WRONG-CODE", eligible_for_credit=True, credit_quantity=8, restock_required=True,
        ),
        SupplierCreditNote(
            note_id="SN-3B", return_line_id="RL-3", sku="SKU-KETTLE-01",
            sequence_number=2, generated_at=datetime(2026, 9, 4, 8, 0), received_at=datetime(2026, 9, 3, 9, 0),
            claimed_batch_code="BC-2026-0901-C", eligible_for_credit=True, credit_quantity=8, restock_required=True,
        ),
        SupplierCreditNote(
            note_id="SN-4", return_line_id="RL-4", sku="SKU-KETTLE-01",
            sequence_number=1, generated_at=datetime(2026, 9, 5, 15, 0), received_at=datetime(2026, 9, 5, 15, 30),
            claimed_batch_code="BC-2026-0901-C", eligible_for_credit=False, credit_quantity=2, restock_required=False,
        ),
    ]

    return warehouse_records, supplier_notes, registry


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconciliation",
        description="Reconcile stock returns when warehouse and supplier assessments conflict.",
    )
    parser.add_argument(
        "--input", metavar="PATH",
        help="JSON shipment file (warehouse_records, supplier_notes, batch_registry). "
             "Omit to run the built-in demo scenario.",
    )
    parser.add_argument(
        "--output", metavar="PATH",
        help="Write the full audit log as JSON to this path.",
    )
    parser.add_argument(
        "--box", action="store_true",
        help="Render as a boxed terminal report instead of plain text, useful for recording.",
    )
    parser.add_argument(
        "--html", metavar="PATH",
        help="Write a static HTML case file to this path. Runs a real determinism check "
             "(reruns and compares) as part of generating it.",
    )
    args = parser.parse_args(argv)

    if args.input:
        warehouse_records, supplier_notes, registry = _load_shipment_file(args.input)
    else:
        print("No --input given, running the built-in demo scenario.\n")
        warehouse_records, supplier_notes, registry = _build_demo_shipment()

    for warning in validate_registry_integrity(registry):
        print(f"WARNING: registry integrity: {warning}", file=sys.stderr)

    decisions = process_return(warehouse_records, supplier_notes, registry)

    if args.box:
        print(render_shipment_box(decisions))
    else:
        print(render_shipment_report(decisions))

    if args.output:
        write_audit_log(decisions, args.output)
        print(f"\nFull audit log written to {args.output}")

    if args.html:
        determinism_verified = verify_determinism(warehouse_records, supplier_notes, registry)
        write_html_report(decisions, determinism_verified, args.html)
        print(f"\nHTML case file written to {args.html} (determinism verified: {determinism_verified})")
        try:
            webbrowser.open(f"file://{os.path.abspath(args.html)}")
        except Exception:  # noqa: BLE001 - opening a browser is a convenience, never a reason to fail the run
            print(f"Could not open a browser automatically, open {args.html} manually.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())