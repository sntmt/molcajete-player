# Molcajete Player

Molcajete Player es un reproductor de video **sin audio** para sistemas Linux (y entornos de desarrollo en macOS), orquestado desde Python y ejecutado mediante el framework multimedia GStreamer a traves de los bindings de PyGObject.

El sistema permite ingerir listas de reproduccion y metadatos en formato JSON. Soporta la reproduccion inmediata de flujos remotos (por ejemplo, HLS desde Mux) mientras gestiona en segundo plano la descarga de archivos definitivos (*renditions* MP4 alojados en Amazon S3), conmutando de forma automatica a los archivos locales en disco una vez completada su descarga.

## Arquitectura y Caracteristicas

El reproductor esta disenado para cubrir tanto entornos de desarrollo local como despliegues en produccion:

- **Sin audio, siempre**: la pista de audio de los videos nunca se reproduce. `playbin` se configura sin el flag de audio, con un `fakesink` como `audio-sink` y en `mute`, de modo que no se abre ningun flujo hacia PulseAudio/PipeWire/ALSA. No existe ninguna opcion para activarlo.
- **Cambio de video sin huecos (pre-buffer)**: unos segundos antes de que termine un video, `playbin` emite `about-to-finish` y el reproductor carga en ese momento la URI del siguiente elemento, que se prepara mientras el actual sigue en pantalla. El cambio ocurre sin pasar por `READY`, por lo que no se congela el ultimo fotograma. La URI precargada se elige en ese instante: si el *rendition* del siguiente elemento ya esta verificado en disco se usa `file://`; si no, su `remote_url`.
- **Descarga anticipada**: al empezar cada elemento se inicia tambien la descarga del `s3_url` del siguiente (desactivable con `--no-prefetch`), para que este en disco cuando le toque.

- **Entornos locales y kiosko con ventana persistente (`auto` / `gtk`)**:
  Incrusta el video en una unica ventana nativa persistente de GTK 3 con fondo negro (utilizando `gtksink` si esta instalado, o asociando directamente el `XID` de un `Gtk.DrawingArea` mediante `GstVideo.VideoOverlay` sobre `xvimagesink`/`glimagesink`, con `ctypes` sobre `libgstvideo-1.0` como respaldo si no esta instalado el typelib `GstVideo`). De este modo, la ventana **nunca se cierra ni se recrea** al cambiar de un video a otro de la lista. La incrustacion por `XID` solo se usa en sesiones X11; en Wayland sin `gtksink` se recurre a `autovideosink`.
- **Produccion sin entorno grafico (`kms` / `fbdev`)**:
  En servidores o dispositivos embebidos Linux sin servidor X11 ni compositor Wayland, el reproductor puede enviar la senal de video directamente al subsistema DRM/KMS del kernel (`kmssink`) o al dispositivo de framebuffer (`fbdevsink`).
- **Modo headless (`fakesink`)**:
  Permite ejecutar y verificar el flujo de reproduccion y descarga sin salida visual, ideal para pruebas automatizadas o servidores sin pantalla.
- **Flujo hibrido seguro Stream -> S3 -> Cache Local de Kiosko** (ver [documentacion detallada y diagramas Mermaid](docs/stream-s3-switch.md)):
  1. **Directorio de cache aislado y nombres deterministas**: En despliegues tipo kiosko *on-site*, el reproductor no confia en nombres ni rutas arbitrarias provenientes del JSON para guardar descargas. Todos los *renditions* se almacenan dentro de un directorio controlado (`--cache-dir` o `~/.cache/molcajete-player/media`) bajo un nombre determinista `<id_sanitizado>_<hash16>.mp4`, bloqueando cualquier intento de *Path Traversal* (`..`) o colision de archivos. Al arrancar se eliminan los *renditions* que ya no pertenecen a la lista y los `.part` huerfanos (desactivable con `--no-prune-cache`).
  2. Si existe el archivo preinstalado de `local_path` o el archivo del elemento ya existe integro en el cache local, se reproduce directamente (`file://`) sin consumo de red. Un `local_path` existente tambien evita la descarga desde `s3_url`.
  3. Si el archivo local aun no existe, el reproductor inicia de inmediato la reproduccion del flujo en `remote_url` (p. ej. HLS/MP4 remoto).
  4. Simultaneamente, si se especifica `s3_url` (incluyendo soporte nativo para **S3 Presigned URLs** con firmas temporales y exigencia de `https://`), `MediaDownloader` descarga el *rendition* en segundo plano hacia un archivo oculto temporal `.<nombre>.part`, reintentando ante respuestas HTTP `403`, `404`, `500`, `502`, `503` o `504`. Las redirecciones se validan en cada salto (se rechaza pasar de `https://` a `http://`), y la descarga se cancela si supera `max_bytes` (8 GiB por defecto) o dejaria menos de `min_free_bytes` (256 MiB) libres en disco.
  5. **Verificacion criptografica SHA-256**: Si el elemento define `sha256` (o `checksum`), el descargador calcula el resumen SHA-256 en vuelo y descarta automaticamente cualquier descarga corrupta o manipulada antes de activarla en disco.
  6. Al completarse y verificarse la descarga, el archivo `.part` reemplaza atomicamente al destino en el cache y, la siguiente vez que el elemento se precargue (o se seleccione), el reproductor usa `file://`. El resultado de la verificacion se recuerda por `(tamano, mtime)`, de modo que el SHA-256 no se recalcula en cada vuelta del bucle salvo que el archivo cambie.
- **Tolerancia a fallos y gestion de red**:
  Control de *buffering* de red (pausa mientras el bufer esta por debajo del 100 % y reanuda al llenarse, sin anular una pausa manual ni afectar a streams en directo) (los mensajes de buffering del elemento precargado se ignoran para no congelar el que esta en pantalla) y proteccion contra bucles infinitos: si un elemento falla (error en el bus o sin URI resoluble), se salta y se continua con el siguiente; si todos los elementos de la lista fallan de forma consecutiva, detiene la ejecucion con un codigo de error fatal.
- **Logs sin secretos**: las URLs con *query string* (firmas S3, tokens de Mux) se registran como `?<redacted>`, tambien dentro de los mensajes de error de GStreamer.

## Estructura del Codigo

| Modulo | Responsabilidad |
| :--- | :--- |
| `src/molcajete_player/env.py` | `sanitize_snap_env()`: limpia variables inyectadas por Snap. Se invoca en `main()` antes de importar `gi`; importar el paquete no modifica el entorno. |
| `src/molcajete_player/models.py` | `MediaItem` y `Playlist` (dataclasses): validacion de campos (IDs unicos, `loop` booleano, `metadata` objeto), normalizacion de `sha256`, nombres de cache deterministas, `confine_local_path()`, `verify_integrity()` y `get_effective_uri()`. Utilidades `sanitize_media_id`, `canonicalize_url`, `extract_safe_extension`, `redact_url_for_log` y `redact_urls_in_text`. |
| `src/molcajete_player/loader.py` | `load_playlist_from_file()` (con `media_root` opcional) y `load_playlist_from_string()`. `load_playlist_from_json()` se mantiene por compatibilidad. |
| `src/molcajete_player/downloader.py` | `MediaDownloader`: descarga cada `s3_url` en un hilo *daemon* (`s3-downloader-<id>`) con reintentos, verificacion SHA-256 en vuelo, limites de tamano/espacio y reemplazo atomico `.part` -> destino. Expone `prepare()`, `discard_cached()`, `prune_cache()`, `clear_cache()`, `resume()` y `stop()`. `validate_download_url()` exige HTTPS, tambien en cada redireccion. |
| `src/molcajete_player/player.py` | `MolcajetePlayer`: tuberia `playbin` sin audio, seleccion del sumidero de video, ventana Gtk persistente, precarga sin huecos (`about-to-finish` + `STREAM_START`), bus de mensajes (EOS, errores, buffering), navegacion de la lista y cierre ordenado ante senales. |
| `src/molcajete_player/contract.py` | Contrato con el agente del dispositivo (sin GStreamer): `ContractEnv` (entorno `MOLCAJETE_APP_*`), `program_to_playlist()` (traduce y valida el programa de la plataforma), lectura de `control.json`, formato de `status.json` y escritura atomica. |
| `src/molcajete_player/controller.py` | `ContractController`: vigila cada segundo `program.json` y `control.json`, aplica los cambios en el reproductor y mantiene `status.json`. |
| `src/molcajete_player/main.py` | CLI (`build_parser()`, `main()`), modo contrato (`run_contract()`) y codigos de salida. |

### Modelo de hilos
- **Hilo principal**: ejecuta el bucle de eventos (`Gtk.main()` o `GLib.MainLoop`). Todos los mensajes del bus de GStreamer, los cambios de elemento y los callbacks `on_eos`/`on_error`/`on_item_start` se procesan aqui.
- **Hilo de streaming de GStreamer**: ejecuta `about-to-finish`, que solo elige y carga la siguiente URI (puede verificar un archivo de cache la primera vez). El cambio se confirma en el hilo principal al recibir `STREAM_START`.
- **Hilos de descarga**: uno por elemento con `s3_url` pendiente. El callback `on_download_complete` de `MediaDownloader` se ejecuta **en ese hilo**. Para reaccionar desde el bucle principal (p. ej. tocar GStreamer o Gtk), usa el parametro `on_download_complete` de `MolcajetePlayer`, que reenvia el evento con `GLib.idle_add`.
- **Cierre**: `SIGINT` (Ctrl+C), `SIGTERM` y `SIGHUP` (cerrar la terminal) se atienden con `GLib.unix_signal_add` y siguen el mismo camino que cerrar la ventana. `MolcajetePlayer.stop()` pasa la tuberia a `NULL` en primer lugar y despues detiene el descargador; `play()` y `run()` lo reactivan con `MediaDownloader.resume()`.

### Documentacion adicional
- [Flujo Stream -> S3 -> Cache local](docs/stream-s3-switch.md): diagramas de decision y secuencia.
- [Servidor de prueba y simulacion](docs/test-server-simulation.md): funcionamiento de `examples/simulate_mux_s3_switch.py`.
- [Revision de codigo](docs/code-review.md): hallazgos de la revision del 2026-10-19 y como se resolvio cada uno.

## Requisitos del Sistema (Linux y macOS)

Para utilizar GStreamer, GTK 3 y sus bindings de Python es necesario contar con las librerias nativas de GStreamer y GObject Introspection.

Distribuciones basadas en Debian y Ubuntu:
```bash
sudo apt update
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gtk-3.0 gstreamer1.0-gtk3 \
    gstreamer1.0-tools libgstreamer1.0-dev \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav
```

Distribuciones basadas en Fedora y RHEL:
```bash
sudo dnf install -y python3-gobject gstreamer1 gstreamer1-devel \
    gtk3 gstreamer1-plugin-gtk3 \
    gstreamer1-plugins-base gstreamer1-plugins-good \
    gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free \
    gstreamer1-libav
```

Arch Linux:
```bash
sudo pacman -S python-gobject gstreamer gst-plugins-base \
    gtk3 gst-plugin-gtk \
    gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav
```

macOS (mediante Homebrew):
```bash
brew install python pygobject3 gtk+3 gstreamer
```

## Configuracion del Entorno Python

Se requiere Python `>= 3.10`. En sistemas Linux se recomienda crear el entorno virtual con acceso a los paquetes del sistema para aprovechar las librerias de PyGObject instaladas por el gestor de paquetes de la distribucion (si `pip` intenta compilar `PyGObject` desde fuente, necesitara ademas `libgirepository1.0-dev`, `libcairo2-dev` y `pkg-config`).:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Si se desea instalar el paquete en modo editable (lo que habilita el comando `molcajete-player`) junto con las herramientas de desarrollo (`pytest`, `ruff`):

```bash
pip install -e ".[dev]"
```

## Estructura del Archivo de Metadatos JSON

El archivo JSON define el nombre de la lista, el comportamiento de repeticion continua y el catalogo de elementos multimedia (tambien puede cargarse desde una cadena JSON en memoria mediante `load_playlist_from_string`):

```json
{
  "name": "Lista de Reproduccion de Demostracion",
  "loop": true,
  "items": [
    {
      "id": "jellyfish-720p",
      "title": "Jellyfish 1280x720 (10s)",
      "remote_url": "https://test-videos.co.uk/vids/jellyfish/mp4/h264/720/Jellyfish_720_10s_1MB.mp4",
      "s3_url": "https://test-videos.co.uk/vids/jellyfish/mp4/h264/720/Jellyfish_720_10s_1MB.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=demo2",
      "sha256": "245730569c806849a3c2b942cbe31056efc76f32fd8bee79780986e19c9a6057",
      "duration": 10.011,
      "metadata": {
        "aspect_ratio": "16:9",
        "resolution": "1280x720"
      }
    }
  ]
}
```

### Campos de la lista (`Playlist`)
- `name`: Nombre identificativo de la lista de reproduccion (obligatorio).
- `loop`: Booleano que indica si la lista debe repetirse indefinidamente al llegar al final (opcional, por defecto `false`). Solo admite `true`/`false` (o las cadenas `"true"`/`"false"`); cualquier otro valor es un error.
- `items`: Lista de objetos multimedia `MediaItem` (opcional, por defecto `[]`). Los `id` deben ser unicos dentro de la lista.

### Campos de cada elemento (`MediaItem`)
- `id`: Identificador unico del video (obligatorio). Se sanitiza automaticamente para generar nombres de archivo deterministas en el cache local.
- `title`: Titulo descriptivo del video (obligatorio, no puede ser `null`).
- `remote_url`: URL de transmision remota (p. ej. HLS `.m3u8` o MP4 por HTTP) utilizada cuando el archivo aun no se encuentra descargado en el cache local (opcional).
- `s3_url` (o alias `download_url`): URL HTTPS de descarga del archivo MP4 definitivo (*rendition* en S3, compatible con **S3 Presigned URLs**). `MediaDownloader` lo descargara en segundo plano dentro del directorio de cache controlado con nombre determinista `<id_sanitizado>_<hash16>.mp4` (opcional).
- `sha256` (o alias `checksum`): Hash hexadecimal SHA-256 de 64 caracteres esperado para el archivo descargado desde `s3_url`. Si se especifica, el archivo se verifica criptograficamente antes de activarse en disco y antes de reproducirse desde el cache (opcional).
- `local_path`: Ruta a un archivo local preinstalado (opcional). Siempre se rechazan rutas con `..`. Con `--media-root DIR`, la ruta (resolviendo enlaces simbolicos) debe quedar dentro de `DIR` y las rutas relativas se interpretan respecto a el. Si el archivo existe, tiene prioridad sobre el cache y sobre `remote_url`, y evita la descarga de `s3_url`. La ruta de cache que asigna `MediaDownloader` se guarda aparte (`cache_path`) y nunca sobrescribe `local_path`.
- `duration`: Duracion nominal en segundos (opcional).
- `metadata`: Objeto libre con informacion contextual adicional (opcional; debe ser un objeto JSON).

> Nota: Cada elemento debe contar con al menos `remote_url` o `local_path` para poder resolverse una URI de reproduccion valida. Un elemento con solo `s3_url` es aceptado (se registra una advertencia al iniciar), pero no es reproducible hasta que termine su descarga: mientras tanto se salta y la lista continua con el siguiente elemento.

> Nota de seguridad: sin `--media-root`, `local_path` admite rutas absolutas, por lo que el JSON de la lista debe considerarse una fuente de confianza. Si la lista proviene de un origen externo, usa `--media-root`.

### Listas de ejemplo
Todas las fuentes son 16:9 y de uso publico para pruebas:

| Archivo | Elemento | Fuente | Resolucion |
| :--- | :--- | :--- | :--- |
| `examples/sample_playlist.json` | `flower-540p` | Time-lapse de una flor, [MDN cc0-videos](https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4) (CC0) | 960x540, 5 s |
| | `jellyfish-720p` | Medusas, [test-videos.co.uk](https://test-videos.co.uk/jellyfish/mp4-h264) | 1280x720, 10 s |
| | `jellyfish-1080p` | Medusas, [test-videos.co.uk](https://test-videos.co.uk/jellyfish/mp4-h264) | 1920x1080, 10 s |
| `examples/test_stream.json` | `mux-pts-shift` | `test-streams.mux.dev/pts_shift` (catalogo de pruebas de hls.js mantenido por Mux), PTS desplazados 2,3 s | ABR hasta 1280x720, 2:45 |
| | `mux-arte-china` | `test-streams.mux.dev/test_001` (ARTE China) | ABR hasta 640x360, 8:30 |
| | `apple-bipbop-16x9` | Patron de prueba BipBop de Apple | ABR hasta 1920x1080, 30:00 |

`sample_playlist.json` sube de resolucion en cada cambio (540p -> 720p -> 1080p) para ejercitar la renegociacion del sumidero de video durante el cambio sin huecos. Las `s3_url` son las mismas URLs con parametros de firma S3 ficticios; los `sha256` corresponden al contenido real servido el 2026-10-19. Mux no publica MP4 de prueba sin una cuenta propia: sus *static renditions* (`https://stream.mux.com/{PLAYBACK_ID}/highest.mp4`) requieren un asset con MP4 habilitado.

## Uso por Linea de Comandos

### Inicio Rapido con `run.sh`

El script `./run.sh` configura automaticamente `PYTHONPATH=src`, utiliza `.venv` si existe y limpia las variables de entorno de Snap (`GTK_PATH`, `GIO_MODULE_DIR`, `LD_LIBRARY_PATH`, etc., cuando se ejecuta desde la terminal integrada de VS Code Snap) para evitar conflictos de librerias `glibc`/`libpthread`. Sin argumentos, ejecuta la lista de ejemplo con `--clean-cache` para partir de una cache limpia; en el resto de casos no borra nada salvo que se pase `--clean-cache` explicitamente. `--clean-cache` solo elimina archivos con el patron de nombres del reproductor (`<id>_<hash16>.<ext>` y `.<...>.part`), nunca otros archivos ni el propio directorio.

```bash
# Ejecuta por defecto examples/sample_playlist.json (clips MP4 16:9 de imagen real) con --sink auto
./run.sh

# Ejecutar la lista de streams HLS de prueba (Mux y Apple, todos 16:9)
./run.sh examples/test_stream.json

# Ejecutar la simulacion end-to-end (Mux Stream -> S3 Presigned URL + SHA-256 -> Cache Local)
./run.sh --sim

# Pasar opciones adicionales sobre la lista por defecto
./run.sh --sink fakesink -v

# Igual, pero vaciando antes la cache del reproductor
./run.sh --sink fakesink --clean-cache
```

### Ejecucion Manual con Python

Tambien puede invocarse directamente mediante el modulo `molcajete_player.main` o con el binario `molcajete-player` (si se instalo el paquete):

Reproduccion en entorno local con interfaz grafica automatica:
```bash
PYTHONPATH=src python3 -m molcajete_player.main examples/sample_playlist.json --sink auto
```

Reproduccion forzando ventana nativa GTK 3:
```bash
PYTHONPATH=src python3 -m molcajete_player.main examples/test_stream.json --sink gtk
```

Reproduccion en produccion sin entorno grafico (salida DRM/KMS):
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink kms --cache-dir /var/cache/molcajete
```

Reproduccion en framebuffer directo:
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink fbdev
```

Modo headless de verificacion sin salida visual:
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink fakesink
```

### Opciones de CLI
- `metadata`: Ruta al archivo JSON con los metadatos y la lista de reproduccion. Sin ella, el reproductor arranca en [modo contrato con el agente](#modo-contrato-con-el-agente-del-dispositivo).
- `--sink {auto,gtk,kms,fbdev,fakesink}`: Selecciona el sumidero de video (`auto` por defecto).
- `--loop` / `--no-loop`: Fuerza o desactiva la reproduccion ciclica de la lista, sobreescribiendo el valor del JSON (sin ninguno de los dos se respeta el JSON).
- `--cache-dir RUTA`: Directorio aislado de cache donde almacenar y verificar los *renditions* descargados desde S3 (por defecto `~/.cache/molcajete-player/media` o el valor de la variable de entorno `MOLCAJETE_CACHE_DIR`).
- `--clean-cache`: Elimina antes de empezar todos los *renditions* y temporales del reproductor en el directorio de cache (los demas archivos se conservan).
- `--no-prune-cache`: No elimina al arrancar los *renditions* en cache que ya no pertenecen a la lista (util si varias listas comparten el mismo directorio de cache).
- `--media-root DIR`: Exige que todos los `local_path` del JSON queden dentro de `DIR`.
- `--no-prefetch`: No inicia por adelantado la descarga del `s3_url` del siguiente elemento.
- `--allow-insecure-http`: **Solo desarrollo.** Acepta descargas por `http://` hacia cualquier host (por ejemplo el Storage local visto desde el contenedor del dispositivo simulado, `http://host.docker.internal:54321`). Sin esta opcion solo se admite `https://` (y `http://` hacia loopback), tambien en cada redireccion; en produccion no se usa.
- `-v, --verbose`: Activa trazas detalladas de depuracion (`DEBUG`), incluyendo el progreso de *buffering* y cambios de estado de la tuberia GStreamer.

### Codigos de Salida
- `0`: Ejecucion finalizada correctamente.
- `1`: Archivo JSON no encontrado, con formato/estructura invalida o con un `local_path` fuera de `--media-root`.
- `2`: GStreamer no disponible en el entorno o error al inicializar la tuberia.
- `3`: Error inesperado durante la ejecucion del bucle principal (se registra el *traceback* completo).
- `4`: Error fatal de reproduccion (todos los elementos de la lista fallaron consecutivamente). En modo contrato no ocurre: el reproductor espera y reintenta.

## Modo contrato con el agente del dispositivo

En el dispositivo, el reproductor no lo lanza una persona sino el **agente** (el programa que habla con la plataforma). El agente no sabe nada del reproductor: lo arranca **sin opciones**, con un entorno estandar, y se comunican por tres ficheros JSON en una carpeta (decision ADR 0015 de `molcajete-ng`). Asi el reproductor se puede actualizar sin tocar el agente.

Sin archivo de metadatos y con este entorno, `molcajete-player` arranca en modo contrato:

| Variable | Que es |
| :--- | :--- |
| `MOLCAJETE_APP_CONTRACT` | Version del contrato. Hoy `1`; con otra, el reproductor no arranca (codigo `1`). |
| `MOLCAJETE_APP_DATA` | Su carpeta (la escribe el reproductor): `status.json` y la cache de videos (`cache/`). |
| `MOLCAJETE_APP_INBOX` | Carpeta de entrada (solo la escribe el agente; el reproductor solo la lee): `program.json` y `control.json`. Sin ella, van en `MOLCAJETE_APP_DATA`. |
| `MOLCAJETE_VIDEO_OUTPUT` | Salida de video que elige el agente: `kms`, `gtk` o `fakesink` (otro valor: `auto`). |
| `MOLCAJETE_DEV_INSECURE_HTTP` | Solo desarrollo, `1`: admite descargas por `http://` (el Storage local). |

Ficheros del contrato (todos se escriben de forma atomica: temporal en la misma carpeta y `rename`):

- **`program.json`** (lo escribe el agente): el programa **tal cual llega de la plataforma**, `{"program_hash", "manifest": {"version": 2, "clips": [{"clip_id", "title", "stream_url", "download_url", "duration_s"}]}}`. El reproductor lo traduce a su lista (en bucle) y lo valida: version de manifiesto soportada, URLs `https` (`http` solo con `MOLCAJETE_DEV_INSECURE_HTTP`), `clip_id` unicos, al menos una URL por clip, `duration_s` positiva, y **nunca** toma una ruta local de la plataforma. Si lo cambia el agente, el reproductor pasa al programa nuevo **sin reiniciarse** (de momento corta el video en curso); si lo rechaza, **sigue con el anterior**.
- **`control.json`** (lo escribe el agente): `{"playback": "playing" | "paused"}`. Sin fichero, `playing`.
- **`status.json`** (lo escribe el reproductor): lo que hace de verdad, y lo unico que el agente informa a la plataforma:

```json
{
  "program_hash": "prog-2",
  "error": {"program_hash": "prog-3", "reason": "clips[0].stream_url must be a https URL."},
  "playback": "playing",
  "updated_at": "2026-10-23T14:07:09+00:00"
}
```

`program_hash` es el programa que se esta reproduciendo; `error` (o `null`) dice que programa posterior se rechazo y por que; `playback` es `playing`, `paused` o `null` (sin programa, programa vacio o todos los elementos fallando). Se reescribe al cambiar algo y, como latido, cada 30 s.

En este modo el reproductor **no se cierra** porque fallen todos los elementos: espera 30 s y lo vuelve a intentar (si se cerrara, el agente lo relanzaria en bucle). Tampoco limpia la cache al arrancar: arranca sin lista y el programa llega despues, asi que la limpieza borraria todo lo descargado.

Probarlo a mano (con `fakesink`; el "agente" eres tu escribiendo los ficheros):

```bash
mkdir -p /tmp/app && cat > /tmp/app/program.json <<'JSON'
{"program_hash": "demo", "manifest": {"version": 2, "clips": [
  {"clip_id": 1, "title": "Flor", "stream_url": "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"}]}}
JSON
MOLCAJETE_APP_CONTRACT=1 MOLCAJETE_APP_DATA=/tmp/app MOLCAJETE_VIDEO_OUTPUT=fakesink \
  PYTHONPATH=src python3 -m molcajete_player.main &
cat /tmp/app/status.json                                   # program_hash "demo", playback "playing"
echo '{"playback": "paused"}' > /tmp/app/control.json      # al segundo, playback "paused"
```

## Simulacion End-to-End (Stream Mux -> S3 -> Disco Local)

El repositorio incluye el script `examples/simulate_mux_s3_switch.py`, que verifica de extremo a extremo la transicion entre el stream remoto y el archivo descargado desde S3 sin depender de servicios externos:

1. Genera dos videos MP4 de prueba (~3s cada uno) con `gst-launch-1.0`:
   - Patron de barras SMPTE para simular el stream remoto de Mux.
   - Patron de pelota en movimiento para simular el *rendition* alojado en S3.
2. Levanta un servidor HTTP local que sirve `/mux/stream.mp4` de inmediato y simula en `/s3/rendition.mp4` un estado `404 Not Found` en el primer intento (transcodificacion en curso) y `200 OK` a partir del segundo intento.
3. Ejecuta `MolcajetePlayer` en bucle y, mediante el callback `on_item_start`, verifica que el primer ciclo reproduce desde `http://` mientras descarga el *rendition* y que un ciclo posterior (el primero que se precarga con el archivo ya verificado) usa `file://`.

Ejecucion con ventana grafica:
```bash
PYTHONPATH=src python3 examples/simulate_mux_s3_switch.py --sink auto
```

Ejecucion en modo headless (sin ventana):
```bash
PYTHONPATH=src python3 examples/simulate_mux_s3_switch.py --sink fakesink
```

## Ejecucion de Pruebas

Para ejecutar la bateria de pruebas unitarias con `unittest`:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

O utilizando `pytest` (si se instalaron las dependencias de desarrollo):

```bash
pytest
```

Las pruebas nunca tocan la cache real del usuario: usan directorios temporales y fijan `MOLCAJETE_CACHE_DIR` al invocar `run.sh`.

Analisis estatico (configurado en `[tool.ruff]` de `pyproject.toml`):

```bash
ruff check src tests examples
```

El flujo de GitHub Actions `.github/workflows/ci.yml` ejecuta `ruff` y la bateria de pruebas con el Python y los bindings de GStreamer del sistema en cada *push* a `main`/`develop` y en cada *pull request*.

