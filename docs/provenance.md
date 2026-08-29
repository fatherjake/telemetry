# Provenance and known gaps

Where this code came from, how the port was verified, and the places where
Claude Code's telemetry does not give us what we would like.

---

## Provenance

This was a Python project until Node's standard library grew a SQLite module.
The constraint that shaped it — zero dependencies — is what had ruled Node out:
the alternative was a native SQLite build, which is exactly the install step
this project exists without. `node:sqlite` removed that obstacle, so the whole
thing was ported.

The port was not trusted, it was checked. The raw archive is the source of
truth and the database is derived from it, so both implementations were pointed
at the same frozen 1.5 GB archive and the two databases were compared row by
row:

|                   | Python            | TypeScript        |
| ----------------- | ----------------- | ----------------- |
| raw files scanned | 26                | 26                |
| log records       | 35,239            | 35,239            |
| metric points     | 31,926            | 31,926            |
| spans             | 24,032            | 24,032            |
| sessions          | 119               | 119               |
| API calls / cost  | 6,030 / $833.1720 | 6,030 / $833.1720 |
| tool calls        | 6,160             | 6,160             |
| wall clock        | 489.5s            | 205.4s            |

All 21 tables derived from the archive matched exactly — 119,516 rows,
including the JSON blob columns byte for byte, which is stricter than it
sounds: Python and JavaScript disagree by default on JSON spacing, on whether
non-ASCII is escaped, and on whether a whole-number float keeps its `.0`. All
three are reproduced deliberately, so a database written by one implementation
is indistinguishable from one written by the other, and an existing database
can simply be picked up and carried on with.

The tables left out of that count are the ones that do not come from the
archive at all: `meta` and `ingest_runs` record when the run happened, the
inventory and git caches are scans of a live machine, and
`local_session_git_context` reads the branch a checkout is sitting on right now
— which someone moved between the two runs, so the two disagree about a branch
name while agreeing on its commit.

The shell command parser was checked separately, since it is the one component
that is inference rather than transcription: all 495 distinct commands in the
archive parse to the same paths, operations and confidences, in the same order.
The MCP server was checked by driving both with the same JSON-RPC script and
comparing the raw output streams.

Four real bugs surfaced only because the comparison was done, and all four were
silent — wrong answers, not errors:

- `"'\"".includes("")` is `true` in JavaScript, so the word splitter mistook
  "inside a token" for "inside quotes" and gave up on any command with a
  closing quote followed by more content.
- `path.join` resolves `..` and keeps trailing slashes; `PurePosixPath` does
  neither, so the same file was recorded under two different paths.
- A statement left part-way through iteration holds a read transaction open,
  which stops SQLite ever checkpointing the write-ahead log.
- `JSON.stringify` refuses BigInt outright, and every nanosecond timestamp is
  one, so `telemetry_sql` failed on any `SELECT *`.

Two differences remain, both deliberate:

- **Plugin skills are found in sorted order.** Python's `Path.glob` walked
  directories in filesystem order, so which copy of a plugin skill it found
  first was not reproducible. Sorted traversal is; it happens also to prefer
  the versioned copy under `plugins/cache`, whose frontmatter parses.
- **Whole-number floats print without the `.0` in MCP output.** SQLite's
  runtime distinction between `16997` and `16997.0` is not exposed by
  `node:sqlite`, and recovering it would mean guessing at SQLite's type
  inference. It is a presentational difference in a payload read by a model.

---

## Assumptions, and gaps between hope and reality

Documented in full in
[`docs/anthropic-telemetry-notes.md`](anthropic-telemetry-notes.md).
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
- **SQLite, not DuckDB.** DuckDB is not in the standard library and would have
  made this a project with a native build step. At this data scale SQLite is not the
  bottleneck, and DuckDB can read a SQLite file directly via its
  `sqlite_scanner` extension if you want columnar analytics later.
- **Delta temporality** is assumed for metrics, which is Claude Code's default.
  Switching to cumulative would make metric sums wrong.
- **The receiver speaks OTLP/JSON only.** gRPC and protobuf would each need a
  dependency, and this project's whole shape depends on not having one.
- **Ported from Python.** This was a Python project until `node:sqlite` made a
  dependency-free Node build possible. The port was verified by running both
  implementations over the same 1.5 GB raw archive and diffing the resulting
  databases row by row; see **Provenance** at the foot of this file.
