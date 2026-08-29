/** Small string, number and JSON helpers the Node standard library has no
 * direct equivalent for: stripping a character set from the ends of a string,
 * microsecond ISO timestamps, and comma-grouped number formatting.
 */

/** Remove any of `chars` from both ends. `String.trim` only does whitespace. */
export function strip(s: string, chars: string): string {
  let i = 0
  let end = s.length
  while (i < end && chars.includes(s[i]!)) i++
  while (end > i && chars.includes(s[end - 1]!)) end--
  return s.slice(i, end)
}

export function rstrip(s: string, chars: string): string {
  let end = s.length
  while (end > 0 && chars.includes(s[end - 1]!)) end--
  return s.slice(0, end)
}

/** Nanosecond epoch to an ISO-8601 UTC timestamp with microseconds.
 *
 * `toISOString()` stops at milliseconds; the raw archive and the database both
 * carry microseconds, so the last three digits are appended from the BigInt
 * rather than rounded away.
 */
export function nsToIsoString(ns: bigint): string {
  const us = ns / 1000n
  const millis = new Date(Number(us / 1000n)).toISOString()
  return millis.slice(0, -1) + String(us % 1000n).padStart(3, '0') + 'Z'
}

/** Now, to the second, with a Z. */
export function utcNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** Git reports author dates in local time with an offset; session timestamps
 * are UTC with a Z. Comparing the two as strings silently breaks commit-to-
 * session attribution, so normalise to UTC. */
export function toUtcIso(stamp: string | null | undefined): string | null {
  if (!stamp) return null
  const trimmed = String(stamp).trim()
  const ms = Date.parse(trimmed)
  if (Number.isNaN(ms)) return trimmed
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** `1234567` -> `1,234,567` */
export function group(n: number | bigint): string {
  return Number(n).toLocaleString('en-US')
}

/** `1234.5` -> `1,234.50` */
export function groupFixed(n: number, places: number): string {
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: places,
    maximumFractionDigits: places,
  })
}

export function humanBytes(n: number): string {
  const units = ['B', 'KB', 'MB', 'GB']
  let value = n
  for (const unit of units) {
    if (value < 1024 || unit === 'GB') {
      return unit === 'B'
        ? `${value.toFixed(0)} B`
        : `${value.toFixed(1)} ${unit}`
    }
    value /= 1024
  }
  return `${value} B`
}

/** Collapse repeated slashes and drop `.` segments and any trailing slash, but
 * leave `..` alone - it may point through a directory that no longer exists,
 * and resolving it would invent a path. `path.posix.normalize` does neither, so
 * this stays hand-rolled. */
export function purePath(p: string): string {
  const parts = p.split('/').filter((s) => s !== '' && s !== '.')
  if (p.startsWith('/')) return '/' + parts.join('/')
  return parts.join('/') || '.'
}

/** JSON with keys sorted at every level, because this string is hashed into a
 * dedupe key. A replacer array applies to every object in the tree and fixes
 * the key order; arrays are unaffected by it. */
export function stableStringify(value: unknown): string {
  const keys = new Set<string>()
  JSON.stringify(value, (k, v) => (keys.add(k), v))
  return JSON.stringify(value, [...keys].sort())
}

/** `JSON.rawJSON` is in Node 22 but not yet in the TypeScript lib types. */
const rawJSON = (JSON as unknown as { rawJSON(text: string): unknown }).rawJSON

/** `JSON.stringify`, except that a BigInt is written as the integer it is
 * rather than throwing. SQLite hands back nanosecond timestamps as BigInt, and
 * refusing them outright used to take out `telemetry_sql`. */
export function dumps(value: unknown): string {
  return JSON.stringify(value ?? null, (_k, v) =>
    typeof v === 'bigint' ? rawJSON(String(v)) : v,
  )
}
