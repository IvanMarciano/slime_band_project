# Busqueda de letras
#
# Usa LRCLIB (https://lrclib.net): gratis, sin API key y con letras
# sincronizadas en formato LRC ("[mm:ss.xx] linea"). Tambien incluye el
# parser LRC y el calculo de la linea actual que usara el renderer.
#
# Estrategia de busqueda:
#   1. /api/get con artista + titulo (+ album/duracion si se conocen)
#   2. /api/search y se elige el mejor candidato
#   3. Lo mismo con el titulo "limpio" (sin "feat.", "Remastered", etc.)
#
# Prueba rapida:  python -m core.lyrics_fetcher "Queen" "Bohemian Rhapsody"
import asyncio
import bisect
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from utils.logger import get_logger

log = get_logger("lyrics_fetcher")

API_URL = "https://lrclib.net/api"
# LRCLIB pide identificar la app en el User-Agent
USER_AGENT = "SlimeBand/0.1 (display musical casero)"
TIMEOUT = 10
RETRIES = 3

# Estados equivalentes a lyrics_status de la base de datos, mas "error"
# (fallo temporal: no se guarda, se reintentara la proxima vez)
FOUND, NOT_FOUND, INSTRUMENTAL, ERROR = "found", "not_found", "instrumental", "error"


@dataclass
class LyricsResult:
    status: str
    plain: Optional[str] = None
    synced: Optional[str] = None
    source_id: Optional[int] = None

    @property
    def lines(self) -> list[tuple[float, str]]:
        return parse_lrc(self.synced) if self.synced else []


class _TransientError(Exception):
    pass


# ---------------------------------------------------------------- formato LRC

_LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Convierte texto LRC en [(segundo, linea)] ordenado por tiempo.

    Soporta varias marcas por linea ("[00:12.00][01:30.00] coro").
    """
    lines: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        tags = _LRC_TAG.findall(raw)
        if not tags:
            continue
        text = _LRC_TAG.sub("", raw).strip()
        for minutes, seconds in tags:
            lines.append((int(minutes) * 60 + float(seconds), text))
    lines.sort(key=lambda item: item[0])
    return lines


def current_line_index(lines: list[tuple[float, str]], position: float) -> int:
    """Indice de la linea que suena en `position` segundos (-1 antes de empezar)."""
    times = [t for t, _ in lines]
    return bisect.bisect_right(times, position) - 1


# ------------------------------------------------------------ normalizacion

_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*(feat\.?|ft\.?|with|remaster|remastered|live|version|edit|mix)[^)\]]*[\)\]]"
    r"|\s+-\s+.*(remaster|live|version|edit|mix).*$",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song'"""
    return _NOISE.sub("", title).strip()


_LIVE = re.compile(r"\blive\b|en vivo|\bconcert|unplugged|\bsessions?\b|in new york|at wembley",
                   re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def primary_artist(artist: str) -> str:
    """'CA7RIEL & Paco Amoroso & Fred again..' -> 'CA7RIEL'"""
    return re.split(r"\s*(?:&|,|\bx\b|\bfeat\.?|\bft\.?|\by\b|\band\b)\s*", artist,
                    flags=re.IGNORECASE)[0].strip() or artist


def _same_artist(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a in b or b in a)


# --------------------------------------------------------------------- HTTP

class LyricsFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _request(self, endpoint: str, params: dict) -> Optional[dict | list]:
        """GET con reintentos. None si no hay resultado; _TransientError si no hubo forma.

        Ojo: LRCLIB responde 503 "ServerOverloaded" de forma sistematica cuando
        la cancion no existe (no un 404). Por eso un 503 repetido se trata como
        "sin resultado", y solo los fallos de red/otros 5xx cuentan como error.
        """
        only_503 = True
        for attempt in range(1, RETRIES + 1):
            try:
                resp = self.session.get(f"{API_URL}/{endpoint}", params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                only_503 = False
                log.warning("LRCLIB sin respuesta (%s), intento %d/%d", exc, attempt, RETRIES)
            else:
                if resp.status_code == 404:
                    return None
                if resp.ok:
                    return resp.json()
                if resp.status_code == 503 and attempt >= 2 and only_503:
                    log.debug("503 repetido en /%s: se asume que no existe", endpoint)
                    return None
                only_503 = only_503 and resp.status_code == 503
                log.debug("LRCLIB respondio %d, intento %d/%d", resp.status_code, attempt, RETRIES)
            time.sleep(attempt * 1.5)
        raise _TransientError(endpoint)

    @staticmethod
    def _score(candidate: dict, artist: str, title: str, duration: Optional[float]) -> float:
        score = 0.0
        if _same_artist(candidate.get("artistName", ""), artist):
            score += 4
        if _norm(clean_title(candidate.get("trackName", ""))) == _norm(clean_title(title)):
            score += 4
        if candidate.get("syncedLyrics"):
            score += 2
        if duration and candidate.get("duration"):
            diff = abs(candidate["duration"] - duration)
            score += 3 if diff <= 2 else (1 if diff <= 8 else -3)
        # Las versiones en vivo tienen otros tiempos: solo si el titulo lo pide
        album = candidate.get("albumName") or ""
        if _LIVE.search(album) and not _LIVE.search(title):
            score -= 2
        return score

    @staticmethod
    def _typical_duration(candidates: list[dict], artist: str, title: str) -> Optional[float]:
        """Duracion mas repetida entre las versiones de la cancion.

        Sin duracion (microfono) no se sabe que version suena; la de estudio
        suele ser la que aparece en mas discos (album, recopilatorios...)."""
        durations = [c["duration"] for c in candidates if c.get("duration")
                     and _same_artist(c.get("artistName", ""), artist)
                     and _norm(clean_title(c.get("trackName", ""))) == _norm(clean_title(title))]
        if not durations:
            return None
        return max(durations, key=lambda d: (sum(abs(d - o) <= 3 for o in durations), -d))

    def _pick(self, candidates: list[dict], artist: str, title: str,
              duration: Optional[float]) -> Optional[dict]:
        if not candidates:
            return None
        best = max(candidates, key=lambda c: self._score(c, artist, title, duration))
        # Exigir al menos que coincida el artista o el titulo
        return best if self._score(best, artist, title, None) >= 4 else None

    @staticmethod
    def _to_result(record: dict) -> LyricsResult:
        if record.get("instrumental"):
            return LyricsResult(INSTRUMENTAL, source_id=record.get("id"))
        plain = record.get("plainLyrics") or None
        synced = record.get("syncedLyrics") or None
        if not plain and synced:
            plain = "\n".join(text for _, text in parse_lrc(synced))
        status = FOUND if (plain or synced) else NOT_FOUND
        return LyricsResult(status, plain, synced, record.get("id"))

    def _lookup(self, artist: str, title: str, album: Optional[str],
                duration: Optional[float]) -> Optional[dict]:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = round(duration)

        record = self._request("get", params)
        # Con duracion conocida (Spotify) /get ya eligio la version correcta
        if record and record.get("syncedLyrics") and duration:
            return record

        candidates = self._request("search", {"artist_name": artist, "track_name": title}) or []
        if record:
            candidates.append(record)
        if not duration:
            duration = self._typical_duration(candidates, artist, title)
        return self._pick(candidates, artist, title, duration) or record

    def _free_text_search(self, artist: str, title: str,
                          duration: Optional[float]) -> Optional[dict]:
        """Ultimo recurso: busqueda libre (q=). Encuentra titulos con signos raros
        ('..FEISTY') o varios artistas que la busqueda por campos no encuentra."""
        bare_title = re.sub(r"[^\w\s']", " ", title).strip()
        queries = dict.fromkeys([f"{bare_title} {artist}", f"{bare_title} {primary_artist(artist)}"])
        for query in queries:
            log.info("Busqueda libre en LRCLIB: %r", query)
            record = self._pick(self._request("search", {"q": query}) or [], artist, title, duration)
            if record:
                return record
        return None

    def fetch(self, artist: str, title: str, album: Optional[str] = None,
              duration: Optional[float] = None) -> LyricsResult:
        """Busca la letra (bloqueante). Nunca lanza excepciones."""
        try:
            record = self._lookup(artist, title, album, duration)
            short = clean_title(title)
            if record is None and short and short != title:
                log.info("Reintentando con titulo limpio: %r", short)
                record = self._lookup(artist, short, None, duration)
            if record is None:
                record = self._free_text_search(artist, short or title, duration)
        except _TransientError:
            log.warning("LRCLIB no disponible ahora; se reintentara mas tarde")
            return LyricsResult(ERROR)
        except ValueError as exc:  # JSON invalido
            log.warning("Respuesta invalida de LRCLIB: %s", exc)
            return LyricsResult(ERROR)

        if record is None:
            log.info("Sin letra para %s - %s", artist, title)
            return LyricsResult(NOT_FOUND)

        result = self._to_result(record)
        log.info("Letra %s para %s - %s (sincronizada: %s, version LRCLIB: %r, %.0f s)",
                 result.status, artist, title, bool(result.synced),
                 record.get("albumName"), record.get("duration") or 0)
        return result

    async def fetch_async(self, artist: str, title: str, album: Optional[str] = None,
                          duration: Optional[float] = None) -> LyricsResult:
        return await asyncio.to_thread(self.fetch, artist, title, album, duration)


def save_to_db(db, track_id: int, result: LyricsResult) -> None:
    """Guarda el resultado en la cache. Los errores temporales no se guardan."""
    if result.status == ERROR:
        return
    db.set_lyrics(track_id, plain=result.plain, synced=result.synced,
                  instrumental=result.status == INSTRUMENTAL)


if __name__ == "__main__":
    artist_arg, title_arg = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("Queen", "Bohemian Rhapsody")
    res = LyricsFetcher().fetch(artist_arg, title_arg)
    print(f"\nEstado: {res.status}")
    for t, line in res.lines[:8]:
        print(f"  [{t:6.2f}] {line}")
    if not res.lines and res.plain:
        print(res.plain[:400])
