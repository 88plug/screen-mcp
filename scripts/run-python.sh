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

_try() {
  local cand="$1"
  [ -n "$cand" ] || return 1
  if [ -x "$cand" ] && _version_ok "$cand"; then
    printf '%s' "$cand"
    return 0
  fi
  if command -v "$cand" >/dev/null 2>&1; then
    local resolved
    resolved="$(command -v "$cand")"
    if _version_ok "$resolved"; then
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

_have_hard_deps() { "$1" -c 'import numpy, PIL' >/dev/null 2>&1; }

if [ "${MCP_SCREEN_NO_AUTO_DEPS:-0}" != "1" ] && ! _have_hard_deps "$PY"; then
  REQ="${ROOT}/requirements-core.txt"
  [ "${MCP_SCREEN_AUTO_DEPS:-}" = "full" ] && REQ="${ROOT}/requirements.txt"
  VENV="${ROOT}/.venv"
  STAMP="${VENV}/.provisioned"

  if [ ! -f "$STAMP" ] && [ -f "$REQ" ]; then
    echo "screen-mcp: missing Python deps; provisioning ${VENV} ..." >&2
    if "$PY" -m venv --system-site-packages "$VENV" >&2 \
       && "${VENV}/bin/python" -m pip install --quiet --upgrade pip >&2 \
       && "${VENV}/bin/python" -m pip install --quiet -r "$REQ" >&2; then
      : > "$STAMP"
      echo "screen-mcp: provisioned ok." >&2
    else
      # Fail open: a degraded server that reports a clear error beats no server.
      echo "screen-mcp: provisioning FAILED (offline?). Install manually:" >&2
      echo "  ${PY} -m pip install -r ${REQ}" >&2
    fi
  fi

  if _have_hard_deps "${VENV}/bin/python" 2>/dev/null; then
    PY="${VENV}/bin/python"
  fi
fi

exec "$PY" "$@"
