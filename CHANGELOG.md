# Changelog

Calver headings match the 88plug hub (`YEAR.MONTH.<commit-count>` on `main`).

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
