"""Shared LLM client wrapper.

Single, generic entry point for OpenAI calls used by every pipeline node. This module
is the *only* place the OpenAI client is instantiated and the only place the API key is
read from the environment. It contains no node-specific logic.

Public surface:
    call_structured(prompt, response_model, *, system=None, max_retries=2,
                     model=None, temperature=None) -> response_model
    call_text(prompt, *, system=None) -> str
    StructuredCallError
    DEFAULT_MODEL
    THRESHOLD_MODEL
    REFINEMENT_MODEL
"""

from __future__ import annotations

import json
import os
from typing import Type, TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ValidationError

# Load .env so a standalone import has the key available. Idempotent: only fills vars
# that are not already set in the process environment.
load_dotenv()

# Model is configurable here / via env, never hardcoded deep in a call site.
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Stronger model reserved for calls whose reasoning quality drives every downstream pick
# (currently: Node 3 fitness threshold derivation). See CLAUDE.md model allocation policy.
THRESHOLD_MODEL = os.getenv("KARMA_THRESHOLD_MODEL", "gpt-4o")

# Used by node3_refinement.py's v2 intent-based refinement parsing
# (parse_refinement_request_v2), active when KARMA_REFINEMENT_MODE=intent.
# Defaults to the same model the rest of the app uses (DEFAULT_MODEL).
REFINEMENT_MODEL = os.getenv("KARMA_REFINEMENT_MODEL", DEFAULT_MODEL)

T = TypeVar("T", bound=BaseModel)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Lazily instantiate the one OpenAI client. API key is read here and nowhere else."""
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to your .env (see .env.example)."
            )
        _client = OpenAI(api_key=api_key)
    return _client


class StructuredCallError(Exception):
    """Raised when call_structured fails to produce a valid instance after all retries.

    Carries the last underlying error and the last raw model output so callers can log
    or inspect exactly what went wrong.
    """

    def __init__(self, last_error: Exception, raw_output: str | None):
        self.last_error = last_error
        self.raw_output = raw_output
        super().__init__(
            f"Structured call failed after retries. "
            f"Last error: {type(last_error).__name__}: {last_error}. "
            f"Last raw output: {raw_output!r}"
        )


def call_structured(
    prompt: str,
    response_model: Type[T],
    *,
    system: str | None = None,
    max_retries: int = 2,
    model: str | None = None,
    temperature: float | None = None,
) -> T:
    """Call the model and return a validated instance of `response_model`.

    Uses OpenAI JSON mode. Validation happens in two distinct stages:
        stage 1: json.loads on the raw text       -> syntax failure
        stage 2: response_model.model_validate(...) -> schema/enum failure

    On either failure the call is retried up to `max_retries` times, feeding the specific
    error back into the conversation so the model can self-correct. The retry message
    distinguishes a JSON-syntax failure from a schema-shape failure. Once retries are
    exhausted, raises StructuredCallError carrying the last error and raw output.

    `model` and `temperature` default to None, which preserves the existing behavior
    (DEFAULT_MODEL, provider-default temperature) for every current call site. Pass them
    explicitly to override per call — e.g. a lower temperature for a threshold-derivation
    task that should be as deterministic as the task allows.
    """
    client = _get_client()
    schema = json.dumps(response_model.model_json_schema(), indent=2)
    resolved_model = model or DEFAULT_MODEL
    extra_kwargs: dict = {} if temperature is None else {"temperature": temperature}

    # JSON mode requires the word "JSON" to appear in the messages; the schema below also
    # gives the model the exact shape to target. Fully generic — no node specifics.
    base_system = (
        (system + "\n\n") if system else ""
    ) + (
        "Respond with ONLY a single valid JSON object — no prose, no markdown fences. "
        "The JSON must conform to this JSON Schema:\n" + schema
    )

    messages: list[dict] = [
        {"role": "system", "content": base_system},
        {"role": "user", "content": prompt},
    ]

    last_error: Exception | None = None
    raw: str | None = None

    # One initial attempt plus up to max_retries corrective attempts.
    for attempt in range(max_retries + 1):
        completion = client.chat.completions.create(
            model=resolved_model,
            messages=messages,
            response_format={"type": "json_object"},
            **extra_kwargs,
        )
        raw = completion.choices[0].message.content or ""

        # --- Stage 1: JSON syntax ---
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was NOT valid JSON (a syntax error). "
                            f"Parser error: {exc}. "
                            "Reply again with ONLY a single valid JSON object."
                        ),
                    }
                )
            continue

        # --- Stage 2: schema / enum validation ---
        try:
            return response_model.model_validate(parsed)
        except ValidationError as exc:
            last_error = exc
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your JSON was syntactically valid but did NOT match the "
                            "required schema (wrong shape / missing fields / bad enum "
                            f"value). Validation errors:\n{exc}\n"
                            "Fix these fields and reply again with ONLY a single valid "
                            "JSON object matching the schema."
                        ),
                    }
                )
            continue

    raise StructuredCallError(last_error, raw)


def call_text(prompt: str, *, system: str | None = None) -> str:
    """Plain text completion: no schema, no JSON mode. Returns the model's text.

    Separate path for conversational free-text (e.g. asking the next intake question).
    """
    client = _get_client()
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    completion = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=messages,
    )
    return completion.choices[0].message.content or ""
