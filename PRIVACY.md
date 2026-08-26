# What this project stores on your machine

Everything stays on this machine. The collector's only exporters are local
files, its ports bind to `127.0.0.1`, and no component in this repository makes
an outbound network request. There is no analytics service, no telemetry about
the telemetry, no phone-home.

Two stores exist, with deliberately different rules.

---

## 1. `data/raw/*.jsonl` — the append-only archive

Every OTLP record Claude Code sends, written verbatim. This is the source of
truth so the schema can be reinterpreted later without losing history.

**Verbatim means verbatim.** Whatever Claude Code chose to export is here,
unfiltered. What Claude Code chooses to export is controlled entirely by the
`OTEL_LOG_*` variables in `.env.telemetry`:

| Setting | Default in `.env.telemetry` | What reaches `data/raw/` |
|---|---|---|
| `OTEL_LOG_USER_PROMPTS` | **off** | prompts appear as `<REDACTED>`; only `prompt_length` is real |
| `OTEL_LOG_ASSISTANT_RESPONSES` | **off** | responses appear as `<REDACTED>` |
| `OTEL_LOG_RAW_API_BODIES` | **off** | no API bodies at all |
| `OTEL_LOG_TOOL_CONTENT` | **off** | no tool input/output bodies in spans |
| `OTEL_LOG_TOOL_DETAILS` | **ON** | see below |

### The one thing to be aware of

`OTEL_LOG_TOOL_DETAILS=1` is on by default because without it Claude Code
redacts tool parameters, and you lose **every file path, every bash command,
every skill name and every subagent type** — most of what this project exists
to measure.

With it on, `tool_input` reaches the raw files. For `Write` and `Edit` that
object contains `content` / `new_string` / `old_string`, so **short excerpts of
file content can land in `data/raw/`**. Claude Code truncates individual values
at 512 characters and the whole payload at roughly 4 KB, so these are excerpts,
never whole files — but they are real content.

If that is not acceptable:

```bash
./setup.sh --minimal     # regenerates .env.telemetry without OTEL_LOG_TOOL_DETAILS
```

You then lose file paths, bash commands, skills and subagent types. Sessions,
models, tokens, cost, tool names, counts, durations and errors all still work.

`data/raw/` is gitignored. It is plain text — read it, grep it, delete files
from it. Deleting a raw file does not corrupt the database; the ingest cursor
is keyed by filename, so removed files are simply never re-read.

---

## 1a. Changing the posture

Two independent switches control content, and **both** must be on for content
to be stored:

1. **Claude Code must export it** — the `OTEL_LOG_*` variables. Set by
   `./setup.sh` (metadata only), `./setup.sh --minimal` (least), or
   `./setup.sh --full` (everything), then pushed to `~/.claude/settings.json`
   with `./telemetry init`.
2. **Telemetry must agree to store it** — a policy file at
   `data/telemetry.policy.json`, managed with:

```bash
./telemetry config privacy                  # show the current posture
./telemetry config privacy --enable-all     # store everything Claude Code exports
./telemetry config privacy --disable-all    # back to metadata only
./telemetry config privacy --store-content on --store-api-bodies off   # individually
```

The policy lives in a file rather than environment variables so it applies to
every `telemetry` invocation, not just shells that happen to have exported them.
Environment variables (`TELEMETRY_STORE_CONTENT` etc.) still override it.

Full revert:

```bash
./setup.sh && ./telemetry init --yes && ./telemetry config privacy --disable-all
```

That stops future collection. It does not retroactively scrub `data/` — delete
the files for that.

## 2. `data/telemetry.db` — the normalized database

The rules below apply **only while content storage is off**, which is the
default. `./telemetry config privacy` shows the live state, as does the report's Privacy
section and `./telemetry status`.

### Never stored, in the default posture

- **File contents.** Only paths.
- **Prompt text** and **assistant response text**.
- **API request/response bodies.**
- **Source code**, in any form.
- Anything read from your filesystem beyond git metadata.

### Never stored, in *any* posture

- **Credentials.** Secret redaction has no off switch in the CLI. Content
  storage is a considered choice; leaking an API key into a database is not.

### What changes when content storage is on

With `./telemetry config privacy --enable-all` and `./setup.sh --full`, the database
additionally holds the text of your prompts, Claude's responses, the full
argument object of every tool call — including the contents of files read and
written — and full API request bodies, which contain the entire conversation
context. Expect roughly **70–260 KB per API call** for bodies alone.

Secret redaction still runs over all of it.

### Tool arguments are reduced to a metadata allowlist

Before any tool argument is written, `tel/redact.py` keeps only keys that are
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
It is off by default and the report tells you when it is on.

### Secret redaction

Every string written to the database passes through pattern-based redaction
first — bash commands, error messages, git remote URLs, commit subjects, and
every attribute value. Matched text becomes `[REDACTED]`:

- Anthropic (`sk-ant-…`), OpenAI (`sk-…`), Stripe (`sk_live_…`, `pk_test_…`)
- GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`)
- AWS access key ids (`AKIA…`, `ASIA…`), Google (`AIza…`), Slack (`xoxb-…`)
- JWTs, PEM private key blocks, `Authorization: Bearer …`
- `password=`, `secret=`, `token=`, `api_key=` and similar assignments
- credentials embedded in URLs — `https://user:pass@host` keeps the user, drops
  the password

Redaction is deliberately conservative in one place: it does **not** blanket
redact long hex strings, because 40-character git SHAs are data this project
needs. A secret that looks like a bare hex blob with no surrounding keyword
will not be caught.

**Redaction is a safety net, not a guarantee.** Treat `data/` as sensitive.

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

## 2a. `data/session_context.jsonl` — hook output

One JSON line per session start and end, written by the optional session hooks:
session id, timestamp, working directory, repo root, remote URL (credentials
stripped), branch, HEAD sha, and whether the tree was dirty. No file contents,
no diffs, no command history. Delete the file at any time; you lose only
project attribution for sessions that had no other path signal.

## 2b. The one thing that leaves this machine

The `classify` pass sends prompt text to Anthropic, through your existing
`claude` CLI authentication. Nothing else in this project makes an outbound
request.

- It is **opt-in** and never part of `analyse`.
- It prints what it is about to do and **asks before sending**.
- `--dry-run` shows the exact payload and sends nothing.
- Each prompt is truncated to 500 characters, plus 160 characters of the
  previous prompt for context. No file contents, no tool output, no secrets —
  the same redaction that guards the database runs first.
- Harness-injected prompts are skipped entirely.
- Results are cached by prompt hash, so nothing is sent twice.

Three other commands use the same route and the same rules — each opt-in, each
printing what it will send and asking first, each cached so nothing is sent
twice:

| Command | What it sends |
|---|---|
| `analyse` (auto-describe) | the prompts of each finished session, to name it |
| `enrich --what classify` | prompt text, plus the previous prompt for context |
| `enrich --what skills` | a session summary — prompts, tool counts, file paths, commands — plus the descriptions of available skills |
| `enrich --what corrections` | one correcting message, the instruction before it, and the tools/files/commands in between |
| `enrich --what docs` | document paths, titles and excerpts capped at 240 characters |
| `enrich --what diagnose` | file paths, command lines, tool names and the counts around them, plus the session description — no file contents |
| the chat pane in `./telemetry run` | one session's brief — turns with their prompt text, file paths with timestamps, command lines, tools, commits, findings — plus the conversation so far |

**One of these runs on its own.** `analyse` names each session from its
prompts, because a description is only useful if it is already there when you
open the database. It is the only step of the pipeline that leaves this machine
without you asking. It is bounded — a dozen sessions per run, and only
sessions that have gained a prompt turn since they were last named, so a run
with no new activity sends nothing. It is switched off with:

```bash
./telemetry config privacy --auto-describe off
```

That switch is deliberately excluded from `--enable-all` and `--disable-all`,
which are about what is *stored*, not about what is *sent*.

`enrich --report-only` sends **nothing at all** — it prints what previous runs
already cached. The friction signals underneath the diagnosis are computed
locally from the database and are free to look at in the browser.

The `docs` pass deserves particular thought: a knowledge base holds
business-sensitive material that was never typed into Claude Code, so unlike
the other three this is genuinely new disclosure. `--titles-only` sends no
content at all, and `--exclude` drops paths before anything leaves the machine
— use it for anything like a contacts or CRM directory.

The chat pane sends the most of any of them — a whole session's brief,
including the prompt text of every turn. It is also the only one you invoke
per question rather than in bulk. Reading a stored conversation back calls
nothing.

Worth weighing honestly: for everything except `docs`, this material was
already sent to Anthropic when you typed it into Claude Code. Sending it again
is the same data going to the same provider — but it *is* a second
transmission, and if that matters for a given project, don't run the command.
The length heuristic still works without it.

## 2c. The MCP server

`./telemetry mcp` serves the database to Claude Code as a set of read-only tools,
and `./telemetry init` offers to register it. Two things follow from that:

- **A Claude Code session can read your telemetry database.** Anything the tools return —
  session prompts, file paths, command lines, costs — becomes part of that
  session's context, and therefore goes to Anthropic like the rest of that
  conversation. The default posture keeps prompt *text* out of the database
  entirely, so what a tool can hand over is bounded by what you chose to store.
- **It cannot write.** The connection is opened read-only at the SQLite level,
  so neither a bug nor a crafted `telemetry_sql` call can alter or delete a
  database.

It is registered per user, not per project, so every Claude Code session on
this machine can see it. Remove it with `./telemetry config uninstall`, or
`claude mcp remove telemetry --scope user`.

## 3. Deleting things

```bash
rm -rf data/                 # everything: raw archive, hook context, database
rm data/telemetry.db*           # database only; rebuild with ./telemetry analyse
```

`./telemetry doctor` removes its own synthetic test session when it finishes;
`--keep` leaves it in place.

Sidecars outlive the database on purpose — `data/*.jsonl` holds everything a
model was paid to produce, so `rm data/telemetry.db*` costs nothing but time.
Deleting a sidecar is what makes that material actually gone.

The raw archive can always rebuild the database. The reverse is not true.

---

## 4. Checking for yourself

The report's **Privacy posture** section shows the live state of all three
`TELEMETRY_STORE_*` switches and counts how many tool calls had content stripped.
To audit directly:

```bash
./telemetry sql "select tool_name, dropped_param_keys, params_json from tool_calls
              where dropped_param_keys is not null limit 20"

./telemetry sql "select count(*) from prompts where prompt_text is not null"   # expect 0

grep -c 'REDACTED' data/raw/logs.jsonl        # Claude Code's own redaction
```

`./telemetry doctor` includes a privacy assertion that fails if file content or a
recognisable secret from the synthetic fixture reaches the database.
