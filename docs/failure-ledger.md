# Failure ledger — 2026-07-25/26

What went wrong in this working block, why, and what the corrected approach is. Kept
because several of these were *shipped* and only caught later by an adversarial review or a
headless run — the pattern matters more than the individual bugs.

## Shipped defects (found after the fact, not before)

| # | Defect | How it escaped | Caught by |
|---|--------|----------------|-----------|
| 1 | Fitted screenshots advertised the PRE-shrink scale, so every view-space click on a capped client missed by the shrink ratio (~5x). `view_id` could not catch it because the id never changed. | Verified the image *arrived*; never verified the coordinate contract still held. | max code review (2 finders independently) |
| 2 | OCR thread cap was **inert in production** — `warmup()` built `RapidOCR()` uncapped and won the double-checked init. My benchmark called `ocr_boxes` first, so it measured a path production never took. | Benchmarked a code path instead of the shipped path. | max code review |
| 3 | `screen_reload` lost a clientInfo-derived output cap (execv keeps env, not state; client never re-sends `initialize`). | Never tested the reload path with a capped client. | max code review |
| 4 | `region` + `monitor` silently returned the whole monitor. | Tested `region` alone, saw it work, dismissed the report. | driving Grok, which isolated the exact condition |
| 5 | `region` + `monitor` was later unclipped — an oversized box read the neighbouring monitor. | Fixed the precedence bug without bounding the result. | max code review |
| 6 | `requirements.txt` named `rapidocr-onnxruntime`, which does not provide the `rapidocr` module the code imports. Anyone installing from it got OCR silently disabled. | Never installed from my own requirements file in a clean env. | 88plug validation pass |
| 7 | CI was RED for four consecutive commits. Tests passed locally (real desktop) and failed headless because conftest substituted a stub `capture`. | Verified locally; never looked at the CI runs. | 88plug validation pass |
| 8 | `Gio.Settings.new()` and `Atspi.init()` **abort()** rather than raise when dconf / the a11y bus is missing — latent server-killers on any headless or minimal-desktop host. | Assumed `try/except` was sufficient for FFI calls. | luck (a headless test run dumped core) |
| 9 | Cap applied per-image, not per tool RESULT — a multi-stop `screen_tour` shipped N x the client limit. | Modelled the limit at the wrong granularity. | max code review |
| 10 | Overwrote `requirements.txt` with `cat >` without reading it, destroying 41 lines of system-dependency documentation. | Did not read before overwriting. | `git diff` after the fact |

## Process failures

- **Verified the wrong layer.** Repeatedly confirmed "the call succeeded" rather than "the
  contract still holds" (#1, #2, #3). A tool call returning 200 is not evidence the
  coordinate space, the shipped path, or the reload path is intact.
- **Local-green treated as green.** Four red-CI commits (#7). This box has a full desktop
  stack; CI does not. Green here proves nothing about there.
- **Dismissed a correct external report.** Grok reported "region didn't stick"; I tested the
  wrong variant, concluded it was the client's fault, and moved on (#4).
- **Asserted an inherited limit.** `prereqs` hard-failed X11 with "targets GNOME on Wayland
  only" — never measured, and wrong about the actual blocker.
- **Claimed a number from the wrong path.** Reported "85s -> 40.6s live, 2.1x" for a fix
  that was inert in production; the improvement was largely load variance (#2).
- **Chased a dead end before checking what was already there.** Reached for a VNC client
  library (blocked by PEP 668) when QEMU's own QMP socket did screendump + input with the
  stdlib.
- **Wrote a poll loop whose heredoc never exported its variable** and let it spin for ten
  minutes doing nothing.

## Still unverified

- The X11 capture backend has **never run end-to-end on a real X11 session**. The borrowed
  host was too old (no portal, GStreamer 1.14, Python 3.8) and became unreachable; the local
  VM bed does not yet reach a desktop.
- AT-SPI was measured only against `gnome-calculator`, not a real application mix.

## Corrective principles

1. Test the SHIPPED path, not a path that reaches the same function.
2. Verify the CONTRACT (coordinates, freshness, identity), not just that a call returned.
3. Headless/CI is a different machine — run the suite the way CI runs it before pushing.
4. An external bug report is evidence; reproduce the exact stated condition before dismissing.
5. FFI/GLib calls may `abort()`; probe preconditions instead of relying on `try/except`.
6. Prefer the mechanism already installed over a new dependency.
