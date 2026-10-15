#!/usr/bin/env bash
# Atalho para iniciar o CourseVault. Pode ser chamado por um link simbólico
# (ex.: ln -s /caminho/coursevault/cursos.sh ~/.local/bin/cursos).
# Usa a .venv do projeto; sem ela, o comando "coursevault" instalado com pipx.
set -e

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
SERVER_PY="$PROJECT_DIR/server.py"
DEFAULT_ROOT="${COURSEVAULT_ROOT:-/media/cd/DIC_ntfs_4TB/Cursos}"

if [[ "${LANG:-}" == pt* ]]; then
    MSG_NO_VENV="❌ Erro: crie a .venv do projeto ou instale com \"pipx install $PROJECT_DIR\". Ambiente não encontrado em"
    MSG_NO_DIR="❌ Erro: a pasta informada não existe"
    MSG_START="📚 Iniciando o CourseVault em"
    MSG_USAGE="📚 Uso: cursos [pasta] [opções do server.py]"
    MSG_EXAMPLES="Exemplos:"
    MSG_DEFAULT="# Pasta padrão"
    MSG_CURRENT="# Pasta atual"
    MSG_PORT="# Outra porta"
    MSG_ENV="Defina COURSEVAULT_ROOT para mudar a pasta padrão."
else
    MSG_NO_VENV="❌ Error: create the project's .venv or install with \"pipx install $PROJECT_DIR\". Environment not found at"
    MSG_NO_DIR="❌ Error: folder does not exist"
    MSG_START="📚 Starting CourseVault at"
    MSG_USAGE="📚 Usage: cursos [folder] [server.py options]"
    MSG_EXAMPLES="Examples:"
    MSG_DEFAULT="# Default folder"
    MSG_CURRENT="# Current folder"
    MSG_PORT="# Another port"
    MSG_ENV="Set COURSEVAULT_ROOT to change the default folder."
fi

INSTALLED="$(command -v coursevault 2>/dev/null || true)"
if [ -x "$VENV_PY" ]; then
    RUN=("$VENV_PY" "$SERVER_PY")
elif [ -n "$INSTALLED" ] && [ "$(readlink -f "$INSTALLED")" != "$SELF" ]; then
    RUN=("$INSTALLED")
else
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
            "${RUN[@]}" --help
            exit 0
            ;;
        -r|--root)
            TARGET_ROOT="$2"
            shift 2
            ;;
        -p|--port|--host|--ttl|--allow-host)
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
exec "${RUN[@]}" --root "$TARGET_ROOT" --follow-symlinks "${EXTRA_ARGS[@]}"
