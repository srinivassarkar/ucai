#!/usr/bin/env bash
set -euo pipefail

# Configuration parameters with fallback defaults
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_DIR="${UCAI_MODEL_DIR:-$SCRIPT_DIR}"
PID_FILE="${UCAI_PID_FILE:-/tmp/llama-server.pid}"
LOG_FILE="${UCAI_LOG_FILE:-$HOME/Library/Logs/llama-server.log}"

MODEL="${UCAI_MODEL:-$MODEL_DIR/model.gguf}"
MMPROJ="${UCAI_MMPROJ:-$MODEL_DIR/mmproj.gguf}"
MCP_JSON="${UCAI_MCP_JSON:-$MODEL_DIR/mcp.json}"
UI_CONFIG="${UCAI_UI_CONFIG:-$MODEL_DIR/webui_config.json}"

HOST="${UCAI_HOST:-127.0.0.1}"
PORT="${UCAI_PORT:-8080}"
CONTEXT_SIZE="${UCAI_CTX:-262144}"
THREADS="${UCAI_THREADS:-16}"
GPU_LAYERS="${UCAI_NGL:-99}"

LLAMA_SERVER="${LLAMA_SERVER_BIN:-/opt/homebrew/bin/llama-server}"

start() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "llama-server is already running."
        return
    fi

    cd "$MODEL_DIR" || exit 1
    mkdir -p "$(dirname "$LOG_FILE")"

    echo "Launching llama-server on $HOST:$PORT (Context: $CONTEXT_SIZE)..."

    nohup "$LLAMA_SERVER" \
        -m "$MODEL" \
        --mmproj "$MMPROJ" \
        --host "$HOST" \
        --port "$PORT" \
        -c "$CONTEXT_SIZE" \
        -ngl "$GPU_LAYERS" \
        -fa on \
        --threads "$THREADS" \
        -np 1 \
        --metrics \
        --mcp-servers-config "$MCP_JSON" \
        --cors-origins "http://$HOST:$PORT,http://localhost:$PORT" \
        --ui-config-file "$UI_CONFIG" \
        >> "$LOG_FILE" 2>&1 < /dev/null &

    echo $! > "$PID_FILE"
    echo "Started llama-server. PID: $(cat "$PID_FILE")"
    echo "Logs: $LOG_FILE"
}

stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "Server is not running."
        return
    fi

    PID=$(cat "$PID_FILE")

    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping llama-server..."
        kill -TERM "$PID"

        for i in {1..30}; do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done

        if kill -0 "$PID" 2>/dev/null; then
            echo "Graceful shutdown timed out. Force stopping."
            kill -KILL "$PID" 2>/dev/null
        fi
    fi

    rm -f "$PID_FILE"
    echo "Server stopped."
}

status() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "RUNNING - PID $(cat "$PID_FILE")"
    else
        echo "STOPPED"
        rm -f "$PID_FILE"
    fi
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 2; start ;;
    status) status ;;
    logs) tail -f "$LOG_FILE" ;;
    *)
        echo "Usage: ./llama.sh {start|stop|restart|status|logs}"
        ;;
esac
