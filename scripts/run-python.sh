#!/usr/bin/env bash
# Resolve a usable Python ≥3.10 and exec it with the given args.
#
# Claude Code's MCP/hook spawn PATH is thin (often no Homebrew/pyenv). Never
# put bare "python3" in plugin.json / hooks.json — route every Python invoke
# through this script.
#
# MCP-safe: diagnostics go to stderr only; stdout is reserved for the child.
#
# Override (first match, version-gated):
#   EIGHTYEIGHT_PYTHON  fleet-wide
#   PLUGIN_PYTHON       generic
#   SCREEN_PYTHON           this plugin
#
# Also honors VIRTUAL_ENV and a plugin-local .venv.
set -euo pipefail

MIN_MAJOR=3
MIN_MINOR=10

_version_ok() {
  local py="$1"
  [ -n "$py" ] && [ -x "$py" ] || return 1
  "$py" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_MAJOR}, ${MIN_MINOR}) else 1)" 2>/dev/null
}

# REQUIRE_GI gates the first selection pass. PyGObject cannot be pip-installed (no wheel),
# so it exists only in whatever interpreter the distro installed it for — usually
# /usr/bin/python3. A pyenv/conda/`python:3.12-slim` interpreter earlier on PATH will pass
# the version check and then die on `import gi`, which reads to the user as "screen-mcp is
# broken" rather than "wrong interpreter". Verified in a clean container: apt-installed
# python3-gi was invisible to the image's /usr/local python. So: prefer an interpreter that
# can import gi; fall back to any valid one (and warn) only if none can.
REQUIRE_GI=1

# find_spec again: probing several candidates x a real `import gi` (which pulls in
# GObject) dominated startup. Locating the module is the question being asked.
_gi_ok() {
  [ "$REQUIRE_GI" != "1" ] || \
    "$1" -c 'import importlib.util as u,sys; sys.exit(0 if u.find_spec("gi") else 1)' >/dev/null 2>&1
}

_try() {
  local cand="$1"
  [ -n "$cand" ] || return 1
  if [ -x "$cand" ] && _version_ok "$cand" && _gi_ok "$cand"; then
    printf '%s' "$cand"
    return 0
  fi
  if command -v "$cand" >/dev/null 2>&1; then
    local resolved
    resolved="$(command -v "$cand")"
    if _version_ok "$resolved" && _gi_ok "$resolved"; then
      printf '%s' "$resolved"
      return 0
    fi
  fi
  return 1
}

find_python() {
  local c root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

  for c in "${EIGHTYEIGHT_PYTHON:-}" "${PLUGIN_PYTHON:-}" "${SCREEN_PYTHON:-}"; do
    _try "$c" && return 0
  done

  if [ -n "${VIRTUAL_ENV:-}" ]; then
    for c in "${VIRTUAL_ENV}/bin/python3" "${VIRTUAL_ENV}/bin/python"; do
      _try "$c" && return 0
    done
  fi

  for c in "${root}/.venv/bin/python" "${root}/.venv/bin/python3" \
           "${root}/venv/bin/python" "${root}/venv/bin/python3"; do
    _try "$c" && return 0
  done

  for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    _try "$c" && return 0
  done

  for c in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3 \
    "${HOME}/.local/bin/python3" \
    /usr/bin/python
  do
    _try "$c" && return 0
  done

  return 1
}

PY="$(find_python)" || {
  # Nothing with gi. Retry ignoring it so the user gets a precise diagnosis from a running
  # server instead of a dead one — the system-layer hint below names the exact command.
  REQUIRE_GI=0
  PY="$(find_python)"
}
[ -n "${PY:-}" ] || {
  echo "screen-mcp: no Python >=${MIN_MAJOR}.${MIN_MINOR} found." >&2
  echo "  Set EIGHTYEIGHT_PYTHON=/path/to/python3 (or SCREEN_PYTHON), or install Python 3." >&2
  echo "  Checked: env overrides, VIRTUAL_ENV, plugin .venv, PATH, Homebrew, /usr/bin." >&2
  exit 1
}

# --- auto-provision the pip layer -------------------------------------------------------
# Users should not have to hand-install numpy/Pillow to get a working server. If the
# resolved interpreter cannot import the hard deps, build a plugin-local venv and install
# them, then use it.
#
# `--system-site-packages` is LOAD-BEARING: PyGObject (`gi`), the GStreamer typelibs and
# the PipeWire binding are SYSTEM packages that cannot be pip-installed — they bind to the
# host's introspection typelibs and the running compositor. A plain `python -m venv` hides
# them and the server dies with "No module named 'gi'" (verified). Never drop the flag.
#
# Opt out: MCP_SCREEN_NO_AUTO_DEPS=1. Heavy optional set: MCP_SCREEN_AUTO_DEPS=full.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# find_spec, not import: this runs on EVERY server start, and actually importing
# onnxruntime + cv2 costs ~3s of pure startup latency. Locating the modules answers the
# only question we have here — are they installed — without executing them.
_have_hard_deps() {
  "$1" -c 'import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ("numpy","PIL")) else 1)' >/dev/null 2>&1
}
_have_full_deps() {
  "$1" -c 'import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ("numpy","PIL","cv2","onnxruntime","rapidocr")) else 1)' >/dev/null 2>&1
}

# The system layer (PyGObject + GStreamer typelibs + PipeWire) is the ONE part that cannot
# ship with us. Measured 2026-07-25: PyGObject has no wheel — pip builds it from source and
# needs a C toolchain plus libgirepository-dev — and even when built it resolves typelibs and
# a pipewiresrc that must match the RUNNING compositor. So detect it and print the exact
# command for this distro instead of a generic "install the deps" that users cannot action.
_system_layer_hint() {
  if command -v pacman >/dev/null 2>&1; then
    echo "  sudo pacman -S --needed python-gobject gobject-introspection gstreamer \\" >&2
    echo "                 gst-plugins-base gst-plugin-pipewire pipewire \\" >&2
    echo "                 xdg-desktop-portal-gnome wl-clipboard" >&2
  elif command -v apt-get >/dev/null 2>&1; then
    echo "  sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \\" >&2
    echo "                   gstreamer1.0-pipewire pipewire xdg-desktop-portal-gnome \\" >&2
    echo "                   wl-clipboard" >&2
  elif command -v dnf >/dev/null 2>&1; then
    echo "  sudo dnf install python3-gobject gstreamer1-plugins-base \\" >&2
    echo "                   gstreamer1-plugin-pipewire pipewire \\" >&2
    echo "                   xdg-desktop-portal-gnome wl-clipboard" >&2
  else
    echo "  install: PyGObject, GStreamer (+ base plugins + pipewire plugin)," >&2
    echo "           PipeWire, xdg-desktop-portal-gnome, wl-clipboard" >&2
  fi
}

if [ "${MCP_SCREEN_NO_AUTO_DEPS:-0}" != "1" ]; then
  VENV="${ROOT}/.venv"
  MODE="${MCP_SCREEN_AUTO_DEPS:-full}"
  # Ship everything in-box by default: a server that starts but silently has no OCR or icon
  # detection reads as a broken tool, not a missing extra. Size is not the constraint.
  if [ "$MODE" = "core" ]; then
    REQ="${ROOT}/requirements-core.txt"
    _satisfied() { _have_hard_deps "$1"; }
  else
    REQ="${ROOT}/requirements-runtime.txt"
    _satisfied() { _have_full_deps "$1"; }
  fi
  STAMP="${ROOT}/.provisioned-${MODE}"

  # `pip install --target` + PYTHONPATH, used when venv is unavailable. Debian/Ubuntu ship
  # the stdlib `venv` module in a SEPARATE python3-venv package, so the distro interpreter
  # — the very one that has gi — often cannot create a venv (measured in a clean container:
  # "apt install python3.13-venv"). --target needs no venv module and layers our pip deps
  # on top of the distro python that already provides gi, which is exactly what we want.
  DEPS="${ROOT}/.deps"
  # Find any interpreter that HAS pip. Debian strips pip AND ensurepip from the distro
  # python, so the gi-capable interpreter frequently cannot install anything itself, while
  # some other python on the box can. pip's `--python` installs FOR a named interpreter with
  # that interpreter's ABI, so a cp312 pip can correctly populate a cp313 target.
  _pip_helper() {
    local c
    for c in "$PY" python3 python /usr/local/bin/python3 /usr/bin/python3; do
      command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
      "$c" -m pip --version >/dev/null 2>&1 && { printf '%s' "$c"; return 0; }
    done
    return 1
  }

  _provision_target() {
    local req="$1" helper
    "$PY" -m pip --version >/dev/null 2>&1 \
      || "$PY" -m ensurepip --default-pip >/dev/null 2>&1 || true
    if "$PY" -m pip --version >/dev/null 2>&1; then
      "$PY" -m pip install --quiet --target "$DEPS" -r "$req" >&2 && return 0
    fi
    helper="$(_pip_helper)" || return 1
    # `--python` MUST come before the subcommand (pip rejects it after).
    "$helper" -m pip --python "$PY" install --quiet --target "$DEPS" -r "$req" >&2
  }

  # rapidocr depends on opencv-python (NOT the headless build), which drags in libGL and
  # makes `import cv2` fail with "libGL.so.1: cannot open shared object file" on any
  # headless/server box. Both distributions install the same `cv2` package, so reinstalling
  # headless LAST overwrites the GL-linked binary with the headless one. Measured: this is
  # the difference between cv2 importing and not in a clean container.
  _force_headless_cv2() {
    local helper target_flag="$1"
    "$PY" -c 'import cv2' >/dev/null 2>&1 && return 0
    helper="$(_pip_helper)" || return 1
    if [ "$target_flag" = "target" ]; then
      "$helper" -m pip --python "$PY" install --quiet --upgrade --force-reinstall \
        --no-deps --target "$DEPS" opencv-python-headless >/dev/null 2>&1
    else
      "${VENV}/bin/python" -m pip install --quiet --upgrade --force-reinstall \
        --no-deps opencv-python-headless >/dev/null 2>&1
    fi
  }
  _use_target_if_ready() {
    [ -d "$DEPS" ] || return 1
    PYTHONPATH="${DEPS}${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c 'import numpy, PIL' >/dev/null 2>&1
  }

  if ! _satisfied "$PY" && [ ! -f "$STAMP" ] && [ -f "$REQ" ]; then
    echo "screen-mcp: provisioning Python deps (one time, ~200MB) ..." >&2
    # --system-site-packages is LOAD-BEARING: gi/GStreamer are system packages a plain
    # venv would hide, and the server dies with "No module named 'gi'" (verified).
    if ! { [ -d "$VENV" ] || "$PY" -m venv --system-site-packages "$VENV" >/dev/null 2>&1; }; then
      # A FAILED `venv` still leaves a partial tree behind (it dies at the ensurepip step
      # after writing bin/python). That stub does NOT inherit system site-packages, so it
      # hides gi — and being at $ROOT/.venv it then WINS interpreter selection, turning a
      # recoverable miss into "No module named 'gi'". Remove it. Guarded: exact literal
      # path under ROOT, created by this script, never a variable-built path.
      if [ -n "${ROOT:-}" ] && [ "$VENV" = "${ROOT}/.venv" ] && [ -d "$VENV" ]; then
        rm -rf "${ROOT}/.venv"
      fi
      # No venv module. Layer deps onto the current (gi-capable) interpreter instead.
      echo "screen-mcp: venv unavailable; installing into ${DEPS} instead ..." >&2
      _provision_target "$REQ" || _provision_target "${ROOT}/requirements-core.txt" || {
        echo "screen-mcp: dep provisioning FAILED (offline, or no pip)." >&2
        echo "  ${PY} -m pip install --target ${DEPS} -r ${REQ}" >&2
      }
      if _use_target_if_ready; then
        : > "$STAMP"
        export PYTHONPATH="${DEPS}${PYTHONPATH:+:$PYTHONPATH}"
        _force_headless_cv2 target
        echo "screen-mcp: provisioned ok (--target)." >&2
      fi
    elif true; then
      "${VENV}/bin/python" -m pip install --quiet --upgrade pip >&2 2>/dev/null
      if "${VENV}/bin/python" -m pip install --quiet -r "$REQ" >&2; then
        : > "$STAMP"
        # evdev separately and best-effort: it builds against kernel headers and its
        # failure must never sink the grounding stack (it did — measured). Prefer the
        # prebuilt wheel, fall back to source, shrug if neither works.
        if [ "$MODE" != "core" ] && ! "${VENV}/bin/python" -c 'import evdev' >/dev/null 2>&1; then
          "${VENV}/bin/python" -m pip install --quiet evdev-binary >/dev/null 2>&1 \
            || "${VENV}/bin/python" -m pip install --quiet evdev >/dev/null 2>&1 \
            || echo "screen-mcp: evdev unavailable (uinput backend off; portal input still works)." >&2
        fi
        _force_headless_cv2 venv
        echo "screen-mcp: provisioned ok." >&2
      elif "${VENV}/bin/python" -m pip install --quiet -r "${ROOT}/requirements-core.txt" >&2; then
        # Degrade rather than die: core gets a working server without grounding.
        echo "screen-mcp: full set failed; core deps installed (no OCR/icon grounding)." >&2
        echo "  retry later: ${VENV}/bin/python -m pip install -r ${REQ}" >&2
      else
        echo "screen-mcp: dep provisioning FAILED (offline?). Install manually:" >&2
        echo "  ${PY} -m pip install -r ${REQ}" >&2
      fi
    fi
  fi

  # Adopt the venv only if it ALSO kept gi — a venv built without --system-site-packages
  # (or a partial one) satisfies numpy while silently losing PyGObject, which is a strictly
  # worse interpreter than the one we already hold.
  if _have_hard_deps "${VENV}/bin/python" 2>/dev/null \
     && { [ "$REQUIRE_GI" != "1" ] || "${VENV}/bin/python" -c 'import gi' >/dev/null 2>&1; }; then
    PY="${VENV}/bin/python"
  elif _use_target_if_ready; then
    # Keep the gi-capable interpreter and bring our deps in via PYTHONPATH.
    export PYTHONPATH="${DEPS}${PYTHONPATH:+:$PYTHONPATH}"
  fi
fi

# Last gate: the un-shippable system layer. Report it precisely rather than letting the
# server die later on an opaque ImportError deep in capture.py.
if ! "$PY" -c 'import gi' >/dev/null 2>&1; then
  echo "screen-mcp: PyGObject (gi) missing — this is the one layer we cannot bundle" >&2
  echo "  (no PyPI wheel; it binds the host's typelibs and running PipeWire). Install:" >&2
  _system_layer_hint
fi

exec "$PY" "$@"
