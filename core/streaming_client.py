# Conexion a Spotify
#
# Lee la cancion que suena en tu cuenta de Spotify (en cualquier dispositivo:
# movil, PC, altavoz...). Ventajas frente al microfono:
#   - posicion exacta en milisegundos -> letra perfectamente sincronizada
#   - detecta pausas y saltos dentro de la cancion
#   - da la duracion, que ayuda a LRCLIB a elegir la version correcta de la letra
#
# Requiere una app en https://developer.spotify.com/dashboard con:
#   - Redirect URI: http://127.0.0.1:8888/callback
#   - SPOTIPY_CLIENT_ID en .env (no hace falta el client secret: se usa PKCE)
# Cada persona inicia sesion con SU cuenta: la primera vez se abre el navegador
# para autorizar, y el token queda guardado solo en su PC (.spotify_cache).
# Para cambiar de cuenta basta con borrar .spotify_cache.
#
# Prueba rapida:  python -m core.streaming_client
import asyncio
import os
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyPKCE

from core.mic_listener import TrackInfo
from utils.logger import get_logger

log = get_logger("spotify")
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SCOPES = "user-read-currently-playing user-read-playback-state"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
POLL_SECONDS = 3


def spotify_configured() -> bool:
    return bool(os.getenv("SPOTIPY_CLIENT_ID"))


class SpotifyListener:
    def __init__(self, poll_seconds: float = POLL_SECONDS):
        self.poll_seconds = poll_seconds
        # PKCE: login de usuario sin client secret, se puede compartir el client id
        self.sp = spotipy.Spotify(auth_manager=SpotifyPKCE(
            client_id=os.getenv("SPOTIPY_CLIENT_ID"),
            scope=SCOPES,
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT),
            cache_path=str(ROOT / ".spotify_cache"),
            open_browser=True,
        ))
        # True mientras Spotify esta reproduciendo algo: el microfono se pausa
        self.active = False

    def current(self) -> Optional[TrackInfo]:
        """Cancion actual de Spotify (bloqueante). None si no suena nada."""
        sent = time.monotonic()
        data = self.sp.current_user_playing_track()
        received = time.monotonic()
        if not data or not data.get("item") or data.get("currently_playing_type") != "track":
            return None  # nada sonando, o un podcast/anuncio

        item = data["item"]
        images = item.get("album", {}).get("images") or [{}]
        return TrackInfo(
            shazam_id=None,
            spotify_id=item.get("id") or item.get("uri"),  # temas locales no tienen id
            title=item["name"],
            # El primer artista: es el que mejor funciona para buscar letras
            artist=item["artists"][0]["name"] if item.get("artists") else "Desconocido",
            album=item.get("album", {}).get("name"),
            cover_url=images[0].get("url"),
            isrc=item.get("external_ids", {}).get("isrc"),
            duration_s=item["duration_ms"] / 1000 if item.get("duration_ms") else None,
            offset=(data.get("progress_ms") or 0) / 1000,
            # La posicion corresponde aprox. a la mitad del viaje de la peticion
            recorded_at=(sent + received) / 2,
            playing=bool(data.get("is_playing")),
        )

    async def listen(self) -> AsyncIterator[TrackInfo]:
        """Consulta Spotify cada pocos segundos y emite el estado actual.

        Emite tambien cuando la cancion no cambia, para corregir la posicion
        (pausas, saltos) en el orquestador.
        """
        warned = False
        while True:
            try:
                track = await asyncio.to_thread(self.current)
            except spotipy.SpotifyException as exc:
                self.active = False
                if exc.http_status == 403 and not warned:
                    log.error("Spotify rechazo la consulta (403). Desde 2026 las apps en modo "
                              "desarrollo requieren cuenta Premium y tu usuario debe estar en "
                              "la lista de usuarios de la app (dashboard > User Management).")
                    warned = True
                elif exc.http_status != 403:
                    log.warning("Error de Spotify: %s", exc)
                await asyncio.sleep(30)
                continue
            except Exception as exc:  # red caida, token invalido...
                self.active = False
                log.warning("No se pudo consultar Spotify: %s", exc)
                await asyncio.sleep(10)
                continue

            was_active = self.active
            self.active = bool(track and track.playing)
            if self.active != was_active:
                log.info("Spotify %s", "reproduciendo: se pausa el microfono" if self.active
                         else "sin reproduccion: vuelve el microfono")
            if track:
                yield track
            await asyncio.sleep(self.poll_seconds)


if __name__ == "__main__":
    if not spotify_configured():
        raise SystemExit("Falta SPOTIPY_CLIENT_ID en .env")
    now = SpotifyListener().current()
    if now is None:
        print("Spotify no esta reproduciendo nada ahora mismo.")
    else:
        state = "sonando" if now.playing else "en pausa"
        print(f"{now.artist} - {now.title} ({state})")
        print(f"  Album:    {now.album}")
        print(f"  Posicion: {now.offset:.1f} / {now.duration_s:.1f} s")
        print(f"  Portada:  {now.cover_url}")
