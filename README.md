# yttui - YouTube Music en tu terminal (CachyOS)

Reproductor simple, sin ads, con atajos. Escribes `yttui` y listo.

## Instalar

```bash
git clone https://github.com/LanderXavier/yttui.git
cd yttui
pip install -e . --break-system-packages
```

Ya tienes todo: `ytmusicapi`, `yt-dlp`, `mpv v0.41`. No necesita Textual (usa `curses` de serie).

El comando queda en `~/.cargo/bin/yttui` (está en tu PATH). Si reinstalas y no lo encuentra:

```bash
ln -sf ~/.local/bin/yttui ~/.cargo/bin/yttui
```

## Configurar (1 vez)

```bash
yttui --setup
```

1. **Cookies (para reproducir sin bloqueos):**
   - Instala "Get cookies.txt LOCALLY" en tu navegador.
   - Entra a `music.youtube.com` con tu cuenta y exporta.
   - Guarda como `~/.config/yttui/cookies.txt`.

2. **Login (para Mis Likes y Mi Librería):**
   - El asistente te pide pegar tus headers: `music.youtube.com` logueado, F12 > Red, filtra `/browse`, copia "Request headers".
   - Lo pega y genera `~/.config/yttui/browser.json` solo.

Sin login igual puedes buscar y reproducir. Con login ves Likes/Librería.

## Usar

```bash
yttui
```

Pantallas: `1` Buscar (temas + playlists), `2` Likes, `3` Mi Librería. La cola va siempre a la derecha.

Atajos:

| Tecla | Acción |
|---|---|
| `Tab` / `Shift+Tab` | cambiar el seleccionado entre buscar y cola |
| `/` | buscar temas o playlists |
| `Enter` | reproducir lo seleccionado (en cola: reproduce ese tema) |
| `a` | añadir a continuación del que suena |
| `A` | añadir al final de la cola |
| `Ctrl+A` | añadir todos los temas visibles al final |
| `Espacio` | pausa |
| `n` / `p` | siguiente / anterior |
| `s` | shuffle on/off |
| `r` | repeat off -> all -> one |
| `+` / `-` | volumen |
| `<` / `->` | +/-10s |
| `j`/`k` o flechas | moverse en el panel con el foco (`>`) |
| `Supr` | quitar de la cola (el del cursor `>` si el foco está en cola) |
| `?` | ayuda |
| `q` | salir |

Visualizador: panel grande de 12 filas estilo WMP/Ares con el título arriba y barras `██` de colores (verde -> amarillo -> rojo) que laten con la música, más picos `──` que caen lento. Las barras centrales son altas y se apagan hacia los lados. Lee el audio real del sistema (monitor PipeWire + FFT con numpy): los graves pegan con los golpes del tema. Si no hay monitor, usa simulación. Terminal mínima 70x29 para que quepa todo.

Modo radio [radio]: al poner un tema suelto se precargan parecidos en la cola automáticamente (hilo en segundo plano, sin congelar). Cuando se acaba un tema o la cola, sigue solo con parecidos (como el autoplay de YouTube). Con `n` al final de la cola también arranca radio. `r` en `one` repite el mismo.

Sin anuncios: usa stream directo vía `yt-dlp + mpv`. La URL se guarda en caché de sesión y el siguiente tema se precarga en segundo plano: el primer cambio tarda 1-3s (red), los siguientes son casi instantáneos. La carga no congela la app: la bolita `⠋` sigue girando mientras resuelve. Al cambiar hay fundido chiquito (baja 0.3s, sube 0.45s).

## Cómo funciona

- `src/yttui/ytm.py`: search + likes + librería + `yt-dlp -g -f bestaudio` para URL.
- `src/yttui/player.py`: mpv `--idle --input-ipc-server=/tmp/yttui-mpv.sock`.
- `src/yttui/app.py`: TUI con curses.
- `src/yttui/setup_wizard.py`: genera config.
- OAuth descartado: ahora pide Client ID de Google Cloud, más lío que cookies.

## Si algo falla

- `yttui: command not found` -> rehaz el symlink de arriba o abre nueva terminal.
- Likes vacíos / "Falta login" -> `yttui --setup` de nuevo.
- No reproduce un tema restringido -> pon `cookies.txt` fresco (caducan si cierras sesión).
- mpv sin sonido -> prueba `mpv --no-video <url>` y revisa volumen PipeWire.
- Reinstalar limpio: `pip install -e . --break-system-packages --force-reinstall`.
