"""Response/request DTOs for the intake API routes.

All Pydantic BaseModels — no domain objects (UserBuildBrief, IntakeSessionState,
SessionRecord) ever appear in a response body. See karma ai/docs/intake_routes_plan.md
section 3 for the full contract this file implements.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# QuestionDTO / ProgressDTO
# ---------------------------------------------------------------------------

class QuestionDTO(BaseModel):
    question_id: str | None
    text: str
    kind: Literal["sequence", "clarification", "confirm_default"]


class ProgressDTO(BaseModel):
    answered: int
    total: int
    floor_met: bool


# ---------------------------------------------------------------------------
# BriefSummaryDTO sub-DTOs
# ---------------------------------------------------------------------------

class SecondaryUseCaseDTO(BaseModel):
    use_case: str
    weight: Literal["low", "medium", "high"]


class SoftwareEntryDTO(BaseModel):
    name: str
    category: str
    frequency: str
    intensity: str


class PeripheralDTO(BaseModel):
    type: str
    requirements: str | None
    priority: Literal["must_have", "nice_to_have"]


class ReusePartDTO(BaseModel):
    slot: str
    identifier: str
    action: Literal["keep", "replace"]


class SpecificPartRequestDTO(BaseModel):
    slot: str
    requested: str


# ---------------------------------------------------------------------------
# BriefSummaryDTO
# ---------------------------------------------------------------------------

class BriefSummaryDTO(BaseModel):
    answered_fields: list[str]
    completeness: dict

    budget: dict
    purpose: dict
    software: list[SoftwareEntryDTO]

    performance: dict
    monitor: dict
    peripherals: list[PeripheralDTO]
    storage: dict
    operating_system: dict

    reuse_parts: list[ReusePartDTO]
    brand_prefs: dict

    physical: dict
    longevity: dict
    extras: dict

    hard_constraints: dict


# ---------------------------------------------------------------------------
# Per-route request models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    client_ref: str | None = None


class SubmitAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Per-route response models — split per route, not one shared model with
# optional fields, per plan section 3's note on cleaner OpenAPI schemas.
# ---------------------------------------------------------------------------

class CreateSessionResponse(BaseModel):
    session_id: str
    status: Literal["asking"]
    question: QuestionDTO
    progress: ProgressDTO
    expires_at: datetime


class AnswerAskingResponse(BaseModel):
    status: Literal["asking"]
    question: QuestionDTO
    progress: ProgressDTO
    expires_at: datetime


class AnswerLockedResponse(BaseModel):
    status: Literal["locked"]
    brief_summary: BriefSummaryDTO
    progress: ProgressDTO


class SnapshotResponse(BaseModel):
    status: Literal["asking", "locked"]
    question: QuestionDTO | None
    progress: ProgressDTO
    brief_summary: BriefSummaryDTO | None
    expires_at: datetime


class LockResponse(BaseModel):
    status: Literal["locked"]
    brief_summary: BriefSummaryDTO


# ---------------------------------------------------------------------------
# Build route DTOs — see karma ai/docs/build_service_plan.md section 7.
# ---------------------------------------------------------------------------

class StartBuildRequest(BaseModel):
    session_id: str


class BuildAcceptedDTO(BaseModel):
    build_id: str
    status: Literal["queued"]
    poll_after_ms: int = 2000


class VerdictDTO(BaseModel):
    verdict: Literal["comfortable", "tight", "impossible"]
    reason: str
    binding_constraint: str | None = None
    suggested_adjustments: list[str] = []


class BuildPartDTO(BaseModel):
    slot: str
    product_id: str
    name: str
    brand: str | None = None
    price_inr: int
    justification: str


class BuildCardDTO(BaseModel):
    parts: list[BuildPartDTO]
    total_price_inr: int
    summary: str
    warnings: list[str] = []


class BuildStatusResponse(BaseModel):
    build_id: str
    status: Literal["queued", "running", "succeeded", "infeasible", "cannot_proceed", "failed"]
    poll_after_ms: int | None = None
    verdict: VerdictDTO | None = None
    build: BuildCardDTO | None = None
    error: ErrorBody | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody
