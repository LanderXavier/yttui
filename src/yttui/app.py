"""TUI amigable con curses: buscador (temas+playlists), cola a la derecha,
visualizador estilo Winamp/Ares, shuffle/repeat."""
from __future__ import annotations

import curses
import math
import random
import threading
import time

from .meter import AudioMeter
from .player import Player
from .ytm import (
    get_library_playlists,
    get_liked_songs,
    get_playlist_tracks,
    get_radio_tracks,
    get_stream_url,
    preresolve_url,
    search_all,
)

HELP = "1·2·3 pantallas   Tab foco   / buscar   Enter reproducir   a/A a cola   Espacio pausa   n/p sig/ant   s shuffle   r repeat   Supr quita   ? ayuda   q salir"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # bolita de carga tipo navegador

VIZ_H = 12  # panel alto estilo WMP/Ares
VIZ_MIN_H = 29
VIZ_MIN_W = 70


def edge_fade(i: int, n: int) -> float:
    """1.0 al centro -> ~0.2 en los bordes (se apaga a los lados)."""
    if n <= 1:
        return 1.0
    x = i / (n - 1) - 0.5  # -0.5..0.5
    return 0.20 + 0.80 * (math.cos(x * math.pi) ** 1.2)


class App:
    def __init__(self, stdscr) -> None:
        self.s = stdscr
        self.player = Player()
        self.screen = "search"  # search|likes|library
        self.results: list[dict] = []
        self.found_playlists: list[dict] = []  # playlists del buscador
        self.playlists: list[dict] = []  # librería propia
        self.queue: list[dict] = []
        self.qpos = -1
        self.sel = 0
        self.qsel = 0  # cursor de la cola (panel derecho)
        self.focus = "left"  # left|queue: dónde está el seleccionado (Tab lo cambia)
        self.msg = "Pulsa / para buscar canciones o playlists. ? ayuda."
        self.show_help = False
        self.shuffle = False
        self.repeat = "off"  # off|all|one
        self.tick = 0
        self.started_at = 0.0
        self._play_token = 0
        self._pending: tuple | None = None  # (token, kind, datos) resuelto en hilo
        self.loading = False  # resolviendo/cambiando de tema (se sigue animando)
        self.live_gain = 0.0  # cuánto de la capa real se suma (entra lento, ~1.5s)
        self.viz_levels: list[float] = []
        self.viz_peaks: list[float] = []
        self.meter = AudioMeter()

    # ---- helpers ----
    def label(self, t: dict) -> str:
        base = f"{t.get('title','')} - {t.get('artists','')}"
        if t.get("duration"):
            base += f" [{t['duration']}]"
        return base

    def left_items(self) -> list[tuple[str, dict]]:
        """(tipo, dato): tipo = song|playlist|libpl."""
        if self.screen == "search":
            items = [("playlist", p) for p in self.found_playlists]
            items += [("song", t) for t in self.results]
            return items
        if self.screen == "likes":
            return [("song", t) for t in self.results]
        if self.screen == "library":
            return [("libpl", p) for p in self.playlists]
        return []

    def left_label(self, kind: str, d: dict) -> str:
        if kind == "song":
            t = f"{d.get('title','').strip()} — {d.get('artists','').strip()}"
            if d.get("duration"):
                t += f"  · {d['duration']}"
            return "♪ " + " ".join(t.split())
        if kind == "playlist":
            t = f"{d.get('title','').strip()} — {d.get('author','').strip()}"
            return "▦ " + " ".join(t.split())
        t = f"{d.get('title','').strip()} ({d.get('count','')})"
        return "▦ " + " ".join(t.split())

    @staticmethod
    def fit(text: str, width: int) -> str:
        """Recorta con … y rellena para que las columnas no se peguen."""
        text = " ".join((text or "").split())
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 1:
            return "…"
        return text[: width - 1] + "…"

    def load_screen(self) -> None:
        try:
            if self.screen == "likes":
                songs, need_auth = get_liked_songs()
                self.results = songs
                self.found_playlists = []
                self.msg = "Falta login: `yttui --setup`" if need_auth else f"* {len(songs)} likes. Enter > | a +siguiente | A +final."
            elif self.screen == "library":
                pls, need_auth = get_library_playlists()
                self.playlists = pls
                self.msg = "Falta login: `yttui --setup`" if need_auth else f"# {len(pls)} playlists. Enter abre y mete en cola."
        except Exception as e:
            self.msg = f"Error: {str(e)[:140]}"
        self.sel = 0
        self.focus = "left"

    def clamp(self) -> None:
        items = self.left_items()
        self.sel = max(0, min(self.sel, max(0, len(items) - 1)))
        self.qsel = max(0, min(self.qsel, max(0, len(self.queue) - 1)))
        if self.qpos >= len(self.queue):
            self.qpos = len(self.queue) - 1

    def switch_focus(self) -> None:
        self.clamp()
        self.focus = "queue" if self.focus == "left" else "left"
        where = "cola ->" if self.focus == "queue" else "< buscar"
        self.msg = f"Foco en {where} (Tab para cambiar)."

    def do_search(self) -> None:
        # El timeout de animación rompe getstr: corta lo que escribes
        # y las letras fugadas disparan atajos (ej. 's' = shuffle). Por eso
        # se pasa a modo bloqueo durante la entrada.
        self.s.timeout(-1)
        try:
            curses.echo()
            self.s.addstr(curses.LINES - 2, 0, "Buscar temas o playlists: ".ljust(curses.COLS - 1))
            self.s.refresh()
            try:
                q = self.s.getstr(curses.LINES - 2, 25, 100).decode().strip()
            except Exception:
                q = ""
        finally:
            curses.noecho()
            self.s.timeout(120)
        if not q:
            return
        self.msg = f"Buscando '{q}'..."
        self.draw()
        try:
            self.screen = "search"
            songs, pls = search_all(q)
            self.results = songs
            self.found_playlists = pls
            self.msg = f"{len(songs)} temas + {len(pls)} playlists para '{q}'. Enter > | a +cola."
        except Exception as e:
            self.msg = f"Error buscando: {str(e)[:140]}"
        self.sel = 0
        self.focus = "left"

    def add_to_queue(self, track: dict, where: str = "next") -> None:
        """where: 'next' = a continuación del que suena, 'end' = al final."""
        if not track.get("videoId"):
            self.msg = "Eso no se puede añadir (es playlist, ábrela con Enter)."
            return
        if where == "next" and self.qpos >= 0:
            pos = self.qpos + 1
            self.queue.insert(pos, track)
            self.qsel = pos
            self.msg = f"+siguiente: {self.label(track)[:70]} ({len(self.queue)} en cola)"
        else:
            self.queue.append(track)
            if self.qpos == -1:
                self.qpos = 0
            self.qsel = len(self.queue) - 1
            self.msg = f"+final: {self.label(track)[:70]} ({len(self.queue)} en cola)"

    def add_many(self, tracks: list[dict], where: str = "end") -> None:
        tracks = [t for t in tracks if t.get("videoId")]
        if not tracks:
            return
        if where == "next" and self.qpos >= 0:
            pos = self.qpos + 1
            self.queue[pos:pos] = tracks
            self.qsel = pos
            self.msg = f"+siguiente: playlist con {len(tracks)} temas."
        else:
            self.queue.extend(tracks)
            if self.qpos == -1 and tracks:
                self.qpos = 0
            self.msg = f"+final: playlist con {len(tracks)} temas."

    def play_track(self, track: dict, add_to_queue: bool = True) -> None:
        if not track.get("videoId"):
            self.msg = "Eso es una playlist: Enter la abre, `a` la suma entera."
            return
        if add_to_queue and track not in self.queue:
            self.queue.append(track)
            self.qpos = len(self.queue) - 1
        self.qsel = max(0, self.qpos)
        # El tema viejo se va apagando mientras se resuelve el nuevo (caché o red).
        # La resolución va en hilo: el loop sigue dibujando y la bolita no se para.
        if self.player.current is not None:
            self.player.fade_out_async(0.35)
        self._play_token += 1
        tok = self._play_token
        self._pending = None
        self.loading = True
        self.msg = f"Cargando: {self.label(track)[:80]}..."
        self.draw()

        def job() -> None:
            try:
                url = get_stream_url(track["videoId"])  # instantáneo si está en caché
                self._pending = (tok, "url", (track, url, ""))
            except Exception as e:
                self._pending = (tok, "url", (track, "", str(e)[:140]))

        threading.Thread(target=job, daemon=True).start()

    def _finish_pending(self) -> None:
        if not self._pending or self._pending[0] != self._play_token:
            return
        _, kind, data = self._pending
        self._pending = None
        if kind == "url":
            track, url, err = data
            if not url:
                self.loading = False
                self.msg = f"No reproduce: {err}"
                return
            self.player.play_url(track, url, fade_out=False, fade_in=True)
            self.started_at = time.time()
            self.loading = False
            self.msg = f"> {self.label(track)[:90]}"
            # Deja resuelto el siguiente para que el cambio sea instantáneo
            if 0 <= self.qpos + 1 < len(self.queue):
                preresolve_url(self.queue[self.qpos + 1].get("videoId", ""))
            # Si pusiste un tema suelto, precarga la radio en la cola por si acaso
            if len(self.queue) <= 2:
                self._prefetch_radio(track, self._play_token)
        elif kind == "radio":
            seed, fresh, err = data
            if not fresh:
                self.loading = False
                self.msg = f"[radio]: {err or 'no encontré parecidos.'}"
                return
            self.queue.extend(fresh)
            preresolve_url(fresh[0].get("videoId", ""))
            self.qpos = len(self.queue) - len(fresh)
            self.qsel = max(0, self.qpos)
            self.msg = f"[radio]: {len(fresh)} parecidos en cola."
            self.play_track(fresh[0], add_to_queue=False)

    def _prefetch_radio(self, seed: dict, token: int) -> None:
        def job() -> None:
            try:
                if len(self.queue) > 40:
                    return
                fresh = [t for t in get_radio_tracks(seed.get("videoId", ""), limit=15) if t not in self.queue]
                if not fresh:
                    return
                self.queue.extend(fresh)
                if fresh:
                    preresolve_url(fresh[0].get("videoId", ""))
                if token == self._play_token:
                    self.msg = f"[radio]: +{len(fresh)} parecidos en cola."
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start()

    def start_radio(self, seed: dict) -> None:
        """Modo radio: mete temas parecidos a la cola y sigue sonando (en hilo)."""
        self._play_token += 1
        tok = self._play_token
        self._pending = None
        self.loading = True
        self.msg = f"[radio]: buscando parecidos a {seed.get('title','')[:40]}..."
        self.draw()

        def job() -> None:
            try:
                fresh = [t for t in get_radio_tracks(seed.get("videoId", ""), limit=15) if t not in self.queue]
                self._pending = (tok, "radio", (seed, fresh, ""))
            except Exception as e:
                self._pending = (tok, "radio", (seed, [], f"falló: {str(e)[:100]}"))

        threading.Thread(target=job, daemon=True).start()

    def auto_advance(self) -> None:
        """Se llama solo cuando mpv queda en idle = tema terminado."""
        if self.loading or self._pending:
            return  # ya hay un cambio en curso
        if not self.queue and not self.player.current:
            return
        if self.repeat == "one" and self.qpos >= 0:
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        if self.shuffle and len(self.queue) > 1:
            import random as _r

            nxt = _r.randrange(len(self.queue))
            if nxt == self.qpos:
                nxt = (nxt + 1) % len(self.queue)
            self.qpos = nxt
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        if self.qpos + 1 < len(self.queue):
            self.qpos += 1
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        if self.repeat == "all" and self.queue:
            self.qpos = 0
            self.play_track(self.queue[0], add_to_queue=False)
            return
        if self.player.current:
            self.start_radio(self.player.current)

    def play_next(self, step: int) -> None:
        if not self.queue:
            self.msg = "Cola vacía: busca con / y suma con `a`."
            return
        if self.repeat == "one" and step > 0 and self.qpos >= 0:
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        if self.shuffle and step > 0 and len(self.queue) > 1:
            import random as _r

            nxt = _r.randrange(len(self.queue))
            if nxt == self.qpos:
                nxt = (nxt + 1) % len(self.queue)
            self.qpos = nxt
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        nxt = self.qpos + step
        if nxt >= len(self.queue):
            if self.repeat == "all":
                nxt = 0
            else:
                if self.player.current:
                    self.start_radio(self.player.current)
                return
        if nxt < 0:
            nxt = 0
        self.qpos = nxt
        self.play_track(self.queue[self.qpos], add_to_queue=False)

    def open_playlist(self, pid: str, autoplay_first: bool = True) -> None:
        self.msg = "Cargando playlist..."
        self.draw()
        try:
            tracks = get_playlist_tracks(pid)
            self.queue.extend(tracks)
            if self.qpos == -1 and tracks:
                self.qpos = 0
            if autoplay_first and tracks:
                self.qpos = len(self.queue) - len(tracks)
                self.play_track(tracks[0], add_to_queue=False)
            self.msg = f"# {len(tracks)} temas a la cola (derecha). n/p para moverte."
        except Exception as e:
            self.msg = f"Error playlist: {str(e)[:140]}"

    # ---- visualizador grande estilo WMP/Ares ----
    def _viz_targets(self, n: int, active: bool, calm: bool = False) -> list[float]:
        if not active:
            return [0.04] * n
        if calm:
            # Esperando audio: olas suaves que no se repiten igual (no finge bailar)
            t = self.tick * 0.15
            out = []
            for i in range(n):
                v = (0.24
                     + 0.10 * math.sin(t * 0.9 + i * 0.55)
                     + 0.06 * math.sin(t * 0.53 + i * 1.30 + 1.7)
                     + 0.04 * math.sin(t * 1.70 + i * 2.10 + 0.6))
                out.append(max(0.03, v * edge_fade(i, n)))
            return out
        t = self.tick * 0.28
        beat = max(0.0, math.sin(self.tick * 0.55)) ** 6  # pulso grave periódico
        out = []
        for i in range(n):
            f = i / max(1, n - 1)
            v = (
                math.sin(t + i * 0.45) * 0.25
                + math.sin(t * 1.7 + i * 1.15) * 0.18
                + math.sin(t * 0.6 - f * 5.0) * 0.20
                + (1.0 - f) * beat * 0.55  # los graves laten más
                + random.random() * 0.22
                + 0.35
            )
            out.append(max(0.03, min(1.0, v)) * edge_fade(i, n))
        return out

    def _update_viz(self, n: int, active: bool, loading: bool = False) -> None:
        # La senoidal va SIEMPRE de fondo (continua, sin cortes). El audio real,
        # cuando hay, solo suma pegada encima. Así nunca hay cambio brusco.
        if len(self.viz_levels) != n:
            self.viz_levels = [0.05] * n
            self.viz_peaks = [0.05] * n
        sim = self._viz_targets(n, active or loading, calm=loading)
        live = None
        energy = 0.0
        if self.meter.live:
            try:
                live = self.meter.levels(n)
                energy = max(live) if live else 0.0
            except Exception:
                live = None
        # La capa real entra lento encima del fondo (y sale más lento aún):
        # el movimiento base nunca cambia de golpe, solo suma pegada.
        want = 1.0 if energy > 0.08 else 0.0
        self.live_gain += (want - self.live_gain) * (0.08 if want > self.live_gain else 0.05)
        g = self.live_gain
        for i in range(n):
            l = max(0.0, min(1.0, live[i])) if live is not None else 0.0
            tgt = (sim[i] * 0.65 + l * 0.9 * g) * edge_fade(i, n)
            tgt = max(0.03, min(1.0, tgt))
            cur = self.viz_levels[i]
            rate = 0.6 if tgt > cur else 0.3
            self.viz_levels[i] = cur + (tgt - cur) * rate
            self.viz_peaks[i] = max(tgt if tgt > self.viz_peaks[i] else self.viz_peaks[i] - 0.02, self.viz_levels[i])

    def _bar_color(self, row_from_bottom: int):
        try:
            if row_from_bottom < VIZ_H * 0.5:
                return curses.color_pair(1)
            if row_from_bottom < VIZ_H * 0.78:
                return curses.color_pair(2)
            return curses.color_pair(3)
        except Exception:
            return curses.A_NORMAL

    # ---- dibujo ----
    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            h, w = self.s.getmaxyx()
            if not (0 <= y < h and 0 <= x < w):
                return
            self.s.addstr(y, x, text[: max(0, w - 1 - x)], attr)
        except curses.error:
            pass

    def _pair(self, n: int, fallback: int = 0) -> int:
        try:
            return curses.color_pair(n)
        except Exception:
            return fallback

    def draw(self) -> None:
        s = self.s
        s.clear()
        h, w = s.getmaxyx()
        if h < VIZ_MIN_H or w < VIZ_MIN_W:
            self._put(0, 0, f"  Agranda la terminal  (mín {VIZ_MIN_W}x{VIZ_MIN_H})")
            s.refresh()
            return
        queue_w = min(38, w // 3)
        left_w = w - queue_w - 1  # col left_w = divisor vertical
        div_x = left_w

        c_green = self._pair(1)
        c_yellow = self._pair(2)
        c_red = self._pair(3)
        c_mag = self._pair(4)
        c_cyan = self._pair(5)
        c_blue = self._pair(6)
        c_dim = curses.A_DIM
        c_bold = curses.A_BOLD

        # ===== 0. cabecera =====
        tabs = [("1", "Buscar", "search"), ("2", "Likes", "likes"), ("3", "Librería", "library")]
        self._put(0, 0, " " * (w - 1))
        self._put(0, 1, "♪", c_cyan | c_bold)
        self._put(0, 3, "yttui", c_cyan | c_bold)
        cx = 10
        for num, name, key in tabs:
            active = self.screen == key
            tab = f" {num} {name} "
            if active:
                attr = curses.A_REVERSE | c_bold
            else:
                attr = c_dim
            self._put(0, cx, tab, attr)
            cx += len(tab) + 1
        # estado derecha: vol + shuffle + repeat + foco
        vol_blocks = round(max(0, min(130, self.player.volume)) / 130 * 8)
        vol_bar = "█" * vol_blocks + "░" * (8 - vol_blocks)
        focus_txt = "◀ lista" if self.focus == "left" else "cola ▶"
        # (evita emoji: solo texto)
        right = f" {focus_txt}   Vol {vol_bar} {self.player.volume}   {'SHUF' if self.shuffle else 'seq'}   rep:{self.repeat} "
        if len(right) < w - cx:
            self._put(0, w - 1 - len(right), right, c_dim)

        # ===== 1. separador superior con unión =====
        self._put(1, 0, "─" * div_x, c_blue | c_dim)
        self._put(1, div_x, "┬", c_blue | c_dim)
        self._put(1, div_x + 1, "─" * max(0, w - div_x - 2), c_blue | c_dim)

        # ===== 2. títulos de columna =====
        LTOP = 3
        items = self.left_items()
        self.clamp()
        if self.screen == "search":
            left_title = f"♪  Resultados  ·  {len(self.results)} temas  ·  {len(self.found_playlists)} listas"
        elif self.screen == "likes":
            left_title = f"♥  Mis Likes  ·  {len(self.results)} temas"
        else:
            left_title = f"▦  Mi Librería  ·  {len(self.playlists)} listas"
        left_mark = "▶ " if self.focus == "left" else "  "
        self._put(2, 0, left_mark, c_green | c_bold)
        self._put(2, 2, self.fit(left_title, div_x - 4), c_bold)
        q_mark = "▶ " if self.focus == "queue" else "  "
        self._put(2, div_x + 1, q_mark, c_green | c_bold)
        self._put(2, div_x + 3, self.fit(f"≡  Cola  ·  {len(self.queue)}", queue_w - 5), c_mag | c_bold)

        # ===== 3. listas =====
        list_rows = h - VIZ_H - 8  # cab(1)+sep(1)+colhead(1)+vizsep(1)+viztitle(1)+viz(H)+pie(3)
        list_rows = max(3, list_rows)
        # divisor vertical
        for y in range(2, LTOP + list_rows):
            self._put(y, div_x, "│", c_blue | c_dim)
        # izquierda
        start = max(0, min(self.sel - list_rows + 1, max(0, len(items) - list_rows)))
        for i in range(list_rows):
            idx = start + i
            y = LTOP + i
            if idx >= len(items):
                break
            kind, d = items[idx]
            txt = self.fit(self.left_label(kind, d), div_x - 5)
            is_cur = idx == self.sel
            cursor = "►" if is_cur else " "
            row_txt = f" {cursor} {txt}".ljust(div_x)[:div_x]
            if self.focus == "left" and is_cur:
                attr = curses.A_REVERSE | c_bold
            elif is_cur:
                attr = c_bold
            elif kind != "song":
                attr = c_cyan
            else:
                attr = 0
            self._put(y, 0, row_txt, attr)
        if not items:
            self._put(LTOP, 2, self.fit("— vacío: pulsa / para buscar —", div_x - 4), c_dim)
        # derecha (cola)
        qstart = max(0, min(max(0, self.qsel - list_rows + 1), max(0, len(self.queue) - list_rows)))
        for i in range(list_rows):
            idx = qstart + i
            y = LTOP + i
            if idx >= len(self.queue):
                break
            t = self.queue[idx]
            txt = self.fit(f"{t.get('title','').strip()} — {t.get('artists','').strip()}", queue_w - 8)
            is_playing = idx == self.qpos
            is_cursor = self.focus == "queue" and idx == self.qsel
            if is_playing and is_cursor:
                cursor = "▶►"
            elif is_playing:
                cursor = "▶ "
            elif is_cursor:
                cursor = "► "
            else:
                cursor = "  "
            num = f"{idx + 1:2d}."
            row_txt = f" {cursor}{num} {txt}"
            row_txt = row_txt.ljust(queue_w - 1)[: queue_w - 1]
            if is_playing:
                attr = c_mag | c_bold
            elif is_cursor:
                attr = curses.A_REVERSE | c_bold
            else:
                attr = 0
            self._put(y, div_x + 1, row_txt, attr)
        if not self.queue:
            self._put(LTOP, div_x + 3, self.fit("— cola vacía: suma con a —", queue_w - 5), c_dim)

        # ===== 4. visualizador =====
        cur = self.player.current
        playing = cur is not None and not self.player.paused
        viz_sep = LTOP + list_rows
        viz_title = viz_sep + 1
        viz_top = viz_sep + 2
        n_bars = max(8, (w - 6) // 3)
        self._update_viz(n_bars, playing, self.loading)
        # línea divisoria del viz
        self._put(viz_sep, 0, "─" * (w - 1), c_blue | c_dim)
        if cur:
            vtxt = f" ♪  {self.fit(self.label(cur), w - 14)} "
        else:
            vtxt = " ♪  nada sonando — busca con / "
        if self.loading:
            ball = SPINNER[self.tick % len(SPINNER)]
            vtxt = f" {ball}  Cargando… "
        vtxt = self.fit(vtxt.strip(), w - 6)
        _vt = f"┌─{vtxt}─"
        _vt = _vt.ljust(w - 2)[: w - 2] + "│"
        self._put(viz_title, 0, _vt, c_bold)
        for r in range(VIZ_H):
            row = viz_top + r
            if row >= h - 3:
                break
            bottom = VIZ_H - 1 - r  # 0 = abajo
            line = [" "] * (w - 1)
            line[0] = "│"
            if w - 2 >= 0:
                line[w - 2] = "│"
            for i in range(n_bars):
                lvl = self.viz_levels[i] * VIZ_H
                peak = self.viz_peaks[i] * VIZ_H
                x = 2 + i * 3
                if x + 1 >= w - 2:
                    break
                if lvl > bottom:
                    line[x] = "█"
                    line[x + 1] = "█"
                elif abs(peak - bottom) < 0.6 and (playing or self.loading):
                    line[x] = "─"
                    line[x + 1] = "─"
            txt = "".join(line)
            # Bordes neutros: solo las barras llevan el color de nivel
            try:
                bar_attr = self._bar_color(bottom)
                edge_attr = c_blue | c_dim
                s.addstr(row, 0, txt[0], edge_attr)
                if len(txt) > 2:
                    s.addstr(row, 1, txt[1:-1], bar_attr)
                s.addstr(row, w - 2, txt[-1], edge_attr)
            except curses.error:
                pass

        # ===== 5. pie =====
        try:
            icon = "▶" if playing else ("❚❚" if cur else "○")
            now_txt = self.fit(self.label(cur), w - 42) if cur else "nada sonando"
            status = f" {icon}  {now_txt} "
            flags = f" Vol {self.player.volume}  │  {'SHUF' if self.shuffle else 'seq'}  │  rep:{self.repeat} "
            line3 = (status + " " * max(1, w - 1 - len(status) - len(flags)) + flags)[: w - 1]
            self._put(h - 3, 0, line3, c_bold)
            self._put(h - 2, 0, ("› " + self.fit(HELP if self.show_help else self.msg, w - 4)).ljust(w - 1)[: w - 1])
            self._put(h - 1, 0, self.fit(HELP, w - 1), c_dim)
        except curses.error:
            pass
        s.refresh()

    def selected_action(self, add_only: bool | str = False) -> None:
        """add_only False = reproducir, 'next' = a continuación, 'end' = al final."""
        # Si el foco está en la cola: Enter reproduce ese tema, `a` no pinta nada.
        if self.focus == "queue":
            if not self.queue:
                self.msg = "Cola vacía: cambia con Tab y suma con `a`."
                return
            self.clamp()
            if add_only:
                self.msg = "Ya está en la cola: pulsa Enter para reproducirlo."
                return
            self.qpos = self.qsel
            self.play_track(self.queue[self.qpos], add_to_queue=False)
            return
        items = self.left_items()
        if not items:
            self.msg = "Nada aquí: busca con / primero."
            return
        self.clamp()
        if not (0 <= self.sel < len(items)):
            return
        kind, d = items[self.sel]
        if kind == "song":
            if add_only:
                self.add_to_queue(d, where=add_only)
            else:
                self.play_track(d)
            return
        pid = d.get("playlistId", "")
        if not pid:
            self.msg = "Playlist sin ID."
            return
        if add_only:
            try:
                self.add_many(get_playlist_tracks(pid), where=add_only)
            except Exception as e:
                self.msg = f"Error: {str(e)[:140]}"
        else:
            self.open_playlist(pid)

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        self.meter.start()  # mide el audio real en segundo plano
        self.s.keypad(True)
        self.s.timeout(120)  # ~8fps: animación suave sin fundir CPU
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)    # graves / foco
            curses.init_pair(2, curses.COLOR_YELLOW, -1)   # medios
            curses.init_pair(3, curses.COLOR_RED, -1)      # agudos/picos
            curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # tema en cola
            curses.init_pair(5, curses.COLOR_CYAN, -1)     # cabecera
            try:
                curses.init_pair(6, curses.COLOR_BLUE, -1)     # separadores
                curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selección / barra estado
            except Exception:
                pass
        except Exception:
            pass
        while True:
            self.tick += 1
            self._finish_pending()  # remata lo resuelto en hilo sin congelar
            self.draw()
            c = self.s.getch()
            if c == -1:
                # ~1 vez por segundo: si mpv quedó idle = tema terminado -> avanza solo
                # (nada mientras se está cargando algo: si no, se duplicaría)
                if self.tick % 8 == 0 and self.player.current and not self.player.paused:
                    if not self.loading and self._pending is None and time.time() - self.started_at > 8:
                        try:
                            if self.player.is_idle():
                                self.auto_advance()
                        except Exception:
                            pass
                continue
            items_len = len(self.left_items())
            if c == ord("q"):
                break
            elif c == ord("?"):
                self.show_help = not self.show_help
            elif c in (ord("1"), ord("2"), ord("3")):
                self.screen = {"1": "search", "2": "likes", "3": "library"}[chr(c)]
                self.load_screen()
            elif c == ord("/"):
                self.do_search()
            elif c in (9, curses.KEY_BTAB, 353):
                # Tab y Shift+Tab: el seleccionado salta entre buscar y cola
                self.switch_focus()
            elif c in (curses.KEY_UP, ord("k")):
                if self.focus == "queue":
                    self.qsel = max(0, self.qsel - 1)
                else:
                    self.sel = max(0, self.sel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                if self.focus == "queue":
                    self.qsel = max(0, min(max(0, len(self.queue) - 1), self.qsel + 1))
                else:
                    self.sel = max(0, min(max(0, items_len - 1), self.sel + 1))
            elif c == ord(" "):
                if self.player.current is None:
                    self.msg = "Nada sonando: busca con / y dale Enter."
                else:
                    self.player.toggle()
            elif c == ord("n"):
                self.play_next(1)
            elif c == ord("p"):
                self.play_next(-1)
            elif c == ord("a"):
                self.selected_action(add_only="next")
            elif c == ord("A"):
                self.selected_action(add_only="end")
            elif c == 1:  # Ctrl+A: todo lo visible al final
                songs = [d for kind, d in self.left_items() if kind == "song"] if self.focus == "left" else []
                if songs:
                    self.add_many(songs, where="end")
                else:
                    self.msg = "Nada que sumar."
            elif c == ord("s"):
                self.shuffle = not self.shuffle
                self.msg = f"Shuffle {'ON SHUF' if self.shuffle else 'OFF SEQ'}"
            elif c == ord("r"):
                order = ["off", "all", "one"]
                self.repeat = order[(order.index(self.repeat) + 1) % 3]
                self.msg = f"Repeat: {self.repeat}"
            elif c in (ord("+"), ord("=")):
                self.player.vol(5)
            elif c in (ord("-"), ord("_")):
                self.player.vol(-5)
            elif c == curses.KEY_RIGHT:
                self.player.seek(10)
            elif c == curses.KEY_LEFT:
                self.player.seek(-10)
            elif c in (curses.KEY_ENTER, 10, 13):
                self.selected_action(add_only=False)
            elif c in (curses.KEY_DC, 127, curses.KEY_BACKSPACE):
                if self.focus == "queue" and self.queue:
                    self.clamp()
                    i = self.qsel
                    gone = self.queue.pop(i)
                    if self.qpos == i:
                        self.qpos = min(i, len(self.queue) - 1)
                        if self.player.current and self.player.current.get("videoId") == gone.get("videoId"):
                            self.player.next_stop()
                    elif self.qpos > i:
                        self.qpos -= 1
                    self.qsel = max(0, min(self.qsel, len(self.queue) - 1))
                    self.msg = f"-cola: {gone.get('title','')[:60]} ({len(self.queue)} quedan)"
                elif self.queue:
                    gone = self.queue.pop()
                    self.msg = f"-cola: {gone.get('title','')[:60]} ({len(self.queue)} quedan)"
        self.meter.stop()
        self.player.stop()


def main(stdscr) -> None:
    app = App(stdscr)
    try:
        app.run()
    finally:
        try:
            app.meter.stop()
        except Exception:
            pass
