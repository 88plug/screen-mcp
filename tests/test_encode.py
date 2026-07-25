"""The WebP encode settings trade CPU for bytes only — they must never trade fidelity.

capture.py imports gi/GStreamer, so these assert the codec contract directly rather than
importing the module: if libwebp or Pillow ever made these settings lossy, screenshots would
silently degrade and OCR/reading accuracy with them.
"""

import io
import os

import numpy as np
import pytest
from PIL import Image


def _frame():
    rng = np.random.default_rng(0)
    a = np.zeros((160, 240, 4), np.uint8)
    a[..., 3] = 255
    a[..., :3] = 30
    a[:, :60, :3] = 55
    for y in range(10, 160, 17):  # text-like high-frequency rows
        a[y : y + 5, 20:220, :3] = rng.integers(120, 240, (5, 200, 3), dtype=np.uint8)
    return Image.fromarray(a, "RGBA")


def _roundtrip(**kw):
    buf = io.BytesIO()
    _frame().save(buf, format="WEBP", lossless=True, **kw)
    buf.seek(0)
    return Image.open(buf).convert("RGBA"), len(buf.getvalue())


@pytest.mark.parametrize("method", [0, 4])
@pytest.mark.parametrize("effort", [0, 20, 80])
def test_webp_lossless_is_pixel_exact_at_every_effort(method, effort):
    out, _ = _roundtrip(method=method, quality=effort)
    assert np.array_equal(np.asarray(out), np.asarray(_frame()))


def test_shipped_defaults_match_capture_module_constants():
    # Mirrors capture.WEBP_METHOD / WEBP_EFFORT without importing capture (needs gi).
    assert int(os.environ.get("MCP_SCREEN_WEBP_METHOD", "0")) == 0
    assert int(os.environ.get("MCP_SCREEN_WEBP_EFFORT", "20")) == 20
    out, _ = _roundtrip(method=0, quality=20)
    assert np.array_equal(np.asarray(out), np.asarray(_frame()))
