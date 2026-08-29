/** Decoding of OTLP/JSON records as written by the receiver.
 *
 * Each line of a raw file is a whole export request (`resourceLogs` /
 * `resourceMetrics` / `resourceSpans`). These helpers flatten those into
 * individual records while keeping the resource and scope context attached.
 */
import { nsToIsoString } from './util/text.js'

export type Attrs = Record<string, unknown>

/** Convert an OTLP AnyValue into a plain value. */
export function anyValue(v: unknown): unknown {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return v
  const obj = v as Record<string, any>
  if ('stringValue' in obj) return obj.stringValue
  if ('intValue' in obj) {
    const parsed = Number.parseInt(String(obj.intValue), 10)
    return Number.isNaN(parsed) ? obj.intValue : parsed
  }
  if ('doubleValue' in obj) {
    const parsed = Number.parseFloat(String(obj.doubleValue))
    return Number.isNaN(parsed) ? obj.doubleValue : parsed
  }
  if ('boolValue' in obj) return Boolean(obj.boolValue)
  if ('arrayValue' in obj) return (obj.arrayValue?.values ?? []).map(anyValue)
  if ('kvlistValue' in obj) return attrs(obj.kvlistValue?.values ?? [])
  if ('bytesValue' in obj) return obj.bytesValue
  return null
}

/** Convert an OTLP KeyValue list into an object. */
export function attrs(kvs: unknown): Attrs {
  const out: Attrs = {}
  if (!kvs || !Array.isArray(kvs)) return out
  for (const kv of kvs) {
    if (kv && typeof kv === 'object' && 'key' in kv) {
      out[(kv as any).key] = anyValue((kv as any).value)
    }
  }
  return out
}

/** Nanosecond epoch (string or number) as an exact integer, or null. */
export function toNs(value: unknown): bigint | null {
  if (value === null || value === undefined || value === '') return null
  try {
    const n = typeof value === 'bigint' ? value : BigInt(String(value).trim())
    return n > 0n ? n : null
  } catch {
    return null
  }
}

/** Nanosecond epoch to an ISO-8601 UTC timestamp. */
export function nsToIso(value: unknown): string | null {
  const n = toNs(value)
  return n === null ? null : nsToIsoString(n)
}

export interface LogRecord {
  resource: Attrs
  scope: string | null
  record: Record<string, any>
  attributes: Attrs
  body: unknown
  ts_ns: bigint | null
  trace_id: string | null
  span_id: string | null
  severity: string | null
}

/** One entry per log record, with resource/scope context merged in. */
export function* iterLogs(payload: Record<string, any>): Generator<LogRecord> {
  for (const rl of payload.resourceLogs ?? []) {
    const resource = attrs(rl?.resource?.attributes)
    for (const sl of rl?.scopeLogs ?? []) {
      const scope = sl?.scope?.name ?? null
      for (const rec of sl?.logRecords ?? []) {
        yield {
          resource,
          scope,
          record: rec,
          attributes: attrs(rec?.attributes),
          body: anyValue(rec?.body),
          ts_ns: toNs(rec?.timeUnixNano) ?? toNs(rec?.observedTimeUnixNano),
          trace_id: rec?.traceId || null,
          span_id: rec?.spanId || null,
          severity: rec?.severityText || null,
        }
      }
    }
  }
}

const POINT_KINDS = [
  'sum',
  'gauge',
  'histogram',
  'exponentialHistogram',
  'summary',
] as const

export interface MetricPoint {
  resource: Attrs
  scope: string | null
  metric_name: string | null
  unit: string | null
  kind: string
  value: number | null
  attributes: Attrs
  ts_ns: bigint | null
  point: Record<string, any>
}

/** One entry per metric data point. */
export function* iterMetricPoints(
  payload: Record<string, any>,
): Generator<MetricPoint> {
  for (const rm of payload.resourceMetrics ?? []) {
    const resource = attrs(rm?.resource?.attributes)
    for (const sm of rm?.scopeMetrics ?? []) {
      const scope = sm?.scope?.name ?? null
      for (const metric of sm?.metrics ?? []) {
        const name = metric?.name ?? null
        const unit = metric?.unit ?? null
        for (const kind of POINT_KINDS) {
          const block = metric?.[kind]
          if (!block) continue
          for (const dp of block.dataPoints ?? []) {
            let value: number | null
            if ('asInt' in dp) value = Number(dp.asInt)
            else if ('asDouble' in dp) value = Number(dp.asDouble)
            else if ('sum' in dp)
              value = Number(dp.sum) // histogram
            else value = null
            yield {
              resource,
              scope,
              metric_name: name,
              unit,
              kind,
              value,
              attributes: attrs(dp?.attributes),
              ts_ns: toNs(dp?.timeUnixNano) ?? toNs(dp?.startTimeUnixNano),
              point: dp,
            }
          }
        }
      }
    }
  }
}

export interface SpanRecord {
  resource: Attrs
  scope: string | null
  span: Record<string, any>
  name: string | null
  trace_id: string | null
  span_id: string | null
  parent_span_id: string | null
  attributes: Attrs
  start_ns: bigint | null
  end_ns: bigint | null
  duration_ms: number | null
  status: unknown
}

/** One entry per span. */
export function* iterSpans(
  payload: Record<string, any>,
): Generator<SpanRecord> {
  for (const rs of payload.resourceSpans ?? []) {
    const resource = attrs(rs?.resource?.attributes)
    for (const ss of rs?.scopeSpans ?? []) {
      const scope = ss?.scope?.name ?? null
      for (const span of ss?.spans ?? []) {
        const start = toNs(span?.startTimeUnixNano)
        const end = toNs(span?.endTimeUnixNano)
        yield {
          resource,
          scope,
          span,
          name: span?.name ?? null,
          trace_id: span?.traceId ?? null,
          span_id: span?.spanId ?? null,
          parent_span_id: span?.parentSpanId || null,
          attributes: attrs(span?.attributes),
          start_ns: start,
          end_ns: end,
          duration_ms:
            start !== null && end !== null ? Number(end - start) / 1e6 : null,
          status: span?.status?.code,
        }
      }
    }
  }
}
