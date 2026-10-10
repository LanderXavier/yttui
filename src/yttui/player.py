"""Control minimalista de mpv vía IPC (sin ads, solo audio)."""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

SOCK = Path("/tmp/yttui-mpv.sock")


def _send(cmd: list) -> None:
    if not SOCK.exists():
        return
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(str(SOCK))
        s.sendall((json.dumps({"command": cmd}) + "\n").encode())
        s.close()
    except Exception:
        pass


def _query(cmd: list):
    """Envía comando y devuelve el campo data (para get_property)."""
    if not SOCK.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(1.5)
        s.connect(str(SOCK))
        s.sendall((json.dumps({"command": cmd}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        resp = json.loads(data.decode().split("\n")[0])
        return resp.get("data")
    except Exception:
        return None


class Player:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.paused = False
        self.volume = 80
        self.current: dict | None = None
        self._fade_gen = 0  # si cambia de tema a mitad del fundido, se cancela el viejo
        self._ramp_lock = threading.Lock()  # un solo fundido a la vez (si no, se pisan y puede quedar a 0)

    @staticmethod
    def _mpris_script() -> str | None:
        """Ruta del plugin mpv-mpris si está instalado (para el widget de Plasma)."""
        for p in (
            "/usr/lib/mpv/mpris.so",
            "/usr/lib/mpv-mpris/mpris.so",
            "/usr/lib32/mpv/mpris.so",
            "/usr/share/mpv/scripts/mpris.so",
        ):
            if Path(p).exists():
                return p
        return None

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if SOCK.exists():
            try:
                SOCK.unlink()
            except Exception:
                pass
        cmd = ["mpv", "--no-video", "--really-quiet", "--idle=yes",
             f"--input-ipc-server={SOCK}", "--no-terminal"]
        mpris = self._mpris_script()
        if mpris:
            cmd.append(f"--script={mpris}")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            if SOCK.exists():
                break
            time.sleep(0.1)
        _send(["set_property", "volume", self.volume])

    def stop(self) -> None:
        try:
            _send(["quit"])
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    def play_url(self, track: dict, url: str, fade_out: bool = True, fade_in: bool = True) -> None:
        sounding = self.proc is not None and self.proc.poll() is None and self.current is not None
        self.start()
        self.current = track
        self.paused = False
        if fade_out and sounding:
            self._ramp(0, 0.30)
        _send(["loadfile", url, "replace"])
        try:
            title = f"{track.get('title','')} - {track.get('artists','')}"
            # force-media-title no lo pisa mpv al cargar (media-title sí).
            _send(["set_property", "force-media-title", title])
            _send(["set_property", "media-title", title])

            def _retitle() -> None:
                for _ in range(3):
                    time.sleep(0.5)
                    _send(["set_property", "media-title", title])
                    _send(["set_property", "force-media-title", title])

            threading.Thread(target=_retitle, daemon=True).start()
        except Exception:
            pass
        if fade_in:
            _send(["set_property", "volume", 0])
            self._ramp(self.volume, 0.45)
        else:
            _send(["set_property", "volume", self.volume])

    def fade_out_async(self, duration: float = 0.35) -> None:
        """Baja el volumen en segundo plano (para solapar con la carga)."""

        def job() -> None:
            try:
                self._ramp(0, duration)
            except Exception:
                pass

        threading.Thread(target=job, daemon=True).start()

    def _ramp(self, target: int, duration: float) -> None:
        gen = self._fade_gen + 1
        self._fade_gen = gen
        try:
            cur = _query(["get_property", "volume"])
            cur = float(cur if isinstance(cur, (int, float)) else self.volume)
        except Exception:
            cur = float(self.volume)
        steps = max(1, int(duration / 0.05))
        # Serializado: el fundido viejo detecta el gen nuevo y aborta rápido
        # (libera el lock en ~50ms), así el nuevo es el único que escribe y
        # el volumen final siempre es el suyo. Sin esto, dos fundidos a la
        # vez intercalaban comandos y a veces quedaba a 0 sonando en silencio.
        with self._ramp_lock:
            for i in range(1, steps + 1):
                if gen != self._fade_gen:
                    return  # otro cambio tomó el mando
                v = round(cur + (target - cur) * i / steps)
                _send(["set_property", "volume", v])
                time.sleep(duration / steps)
            # Cierre autoritario: por si algún comando suelto se coló.
            if gen == self._fade_gen:
                _send(["set_property", "volume", target])

    def restore_volume(self) -> None:
        """Devuelve el volumen real (tras un fallo que nos dejó fundidos a 0)."""
        self._fade_gen += 1  # cancela fundidos en curso
        _send(["set_property", "volume", self.volume])

    def toggle(self) -> None:
        self.paused = not self.paused
        _send(["cycle", "pause"])

    def next_stop(self) -> None:
        _send(["stop"])

    def seek(self, secs: int) -> None:
        _send(["seek", secs, "relative"])

    def vol(self, delta: int) -> None:
        self.volume = max(0, min(100, self.volume + delta))
        _send(["set_property", "volume", self.volume])

    def is_idle(self) -> bool:
        """True si mpv está en idle (tema terminado). False si suena o no se sabe."""
        v = _query(["get_property", "idle-active"])
        return v is True
