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

    def alive(self) -> bool:
        return self.proc.poll() is None  # poll() also reaps it if it died

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

    def _vnc_args(self) -> list[str]:
        s = self.settings
        return ["x11vnc", "-display", s.login_display, "-localhost", "-forever", "-shared", "-quiet",
                "-rfbport", str(s.login_vnc_port), "-passwd", self.password]

    def _ws_args(self) -> list[str]:
        s = self.settings
        return ["websockify", "--web", s.novnc_web_root,
                f"{self.bind_addr}:{s.login_novnc_port}", f"localhost:{s.login_vnc_port}"]

    def start(self) -> None:
        s = self.settings
        self._xvfb = _Proc(["Xvfb", s.login_display, "-screen", "0", "1366x850x24", "-nolisten", "tcp"])
        time.sleep(1.5)
        self._vnc = _Proc(self._vnc_args())
        time.sleep(1.0)
        self._ws = _Proc(self._ws_args())
        time.sleep(1.0)
        self._procs = [self._xvfb, self._vnc, self._ws]
        log.info("VNC portal up on %s:%s (tailscale=%s)", self.bind_addr, s.login_novnc_port, self.via_tailscale)

    def ensure_alive(self) -> list[str]:
        """x11vnc was seen dying on a client connect while a captcha waited,
        leaving the link dead. Respawn the VNC/websockify pieces with the SAME
        password so the link already sent stays valid. Xvfb can't be replaced
        under a running browser - that one is only reported."""
        restarted = []
        if not self._xvfb.alive():
            log.error("Xvfb died - the browser display is gone")
            return ["xvfb"]
        if not self._vnc.alive():
            self._vnc = _Proc(self._vnc_args())
            restarted.append("x11vnc")
        if not self._ws.alive():
            self._ws = _Proc(self._ws_args())
            restarted.append("websockify")
        if restarted:
            self._procs = [self._xvfb, self._vnc, self._ws]
            log.warning("VNC portal pieces restarted: %s", ", ".join(restarted))
        return restarted

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
