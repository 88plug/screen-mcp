"""autoloop.py — server-side self-adaptive micro-loops for mcp-screen (v1.2).

These are deterministic loops that need NO model in the loop, so any consuming CLI gets
them for free and they cost zero LLM round-trips. The flagship is `scroll_to_reveal`:
instead of the agent doing N rounds of (scroll -> screenshot -> read) to see a long page,
this scrolls until the SENSE layer reports no new content, accumulating every interactable
it discovers, and returns the whole survey in ONE call. The takeover guard is re-checked
every iteration, so the user grabbing the mouse stops it immediately (partial results kept).

Imports the sibling modules directly (capture/input/grounding/sense/state) — none import
this, so no cycle. Fail-open: errors degrade to whatever was gathered so far."""
import time

import state
import capture
import input as inp
import grounding
import sense


def _txt(s):
    return {"type": "text", "text": s}


def scroll_to_reveal(args):
    """Read a whole scrollable view in one call. Scrolls down until content stops moving
    (or max_pages), annotating each screen and accumulating unique interactables.

    args: region [x,y,w,h] (default last view), max_pages (12), amount (per-scroll notches,
    10), force (bypass guard), settle_ms (250). Returns the discovered element inventory +
    a final screenshot; leaves SESSION['elements'] set to the LAST screen so click-by-id
    works on what's currently visible."""
    t0 = time.time()
    region = args.get("region")
    if region is None and state.SESSION.get("view"):
        v = state.SESSION["view"]; region = [v["ox"], v["oy"], v["dw"], v["dh"]]
    max_pages = int(args.get("max_pages", 12))
    amount = int(args.get("amount", 10))
    settle = float(args.get("settle_ms", 250)) / 1000.0
    force = args.get("force")

    cx = (region[0] + region[2] // 2) if region else None
    cy = (region[1] + region[3] // 2) if region else None

    seen = {}          # (role, norm_label) -> {"role","label","count"}
    pages = 0
    reached_end = False
    prev_small = None
    last_img = last_ox = last_oy = None
    last_store = {}
    stopped = None

    for i in range(max_pages):
        try:
            inp.guard_user(force)
        except inp.UserControlError as e:
            stopped = str(e); break

        img, ox, oy = capture.capture_desktop(region)
        last_img, last_ox, last_oy = img, ox, oy
        cur_small = sense.downsample(img)

        # On the 2nd+ screen, stop if the last scroll didn't move content (end of page).
        if prev_small is not None:
            if not sense.scroll_from_pair(prev_small, cur_small)["moved"]:
                reached_end = True
                break

        # Annotate this screen, fold its interactables into the running inventory.
        try:
            _marked, els = grounding.annotate(img)
            store = {}
            for e in els:
                x1, y1, x2, y2 = e["bbox"]
                store[e["id"]] = {"x": ox + (x1 + x2) // 2, "y": oy + (y1 + y2) // 2,
                                  "label": e["label"], "role": e["role"]}
                k = (e.get("role", ""), " ".join((e.get("label") or "").lower().split()))
                rec = seen.get(k)
                if rec:
                    rec["count"] += 1
                else:
                    seen[k] = {"role": e.get("role", ""), "label": e.get("label", ""), "count": 1}
            last_store = store
        except Exception:
            pass

        prev_small = cur_small
        pages += 1

        # Scroll down for the next screen.
        if cx is not None:
            inp.scroll({"x": cx, "y": cy, "space": "desktop", "direction": "down", "amount": amount})
        else:
            inp.scroll({"direction": "down", "amount": amount})
        if settle > 0:
            time.sleep(settle)

    # Current screen's elements remain clickable by id (not tied to a world-model row).
    if last_store:
        state.SESSION["elements"] = last_store
        state.SESSION["elements_state_id"] = None

    # Build the report.
    items = sorted(seen.values(), key=lambda r: (r["role"], r["label"]))
    lines = [f"  {r['role']}: {r['label']!r}" + (f" x{r['count']}" if r["count"] > 1 else "")
             for r in items if r["label"]]
    head = (f"READ_PAGE: {pages} screen(s) scrolled, reached_end={reached_end}, "
            f"{len(items)} unique interactables, {int((time.time() - t0) * 1000)}ms total.")
    if stopped:
        head += f"  STOPPED (user takeover): {stopped}"
    body = "discovered interactables (current screen is clickable by [id]; scroll back to reach off-screen ones):\n" + "\n".join(lines)
    content = [_txt(head), _txt(body)]

    # Final screenshot of where we ended up (with cursor marker), so the agent sees the state.
    if last_img is not None:
        try:
            capture.cursor_pos(); shot_img = capture.draw_cursor(last_img, last_ox, last_oy)
        except Exception:
            shot_img = last_img
        content += capture.encode_store(shot_img, last_ox, last_oy, "read_page", time.time())
    return {"content": content, "isError": bool(stopped)}
