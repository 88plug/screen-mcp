<div align="center">

# screen-mcp

Give a model eyes and hands on a Linux Wayland desktop: screenshot, click, type, scroll, drag, and read any visible app.

[![plugin-validate](https://github.com/88plug/screen-mcp/actions/workflows/plugin-validate.yml/badge.svg)](https://github.com/88plug/screen-mcp/actions/workflows/plugin-validate.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue?style=flat)](LICENSE.md)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2?style=flat)](https://github.com/88plug/claude-code-plugins)

</div>

screen-mcp is an MCP server for Claude Code that lets a model see and operate
your GNOME/Wayland desktop. It captures any monitor through PipeWire, drives the
pointer and keyboard through the `xdg-desktop-portal` RemoteDesktop portal, and
optionally reads the screen with OCR and grounds icons with an OmniParser ONNX
model. It is for developers who want an agent that can use real desktop apps,
not just a browser. It is pure Python and runs CPU-only.

## Quickstart

Install the plugin in Claude Code:

```text
/plugin marketplace add 88plug/screen-mcp
/plugin install screen-mcp@screen-mcp
```

Then run the one-time dependency setup (the server has system and Python deps
the manifest cannot install for you):

```bash
# in the installed plugin dir (or a clone)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

On first use the desktop portal pops a consent dialog asking which monitor(s) to
share. Pick one, and ask the model to take a screenshot:

```text
Take a screenshot of my desktop and tell me which window is focused.
```

You should get back a labeled capture plus the focused-window name within a few
seconds. The portal returns a restore token cached at `~/.config/mcp-screen` so
later runs are silent.

> [!IMPORTANT]
> screen-mcp runs on Linux + Wayland + GNOME only, and grounding is CPU-only by
> design. See [Requirements](#requirements) before installing.

## What it does

The server turns one capture-and-act loop into MCP tools a model can call
directly:

- Screenshot any monitor or region, with numbered Set-of-Marks overlays and
  click coordinates.
- Click, type, scroll, and drag in any visible app, including native Wayland
  apps that `xdotool` and XTEST cannot reach.
- Read on-screen text with OCR (RapidOCR) and ground icons with an OmniParser
  ONNX model (both optional).
- Sense changes: an ambient layer diffs frames so the agent knows when something
  opened or when an action was a no-op.
- Cache learned screens: a write-through world model lets a recognized screen
  skip OCR on the next visit.
- Gate destructive actions: an opt-in ack guard blocks close-combos and
  destructive-keyword clicks until the caller passes a confirmation token.

It also ships a `drive-screen` skill that encodes the locate → ground → act →
confirm loop, so the model knows how to use the tools well out of the box.

## MCP tools

These are the tools the server exposes. Every action also accepts `space`
(`view` / `desktop` / `norm`, default `view` — coords as seen in the last
screenshot), `shot: true` to return a screenshot after, `verify: true` to warn
on no-change misclicks, `force: true` to bypass the user-takeover guard, and
`element: <id>` to click an element id from the last annotated shot.

| Tool | What it does |
|---|---|
| `screen_screenshot` | Capture the desktop. `region` / `monitor` to zoom, `annotate=true` for numbered marks, `use_cache=true` to reuse learned elements, `fresh=true` to force a current frame on a static monitor. |
| `screen_list_monitors` | Monitors (origin/size/scale), desktop bounds, focused windows. |
| `screen_move_mouse` | Move the pointer to `x,y`. |
| `screen_click` | Click at `x,y` or in place. `button`, `double`. |
| `screen_scroll` | Wheel scroll by direction and amount. |
| `screen_drag` | Press-drag from one point to another. |
| `screen_key` | Press a key or combo, e.g. `Ctrl+L`, `Enter`, `Alt+Tab`. |
| `screen_type` | Type text into the focused window (Unicode via clipboard paste, ASCII via keysyms). |
| `screen_focus` | Raise and give keyboard focus to a window by app/title/id. |
| `screen_do` | Run a batch of ordered actions in one call. |
| `screen_tour` | Visit several UI states, return a labeled thumbnail of each. |
| `screen_read_page` | Auto-scroll a scrollable view and accumulate every interactable. |
| `screen_wait` | Block until the screen settles, then optionally screenshot. |
| `screen_session` | Recorder: `start` / `stop` / `list` / `status` / `replay-path`. |
| `screen_reload` | Hot-reload the server in place after edits. |
| `screen_diag` | Health dump: session, cursor, grounding backends, world-model stats. |

## Requirements

> [!IMPORTANT]
> screen-mcp targets a specific stack. It will not run elsewhere.

- Linux + Wayland + GNOME. The awareness layer uses a bundled GNOME Shell
  extension; AT-SPI is the fallback for GTK apps.
- Python 3.10+ (tested on 3.14).
- GStreamer >= 1.28 (uses `leaky-type`; the older `drop=` was removed in 1.28).
- PipeWire and `xdg-desktop-portal-gnome`.
- `wl-clipboard` for the Unicode paste path in `screen_type`.
- A DejaVu Sans Bold font for Set-of-Marks labels (falls back to PIL's default).

Grounding is CPU-only by design: the server hard-disables the GPU
(`CUDA_VISIBLE_DEVICES=""`) for predictable latency and no driver flake.

<details>
<summary>System packages</summary>

Install the system deps before the Python deps. See
[requirements.txt](requirements.txt) for the full `pacman` / `apt` one-liners.

```bash
# Arch / Manjaro
sudo pacman -S python-gobject gobject-introspection \
               gstreamer gst-plugins-base gst-plugins-good gst-libav \
               pipewire pipewire-pulse xdg-desktop-portal-gnome \
               wl-clipboard ttf-dejavu

# Python deps
pip install -r requirements.txt
```

</details>

<details>
<summary>GNOME Shell extension (recommended)</summary>

The bundled `window-info` extension gives the awareness layer reliable
focused-window and window-list data, and lets `screen_focus` activate windows
directly. Installing it needs a one-time Wayland re-login.

```bash
gnome-shell-extension/window-info@local/install.sh
gnome-extensions enable window-info@local
```

</details>

<details>
<summary>Kernel input backend (optional)</summary>

For the kernel input backend, grant access to `/dev/uinput` by adding your user
to the `input` group. The launcher (`bin/screen-mcp`) fails with a clear message
if a required dep is missing, so a misconfigured install never silently
half-works.

</details>

## Manual MCP setup

If you are not using the plugin, wire the server in directly. Add to
`~/.claude.json` under `mcpServers`:

```json
{
  "screen": {
    "command": "python3",
    "args": ["/path/to/screen-mcp/server.py"]
  }
}
```

## Configuration

screen-mcp reads optional environment variables. A few common ones:

- `MCP_SCREEN_GUARD=1` — enable the ack gate. Destructive combos
  (`Ctrl+W`, `Alt+F4`), OCR-matched destructive keywords, and out-of-allowlist
  actions block unless the caller passes `ack=<reason>`.
- `MCP_SCREEN_APPS="firefox,terminal"` — with the guard on, restrict actions to
  this allowlist of focused apps.
- `MCP_SCREEN_AMBIENT=0` — disable the ambient `SENSE` hint block.
- `MCP_SCREEN_CPU_THREADS=6` — ONNX intra-op thread count for OmniParser.
- `MCP_SCREEN_MAX_EDGE=2576` — screenshot downscale target (long edge).

<details>
<summary>All environment variables and data paths</summary>

| Variable | Effect |
|---|---|
| `MCP_SCREEN_GUARD=1` | Enable the reliability ack gate (destructive combos / keywords / out-of-allowlist actions block until `ack`). |
| `MCP_SCREEN_APPS` | With guard on, allowlist of focused apps. |
| `MCP_SCREEN_AUDIT_FRAMES=1` | Add pre/post frame hash + `changed_bbox` to each audit line (~100-500ms per action). |
| `MCP_SCREEN_AMBIENT=0` | Disable the ambient `SENSE` hint block. |
| `MCP_SCREEN_GUARD_PX=40` | Threshold for the user-takeover guard (live pointer vs last-commanded). |
| `MCP_SCREEN_CPU_THREADS=6` | ONNX intra-op thread count for OmniParser. |
| `MCP_SCREEN_MAX_EDGE=2576` | Screenshot downscale target (long edge). |
| `MCP_SCREEN_NO_FRESH=1` | Disable forced fresh-frame capture on static monitors. |
| `MCP_SCREEN_FOCUS_SETTLE_MS=150` | Delay after `screen_focus` activates a window. |
| `MCP_SCREEN_NO_NUDGE=1` | Disable the pointer damage-nudge that primes a static monitor's frame. |

| Path | What |
|---|---|
| `~/.config/mcp-screen/token` | Portal restore token (one-time consent). |
| `~/.local/share/mcp-screen/world/map.db` | World-model cache (per-screen learned elements). |
| `~/.local/share/mcp-screen/sessions/<sid>/` | Recorder trajectories, frames, `replay.html`. |
| `~/.local/state/mcp-screen/actions.jsonl` | Reliability audit log (one JSON line per action). |
| `/tmp/screen_err.txt` | Last unhandled tool traceback (dev diagnostic). |

</details>

## Development

```bash
pytest -q          # tests run without a live D-Bus (conftest stubs)
```

Edit a `.py`, then call `screen_reload` in the running session to re-exec the
server in place while preserving the MCP connection. On any tool exception the
dispatcher writes the full traceback to `/tmp/screen_err.txt`; read it when
debugging crashes.

## Contributing

Issues and pull requests are welcome. Please run `pytest -q` before opening a PR.
See [CLAUDE.md](CLAUDE.md) for architecture notes and the hard-won ops details
behind the capture and input layers.

## License

[FSL-1.1-ALv2](LICENSE.md) © 2026 [88plug](https://github.com/88plug). The
Functional Source License converts to Apache 2.0 two years after each release.
