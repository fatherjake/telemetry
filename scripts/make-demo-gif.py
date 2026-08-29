"""Render the README demo GIF frame by frame.

The splash is Claude Code's own: the art and the theme colours were read out
of the installed binary (`strings`), not approximated, so the header in the
clip is the header you get when you run `claude`.

Every figure in the answer is real too, queried out of the database this tool
built while building itself. Nothing here is invented for the picture.

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
W, H = 900, 516
PAD = 20
# The full-block glyph's ink is exactly this tall at this size, and the
# splash is built out of block characters - any looser and the art breaks
# into stripes instead of tiling the way it does in a terminal.
LINE = 15
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"

BG = "#161616"
FG = "#d4d4d4"
DIM = "#6b6b6b"
DIMMER = "#4a4a4a"
CLAUDE = "#d77757"  # theme `claude` / `clawd_body`: rgb(215,119,87)
GREEN = "#7fb069"
CYAN = "#4ec9b0"
WHITE = "#ffffff"
YELLOW = "#d7ba7d"

font = ImageFont.truetype(FONT_PATH, 14 * SCALE, index=0)
bold = ImageFont.truetype(FONT_PATH, 14 * SCALE, index=1)

# Claude Code's startup splash, dark theme, transcribed from the binary.
C = CLAUDE
SPLASH = [
    [("Welcome to Claude Code ", C, True), ("v2.1.251", DIM, False)],
    [("..........................................................", DIMMER, False)],
    [],
    [("     *                                       █████▓▓░     ", FG, False)],
    [("                                 *         ███▓░     ░░   ", FG, False)],
    [("            ░░░░░░                        ███▓░           ", FG, False)],
    [("    ░░░   ░░░░░░░░░░                      ███▓░           ", FG, False)],
    [("   ░░░░░░░░░░░░░░░░░░░    ", FG, False), ("*", WHITE, True),
     ("                ██▓░░      ▓   ", FG, False)],
    [("                                             ░▓▓███▓▓░    ", FG, False)],
    [(" *                                 ░░░░                   ", DIM, False)],
    [("                                 ░░░░░░░░                 ", DIM, False)],
    [("                               ░░░░░░░░░░░░░░░░           ", DIM, False)],
    [("      ", FG, False), (" █████████ ", C, False),
     ("                                       ", FG, False), ("*", DIM, False)],
    [("      ", FG, False), ("██▄█████▄██", C, False),
     ("                        ", FG, False), ("*", WHITE, True)],
    [("      ", FG, False), (" █████████ ", C, False), ("     *", FG, False)],
    [(".......", DIMMER, False), ("█ █   █ █", C, False),
     ("..........................................", DIMMER, False)],
]

PROMPT = "Analyse my sessions this week and tell me which skills are dead weight"

# Real output. `telemetry_overview` and `telemetry_inventory` on this database.
BODY = [
    [("● ", CLAUDE, False), ("telemetry_overview", CYAN, False)],
    [("  └─ ", DIM, False), ("93 sessions · 321 turns · $919.94", DIM, False)],
    [("● ", CLAUDE, False), ("telemetry_inventory", CYAN, False)],
    [("  └─ ", DIM, False), ("70 skills installed · 56 never invoked", DIM, False)],
    [],
    [("56 of your 70 installed skills have never fired once. They", FG, False)],
    [("cost context in every session and return nothing:", FG, False)],
    [],
    [("   build-mcp-server     frontend-design     math-olympiad", YELLOW, False)],
    [("   claude-md-improver   plugin-structure    hook-development", YELLOW, False)],
    [],
    [("The 14 that do fire account for all of the work.", FG, False)],
]

PROMPT_ROW = len(SPLASH) + 1


def draw_line(d, row, spans, x0=PAD):
    x = x0 * SCALE
    y = (PAD + row * LINE) * SCALE
    for text, colour, is_bold in spans:
        f = bold if is_bold else font
        d.text((x, y), text, font=f, fill=colour)
        x += f.getlength(text)


def prompt_box(d, typed, cursor):
    y0 = (PAD + PROMPT_ROW * LINE) * SCALE
    y1 = (PAD + PROMPT_ROW * LINE + LINE + 9) * SCALE
    d.rounded_rectangle(
        [PAD * SCALE, y0, (W - PAD) * SCALE, y1],
        radius=5 * SCALE,
        outline="#3a3a3a",
        width=SCALE,
    )
    spans = [("> ", CLAUDE, False), (typed, FG, False)]
    if cursor:
        spans.append(("▋", CLAUDE, False))
    draw_line(d, PROMPT_ROW + 0.30, spans, PAD + 11)


def render(typed, cursor, body_lines):
    img = Image.new("RGB", (W * SCALE, H * SCALE), BG)
    d = ImageDraw.Draw(img)
    for i, spans in enumerate(SPLASH):
        if spans:
            draw_line(d, i, spans)
    prompt_box(d, typed, cursor)
    for i, spans in enumerate(body_lines):
        if spans:
            draw_line(d, PROMPT_ROW + 3.0 + i, spans)
    return img.resize((W, H), Image.LANCZOS)


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
    print(f"{len(frames)} frames")


if __name__ == "__main__":
    main()
