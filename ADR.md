# Architecture Decision Records

Each entry: context, decision, consequences, alternatives considered and
rejected. These are the decisions I need to be able to defend without
notes.

---

## ADR-001: Deterministic rules engine, not an LLM, on the decision path

**Context:** the agent decides where physical stock goes and assigns a
temporal bucket, decisions with real financial and safety weight. The
brief demands the reasoning be stated explicitly.

**Decision:** every routing, batch, quantity, and eligibility decision is
computed by deterministic, independently unit-tested functions (see
`DECISION_RULES.md`). No LLM call sits anywhere on this path.

**Consequences:** every decision is reproducible bit for bit on rerun,
testable per-rule in isolation, and explainable by citing the exact rule
that fired rather than hedging about model behaviour. Trade-off: a
genuinely novel conflict type the rules don't cover doesn't get
"reasoned about" adaptively, it falls to quarantine by design. I consider
that the correct trade for a domain with financial and safety stakes.

**Alternatives considered:** LLM-as-arbiter, rejected, not reproducible,
can't be unit tested per rule, "why did it decide that" becomes a
probabilistic answer instead of a citation. An LLM narrator layer over the
finished decision was considered as a separate, optional addition and
explicitly kept off this path even if built later.

---

## ADR-002: Sequence-number ingestion, not arrival order

**Context:** the brief's named failure mode, supplier credit notes arrive
out of order.

**Decision:** supplier notes for a `return_line_id` are ordered and
replayed by the supplier's own monotonic `sequence_number`. `received_at`
is stored for audit purposes only and never drives ordering.

**Consequences:** out-of-order arrival becomes structurally a non-event
rather than something patched around with timestamp heuristics. This does
lean on the supplier's sequence counter being trustworthy, if that counter
is itself inconsistent with `generated_at` for the same chain, that's a
distinct anomaly the ingestion layer needs to flag, not something this
ADR solves, noted for Phase 5/6, not yet built.

**Alternatives considered:** sort by `generated_at` (rejected, the brief
targets timestamp reliability directly, trusting a different timestamp
field doesn't address that); reorder by `received_at` with heuristics
(rejected, heuristic reordering is itself undefendable behaviour).

---

## ADR-003: Conflicts arbitrated by independent fact, never by fixed source preference

**Context:** the brief states directly that "always trust the warehouse"
and "always trust the supplier" are both wrong.

**Decision:** no rule in the matrix hardcodes a winning source. Batch and
best-before conflicts are arbitrated against `BatchRegistryEntry`, a fact
neither party controls. Quantity is capped by physical count. Eligibility
is overridden only by cited photographic evidence, never by source
identity alone.

**Consequences:** defensibility rests on "here is the independent fact
that decided this," not "we trust the warehouse more." This creates a
real operational dependency: registry accuracy. Garbage in the registry
propagates directly into wrong decisions, worth naming honestly rather
than treating the registry as infallible scenery.

**Alternatives considered:** a static per-conflict-type source preference
table, rejected outright, that's the exact "obvious approach" the brief
calls wrong.

---

## ADR-004: Batch code repair via edit distance, not embeddings

**Context:** recovering garbled short alphanumeric codes from a faulty
scanner.

**Decision:** character-level fuzzy matching (edit distance, rapidfuzz),
constrained by SKU and a plausible date window. No vector embeddings
anywhere in this path.

**Consequences:** matches the actual failure mode, character-level scanner
corruption, not semantic drift. Match confidence is a plain, bounded
number (edit distance of 1) rather than a cosine similarity that needs its
own justification.

**Alternatives considered:** embedding-based similarity search, rejected
as the wrong tool for this specific corruption pattern. Would have looked
defensible from a distance and fallen apart under a "why not just use
edit distance" question, which is worse than not having it.

---

## ADR-005: Correlation key sourced from a neutral third system

**Context:** the two source records need a stable join key that isn't
itself one of the disputed attributes.

**Decision:** `return_line_id` is assigned by the RMA/returns
authorisation process when the return is opened, independent of both the
warehouse and the supplier systems. Never derived from batch code or any
other field either party supplies.

**Consequences:** the correlation itself can't be argued as biased toward
either source. Requires the RMA system to actually be the origination
point in any real integration, worth stating as an explicit assumption
rather than leaving it implicit.

**Alternatives considered:** deriving the key from batch code (rejected,
batch code is disputed, this was covered when the schema was designed);
deriving it from either source's own item ID (rejected, same bias
problem one level removed).

---

## ADR-006: Total routing function, quarantine as a designed outcome

**Context:** the brief requires a defensible decision under compounded
failure, not a crash and not a default.

**Decision:** the routing function is total. Every input, however
malformed, produces exactly one of `SCRAP` / `RESTOCK` / `QUARANTINE` plus
a temporal bucket, which may itself be `unknown, pending review`.
Quarantine is only reached through one of the explicit trigger conditions
named in `DECISION_RULES.md`, never a bare exception handler.

**Consequences:** no input can crash the engine or silently fall through
to an undocumented default. Quarantine will legitimately fire more often
than a system willing to guess. I consider that correct for a domain
where a confident wrong guess costs more than an honest "needs a human."

**Alternatives considered:** catch-all exception handler defaulting to a
fixed disposition, rejected, that's "default to one source" wearing a
disguise, implicit instead of explicit.

---

## ADR-007: `UNKNOWN` as a first-class value, not `None`

**Context:** distinguishing "the inspector assessed this and couldn't
determine it" from "this field was never populated."

**Decision:** `ConditionGrade` and `DamageType` carry an explicit
`UNKNOWN` enum member. `condition_grade` is a required field, `UNKNOWN`
counts as fulfilling that requirement. `damage_type` defaults to
`UNKNOWN`, never to `NONE`, when unspecified.

**Consequences:** an incomplete record can never accidentally read as a
documented "no damage" case. The audit trail can honestly distinguish
"assessed, inconclusive" from "not assessed at all," which matters when
the eligibility rule (Rule 5) later decides whether ambiguity itself
should trigger quarantine.

**Alternatives considered:** `Optional[ConditionGrade] = None` throughout,
rejected, collapses two genuinely different evidentiary states into one.

---

## ADR-008: Six integrity checks closed after review, not deferred

**Context:** a review of the working system named six real gaps: no
large-batch/state-isolation test, no git history, `warehouse.sku ==
supplier.sku` assumed but never checked, multiple warehouse records per
line silently keeping the last one, `reason_code` values not validated
against a closed set, and the batch registry treated as ground truth
with no integrity check of its own. Original plan was to document these
as known limitations. With runway remaining before submission, closing
the fixable ones was the better trade than leaving them as talking
points.

**Decision:**
- SKU mismatch between warehouse and supplier is now a detected
  integrity failure (`ConflictType.IDENTITY_MISMATCH`,
  `SKU_MISMATCH`), quarantined rather than silently proceeding on
  records that may not even describe the same item.
- Multiple warehouse records for one `return_line_id` are compared;
  identical duplicates pass through harmlessly, genuinely conflicting
  ones quarantine (`CONFLICTING_WAREHOUSE_RECORDS`), the same principle
  already applied to duplicate supplier notes.
- `BatchRegistryEntry` now rejects `best_before_date <=
  manufactured_date` at construction, a self-contradictory registry
  entry can no longer be built at all.
- `KNOWN_REASON_CODES`, a declared closed set in `rules.py`, checked by
  a 200-example property-based test asserting every `reason_code` any
  real decision emits is a member.
- A 500-item batch test proves no cross-item state leakage, each
  item's resolved batch code is checked against its own registry entry
  specifically, not just "did it get a result."
- Git history was **not** retroactively fabricated. Starting a repository
  now and back-filling commits wouldn't prove an incremental process,
  it would perform one. Real commits from this point forward, one per
  fix, are the honest version of closing that gap.

**Consequences:** `_quarantine_for_missing_evidence` gained a
`conflict_type` parameter, it was hardcoded to `MISSING_COUNTERPART`
before, which was actively wrong for a SKU mismatch (nothing is
missing, both sides reported, they disagree). Caught by the new test
suite itself refusing to accept a mislabelled conflict type, worth
noting as another instance of a test catching a real bug rather than
just confirming intended behaviour.

**Alternatives considered:** converting `reason_code` to a full `Enum`
instead of a declared-set-plus-test, rejected for now, the property
test closes the actual risk (an undeclared code silently never
matching anything) without a ~20-call-site refactor for marginal
additional safety.

**Follow-up:** two items originally deferred to the README's "what I'd
do next" list were small enough to close in the same pass:
cross-entry registry integrity (`validate_registry_integrity`, flags a
batch code appearing twice with conflicting data, doesn't silently
resolve against whichever entry comes first) and a real aggregate
confidence per decision (`LineItemDecision.overall_confidence`, the
minimum of the condition and batch confidences, the two rules that
actually drive physical disposition, not a weighted sum across all
five). The other four items on that list (raw records in the audit
trail, the `reason_code` `Enum` conversion, load testing beyond 500
items, an API layer) were left there deliberately, each is either too
large for the remaining time or genuine scope creep with no rubric
coverage, not something simply forgotten.