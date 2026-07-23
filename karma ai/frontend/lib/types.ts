// Mirrors karma ai/api/dtos.py — the frozen v1 frontend contract
// (docs/frontend_contract_plan.md). Keep in lockstep with that file; a
// breaking DTO change ships under /api/v2, never by mutating these in place.

export type QuestionKind = "sequence" | "clarification" | "confirm_default";

export interface QuestionDTO {
  question_id: string | null;
  text: string;
  kind: QuestionKind;
}

export interface ProgressDTO {
  answered: number;
  total: number;
  floor_met: boolean;
}

export interface SoftwareEntryDTO {
  name: string;
  category: string;
  frequency: string;
  intensity: string;
}

export interface PeripheralDTO {
  type: string;
  requirements: string | null;
  priority: "must_have" | "nice_to_have";
}

export interface ReusePartDTO {
  slot: string;
  identifier: string;
  action: "keep" | "replace";
}

export interface BriefSummaryDTO {
  answered_fields: string[];
  completeness: Record<string, unknown>;
  budget: Record<string, unknown>;
  purpose: Record<string, unknown>;
  software: SoftwareEntryDTO[];
  performance: Record<string, unknown>;
  monitor: Record<string, unknown>;
  peripherals: PeripheralDTO[];
  storage: Record<string, unknown>;
  operating_system: Record<string, unknown>;
  reuse_parts: ReusePartDTO[];
  brand_prefs: Record<string, unknown>;
  physical: Record<string, unknown>;
  longevity: Record<string, unknown>;
  extras: Record<string, unknown>;
  hard_constraints: Record<string, unknown>;
}

export interface CreateSessionResponse {
  session_id: string;
  status: "asking";
  question: QuestionDTO;
  progress: ProgressDTO;
  expires_at: string;
}

export interface AnswerAskingResponse {
  status: "asking";
  question: QuestionDTO;
  progress: ProgressDTO;
  expires_at: string;
}

export interface AnswerLockedResponse {
  status: "locked";
  brief_summary: BriefSummaryDTO;
  progress: ProgressDTO;
}

export type SubmitAnswerResponse = AnswerAskingResponse | AnswerLockedResponse;

export interface LockResponse {
  status: "locked";
  brief_summary: BriefSummaryDTO;
}

export interface BuildAcceptedDTO {
  build_id: string;
  status: "queued";
  poll_after_ms: number;
}

export interface VerdictDTO {
  verdict: "comfortable" | "tight" | "impossible";
  reason: string;
  binding_constraint: string | null;
  suggested_adjustments: string[];
}

export interface BuildPartDTO {
  slot: string;
  product_id: string;
  name: string;
  brand: string | null;
  price_inr: number;
  justification: string;
}

export interface BuildCardDTO {
  parts: BuildPartDTO[];
  total_price_inr: number;
  summary: string;
  warnings: string[];
}

export type BuildStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "infeasible"
  | "cannot_proceed"
  | "failed";

export interface ErrorBody {
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown> | null;
}

export interface BuildStatusResponse {
  build_id: string;
  status: BuildStatus;
  poll_after_ms?: number | null;
  verdict?: VerdictDTO | null;
  build?: BuildCardDTO | null;
  error?: ErrorBody | null;
  reason?: string | null;
}
