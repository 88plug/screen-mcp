"""Client tool-output budget fitting.

Grok Build truncates an MCP tool result at 20KB. A base64 image cut mid-string fails its
integrity check and the whole image is dropped, so the model sees NOTHING — strictly worse
than a low-fidelity image. These tests pin the fitting behaviour and, critically, that the
default (Claude, uncapped) path stays a byte-for-byte no-op.
"""

import io

import pytest
from PIL import Image

import capture
import server


@pytest.fixture
def restore_cap():
    prev = capture.MAX_OUT_KB
    yield
    capture.MAX_OUT_KB = prev


def _lossless(img):
    b = io.BytesIO()
    img.convert("RGB").save(b, format="WEBP", lossless=True, method=0, quality=20)
    return b.getvalue()


def _shot(w=2576, h=1449):
    """Structured content, not noise — noise is incompressible and not representative."""
    img = Image.new("RGB", (w, h), (18, 18, 22))
    for i in range(0, h, 40):
        for j in range(0, w, 160):
            img.paste((200, 200, 210), (j, i, min(j + 120, w), min(i + 18, h)))
    return img


def test_uncapped_is_byte_identical_noop(restore_cap):
    capture.MAX_OUT_KB = 0
    img = _shot()
    raw = _lossless(img)
    out, note = capture._fit_to_budget(img, raw)
    assert out is raw
    assert note == ""


def test_small_payload_under_cap_is_untouched(restore_cap):
    capture.MAX_OUT_KB = 20
    img = _shot(400, 200)
    raw = _lossless(img)
    assert capture._b64_len(len(raw)) <= 20 * 1024 - 2048
    out, note = capture._fit_to_budget(img, raw)
    assert out is raw and note == ""


def test_oversized_payload_is_fitted_and_still_decodes(restore_cap):
    capture.MAX_OUT_KB = 20
    img = _shot()
    raw = _lossless(img)
    assert capture._b64_len(len(raw)) > 20 * 1024, "fixture must exceed the cap"

    out, note = capture._fit_to_budget(img, raw)

    assert capture._b64_len(len(out)) <= 20 * 1024 - 2048, "must fit the cap"
    assert "shrunk" in note and "region=" in note, "must tell the agent how to get detail"
    Image.open(io.BytesIO(out)).load()  # a truncated image would raise here


def test_fitted_payload_uses_the_budget_rather_than_collapsing(restore_cap):
    """A fixed ladder overshot the cliff and wasted half the budget on a needlessly
    blurry image. The analytic estimate should land near the cap, not far under it."""
    capture.MAX_OUT_KB = 20
    img = _shot()
    out, _ = capture._fit_to_budget(img, _lossless(img))
    budget = 20 * 1024 - 2048
    assert capture._b64_len(len(out)) > budget * 0.4


def test_b64_len_matches_real_base64():
    import base64

    for n in (0, 1, 2, 3, 4, 100, 1023, 4096):
        assert capture._b64_len(n) == len(base64.b64encode(b"x" * n))


@pytest.mark.parametrize(
    "client,expected",
    [
        ("grok", 20),
        ("Grok Build", 20),
        ("claude-code", 0),
        ("Claude Code", 0),
        ("cursor", 0),
        ("", 0),
    ],
)
def test_client_cap_detection(client, expected, restore_cap, monkeypatch):
    monkeypatch.delenv("MCP_SCREEN_MAX_OUTPUT_KB", raising=False)
    capture.MAX_OUT_KB = 0
    server._apply_client_limits({"clientInfo": {"name": client}})
    assert capture.MAX_OUT_KB == expected


@pytest.mark.parametrize(
    "params", [{}, {"clientInfo": None}, {"clientInfo": {}}, {"clientInfo": {"name": None}}]
)
def test_malformed_handshake_does_not_raise(params, restore_cap, monkeypatch):
    monkeypatch.delenv("MCP_SCREEN_MAX_OUTPUT_KB", raising=False)
    capture.MAX_OUT_KB = 0
    server._apply_client_limits(params)
    assert capture.MAX_OUT_KB == 0


def test_explicit_env_override_beats_client_detection(restore_cap, monkeypatch):
    monkeypatch.setenv("MCP_SCREEN_MAX_OUTPUT_KB", "99")
    capture.MAX_OUT_KB = 99
    server._apply_client_limits({"clientInfo": {"name": "grok"}})
    assert capture.MAX_OUT_KB == 99
