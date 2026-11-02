# Captura de audio y wrapper de shazamio
#
# Graba fragmentos cortos del microfono, descarta el silencio para no gastar
# llamadas y pregunta a Shazam que cancion suena. Solo avisa cuando la
# cancion cambia, para que el orquestador no reprocese la misma pista.
#
# Prueba rapida:  python -m core.mic_listener           (reconocer canciones)
#                 python -m core.mic_listener --nivel   (ver volumen en vivo)
import asyncio
import io
import sys
import time
import wave
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

import numpy as np
import sounddevice as sd
from shazamio import Shazam

from utils.logger import get_logger

log = get_logger("mic_listener")

SAMPLE_RATE = 44100
CHANNELS = 1
RECORD_SECONDS = 8        # con 6 s fallaba seguido con musica a volumen de ambiente
LISTEN_INTERVAL = 15      # segundos entre intentos mientras suena la misma cancion
SILENCE_RMS = 200         # por debajo de esto (int16) se considera silencio


@dataclass
class TrackInfo:
    """Cancion detectada, venga del microfono (Shazam) o de Spotify."""
    shazam_id: Optional[str]
    title: str
    artist: str
    album: Optional[str] = None
    cover_url: Optional[str] = None
    isrc: Optional[str] = None
    # Segundo de la cancion en el instante recorded_at
    offset: float = 0.0
    recorded_at: float = field(default_factory=time.monotonic)
    spotify_id: Optional[str] = None
    duration_s: Optional[float] = None
    playing: bool = True

    @property
    def key(self) -> str:
        """Identificador para saber si sigue sonando la misma cancion."""
        return self.shazam_id or self.spotify_id or f"{self.artist}|{self.title}".lower()

    def position_now(self) -> float:
        """Posicion estimada (s) dentro de la cancion, para sincronizar la letra."""
        if not self.playing:
            return self.offset  # en pausa: la letra se queda quieta
        return self.offset + (time.monotonic() - self.recorded_at)


def _to_wav_bytes(samples: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)  # int16
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def _parse_result(result: dict, recorded_at: float) -> Optional[TrackInfo]:
    track = result.get("track")
    if not track:
        return None

    album = None
    for section in track.get("sections", []):
        for meta in section.get("metadata", []):
            if meta.get("title") == "Album":
                album = meta.get("text")

    images = track.get("images", {})
    matches = result.get("matches") or [{}]

    return TrackInfo(
        shazam_id=str(track.get("key")),
        title=track.get("title", "Desconocido"),
        artist=track.get("subtitle", "Desconocido"),
        album=album,
        cover_url=images.get("coverarthq") or images.get("coverart"),
        isrc=track.get("isrc"),
        offset=float(matches[0].get("offset", 0.0)),
        recorded_at=recorded_at,
    )


class MicListener:
    def __init__(self, device: Optional[int | str] = None,
                 record_seconds: float = RECORD_SECONDS,
                 silence_rms: float = SILENCE_RMS):
        self.device = device
        self.record_seconds = record_seconds
        self.silence_rms = silence_rms
        self.shazam = Shazam(language="es-ES")

    def _record(self) -> tuple[np.ndarray, float]:
        """Graba (bloqueante) y devuelve las muestras y el instante de inicio."""
        frames = int(self.record_seconds * SAMPLE_RATE)
        started = time.monotonic()
        samples = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
                         dtype="int16", device=self.device)
        sd.wait()
        return samples, started

    def _is_silence(self, samples: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        log.debug("RMS del fragmento: %.1f", rms)
        return rms < self.silence_rms

    async def identify(self) -> Optional[TrackInfo]:
        """Graba un fragmento y lo identifica. None si hay silencio o no hay match."""
        samples, started = await asyncio.to_thread(self._record)
        if self._is_silence(samples):
            log.info("Silencio, no se consulta a Shazam")
            return None

        try:
            result = await self.shazam.recognize(_to_wav_bytes(samples))
        except Exception as exc:  # red caida, rate limit de Shazam, etc.
            log.warning("Error consultando Shazam: %s", exc)
            return None

        track = _parse_result(result, started)
        if track is None:
            log.info("Shazam no reconocio la cancion")
        return track

    async def listen(self, interval: float = LISTEN_INTERVAL,
                     should_listen: Callable[[], bool] = lambda: True) -> AsyncIterator[TrackInfo]:
        """Escucha en bucle y emite un TrackInfo en cada reconocimiento.

        Si es la misma cancion que antes tambien se emite: sirve para volver a
        sincronizar la letra con el offset nuevo que da Shazam.
        `should_listen` permite pausar el microfono (ej. mientras suena Spotify).
        """
        current_id: Optional[str] = None
        while True:
            if not should_listen():
                current_id = None
                await asyncio.sleep(2)
                continue
            track = await self.identify()
            if track and not should_listen():
                continue  # otra fuente (Spotify) tomo el control mientras grababa
            if track:
                if track.shazam_id != current_id:
                    current_id = track.shazam_id
                    log.info("Nueva cancion: %s - %s (Shazam offset %.1f s)",
                             track.artist, track.title, track.offset)
                yield track
            # Si no se reconocio nada se reintenta antes para pillar el cambio rapido
            await asyncio.sleep(interval if track else 2)


def list_input_devices() -> None:
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"[{index}] {dev['name']}")


def level_meter(device: Optional[int] = None, seconds: float = 30) -> None:
    """Muestra el volumen del microfono en vivo, para comprobar que capta sonido."""
    print(f"Microfono: {sd.query_devices(device, 'input')['name']}")
    print(f"Umbral de silencio: {SILENCE_RMS} (la barra debe pasar la '|' con musica)\n")
    width = 50
    mark = min(width - 1, int(SILENCE_RMS / 3000 * width))
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", device=device) as stream:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            samples, _ = stream.read(SAMPLE_RATE // 10)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            filled = min(width, int(rms / 3000 * width))
            bar = "".join("#" if i < filled else ("|" if i == mark else ".") for i in range(width))
            state = "SONIDO " if rms >= SILENCE_RMS else "silencio"
            print(f"\r[{bar}] {rms:7.0f}  {state}", end="", flush=True)
    print()


async def _demo() -> None:
    print("Microfonos disponibles:")
    list_input_devices()
    print("\nEscuchando... (Ctrl+C para salir)")
    async for track in MicListener().listen():
        print(f"\n>> {track.artist} - {track.title}")
        print(f"  Album:   {track.album}")
        print(f"  Portada: {track.cover_url}")
        print(f"  Posicion estimada: {track.position_now():.1f} s")


if __name__ == "__main__":
    try:
        if "--nivel" in sys.argv:
            level_meter()
        else:
            asyncio.run(_demo())
    except KeyboardInterrupt:
        pass
