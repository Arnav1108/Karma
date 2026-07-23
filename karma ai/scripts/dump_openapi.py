"""Regenerate the committed OpenAPI snapshot at api/contract/openapi.json.

The Next.js team consumes this static file directly (TypeScript codegen, Postman
import, mock servers) without needing a running server or an API key
(docs/frontend_contract_plan.md section 1.1). tests/test_openapi_contract.py freezes
it, so an accidental DTO shape change fails CI (section 3 / section 7).

Regenerating is a deliberate, reviewable step: run this whenever the contract changes
on purpose and commit the resulting diff.

Usage (from `karma ai/`):
    python -m scripts.dump_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from api.main import create_app

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "api" / "contract" / "openapi.json"


def render_openapi() -> dict:
    """Return the live OpenAPI document from the real app factory."""
    return create_app().openapi()


def serialize(spec: dict) -> str:
    """Stable, human-diffable JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_snapshot(path: Path = SNAPSHOT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(render_openapi()), encoding="utf-8")
    return path


if __name__ == "__main__":
    out = write_snapshot()
    print(f"wrote OpenAPI snapshot to {out}")
