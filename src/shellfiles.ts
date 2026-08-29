/** Extract file reads and writes from shell commands.
 *
 * The file tools (Read/Edit/Write) are not how most real work touches files: a
 * `grep` over a docs vault, a `sed -n` to page through a file, or a `cat >` to
 * write one never appear in tool telemetry. Without parsing commands, a
 * database built on tool events reports zero activity for files that were read
 * dozens of times.
 *
 * This is inference, so it is deliberately conservative: it favours precision
 * over recall and labels every row with a confidence, rather than guessing
 * widely and presenting the result as fact.
 */
import { split as shlexSplit } from './util/shlex.js'
import { rstrip, strip } from './util/text.js'

export type Operation = 'read' | 'write' | 'delete' | 'search'
export type Confidence = 'high' | 'medium' | 'low'

/** Programs that read the files named in their arguments. */
const READERS = new Set([
  'cat',
  'head',
  'tail',
  'less',
  'more',
  'bat',
  'wc',
  'nl',
  'od',
  'strings',
  'md5',
  'shasum',
  'sha256sum',
  'file',
  'stat',
  'column',
  'fold',
  'grep',
  'egrep',
  'fgrep',
  'rg',
  'ag',
  'ack',
  'awk',
  'jq',
  'yq',
  'xmllint',
  'diff',
  'cmp',
  'sort',
  'uniq',
  'cut',
  'tr',
  'sed',
  'python3',
  'python',
  'node',
  'source',
  'pdftotext',
  'open',
  'qlmanage',
  'sips',
])
/** Programs whose *last* argument is written, earlier ones read. */
const COPIERS = new Set(['cp', 'mv', 'rsync', 'install', 'ln'])
const WRITERS = new Set(['touch', 'mkdir', 'tee'])
const DELETERS = new Set(['rm', 'unlink', 'rmdir', 'trash'])
/** Programs that enumerate a directory rather than read a file. */
const LISTERS = new Set(['ls', 'find', 'du', 'tree', 'fd'])
const GREPPERS = new Set(['grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack'])
/** Programs whose first positional argument is a pattern or script, not a file. */
const PATTERN_FIRST = new Set([...GREPPERS, 'sed', 'awk', 'jq', 'yq'])

/** Flags that take a value, so the following token is not a path. */
const VALUE_FLAGS = new Set([
  '-e',
  '--include',
  '--exclude',
  '--exclude-dir',
  '-m',
  '--max-count',
  '-A',
  '-B',
  '-C',
  '--color',
  '-o',
  '--output',
  '-t',
  '--type',
  '-name',
  '-iname',
  '-path',
  '-maxdepth',
  '-mindepth',
  '-exec',
  '-d',
  '-s',
  '--since',
  '--until',
  '-n',
])

/** Split on shell separators, but never inside quotes.
 *
 * `grep -rn "icon\|logo" file.md` uses a pipe inside a quoted regex; a naive
 * split on `|` cuts the command in half and loses the filename entirely.
 */
function splitSegments(text: string): string[] {
  const out: string[] = []
  let buf: string[] = []
  let i = 0
  let quote: string | null = null
  while (i < text.length) {
    const ch = text[i]!
    if (quote) {
      buf.push(ch)
      if (ch === quote && (i === 0 || text[i - 1] !== '\\')) quote = null
      i += 1
      continue
    }
    if (ch === "'" || ch === '"') {
      quote = ch
      buf.push(ch)
      i += 1
      continue
    }
    if (ch === '&' && i + 1 < text.length && text[i + 1] === '&') {
      out.push(buf.join(''))
      buf = []
      i += 2
      continue
    }
    if (ch === '|') {
      if (i + 1 < text.length && text[i + 1] === '|') i += 1
      out.push(buf.join(''))
      buf = []
      i += 1
      continue
    }
    if (ch === ';' || ch === '\n') {
      out.push(buf.join(''))
      buf = []
      i += 1
      continue
    }
    buf.push(ch)
    i += 1
  }
  out.push(buf.join(''))
  return out
}

const HEREDOC = /<<-?\s*'?"?([A-Za-z_][A-Za-z0-9_]*)'?"?/g
const PY_WRITE = /(write_text\s*\(|open\s*\([^)]*['"][wa]\+?['"]|\.dump\s*\()/
const PY_READ = /(read_text\s*\(|\.load\s*\(|open\s*\((?![^)]*['"][wa])) ?/
const QUOTED_PATH = /['"]([^'"\n]{2,200}?\.[A-Za-z0-9]{1,8})['"]/g

/** A token is path-like if it has a separator or a file extension, and is not
 * a flag, a URL, a glob-only fragment, or a shell variable. */
const PATHY =
  /^(?!-)(?!https?:\/\/)(?!\$)[^|&;<>]*(\/[^|&;<>]*|\.[A-Za-z0-9]{1,10})$/
const SKIP_TOKENS = new Set([
  '.',
  '..',
  '-',
  '/dev/null',
  '/dev/stdin',
  '/dev/stdout',
])

function isPathy(tok: string): boolean {
  if (!tok || SKIP_TOKENS.has(tok) || tok.startsWith('-')) return false
  if (
    tok.startsWith('$') ||
    tok.startsWith('`') ||
    tok.startsWith('http://') ||
    tok.startsWith('https://')
  ) {
    return false
  }
  if (tok.split('/')[0]!.includes('=')) return false // FOO=bar
  return PATHY.test(tok)
}

/** Interpreters take a program on the command line; that code is not a path. */
const INTERPRETERS = new Set([
  'python',
  'python3',
  'node',
  'bun',
  'deno',
  'sh',
  'bash',
  'zsh',
  'perl',
  'ruby',
  'php',
  'osascript',
])
const CODE_FLAGS = new Set(['-c', '-e', '--eval', '--execute'])

function clean(path: string): string | null {
  let p = strip(path.trim(), '\'"')
  // Shell fragments leak trailing punctuation: `sed -n '20,28p'` and slice
  // syntax from inline code produce tokens like `28:` or `120]`.
  p = rstrip(p, ';')
  p = rstrip(p, ':,)]}\\')
  if (!p || SKIP_TOKENS.has(p)) return null
  if (!plausible(p)) return null
  return p
}

/** Reject tokens that are clearly not filenames.
 *
 * The parser is inference, and a wrong path is worse than a missing one: it
 * invents activity against a file that was never touched.
 */
function plausible(path: string): boolean {
  if (/[()[\]{}<>]/.test(path)) return false
  if (path.includes('\n') || path.length > 400) return false
  const trimmed = rstrip(path, '/')
  const last = trimmed.slice(trimmed.lastIndexOf('/') + 1)
  if (!last) return false
  if (/^[0-9]+$/.test(last) || /^[0-9.,:;-]+$/.test(last)) return false // `70`, `1,60`, `28:`
  if (!/[A-Za-z]/.test(last)) return false // no letters anywhere: not a filename
  return true
}

/** Return [command without heredoc bodies, list of heredoc bodies]. */
function stripHeredocs(command: string): [string, string[]] {
  const bodies: string[] = []
  let out = command
  HEREDOC.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = HEREDOC.exec(command)) !== null) {
    const tag = m[1]!
    const after = command.slice(m.index + m[0].length)
    const end = new RegExp(
      `^${tag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`,
      'm',
    ).exec(after)
    if (end) {
      const body = after.slice(0, end.index)
      bodies.push(body)
      out = out.split(body).join('\n')
    }
  }
  return [out, bodies]
}

const RANK: Record<Confidence, number> = { high: 3, medium: 2, low: 1 }

/** Return [path, operation, confidence] triples for one command line. */
export function parse(
  command: string | null | undefined,
): [string, Operation, Confidence][] {
  if (!command) return []
  const [head, bodies] = stripHeredocs(command)
  const found = new Map<
    string,
    { path: string; op: Operation; conf: Confidence }
  >()

  const add = (rawPath: string, op: Operation, conf: Confidence) => {
    const p = clean(rawPath)
    if (!p) return
    const key = `${p}\u0000${op}`
    const existing = found.get(key)
    if (!existing || RANK[conf] > RANK[existing.conf]) {
      found.set(key, { path: p, op, conf })
    }
  }

  // Python inline scripts and heredoc bodies: look for explicit file IO.
  const inline = ['write_text', 'read_text', 'open(', 'Image.open'].some((k) =>
    head.includes(k),
  )
    ? [head]
    : []
  for (const body of [...bodies, ...inline]) {
    QUOTED_PATH.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = QUOTED_PATH.exec(body)) !== null) {
      const path = m[1]!
      if (PY_WRITE.test(body)) add(path, 'write', 'medium')
      if (PY_READ.test(body)) add(path, 'read', 'medium')
    }
  }

  const REDIRECT = `(?:"([^"]+)"|'([^']+)'|([^\\s|&;<>]+))`
  const writeRe = new RegExp(`(?<![0-9<>])>>?\\s*${REDIRECT}`, 'g')
  const readRe = new RegExp(`(?<![0-9<>])<(?!<)\\s*${REDIRECT}`, 'g')
  const stripRe = new RegExp(`(?<![0-9<>])[<>]{1,2}\\s*${REDIRECT}`, 'g')

  for (let segment of splitSegments(head)) {
    segment = segment.trim()
    if (!segment || segment.startsWith('#')) continue

    // Redirects are the least ambiguous signal there is.
    for (const [re, op] of [
      [writeRe, 'write'],
      [readRe, 'read'],
    ] as [RegExp, Operation][]) {
      re.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = re.exec(segment)) !== null) {
        const target = m[1] || m[2] || m[3]
        if (target) add(target, op, 'high')
      }
    }
    stripRe.lastIndex = 0
    segment = segment.replace(stripRe, ' ')

    let tokens: string[]
    try {
      tokens = shlexSplit(segment)
    } catch {
      tokens = segment.split(/\s+/).filter(Boolean)
    }
    if (!tokens.length) continue

    // Skip wrappers and env assignments to reach the real program.
    const transparent = new Set([
      'sudo',
      'env',
      'time',
      'nohup',
      'exec',
      'command',
      'npx',
      'then',
      'do',
      'else',
    ])
    let i = 0
    while (
      i < tokens.length &&
      (transparent.has(tokens[i]!) ||
        /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i]!))
    ) {
      i += 1
    }
    if (i >= tokens.length) continue
    const prog = tokens[i]!.split('/').pop()!
    const args = tokens.slice(i + 1)

    // `sed -i` edits in place; `sed -n ... p` prints.
    const sedInplace = prog === 'sed' && args.some((a) => a.startsWith('-i'))
    const recursive = args.some(
      (a) =>
        ['-r', '-R', '-rn', '-rl', '-rI', '--recursive'].includes(a) ||
        (a.startsWith('-') &&
          !a.startsWith('--') &&
          a.slice(1).includes('r') &&
          GREPPERS.has(prog)),
    )
    // `find docs`, `ls docs`, `grep -r pat docs` all name a bare directory,
    // which has neither a slash nor an extension. For those programs the
    // positional arguments are targets by definition, so accept them.
    const bareOk = LISTERS.has(prog) || (GREPPERS.has(prog) && recursive)

    const positional: string[] = []
    let skipNext = false
    for (const a of args) {
      if (skipNext) {
        skipNext = false
        continue
      }
      if (VALUE_FLAGS.has(a) || (INTERPRETERS.has(prog) && CODE_FLAGS.has(a))) {
        skipNext = true
        continue
      }
      if (a.startsWith('-') || a === '') continue
      if (isPathy(a) || (bareOk && isBareName(a))) positional.push(a)
    }

    // grep/sed/awk/jq take a pattern or script first; it is not a file.
    let candidates: string[]
    if (
      PATTERN_FIRST.has(prog) &&
      !args.some((a) => a.startsWith('-e') || a.startsWith('-f'))
    ) {
      candidates = positional.slice(1)
    } else {
      candidates = positional
    }
    if (
      PATTERN_FIRST.has(prog) &&
      !candidates.length &&
      positional.length === 1 &&
      looksLikeFile(positional[0]!)
    ) {
      candidates = positional // `grep foo.txt` with no pattern arg
    }

    // A few git subcommands move files around on disk.
    if (prog === 'git' && args.length) {
      const sub = args.find((a) => !a.startsWith('-'))
      const gpaths = args.slice(1).filter((a) => isPathy(a) && a !== sub)
      if (sub === 'mv' && gpaths.length >= 2) {
        add(gpaths[0]!, 'delete', 'high')
        add(gpaths[gpaths.length - 1]!, 'write', 'high')
      } else if (sub === 'rm') {
        for (const path of gpaths) add(path, 'delete', 'high')
      } else if (sub === 'checkout' && args.includes('--')) {
        for (const path of gpaths) add(path, 'write', 'high') // restores the file on disk
      }
      continue
    }

    // `curl -o file` / `wget -O file` write; the flag value is otherwise skipped.
    if (prog === 'curl' || prog === 'wget') {
      for (let j = 0; j < args.length; j++) {
        if (
          ['-o', '-O', '--output'].includes(args[j]!) &&
          j + 1 < args.length
        ) {
          add(args[j + 1]!, 'write', 'high')
        }
      }
    }

    if (DELETERS.has(prog)) {
      for (const path of candidates) add(path, 'delete', 'high')
    } else if (COPIERS.has(prog) && candidates.length >= 2) {
      for (const path of candidates.slice(0, -1)) add(path, 'read', 'high')
      add(candidates[candidates.length - 1]!, 'write', 'high')
    } else if (WRITERS.has(prog)) {
      for (const path of candidates) add(path, 'write', 'high')
    } else if (sedInplace) {
      for (const path of candidates) add(path, 'write', 'high')
    } else if (LISTERS.has(prog)) {
      for (const path of candidates) add(path, 'search', 'medium')
    } else if (READERS.has(prog)) {
      const conf: Confidence = [
        'cat',
        'head',
        'tail',
        'wc',
        'less',
        'more',
        'bat',
        'nl',
      ].includes(prog)
        ? 'high'
        : 'medium'
      for (const path of candidates) {
        // A recursive grep over a directory is a search, not a read of one
        // file - recording it as a read would be a lie about which file was
        // opened.
        const op: Operation =
          (recursive && isBareName(path)) || path.endsWith('/')
            ? 'search'
            : 'read'
        add(path, op, op === 'read' ? conf : 'medium')
      }
    }
  }
  return [...found.values()].map((f) => [f.path, f.op, f.conf])
}

/** A plain directory-ish name: no slash, no extension, no metacharacters. */
function isBareName(tok: string): boolean {
  return /^[A-Za-z0-9_.-]+$/.test(tok) && !tok.includes('.')
}

const CD_PREFIX = /^\s*cd\s+(["']?)(\/[^\s;&|"']+)\1/

/** A leading `cd /abs/path &&` tells us what relative paths are relative to. */
export function baseDir(command: string | null | undefined): string | null {
  if (!command) return null
  const m = CD_PREFIX.exec(command)
  return m ? m[2]! : null
}

/** A lone grep argument is a file only if it really looks like one. */
function looksLikeFile(tok: string): boolean {
  return tok.includes('/') || /\.[A-Za-z0-9]{1,10}$/.test(tok)
}
