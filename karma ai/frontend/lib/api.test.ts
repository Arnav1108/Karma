import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getBuildStatus } from "./api";

// Fixtures are the frozen v1 error envelope (docs/frontend_contract_plan.md
// section 5a). Imported by relative path from api/contract/ so a backend
// fixture regen breaks this test.
import validationError from "../../api/contract/fixtures/errors/validation_error.json";
import unauthorized from "../../api/contract/fixtures/errors/unauthorized.json";
import sessionNotFound from "../../api/contract/fixtures/errors/session_not_found.json";
import sessionAlreadyLocked from "../../api/contract/fixtures/errors/session_already_locked.json";
import briefFloorNotMet from "../../api/contract/fixtures/errors/brief_floor_not_met.json";
import briefNotLocked from "../../api/contract/fixtures/errors/brief_not_locked.json";
import buildAlreadyActive from "../../api/contract/fixtures/errors/build_already_active.json";
import buildNotFound from "../../api/contract/fixtures/errors/build_not_found.json";
import buildCapacity from "../../api/contract/fixtures/errors/build_capacity.json";
import rateLimited from "../../api/contract/fixtures/errors/rate_limited.json";
import llmUpstreamError from "../../api/contract/fixtures/errors/llm_upstream_error.json";
import databaseUnavailable from "../../api/contract/fixtures/errors/database_unavailable.json";
import internalError from "../../api/contract/fixtures/errors/internal_error.json";
import turnInProgress from "../../api/contract/fixtures/errors/turn_in_progress.json";

// HTTP status + Retry-After wiring per frontend_contract_plan.md section 5a
// (source of truth: api/errors.py + api/rate_limit.py). Fixture bodies only
// carry the JSON envelope; status and the Retry-After header are transport
// concerns the fixtures don't encode, so they're supplied here from the same
// contract table. TURN_IN_PROGRESS is exercised separately below (retry loop).
const CASES: Array<{
  name: string;
  fixture: { error: { code: string; message: string; retryable: boolean; details?: Record<string, unknown> } };
  status: number;
  retryAfterHeader: string | null;
  expectedRetryAfterS: number | null;
}> = [
  { name: "validation_error", fixture: validationError, status: 422, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "unauthorized", fixture: unauthorized, status: 401, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "session_not_found", fixture: sessionNotFound, status: 404, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "session_already_locked", fixture: sessionAlreadyLocked, status: 409, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "brief_floor_not_met", fixture: briefFloorNotMet, status: 409, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "brief_not_locked", fixture: briefNotLocked, status: 409, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "build_already_active", fixture: buildAlreadyActive, status: 409, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "build_not_found", fixture: buildNotFound, status: 404, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "build_capacity", fixture: buildCapacity, status: 429, retryAfterHeader: "30", expectedRetryAfterS: 30 },
  { name: "rate_limited", fixture: rateLimited, status: 429, retryAfterHeader: "42", expectedRetryAfterS: 42 },
  { name: "llm_upstream_error", fixture: llmUpstreamError, status: 502, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "database_unavailable", fixture: databaseUnavailable, status: 503, retryAfterHeader: null, expectedRetryAfterS: null },
  { name: "internal_error", fixture: internalError, status: 500, retryAfterHeader: null, expectedRetryAfterS: null },
];

function mockFetchOnce(body: unknown, status: number, retryAfterHeader: string | null) {
  const headers = new Headers();
  if (retryAfterHeader !== null) headers.set("Retry-After", retryAfterHeader);
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers })
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ApiError mapping (lib/api.ts request())", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  for (const c of CASES) {
    it(`maps ${c.name} (HTTP ${c.status}) to an ApiError with correct code/retryable/details/Retry-After`, async () => {
      mockFetchOnce(c.fixture, c.status, c.retryAfterHeader);

      let caught: unknown;
      try {
        await getBuildStatus("build-under-test");
      } catch (err) {
        caught = err;
      }

      expect(caught).toBeInstanceOf(ApiError);
      const apiError = caught as ApiError;
      expect(apiError.code).toBe(c.fixture.error.code);
      expect(apiError.retryable).toBe(c.fixture.error.retryable);
      expect(apiError.details).toEqual(c.fixture.error.details ?? null);
      expect(apiError.retryAfterS).toBe(c.expectedRetryAfterS);
      expect(apiError.httpStatus).toBe(c.status);
      expect(apiError.message).toBe(c.fixture.error.message);
    });
  }
});

describe("TURN_IN_PROGRESS retry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("retries up to 3 times with backoff, then throws ApiError(TURN_IN_PROGRESS)", async () => {
    // A fresh Response per call — a Response body can only be read once, and
    // this path is hit 4 times (initial + 3 retries).
    const fetchMock = vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify(turnInProgress), {
          status: 409,
          headers: { "Retry-After": "1" },
        })
    );
    vi.stubGlobal("fetch", fetchMock);

    const assertion = expect(getBuildStatus("build-under-test")).rejects.toMatchObject({
      name: "ApiError",
      code: "TURN_IN_PROGRESS",
      retryable: true,
    });

    // Each retry sleeps for Retry-After seconds; flush every pending timer
    // (real elapsed time stays ~0ms) rather than waiting through real backoff.
    await vi.runAllTimersAsync();
    await assertion;

    // 1 initial attempt + 3 retries, bounded by `retryTurnInProgress < 3` in
    // lib/api.ts — a 4th retry would mean the bound regressed.
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
