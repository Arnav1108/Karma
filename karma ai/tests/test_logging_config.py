"""Unit tests for api/logging_config.py.

ContextInjectingFilter is tested against a real logging.LogRecord (not a
mock), constructed the same way the stdlib logging machinery constructs one,
so the assertions guard the actual attribute names (request_id/session_id/
build_id) any %(request_id)s-style formatter string depends on.

Every test that sets one of the three module-level contextvars resets it via
the token pattern in a finally block, so no test leaks its value into a test
that runs after it in the same process/thread.
"""

from __future__ import annotations

import logging

from api.logging_config import (
    ContextInjectingFilter,
    build_id_var,
    request_id_var,
    session_id_var,
)


def _make_record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_filter_copies_set_contextvars_onto_record() -> None:
    req_token = request_id_var.set("req-123")
    sid_token = session_id_var.set("sess-abc")
    bid_token = build_id_var.set("build-xyz")
    try:
        record = _make_record()
        result = ContextInjectingFilter().filter(record)

        assert result is True
        assert record.request_id == "req-123"
        assert record.session_id == "sess-abc"
        assert record.build_id == "build-xyz"
    finally:
        request_id_var.reset(req_token)
        session_id_var.reset(sid_token)
        build_id_var.reset(bid_token)


def test_filter_defaults_to_dash_when_unset() -> None:
    # No .set() calls in this test -- exercises each var's declared default
    # ("-") directly, guarding against the default ever silently changing to
    # None or "" (either of which would render worse in the log format string).
    record = _make_record()
    ContextInjectingFilter().filter(record)

    assert record.request_id == "-"
    assert record.session_id == "-"
    assert record.build_id == "-"


def test_filter_reflects_only_the_currently_set_value_not_a_stale_one() -> None:
    """A previous .set() in an unrelated context must not bleed into a record
    filtered after that context's token was reset."""
    token = session_id_var.set("stale-session")
    session_id_var.reset(token)

    record = _make_record()
    ContextInjectingFilter().filter(record)

    assert record.session_id == "-"


def test_configure_logging_is_idempotent() -> None:
    from api.logging_config import _HANDLER_MARKER, configure_logging

    configure_logging()
    configure_logging()  # second call must not attach a second handler

    root = logging.getLogger()
    marked = sum(1 for h in root.handlers if getattr(h, _HANDLER_MARKER, False))
    assert marked == 1
