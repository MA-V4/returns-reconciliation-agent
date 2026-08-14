# Returns Reconciliation Agent

An agent that reconciles stock returns when the warehouse's physical
inspection and the supplier's credit note disagree. It decides where
each returned item physically goes (scrap, restock, or quarantine) and
what best-before month it belongs in, and it produces a full, cited
audit trail showing exactly which source won each disagreement and why.

The brief is explicit that the obvious approaches are both wrong:
always trusting the warehouse ignores that scanners misread codes under
poor lighting, always trusting the supplier ignores that they have a
financial incentive to minimise credit. Nothing in this system has a
fixed source preference. Every disagreement is arbitrated against an
independent fact (a batch registry, a physical count) or resolved by
cited evidence, never by which party said it.

## Quick start

```powershell
pip install -e ".[dev]"
pytest                              # 155 tests
python -m reconciliation.cli        # runs a built-in demo shipment
python -m reconciliation.cli --box  # same demo, boxed report for recording
python -m reconciliation.cli --output audit_log.json   # writes the full audit log
python -m reconciliation.cli --html report.html        # writes and opens a static HTML case file
```

No input file is required to see it work end to end, the built-in demo
scenario in `cli.py` covers all three dispositions and both named
failure modes in a single run.

## What it actually does

Given a warehouse inspection record and one or more supplier credit
notes for the same returned item, the agent:

1. Identifies every disagreement between the two sources across five
   dimensions: condition, batch code, best-before date, quantity, and
   credit eligibility.
2. Resolves each disagreement with a documented, deterministic rule,
   stating which source won and why.
3. Handles two failure modes that can occur independently or at once:
   a warehouse scanner returning a garbled batch code, and supplier
   credit notes arriving out of order relative to when they were
   actually issued.
4. Produces a decision that is always one of exactly three values,
   scrap, restock, or quarantine, plus a best-before bucket, never a
   crash and never a silent default to one source.
5. Logs the full reasoning for every decision, in JSON for machines and
   in plain text or a boxed report for humans.

## Why the decision engine is deterministic, not an LLM

This was the first architecture decision made and it still holds. A
decision that affects real inventory and real credit needs to be
reproducible on rerun, testable rule by rule, and explainable by citing
the specific rule that fired, not by describing what a model tends to
do. Every one of the 155 tests in this repo tests deterministic code.
An LLM in the decision path would make none of that possible. Full
reasoning in `ADR.md`, decision ADR-001.

## How it works, component by component

The pipeline is a straight line: parse evidence, resolve conflicts,
decide, record. Each stage is a separate file with one job.

### `schemas.py`, the domain model

Defines `WarehouseInspectionRecord`, `SupplierCreditNote`, and
`BatchRegistryEntry`. The one design choice worth knowing here: the two
source records are correlated by `return_line_id`, a stable identifier
assigned when the return is authorised, never by batch code. Batch code
is itself one of the disputed facts, using it as the join key would
make a batch mismatch invisible instead of detectable. `UNKNOWN` is a
first-class value for condition and damage type, not `None`, so the
system can tell "assessed and inconclusive" apart from "never assessed
at all."

### `rules.py`, the five decision rules

Five pure functions, each implementing one rule from `DECISION_RULES.md`:

| Rule | What it resolves | Who wins, and why |
|---|---|---|
| `resolve_condition` | Physical salvageability | Warehouse's direct observation outranks the supplier's financially motivated restock claim, unless condition is `UNKNOWN`, then neither side has authority |
| `resolve_batch_code` | Which batch the item belongs to | Neither party directly. Both claims are checked against an independent batch registry, whichever validates wins. If a code is garbled, `repair_batch_code` attempts a fuzzy match (edit distance, not embeddings, this is character-level scanner noise, not semantic ambiguity) |
| `resolve_best_before` | Temporal bucket | Purely a registry lookup on the batch resolved above, never either party's stated date |
| `resolve_quantity` | Physical count vs. creditable count | Warehouse's physical count is a hard ceiling. A supplier claiming fewer units than were inspected is flagged too, not silently accepted, the gap is real credit-eligible stock going uncredited |
| `resolve_eligibility` | Credit eligibility | Supplier's ineligibility claim is overridden by documented physical damage; the same claim stands unchallenged when there's no physical contradiction |

Every rule returns a `RuleOutcome`: which source won, a confidence
score, a plain-language reasoning string, a machine-readable
`reason_code`, and what evidence was discarded. Rule 2 also returns
`detail`, the full ranked list of batch-code candidates it considered,
not just the one it picked.

### `engine.py`, the aggregator

`reconcile_line_item` runs all five rules for one item and produces a
single `LineItemDecision`. This function is guaranteed total: for any
valid input it returns exactly one of `SCRAP`, `RESTOCK`, or
`QUARANTINE`, wrapped in a `try/except` that routes any unexpected
internal error to quarantine rather than letting it propagate. Physical
disposition is driven only by the condition and batch rules, quantity
and eligibility affect the financial outcome, not where the item
physically goes, a damaged item still gets scrapped even if the credit
dispute over it is unresolved.

### `ingestion.py`, the failure-mode layer

This is where the out-of-order-timestamp failure mode is actually
solved.  `resolve_authoritative_supplier_note` replays multiple supplier
notes for one item by their own `sequence_number`, never by when they
arrived. `received_at` is recorded for the audit trail but never used
to decide anything. It also catches integrity problems the brief didn't
explicitly ask for but that a real system would hit: duplicate sequence
numbers with genuinely conflicting content, a sequence counter that
goes backwards against its own timestamps, and dangling correction
references.

`process_return` is the pipeline entry point: it groups evidence by
`return_line_id`, resolves each item independently, and wraps each one
in its own error handling so one bad line item never sinks the rest of
the shipment. `verify_determinism` reruns `process_return` twice against
identical input and compares the results, a real check, not a demo, it's
the actual property ADR-001 claims.

### `audit.py`, the audit trail

Turns a decision into something visible. `write_audit_log` produces the
full JSON record, every rule outcome, every candidate considered, every
confidence score. `render_decision_report` produces the plain-text
console version. `render_decision_box` produces a boxed terminal report
for recording, same data, formatted for visual clarity rather than
grepping. Nothing in this file makes a decision, it only renders one
that was already made.

### `html_report.py`, the static case file

`render_html_report` / `write_html_report` build a self-contained HTML
page from the same `LineItemDecision` data the other renderers use, no
server, no build step, no external font or script dependency, opens
correctly with zero internet connection. Same read-only principle as
`audit.py`: every value comes from an already-computed decision,
nothing is decided here. The determinism check shown on the page is
real, computed live when the report is generated, not a staged claim.
Design is deliberately not a generic AI-product dashboard, near-black,
one accent color per disposition on the card border and the verdict
text, everything else monochrome.

### `cli.py`, the runnable entry point

`python -m reconciliation.cli` runs a built-in four-item demo shipment
covering all three dispositions and both failure modes, so the whole
system can be demonstrated with no setup. `--input` points it at a real
JSON shipment file instead, `--output` writes the full audit log,
`--box` switches the console report to the boxed format, `--html` writes
the static case file and opens it in the default browser.

## The two failure modes, together

The brief's hardest requirement is that both named failure modes, a
garbled warehouse scan and out-of-order supplier notes, be handled when
they happen on the same item at once, without crashing or defaulting to
one source. The built-in demo's third line item (`RL-3`) is built
specifically to exercise this:

* The warehouse's scanner returns `BC-2O26-O9O1-C` (corrupted).
* Supplier note `SN-3A` (sequence 1, the stale, wrong claim) arrives
  chronologically *after* `SN-3B` (sequence 2, the real correction).

The system correctly uses `SN-3B`'s claim, `sequence_number` beats
arrival order, and independently validates the batch through the
registry rather than trusting either scan directly. Two different
mechanisms, working independently, on the same item, in the same run.
The audit trail names both explicitly: which note was chosen and why
arrival order would have chosen wrong, and which batch-code candidates
were considered and at what confidence.

## Testing

155 tests across seven files, one per module, plus property-based tests
using Hypothesis for the invariants that matter most:

* The engine never raises, for any generated combination of valid
  input, checked across 200 generated examples per test.
* The disposition is always exactly one of the three valid values.
* Reprocessing identical input twice produces an identical decision.
* Creditable quantity never exceeds physical quantity.
* Every line item observed in the input gets exactly one decision.
* Every `reason_code` any real decision emits is a member of a declared
  closed set, an undeclared or typo'd code fails the test, not a
  runtime string comparison somewhere downstream.

Beyond the golden-path cases, the suite specifically covers: a genuine
tie between equally-plausible batch code candidates (left unresolved,
not guessed), duplicate supplier note sequence numbers with conflicting
content, an item with no warehouse evidence at all, an item with no
supplier evidence at all, a forced internal error in the middle of
processing to confirm the quarantine fallback actually fires rather
than propagating a crash, a SKU mismatch between warehouse and supplier
records (a data integrity failure, not a value to arbitrate), multiple
conflicting warehouse records for one return line, a self-contradictory
registry entry rejected at construction, and a 500-item batch checked
line by line to confirm no item's resolved batch code ever leaks into
another's, a registry containing the same batch code twice with
conflicting SKU or dates (flagged, not silently resolved against
whichever entry happens to come first), and that a decision's
`overall_confidence`, the minimum of the two confidences that actually
drive physical disposition, correctly drops to zero when condition is
genuinely unknown rather than only ever reporting a false 1.0.

## Further reading

* `DECISION_RULES.md`, the full rule matrix, trigger, winner, reasoning,
  and what would flip each decision, for all five rules plus the
  combined failure mode.
* `ADR.md`, seven architecture decisions with what was rejected and why,
  including the case against using an LLM on the decision path and the
  case against embedding-based batch code matching.
* `TEST_STRATEGY.md`, the test plan written before this code existed.

## Project structure

```
returns-reconciliation-agent/
├── README.md
├── DECISION_RULES.md
├── ADR.md
├── TEST_STRATEGY.md
├── pyproject.toml
├── .gitignore
├── src/reconciliation/
│   ├── __init__.py
│   ├── schemas.py       domain model
│   ├── rules.py         the five decision rules
│   ├── engine.py        aggregates rules into one total decision
│   ├── ingestion.py     multi-note sequencing, tolerant parsing, pipeline
│   ├── audit.py         JSON log, plain-text and boxed console reports
│   ├── html_report.py   static HTML case file, read-only observability layer
│   └── cli.py           runnable entry point, built-in demo scenario
└── tests/
    ├── test_schemas.py
    ├── test_rules.py
    ├── test_engine.py
    ├── test_ingestion.py
    ├── test_audit.py
    ├── test_cli.py
    └── test_integrity.py
```