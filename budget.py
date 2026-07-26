"""budget.py — fit an encoded screenshot to a client's tool-output cap.

Pure image/arithmetic logic: imports only stdlib + Pillow, NEVER gi/GStreamer/state. That
is deliberate and load-bearing, the same reason `reliability.py` takes an injected grabber:
capture.py cannot be imported without a real gi + GStreamer + D-Bus session, so anything
living there is invisible to CI (conftest substitutes a stub `capture`). This logic had no
CI coverage at all while it sat in capture.py. Keep the downscaler INJECTED so this module
stays importable anywhere.

Why any of this exists: some MCP clients hard-cap tool-result size. Grok Build truncates at
20KB, which cuts a base64 image mid-string; the client then rejects the whole image
("integrity check failed: image bytes are truncated") and the model sees NOTHING — strictly
worse than a low-fidelity image. So shrink until it fits.
"""

import io
import os

#: 0 = unlimited (Claude's limit is 10MB/image, far above any shot we produce). Set from the
#: `initialize` handshake's clientInfo by server._apply_client_limits, or via the env var.
#: This module owns the value; capture and server both read/write it HERE so there is one
#: source of truth rather than two drifting copies.
MAX_OUT_KB = int(os.environ.get("MCP_SCREEN_MAX_OUTPUT_KB", "0"))

#: Default headroom for the accompanying text block + JSON envelope. Callers that know how
#: much text they will emit alongside the image should pass `reserve_bytes` instead — an
#: annotated screenshot can carry one line per detected element, which on a busy 4K desktop
#: far exceeds this default and would push the whole result back over the client's cap.
ENVELOPE_BYTES = 2048

_START_QUALITY = 60


def b64_len(n):
    """Encoded size of n raw bytes — the number the client's cap actually applies to."""
    return ((n + 2) // 3) * 4


def fit_to_budget(out, raw, downscale, max_out_kb=None, reserve_bytes=None):
    """Shrink `out` until its base64 fits the cap. Returns (raw_bytes, note, (w, h)).

    The returned (w, h) is the size of the image ACTUALLY encoded, and callers MUST use it
    to restate their view->desktop transform. Shipping a shrunk image while advertising the
    pre-shrink scale makes every view-space click miss by that ratio — the stale-view guard
    cannot catch it, because the view id never changed.

    `downscale(img, w, h) -> img` is injected (capture passes its cv2/PIL implementation).
    Returns `raw` unchanged, and an empty note, when no cap applies or it already fits —
    that no-op path must stay byte-identical, since it is what Claude takes.

    Lossless WebP of a 4K desktop is ~900KB encoded; a 20KB cap needs ~45x less, so we go
    lossy AND smaller. Degrading is the whole point: a blurry overview the model can see
    beats a crisp one the client throws away. Region shots are usually already under the
    cap and skip this entirely.
    """
    cap = MAX_OUT_KB if max_out_kb is None else max_out_kb
    if not cap:
        return raw, "", out.size
    reserve = ENVELOPE_BYTES if reserve_bytes is None else max(reserve_bytes, 512)
    budget = cap * 1024 - reserve
    if budget <= 0 or b64_len(len(raw)) <= budget:
        return raw, "", out.size

    w0, h0 = out.size
    q = _START_QUALITY

    def enc(scale):
        w, h = max(1, round(w0 * scale)), max(1, round(h0 * scale))
        im = out if scale >= 1.0 else downscale(out, w, h)
        b = io.BytesIO()
        im.convert("RGB").save(b, format="WEBP", quality=q, method=4)
        return b.getvalue(), w, h

    # Encoded size tracks pixel count, so one lossy probe predicts the scale that fits:
    # area_ratio = budget/actual, and scale is its square root. A fixed ladder either
    # overshoots the cliff (wasting half the budget on a needlessly blurry image) or
    # burns six encodes getting there.
    cand, w, h = enc(1.0)
    # The estimate converges in 1-2 steps on real screenshots, but aliasing can make a
    # downscale encode LARGER than predicted. Fall back to halving so fitting is
    # GUARANTEED, not merely likely — returning an over-budget payload is the exact
    # failure this function exists to prevent.
    for i in range(12):
        got = b64_len(len(cand))
        if got <= budget:
            return (
                cand,
                (
                    f" [shrunk to {len(cand) // 1024}KB ({w}x{h}, q{q}) to fit this "
                    f"client's {cap}KB tool-output cap — use region=[x,y,w,h] for a "
                    f"crisp zoom]"
                ),
                (w, h),
            )
        if min(w, h) <= 8:
            break
        scale = min(0.95, (budget / got) ** 0.5 * 0.92) if i < 3 else 0.6
        cand, w, h = enc(scale)
        w0, h0 = w, h

    # Floor reached and still over: send it anyway and say so, rather than silently lying.
    return (
        cand,
        (
            f" [WARNING: {b64_len(len(cand)) // 1024}KB still exceeds the {cap}KB cap; "
            f"the client may drop this image — use region=[x,y,w,h] to capture less]"
        ),
        (w, h),
    )
