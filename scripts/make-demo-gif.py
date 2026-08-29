"""Render the README demo GIF frame by frame.

Every figure shown is real, queried out of the database this tool built while
building itself; nothing here is invented for the picture.
"""
import pathlib
import shutil
from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path("frames")
SCALE = 2
W, H = 880, 606
PAD = 22
LINE = 21
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"

BG = "#161616"
FG = "#d4d4d4"
DIM = "#6b6b6b"
CLAUDE = "#d97757"
GREEN = "#7fb069"
CYAN = "#4ec9b0"
WHITE = "#ffffff"
YELLOW = "#d7ba7d"

font = ImageFont.truetype(FONT_PATH, 15 * SCALE, index=0)
bold = ImageFont.truetype(FONT_PATH, 15 * SCALE, index=1)

CHAR_W = font.getlength("M") / SCALE


def blank():
    return Image.new("RGB", (W * SCALE, H * SCALE), BG)


def draw_line(d, row, spans, x0=PAD):
    """spans: list of (text, colour, bold?)"""
    x = x0 * SCALE
    y = (PAD + row * LINE) * SCALE
    for text, colour, is_bold in spans:
        f = bold if is_bold else font
        d.text((x, y), text, font=f, fill=colour)
        x += f.getlength(text)


def header(d):
    """A rounded welcome box, in the shape Claude Code opens with."""
    x0, y0 = PAD * SCALE, PAD * SCALE
    x1, y1 = (W - PAD) * SCALE, (PAD + LINE * 4 + 10) * SCALE
    d.rounded_rectangle([x0, y0, x1, y1], radius=6 * SCALE, outline="#3a3a3a", width=SCALE)
    inner = PAD + 14
    draw_line(d, 0.45, [("✻ ", CLAUDE, True), ("Welcome to Claude Code", FG, True)], inner)
    draw_line(d, 1.75, [("/help for help, /status for your current setup", DIM, False)], inner)
    draw_line(d, 3.05, [("cwd: ~/Workspace/telemetry", DIM, False)], inner)


def prompt_box(d, row, typed, cursor):
    """The bordered input line, with text appearing a character at a time."""
    y0 = (PAD + row * LINE) * SCALE
    y1 = (PAD + row * LINE + LINE + 12) * SCALE
    d.rounded_rectangle(
        [PAD * SCALE, y0, (W - PAD) * SCALE, y1], radius=5 * SCALE, outline="#3a3a3a", width=SCALE
    )
    spans = [("> ", CLAUDE, False), (typed, FG, False)]
    if cursor:
        spans.append(("▋", CLAUDE, False))
    draw_line(d, row + 0.35, spans, PAD + 12)


PROMPT = "Analyse my sessions this week and tell me which skills are dead weight"

# Everything below is real output from this database.
BODY = [
    [("● ", CLAUDE, False), ("telemetry_overview", CYAN, False)],
    [("  └─ ", DIM, False), ("93 sessions · 321 turns · $919.94", DIM, False)],
    [],
    [("● ", CLAUDE, False), ("telemetry_inventory", CYAN, False)],
    [("  └─ ", DIM, False), ("70 skills installed · 56 never invoked", DIM, False)],
    [],
    [("Last 7 days: ", FG, False), ("93 sessions", WHITE, True), (", ", FG, False),
     ("321 turns", WHITE, True), (", ", FG, False), ("$919.94", GREEN, True), (".", FG, False)],
    [],
    [("56 of your 70 installed skills have never fired once. They", FG, False)],
    [("cost context in every session and return nothing:", FG, False)],
    [],
    [("   build-mcp-server     frontend-design     math-olympiad", YELLOW, False)],
    [("   claude-md-improver   plugin-structure    hook-development", YELLOW, False)],
    [],
    [("The 14 that do fire account for all of the work. Prune the", FG, False)],
    [("rest, or fix their descriptions so they match what you", FG, False)],
    [("actually ask for.", FG, False)],
]


def render(typed, cursor, body_lines, prompt_row):
    img = blank()
    d = ImageDraw.Draw(img)
    header(d)
    prompt_box(d, prompt_row, typed, cursor)
    for i, spans in enumerate(body_lines):
        if spans:
            draw_line(d, prompt_row + 3 + i, spans)
    return img


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    frames = []
    prompt_row = 6.2

    # 1. empty prompt, blinking
    for i in range(8):
        frames.append(render("", i % 4 < 2, [], prompt_row))

    # 2. type the question
    for i in range(1, len(PROMPT) + 1):
        frames.append(render(PROMPT[:i], True, [], prompt_row))
    for _ in range(10):
        frames.append(render(PROMPT, True, [], prompt_row))

    # 3. reveal the answer, a line at a time
    for n in range(1, len(BODY) + 1):
        img = render(PROMPT, False, BODY[:n], prompt_row)
        frames.append(img)
        if n in (2, 5):  # pause after each tool result
            frames.extend([img] * 4)

    # 4. hold the finished frame
    frames.extend([frames[-1]] * 34)

    for i, f in enumerate(frames):
        f.resize((W, H), Image.LANCZOS).save(OUT / f"f{i:04d}.png")
    print(f"{len(frames)} frames")


if __name__ == "__main__":
    main()
