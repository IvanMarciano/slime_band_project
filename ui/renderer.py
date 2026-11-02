# Logica visual
#
# Pantalla pensada para un cuadro colgado en la pared:
#
#   +---------------------+
#   |    +-----------+    |
#   |    |  portada  |    |   portada grande centrada
#   |    +-----------+    |
#   |   titulo · artista  |
#   |  .-------------.    |
#   |  | letra actual |   |   globo de dialogo del slime que canta,
#   |  '---v---------'    |   con la linea actual y la siguiente en gris
#   |   (o)  (o)  (o)     |   slimes tocando
#   +---------------------+
#
# Todos los tamanos son relativos al lado corto de la pantalla, asi que
# funciona igual en un monitor, en la ventana o en la pantalla del cuadro.
#
# Animaciones: los slimes bailan al tempo de la cancion (bpm del LLM) y
# se paran si pausas la musica; cada familia tiene su movimiento (bateria
# salta, cuerdas se balancean, teclados laten, vientos se inclinan, la voz
# abre la boca al cantar), parpadean, sueltan notas musicales y entran
# cayendo al cambiar de cancion. La portada cambia con un fundido.
#
# Si falta el PNG de un instrumento se dibuja un slime provisional, asi la
# pantalla funciona antes de tener todos los dibujos. Con varios PNG
# (<instr>.png, <instr>_1.png, <instr>_2.png...) se alternan en cada pulso.
#
# El renderer no conoce al orquestador: recibe una funcion que devuelve el
# estado actual (o None) con: track (dict), lines, instruments, enriching,
# bpm, playing y position() en segundos.
import asyncio
import colorsys
import math
import os
import re
from pathlib import Path
from typing import Callable, Optional

import pygame

from core.lyrics_fetcher import current_line_index

ASSETS = Path(__file__).resolve().parent / "assets"
SLIMES_DIR = ASSETS / "slimes"

FPS = 30
WINDOW_FILL = 0.85               # ventana cuadrada = 85 % del lado corto del monitor
BG_DEFAULT = (24, 20, 38)
TEXT = (245, 242, 255)
TEXT_DIM = (150, 145, 175)
DEFAULT_BPM = 110
NOTE_LIFE = 1.8          # segundos que dura una nota flotando
COVER_FADE_MS = 450

# Como baila cada instrumento (ver Renderer._pose)
STYLES = {
    "vocals": "sing",
    "drums": "hop", "drum_machine": "hop", "percussion": "hop", "turntables": "hop",
    "electric_guitar": "sway", "acoustic_guitar": "sway", "bass": "sway",
    "ukulele": "sway", "violin": "sway", "cello": "sway",
    "piano": "pulse", "synth": "pulse", "accordion": "pulse",
    "trumpet": "blow", "saxophone": "blow", "flute": "blow", "harmonica": "blow",
}

# Color del slime provisional por instrumento
SLIME_COLORS = {
    "vocals": (255, 120, 170), "electric_guitar": (255, 90, 80),
    "acoustic_guitar": (230, 170, 90), "bass": (120, 110, 255),
    "drums": (255, 200, 60), "drum_machine": (80, 230, 230),
    "percussion": (255, 150, 60), "piano": (235, 235, 245),
    "synth": (180, 100, 255), "turntables": (60, 220, 140),
    "violin": (200, 120, 70), "cello": (160, 90, 60),
    "trumpet": (255, 215, 90), "saxophone": (240, 190, 70),
    "flute": (150, 220, 255), "accordion": (220, 80, 140),
    "harmonica": (170, 200, 210), "ukulele": (130, 220, 100),
}
LABELS = {
    "vocals": "voz", "electric_guitar": "guitarra", "acoustic_guitar": "acustica",
    "bass": "bajo", "drums": "bateria", "drum_machine": "beats",
    "percussion": "percusion", "piano": "piano", "synth": "synth",
    "turntables": "dj", "violin": "violin", "cello": "cello",
    "trumpet": "trompeta", "saxophone": "saxo", "flute": "flauta",
    "accordion": "acordeon", "harmonica": "armonica", "ukulele": "ukelele",
}


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ease_out_back(k: float) -> float:
    """0 -> 1 pasandose un poco y volviendo (rebote al caer)."""
    c = 1.70158
    return 1 + (c + 1) * (k - 1) ** 3 + c * (k - 1) ** 2


def _soft_color(color) -> tuple[int, int, int]:
    """Version oscura y poco saturada de un color, para fondo."""
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in color[:3]))
    r, g, b = colorsys.hls_to_rgb(h, 0.16, min(s, 0.45))
    return int(r * 255), int(g * 255), int(b * 255)


class Renderer:
    def __init__(self, size: Optional[tuple[int, int]] = None, fullscreen: bool = False):
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        # En Windows con escalado (125 %, 150 %...) usar pixeles reales: sin esto
        # la ventana sale mas grande que el monitor y se ve borrosa
        os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")
        pygame.init()
        pygame.display.set_caption("Slime Band")
        if fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
        else:
            if size is None:
                desk_w, desk_h = pygame.display.get_desktop_sizes()[0]
                side = int(min(desk_w, desk_h) * WINDOW_FILL)
                size = (side, side)  # cuadrada, como el cuadro final
            self.screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        self._u = min(self.screen.get_size())  # unidad de medida: lado corto
        self._covers: dict[str, pygame.Surface] = {}
        self._slimes: dict[tuple, pygame.Surface] = {}
        self._pngs: dict[str, list[pygame.Surface]] = {}
        self._rounded: dict[tuple, tuple] = {}
        self._note_cache: dict[tuple, pygame.Surface] = {}
        self._fonts: dict[tuple[int, bool], pygame.font.Font] = {}
        self._bg = BG_DEFAULT
        self._bg_target = BG_DEFAULT
        self._track_id = None
        self._started = pygame.time.get_ticks()
        # Linea que muestra el globo; al cambiar se anima un "pop"
        self._bubble_index: Optional[int] = None
        self._bubble_changed_at = 0
        # Animaciones: transicion de portada, entrada de la banda y notas
        self._cover_key = None
        self._cover_shown: Optional[pygame.Surface] = None
        self._prev_cover: Optional[pygame.Surface] = None
        self._cover_changed_at = 0
        self._band: Optional[list[str]] = None
        self._band_changed_at = 0
        self._notes: list[dict] = []
        self._last_beat: Optional[int] = None
        # Ajuste manual de sincronizacion (flechas izq/der), se reinicia por cancion
        self._nudge = 0.0
        self._nudge_shown_until = 0

    # ----------------------------------------------------------- recursos

    def font(self, size: int, bold: bool = False) -> pygame.font.Font:
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = pygame.font.SysFont("segoeui,dejavusans,arial", size, bold=bold)
        return self._fonts[key]

    def _cover(self, path: Optional[str], side: int) -> Optional[pygame.Surface]:
        if not path or not Path(path).exists():
            return None
        key = f"{path}@{side}"
        if key not in self._covers:
            try:
                image = pygame.image.load(path).convert()
            except pygame.error:
                return None
            self._covers[key] = pygame.transform.smoothscale(image, (side, side))
        return self._covers[key]

    def _png_frames(self, instrument: str) -> list[pygame.Surface]:
        """PNGs del instrumento: <instr>.png y opcionales <instr>_1.png, <instr>_2.png...
        Si hay varios, se alternan como cuadros de animacion, uno por pulso."""
        if instrument not in self._pngs:
            paths = [SLIMES_DIR / f"{instrument}.png"]
            numbered = [p for p in SLIMES_DIR.glob(f"{instrument}_*.png")
                        if re.fullmatch(rf"{re.escape(instrument)}_\d+", p.stem)]
            paths += sorted(numbered, key=lambda p: int(p.stem.rsplit("_", 1)[1]))
            self._pngs[instrument] = [pygame.image.load(str(p)).convert_alpha()
                                      for p in paths if p.exists()]
        return self._pngs[instrument]

    def _slime(self, instrument: str, size: int, frame: int = 0,
               blink: bool = False, mouth: bool = False) -> pygame.Surface:
        frames = self._png_frames(instrument)
        if frames:
            index = frame % len(frames)
            key = (instrument, size, index)
            if key not in self._slimes:
                image = frames[index]
                w, h = image.get_size()
                scale = size / max(w, h)
                self._slimes[key] = pygame.transform.smoothscale(
                    image, (int(w * scale), int(h * scale)))
        else:
            key = (instrument, size, blink, mouth)
            if key not in self._slimes:
                self._slimes[key] = self._placeholder_slime(instrument, size, blink, mouth)
        return self._slimes[key]

    def _placeholder_slime(self, instrument: str, size: int,
                           blink: bool = False, mouth: bool = False) -> pygame.Surface:
        color = SLIME_COLORS.get(instrument, (120, 220, 120))
        ink = (30, 25, 40)
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        body = pygame.Rect(size * 0.08, size * 0.28, size * 0.84, size * 0.68)
        pygame.draw.ellipse(surf, _mix(color, (0, 0, 0), 0.35), body.move(0, size * 0.03))
        pygame.draw.ellipse(surf, color, body)
        shine = pygame.Rect(body.x + body.w * 0.18, body.y + body.h * 0.12, body.w * 0.22, body.h * 0.16)
        pygame.draw.ellipse(surf, _mix(color, (255, 255, 255), 0.6), shine)
        eye_r = max(2, size // 16)
        for ex in (0.36, 0.64):
            center = (int(size * ex), int(body.y + body.h * 0.45))
            if blink:
                pygame.draw.line(surf, ink, (center[0] - eye_r, center[1]),
                                 (center[0] + eye_r, center[1]), max(2, size // 30))
            else:
                pygame.draw.circle(surf, ink, center, eye_r)
                pygame.draw.circle(surf, (255, 255, 255),
                                   (center[0] + size // 50, center[1] - size // 50), max(1, size // 50))
        if mouth:  # boca abierta cantando
            mouth_rect = pygame.Rect(0, 0, size * 0.15, size * 0.13)
            mouth_rect.center = (size // 2, int(body.y + body.h * 0.68))
            pygame.draw.ellipse(surf, ink, mouth_rect)
            tongue = pygame.Rect(0, 0, mouth_rect.w * 0.6, mouth_rect.h * 0.45)
            tongue.midbottom = (mouth_rect.centerx, mouth_rect.bottom - 1)
            pygame.draw.ellipse(surf, (240, 110, 140), tongue)
        else:
            smile = pygame.Rect(size * 0.44, body.y + body.h * 0.58, size * 0.12, size * 0.08)
            pygame.draw.arc(surf, ink, smile, math.pi, 2 * math.pi, max(1, size // 40))
        return surf

    @staticmethod
    def _pose(style: str, beat: float, singing: bool) -> tuple[float, float, float, float]:
        """Postura del slime en este instante del compas.

        Devuelve (salto en fracciones de su tamano, angulo en grados, escala x, escala y).
        """
        frac = beat % 1
        if style == "hop":       # bateria y ritmo: un salto por pulso, se aplasta al caer
            land = max(0.0, 1 - min(frac, 1 - frac) / 0.15)
            return 0.22 * math.sin(math.pi * frac), 0.0, 1 + 0.14 * land, 1 - 0.12 * land
        if style == "sway":      # cuerdas: se balancea de lado a lado
            return 0.05 * abs(math.sin(math.pi * beat)), 9 * math.sin(math.pi * beat), 1.0, 1.0
        if style == "pulse":     # teclados: late en cada golpe
            kick = math.exp(-frac * 5)
            return 0.04 * kick, 0.0, 1 + 0.09 * kick, 1 + 0.09 * kick
        if style == "blow":      # vientos: se echa atras y adelante cada 2 pulsos
            lean = math.sin(2 * math.pi * ((beat / 2) % 1))
            return 0.04 * max(lean, 0), -11 * lean, 1 - 0.03 * lean, 1 + 0.07 * lean
        # sing: se mece suave y se estira al "hablar"
        talk = 0.05 if singing and (beat * 2) % 1 < 0.5 else 0.0
        return (0.06 * abs(math.sin(math.pi * beat)), 3 * math.sin(math.pi * beat / 2),
                1 - talk * 0.5, 1 + talk)

    def _note_surface(self, color, size: int, double: bool) -> pygame.Surface:
        """Nota musical dibujada (corchea o dos corcheas unidas)."""
        key = (color, size, double)
        if key not in self._note_cache:
            surf = pygame.Surface((size, size), pygame.SRCALPHA)
            stem_w = max(2, size // 12)
            head = pygame.Rect(0, 0, size * 0.42, size * 0.3)
            heads = [(0.28, 0.78), (0.74, 0.70)] if double else [(0.36, 0.78)]
            tops = []
            for hx, hy in heads:
                head.center = (int(size * hx), int(size * hy))
                pygame.draw.ellipse(surf, color, head)
                stem_x = head.right - stem_w // 2 - 1
                top_y = int(size * (hy - 0.62))
                pygame.draw.line(surf, color, (stem_x, head.centery), (stem_x, top_y), stem_w)
                tops.append((stem_x, top_y))
            if double:
                pygame.draw.line(surf, color, tops[0], tops[1], stem_w * 2)
            else:
                x, y = tops[0]
                pygame.draw.lines(surf, color, False,
                                  [(x, y), (x + size * 0.22, y + size * 0.16), (x + size * 0.18, y + size * 0.36)],
                                  stem_w)
            self._note_cache[key] = surf
        return self._note_cache[key]

    def _draw_notes(self) -> None:
        now = pygame.time.get_ticks()
        alive = []
        for note in self._notes:
            age = (now - note["born"]) / 1000
            if age > NOTE_LIFE:
                continue
            alive.append(note)
            x = note["x"] + math.sin(age * 3 + note["phase"]) * note["size"] * 0.4
            y = note["y"] - age * note["size"] * 1.6
            fade = min(1.0, (NOTE_LIFE - age) / 0.6) * min(1.0, age / 0.15)
            surf = self._note_surface(note["color"], note["size"], note["double"])
            surf.set_alpha(int(255 * fade))
            self.screen.blit(surf, surf.get_rect(center=(int(x), int(y))))
            surf.set_alpha(255)
        self._notes = alive

    # ------------------------------------------------------------- dibujo

    @staticmethod
    def _ellipsize(text: str, font: pygame.font.Font, max_width: float) -> str:
        if font.size(text)[0] <= max_width:
            return text
        while font.size(text + "…")[0] > max_width and len(text) > 1:
            text = text[:-1]
        return text.rstrip() + "…"

    def _text(self, text: str, size: int, color, center, bold=False, max_width=None):
        font = self.font(size, bold)
        if max_width:
            text = self._ellipsize(text, font, max_width)
        surf = font.render(text, True, color)
        self.screen.blit(surf, surf.get_rect(center=center))

    def _wrap(self, text: str, font: pygame.font.Font, width: int) -> list[str]:
        words, rows, row = text.split(), [], ""
        for word in words:
            candidate = f"{row} {word}".strip()
            if font.size(candidate)[0] <= width or not row:
                row = candidate
            else:
                rows.append(row)
                row = word
        return rows + ([row] if row else [])

    def _draw_background(self, w: int, h: int) -> None:
        self._bg = _mix(self._bg, self._bg_target, 0.05)
        top, bottom = self._bg, _mix(self._bg, (0, 0, 0), 0.6)
        for y in range(0, h, 4):
            pygame.draw.rect(self.screen, _mix(top, bottom, y / h), (0, y, w, 4))

    def _cover_surface(self, track: dict, side: int):
        """Portada con esquinas redondeadas (cacheada) y su color medio."""
        path = track.get("cover_path")
        key = (path if path and Path(path).exists() else f"letra:{track['artist'][:1].upper()}", side)
        if key not in self._rounded:
            radius = side // 22
            surf = pygame.Surface((side, side), pygame.SRCALPHA)
            cover = self._cover(path, side)
            if cover:
                surf.blit(cover, (0, 0))
                mask = pygame.Surface((side, side), pygame.SRCALPHA)
                pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
                surf.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
                average = pygame.transform.average_color(cover)
            else:
                # Portada provisional: la inicial del artista
                pygame.draw.rect(surf, (70, 55, 110), surf.get_rect(), border_radius=radius)
                letter = self.font(side // 2, True).render(track["artist"][:1].upper(), True, (190, 170, 240))
                surf.blit(letter, letter.get_rect(center=(side // 2, side // 2)))
                average = None
            self._rounded[key] = (surf, average)
        return key, self._rounded[key]

    def _draw_cover(self, state, w: int, h: int) -> float:
        """Portada grande centrada, titulo y artista debajo. Devuelve el y final."""
        track = state.track
        side = int(min(w * 0.8, h * 0.5))
        cover_rect = pygame.Rect(0, 0, side, side)
        cover_rect.midtop = (w // 2, int(h * 0.035))

        key, (surf, average) = self._cover_surface(track, side)
        now = pygame.time.get_ticks()
        if key != self._cover_key:
            # Cambio de cancion o llego la portada: fundido desde la anterior
            self._prev_cover = self._cover_shown
            self._cover_key, self._cover_changed_at = key, now
        self._cover_shown = surf
        self._bg_target = _soft_color(average) if average else BG_DEFAULT

        shadow = cover_rect.inflate(side // 30, side // 30).move(0, side // 50)
        pygame.draw.rect(self.screen, (0, 0, 0), shadow, border_radius=side // 18)

        k = min((now - self._cover_changed_at) / COVER_FADE_MS, 1.0)
        if k >= 1:
            self.screen.blit(surf, cover_rect)
        else:
            ease = 1 - (1 - k) ** 3
            prev = self._prev_cover
            if prev is not None and prev.get_size() == surf.get_size():
                prev.set_alpha(int(255 * (1 - ease)))
                self.screen.blit(prev, cover_rect)
                prev.set_alpha(255)
            grown = int(side * (0.92 + 0.08 * ease))
            frame = pygame.transform.smoothscale(surf, (grown, grown))
            frame.set_alpha(int(255 * ease))
            self.screen.blit(frame, frame.get_rect(center=cover_rect.center))

        # Titulo y artista en una linea cada uno, debajo de la portada
        title_size, artist_size = max(18, self._u // 24), max(14, self._u // 36)
        max_w = int(w * 0.9)
        y = cover_rect.bottom + self._u * 0.012 + title_size * 0.7
        self._text(track["title"], title_size, TEXT, (w // 2, y), bold=True, max_width=max_w)
        y += title_size * 0.6 + artist_size * 0.9
        subtitle = track["artist"]
        status = track.get("lyrics_status")
        if not state.lines and not state.enriching:
            subtitle += "  ·  " + {"instrumental": "instrumental",
                                   "found": "letra sin sincronizar"}.get(status, "sin letra")
        self._text(subtitle, artist_size, TEXT_DIM, (w // 2, y), max_width=max_w)
        return y + artist_size

    def _bubble_text(self, state) -> Optional[tuple[str, Optional[str]]]:
        """(linea actual, linea siguiente) para el globo, o None si no hay globo."""
        if not state.lines:
            if state.enriching:
                return "." * (1 + (pygame.time.get_ticks() // 400) % 3), None
            return None
        index = current_line_index(state.lines, state.position() + self._nudge)
        if index != self._bubble_index:
            self._bubble_index = index
            self._bubble_changed_at = pygame.time.get_ticks()
        current = state.lines[index][1] if index >= 0 else ""
        upcoming = state.lines[index + 1][1] if index + 1 < len(state.lines) else None
        return current or "· · ·", upcoming  # linea vacia = pausa instrumental

    def _draw_bubble(self, text: str, upcoming: Optional[str], anchor_x: float,
                     bottom_y: float, w: int) -> None:
        """Globo de dialogo tipo comic, con la cola apuntando a (anchor_x, bottom_y)."""
        u = self._u
        size = max(16, u // 30)
        small = max(12, int(size * 0.72))
        pad = int(size * 0.7)
        max_text_w = int(min(w * 0.82, u * 0.92)) - 2 * pad

        rows = self._wrap(text, self.font(size, True), max_text_w)[:3]
        # La siguiente linea en gris, en una sola fila y solo si hay sitio
        next_rows = [self._ellipsize(upcoming, self.font(small), max_text_w)] \
            if upcoming and len(rows) < 3 else []
        widths = [self.font(size, True).size(r)[0] for r in rows]
        widths += [self.font(small).size(r)[0] for r in next_rows]
        text_w = max(widths + [size * 3])
        text_h = len(rows) * size * 1.25 + (len(next_rows) * small * 1.5 if next_rows else 0)

        bubble = pygame.Rect(0, 0, int(text_w + 2 * pad), int(text_h + 1.4 * pad))
        tail_h = int(size * 0.8)
        bubble.midbottom = (int(anchor_x), int(bottom_y - tail_h))
        margin = int(u * 0.03)
        bubble.left = max(margin, min(bubble.left, w - margin - bubble.w))

        # "Pop" al cambiar de linea: crece de 85 % a 100 % en ~150 ms
        age = pygame.time.get_ticks() - self._bubble_changed_at
        scale = 0.85 + 0.15 * min(age / 150, 1)

        layer = pygame.Surface((w, int(bottom_y) + 2), pygame.SRCALPHA)
        radius = int(size * 0.9)
        outline = max(2, u // 300)
        tail_x = max(bubble.left + radius, min(anchor_x, bubble.right - radius))
        tail = [(tail_x - size * 0.45, bubble.bottom - 2), (tail_x + size * 0.45, bubble.bottom - 2),
                (anchor_x, bottom_y)]
        pygame.draw.rect(layer, (30, 25, 45), bubble.inflate(outline * 2, outline * 2),
                         border_radius=radius + outline)
        pygame.draw.polygon(layer, (30, 25, 45), tail, 0)
        pygame.draw.rect(layer, (252, 250, 255), bubble, border_radius=radius)
        inner_tail = [(tail_x - size * 0.45 + outline * 1.5, bubble.bottom - 3),
                      (tail_x + size * 0.45 - outline * 1.5, bubble.bottom - 3),
                      (anchor_x, bottom_y - outline * 2.5)]
        pygame.draw.polygon(layer, (252, 250, 255), inner_tail, 0)

        y = bubble.y + pad * 0.7 + size * 0.62
        for row in rows:
            surf = self.font(size, True).render(row, True, (35, 30, 50))
            layer.blit(surf, surf.get_rect(center=(bubble.centerx, int(y))))
            y += size * 1.25
        for row in next_rows:
            y += small * 0.25
            surf = self.font(small).render(row, True, (150, 145, 170))
            layer.blit(surf, surf.get_rect(center=(bubble.centerx, int(y))))

        if scale < 1:
            # Escalar desde la punta de la cola, como si el globo saliera del slime
            sw, sh = int(layer.get_width() * scale), int(layer.get_height() * scale)
            scaled = pygame.transform.smoothscale(layer, (sw, sh))
            self.screen.blit(scaled, (anchor_x - anchor_x * scale, bottom_y - bottom_y * scale))
        else:
            self.screen.blit(layer, (0, 0))

    def _draw_band(self, state, rect: pygame.Rect) -> None:
        instruments = list(state.instruments)
        # Si hay letra, siempre hay un slime cantante (el LLM a veces lo omite)
        if state.lines and "vocals" not in instruments:
            instruments.insert(0, "vocals")
        now = pygame.time.get_ticks()
        if instruments != self._band:
            # Banda nueva (o el LLM respondio): los slimes entran saltando
            self._band, self._band_changed_at = instruments, now

        count = len(instruments)
        slot = rect.w / count
        size = int(min(slot * 0.82, rect.h * 0.42, self._u * 0.2))
        t = now / 1000

        # Reloj musical: pulsos desde el inicio de la cancion. Con la posicion
        # de Spotify/Shazam, al pausar la musica los slimes tambien se paran
        playing = getattr(state, "playing", True)
        bpm = getattr(state, "bpm", DEFAULT_BPM)
        beat = max(state.position() + self._nudge, 0.0) * bpm / 60

        bubble = self._bubble_text(state)
        singing = playing and bubble is not None and bubble[0][:1] not in (".", "·")

        floor_y = rect.bottom - size * 0.42
        singer_x = None
        heads = []
        for i, instrument in enumerate(instruments):
            style = STYLES.get(instrument, "hop")
            if playing:
                # Los que saltan se reparten en tiempo y contratiempo
                offset = 0.5 if style == "hop" and i % 2 else 0.0
                lift, angle, sx, sy = self._pose(style, beat + offset, singing)
            else:
                breath = math.sin(t * 2 + i)  # en pausa: solo respiran
                lift, angle, sx, sy = 0.0, 0.0, 1 + 0.015 * breath, 1 - 0.015 * breath

            # Entrada: caen desde arriba uno detras de otro, con rebote
            k = (now - self._band_changed_at - i * 90) / 450
            drop = 0.0 if k >= 1 else (1 - _ease_out_back(max(k, 0.0))) * size * 1.8

            blink = (t + i * 1.37) % 4.1 < 0.14
            mouth = instrument == "vocals" and singing and (beat * 2) % 1 < 0.5
            sprite = self._slime(instrument, size, int(beat) if playing else 0, blink, mouth)
            w, h = sprite.get_size()
            frame = pygame.transform.smoothscale(sprite, (max(1, int(w * sx)), max(1, int(h * sy))))
            if abs(angle) > 0.5:
                frame = pygame.transform.rotate(frame, angle)

            cx = rect.x + slot * (i + 0.5)
            bottom = floor_y - lift * size - drop
            # La sombra se achica cuando el slime esta en el aire
            air = min(lift / 0.22, 1.0) + min(drop / size, 1.0)
            shadow = pygame.Rect(0, 0, int(w * (0.8 - 0.35 * min(air, 1.0))), max(4, size // 10))
            shadow.center = (int(cx), int(floor_y + size * 0.02))
            pygame.draw.ellipse(self.screen, _mix(self._bg, (0, 0, 0), 0.5), shadow)
            self.screen.blit(frame, frame.get_rect(midbottom=(int(cx), int(bottom))))
            self._text(LABELS.get(instrument, instrument), max(12, size // 6), TEXT_DIM,
                       (cx, floor_y + size * 0.22))
            heads.append((cx, bottom - h * sy * 0.75, instrument))
            if instrument == "vocals":
                singer_x = cx

        # Una nota musical sale de un slime distinto en cada pulso
        whole = int(beat)
        if self._last_beat is None or whole < self._last_beat or not playing:
            self._last_beat = whole
        elif whole > self._last_beat:
            self._last_beat = whole
            x, y, instrument = heads[whole % count]
            self._notes.append({
                "x": x, "y": y, "born": now, "phase": whole * 1.7,
                "size": max(14, int(size * 0.3)), "double": whole % 3 == 0,
                "color": _mix(SLIME_COLORS.get(instrument, (120, 220, 120)), (255, 255, 255), 0.25),
            })
        self._draw_notes()

        if bubble and singer_x is not None:
            # La cola apunta a la cabeza del cantante en su punto mas alto
            head_y = floor_y - size * 0.18 - size * 0.62
            self._draw_bubble(*bubble, singer_x, head_y, self.screen.get_width())

    def _draw_idle(self, w: int, h: int) -> None:
        self._bg_target = BG_DEFAULT
        u = self._u
        t = pygame.time.get_ticks() / 1000
        sprite = self._slime("vocals", u // 3)
        y = h * 0.47 - abs(math.sin(t * 2.5)) * u * 0.03
        self.screen.blit(sprite, sprite.get_rect(midbottom=(w // 2, int(y))))
        dots = "." * (1 + int(t * 2) % 3)
        self._text(f"Escuchando{dots}", max(20, u // 18), TEXT, (w // 2, h * 0.47 + u * 0.1), bold=True)
        self._text("pon algo de musica", max(14, u // 28), TEXT_DIM, (w // 2, h * 0.47 + u * 0.17))

    def draw(self, state) -> None:
        w, h = self.screen.get_size()
        self._u = min(w, h)
        self._draw_background(w, h)
        if state is None:
            self._draw_idle(w, h)
        else:
            if state.track["id"] != self._track_id:
                self._track_id = state.track["id"]
                self._bubble_index = None
                self._nudge = 0.0
                self._notes.clear()
                self._last_beat = None
            text_bottom = self._draw_cover(state, w, h)
            band = pygame.Rect(0, int(text_bottom), w, h - int(text_bottom))
            self._draw_band(state, band)
            if pygame.time.get_ticks() < self._nudge_shown_until:
                self._text(f"sincronia {self._nudge:+.1f} s", max(14, self._u // 36), TEXT,
                           (w // 2, band.y + self._u * 0.02))
        pygame.display.flip()

    def nudge(self, seconds: float) -> None:
        """Adelanta (+) o atrasa (-) la letra respecto a la musica."""
        self._nudge += seconds
        self._nudge_shown_until = pygame.time.get_ticks() + 2000

    # ---------------------------------------------------------------- loop

    async def run(self, get_state: Callable[[], object]) -> None:
        """Bucle de pintado. Termina al cerrar la ventana o pulsar Esc/Q."""
        try:
            while True:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_q):
                            return
                        if event.key == pygame.K_RIGHT:
                            self.nudge(+0.5)
                        elif event.key == pygame.K_LEFT:
                            self.nudge(-0.5)
                    if event.type == pygame.VIDEORESIZE:
                        self._covers.clear()
                        self._slimes.clear()
                        self._rounded.clear()
                frame_start = pygame.time.get_ticks()
                self.draw(get_state())
                # Esperar con asyncio (no con clock.tick) para no bloquear al detector
                elapsed = (pygame.time.get_ticks() - frame_start) / 1000
                await asyncio.sleep(max(0.0, 1 / FPS - elapsed))
        finally:
            pygame.quit()
