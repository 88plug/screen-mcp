"""atspi_tree.py — optional AT-SPI element source.

OPT-IN, NEVER PRIMARY. Measured on a live GNOME session before writing this:

  apps exposed with toolkit-accessibility=false ......... 0
  apps exposed right after setting it true ............... 0   (running apps read it at start)
  apps exposed for an app LAUNCHED after enabling ........ 1   (full widget tree)

So AT-SPI requires a global gsettings toggle *and* restarting every app you want to drive.
screen-mcp exists to operate the desktop the user ALREADY has open — their logged-in
browser, their running chat app — and for those AT-SPI silently returns nothing. Making it
the primary perception layer would replace a working OCR path with one that reports an empty
screen and looks like a broken tool.

Where it IS a win: when a target app happens to expose accessibility, the tree gives exact
roles, labels and bounds for ~nothing, against a multi-second OCR + icon-detection pass. So
this module is a fast path that is tried first and falls through silently.

Everything degrades to []: no Atspi typelib, no a11y bus, accessibility off, app not
registered. Callers must treat an empty list as "no information", never as "empty screen".
"""

import os
import shutil
import subprocess

ENABLED = os.environ.get("MCP_SCREEN_ATSPI", "1") != "0"
MAX_NODES = int(os.environ.get("MCP_SCREEN_ATSPI_MAX_NODES", "1200"))
MAX_DEPTH = int(os.environ.get("MCP_SCREEN_ATSPI_MAX_DEPTH", "24"))

#: Roles worth returning as click targets. AT-SPI exposes a lot of structural noise
#: (fillers, panels, scroll panes) that is useless to an agent choosing where to click.
_ACTIONABLE = {
    "push button", "toggle button", "radio button", "check box", "menu item",
    "check menu item", "radio menu item", "link", "text", "entry", "password text",
    "combo box", "list item", "tab", "page tab", "slider", "spin button", "menu",
    "tree item", "table cell", "icon", "label", "heading", "button",
}  # fmt: skip

_ATSPI = None
_TRIED = False


def _a11y_bus_present():
    """Is there an accessibility bus to connect to?

    This gate exists because `Atspi.init()` does not raise when the bus is missing — the
    dbind layer prints "AT-SPI: Couldn't connect to accessibility bus" and calls abort(),
    taking the whole MCP server with it (verified: SIGABRT with XDG_RUNTIME_DIR unset). A
    Python try/except cannot catch abort(), so the only safe route is to not call init()
    unless the bus is really there. Gio's D-Bus calls DO raise properly, so this probe is
    safe on its own.
    """
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return True
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            _variant("(s)", ("org.a11y.Bus",)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(res.unpack()[0])
    except Exception:
        return False


def _variant(sig, vals):
    from gi.repository import GLib

    return GLib.Variant(sig, vals)


def _atspi():
    """Import + init Atspi once. Returns the module or None.

    Never called unless _a11y_bus_present() — see that docstring: a missing bus aborts the
    process rather than raising."""
    global _ATSPI, _TRIED
    if _TRIED:
        return _ATSPI
    _TRIED = True
    if not ENABLED or not _a11y_bus_present():
        return None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        Atspi.init()
        _ATSPI = Atspi
    except Exception:
        _ATSPI = None
    return _ATSPI


def toolkit_accessibility_enabled():
    """Read the GNOME toggle that gates whether apps register with AT-SPI at all.

    Shelled out on purpose. `Gio.Settings.new()` calls abort() — not a Python exception —
    when the dconf backend is unavailable (verified: SIGABRT under pytest with
    XDG_RUNTIME_DIR unset, killing the interpreter mid-test). No try/except can catch that,
    and it would take the whole MCP server down on a screen_diag from a headless or
    minimal-desktop host. A subprocess can only fail.

    Returns True/False, or None when it cannot be determined."""
    exe = shutil.which("gsettings")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    val = (r.stdout or "").strip().lower()
    if r.returncode != 0 or val not in ("true", "false"):
        return None
    return val == "true"


def _extents(node):
    """Screen-coordinate bounds, or None. AT-SPI can report 0x0 or negative for offscreen
    and not-yet-mapped widgets — those are not click targets, so drop them."""
    try:
        e = node.get_extents(1)  # 1 = ATSPI_COORD_TYPE_SCREEN
    except Exception:
        return None
    if e is None or e.width <= 0 or e.height <= 0 or e.x < 0 or e.y < 0:
        return None
    return int(e.x), int(e.y), int(e.width), int(e.height)


def elements(app_name=None):
    """Return [{role, text, x, y, bbox, source}] for on-screen accessible widgets.

    `x, y` is the CENTRE of the widget in desktop pixels — directly clickable, no OCR and no
    coordinate guessing. Empty list means "AT-SPI told us nothing", which is the common case
    (see the module docstring); it never means the screen is blank.
    """
    A = _atspi()
    if A is None:
        return []
    out = []
    try:
        desktop = A.get_desktop(0)
        napps = desktop.get_child_count()
    except Exception:
        return []

    for i in range(napps):
        if len(out) >= MAX_NODES:
            break
        try:
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            name = app.get_name() or ""
            if app_name and app_name.lower() not in name.lower():
                continue
        except Exception:
            continue
        _walk(app, out, 0, name)
    return out[:MAX_NODES]


def _walk(node, out, depth, app):
    """Iterative-ish DFS with hard depth and node caps. A pathological tree (a big web page
    under a browser's a11y bridge) can be enormous, and this runs on the tool's hot path."""
    if depth > MAX_DEPTH or len(out) >= MAX_NODES:
        return
    try:
        n = node.get_child_count()
    except Exception:
        return
    for i in range(n):
        if len(out) >= MAX_NODES:
            return
        try:
            child = node.get_child_at_index(i)
            if child is None:
                continue
            role = (child.get_role_name() or "").lower()
            label = (child.get_name() or "").strip()
        except Exception:
            continue
        if role in _ACTIONABLE and label:
            box = _extents(child)
            if box:
                x, y, w, h = box
                out.append(
                    {
                        "role": role,
                        "text": label,
                        "x": x + w // 2,
                        "y": y + h // 2,
                        "bbox": [x, y, x + w, y + h],
                        "app": app,
                        "source": "atspi",
                    }
                )
        _walk(child, out, depth + 1, app)


def diag():
    """Health for screen_diag — including WHY the tree is empty, which is the useful part."""
    A = _atspi()
    apps = 0
    if A is not None:
        try:
            apps = A.get_desktop(0).get_child_count()
        except Exception:
            apps = -1
    return {
        "enabled": ENABLED,
        "atspi_typelib": A is not None,
        "toolkit_accessibility": toolkit_accessibility_enabled(),
        "apps_exposed": apps,
        "note": (
            "0 apps is normal: GNOME's toolkit-accessibility must be ON *and* each app "
            "restarted after that to register. AT-SPI is an accelerator for apps that do "
            "expose it — OCR/OmniParser remains the path that always works."
        ),
    }
