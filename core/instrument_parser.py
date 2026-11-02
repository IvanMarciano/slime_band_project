# Prompting al LLM para obtener instrumentos
#
# Pregunta a un LLM gratuito que instrumentos suenan en una cancion y
# devuelve una lista de claves fijas (INSTRUMENTS). Cada clave corresponde
# a un PNG de slime en ui/assets/slimes/<clave>.png, asi que el LLM nunca
# puede inventar un instrumento que no tenga dibujo.
#
# Proveedores (todos con API compatible con OpenAI, se elige en .env):
#   groq   -> gratis, sin tarjeta, rapido        (LLM_API_KEY de console.groq.com)
#   gemini -> gratis, sin tarjeta                (LLM_API_KEY de aistudio.google.com)
#   ollama -> local y offline, sin key           (ollama serve en un PC de la casa)
#
# Prueba rapida:  python -m core.instrument_parser "Queen" "Bohemian Rhapsody"
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from dotenv import load_dotenv

from utils.logger import get_logger

log = get_logger("instrument_parser")
load_dotenv()

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "openai/gpt-oss-120b",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        # Alias que Google mueve al Flash-Lite mas reciente (los numerados caducan)
        "model": "gemini-flash-lite-latest",
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "model": "llama3.2",
    },
}

# Vocabulario cerrado: clave -> descripcion que ve el LLM.
# Para anadir un slime nuevo: anadir la clave aqui y su PNG en ui/assets/slimes/.
INSTRUMENTS: dict[str, str] = {
    "vocals": "lead or backing vocals, rap",
    "electric_guitar": "electric guitar (clean or distorted)",
    "acoustic_guitar": "acoustic, classical or Spanish guitar",
    "bass": "bass guitar, double bass or synth bass",
    "drums": "acoustic drum kit",
    "drum_machine": "drum machine, programmed beats, 808",
    "percussion": "congas, bongos, shakers, tambourine, timbales, guiro",
    "piano": "acoustic or electric piano",
    "synth": "synthesizers, keyboards, organ, pads",
    "turntables": "DJ, samples, scratching",
    "violin": "violin, viola, string section",
    "cello": "cello",
    "trumpet": "trumpet, horn section, brass",
    "saxophone": "saxophone",
    "flute": "flute, pan flute, recorder",
    "accordion": "accordion, bandoneon",
    "harmonica": "harmonica",
    "ukulele": "ukulele, cuatro, charango",
}

DEFAULT_BAND = ["vocals", "electric_guitar", "bass", "drums"]
MAX_INSTRUMENTS = 6

# Sinonimos frecuentes que los modelos devuelven aunque se les pida la clave exacta
_ALIASES = {
    "voice": "vocals", "vocal": "vocals", "singer": "vocals", "rap": "vocals",
    "guitar": "electric_guitar", "lead_guitar": "electric_guitar",
    "rhythm_guitar": "electric_guitar", "classical_guitar": "acoustic_guitar",
    "bass_guitar": "bass", "synth_bass": "bass", "double_bass": "bass",
    "drum_kit": "drums", "drumkit": "drums", "808": "drum_machine",
    "beats": "drum_machine", "keyboard": "synth", "keyboards": "synth",
    "organ": "synth", "synthesizer": "synth", "strings": "violin",
    "brass": "trumpet", "horns": "trumpet", "sax": "saxophone", "dj": "turntables",
}

SYSTEM_PROMPT = (
    "You are a music producer who knows the arrangement of recorded songs. "
    "Given a song, list the instruments clearly audible in the studio recording, "
    "ordered from most to least prominent, using ONLY these keys:\n"
    + "\n".join(f"- {key}: {desc}" for key, desc in INSTRUMENTS.items())
    + f"\n\nRules: at most {MAX_INSTRUMENTS} keys; include \"vocals\" only if someone sings or raps; "
    "set \"instrumental\" true if the track has no vocals; if you do not know the song, "
    "infer from the artist's usual style and set \"confidence\" to \"low\".\n"
    "Also give \"bpm\": the approximate tempo of the recording in beats per minute.\n"
    'Reply with JSON only: {"instruments": [...], "instrumental": false, "bpm": 120, '
    '"confidence": "high|medium|low"}'
)


@dataclass
class InstrumentResult:
    instruments: list[str] = field(default_factory=lambda: list(DEFAULT_BAND))
    instrumental: bool = False
    confidence: str = "low"
    # "llm" = respuesta valida, guardar en cache; "fallback" = banda por defecto, no guardar
    source: str = "fallback"
    # Tempo aproximado; los slimes bailan a este ritmo. None si el LLM no lo sabe
    bpm: Optional[float] = None


# --------------------------------------------------------------- validacion

def _normalize_key(raw: str) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if key in INSTRUMENTS:
        return key
    return _ALIASES.get(key)


def parse_response(text: str) -> Optional[InstrumentResult]:
    """Extrae y valida el JSON del modelo. None si no sirve."""
    match = re.search(r"\{.*\}", text, re.DOTALL)  # tolera ```json ... ``` y texto extra
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    raw_list = data.get("instruments")
    if not isinstance(raw_list, list):
        return None

    instruments: list[str] = []
    for item in raw_list:
        key = _normalize_key(str(item))
        if key and key not in instruments:
            instruments.append(key)
        elif not key:
            log.debug("Instrumento descartado (sin slime): %r", item)

    instrumental = bool(data.get("instrumental", False))
    if instrumental and "vocals" in instruments:
        instruments.remove("vocals")
    if not instruments:
        return None

    confidence = str(data.get("confidence", "medium")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    try:
        bpm = float(data.get("bpm"))
    except (TypeError, ValueError):
        bpm = None
    if bpm is not None and not 40 <= bpm <= 220:
        bpm = None
    return InstrumentResult(instruments[:MAX_INSTRUMENTS], instrumental, confidence, "llm", bpm)


# --------------------------------------------------------------------- HTTP

class InstrumentParser:
    def __init__(self, provider: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 60):
        # El free tier de Gemini puede tardar 15-30 s en horas de mucha demanda
        self.provider = (provider or os.getenv("LLM_PROVIDER", "groq")).lower()
        if self.provider not in PROVIDERS:
            raise ValueError(f"LLM_PROVIDER desconocido: {self.provider}")
        config = PROVIDERS[self.provider]
        self.url = os.getenv("LLM_BASE_URL", config["url"])
        self.model = model or os.getenv("LLM_MODEL") or config["model"]
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.timeout = timeout
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @property
    def available(self) -> bool:
        return self.provider == "ollama" or bool(self.api_key)

    def _ask(self, user_prompt: str, retries: int = 3) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("LLM sin respuesta (%s), intento %d/%d", exc, attempt, retries)
                time.sleep(2 * attempt)
                continue

            if resp.status_code == 429:  # limite del free tier
                wait = float(resp.headers.get("retry-after", 5 * attempt))
                log.warning("Rate limit del LLM, esperando %.0f s", wait)
                time.sleep(min(wait, 60))
                continue
            if not resp.ok:
                log.warning("LLM respondio %d: %s", resp.status_code, resp.text[:200])
                if resp.status_code < 500:
                    return None  # key invalida, modelo inexistente...: reintentar no sirve
                time.sleep(2 * attempt)
                continue

            try:
                return resp.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError):
                log.warning("Respuesta del LLM con formato inesperado")
                return None
        return None

    def detect(self, artist: str, title: str, album: Optional[str] = None,
               lyrics_excerpt: Optional[str] = None) -> InstrumentResult:
        """Devuelve los instrumentos (bloqueante). Nunca lanza excepciones."""
        if not self.available:
            log.warning("Sin LLM_API_KEY: se usa la banda por defecto")
            return InstrumentResult()

        prompt = f"Song: \"{title}\" by {artist}"
        if album:
            prompt += f"\nAlbum: {album}"
        if lyrics_excerpt:
            # Unas lineas ayudan a distinguir versiones y a detectar instrumentales
            prompt += "\nFirst lyrics lines:\n" + "\n".join(lyrics_excerpt.splitlines()[:4])

        content = self._ask(prompt)
        result = parse_response(content) if content else None
        if result is None:
            log.warning("No se pudieron obtener instrumentos para %s - %s", artist, title)
            return InstrumentResult()

        log.info("Instrumentos de %s - %s: %s (%s)", artist, title,
                 result.instruments, result.confidence)
        return result

    async def detect_async(self, artist: str, title: str, album: Optional[str] = None,
                           lyrics_excerpt: Optional[str] = None) -> InstrumentResult:
        return await asyncio.to_thread(self.detect, artist, title, album, lyrics_excerpt)


def save_to_db(db, track: dict, result: InstrumentResult) -> None:
    """Guarda en la cache solo respuestas reales del LLM (no la banda por defecto).

    Si el LLM dice que es instrumental y LRCLIB no encontro letra, se corrige
    el estado de la letra para que la UI muestre "instrumental".
    """
    if result.source != "llm":
        return
    db.set_instruments(track["id"], result.instruments, result.bpm)
    if result.instrumental and track.get("lyrics_status") == "not_found":
        db.set_lyrics(track["id"], instrumental=True)


if __name__ == "__main__":
    artist_arg, title_arg = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("Queen", "Bohemian Rhapsody")
    parser = InstrumentParser()
    print(f"Proveedor: {parser.provider} / modelo: {parser.model}")
    res = parser.detect(artist_arg, title_arg)
    print(f"\n{res}")
