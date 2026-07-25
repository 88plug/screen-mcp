# Changelog

Calver headings match the 88plug hub (`YEAR.MONTH.<commit-count>` on `main`).

## Unreleased

- **WebP lossless encode is ~19x cheaper — a plain screenshot went 4194 ms → 1286 ms.**
  Encoding was 85% of a shot's wall time (3564 ms of it), measured on a real 2576x1449
  frame. libwebp's `method`/`quality` only trade CPU for bytes in lossless mode — the
  pixels are identical at every setting — so `method=0, quality=20` is a free win:
  195 ms for 140 KB more. Those bytes cost nothing, because an image is billed at
  `ceil(w/28) * ceil(h/28)` visual tokens (4784 here) regardless of encoded size, and
  759 KB of base64 is far under the API's 10 MB per-image cap. Env-overridable via
  `MCP_SCREEN_WEBP_METHOD` / `MCP_SCREEN_WEBP_EFFORT`; `tests/test_encode.py` asserts
  pixel-exact round-trips at every method/effort combination so the lossless guarantee
  cannot silently regress.

- **Per-stage timing on every screenshot and action (`stages: …ms`).** Stage stamps live in
  `state.py` (the one module everything imports, so no cycle) and cover pull, decode, resize,
  encode, aware, ground, gate and settle, plus an explicit `other` remainder so unexplained
  time is visible rather than hidden. This exists because benching stages OFFLINE kept
  producing numbers that did not transfer — an isolated LANCZOS bench read 553ms while the
  whole live shot was 603ms — and guessing which stage dominated is how ~46ms/grab of memcpy
  and a 264ms-per-shot subprocess both survived an entire optimization pass. First real
  breakdown of a 601ms monitor shot: `resize 408, encode 173, decode 19, aware 1` with `pull`
  under 1ms. Resize is 68% and the current top cost.

- **Awareness probe cached — it was spawning a Python subprocess on every screenshot.**
  `awareness.summary()` runs per shot. When the optional window-info extension isn't loaded
  it fell through to `atspi_titles()`, which spawns a fresh interpreter (measured 263.6ms)
  purely to render the text line "awareness: unavailable". Whether the extension is loaded
  only changes on a Wayland re-login or a `screen_reload`, so the negative verdict is now
  cached (`MCP_SCREEN_AWARENESS_TTL_S`, default 30s) rather than re-probed every frame.
  A plain monitor shot went 1084ms -> 603ms.

- **Two hidden 33MB copies removed from every grab.** `_sample_to_rgba` ran on every frame
  pull — including every poll of the settle and change-gate loops — and did `bytes(mi.data)`
  (a full memcpy of the mapped 4K frame, 9.7ms) followed by `arr[..., [2,1,0,3]]` (a fancy-index
  allocation to swap BGRx→RGBA, 36.7ms). `mi.data` is already a buffer, so `np.frombuffer`
  wraps it as a view for free; and the pipeline now negotiates **RGBA** directly, so
  videoconvert does the channel order in C and there is nothing left to swap. The old
  "BGRx on the wire for speed" comment was backwards once the cost landed in numpy. ~46ms
  saved per grab; a plain monitor shot went 1286ms → 1084ms, and the poll loops that grab
  5–9 times per action save proportionally more. Colors verified live after the caps change.

  The copy must be `.copy()`, not `ascontiguousarray`: with RGBA the trimmed view is already
  contiguous, so `ascontiguousarray` returned it unchanged — leaving the array aliasing the
  buffer after `unmap` (use-after-free) and read-only, which killed the alpha write with
  "assignment destination is read-only" on the first live grab.

  Still open, measured but not wired: a region shot converts the whole frame then discards
  ~95% of it, which is why a 1500x130 region can cost *more* than a full-monitor grab
  (87.9ms vs 3.9ms cropping first). Cropping in `_sample_to_rgba` would make `_note_frame`'s
  per-monitor freshness signature region-scoped, so it needs its own path.

- **`screen_drag` takes `modifiers`, held for the whole press-move-release.** Without it,
  `screen_read_selection` could not be used where it matters most: a terminal running a TUI
  (Claude Code, vim, htop) enables mouse tracking and consumes the drag itself, so no
  terminal-level selection is ever made and the copy returns nothing. `modifiers: ["shift"]`
  makes the terminal emulator bypass the app's mouse grab and select on its own. Verified
  live end-to-end — shift+drag over a TUI pane then `ctrl+shift+c` returned the line
  verbatim, box-drawing glyphs, em-dash and spacing intact, matching the pixels on screen.
  Modifiers press inside the same device lock as the drag and release in a `finally` on both
  the uinput and portal paths, because a stuck Shift would corrupt every later keystroke.

- **New `screen_read_selection` — read text exactly, without OCR.** Copies the focused
  window's selection and returns it verbatim. An OCR-based read of a full 4K monitor
  loses ~8% of characters on sub-12px code (measured against known rendered text across
  a font-size sweep — an artifact of the 4K→2576 downscale, not of the filter: LANCZOS
  beat box/bilinear/nearest at every size and stays the default). The clipboard is
  **cleared before** the copy combo is sent: without that, an app that ignores the combo
  leaves the prior clipboard in place and the tool would return the user's unrelated
  clipboard contents labelled as screen-read text. The user's clipboard is saved and
  restored regardless. Terminals need `combo='ctrl+shift+c'`.

- **Post-action change-gate can wait on damage events instead of polling.**
  `MCP_SCREEN_WAIT_MODE=poll|event|hybrid` (default `poll`, byte-identical to before).
  Damage arrives every ~270–550 ms on an active desktop while the gate polls every 60 ms,
  so most poll iterations grab and convert a 4K frame to learn nothing. `hybrid` blocks on
  a real damage event with the poll interval kept as a backstop, so an absent event
  degrades to today's behavior rather than stalling. `screen_diag` now reports damage
  cadence, keepalive-resend counts and wake stats under `cursor.events`.

- **Focus verification — root-cause fix for clicks landing on the wrong window/tab.**
  `screen_focus`/`focus=` no longer report success without confirming the correct
  window actually got raised: both the window-info-extension path (`activate_window`)
  and the no-extension GNOME Overview fallback (`activate_via_overview`) now check
  `awareness.focused_window()` before claiming success, instead of trusting a bare
  boolean or "the keystrokes were sent." Any real focus/activation attempt now also
  marks the current view stale — `resolve_xy` raises a new `FocusDriftError`
  (a `StaleViewError` subclass) rather than silently clicking screenshot coordinates
  that may no longer match what's on top, until a fresh screenshot is taken.
  `_action`, `screen_do`, and `screen_tour` now all check and act on a focus
  failure instead of discarding it (`screen_tour` previously never applied
  per-step `focus` at all — a complete no-op).
- Element-id staleness guard: `element=<id>` (from an annotated screenshot) now
  raises the same class of stale error when a later screenshot has superseded the
  cached elements, instead of silently resolving to a renumbered/wrong element.
- `verify=true` on `screen_key`/`screen_type` now actually performs a whole-frame
  diff instead of silently no-op'ing — it previously only worked for
  coordinate-bearing tools (click/scroll/drag).
- uinput's fractional-scale miscalibration warning now surfaces into
  `screen_click`/`move`/`scroll`/`drag`'s own returned text instead of sitting
  only in an internal log file the caller never reads.
- Removed `reliability.wrap_call` — fully dead code (zero call sites); its
  ack-gate/hash/diff primitives are already composed directly by server.py's
  `_action`/`_verify`.
- 8 new regression tests (`tests/test_input.py`) covering the above.
- **`screen_watch` — human-eye 1 fps observation (default confirm for thrashy UIs).**
  Samples a region/monitor at `fps` (default 1) for `seconds` (default 6) and
  returns `settled | evolving | jitter | unstable`. Sustained local motion
  without navigation → `jitter` (the "looks crazy" case: force-directed graphs,
  canvas thrash). Drive-screen skill + server `instructions` + docs now make
  watch the default confirm path after graphs/maps/canvases/loaders — a single
  screenshot is a glance, not a visual QA pass.
- Unit tests: `tests/test_watch.py` for verdict classification.
- **`screen_sense` + `sense.to_pixel_signal()` — the pixel half of cross-layer
  verification.** Normalizes the rich SENSE dict into the compact
  `{changed, opened, modal, no_op, activity}` contract and exposes it as a
  read-only tool, so an agent can hand the `pixel` object to os-control-mcp's
  `os_verify` and catch a GUI that changed while the underlying service did not.
  7 new `to_pixel_signal` unit tests.

## 2026.7.24

- 88plug compliance: dual Grok install on docs/install; MkDocs-relative doc links
  (drop 404 `blob/main/*.md` paths); Manual MCP docs use `bin/screen-mcp` (T1
  launcher, not bare python3); document CI Python 3.12 pin (rapidocr wheels);
  unknown-tool `tools/call` returns MCP `isError` result; ruff format clean.

## 2026.7.23

- Fixed the takeover guard false-firing forever on a STATIC monitor: `cursor_pos(prefer_node=...)`
  pins to a per-node cursor sample that never refreshes once a monitor stops repainting, so
  `guard_user` compared every subsequent commanded click against ONE frozen point and raised
  `UserControlError` with the IDENTICAL "live" position every time — also blocking the
  `_nudge_prime` frame-refresh path that would have fixed it (it calls `guard_user()` too).
  Added `capture.cursor_sample_age()` and a `MCP_SCREEN_GUARD_STALE_S` (default 3.0s) cutoff:
  a sample older than that is treated as "can't be read" and fails open, same as today's
  existing no-cursor fail-open path.

## 2026.7.21

- `prereqs` matrix in `screen_diag` + `setup.sh` bootstrap (status + `next_step` per dependency).
- `awareness.extension_state()` — distinguish window-info installed-but-not-loaded vs not installed.
- `server.__version__` derives rolling calver (matches hub); guard test keeps `plugin.json` version-less.

## 2026.6.23

- Initial release: MCP server that gives a model eyes and hands on a Linux
  Wayland desktop — screenshot any monitor and click, type, scroll, drag, and
  read any visible app over xdg-desktop-portal (RemoteDesktop + ScreenCast),
  with optional OCR and OmniParser icon grounding. Ships the `drive-screen`
  skill that encodes the locate-ground-act-confirm loop.
