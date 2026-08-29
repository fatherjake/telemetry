/** The parsing that nothing else checks.
 *
 * Shell word splitting, path normalisation and the one JSON case the standard
 * library refuses outright - each produced wrong rows before it was found.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { split } from '../dist/util/shlex.js'
import { parse, baseDir } from '../dist/shellfiles.js'

// loadIgnores reads TELEMETRY_HOME, so point it somewhere disposable first.
const home = mkdtempSync(join(tmpdir(), 'telemetry-test-'))
process.env.TELEMETRY_HOME = home
import { purePath, dumps, nsToIsoString } from '../dist/util/text.js'

const { loadIgnores, ignoreFilter } = await import('../dist/config.js')

test('shlex splits the way posix shlex does', () => {
  // A closing quote followed by more content: the case that broke the parser.
  assert.deepEqual(split('grep -rn --include="*.ts" "needle" src'), [
    'grep',
    '-rn',
    '--include=*.ts',
    'needle',
    'src',
  ])
  assert.deepEqual(split('echo "=== def ==="'), ['echo', '=== def ==='])
  // Inside double quotes a backslash is kept unless it escapes " or itself.
  assert.deepEqual(split('grep -v "^.*ledger\\.db"'), [
    'grep',
    '-v',
    '^.*ledger\\.db',
  ])
  assert.deepEqual(split("a'b'c d"), ['abc', 'd'])
  assert.deepEqual(split('python3 -c "print(1)" file.txt'), [
    'python3',
    '-c',
    'print(1)',
    'file.txt',
  ])
  assert.throws(() => split('echo "unterminated'), /No closing quotation/)
})

test('a quoted pipe is not a command separator', () => {
  const found = parse('grep -rn "icon\\|logo" docs/notes.md')
  assert.ok(
    found.some(([p]) => p === 'docs/notes.md'),
    `expected docs/notes.md, got ${JSON.stringify(found)}`,
  )
})

test('redirects, deletes and in-place edits are read correctly', () => {
  const ops = Object.fromEntries(
    parse("cat a.txt > b.txt && rm c.txt && sed -i '' d.txt").map(([p, op]) => [
      p,
      op,
    ]),
  )
  assert.equal(ops['a.txt'], 'read')
  assert.equal(ops['b.txt'], 'write')
  assert.equal(ops['c.txt'], 'delete')
  assert.equal(ops['d.txt'], 'write')
})

test('a recursive grep over a directory is a search, not a read', () => {
  assert.deepEqual(parse('grep -rn pattern src'), [['src', 'search', 'medium']])
})

test('interpreter code is not mistaken for a path', () => {
  assert.deepEqual(parse('python3 -c "import os; os.getcwd()"'), [])
})

test('a leading cd gives relative paths their root', () => {
  assert.equal(baseDir('cd /a/b && ls'), '/a/b')
  assert.equal(baseDir('ls'), null)
})

test('purePath collapses without resolving', () => {
  // `.` and duplicate slashes collapse, a trailing slash goes, `..` stays -
  // resolving it would invent a path through a directory that may not exist.
  assert.equal(purePath('/a/b/'), '/a/b')
  assert.equal(purePath('/a//b/./c'), '/a/b/c')
  assert.equal(purePath('/a/b/../c'), '/a/b/../c')
  assert.equal(purePath('/'), '/')
})

test('an ignore glob crosses slashes and sees dotfiles', () => {
  // Both are why `path.matchesGlob` cannot do this job: its `*` stops at a
  // `/`, and `**` never matches a segment starting with a dot - which would
  // quietly put node_modules/.bin back into file activity.
  const ignored = ignoreFilter()
  assert.ok(ignored('/tmp/a/b/c.txt'))
  assert.ok(ignored('/repo/node_modules/x/y.js'))
  assert.ok(ignored('/repo/node_modules/.bin/tsc'))
  assert.ok(ignored('/tmp/.hidden/f'))
  assert.ok(ignored('/repo/a/b/.DS_Store'))
  assert.ok(!ignored('/repo/src/x.js'))
})

test('a saved ignore file is read the same way', () => {
  writeFileSync(join(home, 'ignore'), '/tmp/*\n*/vendor/*\n# comment\n')
  assert.deepEqual(loadIgnores(), ['/tmp/*', '*/vendor/*'])
  assert.ok(ignoreFilter()('/repo/vendor/.cache/x'))
})

test('a nanosecond timestamp survives being written back out', () => {
  // SQLite hands these back as BigInt because they do not fit in a double;
  // JSON.stringify refuses them outright, which took out telemetry_sql.
  const ns = 1787220255355685120n
  assert.equal(dumps({ ts_ns: ns }), '{"ts_ns":1787220255355685120}')
  assert.equal(JSON.parse(dumps({ ts_ns: ns })).ts_ns, 1787220255355685000)
})

test('a nanosecond timestamp keeps its microseconds', () => {
  // toISOString() stops at milliseconds; the database column does not.
  assert.equal(
    nsToIsoString(1787220255355685120n),
    '2026-08-20T10:04:15.355685Z',
  )
  assert.equal(nsToIsoString(0n), '1970-01-01T00:00:00.000000Z')
})

test('an expanded tilde path loses its trailing slash', () => {
  // `~/.claude/skills/` has to lose its trailing slash, or the same directory
  // is recorded under two paths.
  assert.equal(purePath('/home/me/.claude/skills/'), '/home/me/.claude/skills')
})
