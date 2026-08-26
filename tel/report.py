"""Self-contained local HTML report.

No external assets, no network calls, no JavaScript libraries - the file can
be opened straight from disk and works offline.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import sqlite3
from pathlib import Path

from . import config, queries as Q

CSS = """
:root{
  --bg:#fbfbfa; --panel:#ffffff; --ink:#1f1e1c; --muted:#6b6862;
  --line:#e6e3dd; --accent:#c25a2a; --accent-soft:#f2e4dc;
  --ok:#3f7d58; --warn:#a8781f; --bad:#b4432c;
  --bar:#d8d3ca;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#171614; --panel:#201f1c; --ink:#ece9e3; --muted:#9a958c;
    --line:#33312c; --accent:#e08757; --accent-soft:#33251d;
    --ok:#71b088; --warn:#d3ad5f; --bad:#e08c76; --bar:#3a3833;
  }
}
:root[data-theme="dark"]{
  --bg:#171614; --panel:#201f1c; --ink:#ece9e3; --muted:#9a958c;
  --line:#33312c; --accent:#e08757; --accent-soft:#33251d;
  --ok:#71b088; --warn:#d3ad5f; --bad:#e08c76; --bar:#3a3833;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Inter,Helvetica,Arial,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:40px 24px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:19px;margin:44px 0 4px;letter-spacing:-.01em}
h3{font-size:14px;margin:26px 0 8px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.sub{color:var(--muted);font-size:13.5px;margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:18px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tile .v{font-size:23px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.tile .n{font-size:12px;color:var(--muted);margin-top:2px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel);margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
   position:sticky;top:0;background:var(--panel)}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap-cell{white-space:normal;max-width:520px}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--accent-soft);
  color:var(--accent);font-size:11.5px;font-weight:600}
.muted{color:var(--muted)}
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:13px 16px;margin:14px 0;font-size:13.5px}
.bar{position:relative;background:var(--bar);border-radius:3px;height:7px;min-width:44px}
.bar>i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:3px;display:block}
footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
details{margin:10px 0}
summary{cursor:pointer;color:var(--accent);font-size:13.5px}
.empty{color:var(--muted);font-size:13.5px;padding:10px 2px}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def num(v, dp=0) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return esc(v)


def usd(v) -> str:
    """Cents by default; extra precision only where cents would read as zero."""
    if v is None:
        return "—"
    v = float(v)
    return f"${v:,.4f}" if 0 < v < 0.01 else f"${v:,.2f}"


def dur(seconds) -> str:
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.0f}m"
    return f"{s/3600:.1f}h"


def short_ts(v) -> str:
    if not v:
        return "—"
    return str(v)[:19].replace("T", " ")


def table(headers: list, rows: list[list], numeric: set[int] | None = None,
          wrap_cols: set[int] | None = None, empty: str = "No data yet.") -> str:
    numeric = numeric or set()
    wrap_cols = wrap_cols or set()
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    th = "".join(
        f'<th class="num">{esc(h)}</th>' if i in numeric else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers))
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            cls = "num" if i in numeric else ("wrap-cell" if i in wrap_cols else "")
            cell = c if isinstance(c, str) and c.startswith("<") else esc(c)
            tds.append(f'<td class="{cls}">{cell}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<div class="scroll"><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def bar(value: float, maximum: float) -> str:
    pct = 0 if not maximum else max(2, min(100, value / maximum * 100))
    return f'<span class="bar"><i style="width:{pct:.1f}%"></i></span>'


def tile(k: str, v: str, note: str = "") -> str:
    n = f'<div class="n">{esc(note)}</div>' if note else ""
    return f'<div class="tile"><div class="k">{esc(k)}</div><div class="v">{v}</div>{n}</div>'


# ------------------------------------------------------------------ sections --

def _overview(conn) -> str:
    o = Q.overview(conn)
    total_tokens = (o["input_tokens"] + o["output_tokens"]
                    + o["cache_read_tokens"] + o["cache_creation_tokens"])
    tiles = "".join([
        tile("Sessions", num(o["sessions"])),
        tile("Claude cost", usd(o["cost_usd"]), "sum of api_request cost_usd"),
        tile("API calls", num(o["api_calls"]), f'{o["api_failures"]} failed'),
        tile("Total tokens", num(total_tokens)),
        tile("Tool calls", num(o["tool_calls"])),
        tile("Skills invoked", num(o["skill_calls"])),
        tile("Files read", num(o["files_read"]), f'{o["distinct_files_read"]} distinct'),
        tile("Files modified", num(o["files_changed"]), f'{o["distinct_files_changed"]} distinct'),
        tile("Files created", num(o["files_created"]), "derived, see method"),
        tile("Commands executed", num(o["bash_commands"])),
        tile("Subagent runs", num(o["subagents"])),
        tile("Errors", num(o["errors"])),
        tile("Commits", num(o["commits"]), "reconciled from local git"),
        tile("Projects", num(o["projects"])),
    ])
    tokens = table(
        ["Token type", "Tokens"],
        [["Input", num(o["input_tokens"])],
         ["Output", num(o["output_tokens"])],
         ["Cache read", num(o["cache_read_tokens"])],
         ["Cache creation", num(o["cache_creation_tokens"])]],
        numeric={1})

    ua = Q.unattributed_cost(conn)
    pct = (ua["no_project"] / ua["total"] * 100) if ua["total"] else 0
    caveat = (
        f'<div class="note"><b>Attribution caveats.</b> '
        f'{usd(ua["no_project"])} of {usd(ua["total"])} ({pct:.1f}%) could not be tied to a git project. '
        f'{usd(ua["no_skill"])} was spent on API calls carrying no <code>skill.name</code> attribute, so it '
        f'cannot be attributed to a skill. {ua["no_cost_field"]} successful API calls carried no '
        f'<code>cost_usd</code> attribute at all.</div>')

    return (f'<h2 id="overview">Overview</h2>'
            f'<p class="sub">Observation period '
            f'<span class="mono">{esc(short_ts(o["period_start"]))}</span> → '
            f'<span class="mono">{esc(short_ts(o["period_end"]))}</span> (UTC)</p>'
            f'<div class="grid">{tiles}</div>{caveat}'
            f'<h3>Token usage</h3>{tokens}')


def _projects(conn) -> str:
    rows = Q.projects(conn)
    mx = max([r["cost_usd"] or 0 for r in rows], default=0)
    out = []
    for r in rows:
        out.append([
            r["project_name"] or "—",
            r["remote_normalized"] or (r["repo_root"] or "—"),
            num(r["sessions"]), usd(r["cost_usd"]), bar(r["cost_usd"] or 0, mx),
            num(r["tokens"]), num(r["tool_calls"]), num(r["skill_calls"]),
            num(r["files_read"]), num(r["files_changed"]), num(r["commits"]),
        ])
    return ('<h2 id="projects">Projects</h2>'
            '<p class="sub">Grouped by git repository. Sessions with no resolvable '
            'workspace path fall into <code>(unattributed)</code>.</p>'
            + table(["Project", "Repo / remote", "Sessions", "Claude cost", "",
                     "Tokens", "Tool calls", "Skills", "Files read", "Files changed", "Commits"],
                    out, numeric={2, 3, 5, 6, 7, 8, 9, 10}))


def _sessions_table(conn) -> str:
    rows = Q.session_detail(conn)
    mx = max([d["session"]["cost"] or 0 for d in rows], default=0)
    out = []
    for d in rows:
        r, e = d["session"], d["effort"]
        live = ("<span class='pill'>live</span>" if r["prod_deploys"]
                else "<span class='muted'>—</span>")
        out.append([
            esc(r["title"]), short_ts(r["first_seen"]),
            esc(r["project_name"] or "—"),
            num(e["turns"]), num(r["commits"]),
            f'<span class="ok">+{r["insertions"]}</span> '
            f'<span class="bad">-{r["deletions"]}</span>',
            usd(r["cost"]), bar(r["cost"] or 0, mx),
            f'<span class="{"bad" if e["corrections"] else "muted"}">'
            f'{e["corrections"]}</span>',
            live,
            f'<span class="{"bad" if r["reverted"] else "muted"}">{r["reverted"] or 0}</span>',
        ])
    cov = Q.deploy_coverage(conn)
    deploys = Q.deployments(conn)
    drows = [[d["provider"], d["environment"], short_ts(d["created_at"]),
              f'<span class="mono">{esc((d["commit_sha"] or "—")[:8])}</span>',
              d["scope"] or "—", (d["subject"] or "")[:70]] for d in deploys]

    note = ('<div class="note"><b>The session is the unit.</b> One session is one '
            'piece of work, named by what it set out to do. Nothing here is '
            'apportioned: the cost is what that session\'s own API calls cost, and '
            'the commits are the ones made during it. Deployments come from GitHub '
            'and EAS and are joined on commit SHA. Newest first.</div>')
    return ('<h2 id="sessions">Sessions</h2>' + note
            + table(["Session", "Started", "Project", "Turns", "Commits", "Lines",
                     "Cost", "", "Corrections", "Shipped", "Reverted"],
                    out, numeric={3, 4, 6, 8, 10},
                    empty="No sessions observed yet.")
            + f'<p class="sub">{cov["total"]} deployments recorded, '
              f'{cov["matched_to_git"]} matched to an observed commit · '
              f'{cov["prs"]} pull requests · {cov["reverts"]} reverts · '
              f'{cov["outcomes"]} outcome records.</p>'
            + "<h3>Recent deployments</h3>"
            + table(["Provider", "Environment", "When", "Commit", "Scope", "Subject"],
                    drows, wrap_cols={5}, empty="No deployments. Run ./telemetry config connect."))


def _sessions(conn, limit=50) -> str:
    rows = Q.sessions(conn, limit)
    out = []
    for r in rows:
        sid = r["session_id"]
        commits = Q.session_commits(conn, sid)
        out.append([
            f'<span class="mono">{esc(sid[:8])}</span>',
            short_ts(r["first_seen"]), dur(r["duration_s"]),
            r["project_name"] or "—",
            f'<span class="mono">{esc(Q.session_models(conn, sid))}</span>',
            usd(r["cost_usd"]),
            num((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                + (r["cache_read_tokens"] or 0) + (r["cache_creation_tokens"] or 0)),
            num(r["api_calls"]), num(r["tool_calls"]), num(r["skill_calls"]),
            num((r["files_read"] or 0) + (r["files_changed"] or 0)),
            num(r["bash_commands"]), num(r["subagents"]),
            f'<span class="{"bad" if r["errors"] else "muted"}">{r["errors"]}</span>',
            f'<span class="mono">{esc(", ".join(commits)) or "—"}</span>',
        ])
    return ('<h2 id="sessions">Sessions</h2>'
            '<p class="sub">Most recent first. Commit ids come from local git '
            'reconciliation, not from telemetry.</p>'
            + table(["Session", "Start (UTC)", "Duration", "Project", "Model(s)", "Cost",
                     "Tokens", "API", "Tools", "Skills", "Files", "Cmds", "Subagents",
                     "Errors", "Commits"],
                    out, numeric={5, 6, 7, 8, 9, 10, 11, 12, 13}))


def _effort(conn) -> str:
    t = Q.friction_totals(conn)
    if not t["turns"]:
        return ('<h2 id="effort">Human effort</h2>'
                '<p class="empty">No turns recorded yet.</p>')
    pct = lambda n: (n / t["turns"] * 100) if t["turns"] else 0
    share = (t["correction_cost"] / t["total_cost"] * 100) if t["total_cost"] else 0
    tiles = "".join([
        tile("Turns", num(t["turns"]), "one per prompt"),
        tile("Corrections", num(t["corrections"]), f'{pct(t["corrections"]):.0f}% of turns'),
        tile("Steering nudges", num(t["steers"]), f'{pct(t["steers"]):.0f}% of turns'),
        tile("Tool rejects", num(t["rejects"])),
        tile("User overrides", num(t["overrides"]), "user_reject / user_abort"),
        tile("Reworked files", num(t["rework_files"]), "touched in >1 turn"),
        tile("Correction cost", usd(t["correction_cost"]), f'{share:.0f}% of spend'),
    ])
    note = (f'<div class="note"><b>Labels come from a model where possible.</b> '
            f'{t["model_labelled"]} turn(s) were classified by a small model reading each '
            'prompt in the context of the previous one; the rest fall back to a length '
            'heuristic because their prompt text predates content storage. '
            f'{t["system_turns"]} harness-injected prompts (monitor notifications, system '
            f'reminders) are excluded from these counts \u2014 they cost '
            f'{usd(t["system_cost"])} but nobody typed them. <i>Rework</i> is the same '
            'file touched in more than one turn, and undercounts when files are edited '
            'through shell commands rather than the Edit tool.</div>')
    rows = [[r["session_id"][:8], r["project_name"] or "—", num(r["turns"]),
             num(r["corrections"]), num(r["steers"]), num(r["rejects"] or 0),
             num(r["tool_failures"] or 0), num(r["rework_files"] or 0),
             usd(r["cost_usd"]), f'{int(r["avg_gap_s"] or 0)}s']
            for r in Q.friction_by_session(conn, 25)]
    corr = [[c["sess"], short_ts(c["started_at"]),
             f'<code>{esc(c["correction_cue"] or "—")}</code>',
             esc(c["prompt"] or ""), usd(c["cost_usd"])]
            for c in Q.corrections(conn, 15)]
    rework = [[r["path"], num(r["turns"]), num(r["edits"]), r["sess"]]
              for r in Q.rework_files(conn, 15)]
    return ('<h2 id="effort">Human effort</h2>'
            '<p class="sub">How much steering the work needed, as a proxy for how '
            'often the agent missed.</p>'
            f'<div class="grid">{tiles}</div>' + note
            + "<h3>By session</h3>"
            + table(["Session", "Project", "Turns", "Corrections", "Steers", "Rejects",
                     "Tool failures", "Rework", "Cost", "Avg gap"], rows,
                    numeric={2, 3, 4, 5, 6, 7, 8})
            + "<h3>Correction turns</h3>"
            + table(["Session", "When", "Cue", "Prompt", "Cost"], corr,
                    numeric={4}, wrap_cols={3},
                    empty="No correction language detected.")
            + "<h3>Files returned to across turns</h3>"
            + table(["File", "Turns", "Edits", "Session"], rework, numeric={1, 2},
                    wrap_cols={0}, empty="No file revisited across turns."))


def _inventory(conn) -> str:
    skills = Q.skill_usage(conn)
    used = [r for r in skills if r["invocations"]]
    unused = [r for r in skills if not r["invocations"]]
    orphan = Q.skills_used_not_installed(conn)

    used_rows = [[r["name"], r["scope"], num(r["invocations"]), num(r["sessions"]),
                  short_ts(r["last_used"])] for r in used]
    orphan_rows = [[r["skill_name"], num(r["invocations"]),
                    short_ts(r["last_used"])] for r in orphan]
    unused_rows = [[r["name"], r["scope"], (r["description"] or "")[:120]]
                   for r in unused if r["scope"] != "plugin"]

    mcp = Q.mcp_usage(conn)
    mcp_rows = []
    for r in mcp:
        if r["calls"]:
            state = '<span class="pill">used</span>'
        elif r["failures"]:
            state = '<span class="bad">failing</span>'
        elif r["connects"]:
            state = '<span class="warn">connected, unused</span>'
        else:
            state = '<span class="muted">never seen</span>'
        mcp_rows.append([r["name"], r["scope"], r["transport"] or "—",
                         num(r["calls"]), num(r["distinct_tools"]),
                         num(r["connects"]), num(r["failures"]), state,
                         short_ts(r["last_used"])])

    note = ('<div class="note">Inventory is what is installed; usage is what telemetry '
            'saw. A skill only registers as used when Claude Code reports its name, which '
            'needs <code>OTEL_LOG_TOOL_DETAILS=1</code>. Over a short window "never used" '
            'means little; over a month it is a signal. A server marked '
            '<b>connected, unused</b> is started every session and never called - it costs '
            'startup time and context for nothing. A name marked <code>*</code> is '
            'configured in more than one scope, so its usage appears on each row; those '
            'are the same calls, not additional ones.</div>')

    return ('<h2 id="inventory">Skills &amp; MCP inventory</h2>' + note
            + f"<h3>Skills used — {len(used)} of {len(skills)} installed</h3>"
            + table(["Skill", "Scope", "Invocations", "Sessions", "Last used"],
                    used_rows, numeric={2, 3}, empty="No skill usage observed.")
            + "<h3>Used but not found on disk</h3>"
            + table(["Skill", "Invocations", "Last used"], orphan_rows, numeric={1},
                    empty="None.")
            + f"<h3>Never used — {len(unused)} installed, {len(unused_rows)} outside plugins</h3>"
            + table(["Skill", "Scope", "Description"], unused_rows, wrap_cols={2},
                    empty="Every installed skill has been used.")
            + "<h3>MCP servers</h3>"
            + table(["Server", "Scope", "Transport", "Calls", "Tools", "Connects",
                     "Failures", "State", "Last used"], mcp_rows,
                    numeric={3, 4, 5, 6}, empty="No MCP servers configured."))


def _skills(conn) -> str:
    rows = Q.skills(conn)
    out = [[r["skill_name"], num(r["invocations"]), num(r["sessions"]),
            num(r["projects"]),
            usd(r["attributed_cost_usd"]) if r["attributed_api_calls"] else "—",
            num(r["attributed_api_calls"]) or "0"] for r in rows]
    note = ('<div class="note">A <b>skill</b> is something invoked through the '
            '<code>Skill</code> tool or stamped on an API request as '
            '<code>skill.name</code>. <b>MCP servers are not skills</b> and are counted '
            'separately, in the Tools section \u2014 so a server you called 30 times will '
            'not appear here. A skill and an MCP server may legitimately share a name. '
            'Cost is attributable to a skill only when Claude Code stamps '
            '<code>skill.name</code> on the <code>api_request</code> event; skills whose '
            'name is redacted appear as <code>custom</code> unless '
            '<code>OTEL_LOG_TOOL_DETAILS=1</code> is set.</div>')
    return ('<h2 id="skills">Skills</h2>' + note
            + table(["Skill", "Invocations", "Sessions", "Projects",
                     "Attributed cost", "Attributed API calls"], out, numeric={1, 2, 3, 4, 5},
                    empty="No skill invocations observed."))


def _tools(conn) -> str:
    cats = Q.tool_categories(conn)
    mx = max([c["calls"] for c in cats], default=0)
    cat_rows = [[c["tool_category"], num(c["calls"]), bar(c["calls"], mx),
                 num(c["sessions"]), num(c["total_s"], 1) + " s"] for c in cats]
    detail = Q.tools_breakdown(conn)
    det_rows = [[d["tool_category"], d["tool_name"] or "—", num(d["calls"]),
                 num(d["ok"]), f'<span class="{"bad" if d["failed"] else "muted"}">{d["failed"] or 0}</span>',
                 num(d["avg_ms"], 1), num(d["total_s"], 1)] for d in detail]
    servers = Q.mcp_servers(conn)
    mx_mcp = max([m["calls"] for m in servers], default=0)
    srv_rows = [[m["server"], num(m["calls"]), bar(m["calls"], mx_mcp),
                 num(m["distinct_tools"]), num(m["sessions"]),
                 f'<span class="{"bad" if m["failed"] else "muted"}">{m["failed"] or 0}</span>',
                 num(m["avg_ms"], 1), num(m["total_s"], 1)] for m in servers]
    mtools = Q.mcp_tools(conn)
    mt_rows = [[m["server"], m["tool"], num(m["calls"]),
                f'<span class="{"bad" if m["failed"] else "muted"}">{m["failed"] or 0}</span>',
                num(m["avg_ms"], 1)] for m in mtools]
    subs = Q.subagents(conn)
    sub_rows = [[s["agent"], num(s["invocations"]), num(s["sessions"]), num(s["avg_s"], 1)]
                for s in subs]
    return ('<h2 id="tools">Tools</h2>'
            '<p class="sub">Categories are derived locally from the observed '
            '<code>tool_name</code>; Claude Code does not emit a category attribute.</p>'
            + table(["Category", "Calls", "", "Sessions", "Total duration"], cat_rows,
                    numeric={1, 3, 4})
            + "<h3>By tool</h3>"
            + table(["Category", "Tool", "Calls", "OK", "Failed", "Avg ms", "Total s"],
                    det_rows, numeric={2, 3, 4, 5, 6})
            + "<h3>MCP servers</h3>"
            + '<div class="note">Claude Code reports every MCP invocation with the '
              'literal <code>tool_name</code> <code>mcp_tool</code>; the server and '
              'tool names arrive in <code>tool_parameters</code> and need '
              '<code>OTEL_LOG_TOOL_DETAILS=1</code>. An MCP server is <b>not</b> a '
              'skill \u2014 skills appear in the Skills section above, and the two are '
              'counted separately even when a server and a skill share a name.</div>'
            + table(["Server", "Calls", "", "Distinct tools", "Sessions", "Failed",
                     "Avg ms", "Total s"], srv_rows, numeric={1, 3, 4, 5, 6, 7},
                    empty="No MCP activity observed.")
            + "<h3>MCP tools</h3>"
            + table(["Server", "Tool", "Calls", "Failed", "Avg ms"], mt_rows,
                    numeric={2, 3, 4}, empty="No MCP activity observed.")
            + "<h3>Subagents</h3>"
            + table(["Agent", "Invocations", "Sessions", "Avg duration (s)"], sub_rows,
                    numeric={1, 2, 3}, empty="No subagent activity observed."))


def _files(conn) -> str:
    hot = Q.hot_files(conn)
    hot_rows = []
    for f in hot:
        flag = ("<span class='pill'>created</span>" if f["created"] == 1 else "")
        hot_rows.append([f["repo_relative_path"] or f["path"], f["project_name"],
                         num(f["touches"]), num(f["reads"]), num(f["writes"]),
                         num(f["sessions"]), flag])
    created = Q.created_files(conn)
    cre_rows = [[c["path"], short_ts(c["ts"]), c["create_method"],
                 c["create_confidence"]] for c in created]
    dirs = Q.hot_dirs(conn)
    mx = max([d["touches"] for d in dirs], default=0)
    dir_rows = [[d["project_name"], d["area"], num(d["touches"]),
                 bar(d["touches"], mx), num(d["files"])] for d in dirs]
    note = ('<div class="note"><b>Created vs modified is inferred, not reported.</b> '
            'Claude Code telemetry never says whether a Write created a file. '
            '<code>git_added</code> (high confidence) means the file\'s first "A" commit '
            'in the repo is at or after the session start; <code>no_prior_read</code> '
            '(medium) means the database saw no earlier Read or Edit of that path.</div>')
    via = {r["via"] or "?": r["n"] for r in conn.execute(
        "SELECT via, COUNT(*) n FROM file_activity GROUP BY via")}
    return ('<h2 id="files">File activity</h2>'
            '<p class="sub">Paths only. File contents are never stored. '
            f'{via.get("tool", 0)} rows come from the Read/Edit/Write tools; '
            f'{via.get("shell", 0)} were parsed out of shell commands '
            '(<code>grep</code>, <code>sed</code>, <code>cat &gt;</code>), which is how '
            'most real file access happens. Shell rows carry an '
            '<code>op_confidence</code>, because reading an operation off a command '
            'line is inference.</p>'
            + "<h3>Most touched files</h3>"
            + table(["File", "Project", "Touches", "Reads", "Writes", "Sessions", ""],
                    hot_rows, numeric={2, 3, 4, 5}, wrap_cols={0},
                    empty="No file activity observed. This needs OTEL_LOG_TOOL_DETAILS=1.")
            + "<h3>Repository areas consuming the most activity</h3>"
            + table(["Project", "Top-level area", "Touches", "", "Distinct files"],
                    dir_rows, numeric={2, 4})
            + "<h3>Files that appear to have been created</h3>" + note
            + table(["Path", "First seen", "Method", "Confidence"], cre_rows, wrap_cols={0},
                    empty="No creations detected."))


def _commands(conn) -> str:
    top = Q.bash_top(conn)
    rows = [[t["program"], num(t["runs"]),
             f'<span class="{"bad" if t["failures"] else "muted"}">{t["failures"] or 0}</span>',
             num(t["avg_ms"], 1)] for t in top]
    recent = conn.execute(
        "SELECT ts, command, success, duration_ms FROM bash_activity"
        " ORDER BY ts DESC LIMIT 40").fetchall()
    rec_rows = [[short_ts(r["ts"]),
                 f'<code>{esc((r["command"] or "")[:180])}</code>',
                 "<span class='ok'>ok</span>" if r["success"] == 1
                 else ("<span class='bad'>failed</span>" if r["success"] == 0 else "—"),
                 num(r["duration_ms"], 0)] for r in recent]
    return ('<h2 id="commands">Commands</h2>'
            '<p class="sub">Commands pass through secret redaction before storage.</p>'
            + table(["Program", "Runs", "Failures", "Avg ms"], rows, numeric={1, 2, 3})
            + "<h3>Recent commands</h3>"
            + table(["Time", "Command", "Result", "ms"], rec_rows, numeric={3}, wrap_cols={1}))


def _git(conn) -> str:
    rows = Q.git_commits(conn)
    out = [[f'<span class="mono">{esc(r["commit_sha"][:10])}</span>',
            short_ts(r["committed_at"]), r["project_name"] or "—",
            (r["subject"] or "")[:90], num(r["files_changed"]),
            f'<span class="ok">+{r["insertions"] or 0}</span>',
            f'<span class="bad">-{r["deletions"] or 0}</span>',
            (r["session_id"] or "—")[:8], r["attribution"]] for r in rows]
    return ('<h2 id="git">Git activity</h2>'
            '<div class="note">Claude Code emits only a <code>claude_code.commit.count</code> '
            'metric - never commit ids. Everything in this table comes from read-only '
            '<code>git log</code> against repositories observed in telemetry, matched to a '
            'session by commit time falling inside that session\'s window. Treat the session '
            'link as correlation, not proof of authorship.</div>'
            + table(["Commit", "When", "Project", "Subject", "Files", "+", "-",
                     "Session", "Attribution"], out, numeric={4, 5, 6}, wrap_cols={3},
                    empty="No commits observed in the reconciliation window."))


def _cost(conn) -> str:
    def block(title, dim):
        rows = Q.cost_by(conn, dim)
        mx = max([r["cost_usd"] or 0 for r in rows], default=0)
        out = [[r["k"], num(r["api_calls"]), usd(r["cost_usd"]), bar(r["cost_usd"] or 0, mx),
                num(r["input_tokens"]), num(r["output_tokens"]),
                num(r["cache_read_tokens"]), num(r["cache_creation_tokens"])] for r in rows]
        return (f"<h3>{esc(title)}</h3>"
                + table([title, "API calls", "Cost", "", "Input", "Output",
                         "Cache read", "Cache write"], out, numeric={1, 2, 4, 5, 6, 7}))
    return ('<h2 id="cost">Cost</h2>'
            '<p class="sub">All figures are Claude Code\'s own <code>cost_usd</code> '
            'estimate from the <code>api_request</code> event. Nothing is recomputed from '
            'a price list.</p>'
            + block("Day", "day") + block("Model", "model")
            + block("Query source", "query_source") + block("Skill", "skill"))


def _errors(conn) -> str:
    summary = Q.error_summary(conn)
    s_rows = [[r["kind"], r["detail"], num(r["n"])] for r in summary]
    detail = Q.errors(conn)
    d_rows = [[short_ts(r["ts"]), r["kind"], r["tool_name"] or r["model"] or "—",
               r["error_name"] or r["error_code"] or r["status_code"] or "—",
               f'<code>{esc(r["message"] or "")}</code>',
               (r["session_id"] or "—")[:8]] for r in detail]
    return ('<h2 id="errors">Errors</h2>'
            + table(["Kind", "Detail", "Count"], s_rows, numeric={2},
                    empty="No errors observed.")
            + "<h3>Recent</h3>"
            + table(["Time", "Kind", "Subject", "Code", "Message", "Session"], d_rows,
                    wrap_cols={4}, empty="No errors observed."))


def _coverage(conn) -> str:
    ev = Q.event_coverage(conn)
    ev_rows = [[f'claude_code.{r["event_name"]}', num(r["n"]), short_ts(r["first_seen"]),
                short_ts(r["last_seen"])] for r in ev]
    me = Q.metric_coverage(conn)
    me_rows = [[r["metric_name"], r["unit"] or "—", num(r["points"]), num(r["total"], 3)]
               for r in me]
    sp = Q.span_coverage(conn)
    sp_rows = [[r["name"], num(r["n"]), num(r["avg_ms"], 1)] for r in sp]

    documented_events = [
        "user_prompt", "assistant_response", "tool_result", "api_request", "api_error",
        "api_refusal", "api_request_body", "api_response_body", "tool_decision",
        "permission_mode_changed", "auth", "mcp_server_connection", "internal_error",
        "plugin_installed", "plugin_loaded",
    ]
    seen = {r["event_name"] for r in ev}
    missing = [e for e in documented_events if e not in seen]
    extra = sorted(seen - set(documented_events))

    gap = ""
    if missing:
        gap += (f'<p class="sub">Documented but <b>not observed</b> in this dataset: '
                f'<code>{esc(", ".join(missing))}</code>. Absence usually just means the '
                f'situation never arose (no refusal, no plugin install), or the relevant '
                f'<code>OTEL_LOG_*</code> switch is off.</p>')
    if extra:
        gap += (f'<p class="sub">Observed but <b>not in the documented list</b> - worth '
                f'inspecting: <code>{esc(", ".join(extra))}</code></p>')

    return ('<h2 id="coverage">Telemetry coverage</h2>'
            '<p class="sub">What this dataset actually contains, checked against the '
            'documented surface. Use this to see what Claude Code is and is not exposing.</p>'
            + "<h3>Events</h3>" + table(["Event", "Count", "First", "Last"], ev_rows, numeric={1})
            + gap
            + "<h3>Metrics</h3>" + table(["Metric", "Unit", "Points", "Sum of values"],
                                         me_rows, numeric={2, 3})
            + "<h3>Spans (tracing beta)</h3>"
            + table(["Span", "Count", "Avg ms"], sp_rows, numeric={1, 2},
                    empty="No spans. Tracing is off unless CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1."))


def _privacy(conn) -> str:
    dropped = conn.execute(
        "SELECT COUNT(*) n FROM tool_calls WHERE dropped_param_keys IS NOT NULL").fetchone()["n"]
    flags = [
        ("TELEMETRY_STORE_CONTENT", config.STORE_CONTENT, "prompt / response text"),
        ("TELEMETRY_STORE_TOOL_CONTENT", config.STORE_TOOL_CONTENT, "full tool argument JSON"),
        ("TELEMETRY_STORE_API_BODIES", config.STORE_API_BODIES, "raw API request/response bodies"),
    ]
    rows = [[k, "<span class='bad'>ON</span>" if v else "<span class='ok'>off</span>", d]
            for k, v, d in flags]
    return ('<h2 id="privacy">Privacy posture of this dataset</h2>'
            + table(["Setting", "State", "Controls storage of"], rows)
            + f'<p class="sub">Content-bearing tool arguments were stripped from '
              f'<b>{dropped}</b> tool calls. Secret patterns are redacted from every stored '
              f'string. See PRIVACY.md for the full list.</p>')


# -------------------------------------------------------------------- render --

def render(conn: sqlite3.Connection) -> str:
    generated = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    o = Q.overview(conn)
    nav = " · ".join(
        f'<a href="#{i}" style="color:var(--accent);text-decoration:none">{n}</a>'
        for i, n in [("overview", "Overview"), ("projects", "Projects"),
                     ("sessions", "Sessions"),
                     ("sessions", "Sessions"), ("cost", "Cost"),
                     ("output", "Output"), ("time", "Time"),
                     ("fixes", "What to change"),
                     ("effort", "Human effort"), ("causes", "Corrections"),
                     ("knowledge", "Knowledge base"), ("skills", "Skills"),
                     ("inventory", "Inventory"),
                     ("tools", "Tools"), ("files", "Files"), ("commands", "Commands"),
                     ("git", "Git"), ("errors", "Errors"), ("coverage", "Coverage"),
                     ("privacy", "Privacy")])
    from .report_sessions import (_causes as _wf_causes,
                                  _fixes as _wf_fixes,
                                  _knowledge as _wf_knowledge,
                                  _time as _wf_time,
                                  _output as _wf_output,
                                  EXTRA_CSS as _WF_CSS)
    parts = [_overview(conn), _projects(conn), _sessions_table(conn), _wf_output(conn),
             _sessions(conn), _cost(conn), _wf_time(conn),
             _wf_fixes(conn), _wf_causes(conn), _wf_knowledge(conn),
             _effort(conn), _skills(conn), _inventory(conn),
             _tools(conn), _files(conn), _commands(conn),
             _git(conn), _errors(conn), _coverage(conn), _privacy(conn)]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Telemetry</title>
<style>{CSS}{_WF_CSS}</style></head><body><div class="wrap">
<header>
  <h1>Claude Telemetry</h1>
  <p class="sub">Local report generated {esc(generated)} · {num(o["events"])} events ·
     {num(o["metric_points"])} metric points · {num(o["spans"])} spans</p>
  <p class="sub" style="margin-top:10px">{nav}</p>
</header>
{"".join(parts)}
<footer>
  Built from Claude Code OpenTelemetry data captured on this machine. No data left this
  device. Raw telemetry is retained under <code>data/raw/</code> so this report can be
  regenerated, and the schema reinterpreted, at any time.
</footer>
</div></body></html>"""


def write(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    config.ensure_dirs()
    out = Path(path or (config.REPORT_DIR / "report.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(conn), encoding="utf-8")
    return out
