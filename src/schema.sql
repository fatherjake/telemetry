-- Agent Telemetry - normalized store.
--
-- Design notes:
--  * The newline-delimited JSON files under ~/.telemetry/raw/ are the append-only
--    source of truth. Every normalized row keeps `raw_json` plus a
--    (source_path, source_line) pointer so old data can be re-interpreted
--    when this schema evolves.
--  * Column names mirror the attribute names in Anthropic's telemetry
--    documentation. Nothing here is invented; anything Claude Code does not
--    emit is either absent or explicitly marked as locally derived.
--  * Tables prefixed `local_` are populated from read-only git/filesystem
--    inspection on this machine, not from telemetry.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- ingest ---

-- Cursor over the append-only raw files, so `analyse` is incremental.
CREATE TABLE IF NOT EXISTS raw_files (
    path              TEXT PRIMARY KEY,
    signal            TEXT NOT NULL,          -- logs | metrics | traces
    lines_consumed    INTEGER NOT NULL DEFAULT 0,
    bytes_consumed    INTEGER NOT NULL DEFAULT 0,
    inode             INTEGER,                -- detects collector file rotation
    records_ingested  INTEGER NOT NULL DEFAULT 0,
    first_ingested_at TEXT,
    last_ingested_at  TEXT
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    files_scanned INTEGER,
    logs_ingested INTEGER,
    metrics_ingested INTEGER,
    spans_ingested   INTEGER,
    notes         TEXT
);

-- -------------------------------------------------------------- projects ---

CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT PRIMARY KEY,   -- stable hash, see gitctx.project_id()
    project_name      TEXT,
    repo_root         TEXT,               -- git rev-parse --show-toplevel
    remote_url        TEXT,               -- git remote get-url origin (credentials stripped)
    remote_normalized TEXT,               -- host/org/repo form used for the id
    is_git            INTEGER NOT NULL DEFAULT 0,
    detection_method  TEXT,               -- how the project was resolved
    first_seen        TEXT,
    last_seen         TEXT
);

-- -------------------------------------------------------------- sessions ---

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,    -- telemetry attribute session.id
    project_id       TEXT REFERENCES projects(project_id),
    first_seen       TEXT,
    last_seen        TEXT,
    duration_s       REAL,                -- derived: last_seen - first_seen
    start_type       TEXT,                -- fresh | resume | continue | agents_view
    user_id          TEXT,
    user_email       TEXT,
    account_uuid     TEXT,
    account_id       TEXT,
    organization_id  TEXT,
    app_version      TEXT,
    app_entrypoint   TEXT,
    terminal_type    TEXT,
    workspace_paths  TEXT,                -- JSON array, from workspace.host_paths
    cwd              TEXT,                -- from the SessionStart hook, if installed
    project_detection_method TEXT,        -- how THIS session was mapped to its project
    resource_attrs   TEXT                 -- JSON, OTEL_RESOURCE_ATTRIBUTES passthrough
);

-- Locally collected git context. Populated by the optional session hook or by
-- `telemetry analyse` inspecting the workspace paths seen in telemetry.
CREATE TABLE IF NOT EXISTS local_session_git_context (
    session_id  TEXT NOT NULL,
    phase       TEXT NOT NULL,            -- start | end | observed
    captured_at TEXT,
    cwd         TEXT,
    repo_root   TEXT,
    remote_url  TEXT,
    branch      TEXT,
    head_sha    TEXT,
    is_dirty    INTEGER,
    PRIMARY KEY (session_id, phase)
);

-- ---------------------------------------------------------------- events ---

-- One row per received log record. This is the generic landing table: every
-- event Claude Code emits appears here, including ones we do not normalize
-- into a purpose-built table.
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT UNIQUE,
    session_id   TEXT,
    project_id   TEXT,
    event_name   TEXT,                    -- e.g. api_request, tool_result
    ts           TEXT,
    ts_ns        INTEGER,
    sequence     INTEGER,                 -- event.sequence
    prompt_id    TEXT,                    -- prompt.id
    message_uuid TEXT,
    trace_id     TEXT,
    span_id      TEXT,
    attrs_json   TEXT,                    -- redacted attribute map
    raw_json     TEXT,                    -- verbatim record as received
    source_path  TEXT,
    source_line  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_name    ON events(event_name);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_prompt  ON events(prompt_id);

-- One row per metric data point.
CREATE TABLE IF NOT EXISTS metric_points (
    point_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT UNIQUE,
    metric_name TEXT,
    kind        TEXT,                     -- sum | gauge | histogram | ...
    unit        TEXT,
    value       REAL,
    session_id  TEXT,
    project_id  TEXT,
    ts          TEXT,
    ts_ns       INTEGER,
    attrs_json  TEXT,
    raw_json    TEXT,
    source_path TEXT,
    source_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metric_name ON metric_points(metric_name);
CREATE INDEX IF NOT EXISTS idx_metric_sess ON metric_points(session_id);

-- One row per span (tracing beta).
CREATE TABLE IF NOT EXISTS spans (
    span_row_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT UNIQUE,
    trace_id       TEXT,
    span_id        TEXT,
    parent_span_id TEXT,
    name           TEXT,
    session_id     TEXT,
    project_id     TEXT,
    start_ts       TEXT,
    end_ts         TEXT,
    start_ns       INTEGER,
    duration_ms    REAL,
    status_code    TEXT,
    tool_use_id    TEXT,
    attrs_json     TEXT,
    span_events    TEXT,                  -- span events (e.g. tool.output bodies)
    raw_json       TEXT,
    source_path    TEXT,
    source_line    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
CREATE INDEX IF NOT EXISTS idx_spans_tooluse ON spans(tool_use_id);

-- ------------------------------------------------------------- api calls ---

-- From claude_code.api_request / api_error / api_refusal events.
CREATE TABLE IF NOT EXISTS api_calls (
    api_call_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER REFERENCES events(event_id),
    dedupe_key      TEXT UNIQUE,
    session_id      TEXT,
    project_id      TEXT,
    prompt_id       TEXT,
    ts              TEXT,
    ts_ns           INTEGER,
    outcome         TEXT,                 -- ok | error | refusal
    model           TEXT,
    cost_usd        REAL,
    cost_usd_micros INTEGER,
    duration_ms     REAL,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_creation_tokens INTEGER,
    request_id        TEXT,
    client_request_id TEXT,
    speed           TEXT,
    query_source    TEXT,                 -- main | subagent | auxiliary
    effort          TEXT,
    agent_name      TEXT,
    skill_name      TEXT,
    plugin_name     TEXT,
    marketplace_name TEXT,
    mcp_server_name TEXT,
    mcp_tool_name   TEXT,
    status_code     TEXT,
    attempt         INTEGER,
    error           TEXT,
    refusal_category TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_session ON api_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_api_model   ON api_calls(model);

-- ------------------------------------------------------------ tool calls ---

-- One row per (session_id, tool_use_id), merged across the tool_decision
-- event, the tool_result event and any matching span.
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key      TEXT UNIQUE,           -- session_id|tool_use_id (or event key)
    session_id     TEXT,
    project_id     TEXT,
    prompt_id      TEXT,
    ts             TEXT,
    ts_ns          INTEGER,
    tool_use_id    TEXT,
    tool_name      TEXT,
    tool_source    TEXT,                  -- builtin | mcp | sdk_host_builtin_mcp
    tool_category  TEXT,                  -- locally derived from tool_name
    success        INTEGER,
    duration_ms    REAL,
    error_type     TEXT,
    error_message  TEXT,
    decision       TEXT,                  -- accept | reject
    decision_source TEXT,                 -- config | hook | user_* 
    tool_input_size_bytes  INTEGER,
    tool_result_size_bytes INTEGER,
    result_tokens  INTEGER,
    mcp_server_name TEXT,
    mcp_tool_name   TEXT,
    mcp_server_scope TEXT,
    skill_name     TEXT,
    subagent_type  TEXT,
    agent_id       TEXT,
    parent_agent_id TEXT,
    workflow_run_id TEXT,
    file_path      TEXT,
    bash_command   TEXT,                  -- redacted
    params_json    TEXT,                  -- every argument, credentials scrubbed
    sources        TEXT                   -- which signals contributed to this row
);
CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_cat     ON tool_calls(tool_category);

CREATE TABLE IF NOT EXISTS skill_calls (
    skill_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key     TEXT UNIQUE,
    session_id    TEXT,
    project_id    TEXT,
    ts            TEXT,
    skill_name    TEXT,
    invocation_source TEXT,               -- tool_call | api_attribution | span
    tool_use_id   TEXT,
    success       INTEGER,
    duration_ms   REAL
);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill_calls(skill_name);

CREATE TABLE IF NOT EXISTS file_activity (
    file_activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key    TEXT UNIQUE,
    session_id   TEXT,
    project_id   TEXT,
    ts           TEXT,
    tool_use_id  TEXT,
    tool_name    TEXT,
    operation    TEXT,                    -- read | edit | write | notebook_edit
    path         TEXT,
    repo_relative_path TEXT,
    file_ext     TEXT,
    success      INTEGER,
    via          TEXT,                    -- tool | shell (parsed from a command)
    op_confidence TEXT,                   -- high | medium | low (shell rows only)
    created      INTEGER,                 -- 1 = looks created, 0 = modified, NULL = unknown
    create_method TEXT,                   -- git_added | no_prior_read | unknown
    create_confidence TEXT                -- high | medium | none
);
CREATE INDEX IF NOT EXISTS idx_file_path ON file_activity(path);
CREATE INDEX IF NOT EXISTS idx_file_sess ON file_activity(session_id);

CREATE TABLE IF NOT EXISTS bash_activity (
    bash_activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key    TEXT UNIQUE,
    session_id   TEXT,
    project_id   TEXT,
    ts           TEXT,
    tool_use_id  TEXT,
    command      TEXT,                    -- redacted
    command_hash TEXT,
    program      TEXT,                    -- primary program, ignoring cd/env prefixes
    programs     TEXT,                    -- JSON array of every program in the pipeline
    success      INTEGER,
    duration_ms  REAL,
    error_type   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bash_prog ON bash_activity(program);

CREATE TABLE IF NOT EXISTS subagent_activity (
    subagent_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key     TEXT UNIQUE,
    session_id    TEXT,
    project_id    TEXT,
    ts            TEXT,
    subagent_type TEXT,
    agent_name    TEXT,
    tool_use_id   TEXT,
    agent_id      TEXT,
    parent_agent_id TEXT,
    workflow_run_id TEXT,
    success       INTEGER,
    duration_ms   REAL,
    source        TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT UNIQUE,
    session_id    TEXT,
    project_id    TEXT,
    prompt_id     TEXT,
    message_uuid  TEXT,
    ts            TEXT,
    prompt_length INTEGER,
    command_name  TEXT,
    command_source TEXT,
    prompt_text   TEXT                    -- the prompt, verbatim, credentials scrubbed
);

CREATE TABLE IF NOT EXISTS responses (
    response_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT UNIQUE,
    session_id      TEXT,
    project_id      TEXT,
    prompt_id       TEXT,
    message_uuid    TEXT,
    ts              TEXT,
    response_length INTEGER,
    model           TEXT,
    request_id      TEXT,
    query_source    TEXT,
    response_text   TEXT                  -- the response, verbatim, credentials scrubbed
);

-- --------------------------------------------------------------- errors ----

CREATE TABLE IF NOT EXISTS errors (
    error_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key  TEXT UNIQUE,
    session_id  TEXT,
    project_id  TEXT,
    ts          TEXT,
    kind        TEXT,                     -- api_error | api_refusal | tool_failure | internal_error | mcp_connection
    source_event TEXT,
    model       TEXT,
    tool_name   TEXT,
    error_name  TEXT,
    error_code  TEXT,
    status_code TEXT,
    message     TEXT                      -- redacted; only present when the doc says it is exported
);

-- ------------------------------------------------------------ git ---------

-- Commits reconciled locally with read-only git commands. Claude Code's
-- telemetry emits only a commit *count* metric, never commit ids, so the
-- hashes here always come from the local repository.
CREATE TABLE IF NOT EXISTS git_activity (
    git_activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT UNIQUE,
    project_id   TEXT,
    commit_sha   TEXT,
    committed_at TEXT,
    author_name  TEXT,
    author_email TEXT,
    branch       TEXT,
    subject      TEXT,                    -- redacted commit subject line
    files_changed INTEGER,
    insertions   INTEGER,
    deletions    INTEGER,
    session_id   TEXT,                    -- best-effort: session active at commit time
    attribution  TEXT,                    -- changed_files | session_time_window | none
    source       TEXT,                    -- local_git_reconcile
    commit_type  TEXT,                    -- conventional-commit type: feat, fix, ...
    commit_scope TEXT                     -- conventional-commit scope: atlas-video, ...
);
CREATE INDEX IF NOT EXISTS idx_git_project ON git_activity(project_id);

CREATE TABLE IF NOT EXISTS git_commit_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha  TEXT,
    project_id  TEXT,
    path        TEXT,
    change_type TEXT,                     -- A | M | D | R | C
    insertions  INTEGER,
    deletions   INTEGER,
    UNIQUE(commit_sha, path)
);

-- Reverts are the clearest negative signal available: work that had to be
-- undone. Detected from commit subjects.
CREATE TABLE IF NOT EXISTS reverts (
    revert_sha    TEXT PRIMARY KEY,
    project_id    TEXT,
    reverted_sha  TEXT,
    detected_at   TEXT,
    method        TEXT
);

-- ------------------------------------------------------------- turns ------

-- One row per prompt.id: a single human turn and everything it caused.
-- This is the grain at which human effort is visible - a session is too
-- coarse, an event too fine.
CREATE TABLE IF NOT EXISTS turns (
    turn_id        TEXT PRIMARY KEY,      -- prompt.id
    session_id     TEXT,
    project_id     TEXT,
    seq            INTEGER,               -- position within the session
    started_at     TEXT,
    ended_at       TEXT,
    duration_s     REAL,
    gap_before_s   REAL,                  -- human think/review time before this turn
    prompt_length  INTEGER,
    prompt_text    TEXT,                  -- only when content storage is on
    is_steering    INTEGER,               -- resolved label: steering nudge
    is_correction  INTEGER,               -- resolved label: correction
    correction_cue TEXT,                  -- heuristic cue, or the model's rationale
    label_source   TEXT,                  -- model | heuristic
    label_confidence TEXT,
    is_system      INTEGER DEFAULT 0,     -- injected by the harness, not typed
    api_calls      INTEGER DEFAULT 0,
    cost_usd       REAL DEFAULT 0,
    tool_calls     INTEGER DEFAULT 0,
    tool_failures  INTEGER DEFAULT 0,
    rejects        INTEGER DEFAULT 0,     -- decision='reject'
    user_overrides INTEGER DEFAULT 0,     -- user_reject / user_abort
    files_touched  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

-- Files revisited across separate turns. Repeatedly returning to the same
-- file is the clearest available proxy for the agent not getting it right
-- first time.
CREATE TABLE IF NOT EXISTS file_rework (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    project_id   TEXT,
    path         TEXT,
    repo_relative_path TEXT,
    turns        INTEGER,                 -- distinct turns that touched it
    edits        INTEGER,
    first_ts     TEXT,
    last_ts      TEXT,
    UNIQUE(session_id, path)
);

-- ------------------------------------------------- skill / MCP inventory --

-- What is installed, as opposed to what gets used. Populated by scanning
-- skill directories and MCP config files; usage comes from telemetry.
CREATE TABLE IF NOT EXISTS skill_inventory (
    skill_id    TEXT PRIMARY KEY,         -- scope:name
    name        TEXT,
    scope       TEXT,                     -- user | project | plugin
    project_id  TEXT,
    source_path TEXT,
    description TEXT,
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS mcp_inventory (
    mcp_id      TEXT PRIMARY KEY,         -- scope:name
    name        TEXT,
    scope       TEXT,                     -- user | project
    project_id  TEXT,
    config_path TEXT,
    transport   TEXT,
    command     TEXT,                     -- redacted; args/env never stored
    discovered_at TEXT
);

-- ------------------------------------------------- turn classification ----


-- ---------------------------------------------------- skill trigger audit --


-- ------------------------------------------------- correction diagnosis ---


-- ------------------------------------------------- project back-fill ------

-- A record cannot know its project when it is ingested: a session is mapped
-- to a repository by the *weight* of the paths it referenced, so a row from
-- the first batch does not know what the fortieth will decide. project_id is
-- therefore stamped afterwards, by `_propagate_project_ids`.
--
-- These are PARTIAL indexes, and that is the whole point: a row enters the
-- index when it is written with a NULL project_id and leaves it the moment
-- the back-fill assigns one. In the steady state - everything assigned -
-- they are empty, so the back-fill probes an empty index instead of scanning
-- every row of twelve tables looking for work that is not there. Measured on
-- a 90k-row database: 2.50s -> 0.009s.

CREATE INDEX IF NOT EXISTS idx_events_unassigned ON events(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_metric_points_unassigned ON metric_points(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_spans_unassigned ON spans(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_calls_unassigned ON api_calls(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_tool_calls_unassigned ON tool_calls(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_skill_calls_unassigned ON skill_calls(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_file_activity_unassigned ON file_activity(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_bash_activity_unassigned ON bash_activity(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_subagent_activity_unassigned ON subagent_activity(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_prompts_unassigned ON prompts(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_responses_unassigned ON responses(session_id) WHERE project_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_errors_unassigned ON errors(session_id) WHERE project_id IS NULL;

-- The same trick for the one path lookup that is not session-derived: a file
-- can live outside its session's repo, so each distinct path is resolved once
-- and remembered. Paths that resolve to no repository at all (`../../x.js`,
-- unresolvable relative paths) stay NULL and stay in the index - there are a
-- hundred or so and they cost nothing, because the directory->repo answers
-- are cached in git_dir_cache.
CREATE INDEX IF NOT EXISTS idx_file_activity_unmapped ON file_activity(path) WHERE repo_relative_path IS NULL;


-- ----------------------------------------------------------------- views ---

-- Cost and tokens rolled up per session. `costs` is a view rather than a
-- table so it can never drift from api_calls.
CREATE VIEW IF NOT EXISTS costs AS
SELECT
    a.session_id,
    a.project_id,
    substr(a.ts, 1, 10)                       AS day,
    a.model,
    a.query_source,
    a.skill_name,
    a.agent_name,
    COUNT(*)                                  AS api_calls,
    SUM(COALESCE(a.cost_usd, 0))              AS cost_usd,
    SUM(COALESCE(a.input_tokens, 0))          AS input_tokens,
    SUM(COALESCE(a.output_tokens, 0))         AS output_tokens,
    SUM(COALESCE(a.cache_read_tokens, 0))     AS cache_read_tokens,
    SUM(COALESCE(a.cache_creation_tokens, 0)) AS cache_creation_tokens,
    SUM(COALESCE(a.duration_ms, 0))           AS duration_ms
FROM api_calls a
WHERE a.outcome = 'ok'
GROUP BY a.session_id, a.project_id, day, a.model, a.query_source, a.skill_name, a.agent_name;

CREATE VIEW IF NOT EXISTS session_summary AS
SELECT
    s.session_id,
    s.project_id,
    p.project_name,
    s.project_detection_method,
    s.first_seen,
    s.last_seen,
    s.duration_s,
    s.app_version,
    s.terminal_type,
    (SELECT COUNT(*) FROM api_calls a WHERE a.session_id = s.session_id AND a.outcome='ok') AS api_calls,
    (SELECT COALESCE(SUM(cost_usd),0) FROM api_calls a WHERE a.session_id = s.session_id AND a.outcome='ok') AS cost_usd,
    (SELECT COALESCE(SUM(input_tokens),0) FROM api_calls a WHERE a.session_id = s.session_id) AS input_tokens,
    (SELECT COALESCE(SUM(output_tokens),0) FROM api_calls a WHERE a.session_id = s.session_id) AS output_tokens,
    (SELECT COALESCE(SUM(cache_read_tokens),0) FROM api_calls a WHERE a.session_id = s.session_id) AS cache_read_tokens,
    (SELECT COALESCE(SUM(cache_creation_tokens),0) FROM api_calls a WHERE a.session_id = s.session_id) AS cache_creation_tokens,
    (SELECT COUNT(*) FROM tool_calls t WHERE t.session_id = s.session_id) AS tool_calls,
    (SELECT COUNT(*) FROM skill_calls k WHERE k.session_id = s.session_id) AS skill_calls,
    (SELECT COUNT(*) FROM file_activity f WHERE f.session_id = s.session_id AND f.operation='read') AS files_read,
    (SELECT COUNT(*) FROM file_activity f WHERE f.session_id = s.session_id AND f.operation IN ('edit','write','notebook_edit')) AS files_changed,
    (SELECT COUNT(*) FROM bash_activity b WHERE b.session_id = s.session_id) AS bash_commands,
    (SELECT COUNT(*) FROM subagent_activity g WHERE g.session_id = s.session_id) AS subagents,
    (SELECT COUNT(*) FROM errors e WHERE e.session_id = s.session_id) AS errors
FROM sessions s
LEFT JOIN projects p ON p.project_id = s.project_id;

CREATE VIEW IF NOT EXISTS project_summary AS
SELECT
    p.project_id,
    p.project_name,
    p.repo_root,
    p.remote_normalized,
    (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.project_id) AS sessions,
    (SELECT COALESCE(SUM(cost_usd),0) FROM api_calls a WHERE a.project_id = p.project_id AND a.outcome='ok') AS cost_usd,
    (SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),0)
       FROM api_calls a WHERE a.project_id = p.project_id) AS tokens,
    (SELECT COUNT(*) FROM tool_calls t WHERE t.project_id = p.project_id) AS tool_calls,
    (SELECT COUNT(*) FROM skill_calls k WHERE k.project_id = p.project_id) AS skill_calls,
    (SELECT COUNT(*) FROM file_activity f WHERE f.project_id = p.project_id AND f.operation='read') AS files_read,
    (SELECT COUNT(*) FROM file_activity f WHERE f.project_id = p.project_id AND f.operation IN ('edit','write','notebook_edit')) AS files_changed,
    (SELECT COUNT(*) FROM git_activity g WHERE g.project_id = p.project_id) AS commits
FROM projects p;

-- --------------------------------------------------- session diagnosis ---


-- ------------------------------------------------------- session chat ----


-- ------------------------------------------------------- git path cache ----

-- `git log --diff-filter=A --follow` is the expensive part of classifying a
-- write as create-vs-modify, and shell-derived file activity is rebuilt on
-- every run, so without a cache the same paths are probed again every time.
-- A found add-commit never changes and is kept for good. "No add commit yet"
-- is only true of today, so it is re-checked after a few hours; until then
-- the write keeps its weaker no-prior-read classification.
CREATE TABLE IF NOT EXISTS git_first_added (
    repo_root  TEXT NOT NULL,
    path       TEXT NOT NULL,
    added_at   TEXT,                     -- null: git knew of no add commit
    checked_at TEXT,
    PRIMARY KEY (repo_root, path)
);

-- Which repository a directory belongs to. Resolving this costs a `git`
-- subprocess, and attribution asks the same question of the same directories
-- on every run. A "not a repository" answer is only true of today - a
-- directory can be `git init`ed later - so negatives are re-checked after a
-- day, while positives are kept.
CREATE TABLE IF NOT EXISTS git_dir_cache (
    dir        TEXT PRIMARY KEY,
    is_git     INTEGER,
    repo_root  TEXT,
    remote_url TEXT,
    checked_at TEXT
);

-- Sessions deliberately removed from the database. The collector batches, so
-- events for a session can arrive *after* it was purged - without this the
-- next analyse would quietly re-create it, which is how four of the
-- self-test's synthetic sessions ended up back in a real database.
CREATE TABLE IF NOT EXISTS purged_sessions (
    session_id TEXT PRIMARY KEY,
    reason     TEXT,
    purged_at  TEXT
);
