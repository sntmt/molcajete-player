#!/usr/bin/env bash
set -euo pipefail

# Directorio raiz del proyecto (donde reside este script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Usar el interprete del entorno virtual si existe, o python3 del sistema
if [[ -x "${SCRIPT_DIR}/.venv/bin/python3" ]]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python3"
else
    PYTHON_BIN="python3"
fi

# En Linux, limpiar variables inyectadas por terminales Snap (ej. VS Code Snap)
# que causan conflictos con glibc/libpthread del sistema. La lista debe coincidir con
# SNAP_POLLUTED_VARS en src/molcajete_player/env.py; LD_LIBRARY_PATH solo puede
# limpiarse de forma efectiva aqui, antes de arrancar el interprete.
# Se usa printenv en lugar de ${!var:-} por compatibilidad con Bash 3.2 de macOS con set -u.
if [[ "$(uname -s)" == "Linux" ]]; then
    for var in GTK_PATH GTK_EXE_PREFIX GTK_IM_MODULE_FILE GIO_MODULE_DIR \
               GDK_PIXBUF_MODULE_FILE GDK_PIXBUF_MODULEDIR GSETTINGS_SCHEMA_DIR \
               LOCPATH GST_PLUGIN_PATH GST_PLUGIN_SYSTEM_PATH LD_LIBRARY_PATH; do
        val="$(printenv "${var}" 2>/dev/null || true)"
        if [[ "${val}" == *"/snap/"* ]]; then
            unset "${var}"
        fi
    done

    if [[ -n "${XDG_DATA_DIRS_VSCODE_SNAP_ORIG:-}" ]]; then
        export XDG_DATA_DIRS="${XDG_DATA_DIRS_VSCODE_SNAP_ORIG}"
    fi
    if [[ -n "${XDG_CONFIG_DIRS_VSCODE_SNAP_ORIG:-}" ]]; then
        export XDG_CONFIG_DIRS="${XDG_CONFIG_DIRS_VSCODE_SNAP_ORIG}"
    fi
fi

export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

DEFAULT_PLAYLIST="${SCRIPT_DIR}/examples/sample_playlist.json"

# Atajo opcional: ./run.sh --sim [opciones] ejecuta la simulacion Mux -> S3 -> Disco
if [[ "${1:-}" == "--sim" ]]; then
    shift
    exec "${PYTHON_BIN}" "${SCRIPT_DIR}/examples/simulate_mux_s3_switch.py" "$@"
fi

# Si no se pasan argumentos, ejecuta la lista de ejemplo con --sink auto partiendo de una
# cache limpia (--clean-cache solo borra archivos con el patron de nombres del reproductor).
if [[ $# -eq 0 ]]; then
    exec "${PYTHON_BIN}" -m molcajete_player.main "${DEFAULT_PLAYLIST}" --sink auto --clean-cache
fi

# Si el primer argumento es una bandera (empieza por '-'), pero no es -h/--help,
# anteponemos la lista por defecto
if [[ "$1" == -* && "$1" != "-h" && "$1" != "--help" ]]; then
    exec "${PYTHON_BIN}" -m molcajete_player.main "${DEFAULT_PLAYLIST}" "$@"
fi

# En cualquier otro caso (se paso ruta de JSON o --help), pasamos los argumentos tal cual
exec "${PYTHON_BIN}" -m molcajete_player.main "$@"

