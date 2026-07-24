# Tools

Source of truth: `TOOLS` in [`server.py`](https://github.com/88plug/screen-mcp/blob/main/server.py). Titles and descriptions below match the live schemas. Annotation hints: **RO** = read-only, **ACT** = mutates, **DEST** = treated as destructive in the schema.

Shared optional args on position-bearing actions are documented once in [Tool loop → Shared action knobs](tool-loop.md#shared-action-knobs).

## Capture & geometry

### `screen_screenshot` — Capture Screen (RO)

Capture the desktop, lossless, auto-sized to the model's native resolution. Use to **locate** targets (never assume which monitor) or to re-read after an action.

| Arg | Type | Notes |
|---|---|---|
| `region` | `[x,y,w,h]` | Desktop px crop/zoom |
| `monitor` | number | Zoom to monitor index |
| `annotate` | bool | Numbered Set-of-Marks + click coords |
| `use_cache` | bool | With annotate: reuse learned elements (skips OCR) |
| `regeo` | bool | Re-probe geometry / rewarm pipelines |
| `fresh` | bool | Force a **current** frame on a static monitor (default true). `false` / `settle=0` for instantaneous cached frame |

**Returns:** image + text (focused window, `SENSE` line: new elements / modal / no-op).

No args = full multi-monitor overview.

### `screen_list_monitors` — List Monitors (RO)

List monitors (origin, size, scale), desktop bounds, and open windows. Use first when choosing where to screenshot or click.

| Arg | Type |
|---|---|
| `regeo` | bool |

### `screen_wait` — Wait for Settle (RO)

Wait until the screen stops changing (or timeout), then optionally screenshot. Use after async UI instead of guessing a fixed delay. Also usable as `wait_stable` inside `screen_do` / `screen_tour`.

| Arg | Type | Notes |
|---|---|---|
| `timeout` | number | Seconds (default 5) |
| `thresh` | number | Stability threshold |
| `window` | number | Stability window |
| `region` / `monitor` | | Scope settle |
| `shot` | bool | Return screenshot when done |

### `screen_watch` — Watch at 1 fps (human-eye) (RO)

**Default visual confirm for thrashy UIs.** Samples the region/monitor at ~1 fps for several seconds and returns a timeline + verdict (`settled` \| `evolving` \| `jitter` \| `unstable`). Catches continuous animation / force-sim chaos that a single screenshot misses.

| Arg | Type | Notes |
|---|---|---|
| `region` / `monitor` | | What to watch (default last view) |
| `fps` | number | Default **1.0** (clamp 0.2–10) |
| `seconds` | number | Default **6** (clamp 1–60) |
| `annotate` | bool | OCR on first+last only (default false) |
| `shot` | bool | Final frame (default true) |
| `force` | bool | Bypass takeover guard |

**When:** graphs, maps, canvases, connection clouds, loaders, animated dashboards — or any time a human would stare for a second before saying “looks fine.”

**vs `screen_wait`:** wait returns when *stable once*; watch scores *sustained* motion over a window (jitter = fail visual QA).

## Pointer & keyboard

### `screen_move_mouse` — Move Mouse (ACT)

Move mouse to `x,y` (view-space default) or `dx,dy` relative. Use before click when you need an explicit hover position.

### `screen_click` — Click (ACT)

Click at `x,y` (view-space; mapped to the real screen). Omit `x,y` to click in place.

| Arg | Type | Notes |
|---|---|---|
| `x`, `y` | number | Target (view space default) |
| `element` | number | Id from last annotate |
| `button` | string | `left` \| `right` \| `middle` |
| `double` | bool | Double-click |
| + shared knobs | | `space`, `view_id`, `focus`, `shot`, `verify`, `force`, … |

### `screen_scroll` — Scroll (ACT)

Wheel scroll by direction and amount. Use to reveal off-screen content before screenshot. Optional `x,y` to position first.

| Arg | Type | Notes |
|---|---|---|
| `direction` | string | `up` \| `down` \| `left` \| `right` |
| `amount` | number | Notches |
| `x`, `y` | number | Optional position first |

### `screen_drag` — Drag (ACT)

Press-drag from `(x1,y1)` to `(x2,y2)` in view-space. Use for sliders, reorder, selection.

| Arg | Type | Notes |
|---|---|---|
| `x1`, `y1`, `x2`, `y2` | number | **Required** |
| `button` | string | `left` \| `middle` \| `right` |
| `space`, `view_id`, `shot`, `force`, `region`, `settle` | | |

### `screen_key` — Press Key (ACT)

Press a key or combo: `Ctrl+L`, `Enter`, `Alt+Tab`, `F5`. Keys go to the **focused** window — pass `focus='appname'` first if needed.

| Arg | Type | Notes |
|---|---|---|
| `keys` | string | **Required** |
| `focus` | string | Raise + focus before key |
| `shot`, `verify`, `force`, `region`, `settle` | | |

### `screen_type` — Type Text (ACT)

Type text (Unicode ok via clipboard paste; ASCII via keysyms). `enter=true` presses Enter after. Text goes to the **focused** window.

| Arg | Type | Notes |
|---|---|---|
| `text` | string | **Required** |
| `enter` | bool | Press Enter after |
| `focus` | string | Raise + focus before type |
| `shot`, `verify`, `force`, `region`, `settle` | | |

### `screen_focus` — Focus Window (ACT)

Raise and give **keyboard focus** to a window so injected keys/clicks land in it. Use before `screen_type` / `screen_key` on an app you have not clicked into (the #1 reason typing appears to do nothing).

| Arg | Type | Notes |
|---|---|---|
| `app` | string | e.g. `slack`, `firefox` |
| `title` | string | Title substring |
| `id` | string \| number | Window id from `screen_list_monitors` |

## Multi-step helpers

### `screen_do` — Batch Actions (DEST)

Run an ordered batch of actions in one call to cut round-trips.

| Arg | Type | Notes |
|---|---|---|
| `steps` | array | **Required.** `[{action:'move\|click\|scroll\|drag\|key\|type\|wait\|wait_stable', ...}]` |
| `stop_on_error` | bool | Stop mid-batch on failure |
| `shot` | bool | Final screenshot |
| `force` | bool | Batch-level takeover bypass |
| `region` / `monitor` / `settle` | | |

Stops mid-batch if the human takes the mouse (`force=true` to override). Returns per-step results.

### `screen_read_page` — Read Page (ACT)

Auto-scroll a scrollable view until content stops moving, annotating each screen. Use instead of N rounds of scroll+screenshot. Leaves the current screen clickable by element id.

| Arg | Type | Notes |
|---|---|---|
| `region` | `[x,y,w,h]` | Defaults to last view |
| `max_pages` | number | Cap pages |
| `amount` | number | Scroll notches per step |
| `settle_ms` | number | Settle between pages |
| `force` | bool | Bypass takeover guard |

### `screen_tour` — Tour UI States (DEST)

Visit several UI states in one call; return a labeled thumbnail of each.

| Arg | Type | Notes |
|---|---|---|
| `steps` | array | **Required.** `[{label, steps:[{action:…}], region?, settle?}]` |
| `settle` | number | Default settle between stops |
| `shot_max_edge` | number | Thumbnail long edge (default 1280) |
| `force` | bool | |

## Session, reload, diagnostics

### `screen_session` — Record Session (ACT)

Session recording / replay.

| Arg | Type | Notes |
|---|---|---|
| `op` | string | `start` \| `stop` \| `list` \| `status` \| `replay-path` |
| `id` | string | Session id where applicable |

Trajectories land under `~/.local/share/mcp-screen/sessions/<sid>/` (frames + `replay.html`).

### `screen_reload` — Reload Server (DEST)

Hot-reload this MCP server's own code in place (re-exec, preserving the connection). Use after editing server code so tools update without `/mcp` reconnect.

### `screen_diag` — Diagnostics (RO)

Health dump: prereqs matrix (portal, window-info, uinput, GStreamer, …) with `next_step` hints; session/geo; cursor/guard state; grounding backends. **First tool to call** when capture, clicks, or the cursor guard misbehave.

### `screen_sense` — Cross-Layer Pixel Signal (RO)

Return the normalized change signal from the most recent frame diff — `{changed, opened, modal, no_op, activity}` — so a verifier (os-control-mcp's `os_verify`) can fuse the GUI layer with the OS layer. Call right after a screen action, then pass the `pixel` object to `os_verify` (`action=end`). Catches a GUI that changed while the underlying service did not.

## Tool inventory (quick table)

| Tool | Title | Hint |
|---|---|---|
| `screen_screenshot` | Capture Screen | RO |
| `screen_list_monitors` | List Monitors | RO |
| `screen_move_mouse` | Move Mouse | ACT |
| `screen_click` | Click | ACT |
| `screen_scroll` | Scroll | ACT |
| `screen_drag` | Drag | ACT |
| `screen_key` | Press Key | ACT |
| `screen_type` | Type Text | ACT |
| `screen_focus` | Focus Window | ACT |
| `screen_do` | Batch Actions | DEST |
| `screen_read_page` | Read Page | ACT |
| `screen_tour` | Tour UI States | DEST |
| `screen_wait` | Wait for Settle | RO |
| `screen_watch` | Watch (1 fps human-eye) | RO |
| `screen_session` | Record Session | ACT |
| `screen_reload` | Reload Server | DEST |
| `screen_diag` | Diagnostics | RO |
| `screen_sense` | Cross-Layer Pixel Signal | RO |

Eighteen tools. If this page and a live `tools/list` disagree, trust the running server and open an issue.
