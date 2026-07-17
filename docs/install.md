# Install & prerequisites

screen-mcp targets a specific stack. It will not run on macOS, Windows, X11-only desktops, or non-GNOME Wayland compositors in the supported configuration.

## Requirements

!!! important
    Core capture and input need **Linux + Wayland + GNOME**, Python 3.10+, GStreamer ≥ 1.28, PipeWire, and `xdg-desktop-portal-gnome`.

| Layer | Need |
|---|---|
| OS / session | Linux, Wayland, GNOME Shell |
| Python | 3.10+ (tested on 3.14) |
| Capture | GStreamer ≥ 1.28 (`leaky-type`; older `drop=` removed in 1.28), PipeWire, portal ScreenCast |
| Input | portal RemoteDesktop; optional `/dev/uinput` + `evdev` for the kernel backend |
| Clipboard | `wl-clipboard` for Unicode paste in `screen_type` |
| Fonts | DejaVu Sans Bold for Set-of-Marks labels (falls back to PIL default) |
| Grounding (optional) | RapidOCR + onnxruntime + OpenCV; OmniParser ONNX at `models/onnx/model.onnx` |

Grounding is **CPU-only by design**. The server hard-disables the GPU (`CUDA_VISIBLE_DEVICES=""`) for predictable latency and no driver flake.

## Plugin install (recommended)

```text
/plugin marketplace add 88plug/screen-mcp
/plugin install screen-mcp@screen-mcp
```

The plugin manifest cannot install system packages or a venv for you. Run the Python deps once in the installed plugin directory (or a clone):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Or use the helper:

```bash
./setup.sh
```

## Manual MCP setup

Wire the server in `~/.claude.json` under `mcpServers`:

```json
{
  "screen": {
    "command": "python3",
    "args": ["/path/to/screen-mcp/server.py"]
  }
}
```

Prefer the project venv's Python if system packages are incomplete:

```json
{
  "screen": {
    "command": "/path/to/screen-mcp/.venv/bin/python",
    "args": ["/path/to/screen-mcp/server.py"]
  }
}
```

## System packages

Install system deps **before** the Python deps. Full one-liners also live in [requirements.txt](https://github.com/88plug/screen-mcp/blob/main/requirements.txt).

=== "Arch / Manjaro"

    ```bash
    sudo pacman -S python-gobject gobject-introspection \
                   gstreamer gst-plugins-base gst-plugins-good gst-libav \
                   pipewire pipewire-pulse xdg-desktop-portal-gnome \
                   wl-clipboard ttf-dejavu
    ```

=== "Debian / Ubuntu (names vary)"

    ```bash
    # Adjust package names for your release; GStreamer must be ≥ 1.28
    sudo apt install python3-gi gir1.2-gstreamer-1.0 \
                     gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
                     gstreamer1.0-libav pipewire xdg-desktop-portal-gnome \
                     wl-clipboard fonts-dejavu
    ```

Then:

```bash
pip install -r requirements.txt
```

## GNOME Shell extension (recommended)

The bundled `window-info@local` extension gives reliable focused-window / window-list data and lets `screen_focus` activate windows via Mutter. Installing a new extension needs a **one-time Wayland re-login**.

```bash
gnome-shell-extension/window-info@local/install.sh
gnome-extensions enable window-info@local
```

Without it:

- Awareness degrades (AT-SPI covers some GTK apps).
- `screen_focus` falls back to the GNOME overview search.
- Click-to-focus still works everywhere: click into the target window, then type.

## Kernel input backend (optional)

When `uinput` is available, clicks / keys / scroll go through a kernel-level unified pointer device (exact landing, better Electron scroll). Needs:

1. User in group `input` (and often a udev rule so `/dev/uinput` is writable).
2. Python package `evdev`.

Opt out: `MCP_SCREEN_NO_UINPUT=1`. Portal input remains the fallback.

## First-run portal consent

1. Start the MCP server from your client.
2. Call `screen_screenshot` (or any capture tool).
3. Approve the ScreenCast / RemoteDesktop portal dialog for the monitor(s) you want.
4. Restore token is written under `~/.config/mcp-screen` — later sessions stay silent until the token expires or is revoked.

## Verify with `screen_diag`

```text
Call screen_diag and summarize anything not status=ok.
```

`screen_diag` returns a prereqs matrix (portal, window-info, uinput, GStreamer, …) with `next_step` hints, plus session geometry, cursor/guard state, and grounding backends. Use it first when capture, clicks, or the cursor guard misbehave.

| Status | Meaning |
|---|---|
| `ok` | Ready |
| `warn` | Optional missing or degraded; fallbacks exist |
| `fail` | Required for core capture/input on this stack |

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| No frames / black shot | Portal not shared, monitor DPMS sleep, or cold pipeline | Re-consent portal; wake monitor; `screen_screenshot(regeo=true)` |
| "I typed but nothing happened" | Wrong keyboard focus | `screen_click` into the window, or `screen_focus` / `focus=` on the type call |
| Clicks miss by a mile | Stale view coords | Click from the **latest** screenshot, or pass `view_id` / `space=desktop` / `element=` |
| `STOPPED: …` | Human moved the mouse | Re-issue with `force=true` only after the human hands control back |
| GStreamer fail at import | Version &lt; 1.28 or missing plugins | Upgrade GStreamer; install base/good/libav plugins |

See also [Tool loop](tool-loop.md) and [Guards](guards.md).
