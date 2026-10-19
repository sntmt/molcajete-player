"""Saneamiento del entorno de proceso antes de cargar GObject/GStreamer."""

import os
import sys

# Debe coincidir con la lista de run.sh. LD_LIBRARY_PATH solo puede limpiarse de forma
# efectiva antes de arrancar el interprete (run.sh); aqui se limpia para los subprocesos.
SNAP_POLLUTED_VARS = (
    "GTK_PATH",
    "GTK_EXE_PREFIX",
    "GTK_IM_MODULE_FILE",
    "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GSETTINGS_SCHEMA_DIR",
    "LOCPATH",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "LD_LIBRARY_PATH",
)


def sanitize_snap_env(platform: str = sys.platform) -> None:
    """Limpia variables de entorno inyectadas por paquetes Snap en Linux (p. ej. VS Code Snap)
    que apuntan a /snap/ y provocan conflictos de glibc/libpthread al cargar modulos GIO/GTK.

    Debe llamarse antes de importar ``gi`` (``molcajete_player.player``). En otros
    sistemas operativos (como macOS / darwin) no realiza ninguna accion.
    """
    if not platform.startswith("linux"):
        return

    for var in SNAP_POLLUTED_VARS:
        val = os.environ.get(var, "")
        if "/snap/" in val:
            os.environ.pop(var, None)

    if "XDG_DATA_DIRS_VSCODE_SNAP_ORIG" in os.environ:
        os.environ["XDG_DATA_DIRS"] = os.environ["XDG_DATA_DIRS_VSCODE_SNAP_ORIG"]
    if "XDG_CONFIG_DIRS_VSCODE_SNAP_ORIG" in os.environ:
        os.environ["XDG_CONFIG_DIRS"] = os.environ["XDG_CONFIG_DIRS_VSCODE_SNAP_ORIG"]
