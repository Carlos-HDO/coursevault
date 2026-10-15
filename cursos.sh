#!/usr/bin/env bash
# Atalho para iniciar o CourseVault. Pode ser chamado por um link simbólico
# (ex.: ln -s /caminho/coursevault/cursos.sh ~/.local/bin/cursos).
set -e

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
SERVER_PY="$PROJECT_DIR/server.py"
DEFAULT_ROOT="${COURSEVAULT_ROOT:-/media/cd/DIC_ntfs_4TB/Cursos}"

if [[ "${LANG:-}" == pt* ]]; then
    MSG_NO_VENV="❌ Erro: ambiente virtual não encontrado em"
    MSG_NO_DIR="❌ Erro: a pasta informada não existe"
    MSG_START="📚 Iniciando o CourseVault em"
    MSG_USAGE="📚 Uso: cursos [pasta] [opções do server.py]"
    MSG_EXAMPLES="Exemplos:"
    MSG_DEFAULT="# Pasta padrão"
    MSG_CURRENT="# Pasta atual"
    MSG_PORT="# Outra porta"
    MSG_ENV="Defina COURSEVAULT_ROOT para mudar a pasta padrão."
else
    MSG_NO_VENV="❌ Error: virtual environment not found at"
    MSG_NO_DIR="❌ Error: folder does not exist"
    MSG_START="📚 Starting CourseVault at"
    MSG_USAGE="📚 Usage: cursos [folder] [server.py options]"
    MSG_EXAMPLES="Examples:"
    MSG_DEFAULT="# Default folder"
    MSG_CURRENT="# Current folder"
    MSG_PORT="# Another port"
    MSG_ENV="Set COURSEVAULT_ROOT to change the default folder."
fi

if [ ! -f "$VENV_PY" ]; then
    echo "$MSG_NO_VENV: $VENV_PY" >&2
    exit 1
fi

TARGET_ROOT=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "$MSG_USAGE"
            echo ""
            echo "$MSG_EXAMPLES"
            echo "  cursos                              $MSG_DEFAULT ($DEFAULT_ROOT)"
            echo "  cursos ~/Downloads/my_course"
            echo "  cursos .                            $MSG_CURRENT"
            echo "  cursos /path --port 9000            $MSG_PORT"
            echo ""
            echo "$MSG_ENV"
            echo ""
            "$VENV_PY" "$SERVER_PY" --help
            exit 0
            ;;
        -r|--root)
            TARGET_ROOT="$2"
            shift 2
            ;;
        -p|--port|--host|--ttl)
            EXTRA_ARGS+=("$1" "$2")
            shift 2
            ;;
        -*)
            EXTRA_ARGS+=("$1")
            shift
            ;;
        *)
            if [ -z "$TARGET_ROOT" ]; then
                TARGET_ROOT="$1"
            else
                EXTRA_ARGS+=("$1")
            fi
            shift
            ;;
    esac
done

# Sem caminho: usa a pasta padrão (se existir) ou a pasta atual
if [ -z "$TARGET_ROOT" ]; then
    if [ -d "$DEFAULT_ROOT" ]; then
        TARGET_ROOT="$DEFAULT_ROOT"
    else
        TARGET_ROOT="$(pwd)"
    fi
fi

# Expande til (~) se veio como texto literal
TARGET_ROOT="${TARGET_ROOT/#\~/$HOME}"

if [ -d "$TARGET_ROOT" ]; then
    TARGET_ROOT="$(cd "$TARGET_ROOT" && pwd -P)"
else
    echo "$MSG_NO_DIR: $TARGET_ROOT" >&2
    exit 1
fi

echo "$MSG_START: $TARGET_ROOT"
exec "$VENV_PY" "$SERVER_PY" --root "$TARGET_ROOT" --follow-symlinks "${EXTRA_ARGS[@]}"
