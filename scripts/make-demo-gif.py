"""Render the README demo GIF frame by frame.

The header is Claude Code's own session header, verbatim. The theme colour
was read out of the installed binary (`strings`) rather than eyeballed:
`claude` is rgb(215,119,87).

Every figure in the answer is real too, queried out of the database this tool
built while building itself. Nothing here is invented for the picture.

The canvas is measured from the content rather than guessed at. Hardcoding it
is how the first version came out 2.5:1, with two thirds of the frame empty
for most of its run.

    python3 scripts/make-demo-gif.py
    ffmpeg -y -framerate 22 -i frames/f%04d.png \\
      -vf "split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];\\
           [b][p]paletteuse=dither=bayer:bayer_scale=3" -loop 0 docs/demo.gif
"""

import pathlib
import shutil

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path("frames")
SCALE = 2
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"

font = ImageFont.truetype(FONT_PATH, 16 * SCALE, index=0)
bold = ImageFont.truetype(FONT_PATH, 16 * SCALE, index=1)

# The mascot is quadrant blocks spread over three rows, so the rows only join
# up when the line step is exactly the block glyph's ink height. Layout is in
# scaled pixels throughout for the same reason: a fractional unscaled line
# height rounds differently per row and splits the mascot open.
LINE = font.getbbox("█")[3] - font.getbbox("█")[1]
CHAR = font.getlength("M")
PAD = 18 * SCALE

BG = "#161616"
FG = "#d4d4d4"
DIM = "#6b6b6b"
CLAUDE = "#d77757"  # theme `claude` / `clawd_body`: rgb(215,119,87)
CYAN = "#4ec9b0"
YELLOW = "#d7ba7d"

# Claude Code's session header, verbatim.
HEADER = [
    [(" ▐▛███▛█   ", CLAUDE, False), ("Claude Code ", FG, False), ("v2.1.251", DIM, False)],
    [("▝▜██████▀  ", CLAUDE, False), ("Opus 5 (1M context) · Claude Max", DIM, False)],
    [("  ▝▝ ▝▝    ", CLAUDE, False), ("~/Workspace/telemetry", DIM, False)],
]

PROMPT = "Analyse my sessions this week and tell me which skills are dead weight"
WRAP_AT = 46  # characters, before the typed prompt wraps to a second line

# Real output. `telemetry_overview` and `telemetry_inventory` on this database.
BODY = [
    [("● ", CLAUDE, False), ("telemetry_overview", CYAN, False)],
    [("  └─ ", DIM, False), ("93 sessions · 321 turns · $919.94", DIM, False)],
    [("● ", CLAUDE, False), ("telemetry_inventory", CYAN, False)],
    [("  └─ ", DIM, False), ("70 skills installed · 56 never invoked", DIM, False)],
    [],
    [("56 of your 70 installed skills have never", FG, False)],
    [("fired once. They cost context in every", FG, False)],
    [("session and return nothing:", FG, False)],
    [],
    [("   build-mcp-server     frontend-design", YELLOW, False)],
    [("   claude-md-improver   plugin-structure", YELLOW, False)],
    [("   math-olympiad        hook-development", YELLOW, False)],
    [],
    [("The 14 that do fire account for all of it.", FG, False)],
]


def wrap(text):
    """Break the typed prompt on whole words, the way a terminal would."""
    lines, line = [], ""
    for word in text.split(" "):
        candidate = f"{line} {word}".strip()
        if len(candidate) > WRAP_AT and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    return lines


BOX_TOP = len(HEADER) + 1
BOX_ROWS = len(wrap(PROMPT)) + 0.9  # the border sits proud of the text
BODY_TOP = BOX_TOP + BOX_ROWS + 1.1
TOTAL_ROWS = BODY_TOP + len(BODY)


def measure():
    """A canvas that fits the widest line and the tallest frame, and no more."""
    widest = max(
        [sum((bold if b else font).getlength(t) for t, _, b in s) for s in HEADER + BODY if s]
        + [CHAR * (WRAP_AT + 5)]  # the prompt box: its text, marker and border
    )
    w, h = int(PAD * 2 + widest), int(PAD * 2 + TOTAL_ROWS * LINE)
    return w + w % 2, h + h % 2  # even dimensions keep the 2x downscale clean


W, H = measure()


def draw_line(d, row, spans, x0=PAD):
    x, y = x0, PAD + row * LINE
    for text, colour, is_bold in spans:
        f = bold if is_bold else font
        d.text((x, y), text, font=f, fill=colour)
        x += f.getlength(text)


def prompt_box(d, typed, cursor):
    d.rounded_rectangle(
        [PAD, PAD + BOX_TOP * LINE, W - PAD, PAD + (BOX_TOP + BOX_ROWS) * LINE],
        radius=5 * SCALE,
        outline="#3a3a3a",
        width=SCALE,
    )
    for i, text in enumerate(wrap(typed) if typed else [""]):
        spans = [("> ", CLAUDE, False)] if i == 0 else [("  ", FG, False)]
        spans.append((text, FG, False))
        if cursor and i == len(wrap(typed) if typed else [""]) - 1:
            spans.append(("▋", CLAUDE, False))
        draw_line(d, BOX_TOP + 0.42 + i, spans, PAD + 10 * SCALE)


def render(typed, cursor, body_lines):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for i, spans in enumerate(HEADER):
        draw_line(d, i, spans)
    prompt_box(d, typed, cursor)
    for i, spans in enumerate(body_lines):
        if spans:
            draw_line(d, BODY_TOP + i, spans)
    return img.resize((W // SCALE, H // SCALE), Image.LANCZOS)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    frames = []

    for i in range(8):  # cursor blinking on an empty prompt
        frames.append(render("", i % 4 < 2, []))
    for i in range(1, len(PROMPT) + 1):  # the question, typed
        frames.append(render(PROMPT[:i], True, []))
    frames.extend([render(PROMPT, True, [])] * 10)

    for n in range(1, len(BODY) + 1):  # the answer, a line at a time
        img = render(PROMPT, False, BODY[:n])
        frames.append(img)
        if n in (2, 4):  # let each tool result land
            frames.extend([img] * 4)

    frames.extend([frames[-1]] * 36)  # hold on the finished answer

    for i, f in enumerate(frames):
        f.save(OUT / f"f{i:04d}.png")
    print(f"{len(frames)} frames at {W // SCALE}x{H // SCALE} ({W / H:.2f}:1)")


if __name__ == "__main__":
    main()
