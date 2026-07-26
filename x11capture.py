"""x11capture.py — X11 screen capture without the portal or PipeWire.

Why this exists: the portal ScreenCast path is not always available. Measured on a real
GNOME/**X11** box (Zorin OS 15.3): `xdg-desktop-portal` absent, 0 ScreenCast/RemoteDesktop
interfaces on the session bus, GStreamer 1.14 with no appsink `leaky-type`. None of that is
"X11" per se — but on such a host our normal pipeline cannot run at all, while plain X11
capture works fine. `/dev/uinput` was writable there too, so INPUT already works unchanged;
only capture needed a second path.

gi-free by design (same rule as budget.py / reliability.py) so CI can execute it: it imports
only stdlib + Pillow. No new dependencies — every grabber here is either Pillow, which is
already a hard dep, or a binary that was already present on the target host.

This is a FALLBACK, not a replacement. It has no damage-driven streaming, no cursor
metadata, and no per-monitor PipeWire nodes; it grabs whole-root pixels on demand.
"""

import os
import re
import shutil
import subprocess
import tempfile

from PIL import Image

#: Opt out entirely (e.g. to force the portal path for debugging).
DISABLED = os.environ.get("MCP_SCREEN_NO_X11", "") == "1"

_GRAB_TIMEOUT_S = float(os.environ.get("MCP_SCREEN_X11_TIMEOUT", "10"))


def display():
    """The X display to target, or None when there is no X11 at all."""
    return os.environ.get("DISPLAY") or None


_PROBE = None


def available(recheck=False):
    """True only when a grab ACTUALLY succeeds — cached after the first probe.

    "A grabber is on PATH" is not the same as "capture works". Under Xwayland the root
    window rejects XGetImage (BadMatch) and ImageMagick fails too, so a PATH-only check
    reported available=True while every grab returned None. Probe once, for real.
    """
    global _PROBE
    if DISABLED or not display():
        return False
    if recheck:
        _PROBE = None
    if _PROBE is None:
        _PROBE = grab_root() is not None
    return _PROBE


def _pillow_ok():
    """Pillow's ImageGrab needs XCB compiled in. Present on modern Pillow, absent on the
    ancient builds that ship with old LTS distros — which is exactly where we land."""
    try:
        from PIL import features

        return bool(features.check("xcb"))
    except Exception:
        return False


def _which_grabber():
    """First external grabber on PATH. Order is by cost: `import` writes one PNG, ffmpeg
    spins up a whole x11grab pipeline."""
    for exe in ("import", "maim", "scrot", "ffmpeg"):
        if shutil.which(exe):
            return exe
    return None


def geometry():
    """Return [{x, y, w, h}] per connected output, or a single root-sized entry.

    Parsed from `xrandr`; falls back to `xdpyinfo`, then to whatever a grab reports. Never
    raises — a wrong-but-plausible geometry is worse than none, so on doubt we return the
    single root rectangle rather than guessing at multi-monitor layout.
    """
    d = display()
    if not d:
        return []
    if shutil.which("xrandr"):
        try:
            out = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True,
                text=True,
                timeout=_GRAB_TIMEOUT_S,
                env={**os.environ, "DISPLAY": d},
            ).stdout
            mons = []
            for m in re.finditer(
                r"^(\S+) connected(?: primary)?\s+(\d+)x(\d+)\+(\d+)\+(\d+)",
                out,
                re.M,
            ):
                _, w, h, x, y = m.groups()
                mons.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
            if mons:
                return mons
        except Exception:
            pass
    size = root_size()
    return [{"x": 0, "y": 0, "w": size[0], "h": size[1]}] if size else []


def root_size():
    """(w, h) of the whole X root, or None."""
    d = display()
    if not d:
        return None
    if shutil.which("xdpyinfo"):
        try:
            out = subprocess.run(
                ["xdpyinfo"],
                capture_output=True,
                text=True,
                timeout=_GRAB_TIMEOUT_S,
                env={**os.environ, "DISPLAY": d},
            ).stdout
            m = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    img = grab_root()
    return img.size if img is not None else None


def grab_root():
    """Capture the whole X root as an RGB PIL image, or None.

    Tries Pillow's XCB grab first (in-process, no temp file), then an external grabber.
    Returns None rather than raising so callers can fall through to the portal path.
    """
    d = display()
    if DISABLED or not d:
        return None

    if _pillow_ok():
        try:
            from PIL import ImageGrab

            return ImageGrab.grab(xdisplay=d).convert("RGB")
        except Exception:
            pass  # Xwayland roots reject XGetImage with BadMatch; fall through.

    exe = _which_grabber()
    if not exe:
        return None
    env = {**os.environ, "DISPLAY": d}
    # NamedTemporaryFile(delete=False) + explicit unlink: the grabber writes the file, so it
    # must not be open on our side while that happens.
    fd, path = tempfile.mkstemp(prefix="screen-x11-", suffix=".png")
    os.close(fd)
    try:
        if exe == "import":
            cmd = ["import", "-window", "root", path]
        elif exe == "maim":
            cmd = ["maim", path]
        elif exe == "scrot":
            cmd = ["scrot", "-o", path]
        else:
            size = "%dx%d" % (root_size() or (1920, 1080))
            cmd = [
                "ffmpeg", "-loglevel", "quiet", "-f", "x11grab",
                "-video_size", size, "-i", d, "-frames:v", "1", "-y", path,
            ]  # fmt: skip
        r = subprocess.run(cmd, capture_output=True, timeout=_GRAB_TIMEOUT_S, env=env)
        if r.returncode != 0 or not os.path.getsize(path):
            return None
        with Image.open(path) as im:
            return im.convert("RGB")  # load before the file is unlinked
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def grab_region(x, y, w, h):
    """Crop a root grab. X11 has no cheap per-monitor stream, so a region costs a full
    root capture plus a crop — still far cheaper than not working at all."""
    img = grab_root()
    if img is None:
        return None
    W, H = img.size
    x, y = max(0, min(int(x), W - 1)), max(0, min(int(y), H - 1))
    w, h = max(1, min(int(w), W - x)), max(1, min(int(h), H - y))
    return img.crop((x, y, x + w, y + h))


def diag():
    """Backend health for screen_diag."""
    return {
        "display": display(),
        "disabled": DISABLED,
        "pillow_xcb": _pillow_ok(),
        "external_grabber": _which_grabber(),
        "available": available(),
        "monitors": len(geometry()),
    }
