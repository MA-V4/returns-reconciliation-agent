"""
Tests for cli.py. Different shape from the rest of the suite, this
checks stdout/stderr content and file output rather than pure function
return values, that's what actually matters for a CLI: what does the
person running it (or watching the demo video) actually see.
"""

import json

from reconciliation.cli import _build_demo_shipment, _load_shipment_file, main
from reconciliation.ingestion import process_return
from reconciliation.schemas import Disposition


 
# The demo scenario itself, locked in so it can't silently drift
 

class TestBuildDemoShipment:
    def test_produces_all_three_dispositions(self):
        warehouse_records, supplier_notes, registry = _build_demo_shipment()
        decisions = process_return(warehouse_records, supplier_notes, registry)
        dispositions = {d.disposition for d in decisions}
        assert dispositions == {Disposition.RESTOCK, Disposition.SCRAP, Disposition.QUARANTINE}

    def test_exact_disposition_counts(self):
        warehouse_records, supplier_notes, registry = _build_demo_shipment()
        decisions = process_return(warehouse_records, supplier_notes, registry)
        counts: dict[Disposition, int] = {}
        for decision in decisions:
            counts[decision.disposition] = counts.get(decision.disposition, 0) + 1
        assert counts[Disposition.RESTOCK] == 2
        assert counts[Disposition.SCRAP] == 1
        assert counts[Disposition.QUARANTINE] == 1

    def test_rl3_resolves_via_registry_not_via_wrong_arrival_order_note(self):
        # locks in the specific claim made about RL-3: the note that
        # arrived first (SN-3B, the real correction) wins over the one
        # that arrived last (SN-3A, the stale "WRONG-CODE" claim)
        warehouse_records, supplier_notes, registry = _build_demo_shipment()
        decisions = process_return(warehouse_records, supplier_notes, registry)
        rl3 = next(d for d in decisions if d.return_line_id == "RL-3")
        assert rl3.resolved_batch_code == "BC-2026-0901-C"
        assert rl3.disposition == Disposition.RESTOCK

    def test_rl4_quarantines_but_still_resolves_the_batch_it_can(self):
        # quarantine doesn't blank every field, only the ones lacking evidence
        warehouse_records, supplier_notes, registry = _build_demo_shipment()
        decisions = process_return(warehouse_records, supplier_notes, registry)
        rl4 = next(d for d in decisions if d.return_line_id == "RL-4")
        assert rl4.disposition == Disposition.QUARANTINE
        assert rl4.resolved_batch_code == "BC-2026-0901-C"  # still resolved
        assert rl4.eligible_for_credit is None  # genuinely undetermined


 
# main(), demo mode
 

class TestMainDemoMode:
    def test_exit_code_zero(self):
        assert main([]) == 0

    def test_prints_disposition_summary(self, capsys):
        main([])
        out = capsys.readouterr().out
        assert "Processed 4 return line(s)" in out
        assert "2 restock" in out
        assert "1 scrap" in out
        assert "1 quarantine" in out

    def test_prints_all_four_return_lines(self, capsys):
        main([])
        out = capsys.readouterr().out
        for line_id in ("RL-1", "RL-2", "RL-3", "RL-4"):
            assert line_id in out

    def test_notes_it_is_running_the_demo_scenario(self, capsys):
        main([])
        assert "demo scenario" in capsys.readouterr().out.lower()


 
# main(), --output
 

class TestMainWithOutputFile:
    def test_writes_audit_log_and_confirms_it(self, tmp_path, capsys):
        output_path = tmp_path / "audit.json"
        exit_code = main(["--output", str(output_path)])
        assert exit_code == 0

        with open(output_path, encoding="utf-8") as f:
            payload = json.load(f)
        assert len(payload) == 4

        assert str(output_path) in capsys.readouterr().out


 
# _load_shipment_file: tolerant parsing of real files
 

def _write_shipment(tmp_path, payload):
    path = tmp_path / "shipment.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestLoadShipmentFile:
    def test_loads_a_valid_shipment(self, tmp_path):
        payload = {
            "warehouse_records": [
                {
                    "record_id": "WH-1", "return_line_id": "RL-1", "sku": "SKU-1",
                    "condition_grade": "sellable", "inspected_quantity": 5,
                    "inspected_at": "2026-01-01T09:00:00",
                }
            ],
            "supplier_notes": [
                {
                    "note_id": "SN-1", "return_line_id": "RL-1", "sku": "SKU-1",
                    "sequence_number": 1, "generated_at": "2026-01-01T08:00:00",
                    "received_at": "2026-01-01T08:05:00", "eligible_for_credit": True,
                    "credit_quantity": 5, "restock_required": True,
                }
            ],
            "batch_registry": [],
        }
        path = _write_shipment(tmp_path, payload)
        warehouse_records, supplier_notes, registry = _load_shipment_file(path)
        assert len(warehouse_records) == 1
        assert len(supplier_notes) == 1
        assert registry == []

    def test_skips_malformed_warehouse_record_and_warns(self, tmp_path, capsys):
        payload = {
            "warehouse_records": [
                {"record_id": "WH-BAD", "sku": "SKU-1"},  # missing required fields
                {
                    "record_id": "WH-GOOD", "return_line_id": "RL-1", "sku": "SKU-1",
                    "condition_grade": "sellable", "inspected_quantity": 5,
                    "inspected_at": "2026-01-01T09:00:00",
                },
            ],
            "supplier_notes": [],
            "batch_registry": [],
        }
        path = _write_shipment(tmp_path, payload)
        warehouse_records, _, _ = _load_shipment_file(path)

        assert len(warehouse_records) == 1  # only the parseable one made it through
        assert warehouse_records[0].record_id == "WH-GOOD"

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "WH-BAD" in err

    def test_skips_malformed_supplier_note_and_warns(self, tmp_path, capsys):
        payload = {
            "warehouse_records": [],
            "supplier_notes": [{"note_id": "SN-BAD"}],
            "batch_registry": [],
        }
        path = _write_shipment(tmp_path, payload)
        _, supplier_notes, _ = _load_shipment_file(path)
        assert supplier_notes == []

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "SN-BAD" in err


 
# main(), --input
 

class TestMainWithInputFile:
    def test_processes_a_real_file_end_to_end(self, tmp_path, capsys):
        payload = {
            "warehouse_records": [
                {
                    "record_id": "WH-1", "return_line_id": "RL-1", "sku": "SKU-1",
                    "condition_grade": "sellable", "inspected_quantity": 5,
                    "claimed_batch_code": "BC-1", "inspected_at": "2026-01-01T09:00:00",
                }
            ],
            "supplier_notes": [
                {
                    "note_id": "SN-1", "return_line_id": "RL-1", "sku": "SKU-1",
                    "sequence_number": 1, "generated_at": "2026-01-01T08:00:00",
                    "received_at": "2026-01-01T08:05:00", "eligible_for_credit": True,
                    "credit_quantity": 5, "restock_required": True, "claimed_batch_code": "BC-1",
                }
            ],
            "batch_registry": [
                {
                    "batch_code": "BC-1", "sku": "SKU-1",
                    "manufactured_date": "2026-01-01", "best_before_date": "2027-01-01",
                }
            ],
        }
        path = _write_shipment(tmp_path, payload)

        exit_code = main(["--input", path])
        assert exit_code == 0

        out = capsys.readouterr().out
        assert "RL-1" in out
        assert "RESTOCK" in out
        assert "demo scenario" not in out.lower()  # a real file was used, not the built-in one