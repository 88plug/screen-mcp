"""Client tool-output budget fitting.

Grok Build truncates an MCP tool result at 20KB. A base64 image cut mid-string fails its
integrity check and the whole image is dropped, so the model sees NOTHING — strictly worse
than a low-fidelity image. These tests pin the fitting behaviour and, critically, that the
default (Claude, uncapped) path stays a byte-for-byte no-op.
"""

import io

import pytest
from PIL import Image

import conftest

# These exercise the REAL capture/server modules (MAX_OUT_KB, _b64_len, _fit_to_budget,
# capture_desktop). Without gi/GStreamer/a session bus, conftest swaps in a stub `capture`
# that has none of those attributes, so the module must gate exactly like
# test_capture_real.py does — otherwise CI reports AttributeError instead of skipping.
pytestmark = pytest.mark.skipif(
    not conftest.REAL_STACK, reason="needs real gi/GStreamer + session bus"
)

import capture  # noqa: E402
import server  # noqa: E402


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
    assert "shrunk" in note and "region=" in note, (
        "must tell the agent how to get detail"
    )
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
    "params",
    [{}, {"clientInfo": None}, {"clientInfo": {}}, {"clientInfo": {"name": None}}],
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


# --- region + monitor precedence -------------------------------------------------------


def test_region_with_monitor_is_translated_not_dropped(monkeypatch):
    """region+monitor used to silently return the WHOLE monitor. Both together must mean
    'this box, relative to that monitor's origin'."""
    import capture as cap

    geo = [
        {"node": 1, "x": 0, "y": 0, "w": 3840, "h": 2160, "live": True},
        {"node": 2, "x": 3840, "y": 0, "w": 3840, "h": 2160, "live": True},
    ]
    seen = {}

    monkeypatch.setattr(cap, "ensure_geo", lambda force=False: geo)
    monkeypatch.setattr(cap, "validate_scope", lambda *a, **k: None)
    monkeypatch.setattr(
        cap, "monitors_for", lambda r: (seen.update(region=r), [geo[0]])[1]
    )
    monkeypatch.setattr(cap.state, "SESSION", {"W": 7680, "H": 2160})

    class _Img:
        def crop(self, box):
            seen["crop"] = box
            return self

    monkeypatch.setattr(cap, "_full_canvas", lambda m: _Img())
    monkeypatch.setattr(
        cap, "grab", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cold"))
    )

    cap.capture_desktop(region=[0, 0, 600, 300], monitor=1)
    assert seen["region"] == [3840, 0, 600, 300], (
        "region must be offset by monitor origin"
    )
    assert seen["crop"] == (3840, 0, 4440, 300)


def test_region_alone_is_desktop_absolute(monkeypatch):
    """The pre-existing contract: region with no monitor stays desktop-absolute."""
    import capture as cap

    geo = [{"node": 1, "x": 0, "y": 0, "w": 3840, "h": 2160, "live": True}]
    seen = {}
    monkeypatch.setattr(cap, "ensure_geo", lambda force=False: geo)
    monkeypatch.setattr(cap, "validate_scope", lambda *a, **k: None)
    monkeypatch.setattr(cap, "monitors_for", lambda r: (seen.update(region=r), [])[1])
    monkeypatch.setattr(cap.state, "SESSION", {"W": 3840, "H": 2160})
    with pytest.raises(RuntimeError):
        cap.capture_desktop(region=[10, 20, 600, 300])
    assert seen["region"] == [10, 20, 600, 300]
