# What this project stores on your machine

Everything Claude Code exports, in full, on this machine only — prompts,
responses, tool arguments, file contents that pass through a tool, and raw API
request bodies. There is no metadata-only mode and nothing to opt into.

That is a deliberate trade, and it is worth stating before anything else:
**a database that knows a session cost $20 but not what it was asked to do
cannot answer the questions this tool exists for.** "Which skill should have
fired here", "what was the correction actually about", "did the agent read the
file before rewriting it" are all questions about the words. Storing the
counts and discarding the content makes the thing safe and useless at once.

What is _not_ kept is credentials. Every string is scrubbed on the way in, and
that has no off switch.

The two properties that make this a reasonable trade:

- **Nothing leaves this machine.** No model call, no analytics, no network
  egress of any kind. See section 3.
- **It is all deletable, and nothing is load-bearing.** `rm -rf ~/.telemetry`
  and the tool is back to knowing nothing. See section 5.

Treat `~/.telemetry` exactly as you treat the repositories it is watching.

---

## 1. `~/.telemetry/raw/*.jsonl` — the append-only archive

Every OTLP request Claude Code sends, written verbatim, one JSON object per
line, never rewritten. This is the source of truth: the database is derived
from it and can be rebuilt from it at any time.

Because it is verbatim, it holds whatever Claude Code exported — including
prompt and response text and tool arguments. It is not scrubbed. Scrubbing
happens on the way from here into the database.

It also grows. Budget roughly 70–260 KB per API call with content export on,
and check `telemetry status` for the current size.

---

## 2. `~/.telemetry/telemetry.db` — the normalized database

Derived from the raw archive by `telemetry analyse`. Everything in the archive
is represented, scrubbed of credentials:

| Stored                       | Where                                      |
| ---------------------------- | ------------------------------------------ |
| prompt text, verbatim        | `prompts.prompt_text`, `turns.prompt_text` |
| response text, verbatim      | `responses.response_text`                  |
| every tool argument          | `tool_calls.params_json`                   |
| file paths and bash commands | `file_activity`, `bash_activity`           |
| raw API request bodies       | `events.attrs_json`                        |
| tool output bodies           | `spans.span_events`                        |
| cost, tokens, models, timing | `api_calls`, `costs`                       |

### Never stored

- Anything matching a credential pattern — see below.
- File contents this project went and read itself. It never opens a file. What
  it has is what passed through a Claude Code tool call.
- Anything from a repository Claude Code did not touch.

### Secret redaction

Every string written to the database passes through pattern-based redaction
first — bash commands, error messages, git remote URLs, commit subjects, and
every attribute value. Matched text becomes `[REDACTED]`:

- Anthropic (`sk-ant-…`), OpenAI (`sk-…`), Stripe (`sk_live_…`, `pk_test_…`)
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`)
- AWS access key ids (`AKIA…`, `ASIA…`), Google (`AIza…`), Slack (`xoxb-…`)
- JWTs, PEM private key blocks, `Authorization: Bearer …`
- assignments whose key name implies a secret, anywhere inside a longer
  identifier — `MY_TOKEN=`, `GITHUB_API_KEY=`, `x-auth-token:`, `db_password:`
- credentials embedded in URLs — `https://user:pass@host` keeps the user, drops
  the password

Redaction is deliberately conservative in two places.

It does **not** blanket redact long hex strings, because 40-character git SHAs
are data this project needs. A secret that looks like a bare hex blob with no
surrounding keyword will not be caught.

And a secret-sounding key name is not on its own enough to destroy a value.
Bash commands carry source code, JSON and shell banners, so
`TOKEN_MESSENGER: Address`, `const tokens = INDEXED_TOKENS[chainId]` and
`echo "=====TOKENS====="` all parse as assignments to a secret-named key. The
value has to look like a credential too — letters and digits together, or
simply long — and never a number, a `$VAR` reference, a `<placeholder>`, a
boolean, or anything containing brackets. Checked against 6,927 real rows from
this database: zero content destroyed.

Both trade-offs run the same way on purpose: a wrong redaction is silent and
permanent, and you cannot tell from the database that it happened.

**Redaction is a safety net, not a guarantee.** Treat `~/.telemetry` as
sensitive.

### Identity

Claude Code stamps `user.email`, `user.account_uuid`, `user.account_id`,
`user.id` and `organization.id` on every record, and they are stored. On a
single-user local database that is the point — it is how sessions are attributed.
If you do not want the account UUID in metrics, set
`OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false`. Email and `user.id` cannot be
switched off in Claude Code.

### Local git metadata

Telemetry runs read-only git commands (`rev-parse`, `remote get-url`,
`branch --show-current`, `log`, `show`, `ls-files`) against repositories it
sees referenced in telemetry, and stores repo root, remote URL (credentials
stripped), branch, HEAD, commit hashes, changed **paths**, and insertion and
deletion **counts**.

It never reads a diff's contents, never reads file contents, and never runs a
git command that writes. Commit subjects are stored and are passed through
redaction.

Turn it off with `TELEMETRY_GIT_RECONCILE=0`.

---

## 2a. `~/.telemetry/session_context.jsonl` — hook output

One JSON line per session start and end, written by the optional session hooks:
session id, timestamp, working directory, repo root, remote URL (credentials
stripped), branch, HEAD sha, and whether the tree was dirty. No file contents,
no diffs, no command history. Delete the file at any time; you lose only
project attribution for sessions that had no other path signal.

## 3. Nothing leaves this machine

Earlier versions of this project had one exception to that: six model-backed
passes that sent prompt text, session summaries and document excerpts to
Anthropic through the local `claude` CLI to produce session descriptions, turn
labels, correction causes, skill-audit verdicts and knowledge-base profiles.
Each was opt-in, asked first, and cached what it got back.

**They are gone.** The analysis they did is now done by the agent you are
already talking to, reading the facts over MCP. Collection, normalization and
every query in this repository run entirely locally, and there is no flag that
changes that.

That does not mean nothing is disclosed — see the next section. It means this
project never initiates it.

## 4. The MCP server

`telemetry mcp` serves the database to Claude Code as a set of read-only
tools, and `telemetry init` offers to register it. This is now the _only_
route by which anything from this database can reach a network, and it is one
you drive: it happens when you ask a question. Two things follow:

- **A Claude Code session can read your telemetry database.** Anything the tools return —
  session prompts, file paths, command lines, costs — becomes part of that
  session's context, and therefore goes to Anthropic like the rest of that
  conversation. Since the database holds prompts and responses in full, so
  can a tool result — which is the point, and is worth knowing.
- **It cannot write.** The connection is opened read-only at the SQLite level,
  so neither a bug nor a crafted `telemetry_sql` call can alter or delete a
  database.

It is registered per user, not per project, so every Claude Code session on
this machine can see it. Remove it with `telemetry config uninstall`, or
`claude mcp remove telemetry --scope user`.

## 5. Deleting things

```bash
rm -rf ~/.telemetry            # everything: raw archive, hook context, database
rm ~/.telemetry/telemetry.db*  # database only; rebuild with telemetry analyse
```

`telemetry doctor` removes its own test session when it finishes; `--keep`
leaves it in place.

Nothing in the database was paid for, so deleting it costs only the time to
rebuild. The raw archive can always rebuild it. The reverse is not true.

If you used an older version, `~/.telemetry` may still hold sidecar files —
`turn_labels.jsonl`, `skill_audit.jsonl`, `correction_cause.jsonl`,
`session_diagnosis.jsonl`, `doc_profiles.jsonl`, `doc_gaps.jsonl`,
`narratives.jsonl`. Nothing reads them any more. They are left in place rather
than deleted for you, because they contain model output about your work and
that is yours to keep or remove.

---

## 6. Checking for yourself

`telemetry status` shows what is stored and where it lives. To audit the
scrubbing directly:

```bash
telemetry sql "select params_json from tool_calls
              where params_json like '%REDACTED%' limit 20"

telemetry sql "select count(*) from prompts where prompt_text is not null"   # expect 0

grep -c 'REDACTED' ~/.telemetry/raw/logs.jsonl   # Claude Code's own redaction
```

`telemetry doctor` plants a recognisable secret in a test event and fails if
it reaches the database.
