from __future__ import annotations

from datetime import datetime
from typing import Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field

from .source_flag import SourceFlag
from .slots import ComponentSlot


# --- Section 0 helpers ---

class Completeness(BaseModel):
    required_complete: bool
    optional_filled: int
    optional_skipped: int


# --- Section 1 — Budget ---

class Budget(BaseModel):
    currency: Literal["INR"] = "INR"
    comfortable_min: int
    comfortable_max: int
    ceiling: int
    scope: Literal["pc_only", "pc_plus_monitor", "pc_plus_peripherals", "full_setup"]
    notes: str | None = None


# --- Section 2 — Purpose ---

class SecondaryUseCase(BaseModel):
    use_case: str
    weight: Literal["low", "medium", "high"]


class Purpose(BaseModel):
    primary_use_case: Literal[
        "gaming", "content_creation", "work_productivity",
        "storage_homeserver", "general_use"
    ]
    sub_case: str
    secondary_use_cases: list[SecondaryUseCase] = []


# --- Section 3 — Software & workload ---

class SoftwareEntry(BaseModel):
    name: str
    category: Literal["game", "video", "3d", "audio", "dev", "other"]
    frequency: Literal["primary", "secondary", "occasional"]
    intensity: Literal["casual", "moderate", "heavy"]


# --- Section 4 — Performance targets ---

class Performance(BaseModel):
    target_resolution: Literal["1080p", "1440p", "4K"] | None = None
    target_framerate: Union[int, Literal["max"], None] = None
    hdr_wanted: bool = False
    source: SourceFlag


# --- Section 5 — Monitor ---

class OwnedSpecs(BaseModel):
    resolution: str
    refresh_hz: int
    hdr: bool
    size_inch: float


class TargetSpecs(BaseModel):
    resolution: str
    refresh_hz: int
    hdr: bool


class Monitor(BaseModel):
    owned: Literal["yes", "no"]
    owned_specs: OwnedSpecs | None = None
    target_specs: TargetSpecs | None = None
    count: int = 1
    source: SourceFlag


# --- Section 6 — Peripherals ---

class Peripheral(BaseModel):
    type: Literal[
        "keyboard", "mouse", "headset", "mic", "speakers",
        "drawing_tablet", "controller", "webcam"
    ]
    requirements: str | None = None
    priority: Literal["must_have", "nice_to_have"]


# --- Section 7 — Storage ---

class Storage(BaseModel):
    capacity_gb: int | None = None
    speed_tier: Literal["nvme", "sata_ssd", "hdd", "mixed"]
    data_profile: Literal["cold", "warm", "hot", "mixed"]
    source: SourceFlag


# --- Section 8 — Operating system ---

class OperatingSystem(BaseModel):
    os: Literal["windows", "linux", "dual_boot", "none_reuse"]
    license: Literal["oem", "retail", "byo", "na"]
    source: SourceFlag


# --- Section 9 — Existing assets ---

class ReusePart(BaseModel):
    slot: ComponentSlot
    identifier: str
    action: Literal["keep", "replace"]


class EcosystemPrefs(BaseModel):
    cpu_brand_pref: str | None = None
    gpu_brand_pref: str | None = None


class Existing(BaseModel):
    has_existing_parts: Literal["yes", "no"]
    reuse_parts: list[ReusePart] = []
    existing_pc_build_id: UUID | None = None
    ecosystem_prefs: EcosystemPrefs = Field(default_factory=EcosystemPrefs)


# --- Section 10 — Physical ---

class Physical(BaseModel):
    form_factor_pref: Literal[
        "full_tower", "atx_mid", "compact_matx", "sff_itx", "no_preference"
    ] = "no_preference"
    noise_tolerance: Literal["silent_priority", "balanced", "dont_care"] = "balanced"
    placement: Literal["open_desk", "enclosed_cabinet", "hot_room", "normal"] = "normal"
    portability_need: bool = False
    size_notes: str | None = None


# --- Section 11 — Longevity ---

class Longevity(BaseModel):
    reliability_priority: Literal[
        "consumer", "high_stability_alwayson", "mission_critical"
    ] = "consumer"
    upgrade_path: Literal["future_proof", "balanced", "set_and_forget"] = "balanced"
    timeline: Literal["buy_now", "flexible_for_deals"] = "buy_now"


# --- Section 12 — Extras ---

class SpecificPartRequest(BaseModel):
    slot: ComponentSlot
    requested: str


class Extras(BaseModel):
    rgb_pref: Literal["want_rgb", "minimal", "none", "no_preference"] = "no_preference"
    visual_style: Literal["showcase_glass", "clean_sleeper", "no_preference"] = "no_preference"
    connectivity_needs: list[Literal["wifi", "bluetooth", "thunderbolt", "10gbe", "many_usb"]] = []
    specific_part_requests: list[SpecificPartRequest] = []


# --- Section 13 — Hard constraints ---

class Constraint(BaseModel):
    id: UUID
    type: str
    value: str
    source: Literal["user_stated", "derived"]
    locked_at: datetime


class RejectedPart(BaseModel):
    product_id: str
    reason: str
    rejected_at: datetime


class HardConstraints(BaseModel):
    must_have: list[Constraint] = []
    must_not: list[Constraint] = []
    rejected_parts: list[RejectedPart] = []


# --- Top-level ---

class UserBuildBrief(BaseModel):
    # 0 — Envelope
    brief_id: UUID
    user_id: UUID
    chat_id: UUID
    build_id: UUID | None = None
    schema_version: str
    status: Literal["draft", "locked", "revisiting"] = "draft"
    completeness: Completeness
    open_questions: list[str] = []
    created_at: datetime
    updated_at: datetime
    # 1 — Budget (REQUIRED)
    budget: Budget
    # 2 — Purpose (REQUIRED)
    purpose: Purpose
    # 3 — Software & workload
    software: list[SoftwareEntry] = []
    # 4 — Performance targets
    performance: Performance
    # 5 — Monitor
    monitor: Monitor
    # 6 — Peripherals
    peripherals: list[Peripheral] = []
    # 7 — Storage
    storage: Storage
    # 8 — Operating system
    operating_system: OperatingSystem
    # 9 — Existing assets
    existing: Existing
    # 10 — Physical (optional → defaults)
    physical: Physical = Field(default_factory=Physical)
    # 11 — Longevity (optional → defaults)
    longevity: Longevity = Field(default_factory=Longevity)
    # 12 — Extras (optional → defaults)
    extras: Extras = Field(default_factory=Extras)
    # 13 — Hard constraints (PINNED)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
