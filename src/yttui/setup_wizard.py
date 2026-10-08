"""Asistente de 1 paso para cookies + login."""
from __future__ import annotations

import sys

from .ytm import BROWSER_JSON, CONFIG_DIR, COOKIES_TXT, ensure_config_dir


def run_setup() -> int:
    ensure_config_dir()
    print("=== yttui --setup ===")
    print(f"Config en: {CONFIG_DIR}\n")

    print("[1/2] Cookies para reproducir sin bloqueos (yt-dlp):")
    if COOKIES_TXT.exists():
        print(f"  OK: {COOKIES_TXT} existe.")
    else:
        print(f"  Falta: {COOKIES_TXT}")
        print("  Cómo sacarlo en 3 pasos:")
        print("   1. En tu navegador instala 'Get cookies.txt LOCALLY'.")
        print("   2. Entra a music.youtube.com con tu cuenta y exporta las cookies.")
        print("   3. Guarda el archivo como:")
        print(f"      {COOKIES_TXT}")
    print()

    print("[2/2] Login para ver Mis Likes y Mi Librería:")
    if BROWSER_JSON.exists():
        print(f"  OK: {BROWSER_JSON} existe.")
    else:
        print(f"  Falta: {BROWSER_JSON} (sin esto, Likes/Librería no se ven, pero buscar sí funciona).")
        print("  Opción simple: pega tus headers del navegador y lo genero por ti.")
        print("   1. Abre music.youtube.com logueado, F12 > Red, filtra /browse.")
        print("   2. Click en una petición POST browse > Headers > copiar 'Request headers'.")
        print("   3. Pégalos abajo y pulsa Ctrl+D (línea vacía + Enter en algunos terminales):")
        try:
            print("--- pega ahora (Ctrl+D para terminar, o escribe SALTAR) ---")
            raw = sys.stdin.read()
            if raw and raw.strip() and raw.strip().upper() != "SALTAR":
                import ytmusicapi

                ytmusicapi.setup(filepath=str(BROWSER_JSON), headers_raw=raw)
                print(f"  OK: generado {BROWSER_JSON}")
            else:
                print("  Saltado. Podrás buscar y reproducir, pero no ver Likes.")
        except Exception as e:
            print(f"  No se pudo generar: {e}")
            print("  Puedes reintentar con: ytmusicapi browser")
    print("\nListo. Ejecuta `yttui` para abrir el reproductor.")
    return 0
