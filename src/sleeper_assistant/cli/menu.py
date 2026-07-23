"""A tiny arrow-key selection menu — no extra dependency, just stdlib termios +
rich for the redraw.

The pure selection loop (`select`) takes an injectable `read_key` callable so it
can be unit-tested headless; the default reader (`read_key`) puts the terminal in
raw mode to capture single keypresses and arrow escape sequences. Callers that
aren't attached to a TTY should fall back to a plain text prompt instead — this
menu needs a real terminal to read keys.
"""

from __future__ import annotations

import sys
from typing import Callable, Sequence

from rich.console import Console
from rich.text import Text

# Normalized key tokens the selection loop reacts to. `read_key` maps raw
# terminal bytes onto these; tests feed them directly.
UP = "UP"
DOWN = "DOWN"
ENTER = "ENTER"
CANCEL = "CANCEL"


def read_key() -> str:
    """Read one keypress from stdin in raw mode, normalized to a token above.

    Returns the raw character for anything unrecognized (the loop ignores it).
    Unix-only (termios); callers guard with `sys.stdin.isatty()` first.
    """
    import select as select_mod
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "":                           # EOF (e.g. detached terminal) — bail out
            return CANCEL
        if ch == "\x1b":                       # ESC — maybe an arrow sequence "[A"/"[B"
            # A bare Esc keypress only ever sends this one byte, so blocking on
            # read(2) here would hang forever waiting for bytes that never come.
            # Poll with a short timeout: if more bytes follow within it, it's a
            # real arrow sequence; otherwise treat it as a standalone Esc.
            if select_mod.select([sys.stdin], [], [], 0.05)[0]:
                seq = sys.stdin.read(2)
                if seq == "[A":
                    return UP
                if seq == "[B":
                    return DOWN
            return CANCEL                      # bare ESC cancels
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    if ch in ("\r", "\n"):
        return ENTER
    if ch in ("\x03", "q"):                   # Ctrl-C or q
        return CANCEL
    if ch == "k":
        return UP
    if ch == "j":
        return DOWN
    return ch


def select(
    title: str,
    labels: Sequence[str],
    *,
    console: Console,
    read_key: Callable[[], str] = read_key,
) -> int:
    """Show `labels` under `title` and let the user pick one with the arrow keys.

    Returns the chosen 0-based index. Raises KeyboardInterrupt if cancelled
    (q / Esc / Ctrl-C). `read_key` is injectable for testing.
    """
    if not labels:
        raise ValueError("select() needs at least one option")

    idx = 0

    def frame() -> Text:
        t = Text()
        t.append(title, style="bold")
        t.append("\n")
        for i, label in enumerate(labels):
            if i == idx:
                t.append(f"  ❯ {label}\n", style="bold cyan")
            else:
                t.append(f"    {label}\n", style="dim")
        t.append("\n↑/↓ move · Enter select · q cancel", style="dim")
        return t

    # rich.Live gives us in-place redraw on a real terminal; on a non-terminal
    # console (tests) it simply no-ops the animation while update() still runs.
    from rich.live import Live

    with Live(frame(), console=console, auto_refresh=False, screen=False) as live:
        while True:
            key = read_key()
            if key == UP:
                idx = (idx - 1) % len(labels)
            elif key == DOWN:
                idx = (idx + 1) % len(labels)
            elif key == ENTER:
                return idx
            elif key == CANCEL:
                raise KeyboardInterrupt
            else:
                continue  # ignore unrecognized keys without a redraw
            live.update(frame(), refresh=True)
