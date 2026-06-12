"""Tests for the self_debug tool."""

import json

import pytest

from archie.tools.self_debug import make_self_debug_spec


def _record(level="INFO", event=None, session="sess-1", **fields) -> str:
    rec = {"ts": "2026-01-01T12:00:00.000Z", "level": level, "logger": "archie.test"}
    if event:
        rec["event"] = event
    if session:
        rec["session"] = session
    rec.update(fields)
    return json.dumps(rec)


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "nextgen.log"
    lines = [
        _record(event="startup", session=None),
        _record(event="turn_start", turn=1),
        _record(event="request_end", turn=1, duration_s=2.5, cache_read=1000),
        _record(level="WARNING", event="tool_end", turn=1, name="shell", status="error"),
        _record(event="turn_end", turn=1, status="complete"),
        _record(event="turn_start", session="sess-2", turn=1),
        "not json at all {{{",
        _record(level="ERROR", event="turn_end", session="sess-2", status="error"),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def spec(log_file):
    return make_self_debug_spec(log_file, lambda: "sess-1")


class TestSelfDebug:
    def test_defaults_to_current_session(self, spec):
        out = spec.handler({})
        assert "sess-2" not in out
        assert "turn_start" in out
        # startup has no session field — excluded under current-session filter
        assert "startup" not in out

    def test_session_all_includes_everything(self, spec):
        out = spec.handler({"session": "all"})
        assert "sess-2" in out
        assert "startup" in out

    def test_event_filter(self, spec):
        out = spec.handler({"event": "request_end"})
        lines = out.split("\n")[1:]
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "request_end"

    def test_level_filter(self, spec):
        out = spec.handler({"level": "WARNING"})
        lines = out.split("\n")[1:]
        recs = [json.loads(line) for line in lines]
        assert all(r["level"] in ("WARNING", "ERROR", "CRITICAL") for r in recs)
        assert len(recs) == 1  # sess-1 only

    def test_pattern_filter(self, spec):
        out = spec.handler({"pattern": "cache_read"})
        lines = out.split("\n")[1:]
        assert len(lines) == 1
        assert "request_end" in lines[0]

    def test_invalid_pattern_is_error(self, spec):
        out = spec.handler({"pattern": "[unclosed"})
        assert out.startswith("Error:")

    def test_invalid_level_is_error(self, spec):
        out = spec.handler({"level": "BANANAS"})
        assert out.startswith("Error:")

    def test_malformed_lines_skipped_and_reported(self, spec):
        out = spec.handler({"session": "all"})
        assert "1 malformed lines skipped" in out

    def test_tail_limits_count(self, spec):
        out = spec.handler({"tail": 2})
        lines = out.split("\n")[1:]
        assert len(lines) == 2
        # Newest last: final record should be sess-1's turn_end
        assert json.loads(lines[-1])["event"] == "turn_end"

    def test_missing_log_file(self, tmp_path):
        spec = make_self_debug_spec(tmp_path / "nope.log", lambda: "sess-1")
        out = spec.handler({})
        assert out.startswith("Error:")

    def test_no_matches(self, spec):
        out = spec.handler({"event": "nonexistent_event"})
        assert "No matching log records" in out

    def test_output_budget_drops_oldest(self, tmp_path):
        path = tmp_path / "big.log"
        lines = [_record(event="tool_end", turn=i, padding="x" * 500) for i in range(100)]
        path.write_text("\n".join(lines) + "\n")
        spec = make_self_debug_spec(path, lambda: "sess-1")
        out = spec.handler({"tail": 100})
        assert len(out) <= 9000  # budget + header slack
        assert "dropped to fit output budget" in out
        # Newest records survive
        assert json.loads(out.split("\n")[-1])["turn"] == 99

    def test_session_id_fn_is_live(self, log_file):
        current = ["sess-1"]
        spec = make_self_debug_spec(log_file, lambda: current[0])
        current[0] = "sess-2"
        out = spec.handler({})
        assert "sess-2" in out

    def test_is_self_truncating(self, spec):
        assert spec.self_truncating is True
