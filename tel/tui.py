"""A colour preview of the terminal UI, rendered from real telemetry data.

Static, not yet interactive: it draws the two screens the design calls for so
the layout and palette can be judged before the navigation is built.

Colours are the same validated categorical palette the HTML charts use, in
their dark-surface steps, emitted as 24-bit ANSI where the terminal advertises
truecolor and degraded to the 256-colour cube otherwise.
"""
from __future__ import annotations

import os
import shutil

from . import db, queries as Q
from .report import short_ts
from .tuiapp import ago

# Dark-mode steps of the validated palette, plus surface tokens.
PALETTE = {
    "normal":     (0x39, 0x87, 0xe5),   # slot 1 blue
    "correction": (0xd9, 0x59, 0x26),   # slot 2 orange
    "steering":   (0x19, 0x9e, 0x70),   # slot 3 aqua
    "accent":     (0xe0, 0x87, 0x57),
    "ok":         (0x71, 0xb0, 0x88),
    "bad":        (0xe0, 0x8c, 0x76),
    "warn":       (0xd3, 0xad, 0x5f),
    "ink":        (0xec, 0xe9, 0xe3),
    "muted":      (0x9a, 0x95, 0x8c),
    "line":       (0x33, 0x31, 0x2c),
    "bar":        (0x3a, 0x38, 0x33),
}
# 256-colour fallbacks, chosen for the same role rather than nearest hue.
FALLBACK = {"normal": 68, "correction": 166, "steering": 36, "accent": 173,
            "ok": 108, "bad": 174, "warn": 179, "ink": 253, "muted": 245,
            "line": 236, "bar": 238}

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _truecolor() -> bool:
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def fg(name: str) -> str:
    if _truecolor():
        r, g, b = PALETTE[name]
        return f"\033[38;2;{r};{g};{b}m"
    return f"\033[38;5;{FALLBACK[name]}m"


def c(text, name: str, bold: bool = False) -> str:
    return f"{BOLD if bold else ''}{fg(name)}{text}{RESET}"


def rule(width: int, name: str = "line") -> str:
    return c("─" * width, name)


def bar(value: float, maximum: float, width: int, name: str = "normal") -> str:
    """Eighth-block bar, so short values still register."""
    if not maximum:
        return ""
    filled = max(0.0, min(1.0, value / maximum)) * width
    whole = int(filled)
    part = filled - whole
    eighths = " ▏▎▍▌▋▊▉█"
    tail = eighths[round(part * 8)] if whole < width else ""
    return c("█" * whole + tail, name)


def turn_strip(turns: list, width: int) -> str:
    """Each turn a run of blocks, width proportional to how long it ran.

    Allocated by largest remainder so the strip is exactly `width` cells:
    rounding each turn independently overflows the lane it has to line up
    with, and every turn keeps at least one cell while there is room.
    """
    if not turns or width <= 0:
        return ""
    total = sum(t["duration_s"] or 0 for t in turns) or 1
    exact = [(t["duration_s"] or 0) / total * width for t in turns]
    alloc = [int(x) for x in exact]

    # Give every turn a visible cell if the strip can afford it.
    if len(turns) <= width:
        for i in range(len(alloc)):
            if alloc[i] == 0:
                alloc[i] = 1
    while sum(alloc) > width:                      # trim the widest first
        alloc[alloc.index(max(alloc))] -= 1
    remainders = sorted(range(len(turns)),
                        key=lambda i: exact[i] - int(exact[i]), reverse=True)
    j = 0
    while sum(alloc) < width and remainders:
        alloc[remainders[j % len(remainders)]] += 1
        j += 1

    out = []
    for t, n in zip(turns, alloc):
        if n <= 0:
            continue
        role = ("correction" if t["is_correction"]
                else "steering" if t["is_steering"] else "normal")
        out.append(c("█" * n, role))
    return "".join(out)


def lane(events: list, positions, width: int, load_first: bool) -> str:
    """A row of dots on the same compressed axis as the turn strip."""
    cells = [" "] * width
    for i, e in enumerate(events):
        x = positions(e["ts"] if isinstance(e, dict) else e)
        if x is None:
            continue
        col = min(width - 1, max(0, int(x / 100 * width)))
        cells[col] = "◆" if (i == 0 and load_first) else "●"
    return "".join(
        c(ch, "accent") if ch == "◆" else c(ch, "muted") if ch == "●" else ch
        for ch in cells)


def render(conn, width: int | None = None) -> str:
    width = width or min(shutil.get_terminal_size((100, 40)).columns, 100)
    o = Q.overview(conn)
    a, b = Q.observation_period(conn)
    details = Q.session_detail(conn)
    out = []

    # ---- header -----------------------------------------------------------
    title = f"{BOLD}{fg('ink')}claude telemetry{RESET}"
    period = c(f"{short_ts(a)[:10]} → {short_ts(b)[:10]}", "muted")
    spend = c(f"${o['cost_usd']:,.2f}", "ok", bold=True)
    out.append(f"{title}  {period}  {spend}")
    out.append(rule(width))

    # ---- session list -----------------------------------------------------
    out.append(f"{BOLD}{fg('ink')}SESSIONS{RESET} {c(len(details), 'muted')}")
    mx = max([d["session"]["cost"] or 0 for d in details], default=1)
    for i, d in enumerate(details[:12]):
        sess = d["session"]
        sel = i == 4
        marker = c("▶", "accent") if sel else " "
        glyph = c("○", "muted") if not sess["commits"] else c("▸", "muted")
        age = ago(sess.get("last_seen") or sess.get("first_seen"))
        fresh = age == "now" or age.endswith("m")
        age_s = c(age.rjust(4) + "  ", "accent" if fresh else "muted")
        name = sess["title"][:28]
        name_s = c(name.ljust(28), "ink", bold=sel)
        commits = c(f"{sess['commits']:>2}c" if sess["commits"] else "   ", "muted")
        cost = c(f"${sess['cost']:>7,.2f}", "ok" if sess["cost"] else "muted")
        flags = []
        if d.get("doc_gaps"):
            flags.append(c(f"{len(d['doc_gaps'])}d", "warn"))
        if d.get("missed_skills"):
            flags.append(c(f"{len({m['skill_name'] for m in d['missed_skills']})}s",
                           "warn"))
        if d.get("corrections"):
            flags.append(c("!", "bad"))
        out.append(f"{marker}{glyph} {age_s}{name_s} {commits} {cost} "
                   f"{bar(sess['cost'] or 0, mx, 10)} {' '.join(flags)}")
    out.append("")
    out.append(c("○ no commits   d docs unread   s skills missed   ! corrections",
                 "muted"))
    return "\n".join(out)


def render_session(conn, session_id: str, width: int | None = None) -> str:
    width = width or min(shutil.get_terminal_size((100, 40)).columns, 100)
    from .report_sessions import _axis, _fmt_dur
    strips = Q.session_strips(conn, session_ids=[session_id])
    if not strips:
        return c("no such session", "bad")
    e = strips[0]
    sess, turns = e["session"], e["turns"]
    _, pos = _axis(turns)
    lane_w = width - 24
    out = [rule(width)]

    cost_s = c(f"${sess['cost']:,.2f}", "ok", bold=True)
    out.append(f"{BOLD}{fg('ink')}{session_id[:8]}{RESET}  "
               f"{c(sess['project_name'] or '—', 'muted')}  "
               f"{c(short_ts(sess['started'])[:16], 'muted')}   {cost_s}")
    if e.get("description"):
        out.append(c(e["description"], "ink"))
    out.append("")
    out.append(c(f"{sess['turns']} turns · {_fmt_dur(sess['active_s'])} active of "
                 f"{_fmt_dur((sess['active_s'] or 0) + (sess['gap_s'] or 0))} elapsed",
                 "muted"))
    out.append("")

    # The turns bar is the axis the lanes are plotted against, so it leads.
    out.append(f"  {c('turns'.rjust(14), 'muted')} {c('    ', 'line')} "
               f"{turn_strip(turns, lane_w)}")
    for l in (e.get("lanes") or [])[:6]:
        label = c(l["name"][:14].rjust(14), "muted")
        tag = c(l["kind"][:4].ljust(4), "line")
        is_file_lane = l["kind"] in ("files", "file", "docs")
        dots = lane(l["events"], pos, lane_w, not is_file_lane)
        count = c(f"{len(l['events'])}x", "muted")
        out.append(f"  {label} {tag} {dots} {count}")
    out.append("")
    out.append(f"  {c('▏', 'normal')}normal  {c('█', 'correction')}correction  "
               f"{c('█', 'steering')}steering  {c('◆', 'accent')}first load")

    corr = [t for t in turns if t["is_correction"]]
    if corr:
        t = max(corr, key=lambda x: x["cost_usd"] or 0)
        out.append("")
        tcost = c(f"${t['cost_usd'] or 0:,.2f}", "ok")
        quoted = '"' + (t["prompt"] or "")[:70] + '"'
        out.append(f"{BOLD}{fg('ink')}TURN {t['seq']}{RESET}  "
                   f"{c(_fmt_dur(t['duration_s']), 'muted')}  {tcost}  "
                   f"{c('correction', 'correction')}")
        out.append(f"  {c(quoted, 'ink')}")
        if t.get("suggested_fix"):
            cause = (t["cause"] or "").replace("_", " ")
            where = t["fix_location"] or ""
            out.append(f"  {c(f'→ {cause} · {where}', 'warn')}")
            out.append(f"    {c((t['suggested_fix'] or '')[:72], 'muted')}")
    return "\n".join(out)
