"""Synthetic Claude Code telemetry, for testing the pipeline without waiting
for hours of real usage.

Every event/metric/span emitted here mirrors the shape documented at
https://code.claude.com/docs/en/monitoring-usage. It is sent over OTLP/HTTP
JSON to the local collector, so it exercises exactly the same path as real
telemetry.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.request
import uuid

from . import config


def _s(v):
    return {"stringValue": str(v)}


def _i(v):
    return {"intValue": str(int(v))}


def _d(v):
    return {"doubleValue": float(v)}


def _kv(d: dict) -> list:
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": _i(v)})
        elif isinstance(v, float):
            out.append({"key": k, "value": _d(v)})
        elif isinstance(v, (list, tuple)):
            out.append({"key": k, "value": {"arrayValue": {"values": [_s(x) for x in v]}}})
        else:
            out.append({"key": k, "value": _s(v)})
    return out


def _post(path: str, body: dict, host: str, port: int) -> int:
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


class SyntheticSession:
    """Builds one plausible Claude Code session."""

    def __init__(self, workspace: str, session_id: str | None = None,
                 start: _dt.datetime | None = None, model: str = "claude-opus-5"):
        self.session_id = session_id or str(uuid.uuid4())
        self.workspace = workspace
        self.model = model
        self.t = start or (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30))
        self.seq = 0
        self.prompt_id = str(uuid.uuid4())
        self.logs: list = []
        self.metrics: list = []
        self.spans: list = []

    # -- helpers -------------------------------------------------------------

    def _tick(self, seconds: float = 3.0) -> int:
        self.t += _dt.timedelta(seconds=seconds)
        return int(self.t.timestamp() * 1e9)

    def _std(self) -> dict:
        return {
            "session.id": self.session_id,
            "user.id": "synthetic-user-0001",
            "app.version": "2.1.237",
            "terminal.type": "iTerm.app",
            "organization.id": "synthetic-org",
            "workspace.host_paths": [self.workspace],
        }

    def event(self, name: str, attrs: dict, tick: float = 3.0) -> None:
        self.seq += 1
        ts = self._tick(tick)
        body = {
            "event.name": name,
            "event.timestamp": _dt.datetime.fromtimestamp(ts / 1e9, tz=_dt.timezone.utc).isoformat(),
            "event.sequence": self.seq,
            "prompt.id": self.prompt_id,
            **self._std(),
            **attrs,
        }
        self.logs.append({
            "timeUnixNano": str(ts),
            "body": _s(f"claude_code.{name}"),
            "attributes": _kv(body),
        })

    def metric(self, name: str, value, unit: str = "", attrs: dict | None = None) -> None:
        ts = self._tick(1.0)
        self.metrics.append({
            "name": name,
            "unit": unit,
            "sum": {
                "aggregationTemporality": 1,
                "isMonotonic": True,
                "dataPoints": [{
                    "timeUnixNano": str(ts),
                    "asDouble" if isinstance(value, float) else "asInt":
                        float(value) if isinstance(value, float) else str(int(value)),
                    "attributes": _kv({**self._std(), **(attrs or {})}),
                }],
            },
        })

    def span(self, name: str, attrs: dict, duration_ms: float = 250.0) -> None:
        start = self._tick(0.5)
        end = start + int(duration_ms * 1e6)
        self.spans.append({
            "traceId": uuid.uuid4().hex,
            "spanId": uuid.uuid4().hex[:16],
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": _kv({**self._std(), **attrs}),
            "status": {"code": 1},
        })

    # -- scripted activity ---------------------------------------------------

    def api_call(self, inp=12000, out=800, cache_read=40000, cache_write=2000,
                 cost=0.0412, duration=4200, **extra) -> None:
        self.event("api_request", {
            "model": self.model, "cost_usd": cost,
            "cost_usd_micros": int(cost * 1_000_000),
            "duration_ms": duration, "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cache_read, "cache_creation_tokens": cache_write,
            "request_id": "req_" + uuid.uuid4().hex[:16],
            "client_request_id": str(uuid.uuid4()),
            "speed": "normal", "query_source": extra.pop("query_source", "main"),
            "effort": "high", **extra,
        })
        self.metric("claude_code.token.usage", inp, "tokens", {"type": "input", "model": self.model})
        self.metric("claude_code.token.usage", out, "tokens", {"type": "output", "model": self.model})
        self.metric("claude_code.token.usage", cache_read, "tokens", {"type": "cacheRead", "model": self.model})
        self.metric("claude_code.token.usage", cache_write, "tokens", {"type": "cacheCreation", "model": self.model})
        self.metric("claude_code.cost.usage", float(cost), "USD", {"model": self.model})

    def tool(self, tool_name: str, params: dict, success: bool = True,
             duration: float = 180.0, decision_source: str = "config",
             error_type: str | None = None, emit_span: bool = True) -> str:
        tuid = "toolu_" + uuid.uuid4().hex[:20]
        pj = json.dumps(params)
        self.event("tool_decision", {
            "tool_name": tool_name, "tool_use_id": tuid, "decision": "accept",
            "tool_source": "mcp" if tool_name.startswith("mcp__") else "builtin",
            "source": decision_source, "tool_parameters": pj,
        }, tick=1.0)
        self.event("tool_result", {
            "tool_name": tool_name, "tool_use_id": tuid,
            "success": "true" if success else "false",
            "duration_ms": duration, "decision_type": "accept",
            "decision_source": decision_source,
            "tool_input_size_bytes": len(pj),
            "tool_result_size_bytes": 2048 if success else 120,
            "error_type": error_type,
            "tool_parameters": pj, "tool_input": pj,
        }, tick=1.0)
        if emit_span:
            span_attrs = {"tool_name": tool_name, "tool_use_id": tuid,
                          "duration_ms": duration, "success": success}
            if "file_path" in params:
                span_attrs["file_path"] = params["file_path"]
            if "command" in params:
                span_attrs["full_command"] = params["command"]
            if "skill_name" in params:
                span_attrs["skill_name"] = params["skill_name"]
            if "subagent_type" in params:
                span_attrs["subagent_type"] = params["subagent_type"]
            self.span("claude_code.tool", span_attrs, duration)
        return tuid

    def payload_logs(self) -> dict:
        return {"resourceLogs": [{
            "resource": {"attributes": _kv({"service.name": "claude-code"})},
            "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events"},
                           "logRecords": self.logs}]}]}

    def payload_metrics(self) -> dict:
        return {"resourceMetrics": [{
            "resource": {"attributes": _kv({"service.name": "claude-code"})},
            "scopeMetrics": [{"scope": {"name": "com.anthropic.claude_code"},
                              "metrics": self.metrics}]}]}

    def payload_traces(self) -> dict:
        return {"resourceSpans": [{
            "resource": {"attributes": _kv({"service.name": "claude-code"})},
            "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code"},
                            "spans": self.spans}]}]}

    def send(self, host: str = "localhost", port: int | None = None) -> dict:
        port = port or config.OTLP_HTTP_PORT
        out = {}
        if self.logs:
            out["logs"] = _post("/v1/logs", self.payload_logs(), host, port)
        if self.metrics:
            out["metrics"] = _post("/v1/metrics", self.payload_metrics(), host, port)
        if self.spans:
            out["traces"] = _post("/v1/traces", self.payload_traces(), host, port)
        return out


def build_demo_session(workspace: str, model: str = "claude-opus-5",
                       start: _dt.datetime | None = None) -> SyntheticSession:
    """A session that exercises every normalized table."""
    s = SyntheticSession(workspace, start=start, model=model)
    s.metric("claude_code.session.count", 1, "", {"start_type": "fresh"})
    s.event("user_prompt", {"prompt_length": 148, "prompt": "<REDACTED>",
                            "message.uuid": str(uuid.uuid4()),
                            "command_source": "builtin"})
    s.api_call()
    s.tool("Read", {"file_path": f"{workspace}/README.md", "limit": 200})
    s.tool("Grep", {"pattern": "def main", "glob": "*.py"})
    s.tool("Bash", {"command": "pytest -q tests/", "description": "Run the test suite"},
           duration=8200.0)
    s.tool("Bash", {"command": "export API_TOKEN=sk-ant-secret1234567890abcd && deploy.sh",
                    "description": "deploy"}, success=False, error_type="ShellError")
    s.api_call(inp=15000, out=1200, cost=0.0611)
    s.tool("Edit", {"file_path": f"{workspace}/telemetry/ingest.py",
                    "old_string": "SECRET CODE", "new_string": "MORE SECRET CODE"})
    s.tool("Write", {"file_path": f"{workspace}/docs/new-file.md",
                     "content": "file contents that must never be stored"})
    s.tool("Skill", {"skill_name": "code-review", "args": "--fix"}, duration=1200.0)
    s.tool("Task", {"subagent_type": "Explore", "description": "find usages"},
           duration=45000.0)
    s.tool("mcp__railway__list_projects", {"mcp_server_name": "railway",
                                           "mcp_tool_name": "list_projects"})
    s.event("assistant_response", {"response_length": 920, "response": "<REDACTED>",
                                   "model": s.model, "request_id": "req_abc",
                                   "query_source": "main"})
    s.api_call(inp=9000, out=400, cost=0.0189, query_source="subagent",
               **{"agent.name": "Explore"})
    s.event("api_error", {"model": s.model, "error": "overloaded_error",
                          "status_code": "529", "duration_ms": 1200, "attempt": 2})
    s.event("internal_error", {"error_name": "TypeError", "error_code": "ENOENT"})
    s.event("permission_mode_changed", {"from_mode": "default", "to_mode": "acceptEdits",
                                        "trigger": "shift_tab"})
    s.event("mcp_server_connection", {"status": "connected", "transport_type": "stdio",
                                      "server_scope": "user", "duration_ms": 340,
                                      "is_plugin": "false"})
    s.metric("claude_code.lines_of_code.count", 42, "", {"type": "added", "model": s.model})
    s.metric("claude_code.lines_of_code.count", 8, "", {"type": "removed", "model": s.model})
    s.metric("claude_code.commit.count", 1, "")
    s.metric("claude_code.active_time.total", 780.0, "s", {"type": "user"})
    return s
