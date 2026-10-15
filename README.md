# Molcajete Player

Molcajete Player es un reproductor de contenido audiovisual para sistemas Linux, orquestado desde Python y ejecutado mediante el framework multimedia GStreamer a traves de los bindings de PyGObject.

El sistema permite ingerir listas de reproduccion y metadatos en formato JSON. Soporta la reproduccion directa de flujos remotos provistos por un servicio de transcodificacion y streaming en la nube mientras se completa la descarga de los archivos, y conmuta de forma automatica a los archivos locales una vez que estos se encuentran disponibles en el disco.

## Arquitectura y Caracteristicas

El reproductor esta disenado para cubrir tanto entornos de desarrollo local como despliegues en produccion:

Entornos locales con servidor grafico:
En sistemas de escritorio (como distribuciones Linux con X11 o Wayland, o estaciones de desarrollo locales), el reproductor utiliza la salida automatica de video (autovideosink) para renderizar el contenido en ventanas del sistema operativo.

Produccion sin entorno grafico:
En servidores o dispositivos embebidos Linux sin servidor X11 ni compositor Wayland, el reproductor puede enviar la senal de video directamente al subsistema DRM/KMS del kernel (kmssink) o al dispositivo de framebuffer (fbdevsink).

Doble via de contenido (remoto y local):
Cada elemento multimedia en la lista JSON puede declarar tanto una ruta local como una URL remota. El reproductor evalua la existencia del archivo en disco: si ya esta descargado, lo reproduce localmente con minima latencia; si aun no esta presente, consume la transmision remota provista por el servicio cloud de streaming.

## Requisitos del Sistema en Linux

Para utilizar GStreamer y sus bindings de Python en Linux es necesario contar con las librerias nativas de GStreamer y GObject Introspection.

Distribuciones basadas en Debian y Ubuntu:
```bash
sudo apt update
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools libgstreamer1.0-dev \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav
```

Distribuciones basadas en Fedora y RHEL:
```bash
sudo dnf install -y python3-gobject gstreamer1 gstreamer1-devel \
    gstreamer1-plugins-base gstreamer1-plugins-good \
    gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free \
    gstreamer1-libav
```

Arch Linux:
```bash
sudo pacman -S python-gobject gstreamer gst-plugins-base \
    gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav
```

## Configuracion del Entorno Python

En sistemas Linux se recomienda crear el entorno virtual con acceso a los paquetes del sistema para aprovechar las librerias de PyGObject instaladas por el gestor de paquetes de la distribucion:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Si se desea instalar el paquete en modo desarrollo:

```bash
pip install -e .
```

## Estructura del Archivo de Metadatos JSON

El archivo JSON define el nombre de la lista, el comportamiento de repeticion continua y el catalogo de elementos multimedia:

```json
{
  "name": "Canal Principal",
  "loop": true,
  "items": [
    {
      "id": "item-001",
      "title": "Video de Demostracion",
      "local_path": "media/demo.mp4",
      "remote_url": "https://stream.proveedor-cloud.example.com/demo.m3u8",
      "duration": 60.0,
      "metadata": {
        "categoria": "destacado",
        "idioma": "es"
      }
    }
  ]
}
```

Campos de cada elemento:
- id: Identificador unico del contenido audiovisual (obligatorio).
- title: Titulo descriptivo del video (obligatorio).
- local_path: Ruta en el sistema de archivos local hacia el video descargado (opcional).
- remote_url: URL de transmision remota desde el servicio de transcodificacion o streaming (opcional).
- duration: Duracion nominal en segundos (opcional).
- metadata: Objeto libre con informacion contextual adicional (opcional).

## Uso por Linea de Comandos

El punto de entrada principal permite iniciar la reproduccion especificando el archivo JSON y la salida de video deseada:

Reproduccion en entorno local con interfaz grafica:
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink auto
```

Reproduccion en produccion sin entorno grafico (salida DRM/KMS):
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink kms
```

Reproduccion en framebuffer directo:
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink fbdev
```

Modo headless de verificacion sin salida visual:
```bash
python3 -m molcajete_player.main examples/sample_playlist.json --sink fakesink
```

Opciones adicionales:
- --loop: Fuerza la reproduccion ciclica indefinida de la lista.
- -v, --verbose: Activa trazas detalladas de depuracion del bus de GStreamer.

## Estructura del Proyecto

```text
molcajete-player/
  .gitignore
  pyproject.toml
  requirements.txt
  README.md
  examples/
    sample_playlist.json
  src/
    molcajete_player/
      __init__.py
      main.py
      models.py
      loader.py
      player.py
  tests/
    __init__.py
    test_models.py
    test_loader.py
```

## Ejecucion de Pruebas

Para ejecutar la bateria de pruebas unitarias:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
