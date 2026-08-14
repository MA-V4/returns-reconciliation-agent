"""
Domain schemas for the returns reconciliation agent.

Design note (read before touching anything downstream):

The two source records below are correlated by `return_line_id`, a stable
identifier assigned when the return is authorised, NOT by batch code or
best-before date. Batch code and best-before date are themselves disputed
attributes (the brief names "which batch it belongs to" as one of the
required conflict types). Using either as the join key would make a batch
mismatch invisible instead of detectable, the two records would just look
like two unrelated items instead of one item in dispute. SKU is treated as
reliably shared across both sources; everything else on the item is a claim
to be reconciled, not a fact to be trusted.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConditionGrade(str, Enum):
    SELLABLE = "sellable"
    MINOR_DAMAGE = "minor_damage"
    MAJOR_DAMAGE = "major_damage"
    DESTROYED = "destroyed"
    UNKNOWN = "unknown"  # inspector could not determine, do not silently coerce this


class DamageType(str, Enum):
    NONE = "none"
    WATER = "water"
    CRUSH = "crush"
    PACKAGING_ONLY = "packaging_only"
    CONTAMINATION = "contamination"
    EXPIRY = "expiry"
    OTHER = "other"
    UNKNOWN = "unknown"


class Disposition(str, Enum):
    SCRAP = "scrap"
    RESTOCK = "restock"
    QUARANTINE = "quarantine"  # mandatory fallback for anything unresolved, never a silent default


class WarehouseInspectionRecord(BaseModel):
    """One warehouse inspector's assessment of one return line item.

    The warehouse sees the physical state
    but can misread batch codes under poor lighting or scanner fault.
    `raw_scanner_output` is preserved verbatim, garbled or not, so the audit
    trail can later show exactly what was read and what was discarded.
    """

    record_id: str
    return_line_id: str  # correlation key, see module docstring
    sku: str

    condition_grade: ConditionGrade
    damage_type: DamageType = DamageType.UNKNOWN
    inspected_quantity: int = Field(ge=0)

    raw_scanner_output: Optional[str] = None  # verbatim, possibly garbled
    claimed_batch_code: Optional[str] = None  # cleaned/parsed, if parseable at all
    batch_scan_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    best_before_date: Optional[date] = None
    inspector_id: Optional[str] = None
    inspector_has_photo_evidence: bool = False

    inspected_at: datetime


class SupplierCreditNote(BaseModel):
    """One supplier system's assessment of one return line item.

    Also a claim, not ground truth: the supplier knows the product but has a
    financial incentive to minimise credit. `sequence_number` is the
    supplier system's own monotonic revision counter and is what the agent
    trusts for ordering. `received_at` (wall-clock arrival) is explicitly
    NOT trusted for ordering, that gap is the out-of-order-timestamp
    failure mode named in the brief.
    """

    note_id: str
    return_line_id: str
    sku: str

    sequence_number: int  # monotonic per supplier system, source of truth for ordering
    generated_at: datetime  # when the supplier created this note
    received_at: datetime  # when it arrived, may be out of order vs sequence_number

    claimed_batch_code: Optional[str] = None
    claimed_best_before_date: Optional[date] = None

    eligible_for_credit: bool
    credit_quantity: int = Field(ge=0)
    restock_required: bool
    credit_amount: Optional[Decimal] = None

    supersedes_note_id: Optional[str] = None  # explicit correction chain, if supplier provides one


class BatchRegistryEntry(BaseModel):
    """Ground truth reference used to validate disputed batch claims.

    Neither source's batch claim is trusted on its own. Both get checked
    against this registry, constrained by SKU and a plausible date window,
    before either is accepted as the resolved batch.
    """

    batch_code: str
    sku: str
    manufactured_date: date
    best_before_date: date