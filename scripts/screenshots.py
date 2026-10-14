"""Regenerate the pictures in docs/media from `leasehold demo`'s scripted crowd.

    uv run python scripts/screenshots.py [--seconds 120] [--out docs/media]

It starts the demo machine, polls it as `leasehold top` does, and keeps every frame. The
busiest frame becomes `top.png` and the last `GIF_FRAMES` become `top.gif`, each drawn by
`top.render` itself into a terminal-coloured HTML page that headless Chrome photographs. The web
view (`leasehold ui`) is photographed live against the same machine, light and dark.

Run it with Playwright for the live web view, `uv run --with playwright python
scripts/screenshots.py`. It needs headless Chrome (`CHROME`, else Playwright's headless shell or
`google-chrome` on PATH) and ffmpeg for the GIF. Nothing here is part of the package.
"""

from __future__ import annotations

import argparse
import copy
import glob
import html
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from leasehold import top
from leasehold.client import Client
from leasehold.demo import Demo
from leasehold.webui import UiServer

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 110, 34  # the terminal, in cells
GIF_FRAMES = 24  # two seconds apart
CELL_W, CELL_H = 8.4, 17  # px of one cell at the font size below

#: Terminal colours, as a dark theme draws them: the 16 base colours, then xterm's 6x6x6 cube
#: and grey ramp.
BASE = [
    "#1d2127", "#e06c75", "#7fbf6a", "#e5c07b", "#5fa3e8", "#c678dd", "#56b6c2", "#c8ccd4",
    "#5c6370", "#ff7b86", "#98d58a", "#f0d08a", "#7ab8ff", "#d68ef0", "#6fcfdc", "#ffffff",
]  # fmt: skip
BACKGROUND, FOREGROUND = "#14181d", "#d7dce2"


def xterm(n: int) -> str:
    if n < 0:
        return ""
    if n < 16:
        return BASE[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        r, g, b = steps[n // 36], steps[n // 6 % 6], steps[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _css(style: str, pools: list[str]) -> str:
    """The CSS for one `top` style, from the same `top.look` the terminal uses."""
    import curses

    look = top.look(style, color=True, colors=256, pools=pools)
    rules = []
    if look.fg >= 0:
        rules.append(f"color:{xterm(look.fg)}")
    if look.bg >= 0:
        rules.append(f"background:{xterm(look.bg)}")
    if look.attr & curses.A_BOLD:
        rules.append("font-weight:700")
    if look.attr & curses.A_DIM:
        rules.append("opacity:.6")
    if look.attr & curses.A_UNDERLINE:
        rules.append("text-decoration:underline")
    return ";".join(rules)


def terminal_html(lines: list[top.Line], pools: list[str], title: str) -> str:
    rows = []
    for line in lines:
        spans = []
        for text, style in line:
            css = _css(style, pools) if style else ""
            body = html.escape(text)
            spans.append(f'<span style="{css}">{body}</span>' if css else body)
        rows.append("".join(spans))
    width = round(WIDTH * CELL_W) + 32
    return f"""<!doctype html><meta charset="utf-8"><style>
html,body{{margin:0;background:#0b0e11}}
.win{{width:{width}px;margin:0;border-radius:8px;overflow:hidden;background:{BACKGROUND};
  box-shadow:0 0 0 1px #2a3038}}
.bar{{height:28px;background:#22272e;color:#9aa4ae;font:12px system-ui,sans-serif;display:flex;
  align-items:center;justify-content:center;position:relative}}
.bar i{{position:absolute;top:9px;width:10px;height:10px;border-radius:50%;background:#444c56}}
pre{{margin:0;padding:12px 16px;color:{FOREGROUND};font:14px/{CELL_H}px "DejaVu Sans Mono",
  monospace;white-space:pre}}
</style><div class="win"><div class="bar"><i style="left:12px"></i><i style="left:28px"></i>
<i style="left:44px"></i>{html.escape(title)}</div><pre>{chr(10).join(rows)}</pre></div>"""


def chrome() -> str:
    found = os.environ.get("CHROME") or next(
        iter(
            sorted(
                glob.glob(
                    os.path.expanduser(
                        "~/.cache/ms-playwright/chromium_headless_shell-*/"
                        "chrome-headless-shell-linux64/chrome-headless-shell"
                    )
                )
            )
        ),
        None,
    )
    found = found or shutil.which("google-chrome") or shutil.which("chromium")
    if not found:
        sys.exit("no headless Chrome: set CHROME")
    return found


def shoot(url: str, out: Path, size: tuple[int, int]) -> None:
    profile = tempfile.mkdtemp(prefix="lh-shot-")
    argv = [
        chrome(), "--headless", "--no-sandbox", "--hide-scrollbars", f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}", f"--screenshot={out}",
    ]  # fmt: skip
    argv.append("--virtual-time-budget=2000")
    try:
        subprocess.run([*argv, url], check=True, capture_output=True, timeout=60)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


class LiveUi:
    """The web view open in a light and a dark tab from now on; `wait` photographs both once
    they have been open `settle_s`."""

    def __init__(self, url: str, *, settle_s: float) -> None:
        from playwright.sync_api import sync_playwright

        self.started = time.monotonic()
        self.settle_s = settle_s
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(executable_path=chrome())
        self.pages = {}
        for theme in ("light", "dark"):
            page = self.browser.new_page(
                viewport={"width": 1440, "height": 1100}, color_scheme=theme
            )
            page.goto(f"{url}&theme={theme}")
            self.pages[theme] = page

    def wait(self, out: Path) -> None:
        left = self.settle_s - (time.monotonic() - self.started)
        for theme, page in self.pages.items():
            page.wait_for_timeout(max(left, 0) * 1000 + 2500)  # and one more poll
            left = 0
            page.screenshot(path=str(out / f"ui-{theme}.png"))
        self.browser.close()
        self.playwright.stop()


def busiest(frames: list[dict]) -> dict:
    """The frame that shows the most at once: a queue, lending, an observed program, a full
    machine."""

    def score(frame: dict) -> tuple:
        status = frame["status"]
        leases = status.get("leases") or []
        kinds = {lease.get("kind") for lease in leases}
        return (
            bool(status.get("queue")),
            "observed" in kinds,
            "lend" in kinds,
            len(frame["events"]) >= 6,
            sum(lease.get("weight", 0) for lease in leases),
        )

    return max(frames[len(frames) // 3 :], key=score)  # once the sparklines have history


def render_frame(frame: dict, path: Path, scratch: Path) -> None:
    status = frame["status"]
    lines = top.render(
        status, frame["events"], WIDTH, HEIGHT, history=frame["history"], now=frame["now"]
    )
    page = scratch / f"{path.stem}.html"
    page.write_text(terminal_html(lines, list(status.get("pools") or {}), "leasehold top"))
    size = (round(WIDTH * CELL_W) + 32, HEIGHT * CELL_H + 24 + 28)
    shoot(page.as_uri(), path, size)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "media")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="lh-shots-"))
    demo = Demo(seed=args.seed)
    demo.start()
    ui = UiServer(lambda: Client(demo.socket))
    ui.start()
    try:
        browser = LiveUi(ui.url, settle_s=min(60.0, args.seconds / 2))
        watch = top.Watch(Client(demo.socket))
        frames: list[dict] = []
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            if watch.poll():
                frames.append(
                    {
                        "status": copy.deepcopy(watch.status),
                        "events": list(watch.events),
                        "history": {k: list(v) for k, v in watch.history.items()},
                        "now": watch.now(),
                    }
                )
                print(f"\r{len(frames)} frames", end="", file=sys.stderr, flush=True)
            time.sleep(2.0)
        print(file=sys.stderr)
        # The web view, live: open while the frames are taken, so its sparklines fill in.
        browser.wait(args.out)
    finally:
        ui.shutdown()
        ui.server_close()
        demo.stop()
    render_frame(busiest(frames), args.out / "top.png", scratch)
    gif = frames[-GIF_FRAMES:]
    for i, frame in enumerate(gif):
        render_frame(frame, scratch / f"f{i:03d}.png", scratch)
    if shutil.which("ffmpeg"):
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-framerate", "1",
                "-i", str(scratch / "f%03d.png"),
                "-vf", "split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=none",
                "-loop", "0", str(args.out / "top.gif"),
            ],
            check=True,
        )  # fmt: skip
    shutil.rmtree(scratch, ignore_errors=True)
    for made in sorted(args.out.iterdir()):
        print(f"{made}  {made.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
