"""Structured logging correlation IDs.

Per docs/hardening_plan.md section 4: contextvars are the whole mechanism --
no JSON logs, no aggregation pipeline, plain text with correlation fields,
right-sized for a two-person team. Three contextvars carry the trail:

- request_id_var: set once per HTTP request by main.py's middleware.
- session_id_var: set by IntakeService the moment a session_id is known
  (submit_answer) and by BuildService (the build's originating session_id).
- build_id_var: set by BuildService the moment a build_id is known
  (start_build / _run_and_store).

ContextInjectingFilter copies their current values onto every LogRecord, so
existing logger.exception(...) call sites already in errors.py/
build_service.py/etc. carry the ids for free -- zero changes needed at
those call sites themselves, since the filter runs at emission time,
downstream of wherever the contextvar was set for the request/turn/build
currently in flight.

Caveat (plan section 4): contextvars do NOT automatically propagate into
loop.run_in_executor worker threads (that propagation is an asyncio.Task
feature -- copied only when a Task is created -- not a general threading
feature). Core-side log lines emitted inside a pipeline worker thread
(run_from_brief, intake_step) will not carry these ids unless the caller
explicitly threads a copied context in. build_service.py does this for its
executor.submit call (the highest-value trace: correlating a build's own
long-running worker); intake_service.py deliberately does not (short turns,
lower value -- per the plan, not over-engineered).
"""

from __future__ import annotations

import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="-")
build_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("build_id", default="-")

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[req=%(request_id)s sid=%(session_id)s bid=%(build_id)s] %(message)s"
)

# Marker attribute used to detect our own handler already sitting on the root
# logger, rather than a module-level "already configured" flag -- so
# idempotency is judged from the root logger's actual state (robust to e.g.
# create_app() being called more than once in a test suite) instead of
# process-lifetime mutable state that a test can't easily reset.
_HANDLER_MARKER = "_karma_correlation_handler"


class ContextInjectingFilter(logging.Filter):
    """Copies the three correlation contextvars onto every LogRecord.

    Implemented as a logging.Filter (not a custom Handler/Formatter) so it
    applies uniformly to whatever handler it's attached to and composes with
    the stdlib's normal handler/formatter pipeline unmodified.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        record.build_id = build_id_var.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a correlation-aware StreamHandler to the root logger.

    Idempotent: if a handler carrying _HANDLER_MARKER is already on the root
    logger, this is a no-op. Without that guard, a second call (e.g.
    create_app() invoked more than once, as several tests in this repo do)
    would attach a second handler and duplicate every log line.
    """
    root = logging.getLogger()
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers):
        return

    handler = logging.StreamHandler()
    setattr(handler, _HANDLER_MARKER, True)
    handler.addFilter(ContextInjectingFilter())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root.addHandler(handler)
    root.setLevel(level)
