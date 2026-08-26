"""Extract file reads and writes from shell commands.

The file tools (Read/Edit/Write) are not how most real work touches files: a
`grep` over a docs vault, a `sed -n` to page through a file, or a `cat >` to
write one never appear in tool telemetry. Without parsing commands, a database
built on tool events reports zero activity for files that were read dozens of
times.

This is inference, so it is deliberately conservative: it favours precision
over recall and labels every row with a confidence, rather than guessing
widely and presenting the result as fact.
"""
from __future__ import annotations

import re
import shlex

# Programs that read the files named in their arguments.
READERS = {
    "cat", "head", "tail", "less", "more", "bat", "wc", "nl", "od", "strings",
    "md5", "shasum", "sha256sum", "file", "stat", "column", "fold",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "awk", "jq", "yq", "xmllint",
    "diff", "cmp", "sort", "uniq", "cut", "tr", "sed", "python3", "python",
    "node", "source", "pdftotext", "open", "qlmanage", "sips",
}
# Programs whose *last* argument is written, earlier ones read.
COPIERS = {"cp", "mv", "rsync", "install", "ln"}
WRITERS = {"touch", "mkdir", "tee"}
DELETERS = {"rm", "unlink", "rmdir", "trash"}
# Programs that enumerate a directory rather than read a file.
LISTERS = {"ls", "find", "du", "tree", "fd"}
GREPPERS = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
# Programs whose first positional argument is a pattern or script, not a file.
PATTERN_FIRST = GREPPERS | {"sed", "awk", "jq", "yq"}

# Flags that take a value, so the following token is not a path.
VALUE_FLAGS = {"-e", "--include", "--exclude", "--exclude-dir", "-m", "--max-count",
               "-A", "-B", "-C", "--color", "-o", "--output", "-t", "--type",
               "-name", "-iname", "-path", "-maxdepth", "-mindepth", "-exec",
               "-d", "-s", "--since", "--until", "-n"}

def _split_segments(text: str) -> list[str]:
    """Split on shell separators, but never inside quotes.

    `grep -rn "icon\\|logo" file.md` uses a pipe inside a quoted regex; a naive
    split on `|` cuts the command in half and loses the filename entirely.
    """
    out, buf, i, quote = [], [], 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "&" and i + 1 < len(text) and text[i + 1] == "&":
            out.append("".join(buf)); buf = []; i += 2; continue
        if ch == "|":
            if i + 1 < len(text) and text[i + 1] == "|":
                i += 1
            out.append("".join(buf)); buf = []; i += 1; continue
        if ch in ";\n":
            out.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out
_HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?")
_PY_WRITE = re.compile(r"(write_text\s*\(|open\s*\([^)]*['\"][wa]\+?['\"]|\.dump\s*\()")
_PY_READ = re.compile(r"(read_text\s*\(|\.load\s*\(|open\s*\((?![^)]*['\"][wa])) ?")
_QUOTED_PATH = re.compile(r"['\"]([^'\"\n]{2,200}?\.[A-Za-z0-9]{1,8})['\"]")

# A token is path-like if it has a separator or a file extension, and is not a
# flag, a URL, a glob-only fragment, or a shell variable.
_PATHY = re.compile(r"^(?!-)(?!https?://)(?!\$)[^|&;<>]*"
                    r"(/[^|&;<>]*|\.[A-Za-z0-9]{1,10})$")
_SKIP_TOKENS = {".", "..", "-", "/dev/null", "/dev/stdin", "/dev/stdout"}


def _is_pathy(tok: str) -> bool:
    if not tok or tok in _SKIP_TOKENS or tok.startswith("-"):
        return False
    if tok.startswith(("$", "`", "http://", "https://")):
        return False
    if "=" in tok.split("/")[0]:            # FOO=bar
        return False
    return bool(_PATHY.match(tok))


# Interpreters take a program on the command line; that code is not a path.
INTERPRETERS = {"python", "python3", "node", "bun", "deno", "sh", "bash", "zsh",
                "perl", "ruby", "php", "osascript"}
_CODE_FLAGS = {"-c", "-e", "--eval", "--execute"}


def _clean(path: str) -> str | None:
    p = path.strip().strip("'\"").rstrip(";")
    # Shell fragments leak trailing punctuation: `sed -n '20,28p'` and slice
    # syntax from inline code produce tokens like `28:` or `120]`.
    p = p.rstrip(":,)]}\\")
    if not p or p in _SKIP_TOKENS:
        return None
    if not _plausible(p):
        return None
    return p


def _plausible(path: str) -> bool:
    """Reject tokens that are clearly not filenames.

    The parser is inference, and a wrong path is worse than a missing one: it
    invents activity against a file that was never touched.
    """
    if any(ch in path for ch in "()[]{}<>|&$`*?"[:8]):
        return False
    if "\n" in path or len(path) > 400:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return False
    if last.isdigit() or re.fullmatch(r"[0-9.,:;-]+", last):
        return False           # `70`, `1,60`, `28:`
    if not re.search(r"[A-Za-z]", last):
        return False           # no letters anywhere: not a filename
    return True


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """Return (command without heredoc bodies, list of heredoc bodies)."""
    bodies = []
    out = command
    for m in _HEREDOC.finditer(command):
        tag = m.group(1)
        end = re.search(rf"^{re.escape(tag)}\s*$", command[m.end():], re.M)
        if end:
            body = command[m.end():m.end() + end.start()]
            bodies.append(body)
            out = out.replace(body, "\n")
    return out, bodies


def parse(command: str | None) -> list[tuple[str, str, str]]:
    """Return [(path, operation, confidence)] for one command line.

    operation is one of read / write / delete / search.
    """
    if not command:
        return []
    head, bodies = _strip_heredocs(command)
    found: dict[tuple[str, str], str] = {}

    def add(path, op, conf):
        p = _clean(path)
        if not p:
            return
        key = (p, op)
        rank = {"high": 3, "medium": 2, "low": 1}
        if key not in found or rank[conf] > rank[found[key]]:
            found[key] = conf

    # Python inline scripts and heredoc bodies: look for explicit file IO.
    inline = [head] if any(k in head for k in ("write_text", "read_text",
                                               "open(", "Image.open")) else []
    for body in bodies + inline:
        for m in _QUOTED_PATH.finditer(body):
            path = m.group(1)
            if _PY_WRITE.search(body):
                add(path, "write", "medium")
            if _PY_READ.search(body):
                add(path, "read", "medium")

    for segment in _split_segments(head):
        segment = segment.strip()
        if not segment or segment.startswith("#"):
            continue

        # Redirects are the least ambiguous signal there is.
        redirect = r"""(?:"([^"]+)"|'([^']+)'|([^\s|&;<>]+))"""
        for m in re.finditer(r"(?<![0-9<>])>>?\s*" + redirect, segment):
            add(next(g for g in m.groups() if g), "write", "high")
        for m in re.finditer(r"(?<![0-9<>])<(?!<)\s*" + redirect, segment):
            add(next(g for g in m.groups() if g), "read", "high")
        segment = re.sub(r"(?<![0-9<>])[<>]{1,2}\s*" + redirect, " ", segment)

        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue

        # Skip wrappers and env assignments to reach the real program.
        i = 0
        while i < len(tokens) and (tokens[i] in ("sudo", "env", "time", "nohup",
                                                 "exec", "command", "npx", "then",
                                                 "do", "else")
                                   or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])):
            i += 1
        if i >= len(tokens):
            continue
        prog = tokens[i].rsplit("/", 1)[-1]
        args = tokens[i + 1:]

        # `sed -i` edits in place; `sed -n ... p` prints.
        sed_inplace = prog == "sed" and any(a.startswith("-i") for a in args)
        recursive = any(a in ("-r", "-R", "-rn", "-rl", "-rI", "--recursive")
                        or (a.startswith("-") and not a.startswith("--")
                            and "r" in a[1:] and prog in GREPPERS)
                        for a in args)
        # `find docs`, `ls docs`, `grep -r pat docs` all name a bare directory,
        # which has neither a slash nor an extension. For those programs the
        # positional arguments are targets by definition, so accept them.
        bare_ok = prog in LISTERS or (prog in GREPPERS and recursive)

        positional = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in VALUE_FLAGS or (prog in INTERPRETERS and a in _CODE_FLAGS):
                skip_next = True
                continue
            if a.startswith("-") or a == "":
                continue
            if _is_pathy(a) or (bare_ok and _is_bare_name(a)):
                positional.append(a)

        # grep/sed/awk/jq take a pattern or script first; it is not a file.
        if prog in PATTERN_FIRST and not any(a.startswith(("-e", "-f")) for a in args):
            candidates = positional[1:]
        else:
            candidates = positional
        if prog in PATTERN_FIRST and not candidates and len(positional) == 1 \
                and _looks_like_file(positional[0]):
            candidates = positional          # `grep foo.txt` with no pattern arg

        # A few git subcommands move files around on disk.
        if prog == "git" and args:
            sub = next((a for a in args if not a.startswith("-")), None)
            gpaths = [a for a in args[1:] if _is_pathy(a) and a != sub]
            if sub == "mv" and len(gpaths) >= 2:
                add(gpaths[0], "delete", "high")
                add(gpaths[-1], "write", "high")
            elif sub == "rm":
                for path in gpaths:
                    add(path, "delete", "high")
            elif sub == "checkout" and "--" in args:
                for path in gpaths:
                    add(path, "write", "high")   # restores the file on disk
            continue

        # `curl -o file` / `wget -O file` write; the flag value is otherwise skipped.
        if prog in ("curl", "wget"):
            for j, a in enumerate(args):
                if a in ("-o", "-O", "--output") and j + 1 < len(args):
                    add(args[j + 1], "write", "high")

        if prog in DELETERS:
            for path in candidates:
                add(path, "delete", "high")
        elif prog in COPIERS and len(candidates) >= 2:
            for path in candidates[:-1]:
                add(path, "read", "high")
            add(candidates[-1], "write", "high")
        elif prog in WRITERS:
            for path in candidates:
                add(path, "write", "high")
        elif sed_inplace:
            for path in candidates:
                add(path, "write", "high")
        elif prog in LISTERS:
            for path in candidates:
                add(path, "search", "medium")
        elif prog in READERS:
            conf = "high" if prog in ("cat", "head", "tail", "wc", "less",
                                      "more", "bat", "nl") else "medium"
            for path in candidates:
                # A recursive grep over a directory is a search, not a read of
                # one file - recording it as a read would be a lie about which
                # file was opened.
                op = "search" if (recursive and _is_bare_name(path)
                                  or path.endswith("/")) else "read"
                add(path, op, conf if op == "read" else "medium")
    return [(p, op, conf) for (p, op), conf in found.items()]


def _is_bare_name(tok: str) -> bool:
    """A plain directory-ish name: no slash, no extension, no metacharacters."""
    return bool(re.match(r"^[A-Za-z0-9_.-]+$", tok)) and "." not in tok


_CD_PREFIX = re.compile(r"^\s*cd\s+([\"']?)(/[^\s;&|\"']+)\1")


def base_dir(command: str | None) -> str | None:
    """A leading `cd /abs/path &&` tells us what relative paths are relative to."""
    if not command:
        return None
    m = _CD_PREFIX.match(command)
    return m.group(2) if m else None


def _looks_like_file(tok: str) -> bool:
    """A lone grep argument is a file only if it really looks like one."""
    return "/" in tok or re.search(r"\.[A-Za-z0-9]{1,10}$", tok) is not None
