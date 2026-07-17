"""state.py — shared session/portal plumbing for mcp-screen (v1.2).

Every other module imports this. It owns the xdg-desktop-portal RemoteDesktop +
ScreenCast session, the D-Bus bus, and the shared SESSION dict. No app-module imports
here (avoids circular deps)."""

import sys
import os
import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

TOKEN_FILE = os.path.expanduser("~/.config/mcp-screen/token")
PORTAL = "org.freedesktop.portal.Desktop"
OBJ = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"
MAX_EDGE = int(
    os.environ.get("MCP_SCREEN_MAX_EDGE", "2576")
)  # Opus 4.7 native long edge

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
sender = bus.get_unique_name()[1:].replace(".", "_")
_ctr = 0


def tok(p):
    global _ctr
    _ctr += 1
    return f"{p}{_ctr}"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def request(sig, iface, method, lead, options):
    """Portal Request/Response call; blocks until the Response signal; returns results."""
    ht = tok("h")
    options = dict(options)
    options["handle_token"] = GLib.Variant("s", ht)
    handle = f"{OBJ}/request/{sender}/{ht}"
    out = {}
    loop = GLib.MainLoop()

    def on_resp(c, s, o, i, sg, params):
        code, results = params.unpack()
        out["code"] = code
        out["results"] = results
        loop.quit()

    sid = bus.signal_subscribe(
        PORTAL,
        "org.freedesktop.portal.Request",
        "Response",
        handle,
        None,
        Gio.DBusSignalFlags.NONE,
        on_resp,
    )
    args = GLib.Variant(sig, tuple(lead) + (options,))
    bus.call_sync(
        PORTAL, OBJ, iface, method, args, None, Gio.DBusCallFlags.NONE, -1, None
    )
    GLib.timeout_add_seconds(
        120, lambda: (out.setdefault("code", 99), loop.quit()) and False
    )
    loop.run()
    bus.signal_unsubscribe(sid)
    return out


# Shared mutable state. geo = list of {node,x,y,w,h,sx,sy} (native px); view = last
# screenshot transform {ox,oy,scale,dw,dh} for view-space coordinate mapping.
# cursor = last-known real pointer in global native px {gx,gy,t}; cmd_cursor = the last
# position WE commanded (Gx,Gy) — the guard stops if the live cursor drifts off cmd_cursor.
SESSION = {
    "handle": None,
    "streams": None,
    "fd": None,
    "geo": None,
    "W": 0,
    "H": 0,
    "view": None,
    "view_seq": 0,
    "cursor": None,
    "cmd_cursor": None,
    "cmd_node": None,
    "last_input_t": None,
    "last_input_node": None,
    "last_input_hash": None,
}


def next_view_id():
    """Monotonic id stamped onto every screenshot's view transform. A view-space click can
    carry the view_id it was read from; resolve_xy rejects it if a LATER screenshot has since
    rebound the transform (the stale-view hazard: coords from screenshot A applied through
    screenshot B's origin/scale land in the wrong place — wrong monitor, even)."""
    SESSION["view_seq"] = int(SESSION.get("view_seq") or 0) + 1
    return SESSION["view_seq"]


def ensure_session():
    """Create the combined RemoteDesktop+ScreenCast session (idempotent). Restore token
    at TOKEN_FILE keeps it silent after the first one-time approval."""
    if SESSION["handle"]:
        return
    r = request(
        "(a{sv})",
        RD,
        "CreateSession",
        (),
        {"session_handle_token": GLib.Variant("s", tok("s"))},
    )
    if r["code"] != 0:
        raise RuntimeError(f"CreateSession failed: {r}")
    sess = r["results"]["session_handle"]
    restore = open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else ""
    devopts = {"types": GLib.Variant("u", 3), "persist_mode": GLib.Variant("u", 2)}
    if restore:
        devopts["restore_token"] = GLib.Variant("s", restore)
    r = request("(oa{sv})", RD, "SelectDevices", (sess,), devopts)
    if r["code"] != 0:
        raise RuntimeError(f"SelectDevices failed: {r}")
    request(
        "(oa{sv})",
        SC,
        "SelectSources",
        (sess,),
        {
            "types": GLib.Variant("u", 1),
            "multiple": GLib.Variant("b", True),
            # cursor_mode=4 (METADATA): cursor is NOT baked into frames; instead each
            # buffer carries SPA_META_Cursor (position) which capture.py reads to track
            # the real pointer (powers the user-takeover guard). We composite a marker
            # back into plain screenshots so the pointer stays visible.
            "cursor_mode": GLib.Variant("u", 4),
        },
    )
    r = request("(osa{sv})", RD, "Start", (sess, ""), {})
    if r["code"] != 0:
        raise RuntimeError(f"Start failed (dialog denied?): {r}")
    res = r["results"]
    if res.get("restore_token"):
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        open(TOKEN_FILE, "w").write(res["restore_token"])
        os.chmod(TOKEN_FILE, 0o600)
    SESSION["handle"] = sess
    SESSION["streams"] = res[
        "streams"
    ]  # list of (node_id:int, props:{position,size,...})
    reply_v, fds = bus.call_with_unix_fd_list_sync(
        PORTAL,
        OBJ,
        SC,
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (sess, {})),
        GLib.VariantType("(h)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        None,
    )
    SESSION["fd"] = fds.get(reply_v.unpack()[0])
    log("session ready:", sess, "streams:", len(SESSION["streams"]))


def open_pw_fd():
    """Open a fresh, independent PipeWire remote fd for this session. Each persistent
    pipewiresrc needs its OWN fd — sharing one starves concurrent streams."""
    reply_v, fds = bus.call_with_unix_fd_list_sync(
        PORTAL,
        OBJ,
        SC,
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (SESSION["handle"], {})),
        GLib.VariantType("(h)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        None,
    )
    return fds.get(reply_v.unpack()[0])
