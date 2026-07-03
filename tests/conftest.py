"""pytest scaffolding for mcp-screen.

The runtime modules (state.py, capture.py, input.py) import gi.repository at module top
and state.py also opens a D-Bus session bus at import time. Tests of the pure-math
helpers must NOT require a live GNOME session, so we install lightweight stubs into
sys.modules BEFORE any test module triggers the heavy imports.

Real-environment tests (e.g. test_recorder.py) don't need these stubs — recorder.py
touches neither gi nor state. Tests that DO want to exercise a particular fake (e.g.
input._clip_paste interactions) monkey-patch on top of these defaults inside the test."""

import sys
import types
from typing import Any, cast


def _install_gi_stub():
    if "gi" in sys.modules and "gi.repository" in sys.modules:
        return
    gi = cast(Any, types.ModuleType("gi"))
    gi.require_version = lambda *_a, **_kw: None
    repo = cast(Any, types.ModuleType("gi.repository"))

    class _GLib:
        @staticmethod
        def usleep(_):
            return None

        @staticmethod
        def Variant(*_a, **_kw):
            return None

    class _Gio:
        class DBusCallFlags:
            NONE = 0

        class BusType:
            SESSION = 0

        @staticmethod
        def bus_get_sync(*_a, **_kw):
            return types.SimpleNamespace(
                get_unique_name=lambda: ":1.0",
                call_sync=lambda *_a, **_kw: None,
            )

    repo.GLib = _GLib
    repo.Gio = _Gio
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo


def _install_state_stub():
    if "state" in sys.modules:
        return
    state = cast(Any, types.ModuleType("state"))
    state.SESSION = {}
    state.PORTAL = state.OBJ = state.RD = state.SC = ""
    state.bus = types.SimpleNamespace(call_sync=lambda *_a, **_kw: None)
    state.log = lambda *_a, **_kw: None
    sys.modules["state"] = state


def _install_capture_stub():
    """input.guard_user does a lazy `import capture`; stub it so importing input is enough."""
    if "capture" in sys.modules:
        return
    cap = cast(Any, types.ModuleType("capture"))
    cap.cursor_pos = lambda *_a, **_kw: None
    cap.cursor_sample_age = lambda *_a, **_kw: None
    sys.modules["capture"] = cap


_install_gi_stub()
_install_state_stub()
_install_capture_stub()
