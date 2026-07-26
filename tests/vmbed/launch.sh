#!/usr/bin/env bash
# Two local QEMU desktop VMs — one GNOME/Wayland, one GNOME/X11 — as a formal test bed for
# screen-mcp's two capture backends. No second physical machine required.
#
# WHY THIS EXISTS
#   The X11 fallback (x11capture.py) was written against measurements taken on a borrowed
#   host (Zorin 15.3 over tailscale) that then became unreachable mid-session, so the
#   assembled path was never run end-to-end. A borrowed box is not a test bed. This gives a
#   reproducible one on this machine.
#
# WHY ONE ISO FOR BOTH
#   A Manjaro GNOME ISO ships BOTH sessions: GNOME on Wayland (default) and GNOME on Xorg
#   (session picker on the login screen). One download covers both backends, and it is the
#   same distro family as the dev host, so the runtime stack (Python 3.13+, GStreamer 1.28,
#   PipeWire, xdg-desktop-portal-gnome) matches what screen-mcp expects instead of
#   introducing a second variable.
#
# WHY A QMP SOCKET TOO
#   QMP is how the harness actually drives these VMs: `screendump` for pixels and
#   `input-send-event` for absolute pointer + key events, over a plain unix socket with
#   stdlib json. No VNC client library, so no dependency to install — vncdotool needed
#   service_identity, which PEP 668 blocks from pip on this distro. VNC stays exposed for a
#   human to attach a viewer.
#
# WHY VNC RATHER THAN SPICE
#   VNC is compiled into QEMU unconditionally; SPICE needs qemu-system-modules-spice, which
#   was absent when this stack was first explored. VNC is also what screen-in-docker's rfb
#   driver speaks, so the same MCP that drives GUI containers can drive these VMs.
#
# WHY usb-tablet AND NOT THE DEFAULT MOUSE
#   The default QEMU mouse is a RELATIVE PS/2 device. Over VNC that makes the guest pointer
#   drift away from the coordinates the client sent, which would corrupt exactly the
#   click-accuracy behaviour we are here to verify. usb-tablet is an ABSOLUTE pointing
#   device, so a VNC click at (x,y) lands at (x,y) in the guest.
#
# Usage:
#   ./launch.sh wayland     # boot the Wayland VM   (VNC :1 -> 5901, ssh -> 22201)
#   ./launch.sh x11         # boot the X11 VM       (VNC :2 -> 5902, ssh -> 22202)
#   ./launch.sh both
#   ./launch.sh status | stop
set -euo pipefail

ISO="${SMCP_VM_ISO:-}"
STATE="${SMCP_VM_STATE:-$HOME/.cache/screen-mcp-vmbed}"
RAM="${SMCP_VM_RAM:-4G}"
CPUS="${SMCP_VM_CPUS:-4}"
DISK_GB="${SMCP_VM_DISK_GB:-24}"

mkdir -p "$STATE"

_pick_iso() {
  [ -n "$ISO" ] && { printf '%s' "$ISO"; return 0; }
  # Newest Manjaro GNOME ISO already on the box. Sorted so 26.x wins over 25.x.
  local c
  c="$(find "$HOME/Downloads" -maxdepth 1 -name 'manjaro-gnome-*.iso' 2>/dev/null | sort -V | tail -1)"
  [ -n "$c" ] && { printf '%s' "$c"; return 0; }
  return 1
}

_qemu() {  # _qemu <name> <vncdisplay> <sshport> [extra...]
  local name="$1" vnc="$2" ssh="$3"; shift 3
  local iso disk pidf
  iso="$(_pick_iso)" || { echo "no Manjaro GNOME ISO found; set SMCP_VM_ISO" >&2; exit 1; }
  disk="$STATE/$name.qcow2"
  pidf="$STATE/$name.pid"

  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    echo "  $name already running (pid $(cat "$pidf"))"
    return 0
  fi
  # Overlay so a run never mutates the ISO and a reset is one rm of a file we made.
  [ -f "$disk" ] || qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null

  # -vga std, NOT virtio: with virtio-gpu the guest stops driving console 0 once GNOME
  #   takes over, and QMP `screendump` then returns a framebuffer reading
  #   "Display output is not active" (observed: splash captured fine, desktop did not,
  #   query-display-options reported type=none). std VGA keeps a scanout QEMU can dump for
  #   the whole boot, which is the property this harness depends on.
  # -device usb-tablet: absolute pointer — see the header note.
  # hostfwd 22: so the suite can be driven over ssh once the guest is up, instead of
  #   scripting a GUI installer over VNC.
  qemu-system-x86_64 \
    -name "$name" \
    -enable-kvm -cpu host -smp "$CPUS" -m "$RAM" \
    -machine q35 \
    -drive file="$disk",if=virtio,cache=writeback \
    -cdrom "$iso" \
    -boot d \
    -vga std \
    -usb -device usb-tablet -device usb-kbd \
    -netdev "user,id=n0,hostfwd=tcp::${ssh}-:22" -device virtio-net-pci,netdev=n0 \
    -vnc ":${vnc},share=ignore" \
    -qmp "unix:${STATE}/${name}.qmp,server=on,wait=off" \
    -pidfile "$pidf" \
    -daemonize \
    "$@"
  echo "  $name up — VNC 590${vnc} (display :${vnc}), ssh localhost:${ssh}"
}

case "${1:-status}" in
  wayland)
    # Manjaro GNOME boots Wayland by default; nothing to force.
    _qemu smcp-wayland 1 22201
    ;;
  x11)
    # X11 is chosen at the GNOME login screen (gear -> "GNOME on Xorg"). The VM is
    # otherwise identical, which is the point: one variable between the two beds.
    _qemu smcp-x11 2 22202
    ;;
  both)
    "$0" wayland
    "$0" x11
    ;;
  status)
    for n in smcp-wayland smcp-x11; do
      p="$STATE/$n.pid"
      if [ -f "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null; then
        echo "  $n RUNNING pid $(cat "$p")"
      else
        echo "  $n stopped"
      fi
    done
    command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -E ':(5901|5902|22201|22202)' | sed 's/^/    /' || true
    ;;
  stop)
    for n in smcp-wayland smcp-x11; do
      p="$STATE/$n.pid"
      if [ -f "$p" ] && kill -0 "$(cat "$p")" 2>/dev/null; then
        kill "$(cat "$p")" && echo "  stopped $n"
      fi
      rm -f "$p"
    done
    ;;
  *)
    echo "usage: $0 {wayland|x11|both|status|stop}" >&2
    exit 2
    ;;
esac
