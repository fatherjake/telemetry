# What this project stores on your machine

Everything stays on this machine. The receiver writes only local files, binds
to `127.0.0.1`, and **no component in this repository makes an outbound network
request** — there is no model call anywhere in the pipeline, no analytics
service, no telemetry about the telemetry, no phone-home.

Two stores exist, with deliberately different rules.

---

## 1. `~/.telemetry/raw/*.jsonl` — the append-only archive

Every OTLP record Claude Code sends, written verbatim. This is the source of
truth so the schema can be reinterpreted later without losing history.

**Verbatim means verbatim.** Whatever Claude Code chose to export is here,
unfiltered. What Claude Code chooses to export is controlled entirely by the
`OTEL_LOG_*` variables that `telemetry install` writes into
`~/.claude/settings.json`:

| Setting                        | Default | What reaches `~/.telemetry/raw/`                             |
| ------------------------------ | ------- | ------------------------------------------------------------ |
| `OTEL_LOG_USER_PROMPTS`        | **off** | prompts appear as `<REDACTED>`; only `prompt_length` is real |
| `OTEL_LOG_ASSISTANT_RESPONSES` | **off** | responses appear as `<REDACTED>`                             |
| `OTEL_LOG_RAW_API_BODIES`      | **off** | no API bodies at all                                         |
| `OTEL_LOG_TOOL_CONTENT`        | **off** | no tool input/output bodies in spans                         |
| `OTEL_LOG_TOOL_DETAILS`        | **ON**  | see below                                                    |

### The one thing to be aware of

`OTEL_LOG_TOOL_DETAILS=1` is on by default because without it Claude Code
redacts tool parameters, and you lose **every file path, every bash command,
every skill name and every subagent type** — most of what this project exists
to measure.

With it on, `tool_input` reaches the raw files. For `Write` and `Edit` that
object contains `content` / `new_string` / `old_string`, so **short excerpts of
file content can land in `~/.telemetry/raw/`**. Claude Code truncates individual values
at 512 characters and the whole payload at roughly 4 KB, so these are excerpts,
never whole files — but they are real content.

If that is not acceptable:

remove `OTEL_LOG_TOOL_DETAILS` from the `env` block in
`~/.claude/settings.json`. You then lose file paths, bash commands, skills and
subagent types. Sessions, models, tokens, cost, tool names, counts, durations
and errors all still work.

`~/.telemetry/raw/` sits outside any repository, so it cannot be committed by
accident. It is plain text — read it, grep it, delete files
from it. Deleting a raw file does not corrupt the database; the ingest cursor
is keyed by filename, so removed files are simply never re-read.

---

## 1a. Changing the posture

Two independent switches control content, and **both** must be on for content
to be stored:

1. **Claude Code must export it** — the `OTEL_LOG_*` variables, written to
   `~/.claude/settings.json` by `telemetry install` (metadata only) or
   `telemetry install --full` (everything).
2. **Telemetry must agree to store it** — a policy file at
   `~/.telemetry/policy.json`, managed with:

```bash
telemetry config privacy                  # show the current posture
telemetry config privacy --enable-all     # store everything Claude Code exports
telemetry config privacy --disable-all    # back to metadata only
telemetry config privacy --store-content on --store-api-bodies off   # individually
```

The policy lives in a file rather than environment variables so it applies to
every `telemetry` invocation, not just shells that happen to have exported them.
Environment variables (`TELEMETRY_STORE_CONTENT` etc.) still override it.

Full revert:

```bash
telemetry install --yes && telemetry config privacy --disable-all
```

That stops future collection. It does not retroactively scrub `~/.telemetry` —
delete the files for that.

## 2. `~/.telemetry/telemetry.db` — the normalized database

The rules below apply **only while content storage is off**, which is the
default. `telemetry config privacy` shows the live state, as does
`telemetry status`.

### Never stored, in the default posture

- **File contents.** Only paths.
- **Prompt text** and **assistant response text**.
- **API request/response bodies.**
- **Source code**, in any form.
- Anything read from your filesystem beyond git metadata.

### Never stored, in _any_ posture

- **Credentials.** Secret redaction has no off switch in the CLI. Content
  storage is a considered choice; leaking an API key into a database is not.

### What changes when content storage is on

With `telemetry config privacy --enable-all` and `telemetry install
--full`, the database
additionally holds the text of your prompts, Claude's responses, the full
argument object of every tool call — including the contents of files read and
written — and full API request bodies, which contain the entire conversation
context. Expect roughly **70–260 KB per API call** for bodies alone.

Secret redaction still runs over all of it.

### Tool arguments are reduced to a metadata allowlist

Before any tool argument is written, `src/redact.ts` keeps only keys that are
metadata by nature — `file_path`, `path`, `notebook_path`, `command`,
`full_command`, `pattern`, `glob`, `url`, `skill_name`, `subagent_type`,
`mcp_server_name`, `mcp_tool_name`, `description`, `offset`, `limit`, and a
handful more. Everything else is dropped, and the dropped key names are
recorded in `tool_calls.dropped_param_keys` so you can see what was discarded
without seeing what was in it.

`content`, `new_string`, `old_string`, `edits`, `prompt`, `text` and `body` are
on an explicit denylist and are never in the allowlist. Nested objects and
arrays are replaced with a type-and-length marker rather than kept.

Set `TELEMETRY_STORE_TOOL_CONTENT=1` to keep the full argument object instead.
It is off by default and `telemetry status` tells you when it is on.

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

## 2b. Nothing leaves this machine

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

## 2c. The MCP server

`telemetry mcp` serves the database to Claude Code as a set of read-only
tools, and `telemetry init` offers to register it. This is now the _only_
route by which anything from this database can reach a network, and it is one
you drive: it happens when you ask a question. Two things follow:

- **A Claude Code session can read your telemetry database.** Anything the tools return —
  session prompts, file paths, command lines, costs — becomes part of that
  session's context, and therefore goes to Anthropic like the rest of that
  conversation. The default posture keeps prompt _text_ out of the database
  entirely, so what a tool can hand over is bounded by what you chose to store.
- **It cannot write.** The connection is opened read-only at the SQLite level,
  so neither a bug nor a crafted `telemetry_sql` call can alter or delete a
  database.

It is registered per user, not per project, so every Claude Code session on
this machine can see it. Remove it with `telemetry config uninstall`, or
`claude mcp remove telemetry --scope user`.

## 3. Deleting things

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

## 4. Checking for yourself

`telemetry status` and `telemetry config privacy` show the live state of
every `TELEMETRY_STORE_*` switch. To audit directly:

```bash
telemetry sql "select tool_name, dropped_param_keys, params_json from tool_calls
              where dropped_param_keys is not null limit 20"

telemetry sql "select count(*) from prompts where prompt_text is not null"   # expect 0

grep -c 'REDACTED' ~/.telemetry/raw/logs.jsonl   # Claude Code's own redaction
```

`telemetry doctor` plants a recognisable secret in a test event and fails if
it reaches the database.
