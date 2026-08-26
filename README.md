# Claude Telemetry

Capture Claude Code's OpenTelemetry output on your own machine, keep the raw
stream forever, normalize it into SQLite, map every session to the git project
it happened in, and read the result in a terminal browser, a local HTML
report, or from inside Claude Code itself over MCP.

Nothing leaves this machine, with one named exception: `analyse` sends each
finished session's prompts to Anthropic to write the one-line description
it goes by. Turn it off with `./telemetry config privacy --auto-describe off`;
nothing else in the pipeline talks to the network unless you ask it to.

The longer-term aim is an economic ledger that ties AI activity and cost to
projects and eventually to revenue. This first step does the unglamorous half:
**collect real telemetry, learn what is actually in it, attribute it to
repositories, and make it easy to inspect.** No revenue, no valuation, no
attribution modelling yet — the schema just leaves room for them.

---

## What you get

```
Claude Code
    │  OTLP (gRPC 4317 / HTTP 4318)
    ▼
OpenTelemetry Collector               ── docker compose, one container
    │
    ├──▶ data/raw/*.jsonl             ── append-only, verbatim, never rewritten
    │
    ▼
tel/ingest.py                         ── incremental, idempotent normalization
    │
    ▼
data/telemetry.db                     ── SQLite, 21 tables + 3 views
    │
    ├──▶ ./telemetry run | sql | mcp  ── browser, queries, Claude Code
    └──▶ reports/report.html          ── self-contained local dashboard
```

The collector is the only moving part, and it is a stock upstream image with a
config that exports to local files. If you would rather not run Docker,
`./telemetry start --no-docker` swaps in a ~150-line receiver from the Python
standard library that writes the identical file format.

---

## Prerequisites

- **Python 3.9+** — standard library only. There is nothing to `pip install`.
- **Docker** — optional but recommended; without it you get the fallback
  receiver, which speaks `http/json` only.
- **git** — optional; without it project mapping and commit reconciliation are
  skipped.
- **Claude Code 2.1.193+** for `assistant_response`, 2.1.214+ for
  `client_request_id`. Developed against 2.1.237.

## Setup

```bash
git clone <this repo>
cd telemetry
./telemetry init              # prerequisites, settings, hooks, collector, MCP
./telemetry doctor            # prove the whole pipeline works, end to end
claude                        # use Claude Code normally
```

`init` asks before each step that writes outside this directory, and every
step is independently re-runnable, so stopping half way is safe. It is a
wrapper around the parts, not a replacement for them - `./setup.sh` on its own
still writes `.env.telemetry` for you to `source` per shell if you would
rather not touch `~/.claude/settings.json`.

Then, after an hour of real work:

```bash
./telemetry                   # analyse in the background, browse immediately
./telemetry status            # running? events arriving?
./telemetry report --open     # build and open the HTML report
```

`setup.sh` never touches your shell profile. It writes `.env.telemetry` in this
directory for you to `source`. If you want it permanent, run `./install-shell.sh`
— a separate script that shows you the exact lines and asks before appending
them to `~/.zshrc`.

### Options

```bash
./setup.sh --full         # export everything: prompts, responses, tool
                          # input/output and raw API bodies. Pair with
                          # ./telemetry config privacy --enable-all to actually store it.
                          # data/ then contains your source code and the full
                          # text of your conversations. Budget ~70-260 KB per
                          # API call for bodies; watch data/raw/ size.
./setup.sh --minimal      # no OTEL_LOG_TOOL_DETAILS: no tool arguments ever
                          # reach disk, but no file paths, bash commands,
                          # skills or subagent types either
./setup.sh --http         # OTLP over http/protobuf on 4318 instead of gRPC
./setup.sh --http-json    # OTLP over http/json, required by --no-docker
```

### Keeping it running

The collector container is declared `restart: unless-stopped`, so Docker brings
it back whenever the daemon starts. If Docker Desktop is set to open at login,
a reboot needs nothing from you — check with `./telemetry status`.

Two cases are not covered by that. The fallback receiver (`--no-docker`) is an
ordinary process and does not survive a reboot. And if Docker Desktop does not
start at login, nothing starts the daemon for the container to come back into.

On macOS, a launch agent covers both. `./telemetry start` exits once the
collector is up, so this is a one-shot at login, not a daemon — no `KeepAlive`,
or launchd would respawn it forever:

```bash
REPO="$(pwd)"
cat > ~/Library/LaunchAgents/com.claude-telemetry.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-telemetry</string>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$REPO/var/launchagent.log</string>
  <key>StandardErrorPath</key><string>$REPO/var/launchagent.log</string>
  <!-- launchd's PATH has neither homebrew python3 nor docker. -->
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <!-- Docker Desktop is starting at login too, so the daemon is not ready
       yet. Wait up to five minutes rather than losing the race. Written
       without an ampersand so it stays valid XML. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>n=0; while [ \$n -lt 60 ]; do if docker info >/dev/null 2>/dev/null; then break; fi; n=\$((n+1)); sleep 5; done; exec $REPO/telemetry start</string>
  </array>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-telemetry.plist
```

Drop the wait loop and add `--no-docker` to the `telemetry start` call if you
are running the fallback receiver. Undo with `launchctl bootout
gui/$(id -u)/com.claude-telemetry` and delete the file.

---

## Enabling Claude Code telemetry

`.env.telemetry` is generated for you, but this is what it sets and why. Every
variable was checked against the current docs — see
[`docs/anthropic-telemetry-notes.md`](docs/anthropic-telemetry-notes.md).

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_TRACES_EXPORTER=otlp
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1     # spans: file_path, full_command
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_METRIC_EXPORT_INTERVAL=10000         # default 60000 feels dead
export OTEL_LOGS_EXPORT_INTERVAL=5000
export OTEL_LOG_TOOL_DETAILS=1                   # see the trade-off below
```

Telemetry settings are read at startup, so a Claude Code session already running
when you `source` the file will not be observed. Start a new one.

**The one trade-off.** `OTEL_LOG_TOOL_DETAILS=1` is what unlocks file paths,
bash commands, skill names and subagent types. Without it Claude Code redacts
tool parameters and this project can tell you *that* a tool ran, but nothing
about what it touched. With it, the raw archive can contain short excerpts of
file content (Claude Code truncates values at 512 characters). The normalized
database never stores them — [`PRIVACY.md`](PRIVACY.md) is precise about this,
and `./setup.sh --minimal` opts out.

Prompt and response text stay redacted by Claude Code itself either way.

---

## Verifying events are arriving

```bash
./telemetry status
```

```
Collector
  container      running (claude-telemetry-collector)
  health         ok
  OTLP gRPC 4317 open
  OTLP HTTP 4318 open

Raw telemetry  data/raw
  files          3
  size           412.7 KB
  lines          1,204
  last write     2026-08-20 11:36:41  (0.4 min ago)  events arriving

Database  data/telemetry.db
  sessions       7
  events         1,180   metrics 402   spans 233
  api calls      96   cost $4.1233
  ...
  1,204 raw lines not yet analysed - run ./telemetry analyse
```

Other checks:

```bash
./telemetry doctor                        # 11 end-to-end assertions
curl -s localhost:13133                # collector health
tail -1 data/raw/logs.jsonl | python3 -m json.tool | head
docker compose logs --tail=50          # collector's own log
claude --debug                         # Claude Code's OTLP export errors
```

Inside a Claude Code session, `/status` shows its telemetry configuration.

---

## CLI

Twelve commands, plus a hidden `session-hook` that Claude Code calls itself.
The view layer is not among them: it is served to Claude Code over MCP, and
drawn in the browser.

| Command | Does |
|---|---|
| `./telemetry` | the browser, over fresh data (same as `run`) |
| `./telemetry init` | guided first run: settings, hooks, collector, MCP, first analyse |
| `./telemetry run [--no-analyse]` | browse sessions and their turns (`--static` prints one frame) |
| `./telemetry start [--no-docker]` | start the collector |
| `./telemetry stop` | stop it |
| `./telemetry status` | running? events arriving? anything unanalysed? |
| `./telemetry analyse` | ingest new raw telemetry, and name finished sessions (incremental, idempotent) |
| `./telemetry enrich [--what …]` | the model-backed passes; costs money, asks first |
| `./telemetry report [--view sessions] [--open]` | build the HTML report |
| `./telemetry mcp [--register]` | serve the database to Claude Code |
| `./telemetry sql "SELECT …"` | read-only query, `--json` for JSON |
| `./telemetry doctor [--keep]` | end-to-end self test |
| `./telemetry config <privacy\|ignore\|connect\|env\|uninstall>` | everything that writes settings |

`enrich` runs six passes, each cached, each asking before it sends anything:

| Pass | What it produces |
|---|---|
| `--what describe` | one line per session saying what it set out to do |
| `--what classify` | turns labelled correction / steering / normal by a model |
| `--what corrections` | each correction traced to a cause and a proposed fix |
| `--what diagnose` | what went wrong in a session and what to change |
| `--what skills` | skills that should have fired and did not |
| `--what docs` | knowledge-base profiles and per-session document gaps |

With no `--what`, everything except `docs` runs (that one needs a `--root` the
first time). `--report-only` prints what is already cached and calls nothing.

`analyse` is safe to run as often as you like. Each raw file has a line cursor
and every row has a dedupe key, so nothing is counted twice — and a run with
no new finished sessions makes no model call, so it costs nothing but a
second or two.

---

## Querying it from inside Claude Code

The view layer is an MCP server rather than a shelf of table-printing
commands. The questions this data answers — "what did that session cost",
"which skill never fires", "where did I spend the afternoon" — occur while
working, inside a Claude Code session. Printing tables into a terminal meant
reading them out and pasting them back in.

```bash
./telemetry mcp --register        # or let ./telemetry init do it
```

That registers `telemetry` at user scope, so every session on this machine
can reach it. Nine read-only tools:

| Tool | Answers |
|---|---|
| `telemetry_overview` | totals: period, spend, output, friction, what is attributable |
| `telemetry_sessions` | the unit of work, newest first: what each was for, cost, commits, corrections |
| `telemetry_session` | one session in full — turns, effort, commits, deploys, tooling, doc gaps, findings |
| `telemetry_friction` | corrections, steers, rejects, tool failures, rework files |
| `telemetry_inventory` | installed skills and MCP servers against what actually fired |
| `telemetry_files` | files read, written, created; never-opened files under a root |
| `telemetry_docs` | knowledge-base coverage, cold spots, per-session gaps |
| `telemetry_schema` | tables and columns, for writing SQL |
| `telemetry_sql` | read-only SQL for anything the rest do not cover |

It reads `data/telemetry.db` directly and never touches the collector, so it
answers whether or not anything is running, needs no port and no auth, and
behaves the same under Docker and the fallback receiver. The connection is
opened read-only at the SQLite level: `telemetry_sql` cannot be talked into
writing, and neither can a bug.

The protocol side is under a hundred lines of JSON-RPC over stdin/stdout at
the foot of `tel/mcp.py` — no dependency, in keeping with the rest of this
project.

### The skill

`.claude/skills/telemetry/SKILL.md` teaches Claude Code how to use those tools
— which one answers which question, that the current session is never in the
database yet, and how to read the columns without guessing. It ships in this
repository, which makes it a **project** skill: it loads only while you are
working in this directory.

The MCP server is registered at user scope, so the tools themselves are
reachable from every project on this machine. The skill is what makes them
usable there too:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/telemetry ~/.claude/skills/telemetry
```

Copied there it is a personal skill, available in every session on this
machine — which matches what the data covers, since one database holds every
project. Re-copy it after a `git pull` to pick up changes.

---

## Where data lives

| Path | What |
|---|---|
| `data/raw/*.jsonl` | append-only OTLP archive, one export request per line |
| `data/telemetry.db` | normalized SQLite database |
| `data/session_context.jsonl` | append-only cwd/repo/branch/HEAD written by the session hooks |
| `data/session_diagnosis.jsonl` | append-only session findings, so a rebuild does not re-buy them |
| `data/session_chat.jsonl` | append-only per-session conversations |
| `reports/report.html` | generated report |
| `.env.telemetry` | generated shell settings |
| `var/` | fallback receiver pid and log |

`data/`, `reports/` and `.env.telemetry` are gitignored.

The raw files, plus `session_context.jsonl`, are the source of truth. The
database is derived and disposable: delete it, run `./telemetry analyse`, and it
rebuilds completely from those files. The hooks deliberately write to
`session_context.jsonl` rather than to SQLite, both so a rebuild cannot lose
that context and so a hook running at session start never contends for a
database lock. That is
the whole point of keeping the raw stream — when the schema grows a column, old
telemetry can be reinterpreted rather than re-collected.

---

## Database schema

Every normalized row keeps `raw_json` plus a `(source_path, source_line)`
pointer back into the archive. Column names mirror Anthropic's attribute names;
tables prefixed `local_` come from reading your filesystem, not from telemetry.

| Table | Rows are | Source |
|---|---|---|
| `projects` | a git repository | local git |
| `sessions` | a Claude Code session | `session.id` + standard attributes |
| `events` | every log record, whatever its type | logs signal |
| `metric_points` | every metric data point | metrics signal |
| `spans` | every span | traces signal |
| `api_calls` | one model call | `api_request`/`api_error`/`api_refusal` |
| `tool_calls` | one tool invocation | `tool_decision` + `tool_result` + span, merged |
| `skill_calls` | a skill invocation | tool params, `skill.name` |
| `file_activity` | one read/edit/write of one path | tool params, spans |
| `bash_activity` | one command | `full_command` |
| `subagent_activity` | a subagent run | `subagent_type`, `agent.name` |
| `prompts` / `responses` | prompt and response metadata | `user_prompt`, `assistant_response` |
| `errors` | api errors, refusals, tool failures, internal errors, MCP failures | several events |
| `git_activity` | a commit | **local git only** |
| `git_commit_files` | one file in one commit | **local git only** |
| `local_session_git_context` | cwd/repo/branch/HEAD at session start and end | **local git only** |
| `raw_files`, `ingest_runs`, `meta` | ingest bookkeeping | — |

Views: `costs` (cost and tokens by session/day/model/source/skill/agent),
`session_summary`, `project_summary`.

`tool_calls` is the interesting one. A single tool invocation shows up in up to
three places — the permission decision, the result, and a span — and they are
merged on `(session_id, tool_use_id)` with `COALESCE`, so no signal overwrites
another. `tool_calls.sources` records which contributed.

---

## Project mapping

Each session is resolved to a `project_id` by the first of these that works:

1. **Session hook** (`./telemetry init`) — the real `cwd`, captured at
   `SessionStart` and again at `SessionEnd`, along with repo root, remote,
   branch and HEAD, appended to `data/session_context.jsonl`. Most reliable,
   and the only thing that maps a session which ran no tools at all.
2. **`workspace.host_paths`** — the documented telemetry attribute. *Claude
   Code 2.1.237 does not actually emit it*, so in practice this rarely fires.
   Read anyway, in case it returns.
3. **Path inference** — absolute paths are extracted from tool arguments and
   bash command lines, each is resolved to a git repository, and the most
   frequently referenced repository wins. This is what carries real sessions
   today, and it worked on the first real session tested.
4. Otherwise the session lands in `(unattributed)`.

Which one was used is recorded per session in
`sessions.project_detection_method`, so you can always tell how much to trust
a mapping.

`project_id` is a stable hash: `remote:<hash of host/org/repo>` when a remote
exists — so the same repository groups across clones and across ssh/https
forms — falling back to `root:<hash of path>` then `dir:<hash>`.

### The hooks are optional and explicit

`./telemetry init` edits `~/.claude/settings.json`. It shows the exact
JSON, asks for confirmation, and backs up the existing file first.
`./telemetry config uninstall` reverses it. Nothing else in this project touches
configuration outside its own directory.

---

## File activity, and the created-vs-modified problem

Claude Code reports that a `Write` happened. It does not report whether the
file already existed. So creation is **inferred**, and every row records which
method was used and how much to trust it:

| `create_method` | `create_confidence` | Logic |
|---|---|---|
| `git_added` | high | the file's first `A` commit in the repo is at or after the session start |
| `no_prior_read` | medium | no earlier `Read` or `Edit` of that path anywhere in the database |
| `unknown` | none | not in a repo and no prior activity to reason from |

Both are heuristics. `git_added` is wrong if the file was created before the
session and committed during it. `no_prior_read` is wrong if Claude wrote a
file it had seen in a previous, unobserved session. Treat "files created" as an
estimate, and use `create_confidence` to filter.

Questions the `file_activity` table answers directly:

```bash
./telemetry sql "select count(distinct path) from file_activity where operation='read'"
./telemetry sql "select path, count(*) n from file_activity group by path order by n desc limit 10"
./telemetry sql "select count(distinct path) from file_activity where operation in ('edit','write')"
./telemetry sql "select path from file_activity where created=1 and create_confidence='high'"
```

The report's *Repository areas* table answers "which parts of the repo consumed
the most AI activity" by grouping on the first path segment.

---

## Ignoring paths

Agent scratchpads and dependency trees are churn, not work on the project —
they were 22% of all recorded file activity here.

```bash
./telemetry config ignore                          # show the patterns in force
./telemetry config ignore --add '*/coverage/*'
./telemetry config ignore --remove '*/dist/*'
./telemetry config ignore --reset
```

Defaults cover `/tmp`, `/private/tmp`, `node_modules`, `.git`, `.venv`,
`__pycache__`, `.next`, `dist`, `build` and `.DS_Store`. Editing them writes
`data/telemetry.ignore`, and the patterns are applied on every `telemetry analyse`
rather than at write time — tool-derived rows cannot be re-derived without
re-reading the raw archive, so filtering at the end means a changed pattern
takes effect whatever produced the row.

## Git activity

**Claude Code never emits a commit hash.** It emits
`claude_code.commit.count`, a counter. Everything in `git_activity` therefore
comes from read-only `git log` against repositories seen in telemetry, and a
commit is linked to a session when its timestamp falls inside that session's
window.

That is correlation, not authorship — `git_activity.attribution` says
`session_time_window` so the inference is visible rather than implied. A commit
you made by hand while Claude was running will be attributed to that session.

No git command this project runs can modify a repository.

---

## Terminal UI

```bash
./telemetry                    # the default: browse, and keep up
./telemetry run --no-follow    # stored data only, nothing analysed
./telemetry run --static       # print one frame and exit
```

The browser **follows the work as it happens**. Two signals, both cheap:

- **the raw archive growing** means telemetry has arrived that nobody has
  normalised, so it analyses;
- **SQLite's `data_version` changing** means another process wrote to the
  database — a second `analyse`, a `connect`, a cron — so it just reloads.

Doing neither costs one `stat` per raw file every two seconds. Reloads keep
your place: the cursor stays on the same session, and the pane keeps its
scroll. `f` pauses and resumes following; the footer says `paused` when it is
off.

Naming is throttled to once every five minutes while following. A session in
progress becomes eligible again on every turn, and the sentence hardly moves
between them.

A master-detail browser over the same data as the report: sessions on the
left with cost bars and flag glyphs, the selected session's detail on the
right — what it was for, what it committed, its tool lanes and turn strip, and
the most expensive correction with its diagnosed cause.

New telemetry is folded in *behind* the UI, not in front of it: the list draws
immediately, the follower analyses on a second connection, and the view
reloads itself when a pass lands. SQLite takes concurrent readers and one
writer, so nothing has to wait on anything.

| key | |
|---|---|
| `↑ ↓` / `j k` | move |
| `→` / `l` / `space` | step into a session (or expand a project) |
| `←` / `h` | collapse |
| `enter` / `→` | step into a session pane |
| `esc` | leave the session pane |
| `J` `K` | scroll the detail pane |
| `g` | cycle grouping: session / project |
| `s` | cycle sort: recent / cost / turns |
| `/` | filter by name, `esc` clears |
| `r` | reload from the database |
| `f` | follow: analyse new telemetry as it arrives |
| `?` | help · `q` quit |

Each row is `glyph · age · name · cost · flags`. The age column is compact
(`20m`, `3h`, `2d`) rather than spelled out — at a 46-column list pane the
extra " ago" costs four characters of name on every row, and the column says
what it is. Anything under an hour old is drawn in accent so live work stands
out.

### Inside a session

Two panes, left to right: the list of sessions, and the session itself. There
is no third thing a session belongs to.

`→` on a session steps in, so the whole descent — list, session, event — is
the same key repeated. **The list does not go away**: it stays in the left
pane with the session still marked by a `▎` gutter and its bar, minus the
reverse video that says "your keys land here". Only focus moves. The list keeps
its width, so nothing reflows under the cursor as you step in and out.

The session view is two axes. `↑↓` selects a lane (or the turns bar at the
bottom); `←→` walks the events within it. `←` is overloaded on purpose: it
steps *back* through the current lane, and only leaves the pane once there is
nothing left to step back to — the motion you make constantly never costs you
your place. `esc` leaves immediately from anywhere. Selecting a lane lists **everything
in it** below, with the current entry marked and the list scrolled to keep it
in view:

```
other files read · 95 items  ←→ navigate this list
     4 14:00:04 read  atlas-app/locales
     5 14:00:07 read  atlas-sandbox/src
  ▸  6 14:00:07 read  atlas-app/locales/en.json
     7 14:00:19 read  atlas-app/components/screens/Home.tsx
```

Paths are trimmed to the repository they live in. On the turns bar the list is
the session's whole conversation — duration, cost, kind and the prompt itself:

```
turns · 25 items  ←→ navigate this list
     3    7s $   0.09  steering   open them
  ▸  4   26m $   8.84  correction No, no the sandbox app, the atlas-app one
     5    6m $   2.30  normal     Can you show me a screenshot of what the user…
```

Harness-injected prompts — monitor notifications, system reminders — are drawn
as `system` in a recessive colour rather than counted as normal turns, matching
how the effort metrics treat them.

Selection is shown by **texture and weight, never by inverting colour**: the
selected dot becomes `◉`, the selected turn becomes a mixline `▚▚▚` run that
keeps its own hue and exact size, and a `▎` marks the active lane in the gutter. Reverse video
turns a coloured block black-on-hue, which reads badly on a light terminal and
worse on a dark one.

Several events routinely land in the same column at terminal resolution — 121
file reads across 43 columns — so the highlight follows **the column the
cursor's event falls in** rather than whichever event happens to own that cell,
and the panel says how many share it.

On the turns bar each segment is a turn, so stepping through gives its
duration, cost, tool count, the gap before it, the prompt, and — for a
correction — the diagnosed cause and the sentence that would have prevented it.
`0`/`$` jump to the first and last event.

Glyphs: `▸` a session that committed, `○` one that produced none, `▾`/`▸` an
expanded or collapsed project. Flags are `d` docs unread, `s` skills missed,
`!` corrections.

Built on **`curses` from the standard library**, so the project keeps its
"clone and run, nothing to install" property. The cost of that choice is
colour: curses colour pairs address the 256-colour cube rather than 24-bit, so
the palette is the 256-colour approximation of the same validated categorical
steps the HTML charts use, with fallbacks picked by *role* rather than nearest
hue so the meaning survives on a weaker terminal. Below 108 columns the detail
pane is dropped and the list takes the full width.

The turn strip allocates cells by largest remainder so it is exactly as wide as
the lanes above it — rounding each turn independently overflows the column it
has to line up with, which is the whole point of drawing them together.

## The session — the unit of work

**One session is one item.** It has its own prompts, its own cost, its own
commits, and its own turns. Nothing is grouped, shared or apportioned, which
means every number on a session is a fact about that session rather than a
share of something larger.

An earlier version grouped sessions into *workflows* by conventional-commit
scope, on the theory that a piece of work spanning three sessions should be
counted once. It bought a truer unit at the price of every number becoming an
estimate: cost had to be apportioned, effort was a session total attributed to
one of several streams, and a card could not tell you what any single sitting
had actually been like. The session is coarser and honest.

A session earns its place in the list by having produced **at least one prompt
of its own**. Hooks record a row for every session that starts, including ones
that exported no telemetry, and a session can also survive with tool activity
but no prompt text — the storage policy in force when it was ingested may have
dropped the text, or it may be a synthetic test session. Either way there is
nothing to name it by and nothing to read, so it stays out of the list. The
raw archive still holds those events, so a rebuild under a policy that stores
content brings them back.

### What a session is called

Its **description** — one line saying what it set out to do, written from its
prompts by `./telemetry enrich --what describe`. Failing that, the
conventional-commit scope it committed under, and failing that its short id.
Names are never invented from tool traffic: a session that spent an hour
failing to fix a build still had "fix the build" as its goal, and the files it
touched would describe the struggle rather than the intent.

### Attributing a commit to a session

By **which session touched the files the commit changed**, falling back to the
time window only when there is no file evidence. Time alone is a poor signal:
sessions run concurrently, and "the one that started most recently" credited
an `atlas-app` commit to a session that never opened either changed file —
leaving the session that did the work looking commit-less. File evidence moved
attributed spend from 62% to 99.9%.

Commit **type** and **scope** are still parsed out of subjects
(`feat(atlas-video): …`) and kept on the commit, where they are a label
rather than a grouping key. They are what names a session that has no description yet.

Sessions that produced no commit at all — QA, research, writing, debugging that
didn't land — are peers in the list, not a leftover category. They were **a
third of the spend** here.

### Connectors

Read-only, using CLIs you have already authenticated. Nothing is sent anywhere.

```bash
./telemetry config connect github                                   # PRs, CI runs, deployments
./telemetry config connect eas --dir ../your-app --branch production
./telemetry config connect all                                      # remembers settings after the first run
```

- **GitHub** (`gh`) — pull requests, CI runs, and deployments. Deployments are
  the valuable part: they carry a commit SHA and an environment, which is what
  separates *committed* from *reached users*.
- **EAS** (`eas`) — over-the-air mobile updates. `update:list` reports a commit
  *message* rather than a SHA, so updates are matched to commits by exact
  subject; unmatched ones are still recorded, without a SHA. It also reports no
  publish time, so matched updates borrow their commit's timestamp and
  unmatched ones keep a NULL rather than a fabricated date.
- **Reverts** are detected from commit subjects and are the clearest negative
  signal available: work that had to be undone.

Settings are remembered in `data/telemetry.connectors.json`.

### Outcomes

The `outcomes` table is keyed on session and deliberately generic
(`source`, `metric`, `value`, `window_start/end`, `confidence`, `method`), so
product analytics, email signups, social stats and eventually revenue are
*rows*, not schema changes. `surface_map` maps changed code paths to the
analytics events that mean "a user saw this", which is what allows exposure to
be counted at `reached` confidence rather than only `live_to`.

## Human effort

Claude Code emits no notion of "the agent got this wrong", so this is built
from proxies — and every one is labelled as such, with the cue that fired
recorded next to it so any number can be audited.

The grain is the **turn**: `prompt.id` is stamped on every event a single
human instruction causes, so one row in `turns` is one instruction and
everything it cost.

| Signal | Meaning | How |
|---|---|---|
| correction | the developer is telling the agent it got something wrong | model, or a phrase list as fallback |
| steering nudge | a nudge that continues or adjusts work already in progress | model, or prompt ≤ 45 chars as fallback |
| rework | the same file returned to across separate turns | `file_rework` |
| override | you overruled a tool decision | `decision_source` = `user_reject` / `user_abort` — reported directly by Claude Code |
| gap before | how long you spent reading before replying | time since the previous turn ended |

The `telemetry_friction` MCP tool totals these per session and shows the share
of spend that went to correction turns.

### Labelling turns with a model

The length heuristic conflates *short* with *low-information*: `promote to
production` is 21 characters and ships a release. `./telemetry enrich --what classify` labels
turns with a small model (Haiku by default) that reads each prompt alongside
the previous one and the gap between them.

```bash
./telemetry enrich --what classify --dry-run   # show exactly what would be sent
./telemetry enrich --what classify             # asks before sending
./telemetry enrich --what classify --apply-only  # re-apply cached labels, no model calls
```

**This is the only part of the project that leaves your machine.** Prompt text
goes to Anthropic through your already-authenticated `claude` CLI. It is
opt-in, never runs as part of `analyse`, prints the privacy warning and asks
first. The subprocess runs with telemetry and MCP switched off, so classifying
prompts cannot write new sessions into the database it is classifying.

Results are cached by prompt hash in `turn_classification`, so a repeat run
costs nothing and `analyse` re-applies them rather than discarding them.
`turns.label_source` records whether each label came from the model or the
heuristic, and `./telemetry enrich --what classify` prints every row where the two disagree.

### Harness-injected prompts

Not every `user_prompt` event is something a human typed. Monitor
notifications, system reminders and slash-command echoes arrive as prompts and
were inflating turn counts. They are flagged `turns.is_system`, excluded from
every effort metric, and reported separately with their cost — they are real
spend, just not human effort.

## File access, including through the shell

Most real file access never touches the file tools: docs get read with `grep`
and `sed`, files get written with `cat >`. A database built only on tool events
reports zero activity for a file that was read a dozen times.

`tel/shellfiles.py` parses stored bash commands for file access — redirects,
`sed -i`, `cp`/`mv`/`rm`, `curl -o`, readers like `cat`/`head`/`grep`/`wc`,
directory listings, and Python heredocs that call `read_text` / `write_text`.
It is quote-aware (a pipe inside `grep "a\|b"` is not a separator), handles
filenames with spaces, resolves relative paths against a leading `cd` or the
session's working directory, and skips a program's pattern argument so
`grep 's/x/y/' f.md` records `f.md` rather than the pattern.

Every row records `via` (`tool` or `shell`) and, for shell rows, an
`op_confidence`:

| Confidence | Example |
|---|---|
| high | `> file`, `cat file`, `sed -i … file`, `cp a b`, `rm file` |
| medium | `grep pat file`, `sed -n … file`, a Python heredoc calling `read_text` |

A recursive grep over a directory is recorded as a **search of the directory**,
not a read of every file inside it.

Shell rows are **rebuilt from `bash_activity` on every `analyse`**, so parser
improvements apply to history without re-reading the raw archive.

```
telemetry_files(under="docs", unread_under="~/uv/docs")   # an MCP tool call
```

`--unread` compares what is on disk against what was opened — the gap analysis
for a knowledge base, since the point of writing something down is that it gets
consulted.

Inline interpreter code (`python3 -c "…"`) is not treated as a path, but *is*
scanned for real file IO, so `Image.open('/a/img.png')` records the image.
`git mv` / `git rm` / `git checkout --` are recorded as the file operations
they are.

A **plausibility guard** rejects tokens that cannot be filenames — bare numbers,
shell fragments with trailing punctuation (`28:`, `120]`), anything with no
letters in its final component. A wrong path is worse than a missing one: it
invents activity against a file nobody touched.

**Known undercount:** a heredoc that builds a path from variables, or a tool
invoked through a wrapper script, will not be detected. Shell parsing is
inference, and `op_confidence` says how much to trust each row.

## Time

Durations come from tracing spans and from turn boundaries. Two clocks exist
and they are not interchangeable:

- **Active time** — the sum of turn wall-clock. What the work actually took.
- **Elapsed time** — active time plus the idle gaps between your turns.

Inside a turn, `llm_request`, `tool.execution` and `tool.blocked_on_user`
**overlap** (tools run in parallel, and a model request happens inside a turn),
so they are shown as separate magnitudes and never stacked into a total.
`tool.blocked_on_user` is the one worth watching: it is the agent sitting idle
waiting for you to answer a permission prompt.

The **session activity strips** in the session report chunk each session by
turn, width proportional to duration, coloured by whether the turn was normal,
a correction or a steering nudge. Idle gaps are excluded so the work stays
legible; each strip is captioned with active vs elapsed, and each segment names
its own preceding gap on hover.

The strip leads, because it is the axis everything else is plotted
against. Under it sit **lanes**, sharing that x-axis:

- one row per **skill or MCP server**, dotted at every call — the first dot is
  the load, so a session opening with Playwright and picking up a skill halfway
  through is visible at a glance;
- **markdown read**, **other files read** and **files written** as aggregate
  rows — markdown is split out because "was the doc opened" is a different
  question from "was the code opened", and mixing them hides both;
- the **files touched most often** in that session get their own row, tagged
  `hot`, so a file returned to thirty times reads as a row of dots rather than
  a number. These rows are a *zoom*, not a fourth category — every dot in them
  is already counted in the aggregate rows above, and the `hot` tag is there so
  they don't read as more file activity than actually happened.

Every session has a card, so every strip lives on the card of the session it
describes — including sessions that produced no commit, which are a third of
the spend.

Because the strip axis is *compressed* active time, a raw timestamp cannot be
placed by proportion of elapsed time. Each turn occupies a slice sized by its
own duration, an event inside a turn lands at its offset within that slice, and
an event that fell in an idle gap snaps to the nearest turn boundary.

Skill timing is an **upper bound**, not a measurement: a `Skill` tool call takes
milliseconds because it only loads instructions, so what is reported is the
wall-clock of the turns the skill was active in — which included other work too.

The chart palette is the validated default from the `dataviz` skill, checked
with its validator against this report's own light and dark surfaces. Light mode
warns on contrast for two slots, so every chart ships direct labels and a table
view.

## Knowledge-base coverage

"150 files, 4 read" is a statistic, not a finding. A contacts directory going
unread by a coding agent is correct behaviour; a service specification going
unread while that service is being worked on is a real gap. The difference is
who the document is *for*.

`./telemetry enrich --what docs` scans a knowledge base locally, then profiles each document
once for **audience** (`agent` / `human` / `both`) and **agent relevance**
(`high` / `medium` / `low`). **Cold spots** counts only documents that are
agent-facing, at least medium relevance, and never opened — and coverage is
measured against agent-facing documents rather than the whole vault.

```bash
./telemetry enrich --what docs --root ~/notes --dry-run
./telemetry enrich --what docs --root ~/notes                                 # asks before sending
./telemetry enrich --what docs --report-only                                  # cached, no model calls
./telemetry enrich --what docs --root … --titles-only                         # send no content at all
./telemetry enrich --what docs --root … --exclude 'Contacts/.*'               # skip paths entirely
```

Knowledge bases hold business-sensitive material, so profiling sends only a
path, a title and an excerpt capped at 240 characters. `--titles-only` sends no
content, `--exclude` skips paths before anything leaves the machine, and
profiles cache to `data/doc_profiles.jsonl`.

### Per-session document gaps

Vault-wide coldness is a blunt instrument: relevance only means anything
against a specific piece of work. `./telemetry enrich --what docs --gaps` asks, for
each session, **which documents would have helped and were never opened while
that session was happening**.

```bash
./telemetry enrich --what docs --gaps            # judge, then report
./telemetry enrich --what docs --gaps --report-only
./telemetry enrich --what docs --gaps --redo     # re-judge everything
```

Candidates are pre-filtered **locally** by token overlap between the document
and what the session actually touched — its prompts, changed files and commits
— so a judgement is only paid for on plausible pairings. A document read during
that session is excluded, since being read elsewhere does not help this work.

Results appear on the session card in a **Documents** block with two rows —
what was read, and what was relevant and wasn't — each chip carrying the
document's topic, size and (for gaps) the reason, on hover.

## Descriptions

Everything else in this project is counts, paths and identifiers. None of it
tells you at a glance that `32baff1a` was *"investigating why declined users
see no reason, against a running simulator"*.

One line per session, and that line is the name the session goes by everywhere
— the browser, both reports, the MCP tools.

**It is written during `analyse`, not by a command you have to remember.** A
name nobody generated is no name at all, and a database where half the rows are
called `b944dba4` is not readable. This is the only step of the pipeline that
talks to anything off this machine; `./telemetry config privacy --auto-describe
off` stops it, and `./telemetry enrich --what describe` still does it on demand.

The rule is simple: **a session is named as soon as it has said anything, and
renamed whenever it has said more.** A best guess on a session in progress is
worth more than a row called `b944dba4`, and the sentence follows the work
rather than freezing at whatever the first ten minutes were about: a session
that opens by chasing a bug and ends up rewriting a subsystem gets named for
the rewrite once the rewrite is what it did.

A run where no session has gained a prompt turn makes no model call at all.

At most a dozen sessions are named per analyse, newest activity first, so the
session you are in is always the one that gets named and a backlog is worked
through over successive runs rather than stalling one of them.

It is written **from the prompts**, in order, with the files, commits and tools
supplied only as evidence for what the prompts refer to obliquely ("fix it",
"try again"). That split is deliberate: what the agent ended up touching says
how the session went, not what it was for. A session that spent an hour failing
to fix a build still had "fix the build" as its goal.

The instruction is strict about what counts: `"A session involving multiple
tool calls"` describes telemetry, not work, and is rejected.

Read counts come from the shell file parser, so a doc opened with `grep` or
`sed` counts. A recursive grep over a directory is recorded as a search of the
directory, **not** a read of each file inside it — crediting all of them would
report a healthy knowledge base where there isn't one.

## Asking about a session

The report and the browser answer the questions someone thought to build a
view for. Asking answers the rest — "why did this take two hours", "did it
ever read the Checkout doc, and when", "what should I do differently next time".

There are two ways in. Inside a Claude Code session the MCP tools put the
whole database in reach, so the question can be asked where the work is
happening. In the browser, **`c`** opens a conversation in the right-hand pane against
whichever session is under the cursor. Type, `enter` asks, `↑↓` scrolls the
transcript, `esc` closes it. The question is recorded before the model is
called, so a crash mid-answer loses the answer and never the question.

The agent gets a **brief**, not the database: the turns with their prompts,
costs and gaps; every file with read/write counts and first/last timestamps;
commands with run and failure counts; tools, skills, commits; the friction
signals; and any diagnosis already made. About 5k tokens for a thirteen-turn
session. It is told to answer from the brief and to say plainly when the brief
does not cover something — a wrong answer about your own telemetry is worse
than no answer, because you cannot tell it is wrong without going and checking.

That instruction earns its place. Asked "did it read the Checkout doc and when",
the first version answered the *what* and then said the brief carried no
timestamps for file operations, so it could not answer the *when* — rather than
guessing. The fix was to put `first_at`/`last_at` in the brief, not to loosen
the rule.

Conversations are stored per session and appended to a sidecar, so a line of
enquiry survives closing the TUI, and survives a database rebuild like
everything else here that costs money to produce. The whole conversation is
re-sent each time rather than resumed — a handful of short turns about one
session is not worth a session-resumption mechanism, and re-sending means a
conversation works from anything that can read the sidecar.

### Why it polls a subprocess instead of using a thread

The first version ran the model in a worker thread. It deadlocked every time:
`curses.getch()` does not release the GIL, so the worker could not make
progress. The TUI now spawns a detached process writing to temp files and polls
it from the draw loop — one thread, no pipe that can fill, and the spinner
keeps turning. The child gets `start_new_session=True` and no stdin, so it can
never reach for `/dev/tty` and wait there.

## Session diagnosis

Every other analysis here answers a narrow question: why did *this* correction
happen, which skill should *this* session have used, which doc should *this*
session have read. None of them look at the shape of the work itself — that a
file was rewritten eleven times, that the same command ran nine times, that one
turn ate a third of the money.

Those are the cheapest signals in the database and **none of them need a
model**, so the two halves are separate commands:

```
./telemetry enrich --what diagnose --report-only      # free, deterministic, no network
./telemetry enrich --what diagnose                # ...then ask a model what they mean
```

`--signals` computes friction mechanically and prints it:

```
9ae4f69f  acme/atlas
  file written repeatedly  src/atlas/social/ClipEditor.tsx
                           24 writes/edits
  read write alternation   README.md
                           22 switches between reading and writing it (13 reads, 11 writes)
  tool failing             Bash
                           6 failures (ShellError)
```

| Signal | Fires when |
|---|---|
| `file_written_repeatedly` | one file written ≥ 4 times |
| `read_write_alternation` | ≥ 4 flips between reading and writing one path |
| `file_read_repeatedly` | one file read ≥ 6 times and never written |
| `command_repeated` | an identical shell command run ≥ 4 times |
| `command_failing` | one program fails ≥ 3 times |
| `tool_failing` | one tool fails ≥ 3 times with the same error |
| `search_storm` | ≥ 10 searches inside a single turn |
| `cost_spike` | one turn is ≥ 30% of the session and ≥ $1 |
| `work_rejected` | ≥ 3 tool calls rejected or aborted by the human |

Thresholds are deliberately generous: a false negative costs nothing, a false
positive costs a model call and the reader's trust.

**A signal is not a defect.** A video frame refined eleven times is iteration
and has no fix; a config file rewritten eleven times is a loop and does.
Telling those apart is the only thing the model is asked to do — it interprets
evidence, it never goes hunting for it. That keeps the prompt small, the cost
flat per session, and every finding anchored to a number you can check:

```
HIGH   rework loop   9ae4f69f · acme/atlas
  Component was edited 24 times with a systematic read-verify cycle after
  nearly every write, indicating incremental changes made without a clear
  design specification upfront.
  ClipEditor.tsx: 24 writes with 23 reads in 45 read-write alternations (1:1)
  → claude md  Add to CLAUDE.md: For complex visual or animation components,
    write a design spec describing structure, props, animation timing and key
    behaviours before starting edits.
```

Two guardrails came out of testing. The model is given **denominators**
(`totals`: tool calls, shell commands, files read, files changed) because
without them it invented one — "Bash failed in 46% of turns" from 6 failures
and 13 turns. And a fix must **name something concrete**: "improve context
retention" is rejected as a finding, "add this sentence to CLAUDE.md" is not.

Sessions the model finds nothing wrong with are recorded in `session_dx_clean`,
so a re-run does not pay again for the sessions that are fine. Findings appear
on each session in `./telemetry report --view sessions` and in the TUI detail
pane, and the full report gains a **What to change** section that pools them.

That section is where findings stop being anecdotes. One finding on one session
is a story; the same fix landing in the same place across four sessions is a
backlog item, and the combined cost of those sessions is the argument for doing
it. So it leads with fixes grouped by **where the fix lives** — `claude_md`,
`skill`, `hook`, `tooling` — each with the spend behind it, then lists every
finding ranked by severity and then by what its session cost. It also states
its own coverage: how many sessions have been reviewed, and what share of total
spend that represents, so a thin sample cannot pass for a full audit.

### One caveat about the counts

Shell-derived file rows resolve relative paths against a best-guess working
directory, and when that guess is one level off the same file lands under two
repo-relative paths — `src/a/B.tsx` and `atlas-video/src/a/B.tsx`. Left
alone they read as two files each edited half as often, which is exactly the number a
diagnosis would reason from. `fold_paths()` merges them on the suffix relation
before any signal is computed. It is a heuristic; the alternative is knowingly
wrong counts.

## Correction diagnosis

Counting corrections says how often the agent missed. It does not say what to
do about it. `./telemetry enrich --what corrections` reads each correcting message alongside
**the instruction before it** and **what the agent did in between**, and
assigns a cause chosen so that every value maps to an action:

| Cause | What it means | Fix |
|---|---|---|
| `missing_context` | a project fact the agent could not infer | write it down |
| `stale_context` | information that existed but was wrong | update it |
| `wrong_approach` | had what it needed, chose a poor method | a rule or a skill |
| `ambiguous_request` | the instruction genuinely permitted that reading | how work is asked for |
| `design_iteration` | subjective refinement of creative work | **none — not a defect** |
| `tool_failure` | something broke technically | none |

Terminology counts as context: if a correction reveals that you use a name
differently from the codebase, that is a glossary gap, not ambiguity.

The output is a ranked **"what to write down"** list — the sentence to record,
where to record it, and the cost of the corrections it would have prevented.
`design_iteration` deliberately proposes nothing: taste has no right answer
knowable in advance, and scoring it as a defect would be dishonest.

Causes also appear on the correcting turn itself in the report — hover an
orange segment on an activity strip to see the cause and the proposed fix.

```bash
./telemetry enrich --what corrections --dry-run     # show one example payload, send nothing
./telemetry enrich --what corrections               # asks before sending
./telemetry enrich --what corrections --redo        # re-diagnose after changing the taxonomy
```

Diagnoses cache to `data/correction_cause.jsonl` and re-import on `analyse`;
`--redo` clears both the table and the sidecar so a verdict produced under old
rules cannot outlive the change.

## Skill trigger audit

The inventory answers "what is never used", which is too vague to act on:
a skill can be unused because nothing called for it. The useful question is
narrower — **given what a session actually did, should this available skill
have fired?**

```bash
./telemetry enrich --what skills --dry-run          # show a session summary, send nothing
./telemetry enrich --what skills                    # asks before sending
./telemetry enrich --what skills --report-only      # cached results, no model calls
./telemetry enrich --what skills --session 32baff1a # re-audit one session
```

One model call per session. It receives a compact picture of what happened —
prompts, tool counts, bash programs, MCP servers, files touched, commits,
skills that *were* used — plus the descriptions of every skill that was
available and not invoked.

Findings are shown **against the session that missed them** — a "Should have
fired" banner under each activity strip, with the skill's confidence and the
model's reasoning on hover, and a count badge on the session card. A skill
whose `SKILL.md` was read and then skipped is outlined, so it reads differently
from one that never surfaced. The ranked summary in the Skills section is the
roll-up, not the primary view.

**Read + skipped** is the column that matters. It means the agent opened that
skill's `SKILL.md`, considered it, and declined — which points at the
description rather than at discoverability, and is directly fixable.

Two precision guards, both learned from false positives:

- A skill whose `SKILL.md` was **written or edited during the session** is
  excluded. The inventory is current state with no history, so a skill authored
  mid-session would otherwise look like one that was missed.
- Verdicts are cached in `data/skill_audit.jsonl` and re-imported on `analyse`.
  `--session` drops a session's cached verdicts from **both** the database and
  the sidecar, so a verdict produced by older logic cannot outlive the fix.

Skill and MCP inventory is refreshed as part of `analyse`, since several
analyses depend on it.

## Skills and MCP inventory

Usage alone cannot tell you what *isn't* being used. Every `analyse` scans
the same configuration Claude Code reads — user, project
and plugin skill directories, and `mcpServers` in `~/.claude.json`,
`.mcp.json` and settings — then join it to observed usage.

That surfaces three things worth acting on:

- **Dead skills** — installed, never invoked. Over a month this is either a
  skill that should be deleted or one that should be firing and isn't.
- **`connected, unused` servers** — started every session, never called. Pure
  startup latency and context cost.
- **`failing` servers** — connection attempts that error, every session.

MCP `args` and `env` routinely carry tokens, so only the command's program
name is stored. A server configured in more than one scope is marked `*`; its
usage appears on each row and those are the same calls, not additional ones.

## Cost

Every figure comes from Claude Code's own `cost_usd` on the `api_request`
event. Nothing is recomputed from a price list, so nothing drifts when pricing
changes.

Aggregations available: by session, day, project, model, query source
(`main`/`subagent`/`auxiliary`), skill and agent — see the `costs` view.

**Where cost cannot be attributed precisely**, and the report says so on its
face:

- Cost belongs to an *API call*, not to a tool call. A single request that
  drove five tool calls is one cost figure. Splitting it across tools would be
  invented, so it is not attempted.
- Skill and agent attribution exists only when Claude Code stamps `skill.name`
  or `agent.name` on the request. Unstamped requests are counted as
  unattributed rather than spread around.
- Third-party skill and plugin names arrive as `custom` unless
  `OTEL_LOG_TOOL_DETAILS=1`, so they collapse into one bucket.
- Sessions that never resolve to a project keep their cost in
  `(unattributed)`.

The report's Overview states the unattributed totals in dollars, not just as a
footnote.

---

## Inspecting data by hand

```bash
./telemetry sql "select event_name, count(*) from events group by 1 order by 2 desc"
./telemetry sql "select * from costs where day = date('now')" --json
sqlite3 data/telemetry.db ".schema tool_calls"
sqlite3 data/telemetry.db -header -column "select * from session_summary"

# the raw archive is just JSON lines
tail -1 data/raw/logs.jsonl | python3 -m json.tool
grep -c api_request data/raw/logs.jsonl
```

To see every attribute of a specific event, including ones this schema does not
normalize:

```bash
./telemetry sql "select attrs_json from events where event_name='plugin_loaded' limit 1" --json
```

---

## Troubleshooting

**`telemetry status` says no telemetry received.**
Telemetry is read at Claude Code startup — `source .env.telemetry` then start a
*new* session. Confirm with `/status` inside Claude Code, or `claude --debug`
to see export errors.

**Collector will not start.**
`docker compose logs` usually says why. Port 4317 or 4318 already in use is the
common one — another collector, or Jaeger. Change the port in
`docker-compose.yml`, `.env.telemetry` and `tel/config.py`, or set
`TELEMETRY_OTLP_GRPC_PORT`.

**Running `--no-docker` and nothing arrives.**
The fallback receiver accepts `http/json` only. Run `./setup.sh --http-json`
and re-source.

**Events arrive but `analyse` finds nothing.**
Check the signal actually landed: `wc -l data/raw/*.jsonl`. If only
`metrics.jsonl` grows, `OTEL_LOGS_EXPORTER` is not set to `otlp`.

**No file paths, bash commands or skill names.**
`OTEL_LOG_TOOL_DETAILS=1` is missing, or you ran `./setup.sh --minimal`.

**No spans.**
Needs both `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and
`OTEL_TRACES_EXPORTER=otlp`.

**Everything is `(unattributed)`.**
Expected when a session did no file or bash work with absolute paths. Install
the session hooks for reliable mapping.

**A raw line failed to parse.**
The last line of a file being written can be partial. The ingester stops at the
first unparseable line and resumes there next run. `meta.last_ingest_error`
holds the detail.

**Reset everything.**
`./telemetry stop && rm -rf data/ && ./telemetry start`

---

## Assumptions, and gaps between hope and reality

Documented in full in
[`docs/anthropic-telemetry-notes.md`](docs/anthropic-telemetry-notes.md).
The short version:

- **Commit IDs are not in telemetry.** Only a counter. All hashes are local.
- **Working directory is not in telemetry** in 2.1.237, despite
  `workspace.host_paths` being documented. Project mapping leans on path
  inference or the optional hooks.
- **File creation is not in telemetry.** Inferred, with a confidence label.
- **Almost all detail is gated** behind `OTEL_LOG_TOOL_DETAILS=1`.
- **Cost attributes to API calls, not tools.** Per-tool cost is not derivable
  without inventing a split.
- **Numeric attributes arrive as strings** on events and numbers on spans;
  `bash_command` is the program name while `full_command` is the command line.
  Both handled, both worth knowing if you write your own queries.
- **SQLite, not DuckDB.** DuckDB was preferred in the brief, but it is not in
  the standard library and would have made this a `pip install` project. At
  this data scale SQLite is not the bottleneck, and DuckDB can read a SQLite
  file directly via its `sqlite_scanner` extension if you want columnar
  analytics later.
- **Delta temporality** is assumed for metrics, which is Claude Code's default.
  Switching to cumulative would make metric sums wrong.

---

## Room left for what comes next

Deliberately **not built**, but the schema is shaped so they can be added
without a migration of existing data:

- `projects.remote_normalized` is a stable join key for revenue connectors
  (Stripe, Whop) — add a `revenue` table keyed on `project_id` and a monthly
  P&L becomes a join.
- `costs` is a view, so a P&L view can sit beside it without touching ingest.
- `sessions.project_id` plus `git_activity` gives activity-to-outcome
  attribution a spine to hang off.
- `skill_calls` and `api_calls.skill_name` already support value-per-skill once
  a value signal exists.
- `sessions.organization_id` and `resource_attrs` (from
  `OTEL_RESOURCE_ATTRIBUTES`) are the hooks for team and org aggregation.
- Raw retention means a signed monthly summary can be recomputed and verified
  from source rather than trusted.

No economic value is assigned to any activity yet. The goal so far is an
accurate activity graph, and honesty about where it is inferred.
