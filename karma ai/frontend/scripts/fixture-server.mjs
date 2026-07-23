// fixture-server.mjs
//
// Zero-dependency HTTP stub that serves committed contract fixtures from
// ../../api/contract/fixtures/** so the frontend can render error and
// terminal-build states that are unreachable through a live backend run.
// See karma ai/docs/frontend_implementation_plan.md, Phase 2, for why:
// `cannot_proceed` is designed-unreachable, `failed`/5xx require breaking a
// live dependency to provoke, and BRIEF_FLOOR_NOT_MET / BRIEF_NOT_LOCKED are
// unreachable through the UI at all.
//
// This is a standing dev tool, not a throwaway script: Phase 4 (the
// error-state audit) and any future work touching error/terminal-state
// rendering needs it again. Do not delete it after Phase 4 lands.
//
// Fixtures are read from disk on every request (no caching/bundling), so a
// backend fixture regen is picked up without restarting this process.
//
// Usage:   node scripts/fixture-server.mjs [port]     (default 8001)
//   GET /?scenario=<name>  -> the mapped fixture, with its real HTTP status
//                             and headers (e.g. Retry-After).
//   GET /                  -> lists available scenario names as JSON.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.argv[2]) || 8001;
const ALLOWED_ORIGIN = 'http://localhost:3000';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES_ROOT = path.resolve(__dirname, '../../api/contract/fixtures');

// scenario -> { file (relative to FIXTURES_ROOT), status, headers?, patch? }
// Status/header values mirror api/errors.py's exception handlers exactly.
const SCENARIOS = {
  // Build-status endpoint responses: always HTTP 200, error is in-band.
  cannot_proceed: { file: 'result/build_status_cannot_proceed.json', status: 200 },
  failed_retryable: { file: 'result/build_status_failed.json', status: 200 },
  failed_not_retryable: {
    file: 'result/build_status_failed.json',
    status: 200,
    patch: (body) => { body.error.retryable = false; },
  },

  // Floor-gated errors, unreachable through the real UI at all.
  brief_floor_not_met: { file: 'errors/brief_floor_not_met.json', status: 409 },
  brief_not_locked: { file: 'errors/brief_not_locked.json', status: 409 },

  // TURN_IN_PROGRESS: racing two real turns is flaky, so this is stub-only.
  turn_in_progress: {
    file: 'errors/turn_in_progress.json',
    status: 409,
    headers: { 'Retry-After': '1' },
  },

  // 5xx family: otherwise requires breaking a live dependency to provoke.
  llm_upstream_error: { file: 'errors/llm_upstream_error.json', status: 502 },
  database_unavailable: { file: 'errors/database_unavailable.json', status: 503 },
  internal_error: { file: 'errors/internal_error.json', status: 500 },
};

function withCors(res) {
  res.setHeader('Access-Control-Allow-Origin', ALLOWED_ORIGIN);
  res.setHeader('Access-Control-Allow-Credentials', 'true');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-API-Key');
}

const server = http.createServer((req, res) => {
  withCors(res);

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://localhost:${PORT}`);
  const scenario = url.searchParams.get('scenario');

  if (!scenario) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ scenarios: Object.keys(SCENARIOS) }, null, 2));
    return;
  }

  const entry = SCENARIOS[scenario];
  if (!entry) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `unknown scenario: ${scenario}` }));
    return;
  }

  let body;
  try {
    const raw = fs.readFileSync(path.join(FIXTURES_ROOT, entry.file), 'utf8');
    // Unpatched fixtures are forwarded byte-for-byte; only the synthesized
    // failed_not_retryable variant is parsed, mutated, and re-serialized.
    body = raw;
    if (entry.patch) {
      const parsed = JSON.parse(raw);
      entry.patch(parsed);
      body = JSON.stringify(parsed, null, 2);
    }
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `failed to read fixture: ${err.message}` }));
    return;
  }

  res.writeHead(entry.status, { 'Content-Type': 'application/json', ...entry.headers });
  res.end(body);
});

server.listen(PORT, () => {
  console.log(`fixture-server listening on http://localhost:${PORT}`);
});
