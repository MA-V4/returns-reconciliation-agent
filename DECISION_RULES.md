# Decision Rule Matrix

Every rule below follows the same shape: trigger, winning source (or "neither"),
why, and what evidence would flip the decision. Nothing in the reconciliation
logic is allowed to fall back to "always trust warehouse" or "always trust
supplier", every resolution below is either arbitrated against an independent
third fact (the batch registry, the physical quantity ceiling) or explicitly
routed to quarantine when no independent fact is available to arbitrate.

Quarantine is treated as a legitimate designed outcome, not a fallback for
code paths that couldn't be bothered to decide. It is only reached through
one of the explicit trigger conditions below.

---

## Rule 1: Condition / salvageability disagreement

**Trigger:** warehouse `condition_grade` implies a disposition (e.g.
`MAJOR_DAMAGE` or `DESTROYED` implies not restockable) that conflicts with
supplier `restock_required`.

**Winner:** warehouse, for the physical disposition question specifically.

**Why:** the warehouse has direct physical observation of the item. The
supplier's `restock_required` flag is an inference, not an observation, and
the brief names the exact incentive at play: restocking a damaged unit
instead of crediting it is a way to minimise credit exposure. Direct
observation outranks a financially motivated inference.

**What flips it:** if `condition_grade` is `UNKNOWN`, warehouse has no
authority to win this rule, since it isn't asserting a physical fact.
Falls through to quarantine regardless of the supplier's claim. Low
severity claims (`MAJOR_DAMAGE` / `DESTROYED`) without
`inspector_has_photo_evidence` are still trusted (best available first
hand evidence beats no evidence) but are logged in the audit trail at
reduced confidence, this is a transparency requirement, not an override
trigger.

---

## Rule 2: Batch code mismatch

**Trigger:** warehouse and supplier `claimed_batch_code` disagree, or
warehouse's is missing/unparseable from `raw_scanner_output`.

**Winner:** neither source directly. The `BatchRegistryEntry` (SKU +
manufactured date + best-before date, independent of both parties) is the
arbiter. Resolution order:

1. Exact agreement between both claims: trivially resolved, no registry
   lookup needed.
2. Disagreement, or warehouse code missing: attempt fuzzy repair of
   `raw_scanner_output` against registry entries filtered by SKU and a
   plausible date window (edit distance, not vector similarity, this is
   short alphanumeric corruption, not semantic ambiguity).
3. If the repaired warehouse code validates against the registry and
   matches the supplier's claim: resolved, high confidence.
4. If only one claim validates against the registry (the other doesn't
   exist in the registry for that SKU/date window at all): the claim that
   independently validates wins, regardless of which source it came from.
   This is the rule that actually earns the "we don't always trust one
   source" claim, the registry decides, not a fixed preference.
5. If neither claim validates, both validate ambiguously (tie), or repair
   confidence is below threshold: **unresolved**. Batch is not guessed.

**What flips it:** any registry match. This rule cannot be won by
assertion, only by independent verification.

---

## Rule 3: Best-before / temporal disagreement

**Trigger:** warehouse `best_before_date` and supplier
`claimed_best_before_date` disagree.

**Winner:** neither source directly, again. Once Rule 2 resolves the batch
(or fails to), the best-before date is taken from the matched
`BatchRegistryEntry`, not from either party's stated date. Neither party's
memory or scan of a date outranks the manufacturing record for the batch
that was actually identified.

**What flips it:** nothing flips this once the batch is resolved, it's a
lookup, not a dispute, at that point. If Rule 2 is unresolved, the temporal
bucket is explicitly `unknown, pending review`, never a guessed month.

**Implementation note:** this document always described the trigger as
"warehouse and supplier disagree," but for a period the actual rule only
received the resolved batch and the registry, not either party's stated
date, so a real disagreement between the two could be silently reported
as `conflict_detected=False` purely because the registry lookup
succeeded. Caught in review and fixed: `resolve_best_before` now takes
both parties' claims directly. The winner never changed, the registry
always resolved this, only the conflict reporting around it was wrong.
When the two dates disagree, both are named in `evidence_discarded` and
`conflict_detected` is `True`, even though the resolved value is still
the registry's date either way.

---

## Rule 4: Quantity dispute

**Trigger:** supplier `credit_quantity` differs from warehouse
`inspected_quantity`.

**Winner:** warehouse sets a hard ceiling. `credit_quantity` can never
exceed `inspected_quantity`, crediting more units than were physically
received is not a disagreement to arbitrate, it's a physical impossibility.
Supplier claims above the ceiling are capped, not trusted, and the cap
event is logged.

**What flips it:** nothing flips the ceiling itself. Claims at or below the
ceiling pass through to Rule 5.

---

## Rule 5: Eligibility dispute

**Trigger:** supplier `eligible_for_credit = False` for a unit that
warehouse evidence speaks to.

**Winner:** depends on what the warehouse's condition claim actually
asserts, three exhaustive cases when supplier marks a unit ineligible:

- `condition_grade` is `MAJOR_DAMAGE` or `DESTROYED`: **overridden**.
  Physical evidence of damage independently contradicts a "no credit"
  claim from a party with a known incentive to make that claim.
  `eligible_for_credit` is corrected to `True`, logged as an explicit
  override with the evidence cited. Confidence is full with
  `inspector_has_photo_evidence = True`, reduced without it, same
  treatment Rule 1 gives an unphotographed severe-damage claim: still the
  best available first-hand evidence, still wins, just logged at lower
  confidence. Photo evidence is a confidence modifier here, not a
  separate override gate, keeping this consistent with Rule 1 rather than
  contradicting it.
- `condition_grade` is `SELLABLE` or `MINOR_DAMAGE`: no physical
  contradiction exists, supplier's call stands, logged as "no physical
  contradiction found", not silently accepted, stated.
- `condition_grade` is `UNKNOWN`: the one genuine toss-up. There's no
  physical determination in either direction to reason from, not "weak
  evidence for restocking" and not "weak evidence for damage", genuinely
  nothing. **Quarantine**, reached through this explicit trigger.

**What flips it:** `MAJOR_DAMAGE` or `DESTROYED` flips an ineligibility
claim regardless of photo evidence, only the confidence changes. Nothing
flips a sellable/minor-damage claim toward forced eligibility, that would
be the agent inventing evidence that doesn't exist.

---

## Combined failure mode: garbled batch code + out-of-order supplier notes

When both hit the same `return_line_id`:

1. Supplier notes are replayed by `sequence_number`, `received_at` is
   recorded in the audit trail but never used for ordering. Out-of-order
   arrival is neutralised structurally before Rules 1 to 5 ever run.
2. Rule 2's repair attempt runs against whatever `raw_scanner_output`
   exists. If repair clears the confidence threshold, resolution proceeds
   normally, the corruption is recovered, not worked around.
3. If repair does not clear threshold: disposition is `QUARANTINE`,
   temporal bucket is `unknown, pending review`, and the audit record
   states explicitly: which supplier note (by `sequence_number`, not
   arrival order) was treated as authoritative, what raw scanner output
   was evaluated, what repair candidates were considered and at what
   confidence, and that quarantine was chosen because no batch claim
   cleared the threshold, not because of the timestamp disorder. The two
   failure modes are handled independently and the audit trail shows both
   were handled, not that one masked the other.