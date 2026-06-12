"""Tests for the structured JSONL logging infrastructure (logs.py)."""

import json
import logging

import pytest

from archie.logs import ContextFilter, JsonFormatter, bind, clear, log_event


@pytest.fixture(autouse=True)
def _clean_context():
    """Each test starts and ends with empty ambient context."""
    clear()
    yield
    clear()


@pytest.fixture
def capture(request):
    """A logger wired to JsonFormatter + ContextFilter, capturing formatted lines."""
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    log = logging.getLogger(f"test.logs.{request.node.name}")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    yield log, lines
    log.removeHandler(handler)


def _last(lines: list[str]) -> dict:
    return json.loads(lines[-1])


class TestJsonFormatter:
    def test_emits_valid_json_with_base_fields(self, capture):
        log, lines = capture
        log.info("hello %s", "world")
        rec = _last(lines)
        assert rec["msg"] == "hello world"
        assert rec["level"] == "INFO"
        assert rec["logger"].startswith("test.logs")
        # UTC ISO-8601 with ms and Z suffix
        assert rec["ts"].endswith("Z")
        assert "T" in rec["ts"]

    def test_extra_fields_become_top_level(self, capture):
        log, lines = capture
        log.info("msg", extra={"tool": "shell", "duration_s": 1.5})
        rec = _last(lines)
        assert rec["tool"] == "shell"
        assert rec["duration_s"] == 1.5

    def test_event_record_has_no_msg(self, capture):
        log, lines = capture
        log_event(log, logging.INFO, "turn_end", status="complete", iterations=3)
        rec = _last(lines)
        assert rec["event"] == "turn_end"
        assert rec["status"] == "complete"
        assert rec["iterations"] == 3
        assert "msg" not in rec

    def test_exception_captured(self, capture):
        log, lines = capture
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
        rec = _last(lines)
        assert rec["level"] == "ERROR"
        assert "ValueError: boom" in rec["exc"]

    def test_non_serialisable_extra_does_not_crash(self, capture):
        log, lines = capture
        log.info("msg", extra={"path": object()})
        rec = _last(lines)  # must parse — default=str handles it
        assert "path" in rec


class TestContextFilter:
    def test_bound_context_appears_on_records(self, capture):
        log, lines = capture
        bind(session="abc123", turn=2)
        log.debug("anything")
        rec = _last(lines)
        assert rec["session"] == "abc123"
        assert rec["turn"] == 2

    def test_bind_none_removes_key(self, capture):
        log, lines = capture
        bind(session="abc", iteration=1)
        bind(iteration=None)
        log.info("x")
        rec = _last(lines)
        assert rec["session"] == "abc"
        assert "iteration" not in rec

    def test_clear_removes_all(self, capture):
        log, lines = capture
        bind(session="abc", turn=1)
        clear()
        log.info("x")
        rec = _last(lines)
        assert "session" not in rec
        assert "turn" not in rec

    def test_explicit_extra_wins_over_context(self, capture):
        log, lines = capture
        bind(turn=1)
        log.info("x", extra={"turn": 99})
        assert _last(lines)["turn"] == 99

    def test_no_context_means_no_fields(self, capture):
        log, lines = capture
        log.info("x")
        rec = _last(lines)
        assert "session" not in rec
