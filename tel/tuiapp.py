"""Interactive terminal UI over the database.

Built on `curses` from the standard library, so the project keeps its "clone
and run, nothing to install" property. The trade-off is colour: curses colour
pairs address the 256-colour cube, not 24-bit, so the palette here is the
256-colour approximation of the same validated categorical steps the HTML
charts use.

The view model is a flat list of sessions, because a session is the unit of
work: one session, one thing someone set out to do, with its own cost, its own
commits and its own turns. `g` groups them by project if you want that.
"""
from __future__ import annotations

import curses
import datetime as _dt
import textwrap
from dataclasses import dataclass, field

from . import db, queries as Q
from .report import short_ts
from .report_sessions import _axis, _fmt_dur

# Palette roles -> 256-colour indices, chosen by role rather than nearest hue
# so meaning survives on a terminal with a different ramp.
ROLES = {
    "ink": 253, "muted": 245, "line": 238, "bar": 236,
    "normal": 68, "correction": 166, "steering": 36,
    "accent": 173, "ok": 108, "bad": 174, "warn": 179,
    "sel": 254,
}
PAIRS: dict[str, int] = {}


def attr(role: str, bold: bool = False, reverse: bool = False) -> int:
    a = curses.color_pair(PAIRS.get(role, 0))
    if bold:
        a |= curses.A_BOLD
    if reverse:
        a |= curses.A_REVERSE
    return a


def ago(ts: str | None, now: _dt.datetime | None = None) -> str:
    """Compact relative age: 20m, 3h, 2d. Coarse on purpose - the column is
    there to say "recent or not", and a precise timestamp is in the detail."""
    if not ts:
        return "—"
    try:
        when = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    secs = (now - when).total_seconds()
    if secs < 0:
        return "now"
    if secs < 90:
        return "now"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    if secs < 86400:
        return f"{secs / 3600:.0f}h"
    if secs < 86400 * 7:
        return f"{secs / 86400:.0f}d"
    if secs < 86400 * 60:
        return f"{secs / 86400 / 7:.0f}w"
    return f"{secs / 86400 / 30:.0f}mo"


def _turn_kind(t) -> str:
    """A harness-injected prompt is not a normal turn: nobody typed it, and
    the effort metrics already exclude it, so it should not be drawn as one."""
    if t.get("is_system"):
        return "system"
    if t["is_correction"]:
        return "correction"
    if t["is_steering"]:
        return "steering"
    return "normal"


def _role_for(kind: str) -> str:
    return "bar" if kind == "system" else kind


@dataclass
class Row:
    kind: str                 # session | project
    key: str
    depth: int
    label: str
    cost: float
    meta: str
    flags: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    expandable: bool = False
    ts: str | None = None


def _recency(detail) -> str:
    """When a session was last active."""
    s = detail["session"]
    return s.get("last_seen") or s.get("first_seen") or ""


class App:
    GROUPINGS = ("session", "project")
    SORTS = ("recent", "cost", "turns")

    def __init__(self, conn):
        self.conn = conn
        self.grouping = 0
        self.sort = 0
        self.filter = ""
        self.filtering = False
        self.expanded: set[str] = set()
        self.cursor = 0
        self.top = 0
        self.detail_scroll = 0
        self.sess_row = 0           # which lane (or the turns bar) is selected
        self.sess_col = 0           # which dot / segment within it
        # The list never goes away; focus moves into the session pane beside
        # it. "session" is a focus, not a separate screen.
        self.focus = "list"         # list | session
        self.chat = False           # the chat pane is open over the session
        self.chat_input = ""
        self.chat_msgs: list = []   # the loaded conversation for self.chat_sid
        self.chat_sid: str | None = None
        self.chat_busy = False
        self.chat_proc = None
        self.chat_scroll = 0
        self.mode = "list"          # list | help
        self.status = ""
        self.analysing = False      # a background analyse is folding in new data
        self.follow = False         # keep the view current as work happens
        self._follow_thread = None
        self._stop = None           # threading.Event, set on quit
        self._reload_at = None      # set by the follower; the draw loop acts
        self._last_describe = 0.0
        self._raw_sig = None        # size+mtime of the raw archive, last seen
        self._data_version = None   # SQLite's counter of other writers
        self.now = _dt.datetime.now(_dt.timezone.utc)
        self.load()

    # -- data ---------------------------------------------------------------

    def load(self) -> None:
        self.details = Q.session_detail(self.conn)
        self.by_session = {d["session"]["session_id"]: d for d in self.details}
        self.rebuild()

    # How often the follower looks for new work. Two seconds is under the
    # threshold where a list feels stale and far above the cost of stat-ing a
    # handful of files.
    POLL_S = 2.0
    # Naming a session costs a model call, and during active work every turn
    # makes it eligible again. Once every few minutes is enough for a sentence
    # that barely changes between turns.
    DESCRIBE_EVERY_S = 300.0

    def start_following(self) -> None:
        """Keep the view current while you work, without polling the database
        blindly.

        Two signals, both cheap. The raw archive growing means telemetry has
        arrived that nobody has normalised yet, so this analyses it. SQLite's
        `data_version` changing means some *other* process wrote to the database
        - another `analyse`, a `connect`, a cron - so this just reloads. Doing
        neither costs one `stat` per raw file every couple of seconds.
        """
        import threading

        if self._follow_thread is not None:
            return
        self.follow = True
        self._stop = threading.Event()
        self._follow_thread = threading.Thread(target=self._follow_loop,
                                               daemon=True)
        self._follow_thread.start()

    def stop_following(self) -> None:
        self.follow = False
        if self._stop is not None:
            self._stop.set()
        self._follow_thread = None
        self.status = ""

    def _raw_signature(self):
        """A cheap fingerprint of the raw archive: size and mtime per file."""
        from . import config
        try:
            return tuple(sorted(
                (p.name, st.st_size, int(st.st_mtime))
                for p in config.RAW_DIR.glob("*.jsonl")
                for st in (p.stat(),)))
        except OSError:
            return None

    def _follow_loop(self) -> None:
        import time
        from . import db, ingest

        conn = None
        try:
            conn = db.connect()
            self._data_version = self._version(conn)
            # Deliberately unset, so the first tick sees a difference and
            # brings the database up to date the moment the UI opens.
            self._raw_sig = None
            first = True
            while not self._stop.is_set():
                if not first:
                    self._stop.wait(self.POLL_S)
                    if self._stop.is_set():
                        break
                first = False
                sig = self._raw_signature()
                if sig != self._raw_sig:
                    self._raw_sig = sig
                    self.analysing = True
                    self.status = "analysing…"
                    try:
                        # Naming is throttled here rather than in analyse: a
                        # session in progress becomes eligible again on every
                        # turn, and the sentence hardly moves between them.
                        due = (time.monotonic() - self._last_describe
                               >= self.DESCRIBE_EVERY_S)
                        res = ingest.analyse(conn, progress=self._set_status,
                                             describe=due)
                        if due:
                            self._last_describe = time.monotonic()
                        bits = [f"{res['logs']:,} logs", f"{res['metrics']:,} metrics",
                                f"{res['traces']:,} spans"]
                        if res.get("described"):
                            bits.append(f"{res['described']} named")
                        self.status = "analysed " + ", ".join(bits)
                    except Exception as exc:
                        self.status = f"analyse failed: {type(exc).__name__}: {exc}"
                    finally:
                        self.analysing = False
                    self._data_version = self._version(conn)
                    self._reload_at = time.monotonic()
                    continue
                version = self._version(conn)
                if version != self._data_version:
                    # Somebody else wrote to the database. Nothing to compute,
                    # just show it.
                    self._data_version = version
                    self._reload_at = time.monotonic()
        except Exception as exc:
            self.status = f"follow stopped: {type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _version(conn) -> int | None:
        try:
            return conn.execute("PRAGMA data_version").fetchone()[0]
        except Exception:
            return None

    def _set_status(self, message: str) -> None:
        """Called from the follower thread; the draw loop picks it up."""
        self.status = message

    def _poll_follow(self) -> None:
        """Take up anything the follower left, without losing your place."""
        self.now = _dt.datetime.now(_dt.timezone.utc)
        if self._reload_at is None:
            return
        self._reload_at = None
        keep = self.current.key if self.current else None
        keep_top, keep_scroll = self.top, self.detail_scroll
        self.load()
        if keep is not None:
            for i, r in enumerate(self.rows):
                if r.key == keep:
                    self.cursor = i
                    break
        self.top, self.detail_scroll = keep_top, keep_scroll

    def rebuild(self) -> None:
        """One row per session. That is the whole model.

        A session is a self-contained piece of work: its own prompts, its own
        cost, its own commits. Grouping by project is offered because a person
        works on several things, not because a project is a unit of work.
        """
        rows: list[Row] = []
        mode = self.GROUPINGS[self.grouping]

        def session_row(d, depth):
            sess = d["session"]
            e = d["effort"]
            flags = []
            if d.get("doc_gaps"):
                flags.append((f"{len(d['doc_gaps'])}d", "warn"))
            if d.get("missed_skills"):
                flags.append((f"{len(d['missed_skills'])}s", "warn"))
            if d.get("corrections"):
                flags.append(("!", "bad"))
            facts = f"{e['turns']}t"
            if sess["commits"]:
                facts += f" {sess['commits']}c"
            return Row("session", sess["session_id"], depth,
                       sess["title"], sess["cost"] or 0, facts,
                       flags, {"detail": d}, ts=_recency(d) or None)

        if mode == "session":
            for d in self._sorted(self.details):
                rows.append(session_row(d, 0))
        else:  # project
            groups: dict[str, list] = {}
            for d in self.details:
                groups.setdefault(d["session"].get("project_id") or "—", []).append(d)
            for proj, ds in sorted(groups.items(),
                                   key=lambda kv: max(_recency(x) for x in kv[1]),
                                   reverse=True):
                name = ds[0]["session"].get("project_name") or proj
                total = sum(x["session"]["cost"] or 0 for x in ds)
                rows.append(Row("project", f"proj:{proj}", 0, name,
                                total, f"{len(ds)} sessions", [], {},
                                expandable=True,
                                ts=max(_recency(x) for x in ds) or None))
                if f"proj:{proj}" in self.expanded:
                    for d in self._sorted(ds):
                        rows.append(session_row(d, 1))

        if self.filter:
            f = self.filter.lower()
            rows = [r for r in rows if f in r.label.lower()]
        self.rows = rows
        self.cursor = min(self.cursor, max(0, len(rows) - 1))

    def _sorted(self, details):
        key = self.SORTS[self.sort]
        if key == "cost":
            return sorted(details, key=lambda d: -(d["session"]["cost"] or 0))
        if key == "recent":
            return sorted(details, key=_recency, reverse=True)
        return sorted(details, key=lambda d: -(d["effort"]["turns"] or 0))

    @property
    def current(self) -> Row | None:
        return self.rows[self.cursor] if self.rows else None

    # -- drawing ------------------------------------------------------------

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        # Arrow keys arrive as ESC-prefixed sequences. Without a short escape
        # delay a slow terminal can deliver the bare ESC first, which would
        # read as "go back" and drop out of the session view mid-keypress.
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        for i, (name, idx) in enumerate(ROLES.items(), start=1):
            try:
                curses.init_pair(i, idx if curses.COLORS >= 256 else -1, -1)
            except curses.error:
                curses.init_pair(i, curses.COLOR_WHITE, -1)
            PAIRS[name] = i
        while True:
            stdscr.erase()
            try:
                self.draw(stdscr)
            except curses.error:
                pass
            stdscr.refresh()
            # Blocking reads while a question is in flight would freeze the
            # spinner and the answer with it, and while following they would
            # freeze the list, so poll unless there is genuinely nothing that
            # can change on its own.
            # A spinner and a status line need a fast tick; a list that only
            # changes when work happens does not, and redrawing seven times a
            # second for hours is a waste of a laptop battery.
            if self.chat_busy or self.analysing:
                stdscr.timeout(150)
            elif self.follow:
                stdscr.timeout(1000)
            else:
                stdscr.timeout(-1)
            ch = stdscr.getch()
            if ch == -1:
                self._poll_chat()
                self._poll_follow()
                continue
            if self.handle(ch) is False:
                self.stop_following()
                return

    def draw(self, scr) -> None:
        h, w = scr.getmaxyx()
        if self.mode == "help":
            return self._draw_help(scr, h, w)

        split = w >= 108
        in_session = self.focus == "session" and self._session_strip() is not None
        if not split:
            # No room for both: the focused pane takes the screen.
            if self.chat:
                self._draw_header(scr, w)
                self._draw_chat(scr, 2, h - 3, 2, w - 4)
                self._draw_footer(scr, h, w)
                return
            if in_session:
                self._draw_header(scr, w)
                self._draw_session_pane(scr, 2, h - 3, 2, w - 4)
                self._draw_footer(scr, h, w)
                return
            self._draw_header(scr, w)
            self._draw_list(scr, 2, h - 3, w, focused=True)
            self._draw_footer(scr, h, w)
            return

        list_w = 46
        self._draw_header(scr, w)
        self._draw_list(scr, 2, h - 3, list_w, focused=not in_session)
        for y in range(2, h - 1):
            self._put(scr, y, list_w, "│",
                      attr("accent" if in_session else "line"))
        # Two panes: the sessions, and the session. There is no third thing a
        # session belongs to any more.
        main_x = list_w + 2
        main_w = w - main_x - 1
        top, height = 2, h - 3
        if self.chat:
            self._draw_chat(scr, top, height, main_x, main_w)
        elif in_session:
            self._draw_session_pane(scr, top, height, main_x, main_w)
        else:
            self._draw_detail(scr, top, height, main_x, main_w)
        self._draw_footer(scr, h, w)

    # -- chat ---------------------------------------------------------------

    def _chat_session(self) -> str | None:
        r = self.current
        d = r.data.get("detail") if r else None
        return d["session"]["session_id"] if d else None

    def _open_chat(self) -> None:
        sid = self._chat_session()
        if not sid:
            return
        from . import sessionchat
        self.chat = True
        self.chat_input = ""
        self.chat_scroll = 0
        if self.chat_sid != sid:
            self.chat_sid = sid
            self.chat_msgs = sessionchat.history(self.conn, sid)

    def _send_chat(self) -> None:
        """Spawn the model and return immediately; the loop polls for it.

        A blocking call in a worker thread would have to fight `getch` for the
        GIL, and curses does not release it - the first version of this
        deadlocked every time. Polling a detached process keeps one thread and
        keeps the UI drawing.
        """
        from . import sessionchat
        q = self.chat_input.strip()
        if not q or self.chat_busy or not self.chat_sid:
            return
        prompt = sessionchat.build_prompt(self.conn, self.chat_sid, q)
        if not prompt:
            return
        sessionchat.record(self.conn, self.chat_sid, "user", q)
        self.chat_input = ""
        self.chat_msgs.append({"role": "user", "text": q, "ts": None})
        self.chat_scroll = 0
        self.chat_proc = sessionchat.spawn(prompt)
        self.chat_busy = True

    def _poll_chat(self) -> None:
        if not (self.chat_busy and self.chat_proc):
            return
        from . import sessionchat
        answer = sessionchat.reap(self.chat_proc)
        if answer is None:
            return
        self.chat_busy = False
        self.chat_proc = None
        self.chat_msgs.append({"role": "assistant", "text": answer, "ts": None})
        self.chat_scroll = 0
        if self.chat_sid:
            sessionchat.record(self.conn, self.chat_sid, "assistant", answer,
                               sessionchat.MODEL)

    def _chat_lines(self, width) -> list:
        lines: list = []
        for m in self.chat_msgs:
            you = m["role"] == "user"
            text = m["text"] or ""
            err = text.startswith("__error__")
            if err:
                text = text[len("__error__"):].strip()
            lines.append([("you" if you else "telemetry",
                           "accent" if you else "ok")])
            for para in text.split("\n"):
                if not para.strip():
                    lines.append([])
                    continue
                for ln in textwrap.wrap(para, max(8, width - 2)):
                    lines.append([("  " + ln, "bad" if err else "ink")])
            lines.append([])
        if self.chat_busy:
            lines.append([("telemetry", "ok")])
            lines.append([("  thinking…", "muted")])
        return lines

    def _draw_chat(self, scr, top, height, x0, width) -> None:
        sid = self.chat_sid or "?"
        x = self._put(scr, top, x0, "chat", attr("ink", bold=True))
        x = self._put(scr, top, x + 1, f"· {sid[:8]}", attr("muted"))
        self._put(scr, top, x + 2,
                  "esc closes · ↑↓ scrolls" if not self.chat_busy
                  else "waiting for an answer", attr("muted"))
        self._put(scr, top + 1, x0, "─" * max(1, width - 1), attr("line"))

        # The last row of the pane butts against the footer rule, so the
        # input sits one above it and the transcript stops above that.
        input_y = top + height - 2
        body_top = top + 2
        body_h = max(1, input_y - 1 - body_top)
        lines = self._chat_lines(width)
        if not lines:
            lines = [[("Ask anything about this session. It is answered from "
                       "the recorded", "muted")],
                     [("telemetry only, and the conversation is kept.",
                       "muted")]]
        # Pinned to the bottom like every chat, scrolled back with the arrows.
        max_scroll = max(0, len(lines) - body_h)
        self.chat_scroll = max(0, min(self.chat_scroll, max_scroll))
        start = max_scroll - self.chat_scroll
        for i in range(body_h):
            idx = start + i
            if idx >= len(lines):
                break
            x = x0
            for span in lines[idx]:
                x = self._put(scr, body_top + i, x, span[0], attr(span[1]))

        self._put(scr, input_y - 1, x0, "─" * max(1, width - 1), attr("line"))
        prompt = "…" if self.chat_busy else ">"
        self._put(scr, input_y, x0, prompt,
                  attr("muted" if self.chat_busy else "accent", bold=True))
        # Keep the caret in view on a long question by showing the tail.
        room = max(4, width - 3)
        shown = self.chat_input[-room:]
        x = self._put(scr, input_y, x0 + 2, shown, attr("ink"))
        if not self.chat_busy:
            self._put(scr, input_y, x, "█", attr("accent"))

    def _handle_chat(self, ch) -> bool | None:
        if ch == 27:
            self.chat = False
        elif ch in (10, 13, curses.KEY_ENTER):
            self._send_chat()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.chat_input = self.chat_input[:-1]
        elif ch == curses.KEY_UP:
            self.chat_scroll += 2
        elif ch == curses.KEY_DOWN:
            self.chat_scroll = max(0, self.chat_scroll - 2)
        elif ch == curses.KEY_PPAGE:
            self.chat_scroll += 10
        elif ch == curses.KEY_NPAGE:
            self.chat_scroll = max(0, self.chat_scroll - 10)
        elif 32 <= ch < 127:
            self.chat_input += chr(ch)
        return None

    def _session_strip(self):
        r = self.current
        d = r.data.get("detail") if r else None
        return (d or {}).get("strip")

    def _put(self, scr, y, x, text, a=0) -> int:
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return x
        text = text[: max(0, w - x - 1)]
        try:
            scr.addstr(y, x, text, a)
        except curses.error:
            pass
        return x + len(text)

    def _draw_header(self, scr, w) -> None:
        o = Q.overview(self.conn)
        a, b = Q.observation_period(self.conn)
        x = self._put(scr, 0, 1, "claude telemetry", attr("ink", bold=True))
        x = self._put(scr, 0, x + 2,
                      f"{short_ts(a)[:10]} → {short_ts(b)[:10]}", attr("muted"))
        spend = f"${o['cost_usd']:,.2f}"
        self._put(scr, 0, max(x + 2, w - len(spend) - 2), spend, attr("ok", bold=True))
        self._put(scr, 1, 0, "─" * (w - 1), attr("line"))

    def _draw_list(self, scr, top, height, width, focused=True) -> None:
        self.now = _dt.datetime.now(_dt.timezone.utc)
        if self.cursor < self.top:
            self.top = self.cursor
        if self.cursor >= self.top + height:
            self.top = self.cursor - height + 1
        for i in range(height):
            idx = self.top + i
            if idx >= len(self.rows):
                break
            r = self.rows[idx]
            y = top + i
            sel = idx == self.cursor
            rev = curses.A_REVERSE if (sel and focused) else 0
            if sel:
                self._put(scr, y, 0, " " * (width - 1), attr("bar") | rev)
                if not focused:
                    self._put(scr, y, 0, "▎", attr("accent"))
            x = 1 + r.depth * 2
            # Whether a session produced commits is worth seeing at a glance:
            # it separates work that landed from work that only cost.
            d = r.data.get("detail") or {}
            no_commits = (r.kind == "session"
                          and not (d.get("session") or {}).get("commits"))
            if r.kind == "session":
                glyph = "○" if no_commits else "▸"
            elif r.expandable:
                glyph = "▾" if r.key in self.expanded else "▸"
            else:
                glyph = "·"
            x = self._put(scr, y, x, glyph + " ",
                          attr("accent" if sel else "muted") | rev)
            age = ago(r.ts, self.now)
            fresh = age in ("now",) or age.endswith("m")
            x = self._put(scr, y, x, age.rjust(4) + "  ",
                          attr("accent" if fresh else "muted") | rev)
            room = max(4, width - x - 20)
            x = self._put(scr, y, x, r.label[:room].ljust(room),
                          attr("ink", bold=sel) | rev)
            cost = f"${r.cost:,.2f}"
            self._put(scr, y, width - 19, cost.rjust(9),
                      attr("ok" if r.cost else "muted") | rev)
            fx = width - 9
            for text, role in r.flags[:3]:
                fx = self._put(scr, y, fx, text + " ", attr(role) | rev)

    def _detail_lines(self, width) -> list:
        return self._detail_lines_for(self.current, width)

    def _detail_lines_for(self, r, width) -> list:
        if not r:
            return [[("no rows", "muted")]]
        d = r.data.get("detail")
        if d:
            return self._session_lines(d, width)
        if r.kind == "project":
            return [[(r.label, "ink", True)],
                    [],
                    [(f"{r.meta}   ${r.cost:,.2f}", "muted")],
                    [],
                    [("→ expand to its sessions", "muted")]]
        return [[(r.label, "ink")]]

    def _session_lines(self, d, width) -> list:
        """One session, whole: what it was for, what it did, what it cost."""
        sess = d["session"]
        st = d.get("strip") or {}
        turns = d.get("turns") or []
        e = d["effort"]
        title = sess["title"]
        lines = [[(title[:width - 20], "ink", True)]]
        # The title falls back to the short id when nothing has named the
        # session, and printing both would be the same eight characters twice.
        second = ([] if title == sess["session_id"][:8]
                  else [(sess["session_id"][:8] + "   ", "muted")])
        lines.append(second + [(f"${sess['cost'] or 0:,.2f}", "ok"),
                               (f"   {sess['project_name'] or '—'}", "muted")])
        if not d.get("description"):
            from .narrate import naming_note
            lines.append([(naming_note(sess)[:width - 2], "muted")])
        lines.append([])
        strip_sess = st.get("session") or {}
        active = strip_sess.get("active_s")
        elapsed = (active or 0) + (strip_sess.get("gap_s") or 0)
        facts = f"{e['turns']} turns"
        if active:
            facts += (f" · {_fmt_dur(active)} active of "
                      f"{_fmt_dur(elapsed)} elapsed")
        lines.append([(facts, "muted")])
        if sess["commits"]:
            lines.append([(f"{sess['commits']} commits · "
                           f"+{sess['insertions']}/−{sess['deletions']}"
                           + (f" · {sess['prod_deploys']} prod deploys"
                              if sess["prod_deploys"] else "")
                           + (f" · {sess['reverted']} reverted"
                              if sess["reverted"] else ""), "muted")])
        if e["corrections"] or e["steers"] or e["tool_failures"]:
            lines.append([(f"{e['corrections']} corrections · {e['steers']} steering"
                           f" · {e['tool_failures']} tool failures", "muted")])
        lines.append([])
        _, pos = _axis(turns)
        lane_w = max(10, width - 26)
        # The turns bar is the axis every lane is plotted against, so it reads
        # first and the dots below it have something to be relative to.
        spans = [("turns".rjust(13) + "      ", "muted")]
        for text, role in self._strip_spans(turns, lane_w):
            spans.append((text, role))
        lines.append(spans)
        for l in (d.get("lanes") or [])[:7]:
            cells = [" "] * lane_w
            is_file = l["kind"] in ("files", "file", "docs", "hot")
            for i, ev in enumerate(l["events"]):
                x = pos(ev["ts"] if isinstance(ev, dict) else ev)
                if x is None:
                    continue
                col = min(lane_w - 1, max(0, int(x / 100 * lane_w)))
                cells[col] = "●" if (i or is_file) else "◆"
            spans = [(f"{l['name'][:13].rjust(13)} ", "muted"),
                     (f"{l['kind'][:4].ljust(4)} ", "line")]
            spans += [(ch, "accent" if ch == "◆" else "muted") for ch in cells]
            spans.append((f" {len(l['events'])}×", "muted"))
            lines.append(spans)
        lines.append([])
        # Two blocks that look alike and mean opposite things: what to change
        # next time, and what went wrong last time. Without headings they read
        # as one undifferentiated wall.
        if d.get("commits"):
            lines.append([("COMMITS", "muted")])
            for cm in d["commits"][:6]:
                lines.append([(f"  {cm['commit_sha'][:8]} ", "muted"),
                              ((cm["subject"] or "")[:width - 14], "ink")])
            lines.append([])
        if d.get("docs_read"):
            lines.append([("DOCS READ", "muted")])
            for x in d["docs_read"][:4]:
                lines.append([(f"  {x['rel_path'][:width-10]}", "ok"),
                              (f" {x['reads']}×", "muted")])
        if d.get("doc_gaps"):
            lines.append([("NOT READ", "muted")])
            for x in d["doc_gaps"][:4]:
                lines.append([(f"  {x['rel_path'][:width-10]}", "warn")])
        if d.get("missed_skills"):
            lines.append([("SKILLS MISSED", "muted")])
            names = sorted({m["skill_name"] for m in d["missed_skills"]})
            lines.append([("  " + ", ".join(names)[:width - 4], "warn")])
        if lines and lines[-1] != []:
            lines.append([])

        found = d.get("findings") or Q.session_findings(self.conn,
                                                        sess["session_id"])
        if found:
            lines.append([("SUGGESTIONS", "muted")])
        for fi, f in enumerate(found):
            sev = (f["severity"] or "low").lower()
            if fi and lines and lines[-1] != []:
                lines.append([])
            lines.append([(sev.upper(), {"high": "bad", "medium": "warn"}
                           .get(sev, "muted")),
                          (f"  {f['kind'].replace('_', ' ')}", "accent")])
            for ln in textwrap.wrap(f["finding"] or "", width - 4)[:3]:
                lines.append([("  " + ln, "ink")])
            if f["evidence"]:
                for ln in textwrap.wrap(f["evidence"], width - 4)[:2]:
                    lines.append([("  " + ln, "muted")])
            if f["fix"]:
                lines.append([(f"  → {(f['fix_location'] or '').replace('_',' ')}",
                               "ok")])
                for ln in textwrap.wrap(f["fix"], width - 6)[:3]:
                    lines.append([("    " + ln, "muted")])
        corr = [t for t in turns if t["is_correction"]]
        if corr:
            t = max(corr, key=lambda x: x["cost_usd"] or 0)
            if not lines or lines[-1] != []:
                lines.append([])
            # Only the costliest is shown, so the heading says so rather than
            # letting one correction stand in for however many there were.
            head = [("CORRECTIONS", "muted")]
            if len(corr) > 1:
                head.append((f"   {len(corr)} · costliest shown", "muted"))
            lines.append(head)
            lines.append([(f"TURN {t['seq']}", "ink", True),
                          (f"  {_fmt_dur(t['duration_s'])}  "
                           f"${t['cost_usd'] or 0:,.2f}  ", "muted"),
                          ("correction", "correction")])
            for ln in textwrap.wrap('"' + (t["prompt"] or "") + '"', width - 4)[:3]:
                lines.append([("  " + ln, "ink")])
            if t.get("suggested_fix"):
                lines.append([(f"  → {(t['cause'] or '').replace('_',' ')} · "
                               f"{t['fix_location'] or ''}", "warn")])
                for ln in textwrap.wrap(t["suggested_fix"], width - 6)[:3]:
                    lines.append([("    " + ln, "muted")])
        return lines

    @staticmethod
    def _strip_alloc(turns, width) -> list[int]:
        """Cells per turn, summing to exactly `width` (largest remainder)."""
        if not turns or width <= 0:
            return []
        total = sum(t["duration_s"] or 0 for t in turns) or 1
        exact = [(t["duration_s"] or 0) / total * width for t in turns]
        alloc = [int(x) for x in exact]
        if len(turns) <= width:
            alloc = [max(1, a) for a in alloc]
        while sum(alloc) > width:
            alloc[alloc.index(max(alloc))] -= 1
        order = sorted(range(len(turns)),
                       key=lambda i: exact[i] - int(exact[i]), reverse=True)
        j = 0
        while sum(alloc) < width and order:
            alloc[order[j % len(order)]] += 1
            j += 1
        return alloc

    @staticmethod
    def _strip_spans(turns, width):
        if not turns or width <= 0:
            return []
        total = sum(t["duration_s"] or 0 for t in turns) or 1
        exact = [(t["duration_s"] or 0) / total * width for t in turns]
        alloc = [int(x) for x in exact]
        if len(turns) <= width:
            alloc = [max(1, a) for a in alloc]
        while sum(alloc) > width:
            alloc[alloc.index(max(alloc))] -= 1
        order = sorted(range(len(turns)), key=lambda i: exact[i] - int(exact[i]),
                       reverse=True)
        j = 0
        while sum(alloc) < width and order:
            alloc[order[j % len(order)]] += 1
            j += 1
        out = []
        for t, n in zip(turns, alloc):
            if n <= 0:
                continue
            role = ("correction" if t["is_correction"]
                    else "steering" if t["is_steering"] else "normal")
            out.append(("█" * n, role))
        return out

    def _draw_detail(self, scr, top, height, x0, width) -> None:
        lines = self._detail_lines(width)
        self.detail_scroll = max(0, min(self.detail_scroll,
                                        max(0, len(lines) - height)))
        for i in range(height):
            idx = self.detail_scroll + i
            if idx >= len(lines):
                break
            x = x0
            for span in lines[idx]:
                text, role = span[0], span[1]
                bold = len(span) > 2 and span[2]
                x = self._put(scr, top + i, x, text, attr(role, bold=bold))

    def _session_rows(self, st, width):
        """Lanes plus the turns bar, each with the screen column of every event
        so the cursor can land on a specific dot or segment."""
        turns = st["turns"]
        _, pos = _axis(turns)
        lane_w = max(10, width - 26)
        alloc = self._strip_alloc(turns, lane_w)
        starts, acc = [], 0
        for n in alloc:
            starts.append(acc)
            acc += n
        # Turns first, so the axis leads and the cursor lands on it.
        rows = [{"type": "turns", "turns": turns, "alloc": alloc,
                 "starts": starts, "n": len(turns), "lane_w": lane_w}]
        for l in (st.get("lanes") or [])[:8]:
            cols = []
            for ev in l["events"]:
                x = pos(ev["ts"] if isinstance(ev, dict) else ev)
                cols.append(None if x is None
                            else min(lane_w - 1, max(0, int(x / 100 * lane_w))))
            rows.append({"type": "lane", "lane": l, "cols": cols,
                         "n": len(l["events"]), "lane_w": lane_w})
        return rows

    @staticmethod
    def _short_path(path: str, root: str | None, width: int) -> str:
        """Trim a path to the repository it lives in, then to the width."""
        if not path:
            return ""
        out = path
        if root and out.startswith(root.rstrip("/") + "/"):
            out = out[len(root.rstrip("/")) + 1:]
        else:
            for marker in ("/Users/", "/private/tmp/", "/tmp/"):
                if out.startswith(marker):
                    parts = out.split("/")
                    if len(parts) > 4:
                        out = "…/" + "/".join(parts[-3:])
                    break
        return out if len(out) <= width else "…" + out[-(width - 1):]

    def _event_rows(self, row, root, width) -> list:
        """One line per event in the selected lane, newest position last."""
        out = []
        if row["type"] == "turns":
            for i, t in enumerate(row["turns"]):
                kind = _turn_kind(t)
                prompt = (t["prompt"] or "").strip().replace("\n", " ")
                out.append((i, [
                    (f"{t['seq']:>3} ", "muted"),
                    (f"{_fmt_dur(t['duration_s']):>5} ", "muted"),
                    (f"${t['cost_usd'] or 0:>7,.2f}  ", "ok"),
                    (f"{kind:<10} ", _role_for(kind)),
                    (prompt[:max(10, width - 32)], "ink"),
                ]))
            return out
        l = row["lane"]
        for i, ev in enumerate(l["events"]):
            when = str(ev.get("ts") or "")[11:19]
            path = ev.get("path")
            if path:
                op = (ev.get("op") or "read")[:5]
                out.append((i, [
                    (f"{i + 1:>4} ", "muted"),
                    (f"{when} ", "muted"),
                    (f"{op:<6}", "ok" if op == "read" else "warn"),
                    (self._short_path(path, root, max(10, width - 22)), "ink"),
                ]))
            else:
                label = ("loaded" if i == 0 and l["kind"] not in
                         ("files", "file", "docs", "hot") else "call")
                out.append((i, [
                    (f"{i + 1:>4} ", "muted"),
                    (f"{when} ", "muted"),
                    (f"{label:<7}", "accent" if label == "loaded" else "muted"),
                    ((ev.get("what") or "")[:max(10, width - 24)], "ink"),
                ]))
        return out

    def _event_detail(self, row, idx, width) -> list:
        """What the highlighted dot or segment actually is."""
        if row["type"] == "turns":
            t = row["turns"][idx]
            kind = _turn_kind(t)
            out = [[(f"turn {t['seq']}", "ink", True),
                    (f"  {_fmt_dur(t['duration_s'])}  "
                     f"${t['cost_usd'] or 0:,.2f}  {t['tool_calls'] or 0} tools  ",
                     "muted"),
                    (kind, _role_for(kind))]]
            if t.get("gap_before_s"):
                out.append([(f"  {_fmt_dur(t['gap_before_s'])} gap before it",
                             "muted")])
            for ln in textwrap.wrap('"' + (t["prompt"] or "") + '"', width - 4)[:4]:
                out.append([("  " + ln, "ink")])
            if t.get("suggested_fix"):
                out.append([(f"  → {(t['cause'] or '').replace('_',' ')} · "
                             f"{t['fix_location'] or ''}", "warn")])
                for ln in textwrap.wrap(t["suggested_fix"], width - 6)[:2]:
                    out.append([("    " + ln, "muted")])
            return out

        l = row["lane"]
        ev = l["events"][idx]
        when = str(ev.get("ts") or "")[11:19]
        head = [(f"{l['name']}", "ink", True),
                (f"  {l['kind']}  {when} UTC", "muted")]
        out = [head]
        path = ev.get("path")
        if path:
            out.append([(f"  {ev.get('op', 'read')}  ", "muted"),
                        (path[-(width - 12):], "ok" if ev.get("op") == "read"
                         else "warn")])
        elif ev.get("what"):
            label = "loaded" if idx == 0 and l["kind"] not in (
                "files", "file", "docs") else "call"
            out.append([(f"  {label}  ", "muted"), (ev["what"], "ink")])
        same = sum(1 for cx in row["cols"] if cx is not None
                   and cx == row["cols"][idx])
        crowd = f" · {same} share this column" if same > 1 else ""
        out.append([(f"  {idx + 1} of {row['n']} in this lane{crowd}", "muted")])
        return out

    def _draw_session_pane(self, scr, y0, height, px, pw) -> None:
        """The session view rendered into a pane, so the list stays on screen
        beside it while focus is here."""
        st = self._session_strip()
        if st is None:
            return
        r = self.current
        s = st["session"]
        rows = self._session_rows(st, pw)
        self.sess_row = max(0, min(self.sess_row, len(rows) - 1))
        cur = rows[self.sess_row]
        self.sess_col = max(0, min(self.sess_col, max(0, cur["n"] - 1)))

        y = y0
        title = (st.get("description") or s["session_id"][:8])
        cost = f"${s['cost'] or 0:,.2f}"
        self._put(scr, y, px, title[:pw - len(cost) - 2], attr("ink", bold=True))
        self._put(scr, y, px + pw - len(cost), cost, attr("ok", bold=True))
        y += 1
        self._put(scr, y, px, f"{s['session_id'][:8]}  "
                              f"{(s['project_name'] or '—')[:pw - 14]}",
                  attr("muted"))
        y += 1
        elapsed = (s["active_s"] or 0) + (s["gap_s"] or 0)
        self._put(scr, y, px, f"{s['turns']} turns · {_fmt_dur(s['active_s'])} "
                              f"active of {_fmt_dur(elapsed)} elapsed",
                  attr("muted"))
        y += 2

        strip_x = px + 19
        for ri, row in enumerate(rows):
            sel_row = ri == self.sess_row
            if row["type"] == "lane":
                l = row["lane"]
                label, tag, n = l["name"][:13], l["kind"][:4], row["n"]
            else:
                label, tag, n = "turns", "", row["n"]
            self._put(scr, y, px, label.rjust(13),
                      attr("accent" if sel_row else "muted", bold=sel_row))
            if sel_row:
                # One column in from the pane edge, so the lane marker never
                # eats the divider it sits next to.
                self._put(scr, y, max(0, px - 1), "▎", attr("accent"))
            self._put(scr, y, px + 14, tag.ljust(4), attr("line"))
            if row["type"] == "lane":
                cells = [" "] * row["lane_w"]
                is_file = l["kind"] in ("files", "file", "docs", "hot")
                for i, col in enumerate(row["cols"]):
                    if col is None:
                        continue
                    cells[col] = "●" if (i or is_file) else "◆"
                # Several events routinely land in one column, so highlight the
                # column the cursor's event falls in rather than tracking which
                # event happens to own the cell - otherwise stepping through a
                # crowded column highlights nothing at all.
                sel_x = None
                if sel_row and 0 <= self.sess_col < len(row["cols"]):
                    sel_x = row["cols"][self.sess_col]
                for cx, ch in enumerate(cells):
                    if ch == " ":
                        continue
                    hit = sel_x is not None and cx == sel_x
                    glyph = "◉" if hit else ch
                    self._put(scr, y, strip_x + cx, glyph,
                              attr("accent" if (glyph == "◆" or hit) else "muted",
                                   bold=hit))
            else:
                for ti, (start, width_) in enumerate(zip(row["starts"],
                                                        row["alloc"])):
                    if width_ <= 0:
                        continue
                    t = row["turns"][ti]
                    role = _role_for(_turn_kind(t))
                    hit = sel_row and ti == self.sess_col
                    # U+259A (quadrant mixline) rather than a shade block: same
                    # cell metrics, so the run keeps its height and width
                    # exactly, but the diagonal texture reads clearly at any
                    # font weight. Colour is untouched, so the turn's kind
                    # survives the highlight.
                    self._put(scr, y, strip_x + start,
                              ("▚" if hit else "█") * width_,
                              attr(role, bold=hit))
            self._put(scr, y, strip_x + row["lane_w"] + 1, f"{n}×",
                      attr("muted"))
            y += 1

        y += 1
        self._put(scr, y, px, "─" * max(1, pw - 1), attr("line"))
        y += 1

        title = ("turns" if cur["type"] == "turns" else cur["lane"]["name"])
        x = self._put(scr, y, px, title, attr("ink", bold=True))
        x = self._put(scr, y, x + 1, f"· {cur['n']} items", attr("muted"))
        self._put(scr, y, x + 2, "←→ navigate", attr("accent"))
        y += 1

        root = ((r.data.get("detail") or {}).get("session") or {}).get("project_id")
        root = self._project_root(root)
        entries = self._event_rows(cur, root, pw - 4)
        avail = max(3, y0 + height - y - 1)
        top = max(0, min(self.sess_col - avail // 2, len(entries) - avail))
        for i in range(avail):
            ei = top + i
            if ei >= len(entries):
                break
            idx, spans = entries[ei]
            picked = idx == self.sess_col
            if picked:
                self._put(scr, y, px, "▸", attr("accent", bold=True))
            x = px + 2
            for span in spans:
                text, role = span[0], span[1]
                x = self._put(scr, y, x, text, attr(role, bold=picked))
            y += 1
        if len(entries) > avail:
            self._put(scr, y, px + 2, f"… {len(entries) - avail} more",
                      attr("muted"))


    def _draw_help(self, scr, h, w) -> None:
        keys = [
            ("↑ ↓ / j k", "move"), ("→ / l / space", "expand"),
            ("← / h", "collapse"), ("enter / →", "step into a session pane"),
            ("in session: ↑ ↓", "pick a lane or the turns bar"),
            ("in session: ← →", "walk its dots and segments"),
            ("in session: ←", "at the first item, back to the list"),
            ("c", "chat about the session under the cursor"),
            ("in chat: enter", "ask · ↑↓ scroll · esc close"),
            ("esc", "back to the list"), ("J K", "scroll detail"),
            ("g", "cycle grouping: session / project"),
            ("s", "cycle sort: recent / cost / turns"),
            ("/", "filter by name, esc clears"),
            ("r", "reload from the database"),
            ("f", "follow: analyse new telemetry as it arrives"),
            ("?", "this help"), ("q", "quit"),
        ]
        self._put(scr, 1, 2, "keys", attr("ink", bold=True))
        for i, (k, desc) in enumerate(keys):
            self._put(scr, 3 + i, 4, k.ljust(30), attr("accent"))
            self._put(scr, 3 + i, 34, desc, attr("muted"))
        self._put(scr, 5 + len(keys), 4, "any key to go back", attr("muted"))

    def _draw_footer(self, scr, h, w) -> None:
        self._put(scr, h - 2, 0, "─" * (w - 1), attr("line"))
        if self.filtering:
            self._put(scr, h - 1, 1, f"/{self.filter}", attr("accent"))
            return
        if self.chat:
            self._put(scr, h - 1, 1,
                      "type a question   enter asks   ↑↓ scrolls   esc closes",
                      attr("muted"))
            return
        if self.focus == "session":
            self._put(scr, h - 1, 1,
                      "↑↓ lane   ←→ step through events   ← at the start / esc"
                      "  back   c chat", attr("muted"))
            hint = "? help   q quit"
            self._put(scr, h - 1, max(0, w - len(hint) - 2), hint, attr("muted"))
            return
        bits = (f"{len(self.rows)} rows   group:{self.GROUPINGS[self.grouping]}"
                f"   sort:{self.SORTS[self.sort]}")
        if self.filter:
            bits += f"   /{self.filter}"
        # Paused is a state and outranks news: a stale "analysed 3 logs" would
        # otherwise sit there implying the view is still keeping up.
        if self.analysing:
            bits += f"   {self.status or 'analysing…'}"
        elif not self.follow:
            bits += "   paused"
        elif self.status:
            bits += f"   {self.status}"
        self._put(scr, h - 1, 1, bits, attr("muted"))
        hint = "? help   q quit"
        self._put(scr, h - 1, max(0, w - len(hint) - 2), hint, attr("muted"))

    # -- input --------------------------------------------------------------

    def handle(self, ch) -> bool | None:
        if self.filtering:
            if ch in (27,):
                self.filtering, self.filter = False, ""
            elif ch in (10, 13, curses.KEY_ENTER):
                self.filtering = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.filter = self.filter[:-1]
            elif 32 <= ch < 127:
                self.filter += chr(ch)
            self.rebuild()
            return None

        if self.chat:
            return self._handle_chat(ch)

        if self.mode == "help":
            self.mode = "list"
            return None

        if ch in (ord("q"),):
            return False
        if ch == ord("?"):
            self.mode = "help"
            return None

        if ch == ord("c") and self._chat_session():
            self._open_chat()
            return None

        if self.focus == "session":
            return self._handle_session(ch)

        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(self.rows) - 1, self.cursor + 1)
            self.detail_scroll = 0
            self.sess_row = self.sess_col = 0
        elif ch in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
            self.detail_scroll = 0
            self.sess_row = self.sess_col = 0
        elif ch == ord("J"):
            self.detail_scroll += 3
        elif ch == ord("K"):
            self.detail_scroll = max(0, self.detail_scroll - 3)
        elif ch in (curses.KEY_NPAGE,):
            self.cursor = min(len(self.rows) - 1, self.cursor + 10)
        elif ch in (curses.KEY_PPAGE,):
            self.cursor = max(0, self.cursor - 10)
        elif ch in (curses.KEY_RIGHT, ord("l"), ord(" ")):
            r = self.current
            if r and r.kind == "session":
                # Right on a session steps in, so the whole descent from
                # list to dot is the same key repeated.
                self._enter_session()
            elif r and r.expandable:
                self.expanded.symmetric_difference_update({r.key})
                self.rebuild()
        elif ch in (curses.KEY_LEFT, ord("h")):
            r = self.current
            if r and r.key in self.expanded:
                self.expanded.discard(r.key)
                self.rebuild()
        elif ch in (10, 13, curses.KEY_ENTER):
            r = self.current
            if r and r.kind == "session":
                self._enter_session()
            elif r and r.expandable:
                self.expanded.symmetric_difference_update({r.key})
                self.rebuild()
        elif ch == 27:
            self.mode = "list"
            self.detail_scroll = 0
        elif ch == ord("g"):
            self.grouping = (self.grouping + 1) % len(self.GROUPINGS)
            self.cursor = 0
            self.rebuild()
        elif ch == ord("s"):
            self.sort = (self.sort + 1) % len(self.SORTS)
            self.rebuild()
        elif ch == ord("/"):
            self.filtering, self.filter = True, ""
        elif ch == ord("r"):
            self.load()
        elif ch == ord("f"):
            if self.follow:
                self.stop_following()
            else:
                self.start_following()
        return None


    def _project_root(self, project_id):
        if not project_id:
            return None
        row = self.conn.execute(
            "SELECT repo_root FROM projects WHERE project_id=?",
            (project_id,)).fetchone()
        return row["repo_root"] if row else None

    def _enter_session(self) -> None:
        self.focus = "session"
        self.detail_scroll = 0
        self.sess_row = 0
        self.sess_col = 0

    def _handle_session(self, ch) -> bool | None:
        """Two axes: up/down picks a lane, left/right walks its events.

        Left is overloaded: it steps back through the current lane and only
        leaves the pane once there is nothing left to step back to, so the
        common motion never costs you your place."""
        r = self.current
        strip = self._session_strip()
        if not (r and strip):
            self.focus = "list"
            return None
        rows = self._session_rows(strip, 100)
        idx = min(self.sess_row, len(rows) - 1)
        if ch == 27:
            self.focus = "list"
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sess_row = min(len(rows) - 1, self.sess_row + 1)
            self.sess_col = 0
        elif ch in (curses.KEY_UP, ord("k")):
            self.sess_row = max(0, self.sess_row - 1)
            self.sess_col = 0
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.sess_col = min(rows[idx]["n"] - 1, self.sess_col + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            if self.sess_col > 0:
                self.sess_col -= 1
            else:
                self.focus = "list"
        elif ch in (curses.KEY_HOME, ord("0")):
            self.sess_col = 0
        elif ch in (curses.KEY_END, ord("$")):
            self.sess_col = max(0, rows[idx]["n"] - 1)
        return None


def launch(conn, follow: bool = True) -> None:
    app = App(conn)
    if follow:
        app.start_following()
    try:
        curses.wrapper(app.run)
    finally:
        app.stop_following()
