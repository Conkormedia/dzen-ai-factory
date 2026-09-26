"""Always-on noVNC portal for the live publish browser, bound to a private
Tailscale address so a human can solve Dzen's "Я не робот" captcha from
their phone without SSH. Falls back to 127.0.0.1 (SSH-tunnel-only, same as
login_portal.py) if Tailscale isn't up - this must never bind to a public
interface.
"""
from __future__ import annotations

import logging
import secrets
import signal
import subprocess
import time

from .config import Settings

log = logging.getLogger(__name__)


class _Proc:
    def __init__(self, args: list[str], **kwargs):
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def tailscale_ip() -> str:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 and out.stdout.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


class VncPortal:
    """Starts once at service startup, stays up for the process lifetime -
    the live publish browser runs headed-on-Xvfb the whole time so this can
    show it at any moment, not just when a captcha is already known."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.password = secrets.token_hex(4)
        self.bind_addr = tailscale_ip() or "127.0.0.1"
        self.via_tailscale = self.bind_addr != "127.0.0.1"
        self._procs: list[_Proc] = []

    def start(self) -> None:
        s = self.settings
        self._procs.append(_Proc(["Xvfb", s.login_display, "-screen", "0", "1366x850x24", "-nolisten", "tcp"]))
        time.sleep(1.5)
        self._procs.append(_Proc(["x11vnc", "-display", s.login_display, "-forever", "-shared", "-quiet",
                                   "-rfbport", str(s.login_vnc_port), "-passwd", self.password]))
        time.sleep(1.0)
        self._procs.append(_Proc(["websockify", "--web", s.novnc_web_root,
                                   f"{self.bind_addr}:{s.login_novnc_port}", f"localhost:{s.login_vnc_port}"]))
        time.sleep(1.0)
        log.info("VNC portal up on %s:%s (tailscale=%s)", self.bind_addr, s.login_novnc_port, self.via_tailscale)

    def link(self) -> str:
        return (f"http://{self.bind_addr}:{self.settings.login_novnc_port}/vnc.html"
                f"?autoconnect=true&resize=scale&password={self.password}")

    def message(self) -> str:
        if self.via_tailscale:
            return f"Открой на телефоне (Tailscale должен быть включён):\n{self.link()}"
        return (
            "Tailscale недоступен, через SSH-туннель:\n"
            f"1) ssh -L {self.settings.login_novnc_port}:localhost:{self.settings.login_novnc_port} robocall-server\n"
            f"2) http://localhost:{self.settings.login_novnc_port}/vnc.html"
            f"?autoconnect=true&resize=scale&password={self.password}"
        )

    def stop(self) -> None:
        for p in reversed(self._procs):
            p.stop()
