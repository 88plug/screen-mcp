"""prereqs.py — prerequisite / capability matrix for screen_diag and setup.sh.

Each check returns {name, status, detail, next_step}. status is one of:
  ok   — ready
  warn — optional missing or degraded (fallbacks exist)
  fail — required for core capture/input on this stack
Never raises from check_all().
"""

import os
import shutil
import sys

_TOKEN_FILE = os.path.expanduser("~/.config/mcp-screen/token")


def _entry(name, status, detail, next_step=None):
    return {"name": name, "status": status, "detail": detail, "next_step": next_step}


def check_python_deps():
    missing = []
    for mod in ("numpy", "PIL"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if not missing:
        return _entry(
            "python_deps",
            "ok",
            f"numpy + Pillow (python {sys.version_info.major}.{sys.version_info.minor})",
            None,
        )
    return _entry(
        "python_deps",
        "fail",
        f"missing: {', '.join(missing)}",
        "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
    )


def check_pygobject():
    try:
        from gi.repository import Gio, GLib  # noqa: F401

        return _entry("pygobject", "ok", "PyGObject (Gio/GLib)", None)
    except Exception as e:
        return _entry(
            "pygobject",
            "fail",
            str(e),
            "Install python-gobject + gobject-introspection (see README system packages)",
        )


def check_gstreamer():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        major, minor, *_ = Gst.version()
        detail = f"GStreamer {major}.{minor}"
        if (major, minor) < (1, 28):
            return _entry(
                "gstreamer",
                "fail",
                f"{detail} (< 1.28; leaky-type unavailable)",
                "Upgrade to GStreamer >= 1.28",
            )
        return _entry("gstreamer", "ok", detail, None)
    except Exception as e:
        return _entry(
            "gstreamer",
            "fail",
            str(e),
            "Install gstreamer + gst-plugins-base/good/libav (see README)",
        )


def check_wayland():
    wl = os.environ.get("WAYLAND_DISPLAY")
    xdg = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if wl or xdg == "wayland":
        return _entry(
            "wayland",
            "ok",
            f"session type wayland (WAYLAND_DISPLAY={wl or 'set'})",
            None,
        )
    return _entry(
        "wayland",
        "fail",
        f"XDG_SESSION_TYPE={xdg or '(unset)'}; WAYLAND_DISPLAY unset",
        "screen-mcp targets GNOME on Wayland only",
    )


def check_portal_bus():
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Introspectable",
            "Introspect",
            None,
            GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return _entry("portal", "ok", "xdg-desktop-portal Desktop bus reachable", None)
    except Exception as e:
        return _entry(
            "portal",
            "fail",
            str(e),
            "Install and enable xdg-desktop-portal-gnome (PipeWire session)",
        )


def check_portal_token():
    if os.path.isfile(_TOKEN_FILE):
        return _entry("portal_token", "ok", f"restore token at {_TOKEN_FILE}", None)
    return _entry(
        "portal_token",
        "warn",
        "no cached restore token yet",
        "first screenshot will prompt for monitor screen-share consent (one-time)",
    )


def check_window_info():
    try:
        import awareness

        st = awareness.extension_state()
        if st["loaded"]:
            return _entry(
                "window_info", "ok", "window-info@local loaded on D-Bus", None
            )
        if st["installed"]:
            return _entry(
                "window_info",
                "warn",
                "extension installed but not loaded in running Shell",
                "log out and log back in (Wayland) to activate window-info",
            )
        return _entry(
            "window_info",
            "warn",
            "window-info@local not installed",
            "run gnome-shell-extension/window-info@local/install.sh (or ./setup.sh)",
        )
    except Exception as e:
        return _entry("window_info", "warn", f"check failed: {e}", None)


def check_uinput():
    try:
        import uinput_backend as ui

        d = ui.diag()
        if d.get("available"):
            return _entry("uinput", "ok", "kernel uinput backend available", None)
        if not d.get("evdev"):
            return _entry(
                "uinput",
                "warn",
                "python-evdev not installed",
                "pip install evdev for more reliable clicks on static monitors (optional)",
            )
        if not d.get("uinput_writable"):
            return _entry(
                "uinput",
                "warn",
                "/dev/uinput not writable",
                "add user to input group + udev rule, or use portal input fallback",
            )
        return _entry(
            "uinput", "warn", "uinput unavailable", "portal input path will be used"
        )
    except Exception as e:
        return _entry("uinput", "warn", str(e), None)


def check_wl_clipboard():
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        return _entry("wl_clipboard", "ok", "wl-copy / wl-paste on PATH", None)
    return _entry(
        "wl_clipboard",
        "warn",
        "wl-clipboard not found",
        "install wl-clipboard for Unicode paste in screen_type (ASCII keys still work)",
    )


def check_grounding():
    try:
        import grounding

        d = grounding.diag()
        parts = []
        if d.get("have_ocr"):
            parts.append("OCR")
        if d.get("have_omni"):
            parts.append("OmniParser")
        if not parts:
            return _entry(
                "grounding",
                "warn",
                "no OCR/ONNX backends importable",
                "pip install -r requirements.txt for annotate=true Set-of-Marks (optional)",
            )
        return _entry("grounding", "ok", "backends: " + ", ".join(parts), None)
    except Exception as e:
        return _entry("grounding", "warn", str(e), None)


_CHECKS = (
    check_python_deps,
    check_pygobject,
    check_gstreamer,
    check_wayland,
    check_portal_bus,
    check_portal_token,
    check_window_info,
    check_uinput,
    check_wl_clipboard,
    check_grounding,
)


def check_all():
    """Run every prerequisite check. Returns {prereqs: [...], summary: {ok, warn, fail}}."""
    items = []
    for fn in _CHECKS:
        try:
            items.append(fn())
        except Exception as e:
            items.append(
                _entry(
                    fn.__name__.removeprefix("check_"),
                    "warn",
                    f"check error: {e}",
                    None,
                )
            )
    summary = {
        k: sum(1 for i in items if i["status"] == k) for k in ("ok", "warn", "fail")
    }
    return {"prereqs": items, "summary": summary}


if __name__ == "__main__":
    import json

    print(json.dumps(check_all(), indent=2))
