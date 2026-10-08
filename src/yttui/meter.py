"""Medidor de audio real: lee el monitor del sistema (PipeWire/Pulse)
y entrega niveles por banda + golpe (beat) para el visualizador.

Si no hay monitor disponible, trabaja en modo simulado para no romper nada.
"""
from __future__ import annotations

import math
import random
import shutil
import subprocess
import threading
import time

NBANDS = 32
RATE = 44100
CHUNK = 2048  # muestras por análisis


def _find_monitor() -> str | None:
    # 1. el sink que esté sonando ahora (por si mpv sale por otro lado)
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-1] == "RUNNING":
                return parts[1] + ".monitor"
    except Exception:
        pass
    # 2. el sink por defecto
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5)
        name = (sink.stdout or "").strip()
        if name:
            return name + ".monitor"
    except Exception:
        pass
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].endswith(".monitor"):
                return parts[1]
    except Exception:
        pass
    return None


class AudioMeter(threading.Thread):
    """Hilo que captura audio y calcula bandas. Usar con start()/stop()."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._bands = [0.0] * NBANDS
        self._beat = 0.0
        self._run = False
        self.live = False  # True = leyendo audio real
        self._proc: subprocess.Popen | None = None
        self._tick = 0

    # -- API para la UI --
    def levels(self, n: int) -> list[float]:
        with self._lock:
            if n == NBANDS:
                return list(self._bands)
            # reescala a n barras por interpolación
            out = []
            for i in range(n):
                f = i / max(1, n - 1) * (NBANDS - 1)
                lo, hi = int(f), min(NBANDS - 1, int(f) + 1)
                frac = f - lo
                out.append(self._bands[lo] * (1 - frac) + self._bands[hi] * frac)
            return out

    @property
    def beat(self) -> float:
        with self._lock:
            return self._beat

    def run(self) -> None:
        self._run = True
        monitor = _find_monitor()
        if not monitor or not shutil.which("parec"):
            self._sim_loop()
            return
        try:
            self._proc = subprocess.Popen(
                ["parec", "-d", monitor, f"--rate={RATE}", "--channels=1",
                 "--format=float32le", "--latency-msec=50"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._sim_loop()
            return
        try:
            import numpy as np
        except Exception:
            self._proc.terminate()
            self._sim_loop()
            return
        self.live = True
        buf = b""
        need = CHUNK * 4
        fmin, fmax = 55.0, 14000.0
        edges = np.logspace(math.log10(fmin), math.log10(fmax), NBANDS + 1)
        bin_hz = RATE / CHUNK
        peak = np.ones(NBANDS) * 1e-3
        bass_avg = 0.05
        while self._run:
            try:
                assert self._proc.stdout is not None
                data = self._proc.stdout.read(need)
                if not data:
                    time.sleep(0.05)
                    continue
                buf += data
                if len(buf) < need:
                    continue
                buf = buf[-need:]
                samples = np.frombuffer(buf, dtype=np.float32).copy()
                rms = float(np.sqrt(np.mean(samples ** 2)))
                if rms < 0.004:  # silencio: deja caer todo
                    with self._lock:
                        self._bands = [b * 0.8 for b in self._bands]
                        self._beat *= 0.8
                    continue
                spec = np.abs(np.fft.rfft(samples * np.hanning(CHUNK)))
                freqs = np.arange(len(spec)) * bin_hz
                vals = []
                for b in range(NBANDS):
                    m = (freqs >= edges[b]) & (freqs < edges[b + 1])
                    vals.append(float(np.mean(spec[m])) if m.any() else 0.0)
                vals = np.array(vals)
                peak = np.maximum(vals, peak * 0.998)  # ganancia automática lenta
                norm = np.log1p(vals * 40.0 / (peak + 1e-6))
                norm = np.clip(norm / 3.0, 0.0, 1.0)
                bass = float(np.mean(norm[:3]))
                onset = max(0.0, bass - bass_avg)
                bass_avg += (bass - bass_avg) * 0.08
                with self._lock:
                    for i in range(NBANDS):
                        tgt = float(norm[i])
                        if i < 4:  # los graves pegan con el golpe
                            tgt = min(1.0, tgt + min(1.0, onset * 4.0) * 0.6)
                        cur = self._bands[i]
                        self._bands[i] = cur + (tgt - cur) * (0.6 if tgt > cur else 0.3)
                    self._beat = max(min(1.0, onset * 4.0), self._beat * 0.86)
            except Exception:
                time.sleep(0.05)
        try:
            if self._proc is not None:
                self._proc.terminate()
        except Exception:
            pass

    def _sim_loop(self) -> None:
        """Sin monitor: animación que parece viva (como antes)."""
        t = 0
        while self._run:
            t += 1
            beat = max(0.0, math.sin(t * 0.55)) ** 6
            with self._lock:
                for i in range(NBANDS):
                    f = i / (NBANDS - 1)
                    tgt = (math.sin(t * 0.28 + i * 0.45) * 0.25
                           + math.sin(t * 0.48 + i * 1.15) * 0.18
                           + (1.0 - f) * beat * 0.55
                           + random.random() * 0.22 + 0.35)
                    tgt = max(0.03, min(1.0, tgt))
                    cur = self._bands[i]
                    self._bands[i] = cur + (tgt - cur) * (0.55 if tgt > cur else 0.22)
                self._beat = beat
            time.sleep(0.12)

    def stop(self) -> None:
        self._run = False
