"""capture.py — PipeWire frame capture + desktop compositing for mcp-screen (v1.2).

Owns one PERSISTENT pipewiresrc->appsink pipeline per monitor node, kept in PLAYING
so each grab just pulls the latest negotiated frame (no per-call pipeline rebuild).
The single OpenPipeWireRemote fd from state.SESSION backs every pipewiresrc; each
pipewiresrc dup()s the fd internally so sharing is safe. BGRx is used on the wire for
speed, then swapped to RGBA in numpy. GStreamer 1.28: `drop=` is gone — we use
`leaky-type=downstream` + max-buffers=1 to keep only the freshest frame.

DAMAGE-DRIVEN STREAMING: GNOME/Mutter negotiates framerate 0/1 (send-on-damage), so a
monitor that is powered ON but STATIC (no cursor, no animation) produces NO buffer until
something on it changes. pipewiresrc's keepalive-time only re-sends an ALREADY-EXISTING
last buffer, and resend-last only fires on EOS — neither can bootstrap the FIRST frame of a
never-damaged source. There is no portal API to force a frame, so the only lever is to
generate a damage event; ensure_geo() nudges the pointer onto a static-but-ON monitor to
prime it (see _nudge_prime).

Imports state (session/portal plumbing). Does NOT open a portal session itself beyond
calling state.ensure_session() at runtime via ensure_geo()."""
import io
import os
import json
import time
import base64
import ctypes
import threading

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp, GLib  # noqa: F401  (GstApp registers appsink type)
import numpy as np
from PIL import Image, ImageDraw

import state

LANCZOS = Image.Resampling.LANCZOS

Gst.init(None)

# node_id -> {"pipe": Gst.Pipeline, "sink": appsink}. Guarded by _LOCK.
_PIPES = {}
_LOCK = threading.Lock()
# node_id -> Lock: serializes concurrent BUILDS of the same node without holding _LOCK during
# the slow build, so different nodes build in parallel. Guarded by _LOCK on first insert.
_BUILDING = {}


def _build_pipe(node_id):
    """Create + start a persistent pipewiresrc->appsink pipeline for one node.

    Blocks until caps are negotiated (PLAYING reached, ~5s negotiate) and warms it with a
    pull (~3s) so last_sample is primed. Returns (pipe, sink, fd)."""
    fd = state.open_pw_fd()   # OWN fd per pipeline; sharing one starves concurrent streams
    # keepalive-time only re-sends an EXISTING last buffer and resend-last only fires on EOS,
    # so neither primes the FIRST frame of a never-damaged static source — that needs a damage
    # event (ensure_geo's _nudge_prime). They still help once a buffer has flowed.
    pipe = Gst.parse_launch(
        f"pipewiresrc name=pwsrc fd={fd} path={node_id} keepalive-time=1000 resend-last=true "
        f"! videoconvert ! video/x-raw,format=BGRx "
        f"! appsink name=s sync=false max-buffers=1 leaky-type=downstream "
        f"emit-signals=false enable-last-sample=true")
    sink = pipe.get_by_name("s")
    src = pipe.get_by_name("pwsrc")   # read the cursor meta HERE; videoconvert strips it downstream
    if src is not None:
        pad = src.get_static_pad("src")
        if pad is not None:
            pad.add_probe(Gst.PadProbeType.BUFFER, _cursor_probe, node_id)
    pipe.set_state(Gst.State.PLAYING)
    # Block until the pipeline finishes negotiating (or 5s elapse).
    pipe.get_state(5 * Gst.SECOND)
    # Warm: prime last_sample. An IDLE/static monitor can be slow to emit its first
    # frame, so block up to ~3s (3 x 1s), breaking as soon as a sample lands.
    for _ in range(3):   # active monitors deliver in <200ms; idle/DPMS ones never do — fail fast
        if sink.emit("try-pull-sample", 1 * Gst.SECOND) is not None:
            break
    return pipe, sink, fd


def _get_sink(node_id):
    """Return the live appsink for node_id, building/caching the pipeline if needed.

    The ~8s _build_pipe runs OUTSIDE _LOCK so concurrent grabs of DIFFERENT nodes build in
    parallel (was: the lock serialized every cold build, so N monitors cost N*8s). A per-node
    build lock prevents two threads building the SAME node; if we lose that race the loser
    tears down its now-redundant pipeline (each carries its own fd — must not leak)."""
    with _LOCK:
        ent = _PIPES.get(node_id)
        if ent is not None:
            return ent["sink"]
        blk = _BUILDING.setdefault(node_id, threading.Lock())
    with blk:                                    # serialize builds of THIS node only
        with _LOCK:
            ent = _PIPES.get(node_id)
            if ent is not None:
                return ent["sink"]
        pipe, sink, fd = _build_pipe(node_id)    # slow, lock-free
        with _LOCK:
            ent = _PIPES.get(node_id)
            if ent is not None:                  # someone built it while we were building
                redundant = (pipe, fd); winning = ent["sink"]
            else:
                _PIPES[node_id] = {"pipe": pipe, "sink": sink, "fd": fd}
                redundant = None; winning = sink
    if redundant:                                # captured under _LOCK above; a concurrent
        try:                                     # shutdown() can't KeyError us here
            redundant[0].set_state(Gst.State.NULL); os.close(redundant[1])
        except Exception:
            pass
    return winning


def _sample_to_rgba(sample):
    """Decode a GstSample (BGRx) -> (w, h, ndarray HxWx4 uint8 RGBA)."""
    st = sample.get_caps().get_structure(0)
    w, h = st.get_value("width"), st.get_value("height")
    buf = sample.get_buffer()
    ok, mi = buf.map(Gst.MapFlags.READ)
    try:
        arr = np.frombuffer(bytes(mi.data), dtype=np.uint8)
    finally:
        buf.unmap(mi)
    # Account for row padding via stride, then trim to exactly w*4 bytes/row.
    stride = len(arr) // h
    arr = arr[:stride * h].reshape((h, stride))[:, :w * 4].reshape((h, w, 4))
    # BGRx -> RGBA channel swap (x byte becomes opaque alpha below).
    arr = arr[..., [2, 1, 0, 3]]
    arr = np.ascontiguousarray(arr)
    arr[..., 3] = 255  # BGRx has no real alpha; force opaque.
    return w, h, arr


def _drop(node_id):
    """Tear down + uncache a node's pipeline (so the next grab rebuilds it)."""
    with _LOCK:
        ent = _PIPES.pop(node_id, None)
    if ent:
        try:
            ent["pipe"].set_state(Gst.State.NULL)
        except Exception:
            pass
        try:
            os.close(ent["fd"])
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Cursor position (SPA_META_Cursor, via cursor_mode=METADATA)
#
# pipewiresrc attaches the cursor position to each buffer as a
# GstVideoRegionOfInterestMeta labelled "cursor" (roi_type == quark("cursor")).
# PyGObject can't downcast that meta to read its x/y, and the by-id accessor is a
# broken binding, so we reach the fields with ctypes: pull the GstBuffer* out of the
# PyGObject wrapper, iterate its metas, and read the x/y guints at their struct offsets.
# Offsets are x86-64 (GstMeta = 16 bytes; roi_type@16, x@28, y@32). Best-effort: any
# failure returns None and the guard fails open.
# ---------------------------------------------------------------------------
_GST = ctypes.CDLL("libgstreamer-1.0.so.0")
_GST.gst_buffer_iterate_meta.restype = ctypes.c_void_p
_GST.gst_buffer_iterate_meta.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_CURSOR_QUARK = GLib.quark_from_string("cursor")


class _PyWrap(ctypes.Structure):  # PyGObject MiniObject wrapper head: refcnt, type, C ptr
    _fields_ = [("rc", ctypes.c_ssize_t), ("ty", ctypes.c_void_p), ("obj", ctypes.c_void_p)]


# node_id -> (frame_x, frame_y, monotonic_t), fed by the src-pad probe below. The cursor
# ROI meta lives on the pipewiresrc src buffer but videoconvert drops it, so we read it at
# the source rather than off the appsink sample.
_CURSOR = {}


def _cursor_xy_from_buf(buf):
    """Read the 'cursor' ROI meta (x,y in stream frame px) off a GstBuffer, or None."""
    try:
        bp = _PyWrap.from_address(id(buf)).obj
        st = ctypes.c_void_p(0)
        while True:
            mp = _GST.gst_buffer_iterate_meta(bp, ctypes.byref(st))
            if not mp:
                return None
            if ctypes.c_uint32.from_address(mp + 16).value == _CURSOR_QUARK:
                return (ctypes.c_uint32.from_address(mp + 28).value,
                        ctypes.c_uint32.from_address(mp + 32).value)
    except Exception:
        return None


def _cursor_probe(pad, info, node_id):
    """pipewiresrc src-pad buffer probe: stash the latest cursor position for this node."""
    try:
        xy = _cursor_xy_from_buf(info.get_buffer())
        if xy is not None:
            # Reject out-of-frame cursor metas: a stale/garbage ROI value (e.g. x beyond the
            # stream width, seen when a monitor goes static) must not poison the cache and
            # resolve to a bogus global position later. Caps live on the pad, not the buffer.
            fw = fh = None
            cap = pad.get_current_caps()
            if cap is not None:
                st = cap.get_structure(0)
                fw, fh = st.get_value("width"), st.get_value("height")
            if (not fw or 0 <= xy[0] < fw) and (not fh or 0 <= xy[1] < fh):
                with _LOCK:
                    _CURSOR[node_id] = (xy[0], xy[1], time.monotonic())
    except Exception:
        pass
    return Gst.PadProbeReturn.OK


def cursor_pos(refresh=True, prefer_node=None):
    """Best-effort real pointer position in GLOBAL native px (gx, gy), or None.

    Reads the per-node cache the src-pad probes keep current. Normally the cursor meta rides
    only on the stream the pointer is over, so the node with the freshest sample wins. But a
    STATIC monitor never refreshes its sample, so "freshest across all monitors" would resolve
    to whatever LIVE monitor last produced a frame — the wrong monitor. When the caller just
    moved the pointer to a known node, pass prefer_node=<node> to pin the readback to THAT
    monitor's sample (regardless of freshness), falling back to freshest only if it has none.
    Updates the state.SESSION['cursor'] cache."""
    geo = {m["node"]: m for m in (state.SESSION.get("geo") or [])}
    with _LOCK:
        items = dict(_CURSOR)
    if prefer_node is not None and prefer_node in items and prefer_node in geo:
        x, y, t = items[prefer_node]; m = geo[prefer_node]
        best, bt = (m["x"] + x, m["y"] + y), t
    else:
        best, bt = None, -1.0
        for nid, (x, y, t) in items.items():
            m = geo.get(nid)
            if m and t > bt:
                bt, best = t, (m["x"] + x, m["y"] + y)
    if best is not None:
        state.SESSION["cursor"] = {"gx": best[0], "gy": best[1], "t": bt}
        return best
    c = state.SESSION.get("cursor")
    return (c["gx"], c["gy"]) if c else None


def diag():
    """Capture-subsystem health for screen_diag: live pipelines + the probe-fed cursor cache
    (per-node frame px + monotonic age) and the resolved global cursor_pos."""
    with _LOCK:
        cache = {nid: {"frame_xy": [x, y], "age_s": round(time.monotonic() - t, 2)}
                 for nid, (x, y, t) in _CURSOR.items()}
        pipes = list(_PIPES.keys())
    return {"live_pipelines": pipes, "cursor_quark": _CURSOR_QUARK,
            "probe_cache": cache, "cursor_pos": cursor_pos()}


def draw_cursor(img, ox, oy):
    """Composite a simple pointer marker into a screenshot crop at the live cursor position
    (METADATA cursor mode doesn't bake the cursor into frames). No-op if the cursor is
    unknown or outside this crop. `img` is at real desktop px; (ox,oy) is its origin."""
    c = state.SESSION.get("cursor")
    if not c:
        return img
    lx, ly = c["gx"] - ox, c["gy"] - oy
    if not (0 <= lx < img.width and 0 <= ly < img.height):
        return img
    try:
        d = ImageDraw.Draw(img)
        a = [(lx, ly), (lx, ly + 22), (lx + 6, ly + 16), (lx + 11, ly + 24),
             (lx + 15, ly + 22), (lx + 10, ly + 14), (lx + 17, ly + 14)]
        d.polygon(a, fill=(255, 255, 255), outline=(0, 0, 0))
    except Exception:
        pass
    return img


def grab(node_id, rebuild=True):
    """Pull the latest frame for one monitor node -> (w, h, ndarray HxWx4 uint8 RGBA).

    rebuild=False skips the stale-pipeline teardown+rebuild path so an idle monitor
    fails fast (one build) instead of paying a second full pipeline build."""
    sink = _get_sink(node_id)
    sample = sink.emit("try-pull-sample", 2 * Gst.SECOND) or sink.props.last_sample
    if sample is None and rebuild:
        # Stale/half-dead pipeline (e.g. idle monitor never primed): rebuild once.
        _drop(node_id)
        sink = _get_sink(node_id)
        sample = sink.emit("try-pull-sample", 5 * Gst.SECOND) or sink.props.last_sample
    if sample is None:
        raise RuntimeError(
            f"no frame from node {node_id} (monitor may be off, or ON-but-static — GNOME "
            f"streams on damage, so an idle screen emits no frame until something changes)")
    w, h, arr = _sample_to_rgba(sample)
    _note_frame(node_id, arr)   # track freshness so screenshots can flag a possibly-stale frame
    return w, h, arr


def _peek(node_id):
    """Cheap, non-blocking native (w,h) from a live pipeline's cached last_sample, or None.

    Never builds, drops, or blocks — used to detect a woken idle monitor before compositing."""
    ent = _PIPES.get(node_id)
    if ent is None:
        return None
    sample = ent["sink"].props.last_sample
    if sample is None:
        return None
    st = sample.get_caps().get_structure(0)
    return st.get_value("width"), st.get_value("height")


def grab_all(node_ids, rebuild=True):
    """Grab several monitor nodes concurrently -> {node_id: (w, h, arr)}.

    rebuild=False makes a no-frame node fail fast instead of paying the ~15s teardown+rebuild;
    the composite path uses this because ensure_geo() has already primed every live monitor, so
    a cached last_sample returns instantly and a still-dark node is simply skipped (left black)."""
    out = {}
    errs = {}

    def work(nid):
        try:
            out[nid] = grab(nid, rebuild=rebuild)
        except Exception as e:  # noqa: BLE001 — record per-node, surface after join
            errs[nid] = e

    threads = [threading.Thread(target=work, args=(nid,)) for nid in node_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errs and not out:
        raise RuntimeError(f"grab_all: all nodes failed: {errs}")
    for nid, e in errs.items():
        state.log("WARN grab_all node", nid, "failed:", e)
    return out


def _power_verdicts(probed):
    """Map each probed monitor to 'on'/'off'/'unknown' using awareness.monitor_power().

    probed rows are [node, lpx, lpy, lw, lh, ...]; awareness reports active logical monitors
    in LOGICAL desktop coords. We match by logical position+size. A monitor we DON'T find in
    the active layout is 'off' (DPMS/disabled); if the probe returned nothing at all we can't
    tell, so 'unknown'. Best-effort: never raises."""
    try:
        import awareness
        active = awareness.monitor_power()
    except Exception:
        active = []
    if not active:
        return ["unknown"] * len(probed)
    out = []
    for row in probed:
        lpx, lpy = row[1], row[2]
        # Match on logical ORIGIN only. Origin uniquely identifies a monitor and both sides
        # report it identically; size is derived via different rounding paths (portal props vs
        # native/scale) and can differ by >2px under fractional scaling — matching on size too
        # would mis-label a powered-on scaled monitor as 'off' and skip the prime nudge.
        found = any(abs(a["x"] - lpx) <= 2 and abs(a["y"] - lpy) <= 2 for a in active)
        out.append("on" if found else "off")
    return out


def _nudge_prime(node_id, lpx, lpy, lw, lh, sx, sy):
    """Generate one damage event on a static-but-ON monitor to prime its first frame, then
    retry the grab once. Returns (w, h, arr) on success, or None.

    Mechanism: GNOME streams on damage, so we move the pointer to the monitor's center (a tiny
    wiggle = damage), wait briefly for the now-damaged frame, then RESTORE the pointer so the
    user isn't disrupted. Skipped if the user appears to be driving the pointer (takeover
    guard) or via MCP_SCREEN_NO_NUDGE=1. Lazy `import input` keeps capture's import graph
    acyclic (input imports state + lazily imports capture, not the reverse at module load)."""
    if os.environ.get("MCP_SCREEN_NO_NUDGE") == "1":
        return None
    try:
        import input as inp  # lazy: input lazily imports capture; importing it at top would cycle
        try:
            inp.guard_user()        # respect user takeover — don't fight for the pointer
        except Exception:
            return None
        prior = state.SESSION.get("cmd_cursor") or cursor_pos()  # restore target (real pos if unmoved this session)
        # Damage target in GLOBAL native px (the space _goto expects). Prefer wiggling IN PLACE at
        # the current pointer when it's already ON this monitor — a ±3px nudge is enough damage and
        # near-invisible, vs jumping to monitor center (disruptive, and this now runs per fresh
        # screenshot). Only fall back to center when the pointer is elsewhere / unknown.
        x0, y0 = round(lpx * sx), round(lpy * sy)
        mw, mh = round(lw * sx), round(lh * sy)
        if prior is not None and x0 <= prior[0] < x0 + mw and y0 <= prior[1] < y0 + mh:
            gx, gy = int(prior[0]), int(prior[1])
        else:
            gx, gy = int(x0 + mw / 2), int(y0 + mh / 2)
        inp._goto(gx, gy)
        if GLib is not None:
            GLib.usleep(20000)
        wx = gx + 3 if gx + 3 < x0 + mw else gx - 3   # tiny wiggle: ensure a fresh damage region
        wy = gy + 3 if gy + 3 < y0 + mh else gy - 3
        inp._goto(wx, wy)
        sink = _get_sink(node_id)
        sample = None
        for _ in range(3):          # up to ~3s for the damaged frame to arrive
            sample = sink.emit("try-pull-sample", 1 * Gst.SECOND)
            if sample is not None:
                break
        return _sample_to_rgba(sample) if sample is not None else None
    except Exception:
        return None
    finally:
        try:                        # restore the pointer wherever the user/we last had it
            if prior is not None:
                inp._goto(int(prior[0]), int(prior[1]))
        except Exception:
            pass


def _grab_or_prime(m):
    """grab() one monitor (geo entry m); if it has no frame because the monitor is static,
    prime it once via a damage nudge and retry. Returns (w, h, arr). Raises if still no frame
    (e.g. the monitor is genuinely off). This is why a single-monitor / region capture of a
    STATIC monitor no longer fails where the full composite would have re-primed it."""
    try:
        return grab(m["node"], rebuild=False)
    except Exception:
        primed = _nudge_prime(m["node"], m["lpx"], m["lpy"], m["lw"], m["lh"],
                              m.get("sx", 1.0) or 1.0, m.get("sy", 1.0) or 1.0)
        if primed is not None:
            return primed
        return grab(m["node"])   # last resort: full rebuild path (raises with the asleep msg)


# Per-node signature of the last frame we returned — lets us detect a keepalive RESEND (the
# byte-identical stale buffer pipewiresrc re-pushes for a damage-driven idle monitor) so a
# fresh grab can force a current frame. Cheap: a sparse byte sample, not a full hash.
_LAST_SIG = {}

# Per-node FRESHNESS tracking (separate from _LAST_SIG, which force_fresh_grab owns): when did
# this node's frame CONTENT last change? On a damage-driven static monitor the screencast stops
# sending frames, so a grab returns the last (possibly STALE) buffer — and if the real screen
# changed since, we'd silently report old pixels as current. We can't tell "old-but-accurate"
# from "old-and-wrong" without a fresh frame, so we surface the frame's AGE and let screenshots
# flag the risk (see _stale_note) instead of asserting staleness either way.
_CHG_SIG = {}          # node -> last frame signature (for change detection)
_LAST_CHANGE_T = {}    # node -> monotonic time the signature last changed
STALE_AGE_S = float(os.environ.get("MCP_SCREEN_STALE_AGE_S", "1.5"))   # older => flag stale-risk


def _note_frame(node_id, arr):
    """Record when this node's frame content last changed (freshness). Cheap; never raises."""
    try:
        sig = _sig(arr)
    except Exception:
        return
    if sig != _CHG_SIG.get(node_id):
        _CHG_SIG[node_id] = sig
        _LAST_CHANGE_T[node_id] = time.monotonic()
    elif node_id not in _LAST_CHANGE_T:
        _LAST_CHANGE_T[node_id] = time.monotonic()


def frame_age(node_id):
    """Seconds since this node's frame content last changed, or None if never seen."""
    t = _LAST_CHANGE_T.get(node_id)
    return None if t is None else max(0.0, time.monotonic() - t)


def _stale_note(ox, oy, dw, dh):
    """Warning string if any monitor overlapping the captured rect [ox,oy,dw,dh] is STATIC with an
    old frame — its pixels may not reflect the current screen (GNOME streams only on change, and a
    grab can return a stale buffer). Empty when all covered monitors are fresh. This is the guard
    against silently reporting a stale frame as live."""
    geo = state.SESSION.get("geo") or []
    stale = []
    for i, m in enumerate(geo):
        if ox + dw <= m["x"] or ox >= m["x"] + m["w"] or oy + dh <= m["y"] or oy >= m["y"] + m["h"]:
            continue
        age = frame_age(m["node"])
        if age is not None and age >= STALE_AGE_S:
            stale.append(f"monitor {i} ~{int(age)}s")
    if not stale:
        return ""
    return (f"  ⚠ STALE-RISK ({', '.join(stale)} since last change): this monitor is STATIC, so the "
            f"frame may NOT reflect the live screen if it changed without the screencast catching it. "
            f"Do NOT assert this is current — pass fresh=true, or click/scroll that monitor to confirm.")


def _sig(arr):
    """Cheap content signature (sparse pixel sample) for staleness detection, or None."""
    try:
        return hash(arr[::64, ::64, :3].tobytes())
    except Exception:
        return None


def force_fresh_grab(m):
    """Grab one monitor, forcing a CURRENT frame on a damage-driven STATIC monitor.

    A plain grab() of an idle GNOME output returns the keepalive-RESENT last buffer, so if the
    screen changed since it last emitted a frame (a click switched views, a message arrived) the
    capture is STALE — the bug behind 'I clicked but the screenshot shows the old state'. Here we
    pull once; if the monitor looks static (geo live:false, or the pull is byte-identical to our
    previous pull of this node) we generate a damage event (_nudge_prime, which restores the
    pointer) and use the post-damage frame, which reflects NOW. Falls back to whatever we have if
    priming can't run (user driving the pointer, MCP_SCREEN_NO_NUDGE). Updates the signature cache."""
    nid = m["node"]
    w, h, arr = _grab_or_prime(m)
    sig = _sig(arr)
    if (not m.get("live")) or (sig is not None and sig == _LAST_SIG.get(nid)):
        primed = _nudge_prime(nid, m["lpx"], m["lpy"], m["lw"], m["lh"],
                              m.get("sx", 1.0) or 1.0, m.get("sy", 1.0) or 1.0)
        if primed is not None:
            w, h, arr = primed
            sig = _sig(arr)
            _note_frame(nid, arr)   # the damage-primed frame is current — reset its freshness age
    _LAST_SIG[nid] = sig
    return w, h, arr


def _prime_static(mons):
    """Damage-prime each STATIC monitor in mons so the next grab_all() returns a CURRENT frame
    (the primed frame stays in the appsink's last_sample). Used by the fresh composite path."""
    for m in mons:
        if not m.get("live"):
            try:
                _nudge_prime(m["node"], m["lpx"], m["lpy"], m["lw"], m["lh"],
                             m.get("sx", 1.0) or 1.0, m.get("sy", 1.0) or 1.0)
            except Exception:
                pass


def ensure_geo(force=False):
    """Compute (and cache) per-monitor native-pixel geometry. Runtime only.

    For each stream: read logical position/size from props, grab once for native
    frame size, derive scale, and place the monitor on the global native canvas.
    Stores state.SESSION['geo'] and the canvas bounds W/H."""
    state.ensure_session()
    if state.SESSION["geo"] and not force:
        return state.SESSION["geo"]
    # Probe all streams CONCURRENTLY: each cold pipeline build blocks ~8s, and they no longer
    # share a lock during the build (see _get_sink), so N monitors cost ~8s total, not N*8s.
    streams = list(state.SESSION["streams"])
    probe_res = {}

    def _probe(idx, node_id):
        try:
            fw, fh, _arr = grab(node_id, rebuild=False)
            probe_res[idx] = (fw, fh, True)
        except Exception as e:  # noqa: BLE001  (off, or ON-but-static: no first frame yet)
            state.log("WARN no frame from", node_id, ":", e, "(no frame; registering by metadata)")
            probe_res[idx] = (None, None, False)

    threads = [threading.Thread(target=_probe, args=(i, nid))
               for i, (nid, _props) in enumerate(streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    probed = []
    for i, (node_id, props) in enumerate(streams):
        lpx, lpy = props.get("position", (0, 0))
        lw, lh = props.get("size", (0, 0))
        fw, fh, live = probe_res[i]
        probed.append([node_id, lpx, lpy, lw, lh, fw, fh, live])
    # Monitors usually share a scale; borrow it for any monitor that couldn't be grabbed.
    dsx = next((p[5] / p[3] for p in probed if p[7] and p[3]), 1.0)
    dsy = next((p[6] / p[4] for p in probed if p[7] and p[4]), 1.0)
    power = _power_verdicts(probed)

    def _publish():
        geo = []
        for (node_id, lpx, lpy, lw, lh, fw, fh, live), pw in zip(probed, power):
            if live:
                sx = fw / lw if lw else 1.0; sy = fh / lh if lh else 1.0
            else:
                sx, sy = dsx, dsy; fw, fh = round(lw * sx), round(lh * sy)
            geo.append({
                "node": node_id, "x": round(lpx * sx), "y": round(lpy * sy),
                "w": fw, "h": fh, "sx": sx, "sy": sy,
                "lpx": lpx, "lpy": lpy, "lw": lw, "lh": lh, "live": live, "power": pw,
            })
        if not geo:
            raise RuntimeError("ensure_geo: no streams")
        state.SESSION["geo"] = geo
        state.SESSION["W"] = max(m["x"] + m["w"] for m in geo)
        state.SESSION["H"] = max(m["y"] + m["h"] for m in geo)
        return geo

    # Publish a PRELIMINARY geo first: the nudge below moves the pointer via input._goto, which
    # resolves coords through global_to_logical(state.SESSION['geo']). Without this, on a fresh
    # session geo is None and the nudge throws (fails open) — i.e. it never actually fired.
    geo = _publish()
    # A no-frame monitor that is powered ON (or power 'unknown') is likely ON-but-static, not
    # asleep: GNOME streams on damage. Nudge once to generate a damage event and prime a frame.
    nudged = False
    for row, pw in zip(probed, power):
        if row[7] or pw == "off":
            continue
        sx0 = dsx if row[3] else 1.0; sy0 = dsy if row[4] else 1.0
        primed = _nudge_prime(row[0], row[1], row[2], row[3], row[4], sx0, sy0)
        if primed is not None:
            row[5], row[6], row[7] = primed[0], primed[1], True
            nudged = True
    # Republish with the real frame size of any primed monitor.
    return _publish() if nudged else geo


def monitors_for(region):
    """geo entries intersecting region [x,y,w,h] (global native px); all if None."""
    geo = ensure_geo()
    if not region:
        return geo
    rx, ry, rw, rh = region
    hit = [m for m in geo if not (rx + rw <= m["x"] or rx >= m["x"] + m["w"]
                                  or ry + rh <= m["y"] or ry >= m["y"] + m["h"])]
    return hit or geo


def _full_canvas(mons):
    """Composite the given monitors onto the full native canvas -> PIL RGBA."""
    canvas = np.zeros((state.SESSION["H"], state.SESSION["W"], 4), dtype=np.uint8)
    canvas[..., 3] = 255
    # rebuild=False: ensure_geo already primed live monitors, so this returns cached frames
    # instantly instead of paying the ~15s rebuild penalty per static monitor (the old
    # full-desktop capture spent 10-30s almost entirely here).
    frames = grab_all([m["node"] for m in mons], rebuild=False)
    geo_by_node = {m["node"]: m for m in mons}
    for nid, (w, h, arr) in frames.items():
        m = geo_by_node[nid]
        ch = min(h, canvas.shape[0] - m["y"])
        cw = min(w, canvas.shape[1] - m["x"])
        if ch > 0 and cw > 0:
            canvas[m["y"]:m["y"] + ch, m["x"]:m["x"] + cw] = arr[:ch, :cw]
    return Image.fromarray(canvas, "RGBA")


def asleep_hint(monitor=None):
    """If the relevant monitor(s) have no frame (live:false), return an agent/human-friendly
    explanation distinguishing genuinely-OFF (DPMS) from ON-but-static; else None.

    A no-frame monitor is NOT necessarily asleep: GNOME/Mutter streams on damage, so a
    powered-ON but idle screen yields no frame until something on it changes (we already tried
    nudging it once). We use the per-monitor power verdict to word the message correctly.
    `monitor=N` scopes the check to that one; otherwise reports any no-frame monitor."""
    geo = state.SESSION.get("geo") or []
    idle = [i for i, m in enumerate(geo) if not m.get("live")]
    if not idle:
        return None
    if monitor is not None and int(monitor) not in idle:
        return None
    sel = [int(monitor)] if monitor is not None else idle
    which = f"Monitor {int(monitor)}" if monitor is not None else f"Monitor(s) {idle}"
    powers = {geo[i].get("power", "unknown") for i in sel if i < len(geo)}
    if powers == {"off"}:
        return (f"{which} is ASLEEP (DPMS/power-save): a blanked Wayland output emits no frames, "
                f"so it can't be captured (expected, not a fault). Ask the user to wake that "
                f"monitor (move the mouse onto it or press a key) and bring the target app to the "
                f"foreground, then retry. Any awake monitor is still capturable.")
    if powers == {"on"}:
        return (f"{which} is ON but STATIC — GNOME/Mutter only streams frames on change, so an "
                f"idle screen yields no capture frame until something on it changes. mcp-screen "
                f"will nudge it to refresh; if it persists, click/scroll on that monitor.")
    return (f"{which} produced no frame: it is either ASLEEP (DPMS — wake it: move the mouse onto "
            f"it or press a key) or ON but STATIC (GNOME streams on damage, so an idle screen "
            f"yields no frame until something changes — click/scroll on it). mcp-screen already "
            f"tried nudging it. Any awake monitor is still capturable.")


def capture_desktop(region=None, monitor=None, fresh=False):
    """Return (PIL RGBA at real desktop pixels, origin_x, origin_y).

    monitor=index -> that monitor's frame (origin = its x,y).
    region within a single monitor -> fast single grab + crop.
    region spanning monitors / no region -> full composite then crop.

    fresh=True forces a CURRENT frame for the captured monitor(s) on a damage-driven static
    monitor (defeats the keepalive-resend stale-capture); see force_fresh_grab. Costs a pointer
    nudge per static monitor, so callers pass it when freshness matters (a normal screenshot),
    not for bulk/locate composites."""
    geo = ensure_geo()
    # Single-monitor/region grabber. fresh=True nudges for a current frame. Otherwise use a
    # NON-DESTRUCTIVE grab (rebuild=False): it returns the new/last-retained frame and RAISES on a
    # cold monitor WITHOUT dropping+rebuilding the pipeline — the destructive rebuild (_grab_or_prime
    # → grab(rebuild=True)) cooled a static monitor's warm pipeline (clearing last_sample), which is
    # why repeated region reads of an idle monitor kept failing while the composite still worked.
    # On a raise, the caller falls back to cropping the composite (also non-destructive, tolerates
    # a no-frame monitor) instead of erroring.
    _grab1 = force_fresh_grab if fresh else (lambda m: grab(m["node"], rebuild=False))
    # A monitor registered idle (live:false) carries synthesized w/h from a borrowed scale.
    # If it has since woken, its cached last_sample now reports a real (and possibly different)
    # size; re-probe once so its true scale and the canvas bounds are recomputed before blit.
    for m in geo:
        if not m.get("live"):
            wh = _peek(m["node"])
            if wh and (wh[0] != m["w"] or wh[1] != m["h"]):
                geo = ensure_geo(force=True)
                break
    if isinstance(region, str):     # tolerate a stringified "[x,y,w,h]" (e.g. an undeclared arg)
        try:
            region = json.loads(region)
        except Exception:
            region = None
    if monitor is not None:
        m = geo[int(monitor)]
        try:
            w, h, arr = _grab1(m)
            return Image.fromarray(arr, "RGBA"), m["x"], m["y"]
        except Exception:
            # Static-monitor fallback: the single-monitor grab can fail (and its rebuild path goes
            # COLD) where the composite — which never rebuilds and tolerates a no-frame monitor —
            # still returns that node's last frame. Crop the native composite instead of erroring.
            return _full_canvas(geo).crop((m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])), m["x"], m["y"]
    if region:
        rx, ry, rw, rh = [int(v) for v in region]
        mons = monitors_for([rx, ry, rw, rh])
        if len(mons) == 1:
            m = mons[0]
            try:
                w, h, arr = _grab1(m)
                img = Image.fromarray(arr, "RGBA")
                cx, cy = rx - m["x"], ry - m["y"]
                return (img.crop((max(0, cx), max(0, cy), cx + rw, cy + rh)),
                        max(rx, m["x"]), max(ry, m["y"]))
            except Exception:
                pass   # fall through to the composite crop (robust for a static monitor)
        if fresh:
            _prime_static(mons)
        return _full_canvas(mons).crop((rx, ry, rx + rw, ry + rh)), rx, ry
    if fresh:
        _prime_static(geo)
    return _full_canvas(geo), 0, 0


def encode_store(img, ox, oy, label, t0, max_edge=None):
    """Downscale to <=max_edge (default state.MAX_EDGE) long edge, remember the view->desktop
    transform, encode WebP. Pass a smaller max_edge for tour thumbnails (fewer tokens). For
    lossy thumbnails (max_edge < MAX_EDGE) we use WebP q=80 to shrink payload further.

    Returns MCP content list: a text summary + the WebP image."""
    dw, dh = img.size
    le = max(dw, dh)
    me = int(max_edge) if max_edge else state.MAX_EDGE
    scale = min(1.0, me / le) if le else 1.0
    out = img if scale >= 1.0 else img.resize(
        (max(1, round(dw * scale)), max(1, round(dh * scale))), LANCZOS)
    vid = state.next_view_id()
    state.SESSION["view"] = {"ox": ox, "oy": oy, "scale": scale, "dw": dw, "dh": dh, "id": vid}
    buf = io.BytesIO()
    if max_edge and me < state.MAX_EDGE:
        out.convert("RGB").save(buf, format="WEBP", quality=80, method=4)
    else:
        out.convert("RGBA").save(buf, format="WEBP", lossless=True)
    raw = buf.getvalue()
    ms = int((time.time() - t0) * 1000)
    txt = (f"{label}: view#{vid} {out.width}x{out.height} (scale {scale:.4f}) covering desktop "
           f"origin=({ox},{oy}) size={dw}x{dh}. {len(raw) // 1024}KB, {ms}ms. "
           f"Click with space='view' using coords as seen here (pass view_id={vid} to bind "
           f"the click to THIS screenshot).")
    txt += _stale_note(ox, oy, dw, dh)   # flag a possibly-stale static monitor — never report old pixels as live
    return [{"type": "text", "text": txt},
            {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/webp"}]


def shutdown():
    """Tear down all persistent pipelines and clear the stream cache."""
    with _LOCK:
        for ent in _PIPES.values():
            try:
                ent["pipe"].set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.close(ent["fd"])
            except Exception:  # noqa: BLE001
                pass
        _PIPES.clear()
        _LAST_SIG.clear()
        _CHG_SIG.clear()
        _LAST_CHANGE_T.clear()
