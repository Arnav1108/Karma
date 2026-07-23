export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

// v1 auth model is a static shared key shipped to the browser (see
// docs/frontend_contract_plan.md section 6) — acceptable only because v1's
// audience is internal/controlled. Must be replaced by per-user auth before
// any public launch.
export const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";
