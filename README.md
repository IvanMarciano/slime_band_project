# Slime Band

Display musical para colgar en la pared como un cuadro: detecta la canción que suena y muestra la portada, la letra sincronizada en el globo de diálogo de un slime cantante, y una banda de slimes tocando los instrumentos reales de la canción.

## Cómo funciona

| Paso | Servicio | Costo |
|---|---|---|
| Qué suena | **Spotify** (tu cuenta) o **micrófono + Shazam** | gratis |
| Letra sincronizada | [LRCLIB](https://lrclib.net) | gratis, sin key |
| Instrumentos | Gemini / Groq / Ollama | gratis (free tier) |
| Caché | SQLite local | cada canción consulta las APIs una sola vez |

En modo automático usa Spotify mientras está reproduciendo y, si no, escucha por el micrófono (radio, vinilo, otro dispositivo...).

## Instalación (Windows)

1. Instala **Python 3.12** desde [python.org](https://www.python.org/downloads/release/python-31210/) (marca *Add python.exe to PATH*). Python 3.13+ todavía no es compatible con `shazamio`.
2. Descarga este repositorio (botón verde **Code → Download ZIP**, o `git clone`).
3. Haz doble clic en **`setup.bat`**: crea el entorno, instala todo y genera tu archivo `.env`.
4. Abre `.env` con el Bloc de notas y completa tus claves (ver abajo).
5. Haz doble clic en **`Slime Band.bat`**.

## Configuración (`.env`)

### Instrumentos (recomendado)

Saca una key gratis en [Google AI Studio](https://aistudio.google.com/apikey) y ponla en `LLM_API_KEY`. Sin key se muestra una banda por defecto.

### Conectar tu Spotify (opcional)

Cada persona inicia sesión **con su propia cuenta**: la primera vez que abres la app se abre el navegador para autorizar, y la sesión queda guardada solo en tu PC (`.spotify_cache`). Para cambiar de cuenta, borra ese archivo.

Spotify exige una "app de desarrollador" para leer qué estás escuchando. Hay dos formas:

**A. Crear tu propia app (recomendado)**
1. Entra en [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → **Create app**.
2. Redirect URI: `http://127.0.0.1:8888/callback` (exactamente así) y marca **Web API**.
3. Copia el **Client ID** en `SPOTIPY_CLIENT_ID` del `.env`. No hace falta el *client secret*.

**B. Usar la app de un amigo**
Pídele su Client ID y que te agregue en su app (**User Management**, con el email de tu cuenta de Spotify). Las apps en modo desarrollo admiten pocos usuarios.

> Desde 2026 Spotify pide cuenta **Premium** para las apps en modo desarrollo. Si tu cuenta es gratuita, la app sigue funcionando con el micrófono.

## Uso

```
Slime Band.bat                     ventana cuadrada
Slime Band.bat --fullscreen        pantalla completa (modo cuadro)
Slime Band.bat --source mic        solo micrófono
Slime Band.bat --source spotify    solo Spotify
```

Teclas: **← →** ajustan la sincronía de la letra, **Esc** cierra.

Pruebas sueltas:

```
.venv\Scripts\python -m core.mic_listener --nivel    volumen del micrófono en vivo
.venv\Scripts\python -m core.mic_listener            reconocer canciones por micrófono
.venv\Scripts\python -m core.streaming_client        qué suena en tu Spotify
```

## Slimes personalizados

Pon un PNG con fondo transparente por instrumento en `ui/assets/slimes/<instrumento>.png`. Instrumentos: `vocals`, `electric_guitar`, `acoustic_guitar`, `bass`, `drums`, `drum_machine`, `percussion`, `piano`, `synth`, `turntables`, `violin`, `cello`, `trumpet`, `saxophone`, `flute`, `accordion`, `harmonica`, `ukulele`. Los que falten se dibujan automáticamente.

## Estructura

```
main.py                    orquestador (detección -> caché -> letra/instrumentos -> pantalla)
core/streaming_client.py   Spotify (login PKCE por usuario)
core/mic_listener.py       micrófono + Shazam
core/lyrics_fetcher.py     letras de LRCLIB
core/instrument_parser.py  instrumentos con LLM
database/db_handler.py     caché SQLite
ui/renderer.py             pantalla Pygame
```
