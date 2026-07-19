# Changelog

Calver headings match the 88plug hub (`YEAR.MONTH.<commit-count>` on `main`).

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
