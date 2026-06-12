# screen-mcp

An MCP server that gives a model **eyes and hands** on a Linux Wayland desktop.
Screenshots via PipeWire, pointer/keyboard via the RemoteDesktop portal, OCR +
icon detection via RapidOCR + an OmniParser ONNX, an ambient sense layer that
diffs frames so the agent knows when something opened / nothing changed, a
write-through world-model cache so a recognised screen skips OCR, and an opt-in
ack gate that blocks close-combos / destructive-keyword clicks until the caller
passes a confirmation token.

Current version: **1.3.2**.

## Requirements

- Linux + Wayland + GNOME (the awareness layer uses a bundled GNOME Shell
  extension; AT-SPI is the fallback for GTK apps).
- Python 3.10+ (tested on 3.14).
- GStreamer **>= 1.28** (uses `leaky-type`; the older `drop=` was removed in
  1.28). PipeWire + `xdg-desktop-portal-gnome`.
- `wl-clipboard` (for the Unicode paste path in `screen_type`).
- A DejaVu Sans Bold font (Set-of-Marks labels; falls back to PIL's default).

## Install

System packages first — see [requirements.txt](requirements.txt) for the full
`pacman` / `apt` one-liners.

```bash
# Arch
sudo pacman -S python-gobject gobject-introspection \
               gstreamer gst-plugins-base gst-plugins-good gst-libav \
               pipewire pipewire-pulse xdg-desktop-portal-gnome \
               wl-clipboard ttf-dejavu

# Python deps
pip install -r requirements.txt
```

Install the GNOME Shell extension (optional but recommended — gives the
awareness layer reliable focused-window + window-list data):

```bash
gnome-shell-extension/window-info@local/install.sh
# then enable via gnome-extensions enable window-info@local
```

## Wire it into Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcp-screen": {
    "command": "python3",
    "args": ["/path/to/mcp-screen/server.py"]
  }
}
```

The first run triggers an `xdg-desktop-portal` consent dialog (pick which
monitor(s) to share). The portal returns a restore token which is persisted to
`~/.config/mcp-screen/token` — subsequent runs are silent.

## Tools

| Name | What it does |
|---|---|
| `screen_screenshot` | Capture the desktop. `region=[x,y,w,h]` or `monitor=N` to zoom. `annotate=true` overlays numbered Set-of-Marks + lists click coords. `use_cache=true` (with annotate) reuses learned elements for a known screen (skips OCR). `fresh=true` forces a current frame on a damage-driven static monitor (defeats the keepalive-resend stale read) — but it nudges the pointer, so it's used sparingly (auto only right after an unconfirmed action), not on every shot; pass it explicitly if a static-monitor read looks stale. |
| `screen_list_monitors` | Monitors (origin/size/scale), desktop bounds, focused windows. |
| `screen_move_mouse` | Move pointer to `x,y` (view-space default; server maps to real px). |
| `screen_click` | Click at `x,y` or in place. `button: left\|right\|middle`, `double: true`. |
| `screen_scroll` | Wheel scroll. `direction: up\|down\|left\|right`, `amount: notches`. |
| `screen_drag` | Press-drag from `(x1,y1)` to `(x2,y2)`. |
| `screen_key` | Press a key/combo: `"Ctrl+L"`, `"Enter"`, `"Alt+Tab"`, `"F5"`. |
| `screen_type` | Type text (Unicode via `wl-copy` + Ctrl+V; ASCII via keysyms). `enter: true` presses Enter after. Keys go to the FOCUSED window — pass `focus: "app"` or call `screen_focus` first. |
| `screen_focus` | Raise + give KEYBOARD focus to a window (`app`/`title`/`id`) so injected keys/clicks land in it. Uses the `window-info` extension's `ActivateWindow` when loaded, else the GNOME overview. |
| `screen_do` | Batched ordered actions in one call. |
| `screen_tour` | Visit several UI states and get a labeled thumbnail of each. |
| `screen_read_page` | Auto-scroll a scrollable view in one call; accumulates every interactable. |
| `screen_wait` | Block until the screen settles, then optionally screenshot. |
| `screen_session` | Recorder: `op=start\|stop\|list\|status\|replay-path`. |
| `screen_reload` | Hot-reload the server in place after edits (no `/mcp` reconnect). |
| `screen_diag` | Health dump: session/geo, cursor, grounding backends, world-model stats. |

Every action takes `space: 'view' \| 'desktop' \| 'norm'` (default `view` — coords
as seen in the last screenshot), `shot: true` to return a screenshot after,
`verify: true` to warn on no-screen-change misclicks, `force: true` to bypass
the user-takeover guard, and `element: <id>` to click an element id returned by
the last `annotate=true` shot (server resolves exact coords; no guessing).

## Environment variables

| Variable | Effect |
|---|---|
| `MCP_SCREEN_GUARD=1` | Enable the reliability ack gate. Destructive combos (`Ctrl+W`, `Alt+F4`, `cmd+q`), OCR-matched destructive keywords (`delete`/`pay`/`submit`/...), and out-of-allowlist actions block unless the caller passes `ack=<reason>`. |
| `MCP_SCREEN_APPS="firefox,terminal"` | With guard on, restrict actions to this allowlist of focused apps. |
| `MCP_SCREEN_AUDIT_FRAMES=1` | Add pre/post frame hash + `changed_bbox` to every audit log line. ~100-500ms latency per action. |
| `MCP_SCREEN_AMBIENT=0` | Disable the ambient `SENSE` hint block. |
| `MCP_SCREEN_GUARD_PX=40` | Threshold for the user-takeover guard (live pointer vs last-commanded). |
| `MCP_SCREEN_CPU_THREADS=6` | ONNX intra-op thread count for OmniParser. |
| `MCP_SCREEN_MAX_EDGE=2576` | Screenshot downscale target (long edge). |
| `MCP_SCREEN_NO_FRESH=1` | Disable forced fresh-frame capture on static monitors (screenshots may then return the keepalive-resent stale frame). |
| `MCP_SCREEN_FOCUS_SETTLE_MS=150` | Delay after `screen_focus` activates a window (lets the compositor deliver keyboard focus before a following keystroke burst). |
| `MCP_SCREEN_NO_NUDGE=1` | Disable the pointer damage-nudge used to prime/refresh a static monitor's frame. |

## Data paths

| Path | What |
|---|---|
| `~/.config/mcp-screen/token` | Portal restore token (one-time consent). |
| `~/.local/share/mcp-screen/world/map.db` | World-model SQLite cache (per-screen learned elements). |
| `~/.local/share/mcp-screen/sessions/<sid>/` | Recorder trajectories + WebP frames + `replay.html`. |
| `~/.local/state/mcp-screen/actions.jsonl` | Reliability audit log (one JSON line per action). |
| `/tmp/screen_err.txt` | Last unhandled tool traceback (dev-diagnostic only). |

## Dev workflow

```bash
pytest -q                   # 78 tests, ~0.7s, no live D-Bus needed (conftest stubs)
```

Edit a `.py`, then in the running Claude Code session:

```
screen_reload              # re-execs the server in place (preserves the MCP connection)
```

On any tool exception the dispatcher writes the full traceback to
`/tmp/screen_err.txt` (the JSON-RPC error only carries the message); read it
when debugging crashes.

## Ops notes (hard-won — read before touching capture/input)

- **Fractional scaling** — `NotifyPointerMotionAbsolute` coords are *logical*
  and *local* to each stream (keyed by `node_id`). Don't add a global logical
  origin; the portal clamps with "Invalid position". See `input.global_to_logical`.
- **Cursor position** — `cursor_mode=METADATA(4)` means the cursor is NOT baked
  into frames. PipeWire attaches a `SPA_META_Cursor` to its src pad, but
  `videoconvert` strips it and PyGObject can't downcast it — `capture.py` reads
  it via a `ctypes` pad-probe with x86-64 offsets. We composite a marker back
  into plain screenshots so the pointer stays visible.
- **User-takeover guard** — `input.guard_user` compares the live pointer to
  where WE last commanded it; > `MCP_SCREEN_GUARD_PX` px drift ⇒ caller took
  the mouse ⇒ STOP. Pass `force=true` to bypass / take control back. Fails open
  if the cursor can't be read.
- **Unicode typing** — the portal keysym path drops non-ASCII; `input.type_text`
  auto-pastes any non-ASCII string via `wl-copy` + Ctrl+V, with a `finally`
  restoring the prior clipboard (or `wl-copy --clear` if it couldn't be saved)
  so sensitive text never outlives the call. Falls back to ASCII-only keysyms
  if `wl-clipboard` is absent. xdotool / XTEST can NOT reach native-Wayland apps.
- **Modifier+letter combos** — `input.key` lowercases single-letter trailing
  parts when modifiers are present, so `"Ctrl+A"` is select-all, not Ctrl+Shift+a
  (capital-A is the X11 keysym for shifted A). Standalone `key("A")` keeps its
  case for legacy text-input behavior.
- **GPU is hard-disabled** (`CUDA_VISIBLE_DEVICES=""` at server top); grounding
  is CPU-only by design — predictable latency, no driver flake.

## Install as a Claude Code plugin

`screen-mcp` ships as a Claude Code plugin that bundles the MCP server **and** a
`drive-screen` skill (the locate → ground → act → confirm loop).

```
/plugin marketplace add 88plug/screen-mcp
/plugin install screen-mcp@screen-mcp
```

One-time setup after install (the server has system + Python deps the manifest
can't install for you):

```
# in the installed plugin dir (or a clone)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# system packages (Arch/Manjaro names; use your distro equivalents):
#   gstreamer>=1.28, pipewire, python-gobject, xdg-desktop-portal-gnome, wl-clipboard
```

Requirements: **Linux + Wayland + GNOME**. First run pops an `xdg-desktop-portal`
RemoteDesktop + ScreenCast consent dialog (token cached at `~/.config/mcp-screen`).
Optional: `/dev/uinput` (group `input`) for the kernel input backend, and the
bundled GNOME-Shell extension for full window awareness (one-time Wayland re-login).

The launcher (`bin/screen-mcp`) fails with a clear message if the deps are missing,
so a misconfigured install never silently half-works.

## License

[FSL-1.1-ALv2](LICENSE.md) © 2026 [88plug](https://github.com/88plug) —
Functional Source License; converts to Apache 2.0 two years after each release.
