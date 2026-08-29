/** Redaction is the one thing here with no off switch, so it gets the tests.
 *
 * Every "must not" case below was found in real telemetry, not invented. The
 * key-name pattern is deliberately wide - `MY_TOKEN=`, `GITHUB_API_KEY=`,
 * `x-auth-token:` - and bash commands are full of source code, JSON and echo
 * banners, so the guard against destroying content lives in looksSecret.
 *
 * Run:  npm test
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { scrub, filterToolParams, REDACTED } from '../dist/redact.js'

const redacted = (text) =>
  assert.ok(
    String(scrub(text)).includes(REDACTED),
    `should have been redacted: ${text}`,
  )
const kept = (text) =>
  assert.equal(scrub(text), text, `should have been left alone: ${text}`)

test('known key formats are redacted', () => {
  for (const t of [
    'export KEY=sk-ant-api03-AbCdEf1234567890xyz',
    "curl -H 'Authorization: Bearer sk-proj-AbCdEf1234567890abcdefgh'",
    'git remote add o https://ghp_AbCdEf1234567890AbCdEf12@github.com/a/b',
    'AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE aws s3 ls',
    'GOOGLE=AIzaSyA1234567890abcdefghijklmnopqrstuv',
    'slack xoxb-1234567890-abcdefghij',
    'stripe sk_live_abcdefghijklmnop1234',
    'token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP',
  ]) {
    redacted(t)
  }
})

test('url credentials keep the user', () => {
  const out = scrub('git clone https://alice:hunter2@github.com/org/repo.git')
  assert.ok(out.includes('alice'))
  assert.ok(!out.includes('hunter2'))
  assert.ok(out.includes('github.com/org/repo.git'))
})

test('secret-named keys anywhere in the identifier', () => {
  for (const t of [
    'export MY_TOKEN=8f3kd92mfnw01xzq',
    'export MY_TOKEN_VALUE=8f3kd92mfnw01xzq',
    'GITHUB_API_KEY=abcd1234efgh5678',
    "curl -H 'x-auth-token: 8f3kd92mfnw01xzq'",
    'db_password: hunter2xyz',
    "psql 'password=s3cr3tp4ss' -h db",
  ]) {
    redacted(t)
  }
})

test('a git sha survives', () => {
  // The reason there is no blanket hex rule: this is data we need.
  kept('git show 8a7362e1f4c9b2d3e5a6f7089c1b2d3e4f5a6b7c')
})

test('numbers are not eaten', () => {
  for (const t of [
    'claude --max_tokens=4096',
    '--token-budget=5000',
    'timeout=30.5',
    'output_tokens=3643566',
  ]) {
    kept(t)
  }
})

test('references and placeholders are not eaten', () => {
  for (const t of [
    'export API_KEY=$MY_SECRET_KEY',
    'TOKEN=${GH_TOKEN}',
    'api_key=<your-api-key>',
    'auth_token={{TOKEN}}',
  ]) {
    kept(t)
  }
})

test('booleans are not eaten', () => {
  for (const t of ['store_tool_content=true', 'SECRET_ENABLED=false']) kept(t)
})

test('source code is not eaten', () => {
  // All four found in real bash_activity rows.
  for (const t of [
    'echo "=====TOKENS====="',
    'const tokens = INDEXED_TOKENS[chainId] ?? []',
    'export const TOKEN_MESSENGER: Address = getAddress(x)',
    "jq '{auth_method, token_valid}'",
  ]) {
    kept(t)
  }
})

test('non-strings pass through', () => {
  for (const v of [null, undefined, 42, 3.5, true]) assert.equal(scrub(v), v)
})

test('content keys dropped, paths kept', () => {
  const [keptParams, dropped] = filterToolParams({
    file_path: '/repo/src/app.ts',
    old_string: 'SECRET',
    new_string: 'MORE SECRET',
    limit: 200,
    command: 'echo sk-ant-abcdefgh12345678',
  })
  assert.equal(keptParams.file_path, '/repo/src/app.ts')
  assert.equal(keptParams.limit, 200)
  assert.ok(keptParams.command.includes(REDACTED))
  assert.deepEqual([...dropped].sort(), ['new_string', 'old_string'])
})

test('nested structures become size markers', () => {
  const [keptParams] = filterToolParams({ todos: [{ a: 1 }, { b: 2 }] })
  assert.equal(keptParams.todos, '<list len=2>')
})

test('unknown keys are dropped, not kept', () => {
  const [keptParams, dropped] = filterToolParams({ something_new: 'whatever' })
  assert.deepEqual(keptParams, {})
  assert.deepEqual(dropped, ['something_new'])
})

test('store_content keeps everything but still scrubs', () => {
  const [keptParams, dropped] = filterToolParams(
    { content: 'key sk-ant-abcdefgh12345678' },
    true,
  )
  assert.deepEqual(dropped, [])
  assert.ok(keptParams.content.includes(REDACTED))
})
