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

- **New `screen_read_text` — read the screen as text + click coords, no image.** Every
  latency win this session left the token cost untouched: an image bills a FIXED
  `ceil(w/28)*ceil(h/28)` visual tokens (4784 for a 4K monitor) regardless of how small the
  encode gets. For navigate-by-text work the pixels are not what the caller needs. Measured
  on a 4K monitor: 4784 tokens / 412ms for a screenshot vs **~1350 tokens / 67ms** unfiltered
  (104 elements) and **~95 tokens** with `contains=`. Reuses the existing perception path —
  world-model recall first, which skips OCR entirely on a known screen (7747ms → 42ms
  measured) — so it was almost entirely wiring, not new machinery. Coordinates return in
  desktop space and are directly clickable. (Steal from QuickDesk's `get_ui_state`.)

  The first draft of the tool description claimed "~10x cheaper". Measuring it gave 3.5x
  unfiltered and 50x filtered, so the description now carries the real numbers.

- **`screen_read_selection` waits for the clipboard instead of guessing 140ms.** It slept a
  fixed `GLib.usleep(140000)` after sending the copy combo, then read. That was wrong in both
  directions: it charged every fast app the full delay, and any app slower than it read back
  EMPTY — which, since we clear the clipboard first, is indistinguishable from "nothing was
  copied" and surfaced as a false negative. Now polls at 15ms until non-empty or
  `MCP_SCREEN_SELECTION_TIMEOUT_S` (default 1.5s). Clearing first is what makes polling sound:
  non-empty can only mean this copy landed. Verified live — the terminal under test took
  ~1.3s to publish the selection, i.e. the old fixed sleep would have failed it.
  (Steal from QuickDesk's `wait_for_clipboard_change`.)

- **Encode re-swept and left alone — it is already at the floor (negative result).**
  With the shipped `m=0 q=20` at 202ms median (9 reps) on the current RGB/INTER_AREA output,
  nothing beats it: higher lossless effort is 237-251ms, `method=1` is 344-613ms, PNG is
  343-768ms, and **lossy WebP is SLOWER** (228-307ms) because it runs rate-distortion
  analysis while lossless method=0 is fast entropy coding. So the lossless guarantee costs
  no speed at all — it is not a quality/latency tradeoff, and there is no reason to weaken
  it. Recorded as a DO-NOT-RE-TUNE block in `capture.py` next to the constants.

  Method note: a 3-rep bench made `q=40` look 8% faster and 8% smaller; at 9 reps it is 24%
  SLOWER. Third time this session a small-sample bench pointed the wrong way (see also the
  offline LANCZOS figure and the first filter eval) — medians, repeats, and measuring the
  live path are the only things that have held up.

- **Test harness is REAL-FIRST — the suite now executes capture.py instead of a stub.**
  `tests/conftest.py` unconditionally installed stub `gi`/`state`/`capture` modules into
  `sys.modules`, so 144 passing tests never ran a line of the shipped capture path. Every
  capture bug this session was caught by driving the real desktop instead: an ndarray
  aliasing an unmapped GstBuffer (read-only + use-after-free), a missed `_sample_to_rgba`
  rename, and a handler returning a bare `str` where the dispatcher indexes `["content"]`.
  conftest now probes for real gi + Gst + a session bus and only stubs what genuinely will
  not import, exposing `REAL_STACK` so tests can gate. New `tests/test_capture_real.py`
  covers the real `_sample_to_rgb` (exact pixels, row-padding stride trim, and that the
  result is writable and owns its data), `_downscale` incl. the PIL fallback, and a
  **view-transform round-trip** — the click-accuracy invariant, previously untested.
  150 pass locally against the real stack; 144 pass + 6 skip in CI where gi is absent.

  The new tests were mutation-checked rather than assumed: reintroducing the alias bug fails
  2, adding a broken stride trim fails 3, and forcing the view transform to 1:1 fails 4.

  Re-ran legibility end-to-end through the real `encode_store` (real downscale, real WebP
  settings, real transform) rather than an offline approximation: 92% mean / 91% across the
  three smallest fonts — matching the offline cv2 INTER_AREA figures exactly.

- **Screenshot downscale moved to cv2 INTER_AREA — 15x faster than PIL LANCZOS.**
  287.5ms → 18.8ms on a real 3840x2160 → 2576x1449 frame; live shot 528ms → 412ms with
  the resize stage at 84ms (was 408ms two commits ago). INTER_AREA is also the principled
  filter for minification: it averages the source pixels each output pixel covers, so it
  antialiases without the ringing a cubic/lanczos kernel leaves on text edges. The encoded
  payload got *smaller* too (538KB → 502KB) — less high-frequency ringing to compress.
  Falls back to PIL when cv2 is absent, so capture never hard-depends on grounding's stack.

  This also **reverses an earlier call in this changelog**. A narrower eval had LANCZOS 1pp
  ahead of the alternatives and concluded "don't swap the filter". Re-run against known
  rendered text at 9/10/11/12/13/14/16/20px, PIL-LANCZOS / INTER_AREA / INTER_LANCZOS4 /
  INTER_CUBIC all score 92-93% mean and 91-92% across the three smallest sizes — the gap
  was never real, and 15x was being paid for nothing.

  It also kills a planned GStreamer `videoscale` change. videoscale-lanczos measured 78.5ms
  net, genuinely faster than PIL — but it would have required teaching every coordinate path
  that frame px != desktop px (the documented click-accuracy hazard), and pipeline scaling is
  EAGER: it runs on every damage frame (~3.5/s) rather than only frames we pull. cv2 is
  lazy, 4x faster still, and touches no coordinates.

- **Alpha plane dropped end-to-end — pipeline negotiates RGB, not RGBA.** The stage timer
  showed resize was 68% of a shot (408ms of 601ms), and it was resizing a channel nothing
  reads: the alpha byte was forced to a constant 255, carried through LANCZOS, then encoded.
  videoconvert now emits 24-bit RGB, so the decode copies 24.9MB instead of 33.2MB and the
  resize works on 3 channels — measured 471ms → 298ms (1.58x) on a real frame. Encode is
  ~18ms slower on RGB but the output is byte-for-byte the same size (745KB), because a
  constant alpha plane compresses to nothing. Net ~155ms/shot; live shot 601ms → 528ms with
  resize 408 → 264. Colors verified live after the caps change.

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
