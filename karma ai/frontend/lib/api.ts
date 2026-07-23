import { API_BASE_URL, API_KEY } from "./config";
import type {
  BuildAcceptedDTO,
  BuildStatusResponse,
  CreateSessionResponse,
  ErrorBody,
  LockResponse,
  SubmitAnswerResponse,
} from "./types";

export class ApiError extends Error {
  code: string;
  retryable: boolean;
  details: Record<string, unknown> | null;
  retryAfterS: number | null;
  httpStatus: number;

  constructor(httpStatus: number, body: ErrorBody, retryAfterS: number | null) {
    super(body.message);
    this.name = "ApiError";
    this.code = body.code;
    this.retryable = body.retryable;
    this.details = body.details ?? null;
    this.retryAfterS = retryAfterS;
    this.httpStatus = httpStatus;
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  { retryTurnInProgress = 0 }: { retryTurnInProgress?: number } = {}
): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": API_KEY,
      ...init.headers,
    },
  });

  if (res.status === 204) {
    return undefined as T;
  }

  const payload = await res.json().catch(() => null);

  if (!res.ok) {
    const body: ErrorBody = payload?.error ?? {
      code: "UNKNOWN_ERROR",
      message: `Request failed with status ${res.status}`,
      retryable: false,
    };
    const retryAfterHeader = res.headers.get("Retry-After");
    const retryAfterS = retryAfterHeader ? Number(retryAfterHeader) : null;

    // TURN_IN_PROGRESS is UX-only on the server (nothing queues/retries
    // there) — the contract explicitly puts the backoff-and-repost burden on
    // the client (frontend_contract_plan.md section 8 item 6).
    if (body.code === "TURN_IN_PROGRESS" && retryTurnInProgress < 3) {
      await sleep((retryAfterS ?? 1) * 1000);
      return request<T>(path, init, { retryTurnInProgress: retryTurnInProgress + 1 });
    }

    throw new ApiError(res.status, body, retryAfterS);
  }

  return payload as T;
}

export function createSession(clientRef?: string) {
  return request<CreateSessionResponse>("/intake/sessions", {
    method: "POST",
    body: JSON.stringify({ client_ref: clientRef ?? null }),
  });
}

export function submitAnswer(sessionId: string, answer: string) {
  return request<SubmitAnswerResponse>(
    `/intake/sessions/${encodeURIComponent(sessionId)}/answers`,
    { method: "POST", body: JSON.stringify({ answer }) }
  );
}

export function lockSession(sessionId: string) {
  return request<LockResponse>(
    `/intake/sessions/${encodeURIComponent(sessionId)}/lock`,
    { method: "POST" }
  );
}

export function abandonSession(sessionId: string) {
  return request<void>(`/intake/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  }).catch(() => {
    // Best-effort cleanup only — never block the UI on it.
  });
}

export function startBuild(sessionId: string) {
  return request<BuildAcceptedDTO>("/builds", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export function getBuildStatus(buildId: string) {
  return request<BuildStatusResponse>(`/builds/${encodeURIComponent(buildId)}`);
}
