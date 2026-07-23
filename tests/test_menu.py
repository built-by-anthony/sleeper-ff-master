"""Arrow-key selection menu — logic is unit-tested headless by injecting a
key-reader, so no real terminal/termios is needed."""

import io

import pytest
from rich.console import Console

from sleeper_assistant.cli.menu import CANCEL, DOWN, ENTER, UP, select


def _console() -> Console:
    # Non-terminal console: Live won't animate but update() still works.
    return Console(file=io.StringIO(), force_terminal=False)


def _keys(seq):
    it = iter(seq)
    return lambda: next(it)


def test_enter_on_first_selects_index_zero():
    idx = select("pick", ["A", "B", "C"], console=_console(), read_key=_keys([ENTER]))
    assert idx == 0


def test_down_then_enter_selects_next():
    idx = select("pick", ["A", "B", "C"], console=_console(), read_key=_keys([DOWN, ENTER]))
    assert idx == 1


def test_down_moves_and_up_returns():
    idx = select(
        "pick", ["A", "B", "C"], console=_console(),
        read_key=_keys([DOWN, DOWN, UP, ENTER]),
    )
    assert idx == 1


def test_up_from_top_wraps_to_bottom():
    idx = select("pick", ["A", "B", "C"], console=_console(), read_key=_keys([UP, ENTER]))
    assert idx == 2


def test_down_from_bottom_wraps_to_top():
    idx = select(
        "pick", ["A", "B", "C"], console=_console(),
        read_key=_keys([DOWN, DOWN, DOWN, ENTER]),
    )
    assert idx == 0


def test_cancel_raises_keyboardinterrupt():
    with pytest.raises(KeyboardInterrupt):
        select("pick", ["A", "B", "C"], console=_console(), read_key=_keys([CANCEL]))


def test_unknown_keys_are_ignored():
    idx = select(
        "pick", ["A", "B", "C"], console=_console(),
        read_key=_keys(["x", "\t", DOWN, ENTER]),
    )
    assert idx == 1


def test_empty_options_raises():
    with pytest.raises(ValueError):
        select("pick", [], console=_console(), read_key=_keys([ENTER]))
