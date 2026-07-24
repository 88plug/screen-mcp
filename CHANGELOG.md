# Changelog

Calver headings match the 88plug hub (`YEAR.MONTH.<commit-count>` on `main`).

## Unreleased

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
