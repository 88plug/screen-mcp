"""Unit tests for input.py pure helpers + the clipboard-restore patch.

Two patches landed:
  - gi.repository is now lazy (PEP 562 module __getattr__) so the coordinate math
    and keysym helpers below can be imported without a live PyGObject / portal.
  - _clip_paste restores in a `finally` so a paste-side failure can never leave
    the typed text (often a password/token) sitting in the user's clipboard.
The conftest stubs state and gi so this file imports input safely."""

import types

import pytest

import input as inp


@pytest.fixture(autouse=True)
def _reset_session():
    """Each test gets a fresh empty SESSION — coord-math reads state.SESSION directly."""
    inp.state.SESSION = {}
    yield
    inp.state.SESSION = {}


# ---------------------------------------------------------------------------
# Coordinate math
# ---------------------------------------------------------------------------
def test_resolve_xy_desktop_passthrough():
    assert inp.resolve_xy({"x": 100, "y": 50, "space": "desktop"}) == (100, 50)


def test_resolve_xy_view_applies_view_transform():
    inp.state.SESSION["view"] = {
        "ox": 10,
        "oy": 20,
        "scale": 0.5,
        "dw": 1000,
        "dh": 800,
    }
    # desktop = (ox + x/scale, oy + y/scale) = (10 + 100/0.5, 20 + 50/0.5) = (210, 120)
    assert inp.resolve_xy({"x": 100, "y": 50}) == (210, 120)


def test_resolve_xy_view_falls_back_to_passthrough_when_no_view():
    inp.state.SESSION["view"] = None
    assert inp.resolve_xy({"x": 100, "y": 50, "space": "view"}) == (100, 50)


def test_resolve_xy_norm_round_trips_to_midpoint():
    inp.state.SESSION["view"] = {"ox": 0, "oy": 0, "scale": 0.5, "dw": 400, "dh": 240}
    # norm 500/1000 of rendered (dw*scale, dh*scale) = (200, 120); /scale -> (200, 120).
    assert inp.resolve_xy({"x": 500, "y": 500, "space": "norm"}) == (200, 120)


def test_resolve_xy_matching_view_id_resolves_normally():
    inp.state.SESSION["view"] = {
        "ox": 10,
        "oy": 20,
        "scale": 0.5,
        "dw": 1000,
        "dh": 800,
        "id": 7,
    }
    # coords bound to the CURRENT view#7 resolve as usual.
    assert inp.resolve_xy({"x": 100, "y": 50, "view_id": 7}) == (210, 120)


def test_resolve_xy_stale_view_id_raises():
    # coords were read from view#7, but a later screenshot rebound the transform to view#9.
    inp.state.SESSION["view"] = {
        "ox": 999,
        "oy": 999,
        "scale": 1.0,
        "dw": 100,
        "dh": 100,
        "id": 9,
    }
    with pytest.raises(inp.StaleViewError):
        inp.resolve_xy({"x": 100, "y": 50, "view_id": 7})


def test_resolve_xy_no_view_id_skips_guard_for_backcompat():
    # without an explicit view_id the guard is inert — legacy callers keep working.
    inp.state.SESSION["view"] = {
        "ox": 0,
        "oy": 0,
        "scale": 1.0,
        "dw": 100,
        "dh": 100,
        "id": 42,
    }
    assert inp.resolve_xy({"x": 100, "y": 50}) == (100, 50)


def test_resolve_xy_stale_view_id_ignored_for_desktop_space():
    # desktop coords are transform-independent, so a stale view_id must not block them.
    inp.state.SESSION["view"] = {
        "ox": 0,
        "oy": 0,
        "scale": 1.0,
        "dw": 100,
        "dh": 100,
        "id": 9,
    }
    assert inp.resolve_xy({"x": 100, "y": 50, "space": "desktop", "view_id": 7}) == (
        100,
        50,
    )


def test_global_to_logical_picks_monitor_by_containment_and_scales():
    inp.state.SESSION["geo"] = [
        {"node": 1, "x": 0, "y": 0, "w": 1920, "h": 1080, "sx": 1.0, "sy": 1.0},
        {"node": 2, "x": 1920, "y": 0, "w": 2560, "h": 1440, "sx": 2.0, "sy": 2.0},
    ]
    node, lx, ly = inp.global_to_logical(1920 + 960, 540)
    assert node == 2 and lx == 480.0 and ly == 270.0


def test_global_to_logical_falls_back_to_first_monitor_when_outside_all():
    inp.state.SESSION["geo"] = [
        {"node": 7, "x": 0, "y": 0, "w": 1920, "h": 1080, "sx": 1.0, "sy": 1.0}
    ]
    node, _lx, _ly = inp.global_to_logical(-5, -5)
    assert node == 7


def test_global_to_logical_treats_zero_scale_as_one():
    inp.state.SESSION["geo"] = [
        {"node": 3, "x": 0, "y": 0, "w": 1000, "h": 1000, "sx": 0, "sy": 0}
    ]
    node, lx, ly = inp.global_to_logical(100, 200)
    assert node == 3 and lx == 100.0 and ly == 200.0  # `or 1.0` guards div-by-zero


# ---------------------------------------------------------------------------
# Keysym mapping
# ---------------------------------------------------------------------------
def test_keysym_special_names():
    assert inp._keysym("Enter") == 0xFF0D
    assert inp._keysym("ESC") == 0xFF1B
    assert inp._keysym("space") == 0x20


def test_keysym_modifier_names():
    assert inp._keysym("ctrl") == 0xFFE3
    assert inp._keysym("shift") == 0xFFE1


def test_keysym_function_keys():
    assert inp._keysym("F1") == 0xFFBE
    assert inp._keysym("f5") == 0xFFBE + 4


def test_keysym_single_char_uses_ord():
    assert inp._keysym("a") == ord("a")


def test_keysym_unknown_name_raises():
    with pytest.raises(ValueError):
        inp._keysym("zz")


# ---------------------------------------------------------------------------
# key() combo case-handling (regression: "Ctrl+A" must not become Ctrl+Shift+a)
# ---------------------------------------------------------------------------
def _capture_keycodes(monkeypatch):
    """Record every _notify_keycode call (keycode, press_state) — the PRIMARY keyboard path
    (raw evdev keycodes; reliable on Mutter). Fakes GLib so usleep is instant."""
    events = []
    monkeypatch.setattr(
        inp, "_notify_keycode", lambda kc, state: events.append((kc, state))
    )
    monkeypatch.setattr(inp, "GLib", types.SimpleNamespace(usleep=lambda _: None))
    return events


def _capture_keypresses(monkeypatch):
    """Record _notify_keysym calls — the FALLBACK path, used only for tokens we can't map to
    an evdev keycode."""
    events = []
    monkeypatch.setattr(
        inp, "_notify_keysym", lambda ks, state: events.append((ks, state))
    )
    monkeypatch.setattr(inp, "GLib", types.SimpleNamespace(usleep=lambda _: None))
    return events


def test_key_combo_ctrl_letter_uses_keycodes_no_implicit_shift(monkeypatch):
    """`key('Ctrl+A')` must press Ctrl (kc 29) + A (kc 30) as KEYCODES — no Shift. The old
    keysym path expanded capital-letter keysyms to key+Shift (Firefox saw Ctrl+Shift+a and
    opened about:addons); keycodes can't do that, so the bug is structurally gone."""
    events = _capture_keycodes(monkeypatch)
    inp.key({"keys": "Ctrl+A"})
    presses = [kc for kc, s in events if s == 1]
    assert presses == [29, 30], f"expected [ctrl=29, a=30], got {presses}"


def test_key_combo_ctrl_l_uses_keycodes(monkeypatch):
    """`key('Ctrl+L')` -> [ctrl=29, l=38] as keycodes (the live Ctrl+L bug)."""
    events = _capture_keycodes(monkeypatch)
    inp.key({"keys": "Ctrl+L"})
    presses = [kc for kc, s in events if s == 1]
    assert presses == [29, 38]


def test_key_explicit_shift_plus_letter_emits_shift_keycode(monkeypatch):
    """'shift+a' -> [shift=42, a=30] keycodes; the compositor combines them into 'A'."""
    events = _capture_keycodes(monkeypatch)
    inp.key({"keys": "Shift+a"})
    presses = [kc for kc, s in events if s == 1]
    assert presses == [42, 30]


def test_key_ctrl_k_quick_switcher_uses_keycodes(monkeypatch):
    """The exact shortcut that no-op'd on the keysym path: Ctrl+K must now emit keycodes
    [ctrl=29, k=37] through NotifyKeyboardKeycode."""
    events = _capture_keycodes(monkeypatch)
    inp.key({"keys": "ctrl+k"})
    presses = [kc for kc, s in events if s == 1]
    assert presses == [29, 37]


def test_key_combo_release_order_is_reverse_of_press(monkeypatch):
    """Modifiers released LAST: the keycode release sequence is the press sequence reversed."""
    events = _capture_keycodes(monkeypatch)
    inp.key({"keys": "ctrl+shift+t"})
    releases = [kc for kc, s in events if s == 0]
    presses = [kc for kc, s in events if s == 1]
    assert releases == list(reversed(presses))


def test_key_combo_uses_dash_or_plus_separator(monkeypatch):
    """'+' and '-' separators must produce identical keycode sequences."""
    events_plus = _capture_keycodes(monkeypatch)
    inp.key({"keys": "ctrl+l"})
    plus_seq = list(events_plus)
    events_dash = _capture_keycodes(monkeypatch)
    inp.key({"keys": "ctrl-l"})
    assert events_dash == plus_seq


def test_key_unmappable_token_falls_back_to_keysym(monkeypatch):
    """A token with no evdev keycode mapping but a valid keysym falls back to the keysym path,
    so nothing that used to work regresses. A non-ASCII single char (é) has no US-QWERTY
    keycode but maps to an X11 unicode keysym."""
    kc_events = _capture_keycodes(monkeypatch)
    ks_events = _capture_keypresses(monkeypatch)
    inp.key({"keys": "é"})
    assert kc_events == [], "should not use the keycode path when a token is unmappable"
    assert any(s == 1 for _ks, s in ks_events), "should fall back to the keysym path"


def test_char_keysym_ascii_passthrough():
    assert inp._char_keysym("A") == ord("A")


def test_char_keysym_unicode_uses_x11_offset():
    assert inp._char_keysym("é") == 0x01000000 + ord("é")


def test_char_keysym_newline_and_tab():
    assert inp._char_keysym("\n") == 0xFF0D
    assert inp._char_keysym("\t") == 0xFF09


# ---------------------------------------------------------------------------
# _clip_paste finally-restore contract (the patched leak)
# ---------------------------------------------------------------------------
class _Recorder:
    """Replacement for subprocess.run that records every call and returns scriptable results."""

    def __init__(self, paste_returncode=0, paste_stdout=b"ORIGINAL_USER_CLIPBOARD"):
        self.calls = []
        self._paste_returncode = paste_returncode
        self._paste_stdout = paste_stdout

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw.get("input")))
        if cmd[:1] == ["wl-paste"]:
            return types.SimpleNamespace(
                returncode=self._paste_returncode, stdout=self._paste_stdout
            )
        return types.SimpleNamespace(returncode=0, stdout=b"")


def _install_clip_paste_fakes(
    monkeypatch,
    *,
    paste_returncode=0,
    paste_stdout=b"ORIGINAL_USER_CLIPBOARD",
    key_side_effect=None,
):
    rec = _Recorder(paste_returncode=paste_returncode, paste_stdout=paste_stdout)
    monkeypatch.setattr(inp.subprocess, "run", rec)
    monkeypatch.setattr(inp.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(inp, "key", key_side_effect or (lambda args: None))
    return rec


def test_clip_paste_happy_path_restores_prior_clipboard(monkeypatch):
    rec = _install_clip_paste_fakes(monkeypatch)
    assert inp._clip_paste("hello") is True
    cmds = [c[0] for c in rec.calls]
    assert cmds[0][:2] == ["wl-paste", "--no-newline"]
    assert cmds[1] == ["wl-copy"] and rec.calls[1][1] == b"hello"
    assert cmds[-1] == ["wl-copy"] and rec.calls[-1][1] == b"ORIGINAL_USER_CLIPBOARD"


def test_clip_paste_restores_even_when_paste_raises(monkeypatch):
    """Regression: a portal-side Ctrl+V failure must not leave `text` in the clipboard."""

    def boom(args):
        raise RuntimeError("portal blew up on ctrl+v")

    rec = _install_clip_paste_fakes(monkeypatch, key_side_effect=boom)
    assert inp._clip_paste("SECRET_TOKEN") is False
    assert rec.calls[-1][0] == ["wl-copy"]
    assert rec.calls[-1][1] == b"ORIGINAL_USER_CLIPBOARD", (
        "clipboard MUST be restored even when Ctrl+V raises"
    )


def test_clip_paste_clears_clipboard_when_prev_unreadable(monkeypatch):
    """If we couldn't save the user's clipboard, we must still scrub ours — leaving
    the typed text behind on paste failure is the patched leak."""

    def boom(args):
        raise RuntimeError("portal blew up")

    rec = _install_clip_paste_fakes(
        monkeypatch, paste_returncode=1, key_side_effect=boom
    )
    inp._clip_paste("ANOTHER_SECRET")
    assert rec.calls[-1][0] == ["wl-copy", "--clear"], (
        "must --clear when prev clipboard was unreadable"
    )


def test_clip_paste_returns_false_when_wl_copy_missing(monkeypatch):
    monkeypatch.setattr(inp.shutil, "which", lambda name: None)
    assert inp._clip_paste("anything") is False


def test_clip_paste_returns_false_when_initial_copy_fails_without_attempting_paste(
    monkeypatch,
):
    """wl-copy --check fails -> we never wrote the clipboard, so there's nothing to undo
    and Ctrl+V must NOT fire."""

    def bad_run(cmd, **kw):
        if cmd == ["wl-copy"] and kw.get("input"):
            raise RuntimeError("wl-copy died")
        return types.SimpleNamespace(returncode=0, stdout=b"")

    key_calls = []
    monkeypatch.setattr(inp.subprocess, "run", bad_run)
    monkeypatch.setattr(inp.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(inp, "key", lambda args: key_calls.append(args))
    assert inp._clip_paste("x") is False
    assert key_calls == [], "must not press Ctrl+V if we never wrote the clipboard"
