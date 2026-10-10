"""Demonio MPRIS aparte para el widget de Plasma.

La app (curses) nunca toca D-Bus: este proceso expone
``org.mpris.MediaPlayer2.yttui`` y le manda órdenes a la app por
``/tmp/yttui-cmd.fifo`` (next/prev/toggle/...). Lee lo que suena de
``/tmp/yttui-nowplaying.json`` y la posición de mpv directo.
Si muere, la app sigue intacta. Se apaga solo si mpv falta 30s.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from pathlib import Path

BUS = "org.mpris.MediaPlayer2.yttui"
OPATH = "/org/mpris/MediaPlayer2"
FIFO = Path("/tmp/yttui-cmd.fifo")
STATE = Path("/tmp/yttui-nowplaying.json")


def _send_cmd(line: str) -> None:
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ENXIO:
            return  # la app no está escuchando
        return
    try:
        os.write(fd, (line + "\n").encode())
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _mpv(prop: str):
    try:
        from .player import _query as _q
        return _q(["get_property", prop])
    except Exception:
        return None


def _mpv_seek(delta_secs: int) -> None:
    try:
        from .player import _send as _s
        _s(["seek", int(delta_secs), "relative"])
    except Exception:
        pass


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _status() -> str:
    try:
        try:
            from .player import SOCK
        except Exception:
            SOCK = Path("/tmp/yttui-mpv.sock")
        if not SOCK.exists():
            return "Stopped"
        idle = _mpv("idle-active")
        if idle is None:
            return "Stopped"  # socket huérfano: mpv no responde
        if idle:
            return "Stopped"
        return "Paused" if _mpv("pause") else "Playing"
    except Exception:
        return "Stopped"


def _trackid(vid: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in (vid or "none"))[:40]
    return f"/org/mpris/MediaPlayer2/Track/{safe or 'none'}"


async def _serve() -> None:
    from dbus_next.aio import MessageBus
    from dbus_next.service import (
        ServiceInterface,
        dbus_property,
        method,
        signal,
        PropertyAccess,
    )
    from dbus_next import Variant

    class RootInterface(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self):
            pass

        @method()
        def Quit(self):
            pass

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return "yttui"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "yttui"

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return ["https", "http"]

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return ["audio/webm", "audio/mp4", "audio/x-mpeg"]

    class PlayerInterface(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")

        def _metadata(self) -> dict:
            st = _state()
            vid = str(st.get("videoId", ""))
            try:
                dur = _mpv("duration")
                length = int(float(dur or 0) * 1_000_000)
            except Exception:
                length = 0
            return {
                "mpris:trackid": Variant("o", _trackid(vid)),
                "mpris:length": Variant("x", length),
                "xesam:title": Variant("s", str(st.get("title", "yttui") or "yttui")),
                "xesam:artist": Variant("as", [str(st.get("artists", "Desconocido"))]),
                "xesam:album": Variant("s", str(st.get("album", ""))),
                "xesam:url": Variant(
                    "s", f"https://music.youtube.com/watch?v={vid}" if vid else ""
                ),
            }

        @method()
        def Next(self):
            _send_cmd("next")

        @method()
        def Previous(self):
            _send_cmd("prev")

        @method()
        def Pause(self):
            _send_cmd("pause")

        @method()
        def Play(self):
            _send_cmd("play")

        @method()
        def PlayPause(self):
            _send_cmd("toggle")

        @method()
        def Stop(self):
            _send_cmd("stop")

        @method()
        def Seek(self, offset: "x"):
            _mpv_seek(int(int(offset) // 1_000_000))
            try:
                pos = _mpv("time-pos")
                self.Seeked(int(float(pos or 0) * 1_000_000))
            except Exception:
                pass

        @method()
        def SetPosition(self, _track: "o", position: "x"):
            try:
                cur = float(_mpv("time-pos") or 0)
                _mpv_seek(int(int(position) // 1_000_000) - int(cur))
            except Exception:
                pass

        @method()
        def OpenUri(self, _uri: "s"):
            pass

        @signal()
        def Seeked(self, position: "x"):
            pass

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return _status()

        @dbus_property()
        def LoopStatus(self) -> "s":
            return {"off": "None", "all": "Playlist", "one": "Track"}.get(
                _state().get("repeat", "off"), "None"
            )

        @LoopStatus.setter
        def LoopStatus(self, val: "s"):
            m = {"None": "off", "Playlist": "all", "Track": "one"}.get(val)
            if m:
                _send_cmd(f"repeat:{m}")

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property()
        def Shuffle(self) -> "b":
            return bool(_state().get("shuffle", False))

        @Shuffle.setter
        def Shuffle(self, val: "b"):
            _send_cmd(f"shuffle:{1 if val else 0}")

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return self._metadata()

        @dbus_property()
        def Volume(self) -> "d":
            try:
                return float(_mpv("volume") or 80) / 100.0
            except Exception:
                return 0.8

        @Volume.setter
        def Volume(self, val: "d"):
            try:
                _send_cmd(f"vol:{int(round(float(val) * 100))}")
            except Exception:
                pass

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            try:
                return int(float(_mpv("time-pos") or 0) * 1_000_000)
            except Exception:
                return 0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

    bus = await MessageBus().connect()
    root, player = RootInterface(), PlayerInterface()
    bus.export(OPATH, root)
    bus.export(OPATH, player)
    reply = await bus.request_name(BUS)
    try:
        primary = reply.body[0] == 1
    except Exception:
        primary = True
    if not primary:
        return  # ya hay otro dueño (otra instancia): salir callado

    try:
        app_pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    except Exception:
        app_pid = 0

    def _app_dead() -> bool:
        if not app_pid:
            return False
        try:
            os.kill(app_pid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    last = ("", "", 0.0, "", "")
    missing = 0
    while True:
        await asyncio.sleep(1)
        if _app_dead():
            return  # la app murió (q, Ctrl+C, terminal): apagarse al instante
        try:
            from .player import SOCK
        except Exception:
            SOCK = Path("/tmp/yttui-mpv.sock")
        # Vivo = socket existe Y responde (un archivo huérfano no cuenta).
        try:
            mpv_vivo = _mpv("idle-active") is not None
        except Exception:
            mpv_vivo = False
        if not SOCK.exists() or not mpv_vivo:
            missing += 1
            if missing >= 30:
                return  # mpv lleva 30s muerto: la app se cerró, apagarse
            if _status() != last[0]:
                last = (_status(), last[1], last[2], last[3], last[4])
                try:
                    player.emit_properties_changed({"PlaybackStatus": last[0]})
                except Exception:
                    pass
            continue
        missing = 0
        st = _state()
        try:
            vol = int(float(_mpv("volume") or 80))
        except Exception:
            vol = 80
        cur = (_status(), str(st.get("videoId", "")), vol,
               str(st.get("shuffle", "")), str(st.get("repeat", "")))
        if cur != last:
            last = cur
            try:
                player.emit_properties_changed({
                    "PlaybackStatus": cur[0],
                    "Metadata": player._metadata(),
                    "Volume": vol / 100.0,
                    "Shuffle": bool(st.get("shuffle", False)),
                    "LoopStatus": {"off": "None", "all": "Playlist", "one": "Track"}.get(
                        st.get("repeat", "off"), "None"),
                })
            except Exception:
                pass


def main() -> None:
    try:
        asyncio.run(_serve())
    except Exception:
        pass


if __name__ == "__main__":
    main()
