/** Secret redaction and content minimisation.
 *
 * Two independent jobs:
 *
 * 1. `scrub()` removes things that look like credentials from any string we
 *    are about to persist (bash commands, error messages, remote URLs).
 * 2. `filterToolParams()` reduces a tool's argument JSON to a metadata-only
 *    allowlist, so file *paths* are kept but file *contents* never are.
 */
import { createHash } from 'node:crypto'

export const REDACTED = '[REDACTED]'

/** Ordered, targeted patterns. Deliberately conservative: we do not blanket
 * redact long hex strings, because git SHAs are 40 hex characters and are data
 * we actively want to keep. */
const PATTERNS: [string, RegExp][] = [
  ['anthropic_key', /sk-ant-[A-Za-z0-9_-]{8,}/g],
  ['openai_key', /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}/g],
  ['github_token', /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g],
  ['github_pat', /\bgithub_pat_[A-Za-z0-9_]{20,}/g],
  ['aws_access_key', /\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b/g],
  ['google_key', /\bAIza[0-9A-Za-z_-]{30,}/g],
  ['slack_token', /\bxox[baprs]-[A-Za-z0-9-]{10,}/g],
  ['stripe_key', /\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}/g],
  ['jwt', /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g],
  [
    'private_key',
    /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
  ],
  [
    'bearer',
    /\b(bearer|authorization\s*[:=]\s*bearer)\s+[A-Za-z0-9._-]{12,}/gi,
  ],
  // key=value / key: value where the key name implies a secret.
  //
  // The secret word may sit anywhere inside a longer identifier, so
  // `MY_TOKEN=`, `GITHUB_API_KEY=` and `x-auth-token:` all match - an earlier
  // version anchored on \b and caught only a bare `token=`, which is the least
  // common way anyone actually writes it.
  //
  // Matching more key names means matching more values, so which values are
  // worth redacting is decided in keywordSub rather than in the pattern.
  [
    'keyword_assign',
    /(?<![A-Za-z0-9_-])([A-Za-z0-9_-]*(?:api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token|session[_-]?key)[A-Za-z0-9_-]*)(["']?\s*[:=]\s*)["']?([^\s"',;)]{4,})/gi,
  ],
  // credentials embedded in URLs
  [
    'url_credentials',
    /\b([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)([^/\s:@]+):([^/\s@]+)@/g,
  ],
]

/** Values that a secret-named key can hold without being a secret. Widening
 * the key pattern to catch `MY_TOKEN=` also catches `max_tokens=4096`, and
 * eating the numbers out of somebody's command line is its own kind of data
 * loss. */
const NUMERIC = /^[\d_.,:+-]+$/
const PLACEHOLDER = /^[<{[(].*[>}\])]$/
const NOT_SECRET = new Set([
  'true',
  'false',
  'null',
  'none',
  'nil',
  'yes',
  'no',
  'on',
  'off',
  'undefined',
  'auto',
  'default',
])

/** Is this value worth destroying to be safe?
 *
 * The key name alone is not enough. Bash commands carry source code, JSON and
 * echo banners, so `TOKEN_MESSENGER: Address`, `const tokens = X ?? []` and
 * `echo "=====TOKENS====="` all look like assignments to a secret-sounding
 * key. Measured against 4,200 real commands, matching on the key name alone
 * produced four hits, and all four were content rather than credentials.
 *
 * So the value has to look like a credential too. Real secrets are
 * high-entropy: mixed letters and digits, or simply long. Identifiers, words,
 * banners and booleans are none of those.
 */
function looksSecret(value: string): boolean {
  const v = value.trim()
  if (v.length < 8) return false
  // `$TOKEN`, `${TOKEN}`, `%TOKEN%` - a reference to a secret, not one.
  if (v[0] === '$' || v[0] === '%') return false
  // `max_tokens=4096`, `--token-budget=5000`, `timeout=30.5`
  if (NUMERIC.test(v)) return false
  // `<your-api-key>`, `{{TOKEN}}` - documentation, not credentials.
  if (PLACEHOLDER.test(v)) return false
  if (NOT_SECRET.has(v.toLowerCase())) return false
  // Brackets are code, never credentials: `INDEXED_TOKENS[chainId]`,
  // `getToken()`, `{ a: 1 }`. No key format in PATTERNS uses them, and base64
  // and hex do not either.
  if (/[[\](){}<>]/.test(v)) return false
  const hasDigit = /[0-9]/.test(v)
  const hasAlpha = /\p{L}/u.test(v)
  // Letters and digits together is the signature of a generated credential.
  // Failing that, anything past 20 characters on a secret-named key is worth
  // losing - an identifier that long is rare, a passphrase that long is not.
  return (hasDigit && hasAlpha) || v.length >= 20
}

/** Replace credential-looking substrings in `text`. */
export function scrub<T>(text: T): T {
  if (text === null || text === undefined) return text
  if (typeof text !== 'string') return text
  let out: string = text
  for (const [name, pattern] of PATTERNS) {
    pattern.lastIndex = 0
    if (name === 'keyword_assign') {
      out = out.replace(
        pattern,
        (whole, key: string, sep: string, value: string) =>
          looksSecret(value) ? `${key}${sep}${REDACTED}` : whole,
      )
    } else if (name === 'url_credentials') {
      out = out.replace(
        pattern,
        (_whole, scheme: string, user: string) =>
          `${scheme}${user}:${REDACTED}@`,
      )
    } else {
      out = out.replace(pattern, REDACTED)
    }
  }
  return out as unknown as T
}

/** Recursively scrub every string in a JSON-ish structure. */
export function scrubDeep(obj: unknown): unknown {
  if (typeof obj === 'string') return scrub(obj)
  if (Array.isArray(obj)) return obj.map(scrubDeep)
  if (obj && typeof obj === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(obj as Record<string, unknown>))
      out[k] = scrubDeep(v)
    return out
  }
  return obj
}

// --- Tool parameter minimisation --------------------------------------------

/** Keys we keep from tool arguments. Everything here is metadata: paths,
 * commands, patterns, identifiers. Content-bearing keys (`content`,
 * `new_string`, `old_string`, `prompt`, `edits`) are absent on purpose. */
export const TOOL_PARAM_ALLOWLIST = new Set([
  'file_path',
  'filePath',
  'path',
  'notebook_path',
  'notebookPath',
  'command',
  'bash_command',
  'full_command',
  'pattern',
  'glob',
  'type',
  'output_mode',
  'head_limit',
  '-n',
  '-i',
  'url',
  'query',
  'domain',
  'allowed_domains',
  'blocked_domains',
  'skill_name',
  'skill',
  'subagent_type',
  'agent_type',
  'agentType',
  'mcp_server_name',
  'mcp_tool_name',
  'server_name',
  'tool_name',
  'description',
  'offset',
  'limit',
  'timeout',
  'run_in_background',
  'replace_all',
  'args',
  'isolation',
  'model',
  'effort',
  'label',
  'phase',
  'shell_id',
  'filter',
  'plan',
  'todos',
])

/** Keys that are known to carry file or message content; always dropped unless
 * TELEMETRY_STORE_TOOL_CONTENT is on, and never included in the allowlist. */
export const CONTENT_KEYS = new Set([
  'content',
  'new_string',
  'old_string',
  'edits',
  'prompt',
  'text',
  'new_source',
  'old_source',
  'body',
  'message',
  'response',
  'file_text',
])

/** Reduce tool arguments to metadata.
 *
 * Returns `[filtered, droppedKeys]`. `args` and `plan`/`todos` are truncated
 * rather than kept whole because they can be long free text.
 */
export function filterToolParams(
  params: unknown,
  storeContent = false,
): [unknown, string[]] {
  const dropped: string[] = []
  if (params === null || params === undefined) return [null, dropped]
  if (typeof params !== 'object' || Array.isArray(params)) {
    return [storeContent ? scrubDeep(params) : null, dropped]
  }

  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (storeContent) {
      out[k] = scrubDeep(v)
      continue
    }
    if (CONTENT_KEYS.has(k) || !TOOL_PARAM_ALLOWLIST.has(k)) {
      dropped.push(k)
      continue
    }
    if (v !== null && typeof v === 'object') {
      // Nested structures may hide content; keep only a size marker.
      const kind = Array.isArray(v) ? 'list' : 'dict'
      const size = Array.isArray(v) ? v.length : Object.keys(v as object).length
      out[k] = `<${kind} len=${size}>`
    } else if (typeof v === 'string' && v.length > 2048) {
      out[k] = scrub(v.slice(0, 2048)) + '…[truncated]'
    } else {
      out[k] = scrubDeep(v)
    }
  }
  return [out, dropped]
}

/** Stable short hash, used to correlate repeated commands without storing them
 * twice. */
export function hashText(text: string | null | undefined): string | null {
  if (text === null || text === undefined) return null
  return createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 16)
}
