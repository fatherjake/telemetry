"""Decoding of OTLP/JSON records as written by the collector's file exporter.

The file exporter writes one JSON object per line, each holding a whole
export request (``resourceLogs`` / ``resourceMetrics`` / ``resourceSpans``).
These helpers flatten those into individual records while keeping the
resource and scope context attached.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterator


def any_value(v: Any) -> Any:
    """Convert an OTLP AnyValue into a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return v["doubleValue"]
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return attrs(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attrs(kvs: Any) -> dict:
    """Convert an OTLP KeyValue list into a dict."""
    out: dict = {}
    if not kvs:
        return out
    for kv in kvs:
        if isinstance(kv, dict) and "key" in kv:
            out[kv["key"]] = any_value(kv.get("value"))
    return out


def ns_to_iso(ns: Any) -> str | None:
    """Nanosecond epoch (string or int) to an ISO-8601 UTC timestamp."""
    if ns in (None, "", 0, "0"):
        return None
    try:
        n = int(ns)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return (
        _dt.datetime.fromtimestamp(n / 1e9, tz=_dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def to_ns(ns: Any) -> int | None:
    try:
        n = int(ns)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def iter_logs(payload: dict) -> Iterator[dict]:
    """Yield one dict per log record, with resource/scope context merged in."""
    for rl in payload.get("resourceLogs", []) or []:
        resource = attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs", []) or []:
            scope = (sl.get("scope") or {}).get("name")
            for rec in sl.get("logRecords", []) or []:
                yield {
                    "resource": resource,
                    "scope": scope,
                    "record": rec,
                    "attributes": attrs(rec.get("attributes")),
                    "body": any_value(rec.get("body")),
                    "ts_ns": to_ns(rec.get("timeUnixNano"))
                    or to_ns(rec.get("observedTimeUnixNano")),
                    "trace_id": rec.get("traceId") or None,
                    "span_id": rec.get("spanId") or None,
                    "severity": rec.get("severityText") or None,
                }


_POINT_KINDS = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


def iter_metric_points(payload: dict) -> Iterator[dict]:
    """Yield one dict per metric data point."""
    for rm in payload.get("resourceMetrics", []) or []:
        resource = attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []) or []:
            scope = (sm.get("scope") or {}).get("name")
            for metric in sm.get("metrics", []) or []:
                name = metric.get("name")
                unit = metric.get("unit")
                for kind in _POINT_KINDS:
                    block = metric.get(kind)
                    if not block:
                        continue
                    for dp in block.get("dataPoints", []) or []:
                        if "asInt" in dp:
                            value = float(dp["asInt"])
                        elif "asDouble" in dp:
                            value = float(dp["asDouble"])
                        elif "sum" in dp:  # histogram
                            value = float(dp["sum"])
                        else:
                            value = None
                        yield {
                            "resource": resource,
                            "scope": scope,
                            "metric_name": name,
                            "unit": unit,
                            "kind": kind,
                            "value": value,
                            "attributes": attrs(dp.get("attributes")),
                            "ts_ns": to_ns(dp.get("timeUnixNano"))
                            or to_ns(dp.get("startTimeUnixNano")),
                            "point": dp,
                        }


def iter_spans(payload: dict) -> Iterator[dict]:
    """Yield one dict per span."""
    for rs in payload.get("resourceSpans", []) or []:
        resource = attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or []:
            scope = (ss.get("scope") or {}).get("name")
            for span in ss.get("spans", []) or []:
                start = to_ns(span.get("startTimeUnixNano"))
                end = to_ns(span.get("endTimeUnixNano"))
                yield {
                    "resource": resource,
                    "scope": scope,
                    "span": span,
                    "name": span.get("name"),
                    "trace_id": span.get("traceId"),
                    "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId") or None,
                    "attributes": attrs(span.get("attributes")),
                    "start_ns": start,
                    "end_ns": end,
                    "duration_ms": ((end - start) / 1e6) if (start and end) else None,
                    "status": (span.get("status") or {}).get("code"),
                }


def signal_of(payload: dict) -> str | None:
    if "resourceLogs" in payload:
        return "logs"
    if "resourceMetrics" in payload:
        return "metrics"
    if "resourceSpans" in payload:
        return "traces"
    return None
