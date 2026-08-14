# Test Strategy

Written before implementation, not after. Every rule in `DECISION_RULES.md`
gets test cases named directly after it, so there's a traceable line from
brief requirement, to documented rule, to test, to eventual code.

## Golden scenarios, one block per rule

**Rule 1 (condition/salvageability)**
- clean agreement: warehouse and supplier consistent, no conflict raised
- conflict: warehouse `MAJOR_DAMAGE` vs supplier `restock_required=True`,
  warehouse wins, disposition reflects physical damage
- warehouse `condition_grade=UNKNOWN`: no authority to win, falls to
  quarantine regardless of supplier's claim
- `MAJOR_DAMAGE` without photo evidence: still trusted, audit record shows
  reduced confidence flag, disposition unchanged

**Rule 2 (batch code mismatch)**
- exact agreement: trivially resolved, no registry lookup performed
- garbled warehouse code, repair succeeds and corroborates supplier's
  claim: resolved, high confidence
- only supplier's code validates against the registry: supplier wins,
  this is the test that actually proves "no fixed source preference"
- only warehouse's (repaired) code validates: warehouse wins
- neither validates, or both validate ambiguously: unresolved, disposition
  quarantine, batch code not guessed

**Rule 3 (best-before)**
- batch resolved: best-before taken from the registry entry, test
  explicitly asserts the result matches neither party's stated date when
  both are wrong, only the registry value
- batch unresolved: temporal bucket is exactly `unknown, pending review`,
  never a computed guess

**Rule 4 (quantity)**
- supplier claim exceeds `inspected_quantity`: capped, cap event present
  in the audit record
- supplier claim within `inspected_quantity`: passes through unmodified

**Rule 5 (eligibility)**
- `MAJOR_DAMAGE` + photo evidence + supplier ineligible: overridden to
  eligible, audit cites the specific evidence used
- `SELLABLE` + supplier ineligible: supplier's claim stands, audit states
  "no physical contradiction found" explicitly, not silently accepted
- `condition_grade=UNKNOWN` + disputed eligibility: quarantine, the one
  genuine toss-up, reached via its named trigger

## Combined failure mode suite

This is the suite the brief is actually testing for, most of the effort
goes here, not into the individual golden cases above.

- out-of-order supplier notes alone, correct outcome regardless of arrival
  shuffle
- garbled batch code alone, repair succeeds and fails paths, both
  exercised
- both on the same `return_line_id`: must still resolve to exactly one of
  `SCRAP` / `RESTOCK` / `QUARANTINE`, audit trail names which supplier
  note (by `sequence_number`) was authoritative and what batch repair
  candidates were considered and at what confidence, never a crash, never
  a silent single-source default

## Property-based invariants (hypothesis)

These are checked against generated input, not just hand-picked examples,
because the failure modes in this brief are exactly the kind that hide
between hand-picked cases:

- the engine never raises on malformed or missing input, for any generated
  combination of fields
- output disposition is always exactly one of the three valid values,
  never `None`, never anything else
- reprocessing identical input twice produces an identical decision
  (idempotency)
- shuffling `received_at` across a set of supplier notes for the same
  `return_line_id`, while holding `sequence_number` fixed, never changes
  the resolved outcome, this is the out-of-order failure mode expressed as
  an invariant rather than a handful of examples
- resolved `credit_quantity` never exceeds `inspected_quantity`, for any
  generated pair of values, this is Rule 4 as an invariant rather than an
  example

## Edge cases beyond the brief's minimum

- warehouse record entirely missing for a `return_line_id` (supplier-only
  evidence)
- duplicate or directly conflicting credit notes at the same
  `sequence_number`
- batch repair producing two equally plausible candidates at identical
  confidence, forced to quarantine rather than an arbitrary tie-break
- large batch of returns processed together, checked for the same
  per-item correctness and for no cross-item state leakage