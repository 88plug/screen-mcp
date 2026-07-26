# vmbed — local QEMU test bed for the two capture backends

Two VMs from **one** Manjaro GNOME ISO: `smcp-wayland` (GNOME/Wayland, the default session)
and `smcp-x11` (GNOME/Xorg, chosen at the login screen). One variable between them, so a
behavioural difference is attributable to the display server and nothing else.

This exists because `x11capture.py` was written against measurements taken on a borrowed
host (Zorin 15.3 over tailscale) that became unreachable mid-session, so the assembled X11
path was never executed end-to-end. A borrowed box is not a test bed.

## Use

    ./launch.sh both        # boot both   (VNC 5901/5902, ssh 22201/22202)
    ./launch.sh status
    ./vmctl.py shot wayland /tmp/x.png
    ./vmctl.py click x11 640 400
    ./vmctl.py key wayland ret
    ./launch.sh stop

## Design notes

**QMP, not a VNC client.** `vmctl.py` speaks QEMU's QMP over a unix socket with nothing but
the stdlib: `screendump` for pixels, `input-send-event` for absolute pointer and keys. The
obvious route was `vncdotool` (already installed) but it needs `service_identity`, and PEP
668 blocks pip from the system Python here — a harness that cannot install on the machine it
tests is not a harness. VNC stays exposed for a human viewer.

**`-vga std`, not virtio.** With virtio-gpu the guest stops driving console 0 once the
session takes over and `screendump` returns a framebuffer reading *"Display output is not
active"* (`query-display-options` → `type: none`). Observed directly: the splash captured
fine, the desktop did not. std VGA keeps a dumpable scanout across the whole boot.

**`usb-tablet`, not the default mouse.** QEMU's default is a *relative* PS/2 device, which
makes guest pointer position drift from the coordinates sent — corrupting exactly the
click-accuracy behaviour this bed exists to verify. `usb-tablet` is absolute.

## Status — what is proven, and what is not

Proven working:

| | |
|---|---|
| Two VMs boot from one ISO | yes |
| QMP `screendump` | yes — captured the GRUB menu and Manjaro splash |
| QMP input injection | yes — `key ret` dismissed GRUB and boot proceeded (resolution changed 1024x768 → 1280x800) |
| VNC exposed | yes, 5901 / 5902 |
| ssh hostfwd wired | yes, host side |

**Not yet working: the live ISO does not reach a visible GNOME desktop under `-vga std`.**
After GRUB the framebuffer goes fully black (mean 0, stddev 0) while both guests sit at ~40%
CPU with 2.6G RSS — alive and doing work, but not presenting a session. The live ISO also
does not run sshd, so neither channel gives a way in. This is a GUEST configuration problem,
not a harness problem: capture and input are demonstrably working.

Next steps to try, cheapest first: boot with `nomodeset`; keep virtio-gpu but drive the
desktop phase over VNC only; or replace the live ISO with a cloud image plus autologin and
a pre-enabled sshd, which also removes the interactive installer from the loop entirely.
