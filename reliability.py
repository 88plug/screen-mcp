"""reliability.py — reliability layer for mcp-screen (v1.2).

Kills the two silent failure modes that make desktop automation flaky:
  1. Stale screenshots — acting on a frame that is mid-animation / not yet settled.
  2. Silent misclicks — a click/type that lands on nothing and produces no visible change.

Strategy: before every mutating action, poll the live frame until it stops changing
(`wait_for_stable_frame`); hash the pre-frame; run the action; wait for the frame to
settle again; diff the region around the click (`region_diff`). If a click/type made NO
visible change, RE-OBSERVE once with a fresh capture (settle + grab + diff again) WITHOUT
re-running the handler — the action is issued at most once — then prepend a misclick
WARNing if it still looks like nothing changed. Every action is appended to a JSONL audit
log. An opt-in guard (`needs_ack`) can require an explicit ack token before destructive
keystrokes.

Imports `state` only (state.SESSION{W,H,view}, state.log). The frame grabber is GIVEN to
us at call time as `grab(node) -> (w, h, RGBA ndarray)` — we never import capture, so this
module stays trivially unit-testable on synthetic numpy arrays. All numpy ops are kept
cheap (strided downsample, min-length flatten, single boolean reduction)."""

import os
import re
import json
import time
import hashlib

import numpy as np

import state


# ---------------------------------------------------------------------------
# Tunables (env-overridable starting points, not hard limits)
# ---------------------------------------------------------------------------
LOG_PATH = os.path.expanduser("~/.local/state/mcp-screen/actions.jsonl")

# Keystroke combos that close/kill a window — always worth an ack when the guard is on.
# `cmd+q` is here so the comment at the normalisation site below isn't a lie: aliases
# control+/command+ are mapped to ctrl+/cmd+ FIRST, then matched. (Bug: the prior set
# omitted cmd+q, so command+q slipped through ungated.)
_CLOSE_COMBOS = {"alt+f4", "ctrl+w", "ctrl+q", "cmd+q"}

# Destructive intent words; matched case-insensitively against OCR text near the target.
_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|close|quit|submit|pay|purchase|confirm|send|discard|format)\b",
    re.IGNORECASE,
)

# Tools where a no-visible-change result is expected and must NOT raise a misclick warning.
_NO_NOOP_WARN = {
    "screen_screenshot",
    "screen_list_monitors",
    "screen_move_mouse",
    "screen_reload",
}


# ---------------------------------------------------------------------------
# Frame primitives (pure numpy — cheap, synthetic-array friendly)
# ---------------------------------------------------------------------------
def frame_hash(arr, ds=16):
    """Perceptual-ish fingerprint of a frame: downsample RGB to ~ds×ds, blake2b hex.

    Strided slicing (not interpolation) keeps this O(ds^2) regardless of frame size.
    Alpha is dropped so an opaque-vs-transparent flag never perturbs the hash. Returns an
    8-byte (16 hex char) digest — collision-cheap but tiny for the audit log."""
    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[2] >= 3:
        a = a[..., :3]
    h = a.shape[0] if a.ndim >= 1 else 0
    w = a.shape[1] if a.ndim >= 2 else 0
    if h and w:
        step_y = max(1, h // ds)
        step_x = max(1, w // ds)
        small = a[::step_y, ::step_x]
    else:
        small = a
    small = np.ascontiguousarray(small, dtype=np.uint8)
    return hashlib.blake2b(small.tobytes(), digest_size=8).hexdigest()


def mean_abs_diff(a, b):
    """Mean absolute per-channel difference over RGB, 0..255.

    Flattens both frames' RGB to 1-D and compares over the shared min length, so frames
    of slightly different size (a resize mid-transition) still yield a sane scalar instead
    of throwing. Returns 0.0 when there is nothing to compare."""
    a = np.asarray(a)
    b = np.asarray(b)
    if a.ndim == 3 and a.shape[2] >= 3:
        a = a[..., :3]
    if b.ndim == 3 and b.shape[2] >= 3:
        b = b[..., :3]
    fa = a.reshape(-1)
    fb = b.reshape(-1)
    n = min(fa.shape[0], fb.shape[0])
    if n == 0:
        return 0.0
    # int16 subtraction avoids uint8 wraparound; mean over the shared prefix.
    diff = np.abs(fa[:n].astype(np.int16) - fb[:n].astype(np.int16))
    return float(diff.mean())


def wait_for_changed_frame(grab_fn, node, baseline_hash, interval=0.06, timeout=2.5):
    """Poll grab_fn(node)[2] until its frame_hash DIFFERS from baseline_hash, or timeout.

    This is the anti-stale primitive for a damage-driven (static) monitor: after an action,
    `wait_for_stable_frame` is satisfied instantly by a REPEATED stale frame ("stable" != "new"),
    and a keepalive resend carries old pixels with a fresh timestamp — so neither stability nor
    PTS proves the capture reflects the action. A changed pixel-hash does. Returns
    (changed: bool, last_hash). On timeout returns (False, last_hash) so the caller still
    proceeds with whatever frame it has."""
    deadline = time.monotonic() + timeout
    last = baseline_hash
    while True:
        try:
            last = frame_hash(grab_fn(node)[2])
        except Exception as ex:  # noqa: BLE001 — never crash the capture path
            state.log("wait_for_changed_frame: grab failed:", ex)
            return False, last
        if last != baseline_hash:
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(interval)


def wait_for_stable_frame(
    grab_fn, node, interval=0.10, window=2, timeout=2.5, thresh=0.5
):
    """Poll grab_fn(node)[2] until the frame stops changing, capping animation via timeout.

    "Stable" = `window` consecutive frame-to-frame mean_abs_diff values all below `thresh`.
    Returns (stable: bool, last_diff: float). On a frame that never settles (e.g. a spinner)
    we bail at `timeout` with stable=False and the most recent diff, so the caller still
    proceeds rather than hanging forever."""
    deadline = time.monotonic() + timeout
    try:
        prev = grab_fn(node)[2]
    except Exception as e:  # noqa: BLE001 — a grab failure shouldn't crash the action path
        state.log("wait_for_stable_frame: initial grab failed:", e)
        return False, float("inf")

    stable_run = 0
    last_diff = float("inf")
    while True:
        if interval > 0:
            time.sleep(interval)
        try:
            cur = grab_fn(node)[2]
        except Exception as e:  # noqa: BLE001
            state.log("wait_for_stable_frame: grab failed:", e)
            return False, last_diff
        last_diff = mean_abs_diff(prev, cur)
        prev = cur
        if last_diff < thresh:
            stable_run += 1
            if stable_run >= window:
                return True, last_diff
        else:
            stable_run = 0
        if time.monotonic() >= deadline:
            return False, last_diff


def region_diff(before, after, bbox, thresh=2.0):
    """Detect + localise change inside `bbox` between two frames.

    bbox = [x, y, w, h] in frame pixels (clipped to the frame). A pixel "changed" if its
    max per-channel abs diff exceeds `thresh`. Returns (changed: bool, changed_bbox | None)
    where changed_bbox is the TIGHT [x, y, w, h] (frame coords) enclosing changed pixels."""
    before = np.asarray(before)
    after = np.asarray(after)
    if before.ndim != 3 or after.ndim != 3:
        return False, None

    fh = min(before.shape[0], after.shape[0])
    fw = min(before.shape[1], after.shape[1])
    if fh == 0 or fw == 0:
        return False, None

    x, y, w, h = (int(round(v)) for v in bbox)
    x0 = max(0, min(x, fw))
    y0 = max(0, min(y, fh))
    x1 = max(x0, min(x + w, fw))
    y1 = max(y0, min(y + h, fh))
    if x1 <= x0 or y1 <= y0:
        return False, None

    b = before[y0:y1, x0:x1, :3].astype(np.int16)
    a = after[y0:y1, x0:x1, :3].astype(np.int16)
    # Max-channel abs diff per pixel, then threshold -> boolean change mask.
    mask = np.abs(a - b).max(axis=2) > thresh
    if not mask.any():
        return False, None

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    ry0, ry1 = np.where(rows)[0][[0, -1]]
    cx0, cx1 = np.where(cols)[0][[0, -1]]
    changed_bbox = [
        int(x0 + cx0),
        int(y0 + ry0),
        int(cx1 - cx0 + 1),
        int(ry1 - ry0 + 1),
    ]
    return True, changed_bbox


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def log_action(rec):
    """Append one action record as a JSON line to ~/.local/state/mcp-screen/actions.jsonl.

    Schema keys: ts, tool, args, resolved_coords, pre_hash, post_hash, changed,
    changed_bbox, ms, warn. Best-effort: a logging failure must never break the action."""
    schema = (
        "ts",
        "tool",
        "args",
        "resolved_coords",
        "pre_hash",
        "post_hash",
        "changed",
        "changed_bbox",
        "ms",
        "warn",
    )
    out = {k: rec.get(k) for k in schema}
    # `setdefault` would no-op here because the dict-comp above pre-populates "ts"
    # to None when the caller didn't pass one — so the key exists but is empty.
    # Replace any None/missing ts with now() so every audit line is timestamped.
    if not out.get("ts"):
        out["ts"] = time.time()
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(out, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        state.log("log_action: write failed:", e)


# ---------------------------------------------------------------------------
# Opt-in destructive-action guard
# ---------------------------------------------------------------------------
def needs_ack(tool, args, ocr_near_target):
    """Return a guard reason token if this action should require an explicit `ack`, else None.

    Off by default — only active when env MCP_SCREEN_GUARD=1. Reasons (first match wins):
      'window-close'   — screen_key combo in {alt+f4, ctrl+w, ctrl+q}
      'keyword:<word>' — a destructive verb appears in OCR text near the target
      'out-of-allowlist' — MCP_SCREEN_APPS is set and the focused app isn't in it
    The caller compares the returned token against args['ack'] to decide whether to block."""
    if os.environ.get("MCP_SCREEN_GUARD") != "1":
        return None

    # Allowlist gate: if configured, the focused app must be on it.
    allow = os.environ.get("MCP_SCREEN_APPS")
    if allow:
        apps = {a.strip() for a in allow.split(",") if a.strip()}
        if (args.get("_focused_app") or None) not in apps:
            return "out-of-allowlist"

    if tool == "screen_key":
        combo = str(args.get("keys", "")).lower().replace("-", "+").replace(" ", "")
        # Normalise modifier aliases so 'control+w' / 'cmd+q' map onto the close set.
        combo = combo.replace("control+", "ctrl+").replace("command+", "cmd+")
        if combo in _CLOSE_COMBOS:
            return "window-close"

    if ocr_near_target:
        m = _DESTRUCTIVE.search(ocr_near_target)
        if m:
            return f"keyword:{m.group(1).lower()}"

    return None


# ---------------------------------------------------------------------------
# Integration wrapper
# ---------------------------------------------------------------------------
def _is_error(result):
    return isinstance(result, dict) and result.get("isError")


def _prepend_text(result, text):
    """Prepend a text content block to an MCP result dict (defensive about shape)."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        content = [] if content is None else [content]
    result["content"] = [{"type": "text", "text": text}] + content
    return result


def _err(text, reason=None):
    r = {"content": [{"type": "text", "text": text}], "isError": True}
    if reason is not None:
        r["ack_reason"] = reason
    return r


def wrap_call(handler, name, args, ctx):
    """Reliability wrapper the server puts around each tool handler.

    `ctx` adapts the live server to us, exposing:
      ctx.grab(node) -> (w, h, arr)         live frame grab
      ctx.resolve_xy(args) -> (x, y)        tool coords -> desktop px (None if no coords)
      ctx.node_for(x, y) -> node            monitor node under a desktop point
      ctx.primary_node                      fallback node when there are no coords
      ctx.W, ctx.H                          desktop bounds (native px)
      ctx.local_bbox(x, y, size) -> [x,y,w,h]   click-local bbox in that node's frame px

    Flow: bounds-check -> settle pre-frame -> ack gate -> pre_hash -> handler (run
    exactly ONCE) -> settle post-frame -> region_diff around the click -> on a no-op for
    click/type, RE-OBSERVE once with a fresh capture (never re-run the handler) -> log.
    Defensive: if ctx can't provide live capture/coords, we still run the handler
    (degrading to a plain pass-through with logging)."""
    t0 = time.time()
    args = dict(args or {})

    # --- Resolve coordinates (may be absent for keyboard / screenshot tools) -----------
    coords = None
    try:
        coords = ctx.resolve_xy(args)
    except Exception:  # noqa: BLE001 — no coords for this tool, or ctx can't resolve
        coords = None

    # --- Bounds check: a click outside the desktop is a hard error ---------------------
    if coords is not None:
        x, y = coords
        W = getattr(ctx, "W", 0) or 0
        H = getattr(ctx, "H", 0) or 0
        if W and H and not (0 <= x < W and 0 <= y < H):
            res = _err(f"ERROR: coords ({x},{y}) outside desktop bounds {W}x{H}")
            log_action(
                {
                    "tool": name,
                    "args": args,
                    "resolved_coords": [x, y],
                    "warn": "out-of-bounds",
                    "ms": int((time.time() - t0) * 1000),
                }
            )
            return res

    # --- Pick the node we'll watch for change ------------------------------------------
    node = None
    try:
        if coords is not None:
            node = ctx.node_for(*coords)
        else:
            node = getattr(ctx, "primary_node", None)
    except Exception:  # noqa: BLE001
        node = getattr(ctx, "primary_node", None)

    can_capture = node is not None and hasattr(ctx, "grab")

    # --- Ack gate (opt-in; needs OCR text the ctx may optionally supply) ----------------
    ocr_near = args.get("_ocr_near_target")
    reason = needs_ack(name, args, ocr_near)
    if reason and args.get("ack") != reason:
        res = _err(
            f"BLOCKED: this action needs confirmation ({reason}). "
            f"Re-issue with ack='{reason}' to proceed.",
            reason=reason,
        )
        log_action(
            {
                "tool": name,
                "args": args,
                "resolved_coords": list(coords) if coords else None,
                "warn": f"blocked:{reason}",
                "ms": int((time.time() - t0) * 1000),
            }
        )
        return res

    # --- Degraded path: no live capture -> just run + log ------------------------------
    if not can_capture:
        result = handler(args)
        log_action(
            {
                "tool": name,
                "args": args,
                "resolved_coords": list(coords) if coords else None,
                "ms": int((time.time() - t0) * 1000),
                "warn": "no-capture",
            }
        )
        return result

    # --- Settle, then capture the pre-frame --------------------------------------------
    wait_for_stable_frame(ctx.grab, node)
    try:
        before = ctx.grab(node)[2]
    except Exception as e:  # noqa: BLE001
        state.log("wrap_call: pre-grab failed:", e)
        before = None
    pre_hash = frame_hash(before) if before is not None else None

    # Click-local diff box (frame px) around the action point, if we have coords.
    bbox = None
    if coords is not None and hasattr(ctx, "local_bbox"):
        try:
            bbox = ctx.local_bbox(coords[0], coords[1], 80)
        except Exception:  # noqa: BLE001
            bbox = None

    def _observe():
        """Settle, capture after-frame, diff the click region. Does NOT run the handler."""
        wait_for_stable_frame(ctx.grab, node)
        try:
            after = ctx.grab(node)[2]
        except Exception as e:  # noqa: BLE001
            state.log("wrap_call: post-grab failed:", e)
            after = None
        ch, cbox = False, None
        if before is not None and after is not None:
            if bbox is not None:
                ch, cbox = region_diff(before, after, bbox)
            else:
                # No coords (keyboard tool): compare whole-frame via hash equality.
                ch = frame_hash(after) != pre_hash
        return after, ch, cbox

    result = handler(args)  # issue the action ONCE
    after, changed, changed_bbox = _observe()
    warn = None

    # --- No-op detection + single fresh RE-OBSERVE for click/type (never re-issue) ------
    expects_change = name not in _NO_NOOP_WARN
    if expects_change and before is not None and after is not None and not changed:
        # No re-issue: the effect may have landed outside the sampled region or after settle.
        after, changed, changed_bbox = _observe()
        if not changed:
            warn = "no-op"
            result = _prepend_text(result, "WARN: no screen change — likely misclick")

    post_hash = frame_hash(after) if after is not None else None
    log_action(
        {
            "tool": name,
            "args": args,
            "resolved_coords": list(coords) if coords else None,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
            "changed": bool(changed),
            "changed_bbox": changed_bbox,
            "ms": int((time.time() - t0) * 1000),
            "warn": warn,
        }
    )
    return result
