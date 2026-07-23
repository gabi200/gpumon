#!/usr/bin/env bash
#
# End-to-end local test harness for gpumon.
#
# Brings up the full central stack (Mosquitto + bridge + Prometheus + Grafana)
# with docker compose, then starts a local gpumon *node* that monitors this
# machine's GPUs and publishes over MQTT to the local broker — so you can watch
# real telemetry flow all the way into Grafana on one box.
#
#   ./deploy/test-stack.sh              # start everything, stream node logs
#   ./deploy/test-stack.sh --keep       # on Ctrl-C leave the stack running
#   ./deploy/test-stack.sh --node foo   # override the node id (default: test-<host>)
#   ./deploy/test-stack.sh down         # tear the stack down and exit
#
# Ctrl-C stops the node and (unless --keep) runs `docker compose down`.

set -euo pipefail

# --- locate paths --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CENTRAL_DIR="$SCRIPT_DIR/central"

# --- args ----------------------------------------------------------------
KEEP=0
NODE_ID="test-$(hostname -s 2>/dev/null || hostname)"
INTERVAL=2
CMD="up"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep)     KEEP=1; shift ;;
        --node)     NODE_ID="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        down|stop)  CMD="down"; shift ;;
        -h|--help)
            sed -n '3,${/^#/!q;s/^# \{0,1\}//p}' "$0"
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# --- pick a docker compose invocation ------------------------------------
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "error: need 'docker compose' or 'docker-compose' on PATH" >&2
    exit 1
fi

compose() { ( cd "$CENTRAL_DIR" && "${COMPOSE[@]}" "$@" ); }

# --- 'down' subcommand: just tear things down and exit -------------------
if [[ "$CMD" == "down" ]]; then
    echo ">> stopping central stack..."
    compose down
    exit 0
fi

# --- python + paho check for the node ------------------------------------
PY="${PYTHON:-python3}"
if ! "$PY" -c 'import paho.mqtt.client' 2>/dev/null; then
    echo ">> paho-mqtt not found for $PY; installing into a local venv..."
    VENV="$REPO_ROOT/.test-venv"
    "$PY" -m venv "$VENV"
    # shellcheck disable=SC1091
    "$VENV/bin/pip" install --quiet --upgrade pip paho-mqtt
    PY="$VENV/bin/python"
fi

NODE_PID=""
cleanup() {
    echo
    if [[ -n "$NODE_PID" ]] && kill -0 "$NODE_PID" 2>/dev/null; then
        echo ">> stopping gpumon node (pid $NODE_PID)..."
        kill "$NODE_PID" 2>/dev/null || true
        wait "$NODE_PID" 2>/dev/null || true
    fi
    if [[ "$KEEP" -eq 1 ]]; then
        echo ">> --keep set: leaving central stack running."
        echo "   tear down later with:  $0 down"
    else
        echo ">> stopping central stack..."
        compose down
    fi
}
trap cleanup EXIT INT TERM

# --- bring up the central stack ------------------------------------------
echo ">> building + starting central stack (mosquitto, bridge, prometheus, grafana)..."
compose up -d --build

# --- wait for the broker to accept connections ---------------------------
echo -n ">> waiting for MQTT broker on localhost:1883 "
for _ in $(seq 1 30); do
    if "$PY" - <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", 1883)); s.close()
except OSError:
    sys.exit(1)
PYEOF
    then
        echo " ok"
        break
    fi
    echo -n "."
    sleep 1
done

# --- wait for the bridge's metrics endpoint ------------------------------
echo -n ">> waiting for bridge metrics on localhost:9109 "
for _ in $(seq 1 30); do
    if curl -sf localhost:9109/metrics >/dev/null 2>&1; then
        echo " ok"
        break
    fi
    echo -n "."
    sleep 1
done

cat <<EOF

============================================================
  gpumon local test stack is up
------------------------------------------------------------
  Grafana     http://localhost:3000   (admin / admin)
              dashboard: "gpumon — GPU Fleet"
  Prometheus  http://localhost:9090
  Bridge      http://localhost:9109/metrics
  Broker      mqtt://localhost:1883
  Node id     $NODE_ID   (interval ${INTERVAL}s)
============================================================

Starting local gpumon node -> publishing to the broker.
Watch it appear in Grafana under \$node = "$NODE_ID".
Press Ctrl-C to stop the node and tear the stack down.

EOF

# --- run the local node in the foreground (streams its logs) -------------
# Uses a throwaway DB so repeated test runs start clean.
# PYTHONPATH points at the repo root so `-m gpumon` works from any cwd / venv.
NODE_DB="$(mktemp -d)/gpumon-node.db"
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m gpumon \
    --mqtt-host localhost \
    --mqtt-node "$NODE_ID" \
    --interval "$INTERVAL" \
    --db "$NODE_DB" \
    --no-api \
    --log-level INFO &
NODE_PID=$!
wait "$NODE_PID"
