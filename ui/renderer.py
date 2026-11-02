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
# Si falta el PNG de un instrumento se dibuja un slime provisional, asi la
# pantalla funciona antes de tener todos los dibujos.
#
# El renderer no conoce al orquestador: recibe una funcion que devuelve el
# estado actual (o None) con: track (dict), lines, instruments, enriching
# y position() en segundos.
import asyncio
import colorsys
import math
import os
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
        self._slimes: dict[tuple[str, int], pygame.Surface] = {}
        self._fonts: dict[tuple[int, bool], pygame.font.Font] = {}
        self._bg = BG_DEFAULT
        self._bg_target = BG_DEFAULT
        self._track_id = None
        self._started = pygame.time.get_ticks()
        # Linea que muestra el globo; al cambiar se anima un "pop"
        self._bubble_index: Optional[int] = None
        self._bubble_changed_at = 0
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

    def _slime(self, instrument: str, size: int) -> pygame.Surface:
        key = (instrument, size)
        if key not in self._slimes:
            png = SLIMES_DIR / f"{instrument}.png"
            if png.exists():
                image = pygame.image.load(str(png)).convert_alpha()
                w, h = image.get_size()
                scale = size / max(w, h)
                surf = pygame.transform.smoothscale(image, (int(w * scale), int(h * scale)))
            else:
                surf = self._placeholder_slime(instrument, size)
            self._slimes[key] = surf
        return self._slimes[key]

    def _placeholder_slime(self, instrument: str, size: int) -> pygame.Surface:
        color = SLIME_COLORS.get(instrument, (120, 220, 120))
        surf = pygame.Surface((size, size), pygame.SRCALPHA)
        body = pygame.Rect(size * 0.08, size * 0.28, size * 0.84, size * 0.68)
        pygame.draw.ellipse(surf, _mix(color, (0, 0, 0), 0.35), body.move(0, size * 0.03))
        pygame.draw.ellipse(surf, color, body)
        shine = pygame.Rect(body.x + body.w * 0.18, body.y + body.h * 0.12, body.w * 0.22, body.h * 0.16)
        pygame.draw.ellipse(surf, _mix(color, (255, 255, 255), 0.6), shine)
        for ex in (0.36, 0.64):
            center = (int(size * ex), int(body.y + body.h * 0.45))
            pygame.draw.circle(surf, (30, 25, 40), center, max(2, size // 16))
            pygame.draw.circle(surf, (255, 255, 255),
                               (center[0] + size // 50, center[1] - size // 50), max(1, size // 50))
        mouth = pygame.Rect(size * 0.44, body.y + body.h * 0.58, size * 0.12, size * 0.08)
        pygame.draw.arc(surf, (30, 25, 40), mouth, math.pi, 2 * math.pi, max(1, size // 40))
        return surf

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

    def _draw_cover(self, state, w: int, h: int) -> float:
        """Portada grande centrada, titulo y artista debajo. Devuelve el y final."""
        track = state.track
        side = int(min(w * 0.8, h * 0.5))
        cover_rect = pygame.Rect(0, 0, side, side)
        cover_rect.midtop = (w // 2, int(h * 0.035))

        cover = self._cover(track.get("cover_path"), side)
        shadow = cover_rect.inflate(side // 30, side // 30).move(0, side // 50)
        pygame.draw.rect(self.screen, (0, 0, 0), shadow, border_radius=side // 18)
        if cover:
            self._bg_target = _soft_color(pygame.transform.average_color(cover))
            mask = pygame.Surface((side, side), pygame.SRCALPHA)
            pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=side // 22)
            rounded = cover.copy()
            rounded.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
            self.screen.blit(rounded, cover_rect)
        else:
            # Portada provisional: la inicial del artista
            self._bg_target = BG_DEFAULT
            pygame.draw.rect(self.screen, (70, 55, 110), cover_rect, border_radius=side // 22)
            self._text(track["artist"][:1].upper(), side // 2, (190, 170, 240),
                       cover_rect.center, bold=True)

        # Titulo y artista en una linea cada uno, debajo de la portada
        title_size, artist_size = max(18, self._u // 24), max(14, self._u // 36)
        max_w = int(w * 0.9)
        y = cover_rect.bottom + self._u * 0.012 + title_size * 0.7
        self._text(track["title"], title_size, TEXT, (w // 2, y), bold=True, max_width=max_w)
        y += title_size * 0.6 + artist_size * 0.9
        subtitle = track["artist"]
        status = track.get("lyrics_status")
        if not state.lines and not state.enriching:
            subtitle += "  ·  " + ("instrumental" if status == "instrumental" else "sin letra")
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
        count = len(instruments)
        slot = rect.w / count
        size = int(min(slot * 0.82, rect.h * 0.42, self._u * 0.2))
        t = (pygame.time.get_ticks() - self._started) / 1000
        bpm_phase = t * 2 * math.pi * 1.8  # ~108 bpm de "bailoteo"

        floor_y = rect.bottom - size * 0.42
        singer_x = None
        for i, instrument in enumerate(instruments):
            phase = bpm_phase + i * 1.3
            bounce = abs(math.sin(phase))
            squash = 1 + 0.10 * (1 - bounce)       # se aplasta al tocar el suelo
            stretch = 1 - 0.08 * (1 - bounce)
            sprite = self._slime(instrument, size)
            w, h = sprite.get_size()
            sw, sh = int(w * squash), int(h * stretch)
            frame = pygame.transform.smoothscale(sprite, (sw, sh))

            cx = rect.x + slot * (i + 0.5)
            if instrument == "vocals":
                singer_x = cx
            bottom = floor_y - bounce * size * 0.18
            shadow_w = int(sw * (0.75 - 0.25 * bounce))
            shadow = pygame.Rect(0, 0, shadow_w, max(4, size // 10))
            shadow.center = (int(cx), int(floor_y + size * 0.02))
            pygame.draw.ellipse(self.screen, _mix(self._bg, (0, 0, 0), 0.5), shadow)
            self.screen.blit(frame, frame.get_rect(midbottom=(int(cx), int(bottom))))
            self._text(LABELS.get(instrument, instrument), max(12, size // 6), TEXT_DIM,
                       (cx, floor_y + size * 0.22))

        bubble = self._bubble_text(state)
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
                frame_start = pygame.time.get_ticks()
                self.draw(get_state())
                # Esperar con asyncio (no con clock.tick) para no bloquear al detector
                elapsed = (pygame.time.get_ticks() - frame_start) / 1000
                await asyncio.sleep(max(0.0, 1 / FPS - elapsed))
        finally:
            pygame.quit()
