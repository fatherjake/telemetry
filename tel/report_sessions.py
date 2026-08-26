"""A focused operator view: sessions, cost, output, skills, MCP.

Deliberately narrower than the full telemetry report. It answers "what did I
work on, what did it cost, what came out of it, and how much steering did it
take" - one card per session, with human effort shown inside the session
rather than as a separate section, because that is where it means something.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

from . import config, queries as Q
from .report import CSS, esc, num, usd, short_ts, table, bar, tile

EXTRA_CSS = """
/* --- data viz -------------------------------------------------------------
   Categorical slots from the validated default palette. Light mode WARNs on
   contrast for aqua/yellow, so every chart here ships direct labels and a
   table view - the documented relief. */
.viz{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--idle:#d8d3ca;
     --vsurf:var(--panel)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .viz{
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--idle:#3a3833}}
:root[data-theme="dark"] .viz{--s1:#3987e5;--s2:#d95926;--s3:#199e70;
  --s4:#c98500;--idle:#3a3833}

.legend{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 14px;font-size:12.5px;
  color:var(--muted)}
.legend b{display:inline-block;width:10px;height:10px;border-radius:3px;
  margin-right:6px;vertical-align:-1px}

/* segmented activity strip: one row per session, one segment per turn.
   Lanes and the strip share one grid so their x-axes line up exactly. */
.tl{display:grid;grid-template-columns:150px 1fr 46px;gap:10px;align-items:center}
.tl-lab{font-size:11.5px;color:var(--muted);text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.tl-lab .tag{display:inline-block;font-size:9px;text-transform:uppercase;
  letter-spacing:.06em;padding:0 4px;border-radius:3px;background:var(--bar);
  margin-left:5px;vertical-align:1px}
.tl-n{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.lane{height:15px;position:relative}
.lane .span{position:absolute;top:6px;height:3px;border-radius:2px;
  background:var(--bar)}
.dot{position:absolute;top:3px;width:9px;height:9px;border-radius:50%;
  margin-left:-4.5px;background:var(--muted);
  box-shadow:0 0 0 2px var(--vsurf);cursor:default}
/* Dense lanes: smaller marks so overlapping rings read as density rather
   than a row of crescents. Still >= 8px with the ring. */
.lane.dense .dot{width:6px;height:6px;top:4.5px;margin-left:-3px;
  box-shadow:0 0 0 1.5px var(--vsurf)}
.dot.load{width:11px;height:11px;top:2px;margin-left:-5.5px;
  background:var(--accent)}
.lane.dense .dot.load{width:9px;height:9px;top:3px;margin-left:-4.5px}
.dot:hover{background:var(--ink);z-index:12}
.dot .tip{display:none}
.dot:hover .tip{display:block;position:absolute;bottom:calc(100% + 9px);
  left:50%;transform:translateX(-50%);z-index:22;background:var(--ink);
  color:var(--bg);padding:6px 9px;border-radius:6px;font-size:11.5px;
  white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.28)}
.strip-row{margin:14px 0 22px}
.corr{margin-top:9px;padding:9px 12px;border-radius:8px;font-size:12.5px;
  background:var(--bg);border:1px solid var(--line)}
.corr .hd{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600;margin-right:8px}
.corr .c{display:inline-block;position:relative;margin:2px 6px 2px 0;
  padding:1px 9px;border-radius:999px;background:var(--panel);
  border:1px solid var(--line);cursor:default}
.corr .c.fix{border-color:var(--accent)}
.corr .c .tip{display:none}
.corr .c:hover .tip{display:block;position:absolute;bottom:calc(100% + 7px);
  left:0;z-index:24;background:var(--ink);color:var(--bg);padding:7px 10px;
  border-radius:6px;font-size:11.5px;width:290px;white-space:normal;
  box-shadow:0 4px 14px rgba(0,0,0,.28);font-weight:400}
.desc{font-size:13.5px;color:var(--ink);opacity:.9;margin:6px 0 2px;
  line-height:1.5}
.strip-meta .desc{margin:0;font-size:12.5px;opacity:.75;flex-basis:100%}
.docs{margin-top:9px;padding:9px 12px;border-radius:8px;font-size:12.5px;
  background:var(--bg);border:1px solid var(--line)}
.docs .row{display:flex;gap:9px;align-items:baseline;margin:3px 0;flex-wrap:wrap}
.docs .lab{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600;flex:0 0 96px;text-align:right}
.docs .d{display:inline-block;position:relative;padding:1px 9px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line);cursor:default;
  font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.docs .d.read{border-color:var(--ok)}
.docs .d.gap{border-color:var(--s4)}
.docs .d .tip{display:none}
.docs .d:hover .tip{display:block;position:absolute;bottom:calc(100% + 7px);
  left:0;z-index:24;background:var(--ink);color:var(--bg);padding:7px 10px;
  border-radius:6px;font-size:11.5px;width:290px;white-space:normal;
  box-shadow:0 4px 14px rgba(0,0,0,.28);font-family:inherit}
.docgap{margin-top:9px;padding:9px 12px;border-radius:8px;font-size:12.5px;
  background:var(--bg);border:1px solid var(--line);border-left:3px solid var(--s4)}
.docgap .hd{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:600;margin-right:8px}
.docgap .d{display:inline-block;position:relative;margin:2px 6px 2px 0;
  padding:1px 9px;border-radius:999px;background:var(--panel);
  border:1px solid var(--line);cursor:default;font-family:ui-monospace,Menlo,monospace;
  font-size:11.5px}
.docgap .d.high{border-color:var(--s4)}
.docgap .d .tip{display:none}
.docgap .d:hover .tip{display:block;position:absolute;bottom:calc(100% + 7px);
  left:0;z-index:24;background:var(--ink);color:var(--bg);padding:7px 10px;
  border-radius:6px;font-size:11.5px;width:290px;white-space:normal;
  box-shadow:0 4px 14px rgba(0,0,0,.28);font-family:inherit}
.sev-high{color:var(--bad);font-weight:700;text-transform:uppercase;
  font-size:11px;letter-spacing:.05em}
.sev-medium{color:var(--warn);font-weight:700;text-transform:uppercase;
  font-size:11px;letter-spacing:.05em}
.sev-low{color:var(--muted);font-weight:600;text-transform:uppercase;
  font-size:11px;letter-spacing:.05em}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.findings{margin-top:9px}
.findings .hd{display:block;font-size:11px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);font-weight:600;margin-bottom:5px}
.fnd{margin-bottom:7px;padding:9px 12px;border-radius:8px;font-size:12.5px;
  background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--muted)}
.fnd.high{border-left-color:var(--bad)}
.fnd.medium{border-left-color:var(--warn)}
.fnd-h{display:flex;gap:8px;align-items:baseline;margin-bottom:3px}
.fnd .sev{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
  font-weight:700;color:var(--muted)}
.fnd.high .sev{color:var(--bad)}
.fnd.medium .sev{color:var(--warn)}
.fnd .knd{font-size:11.5px;color:var(--muted)}
.fnd-b{line-height:1.45}
.fnd-e{margin-top:3px;font-size:11.5px;color:var(--muted);
  font-family:ui-monospace,Menlo,monospace}
.fnd-f{margin-top:6px;padding-top:6px;border-top:1px solid var(--line);
  line-height:1.45}
.fnd-f .loc{display:inline-block;margin-right:7px;padding:1px 8px;
  border-radius:999px;font-size:11px;background:var(--accent-soft);
  color:var(--accent);font-weight:600}
.missed{margin-top:9px;padding:9px 12px;border-radius:8px;font-size:12.5px;
  background:var(--accent-soft);border:1px solid transparent}
.missed .hd{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--accent);font-weight:600;margin-right:8px}
.missed .sk{display:inline-block;position:relative;margin:2px 6px 2px 0;
  padding:1px 9px;border-radius:999px;background:var(--panel);
  border:1px solid var(--line);cursor:default}
.missed .sk.read{border-color:var(--accent);font-weight:600}
.missed .sk .tip{display:none}
.missed .sk:hover .tip{display:block;position:absolute;bottom:calc(100% + 7px);
  left:0;z-index:24;background:var(--ink);color:var(--bg);padding:7px 10px;
  border-radius:6px;font-size:11.5px;width:260px;white-space:normal;
  box-shadow:0 4px 14px rgba(0,0,0,.28);font-weight:400}
.tl-head{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);margin:20px 0 2px;padding-top:14px;
  border-top:1px solid var(--line)}
.strip-meta{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;
  font-size:13px;margin-bottom:5px}
.strip-meta .nm{font-weight:600}
.strip{position:relative;display:flex;height:26px;border-radius:5px;
  overflow:visible;background:var(--bar)}
.seg{position:relative;height:100%;background:var(--s1);
  border-right:2px solid var(--vsurf);min-width:2px}
.seg:first-child{border-top-left-radius:4px;border-bottom-left-radius:4px}
.seg:last-child{border-right:0;border-top-right-radius:4px;
  border-bottom-right-radius:4px}
.seg.corr{background:var(--s2)} .seg.steer{background:var(--s3)}
.seg .lbl{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:10.5px;font-weight:600;color:#fff;
  pointer-events:none;overflow:hidden}
.seg .tip{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;
  transform:translateX(-50%);z-index:20;background:var(--ink);color:var(--bg);
  padding:7px 10px;border-radius:6px;font-size:12px;line-height:1.45;
  white-space:normal;width:270px;box-shadow:0 4px 14px rgba(0,0,0,.28)}
.seg:hover{filter:brightness(1.12)}
.seg:hover .tip{display:block}
.seg .tip b{color:var(--bg)}

/* horizontal magnitude bars */
.hbar{display:grid;grid-template-columns:210px 1fr 118px;gap:12px;
  align-items:center;margin:7px 0;font-size:13px}
.hbar .lab{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hbar .val{white-space:nowrap}
@media (max-width:640px){.hbar{grid-template-columns:130px 1fr 92px;gap:8px;
  font-size:12px}}
.hbar .track{background:var(--bar);border-radius:4px;height:14px;position:relative}
.hbar .fill{height:100%;border-radius:4px;background:var(--s1)}
.hbar .val{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted)}

.stream{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:20px 22px;margin:16px 0}
.stream.shipped{border-left:3px solid var(--ok)}
.stream.unshipped{border-left:3px solid var(--bar)}
.stream-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:4px}
.stream-name{font-size:19px;font-weight:600;letter-spacing:-.01em}
.stream-cost{margin-left:auto;font-size:20px;font-weight:600;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;
  font-weight:600;background:var(--accent-soft);color:var(--accent)}
.chip.grey{background:var(--bar);color:var(--muted)}
.chip.good{background:transparent;color:var(--ok);border:1px solid var(--ok)}
.chip.warn{background:transparent;color:var(--warn);border:1px solid var(--warn)}
.chip.bad{background:transparent;color:var(--bad);border:1px solid var(--bad)}
.facts{display:flex;flex-wrap:wrap;gap:22px;margin:14px 0 4px}
.fact{min-width:74px}
.fact .l{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.fact .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:1px}
.effort{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:11px 14px;margin-top:14px;font-size:13px}
.effort .hd{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);margin-bottom:6px}
.commitlist{margin:12px 0 0;padding:0;list-style:none;font-size:13px}
.commitlist li{padding:5px 0;border-top:1px solid var(--line);display:flex;gap:10px}
.commitlist .sha{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);
  flex:0 0 72px}
.commitlist .sub{flex:1;min-width:0}
.commitlist .st{flex:0 0 auto;font-variant-numeric:tabular-nums;font-size:12px}
.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
"""


def _pct(a, b):
    return (a / b * 100) if b else 0


def _chips(items, cls="grey", limit=6):
    if not items:
        return '<span class="muted">—</span>'
    out = [f'<span class="chip {cls}">{esc(n)}<span style="opacity:.6"> {v}</span></span>'
           for n, v in items[:limit]]
    if len(items) > limit:
        out.append(f'<span class="chip grey">+{len(items)-limit}</span>')
    return " ".join(out)


def _fact(label, value):
    return f'<div class="fact"><div class="l">{esc(label)}</div><div class="v">{value}</div></div>'


def _documents(read: list, gaps: list, read_label: str = "Docs read") -> str:
    """What the knowledge base contributed, and what it could have."""
    if not read and not gaps:
        return ""
    rows = []
    if read:
        chips = "".join(
            f'<span class="d read">{esc(r["rel_path"])}'
            f'<span style="opacity:.6"> {r["reads"]}\u00d7</span>'
            f'<span class="tip"><b>{esc(r["rel_path"])}</b> · '
            f'{num(r["word_count"])} words · read {r["reads"]}\u00d7<br>'
            f'<span style="opacity:.8">{esc(r["topic"] or "")}</span>'
            f'</span></span>' for r in read)
        rows.append(f'<div class="row"><span class="lab">{esc(read_label)}</span>'
                    f'<span>{chips}</span></div>')
    else:
        rows.append(f'<div class="row"><span class="lab">{esc(read_label)}</span>'
                    f'<span class="muted">none</span></div>')
    if gaps:
        words = sum(g["word_count"] or 0 for g in gaps)
        chips = "".join(
            f'<span class="d gap">{esc(g["rel_path"])}'
            f'<span class="tip"><b>{esc(g["rel_path"])}</b> · '
            f'{num(g["word_count"])} words · {esc(g["confidence"] or "?")} '
            f'confidence<br><span style="opacity:.8">{esc(g["topic"] or "")}</span>'
            f'<br><br>{esc(g["reason"] or "")}</span></span>' for g in gaps)
        rows.append(f'<div class="row"><span class="lab">Not read</span>'
                    f'<span>{chips}<span class="muted" style="margin-left:6px">'
                    f'{num(words)} words</span></span></div>')
    return f'<div class="docs">{"".join(rows)}</div>'


def _session_card(d: dict, max_cost: float) -> str:
    """One session, whole. Nothing here is a share of anything."""
    sess, e = d["session"], d["effort"]
    strip = d.get("strip") or {}
    shipped = bool(sess["prod_deploys"])

    badges = []
    scopes = sorted({c["commit_scope"] for c in d["commits"] if c["commit_scope"]})
    if scopes:
        badges.append(f'<span class="chip">{esc(", ".join(scopes[:3]))}</span>')
    if not sess["commits"]:
        badges.append('<span class="chip grey">no commits</span>')
    elif shipped:
        badges.append('<span class="chip good">live</span>')
    else:
        badges.append('<span class="chip grey">not shipped</span>')
    if sess["reverted"]:
        badges.append(f'<span class="chip bad">{sess["reverted"]} reverted</span>')
    corr_cost = sum(c["cost_usd"] or 0 for c in (d.get("corrections") or []))
    if corr_cost:
        badges.append(f'<span class="chip warn">{usd(corr_cost)} corrections</span>')
    if d.get("doc_gaps"):
        badges.append(f'<span class="chip warn">{len(d["doc_gaps"])} docs '
                      f'unread</span>')
    missed_names = sorted({m["skill_name"] for m in (d.get("missed_skills") or [])})
    if missed_names:
        badges.append(f'<span class="chip warn">{len(missed_names)} skill'
                      f'{"" if len(missed_names) == 1 else "s"} missed</span>')

    active = (strip.get("session") or {}).get("active_s") or 0
    elapsed = active + ((strip.get("session") or {}).get("gap_s") or 0)
    facts_list = [_fact("started", short_ts(sess["first_seen"])),
                  _fact("project", esc(sess["project_name"] or "—")),
                  _fact("turns", num(e["turns"]) if e["turns"] else "—"),
                  _fact("active", _fmt_dur(active) if active else "—"),
                  _fact("elapsed", _fmt_dur(elapsed) if elapsed else "—")]
    if sess["commits"]:
        facts_list += [
            _fact("commits", num(sess["commits"])),
            _fact("lines", f'<span class="ok">+{sess["insertions"]}</span> '
                           f'<span class="bad">−{sess["deletions"]}</span>'),
            _fact("prod deploys", num(sess["prod_deploys"])),
        ]
    facts = "".join(facts_list)

    # Human effort, shown where it is meaningful: against the work it slowed.
    if e["turns"]:
        bits = [
            f'<b>{e["corrections"]}</b> correction'
            f'{"" if e["corrections"] == 1 else "s"} of {e["turns"]} turns '
            f'({_pct(e["corrections"], e["turns"]):.0f}%)',
            f'<b>{e["steers"]}</b> steering nudge{"" if e["steers"] == 1 else "s"}',
        ]
        if e["overrides"]:
            bits.append(f'<b class="bad">{e["overrides"]}</b> tool override')
        if e["rejects"]:
            bits.append(f'<b>{e["rejects"]}</b> reject{"" if e["rejects"] == 1 else "s"}')
        if e["tool_failures"]:
            bits.append(f'<b>{e["tool_failures"]}</b> tool failure'
                        f'{"" if e["tool_failures"] == 1 else "s"}')
        if e["rework"]:
            bits.append(f'<b>{e["rework"]}</b> file{"" if e["rework"] == 1 else "s"} reworked')
        if e["correction_cost"]:
            share = min(_pct(e["correction_cost"], sess["cost"] or 0), 100)
            bits.append(f'<b>{usd(e["correction_cost"])}</b> on correction turns '
                        f'({share:.0f}% of this session)')
        if e["gap"]:
            bits.append(f'~{int(e["gap"])}s average gap between turns')
        effort = (f'<div class="effort"><div class="hd">Human effort</div>'
                  f'{" · ".join(bits)}</div>')
    else:
        effort = ('<div class="effort"><div class="hd">Human effort</div>'
                  '<span class="muted">No turns recorded — tool activity only.'
                  '</span></div>')

    tooling = ""
    if d["skills"] or d["mcps"]:
        tooling = (f'<div class="facts" style="margin-top:12px">'
                   f'<div><div class="l">skills</div><div style="margin-top:4px">'
                   f'{_chips([(s["skill_name"], s["n"]) for s in d["skills"]])}</div></div>'
                   f'<div><div class="l">mcp servers</div><div style="margin-top:4px">'
                   f'{_chips([(m["srv"], m["n"]) for m in d["mcps"]])}</div></div></div>')

    commits = ""
    if d["commits"]:
        items = "".join(
            f'<li><span class="sha">{esc(c["commit_sha"][:8])}</span>'
            f'<span class="sub">{esc((c["subject"] or "")[:96])}</span>'
            f'<span class="st"><span class="ok">+{c["insertions"] or 0}</span> '
            f'<span class="bad">−{c["deletions"] or 0}</span></span></li>'
            for c in d["commits"][:6])
        more = (f'<li><span class="sha"></span><span class="sub muted">'
                f'+{len(d["commits"]) - 6} more</span></li>'
                if len(d["commits"]) > 6 else "")
        commits = f'<ul class="commitlist">{items}{more}</ul>'

    gaps_html = _documents(d.get("docs_read") or [], d.get("doc_gaps") or [],
                           read_label="Docs read")
    # The title is the description when there is one, so printing it again
    # underneath would be the same sentence twice.
    if d.get("description"):
        desc_html = ""
    else:
        from .narrate import naming_note
        desc_html = f'<div class="desc muted">{esc(naming_note(sess))}</div>' 
    timeline = ('<div class="viz">' + _strip(strip) + "</div>") if strip else ""

    return (f'<div class="stream {"shipped" if shipped else "unshipped"}">'
            f'<div class="stream-head"><span class="stream-name">'
            f'{esc(sess["title"])}</span>'
            f'{" ".join(badges)}'
            f'<span class="stream-cost">{usd(sess["cost"])}</span></div>'
            f'{desc_html}'
            f'<div>{bar(sess["cost"] or 0, max_cost)}</div>'
            f'<div class="facts">{facts}</div>'
            f'{tooling}{effort}{gaps_html}{commits}{timeline}</div>')


def _sessions(conn) -> str:
    details = Q.session_detail(conn)
    mx = max([d["session"]["cost"] or 0 for d in details], default=0)
    cards = "".join(_session_card(d, mx) for d in details)
    legend = (_legend([("normal turn", "var(--s1)"), ("correction turn", "var(--s2)"),
                       ("steering nudge", "var(--s3)")])
              + '<div class="legend"><span><b style="background:var(--accent);'
                'border-radius:50%"></b>first load of a skill / MCP</span>'
                '<span><b style="background:var(--muted);border-radius:50%"></b>'
                'call or file access</span></div>')
    note = ('<div class="note"><b>One session, one piece of work.</b> '
            'Everything on a card belongs to that session and nothing else: the '
            'cost is what its own API calls cost, the commits are the ones made '
            'during it, and the effort is its own turns. Newest first. The name '
            'is a one-line summary of what the session set out to do, written '
            'from its prompts.</div>')
    return (f'<h2 id="sessions">Sessions</h2>{note}'
            f'<div class="viz">{legend}</div>{cards}')


def _fmt_dur(seconds) -> str:
    if not seconds:
        return "—"
    s = float(seconds)
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.0f}m"
    return f"{s/3600:.1f}h"


def _legend(items) -> str:
    return ('<div class="legend">' + "".join(
        f'<span><b style="background:{c}"></b>{esc(l)}</span>' for l, c in items)
        + "</div>")


def _hbars(rows, colour="var(--s1)") -> str:
    """rows: (label, value, display) - magnitude comparison, always labelled."""
    mx = max([v for _, v, _ in rows], default=0) or 1
    return "".join(
        f'<div class="hbar"><div class="lab" title="{esc(l)}">{esc(l)}</div>'
        f'<div class="track"><div class="fill" style="width:{max(2, v/mx*100):.1f}%;'
        f'background:{colour}"></div></div>'
        f'<div class="val">{esc(d)}</div></div>' for l, v, d in rows)


def _parse_ts(v):
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _axis(turns):
    """Map a timestamp onto the strip's compressed active-time axis.

    The strip excludes idle gaps, so a raw timestamp cannot be placed by
    proportion of elapsed time. Each turn occupies a slice sized by its own
    duration; an event inside a turn lands at its offset within that slice,
    and an event that fell in a gap snaps to the nearest turn boundary.
    """
    segs, cum = [], 0.0
    for t in turns:
        dur = float(t["duration_s"] or 0)
        start = _parse_ts(t["started_at"])
        if start is None:
            continue
        segs.append((start, start + _dt.timedelta(seconds=dur), cum, dur))
        cum += dur
    total = cum or 1.0

    def pos(ts):
        d = _parse_ts(ts)
        if d is None or not segs:
            return None
        for start, end, before, dur in segs:
            if start <= d <= end:
                off = min(max((d - start).total_seconds(), 0.0), dur)
                return (before + off) / total * 100.0
        if d < segs[0][0]:
            return 0.0
        for i, (start, end, before, dur) in enumerate(segs):
            if d < start:                      # fell in the gap before this turn
                return (segs[i - 1][2] + segs[i - 1][3]) / total * 100.0
        return 100.0

    return total, pos


def _lanes(entry: dict, pos) -> str:
    """A row per skill / MCP server, dotted at every call along the same axis."""
    lanes = entry.get("lanes") or []
    rows = []
    for lane in lanes:
        points = [(e, pos(e["ts"])) for e in lane["events"]]
        points = [(e, x) for e, x in points if x is not None]
        if not points:
            continue
        first_x, last_x = points[0][1], points[-1][1]
        # docs/files/file lanes are file access: no "first load" marker, since
        # nothing is loaded - every dot is just an access.
        is_file = lane["kind"] in ("files", "file", "docs", "hot")
        dots = []
        for i, (e, x) in enumerate(points):
            when = str(e["ts"])[11:19]
            path = e.get("path")
            if path:
                # Each dot on an aggregate lane is a specific file, so name it
                # rather than repeating the lane's summary.
                head = f'{esc(path.rsplit("/", 1)[-1])} · {esc(e.get("op") or "")}'
                tail = f'<br><span style="opacity:.75">{esc(path)}</span>'
            elif is_file:
                head, tail = f'{esc(lane["name"])} · access {i + 1}', ""
            else:
                head = (f'{esc(lane["name"])} · '
                        f'{"loaded" if i == 0 else f"call {i + 1}"}')
                tail = (f'<br><span style="opacity:.75">{esc(lane["detail"])}</span>'
                        if lane.get("detail") else "")
            tip = f'{head} · {esc(when)} UTC{tail}'
            first_cls = " load" if (i == 0 and not is_file) else ""
            dots.append(f'<span class="dot{first_cls}" style="left:{x:.3f}%">'
                        f'<span class="tip">{tip}</span></span>')
        span = (f'<span class="span" style="left:{first_x:.3f}%;'
                f'width:{max(last_x - first_x, 0):.3f}%"></span>'
                if last_x > first_x else "")
        dense = " dense" if len(points) > 20 else ""
        rows.append(
            f'<div class="tl"><div class="tl-lab" title="'
            f'{esc(lane.get("detail") or lane["name"])}">{esc(lane["name"])}'
            f'<span class="tag">{esc(lane["kind"])}</span></div>'
            f'<div class="lane{dense}">{span}{"".join(dots)}</div>'
            f'<div class="tl-n">{len(points)}\u00d7</div></div>')
    return "".join(rows)


def _missed(missed: list) -> str:
    """Skills that should have fired, shown against the session that missed them."""
    if not missed:
        return ""
    chips = []
    for m in missed:
        cls = " read" if m["was_read"] else ""
        note = ("opened this skill and declined it" if m["was_read"]
                else "available, never surfaced")
        chips.append(
            f'<span class="sk{cls}">{esc(m["skill_name"])}'
            f'<span class="tip"><b>{esc(m["skill_name"])}</b> · '
            f'{esc(m["confidence"] or "?")} confidence<br>{esc(note)}<br>'
            f'<span style="opacity:.8">{esc(m["reason"] or "")}</span></span></span>')
    return (f'<div class="missed"><span class="hd">Should have fired</span>'
            f'{"".join(chips)}</div>')


def _findings(items: list) -> str:
    """What to change about how this session went.

    Everything else on a card describes what happened. This is the only block
    that says what to do about it, so it leads with the fix and keeps the
    evidence underneath - the numbers are there to be checked, not read first.
    """
    if not items:
        return ""
    out = []
    for f in items:
        sev = (f["severity"] or "low").lower()
        loc = (f["fix_location"] or "").replace("_", " ")
        out.append(
            f'<div class="fnd {esc(sev)}">'
            f'<div class="fnd-h"><span class="sev">{esc(sev)}</span>'
            f'<span class="knd">{esc((f["kind"] or "").replace("_", " "))}</span>'
            f'</div>'
            f'<div class="fnd-b">{esc(f["finding"] or "")}</div>'
            + (f'<div class="fnd-e">{esc(f["evidence"] or "")}</div>'
               if f["evidence"] else "")
            + (f'<div class="fnd-f"><span class="loc">{esc(loc)}</span>'
               f'{esc(f["fix"])}</div>' if f["fix"] else "")
            + '</div>')
    return (f'<div class="findings"><span class="hd">What to change</span>'
            f'{"".join(out)}</div>')


def _corrections(items: list) -> str:
    """Corrections shown against the session they happened in."""
    if not items:
        return ""
    chips = []
    for c in items:
        cause = (c["cause"] or "").replace("_", " ")
        has_fix = bool(c["suggested_fix"])
        tip = (f'<b>{esc(cause)}</b> · {esc(c["confidence"] or "?")} confidence<br>'
               f'<span style="opacity:.8">{esc((c["prompt"] or "")[:110])}</span>')
        if has_fix:
            tip += (f'<br><br>fix ({esc(c["fix_location"])}): '
                    f'{esc(c["suggested_fix"][:170])}')
        else:
            tip += '<br><br>no fix — nothing written down would have prevented it'
        chips.append(f'<span class="c{" fix" if has_fix else ""}">{esc(cause)} '
                     f'<span style="opacity:.6">{usd(c["cost_usd"])}</span>'
                     f'<span class="tip">{tip}</span></span>')
    total = sum(c["cost_usd"] or 0 for c in items)
    return (f'<div class="corr"><span class="hd">Corrections · {usd(total)}</span>'
            f'{"".join(chips)}</div>')


def _strip(entry: dict) -> str:
    sess, turns = entry["session"], entry["turns"]
    total = sum(t["duration_s"] or 0 for t in turns) or 1
    segs = []
    for t in turns:
        dur = t["duration_s"] or 0
        pct = dur / total * 100
        cls = "corr" if t["is_correction"] else ("steer" if t["is_steering"] else "")
        label = f'{dur/60:.0f}m' if pct > 7 and dur >= 60 else ""
        prompt = (t["prompt"] or "").strip()
        tip = (f'<b>Turn {t["seq"]}</b> · {_fmt_dur(dur)} · {usd(t["cost_usd"])} · '
               f'{t["tool_calls"] or 0} tools')
        if t["is_correction"]:
            tip += "<br><b>correction</b>"
            if t.get("cause"):
                tip += f' — {esc(t["cause"].replace("_", " "))}'
            if t.get("suggested_fix"):
                tip += (f'<br><span style="opacity:.8">fix ({esc(t["fix_location"])}): '
                        f'{esc(t["suggested_fix"][:150])}</span>')
            elif t.get("correction_cue"):
                tip += f'<br><span style="opacity:.75">{esc(t["correction_cue"])}</span>'
        elif t["is_steering"]:
            tip += "<br>steering nudge"
        if t["gap_before_s"]:
            tip += f'<br>{_fmt_dur(t["gap_before_s"])} gap before'
        if prompt:
            tip += f'<br><span style="opacity:.75">{esc(prompt[:120])}</span>'
        segs.append(f'<div class="seg {cls}" style="width:{pct:.3f}%">'
                    f'<span class="lbl">{label}</span>'
                    f'<span class="tip">{tip}</span></div>')
    elapsed = (sess["active_s"] or 0) + (sess["gap_s"] or 0)
    _, pos = _axis(turns)
    lane_html = _lanes(entry, pos)
    missed_html = _missed(entry.get("missed_skills") or [])
    corr_html = _corrections(entry.get("corrections") or [])
    find_html = _findings(entry.get("findings") or [])
    sdocs_html = _documents(entry.get("docs_read") or [], [])
    sdesc = entry.get("description")
    return (f'<div class="strip-row">'
            f'<div class="strip-meta"><span class="nm">'
            f'{esc(sess["session_id"][:8])}</span>'
            f'<span class="muted">{esc(sess["project_name"] or "—")}</span>'
            f'<span class="muted">· {sess["turns"]} turns · '
            f'{_fmt_dur(sess["active_s"])} active of {_fmt_dur(elapsed)} elapsed · '
            f'{usd(sess["cost"])}</span>'
            + (f'<span class="desc">{esc(sdesc)}</span>' if sdesc else "")
            + '</div>'
            # The turns bar is the axis the lanes are plotted against, so it
            # reads first and the dots below have something to be relative to.
            f'<div class="tl"><div class="tl-lab">turns</div>'
            f'<div class="strip">{"".join(segs)}</div>'
            f'<div class="tl-n"></div></div>'
            f'{lane_html}'
            f'{sdocs_html}{corr_html}{missed_html}{find_html}</div>')


def _time(conn) -> str:
    c = Q.time_components(conn)
    t = Q.friction_totals(conn)
    # Activity strips live on the session cards, where the work they describe
    # is, so this section is about time in aggregate.
    strips = []

    tiles = "".join([
        tile("Active time", _fmt_dur(c["turn_wall"]), "sum of turn durations"),
        tile("Your thinking time", _fmt_dur(c["human_gap"]), "gaps between turns"),
        tile("Model requests", _fmt_dur(c["llm_request"])),
        tile("Tool execution", _fmt_dur(c["tool_execution"])),
        tile("Blocked on you", _fmt_dur(c["blocked_on_user"]), "waiting for permission"),
        tile("On corrections", _fmt_dur(c["correction_wall"]),
             f'{c["correction_wall"]/c["turn_wall"]*100:.0f}% of active time'
             if c["turn_wall"] else ""),
    ])

    # Overlapping components: NOT a stack. Model requests happen inside a turn
    # and tools run in parallel, so these do not sum to elapsed time.
    comp_rows = [
        ("Model requests", c["llm_request"], _fmt_dur(c["llm_request"])),
        ("Tool span (total)", c["tool_span"], _fmt_dur(c["tool_span"])),
        ("  · executing", c["tool_execution"], _fmt_dur(c["tool_execution"])),
        ("  · blocked on you", c["blocked_on_user"], _fmt_dur(c["blocked_on_user"])),
    ]
    cats = Q.time_by_tool_category(conn)
    cat_rows = [(r["tool_category"], r["total_s"] or 0,
                 f'{_fmt_dur(r["total_s"])} · {r["calls"]}×') for r in cats]
    tools = Q.time_by_tool(conn, 12)
    tool_rows = [(r["tool"], r["total_s"] or 0,
                  f'{_fmt_dur(r["total_s"])} · {int(r["avg_ms"] or 0)}ms')
                 for r in tools]
    skills = Q.time_by_skill(conn)
    skill_tbl = [[r["skill_name"], num(r["invocations"]), num(r["turns"]),
                  _fmt_dur(r["total_s"]), _fmt_dur(r["avg_s"]),
                  usd(r["cost_usd"])] for r in skills]

    # Table view - required relief for the light-mode contrast WARN, and the
    # place every number in the strips is readable without hovering.
    strip_tbl = []
    for e in strips:
        s_ = e["session"]
        strip_tbl.append([s_["session_id"][:8], s_["project_name"] or "—",
                          num(s_["turns"]), _fmt_dur(s_["active_s"]),
                          _fmt_dur(s_["gap_s"]), usd(s_["cost"])])

    lbl_note = (f'<div class="note"><b>Turn labels come from a small model</b> reading '
                f'each prompt with the previous one as context — {t["model_labelled"]} of '
                f'{t["turns"]} turns. The rest use a length heuristic because their text '
                f'predates content storage. {t["system_turns"]} harness-injected prompts '
                f'(monitor notifications) are excluded: they cost {usd(t["system_cost"])} '
                f'but nobody typed them.</div>')
    legend = (_legend([("normal turn", "var(--s1)"), ("correction turn", "var(--s2)"),
                       ("steering nudge", "var(--s3)")])
              + '<div class="legend"><span><b style="background:var(--accent);'
                'border-radius:50%"></b>first load of a skill / MCP server</span>'
                '<span><b style="background:var(--muted);border-radius:50%"></b>'
                'subsequent call</span></div>')

    return ('<div class="viz"><h2 id="time">Time</h2>'
            f'<div class="grid">{tiles}</div>'
            '<div class="note"><b>Two clocks, and they are not the same.</b> '
            f'Active time is {_fmt_dur(c["turn_wall"])}; the gaps between your turns '
            f'add another {_fmt_dur(c["human_gap"])}. Model requests, tool execution and '
            'time blocked on you all happen <i>inside</i> active time and overlap each '
            'other — tools run in parallel — so they are shown as separate magnitudes '
            'below, never stacked into a total.</div>'

            + lbl_note

            + '<h3>Where time goes inside a turn</h3>'
            + _hbars(comp_rows, "var(--s1)")
            + '<p class="sub">These overlap and deliberately do not sum.</p>'

            + '<h3>Time by tool category</h3>' + _hbars(cat_rows, "var(--s2)")
            + '<h3>Slowest tools</h3>' + _hbars(tool_rows, "var(--s3)")

            + '<h3>Time by skill</h3>'
            + '<div class="note">A <code>Skill</code> tool call itself takes '
              'milliseconds — it only loads instructions. What is measured here is the '
              'wall-clock of the turns the skill was active in, which is an upper bound: '
              'other work happened in those turns too.</div>'
            + table(["Skill", "Invocations", "Turns", "Total", "Avg turn", "Cost"],
                    skill_tbl, numeric={1, 2, 3, 4, 5},
                    empty="No skill usage observed.")

            + '<h3>Session table</h3>'
            + table(["Session", "Project", "Turns", "Active", "Idle gaps", "Cost"],
                    strip_tbl, numeric={2, 3, 4, 5},
                    empty="No turns recorded.")
            + '</div>')


def _causes(conn) -> str:
    """Corrections traced to a cause, and what would have prevented them."""
    try:
        from . import corrections as C
        by_cause = C.by_cause(conn)
        proposed = C.proposed_knowledge(conn)
    except Exception:
        return ""
    if not by_cause:
        return ""

    total_cost = sum(r["cost"] or 0 for r in by_cause)
    mx = max([r["cost"] or 0 for r in by_cause], default=0)
    crows = [[r["cause"].replace("_", " "), num(r["n"]), usd(r["cost"]),
              bar(r["cost"] or 0, mx), f'{r["minutes"]} min'] for r in by_cause]

    prows = [[f'<span class="chip">{esc(p["fix_location"].replace("_", " "))}</span>',
              esc(p["suggested_fix"] or ""), esc(p["what_was_missing"] or "—"),
              num(p["corrections"]), usd(p["cost"])] for p in proposed]

    fixable = sum(p["cost"] or 0 for p in proposed)
    return ('<div class="viz"><h2 id="causes">Why corrections happened</h2>'
            '<div class="note"><b>Counting corrections says how often; this says '
            'why.</b> Each correcting message is read alongside the instruction '
            'before it and what the agent did in between. The taxonomy is '
            'action-shaped: <code>missing_context</code> and '
            '<code>stale_context</code> mean something should be written down, '
            '<code>wrong_approach</code> usually means a rule or a skill, '
            '<code>ambiguous_request</code> is on how the work was asked for, and '
            '<code>design_iteration</code> is subjective refinement with no right '
            'answer knowable in advance — cost, but not a defect.</div>'
            + table(["Cause", "Corrections", "Cost", "", "Time"], crows,
                    numeric={1, 2, 4})
            + f'<h3>What to write down</h3>'
            + f'<p class="sub">Ranked by the cost of the corrections each would '
              f'address — <b>{usd(fixable)}</b> of {usd(total_cost)} in corrections '
              f'traces to something writable.</p>'
            + table(["Where", "Sentence to record", "What was missing",
                     "Corrections", "Cost"], prows, numeric={3, 4},
                    wrap_cols={1, 2},
                    empty="No correction traced to a recordable fix.")
            + '</div>')


def _fixes(conn) -> str:
    """Every finding across every session, ranked so the list is a backlog."""
    try:
        from . import sessiondx as D
        rows = D.top_findings(conn, 40)
        cov = D.coverage(conn)
        locs = D.by_location(conn)
    except Exception:
        return ""
    if not cov["reviewed"]:
        return ""

    lrows = [[f'<span class="chip">{esc((l["fix_location"] or "—").replace("_", " "))}</span>',
              num(l["n"]), num(l["sessions"]), num(l["high"] or 0),
              usd(l["cost"])] for l in locs]

    frows = []
    for r in rows:
        sev = (r["severity"] or "low").lower()
        frows.append([
            f'<span class="sev-{esc(sev)}">{esc(sev)}</span>',
            esc((r["kind"] or "").replace("_", " ")),
            f'{esc(r["sess"])}<br><span class="muted">'
            f'{esc(r["project_name"] or "—")}</span>',
            f'{esc(r["finding"] or "")}<br>'
            f'<span class="muted mono">{esc(r["evidence"] or "")}</span>',
            esc(r["fix"] or "—"),
            usd(r["total_cost_usd"])])

    pct = (100 * cov["cost_reviewed"] / cov["cost_total"]) if cov["cost_total"] else 0
    return ('<div class="viz"><h2 id="fixes">What to change</h2>'
            '<div class="note"><b>Everything else here describes what happened; '
            'this says what to do about it.</b> Friction is measured '
            'mechanically first — rewrite loops, repeated commands, failing '
            'tools, cost spikes — and only those measurements are read for '
            'meaning. A signal is not a defect: a video frame refined eleven '
            'times is iteration, the same config file rewritten eleven times is '
            'a loop, and only the second one has a fix. Every finding names the '
            'numbers it rests on, so you can check it.</div>'
            f'<p class="sub">{num(cov["reviewed"])} of {num(cov["sessions"])} '
            f'sessions reviewed — {num(cov["with_findings"])} with findings, '
            f'{num(cov["clean"])} clean — covering {usd(cov["cost_reviewed"])} '
            f'of {usd(cov["cost_total"])} ({pct:.0f}%).</p>'
            + table(["Where the fix lives", "Fixes", "Sessions", "High",
                     "Spend behind them"], lrows, numeric={1, 2, 3, 4},
                    empty="Nothing diagnosed yet.")
            + '<h3>Every finding</h3>'
            + '<p class="sub">Ranked by severity, then by what the session '
              'cost — the argument for fixing a thing is the money already '
              'spent around it.</p>'
            + table(["", "Kind", "Session", "Finding", "Fix", "Session cost"],
                    frows, numeric={5}, wrap_cols={3, 4},
                    empty="No findings.")
            + '</div>')


def _knowledge(conn) -> str:
    """Knowledge-base coverage, with coldness weighted by whether it matters."""
    try:
        from . import docs as D
        sm = D.summary(conn)
        if not sm["total"]:
            return ""
        cold = D.cold_spots(conn)
        rows = D.coverage(conn)
        active = D.cold_against_work(conn)
    except Exception:
        return ""

    warm = [r for r in rows if r["reads"]]
    agentish = [r for r in rows if r["audience"] in ("agent", "both")]
    pct = (sm["agent_facing_read"] / sm["agent_facing"] * 100) if sm["agent_facing"] else 0

    tiles = "".join([
        tile("Documents", num(sm["total"]), f'{sm["profiled"]} profiled'),
        tile("Agent-facing", num(sm["agent_facing"]),
             f'{sm["human_only"]} are human-only'),
        tile("Ever opened", num(sm["read"]),
             f'{sm["agent_facing_read"]} of the agent-facing ones'),
        tile("Agent-facing coverage", f'{pct:.0f}%'),
        tile("Cold spots", num(sm["cold_spots"]), "should be warm, never opened"),
    ])

    mx_a = max([a["work_cost"] or 0 for a in active], default=0)
    active_rows = [[a["rel_path"], num(a["word_count"]), num(a["sessions"]),
                    num(a["touches"]), usd(a["work_cost"]),
                    bar(a["work_cost"] or 0, mx_a)] for a in active]
    active_html = ""
    if active:
        worst = active[0]
        active_html = (
            '<h3>Cold documents describing work that actually happened</h3>'
            '<div class="note">These are the cold spots that cost something: the '
            'document was never opened while its subject was being worked on. '
            'Joined on <b>file activity</b>, not on commits — the most expensive '
            'work here produced no commit, so a commit-based join would miss '
            'exactly the sessions worth flagging. <b>Work cost</b> is the total '
            'spend of the sessions that touched that subject, not what the '
            'document would have saved.</div>'
            f'<p class="sub"><b>{esc(worst["rel_path"])}</b> is '
            f'{num(worst["word_count"])} words describing a subject that saw '
            f'<b>{usd(worst["work_cost"])}</b> of work across '
            f'{worst["sessions"]} sessions — and was never opened.</p>'
            + table(["Document", "Words", "Sessions on this subject",
                     "File touches", "Work cost", ""],
                    active_rows, numeric={1, 2, 3, 4}, wrap_cols={0}))

    cold_rows = [[c["rel_path"], c["agent_relevance"], num(c["word_count"]),
                  (c["topic"] or "—"), short_ts(c["modified_at"])[:10]]
                 for c in cold[:40]]
    warm_rows = [[w["rel_path"], num(w["reads"]), num(w["sessions"]),
                  w["audience"] or "?", w["agent_relevance"] or "?",
                  short_ts(w["last_read"])] for w in
                 sorted(warm, key=lambda r: -r["reads"])[:25]]

    return ('<h2 id="knowledge">Knowledge base</h2>'
            f'<div class="grid">{tiles}</div>'
            '<div class="note"><b>An unread document is not automatically a '
            'problem.</b> A contacts file going unopened by a coding agent is '
            'correct behaviour, not a gap. Every document is profiled once for who '
            'it is for and how relevant it would be to an agent working here, and '
            '<b>cold spots</b> counts only the documents that were meant to be '
            'consulted and never were. Coverage is measured against agent-facing '
            'documents, not against the whole vault.</div>'
            + active_html
            + f'<h3>All cold spots — agent-facing, never opened</h3>'
            + table(["Document", "Relevance", "Words", "Topic", "Modified"],
                    cold_rows, numeric={2}, wrap_cols={0, 3},
                    empty="Every agent-facing document has been opened.")
            + "<h3>Documents actually consulted</h3>"
            + table(["Document", "Reads", "Sessions", "Audience", "Relevance",
                     "Last read"], warm_rows, numeric={1, 2}, wrap_cols={0},
                    empty="No documents read yet."))



def _output(conn) -> str:
    o = Q.output_summary(conn)
    tiles = "".join([
        tile("Commits", num(o["commits"]),
             f'+{o["insertions"]:,} / −{o["deletions"]:,}'),
        tile("Sessions that shipped",
             f'{o["sessions_shipped"]} / {o["sessions_with_commits"]}',
             "of those that committed, reached production"),
        tile("Production deploys", num(o["prod_deploys"])),
        tile("Staging deploys", num(o["staging_deploys"])),
        tile("Reverts", num(o["reverts"]), "work that was undone"),
        tile("Pull requests", num(o["prs"])),
    ])
    deploys = Q.deployments(conn, 15)
    rows = [[d["provider"], d["environment"], short_ts(d["created_at"]),
             f'<span class="mono">{esc((d["commit_sha"] or "—")[:8])}</span>',
             d["scope"] or "—", (d["subject"] or "")[:66]] for d in deploys]
    return ('<h2 id="output">What came out</h2>'
            f'<div class="grid">{tiles}</div>'
            '<div class="note">Deployments come from GitHub and EAS and are joined to '
            'work on commit SHA. GitHub history reaches back further than telemetry, so '
            'deploy counts cover a longer window than the cost figures above.</div>'
            '<h3>Recent deployments</h3>'
            + table(["Provider", "Environment", "When", "Commit", "Scope", "Subject"],
                    rows, wrap_cols={5}, empty="No deployments recorded."))




def render(conn: sqlite3.Connection) -> str:
    generated = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    a, b = Q.observation_period(conn)
    o = Q.overview(conn)
    t = Q.friction_totals(conn)
    out = Q.output_summary(conn)

    tc = Q.time_components(conn)
    headline = "".join([
        tile("Spend", usd(o["cost_usd"]), f'{short_ts(a)[:10]} → {short_ts(b)[:10]}'),
        tile("Sessions", num(o["sessions"]),
             (f'{o["sessions_hook_only"]} started without telemetry'
              if o.get("sessions_hook_only") else "")),
        tile("Sessions that shipped", num(out["sessions_shipped"]),
             f'{out["sessions_with_commits"]} produced commits'),
        tile("Active time", _fmt_dur(tc["turn_wall"]),
             f'{_fmt_dur(tc["human_gap"])} between turns'),
        tile("Turns", num(t["turns"]), f'{t["corrections"]} corrections'),
        tile("Commits", num(out["commits"]), f'{out["reverts"]} reverted'),
        tile("Tool calls", num(o["tool_calls"])),
        tile("Blocked on you", _fmt_dur(tc["blocked_on_user"]),
             "waiting for permission"),
    ])
    nav = ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workflow Review</title>
<style>{CSS}{EXTRA_CSS}</style></head><body><div class="wrap">
<header>
  <h1>Workflow Review</h1>
  <p class="sub">What was worked on, what it cost, what shipped — generated {esc(generated)}</p>
</header>
<div class="grid">{headline}</div>
{_sessions(conn)}
<footer>
  Built from Claude Code telemetry, local git, GitHub and EAS — all on this machine.
  Cost is Claude Code's own estimate. Workflow grouping, effort signals and deploy
  joins are derived, and each is labelled where it is an inference rather than a fact.
</footer>
</div></body></html>"""


def write(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    config.ensure_dirs()
    out = Path(path or (config.REPORT_DIR / "session-review.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(conn), encoding="utf-8")
    return out
