# Bucle de orquestacion principal
#
#   Spotify / microfono+Shazam -> cache SQLite -> (si falta) LRCLIB + LLM -> pantalla
#
# Tres tareas asyncio en paralelo:
#   - detector:  consulta Spotify y/o escucha el microfono; avisa al cambiar la cancion
#   - enricher:  completa portada, letra e instrumentos (solo lo que falte en cache)
#   - display:   pinta el estado actual (ventana Pygame, o consola con --console)
#
# Uso:
#   python main.py                                  # Spotify si suena algo, si no microfono
#   python main.py --source mic                     # solo microfono
#   python main.py --source spotify                 # solo Spotify
#   python main.py --device 1                       # elegir microfono
#   python main.py --fullscreen                     # modo cuadro
#   python main.py --size 600x1024                  # probar otra proporcion
#   python main.py --simulate "Queen" "Bohemian Rhapsody"   # sin microfono
import argparse
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from core import instrument_parser, lyrics_fetcher
from core.instrument_parser import InstrumentParser, DEFAULT_BAND
from core.lyrics_fetcher import LyricsFetcher, parse_lrc, current_line_index
from core.mic_listener import MicListener, TrackInfo
from database.db_handler import DBHandler
from utils.logger import get_logger

log = get_logger("main")

COVERS_DIR = Path(__file__).resolve().parent / "ui" / "assets" / "covers"
FPS = 10  # refresco del display en consola
DEFAULT_BPM = 110  # si el LLM no dio el tempo


@dataclass
class NowPlaying:
    """Estado compartido entre el orquestador y la pantalla."""
    detection: TrackInfo
    track: dict
    lines: list[tuple[float, str]] = field(default_factory=list)
    enriching: bool = True

    @property
    def instruments(self) -> list[str]:
        return self.track.get("instruments") or DEFAULT_BAND

    @property
    def bpm(self) -> float:
        return self.track.get("bpm") or DEFAULT_BPM

    @property
    def playing(self) -> bool:
        return self.detection.playing

    def position(self) -> float:
        return self.detection.position_now()

    def current_line(self) -> Optional[str]:
        if not self.lines:
            return None
        index = current_line_index(self.lines, self.position())
        return self.lines[index][1] if index >= 0 else None


class SlimeBand:
    def __init__(self, device: Optional[int] = None, source: str = "auto"):
        """source: "mic", "spotify" o "auto" (Spotify si suena algo, si no microfono)."""
        self.db = DBHandler()
        self.lyrics = LyricsFetcher()
        self.llm = InstrumentParser()
        self.now: Optional[NowPlaying] = None
        self._enrich_tasks: set[asyncio.Task] = set()
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

        self.spotify = None
        if source in ("spotify", "auto"):
            from core.streaming_client import SpotifyListener, spotify_configured
            if spotify_configured():
                self.spotify = SpotifyListener()
            elif source == "spotify":
                raise SystemExit("Falta SPOTIPY_CLIENT_ID en .env")
            else:
                log.info("Spotify no configurado: solo microfono")
        self.listener = MicListener(device=device) if source in ("mic", "auto") else None

    # ------------------------------------------------------------ deteccion

    async def detector(self) -> None:
        sources = []
        if self.spotify:
            sources.append(self._consume(self.spotify.listen()))
        if self.listener:
            # El microfono descansa mientras Spotify esta reproduciendo
            spotify_idle = lambda: not (self.spotify and self.spotify.active)
            sources.append(self._consume(self.listener.listen(should_listen=spotify_idle)))
        await asyncio.gather(*sources)

    async def _consume(self, detections) -> None:
        async for detection in detections:
            same = self.now is not None and detection.key == self.now.detection.key
            if same:
                self.resync(detection)
            elif detection.playing:
                await self.on_new_track(detection)
            # Una cancion en pausa (Spotify) solo sirve para congelar la que ya
            # se muestra; nunca reemplaza a lo que detecto el microfono

    def resync(self, detection: TrackInfo) -> None:
        """Misma cancion otra vez: corregir la posicion de la letra (y pausas)."""
        drift = detection.position_now() - self.now.position()
        paused_or_resumed = detection.playing != self.now.detection.playing
        # Spotify consulta cada 3 s: solo avisar de desfases reales
        if abs(drift) > 1.0 and not paused_or_resumed:
            source = "Spotify" if detection.spotify_id else "Shazam"
            log.info("Resincronizado: la letra iba %.1f s %s (%s dice %.1f s)", abs(drift),
                     "atrasada" if drift > 0 else "adelantada", source, detection.offset)
        self.now.detection = detection

    async def on_new_track(self, detection: TrackInfo) -> None:
        track = self.db.upsert_detection(detection)
        self.db.mark_played(track["id"])
        self.now = NowPlaying(detection, track, self._lines(track))

        if self.db.is_complete(track) and self._cover_ok(track):
            log.info("Desde cache (0 llamadas externas): %s - %s", track["artist"], track["title"])
            self.now.enriching = False
        else:
            # Si la cancion cambia a mitad, la tarea anterior no se cancela: termina
            # y deja la cache completa, solo que ya no es la que se muestra
            task = asyncio.create_task(self.enrich(self.now))
            self._enrich_tasks.add(task)
            task.add_done_callback(self._enrich_tasks.discard)

    # ---------------------------------------------------------- enriquecer

    @staticmethod
    def _lines(track: dict) -> list[tuple[float, str]]:
        return parse_lrc(track["lyrics_synced"]) if track.get("lyrics_synced") else []

    @staticmethod
    def _cover_ok(track: dict) -> bool:
        return not track.get("cover_url") or bool(
            track.get("cover_path") and Path(track["cover_path"]).exists())

    def _download_cover(self, track: dict) -> Optional[str]:
        path = COVERS_DIR / f"{track['id']}.jpg"
        try:
            resp = requests.get(track["cover_url"], timeout=10,
                                headers={"User-Agent": "SlimeBand/0.1 (display musical casero)"})
            resp.raise_for_status()
            path.write_bytes(resp.content)
            return str(path)
        except requests.RequestException as exc:
            log.warning("No se pudo descargar la portada: %s", exc)
            return None

    async def enrich(self, now: NowPlaying) -> None:
        track = now.track
        try:
            # Portada y letra son independientes: se piden a la vez
            jobs = {}
            if not self._cover_ok(track):
                jobs["cover"] = asyncio.to_thread(self._download_cover, track)
            if track["lyrics_status"] == "pending":
                jobs["lyrics"] = self.lyrics.fetch_async(
                    track["artist"], track["title"], track.get("album"), track.get("duration_s"))
            results = dict(zip(jobs, await asyncio.gather(*jobs.values())))

            if results.get("cover"):
                self.db.set_cover_path(track["id"], results["cover"])
            if "lyrics" in results:
                lyrics_fetcher.save_to_db(self.db, track["id"], results["lyrics"])

            # Instrumentos despues, para pasarle al LLM el inicio de la letra
            track = self.db.get_by_id(track["id"])
            # Tambien para canciones viejas de la cache que aun no tienen tempo
            if track["instruments"] is None or track.get("bpm") is None:
                result = await self.llm.detect_async(
                    track["artist"], track["title"], track.get("album"), track.get("lyrics_plain"))
                instrument_parser.save_to_db(self.db, track, result)
                track = self.db.get_by_id(track["id"])
                if result.source != "llm":
                    track["instruments"] = result.instruments  # mostrar, sin guardar

            now.track = track
            now.lines = self._lines(track)
        except Exception:
            log.exception("Error enriqueciendo %s - %s", track["artist"], track["title"])
        finally:
            now.enriching = False

    # ------------------------------------------------------------- pantalla

    async def console_display(self) -> None:
        """Display provisional hasta que exista ui/renderer.py con Pygame."""
        shown_track, shown_line, shown_enriching = None, None, None
        while True:
            now = self.now
            if now:
                if (now.track["id"], now.enriching) != (shown_track, shown_enriching):
                    shown_track, shown_enriching, shown_line = now.track["id"], now.enriching, None
                    t = now.track
                    print(f"\n=== {t['artist']} - {t['title']} ===")
                    print(f"  Portada:      {t.get('cover_path') or t.get('cover_url')}")
                    print(f"  Letra:        {t['lyrics_status']}"
                          f"{' (sincronizada)' if now.lines else ''}")
                    print(f"  Slimes:       {', '.join(now.instruments)}"
                          f"{'  [cargando...]' if now.enriching else ''}")
                line = now.current_line()
                if line and line != shown_line:
                    shown_line = line
                    print(f"  [{now.detection.position_now():6.1f}s] {line}")
            await asyncio.sleep(1 / FPS)

    # ----------------------------------------------------------------- run

    async def simulated_detector(self, artist: str, title: str) -> None:
        await self.on_new_track(TrackInfo(
            shazam_id="", title=title, artist=artist, recorded_at=time.monotonic()))
        await asyncio.Event().wait()  # seguir mostrando la letra hasta salir

    async def run(self, simulate: Optional[tuple[str, str]] = None, console: bool = False,
                  fullscreen: bool = False, size: Optional[tuple[int, int]] = None) -> None:
        if console:
            display = asyncio.create_task(self.console_display())
        else:
            from ui.renderer import Renderer  # pygame solo si hace falta
            renderer = Renderer(size=size, fullscreen=fullscreen)
            display = asyncio.create_task(renderer.run(lambda: self.now))
        detector = asyncio.create_task(
            self.simulated_detector(*simulate) if simulate else self.detector())
        try:
            # Termina cuando se cierra la ventana (o si el detector falla)
            done, _ = await asyncio.wait({display, detector}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # propaga excepciones
        finally:
            for task in (display, detector):
                task.cancel()
            self.db.close()


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("usa el formato ANCHOxALTO, ej. 800x800")
    return width, height


def main() -> None:
    parser = argparse.ArgumentParser(description="Slime Band")
    parser.add_argument("--source", choices=("auto", "mic", "spotify"), default="auto",
                        help="de donde sale la cancion (auto: Spotify si suena algo, si no microfono)")
    parser.add_argument("--device", type=int, help="indice del microfono (ver python -m core.mic_listener)")
    parser.add_argument("--simulate", nargs=2, metavar=("ARTISTA", "TITULO"),
                        help="simular una deteccion sin microfono")
    parser.add_argument("--fullscreen", action="store_true", help="pantalla completa (modo cuadro)")
    parser.add_argument("--console", action="store_true", help="mostrar en consola, sin ventana")
    parser.add_argument("--size", type=_parse_size, metavar="ANCHOxALTO",
                        help="tamano de ventana, ej. 800x800 (por defecto: cuadrada, ajustada al monitor)")
    args = parser.parse_args()

    print("Iniciando Slime Band... (Esc o Ctrl+C para salir)")
    try:
        asyncio.run(SlimeBand(device=args.device, source=args.source).run(
            args.simulate, args.console, args.fullscreen, args.size))
    except KeyboardInterrupt:
        print("\nHasta luego!")


if __name__ == "__main__":
    main()
