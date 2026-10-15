#!/usr/bin/env bash
set -e

PROJECT_DIR="/home/cd/Documentos/00_1_syncthing-debian-cdciber/7_my_codes/projects/painel_arquivos_cursos_local "
VENV_PY="$PROJECT_DIR/.venv/bin/python"
SERVER_PY="$PROJECT_DIR/server.py"
DEFAULT_ROOT="/media/cd/DIC_ntfs_4TB/Cursos"

if [ ! -f "$VENV_PY" ]; then
    echo "❌ Erro: Ambiente virtual não encontrado em: $VENV_PY" >&2
    exit 1
fi

TARGET_ROOT=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "📚 Uso: cursos [caminho_da_pasta] [opções do server.py]"
            echo ""
            echo "Exemplos:"
            echo "  cursos                              # Pasta padrão ($DEFAULT_ROOT)"
            echo "  cursos /media/cd/DIC_ntfs_4TB/Cursos"
            echo "  cursos ~/Downloads/meu_curso"
            echo "  cursos .                            # Pasta atual"
            echo "  cursos /caminho --port 9000         # Porta alternativa"
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

# Se não foi fornecido um caminho, usa a pasta padrão (se existir) ou a pasta atual
if [ -z "$TARGET_ROOT" ]; then
    if [ -d "$DEFAULT_ROOT" ]; then
        TARGET_ROOT="$DEFAULT_ROOT"
    else
        TARGET_ROOT="$(pwd)"
    fi
fi

# Expande til (~) se presente como string literal
TARGET_ROOT="${TARGET_ROOT/#\~/$HOME}"

# Valida se a pasta existe e resolve o caminho absoluto
if [ -d "$TARGET_ROOT" ]; then
    TARGET_ROOT="$(cd "$TARGET_ROOT" && pwd -P)"
else
    echo "❌ Erro: A pasta informada não existe: $TARGET_ROOT" >&2
    exit 1
fi

echo "📚 Iniciando Hub de Cursos em: $TARGET_ROOT"
exec "$VENV_PY" "$SERVER_PY" --root "$TARGET_ROOT" --follow-symlinks "${EXTRA_ARGS[@]}"
