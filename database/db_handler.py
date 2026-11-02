# Consultas e inserciones en SQL/SQLite
#
# Cache local de canciones ya procesadas. La idea es que cada cancion pase
# por Shazam/LRCLIB/LLM una sola vez: la siguiente vez que suene, todo sale
# de aqui sin tocar ninguna API externa.
#
# Flujo tipico desde el orquestador:
#   db = DBHandler()
#   row = db.upsert_detection(track)         # crea o refresca metadatos
#   if row["lyrics_status"] == "pending":    -> buscar letra -> db.set_lyrics(...)
#   if row["instruments"] is None:           -> preguntar LLM -> db.set_instruments(...)
#   db.mark_played(row["id"])
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger("db_handler")

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "slime_band.db"

# Subir este numero y anadir el SQL correspondiente en _MIGRATIONS cuando
# cambie el esquema; las bases existentes se actualizan solas al arrancar.
SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE tracks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        shazam_id       TEXT UNIQUE,
        isrc            TEXT,
        title           TEXT NOT NULL,
        artist          TEXT NOT NULL,
        album           TEXT,
        duration_s      REAL,
        cover_url       TEXT,
        cover_path      TEXT,               -- copia local en ui/assets/covers
        lyrics_plain    TEXT,
        lyrics_synced   TEXT,               -- formato LRC: [mm:ss.xx] linea
        lyrics_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK (lyrics_status IN ('pending', 'found', 'not_found', 'instrumental')),
        instruments     TEXT,               -- array JSON, ej. ["guitar","drums"]
        play_count      INTEGER NOT NULL DEFAULT 0,
        last_played     TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_tracks_isrc ON tracks (isrc);
    CREATE INDEX idx_tracks_artist_title ON tracks (lower(artist), lower(title));
    """,
    2: """
    ALTER TABLE tracks ADD COLUMN spotify_id TEXT;
    CREATE UNIQUE INDEX idx_tracks_spotify ON tracks (spotify_id);
    """,
    # bpm: tempo aproximado para que los slimes bailen al ritmo (0 = desconocido)
    3: """
    ALTER TABLE tracks ADD COLUMN bpm REAL;
    """,
}

# Columnas que se pueden actualizar desde fuera (evita SQL con nombres arbitrarios)
_EDITABLE = {
    "shazam_id", "spotify_id", "isrc", "title", "bpm", "artist", "album", "duration_s", "cover_url",
    "cover_path", "lyrics_plain", "lyrics_synced", "lyrics_status", "instruments",
}


class DBHandler:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Una sola conexion compartida; el lock la protege si algun modulo
        # la usa desde asyncio.to_thread
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    # ------------------------------------------------------------------ esquema

    def _migrate(self) -> None:
        with self._lock, self._conn:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current + 1, SCHEMA_VERSION + 1):
                log.info("Aplicando migracion de esquema v%d", version)
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(f"PRAGMA user_version = {version}")

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- utilidades

    @staticmethod
    def _to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        if data.get("instruments"):
            data["instruments"] = json.loads(data["instruments"])
        return data

    def _fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return self._to_dict(row)

    def _update(self, track_id: int, **fields: Any) -> None:
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise ValueError(f"Columnas no editables: {unknown}")
        if "instruments" in fields and fields["instruments"] is not None:
            fields["instruments"] = json.dumps(fields["instruments"], ensure_ascii=False)
        assignments = ", ".join(f"{col} = ?" for col in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE tracks SET {assignments}, updated_at = datetime('now') WHERE id = ?",
                (*fields.values(), track_id),
            )

    # ------------------------------------------------------------------ lectura

    def get_by_id(self, track_id: int) -> Optional[dict[str, Any]]:
        return self._fetch_one("SELECT * FROM tracks WHERE id = ?", (track_id,))

    def get_by_shazam_id(self, shazam_id: str) -> Optional[dict[str, Any]]:
        return self._fetch_one("SELECT * FROM tracks WHERE shazam_id = ?", (shazam_id,))

    def find(self, artist: str, title: str) -> Optional[dict[str, Any]]:
        """Busqueda por artista + titulo, para unir detecciones de distintas fuentes.

        El artista puede venir parcial: Shazam da "A & B & C" y Spotify solo el
        primero ("A & B"), asi que basta con que uno contenga al otro.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE lower(title) = lower(?) ORDER BY id",
                (title.strip(),),
            ).fetchall()
        wanted = artist.strip().lower()
        for row in rows:
            stored = row["artist"].lower()
            if stored == wanted or wanted in stored or stored in wanted:
                return self._to_dict(row)
        return None

    def is_complete(self, track: dict[str, Any]) -> bool:
        """True si la cancion ya no necesita ninguna llamada externa."""
        return (track["lyrics_status"] != "pending" and track["instruments"] is not None
                and track.get("bpm") is not None)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE last_played IS NOT NULL "
                "ORDER BY last_played DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._to_dict(r) for r in rows]

    # ---------------------------------------------------------------- escritura

    def upsert_detection(self, track: Any) -> dict[str, Any]:
        """Guarda una deteccion (TrackInfo del microfono o de Spotify).

        Si la cancion ya existe (por shazam_id, spotify_id, ISRC o artista+titulo)
        solo rellena los metadatos que faltaban; nunca borra letra ni instrumentos.
        Asi una cancion detectada por microfono y luego por Spotify es la misma fila.
        """
        shazam_id = getattr(track, "shazam_id", None)
        spotify_id = getattr(track, "spotify_id", None)
        isrc = getattr(track, "isrc", None)

        existing = None
        if shazam_id:
            existing = self.get_by_shazam_id(shazam_id)
        if existing is None and spotify_id:
            existing = self._fetch_one("SELECT * FROM tracks WHERE spotify_id = ?", (spotify_id,))
        if existing is None and isrc:
            existing = self._fetch_one("SELECT * FROM tracks WHERE isrc = ?", (isrc,))
        if existing is None:
            existing = self.find(track.artist, track.title)

        incoming = {
            "shazam_id": shazam_id,
            "spotify_id": spotify_id,
            "isrc": isrc,
            "album": getattr(track, "album", None),
            "cover_url": getattr(track, "cover_url", None),
            "duration_s": getattr(track, "duration_s", None),
        }

        if existing:
            missing = {k: v for k, v in incoming.items() if v and not existing.get(k)}
            if missing:
                self._update(existing["id"], **missing)
                return self.get_by_id(existing["id"])
            return existing

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tracks (shazam_id, spotify_id, isrc, title, artist, album, "
                "duration_s, cover_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (shazam_id or None, spotify_id, isrc, track.title, track.artist, incoming["album"],
                 incoming["duration_s"], incoming["cover_url"]),
            )
        log.info("Nueva cancion en cache: %s - %s", track.artist, track.title)
        return self.get_by_id(cursor.lastrowid)

    def set_lyrics(self, track_id: int, plain: Optional[str] = None,
                   synced: Optional[str] = None, instrumental: bool = False) -> None:
        if instrumental:
            status = "instrumental"
        elif plain or synced:
            status = "found"
        else:
            status = "not_found"
        self._update(track_id, lyrics_plain=plain, lyrics_synced=synced, lyrics_status=status)

    def set_instruments(self, track_id: int, instruments: list[str],
                        bpm: Optional[float] = None) -> None:
        self._update(track_id, instruments=instruments, bpm=bpm or 0)

    def set_cover_path(self, track_id: int, path: str) -> None:
        self._update(track_id, cover_path=path)

    def reset_enrichment(self, track_id: int) -> None:
        """Fuerza a volver a pedir letra e instrumentos (si el LLM se equivoco)."""
        self._update(track_id, lyrics_plain=None, lyrics_synced=None,
                     lyrics_status="pending", instruments=None, bpm=None)

    def mark_played(self, track_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tracks SET play_count = play_count + 1, "
                "last_played = datetime('now') WHERE id = ?", (track_id,),
            )
