#!/usr/bin/env bash
# experiments/lib/common.sh
#
# Shared helpers for all experiment scripts.
# Source this file at the top of each experiment script:
#   source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/../.." && pwd)"
DATA_DIR="$REPO_ROOT/data/experiments"
CHAOS_DIR="$REPO_ROOT/kind/chaos"

# Port-forward PIDs (tracked for cleanup)
_PF_PIDS=()

# Prometheus, Jaeger, and Loki URLs (set by ensure_portforward_*)
PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
JAEGER_URL="${JAEGER_URL:-http://127.0.0.1:16686}"
LOKI_URL="${LOKI_URL:-http://127.0.0.1:3100}"

# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------
_cleanup() {
    [[ ${#_PF_PIDS[@]} -eq 0 ]] && return
    for pid in "${_PF_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Port-forward helpers
# ---------------------------------------------------------------------------

# start_portforward <namespace> <resource> <local_port> <remote_port>
start_portforward() {
    local ns="$1" resource="$2" local_port="$3" remote_port="$4"
    # On a fresh cluster recreate the backing pod may not be serving yet; a
    # port-forward launched against an endpointless service dies instantly.
    # Wait for a ready endpoint first (non-fatal), then retry the forward a few
    # times so a single slow startup can't abort the whole batch.
    if [[ "$resource" == svc/* ]]; then
        kubectl -n "$ns" wait --for=jsonpath='{.subsets[0].addresses[0].ip}' \
            "endpoints/${resource#svc/}" --timeout=180s >/dev/null 2>&1 || true
    fi
    local attempt pid i
    for attempt in 1 2 3 4 5; do
        # Kill any stale process holding the port
        local stale
        stale=$(lsof -ti tcp:"$local_port" 2>/dev/null || true)
        [[ -n "$stale" ]] && kill "$stale" 2>/dev/null && sleep 1 || true
        kubectl port-forward -n "$ns" "$resource" "${local_port}:${remote_port}" \
            --address=127.0.0.1 >/dev/null 2>&1 &
        pid=$!
        _PF_PIDS+=("$pid")
        # Wait until the port is actually open (max 30s per attempt); bail early
        # if kubectl already died so we retry instead of waiting out the clock.
        i=0
        while ! (echo > /dev/tcp/127.0.0.1/"$local_port") 2>/dev/null; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1; i=$((i+1))
            [[ $i -ge 30 ]] && break
        done
        if (echo > /dev/tcp/127.0.0.1/"$local_port") 2>/dev/null; then
            echo "[pf] $resource → localhost:$local_port (pid $pid, attempt $attempt)"
            return 0
        fi
        kill "$pid" 2>/dev/null || true
        echo "[pf] attempt $attempt for $resource:$remote_port not ready, retrying..." >&2
        sleep 3
    done
    echo "[ERROR] Port-forward to $resource:$remote_port never became ready" >&2
    return 1
}

# ensure_portforward_prometheus — idempotent, sets PROM_URL
ensure_portforward_prometheus() {
    PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
    if ! (echo > /dev/tcp/127.0.0.1/9090) 2>/dev/null; then
        start_portforward monitoring \
            svc/kube-prom-kube-prometheus-prometheus 9090 9090
    else
        echo "[pf] Prometheus already reachable at localhost:9090"
    fi
}

# ensure_portforward_jaeger — idempotent, sets JAEGER_URL
ensure_portforward_jaeger() {
    JAEGER_URL="${JAEGER_URL:-http://127.0.0.1:16686}"
    if ! (echo > /dev/tcp/127.0.0.1/16686) 2>/dev/null; then
        start_portforward monitoring svc/jaeger 16686 16686
    else
        echo "[pf] Jaeger already reachable at localhost:16686"
    fi
}

# ensure_portforward_loki — idempotent, sets LOKI_URL
ensure_portforward_loki() {
    LOKI_URL="${LOKI_URL:-http://127.0.0.1:3100}"
    if ! (echo > /dev/tcp/127.0.0.1/3100) 2>/dev/null; then
        start_portforward monitoring svc/loki 3100 3100
    else
        echo "[pf] Loki already reachable at localhost:3100"
    fi
}

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

now_ts() { date +%s; }

# sleep_with_progress <seconds> <label>
sleep_with_progress() {
    local secs="$1" label="${2:-waiting}"
    echo -n "  [$label] ${secs}s "
    local i=0
    while [[ $i -lt $secs ]]; do
        sleep 10
        i=$((i+10))
        echo -n "."
    done
    echo " done"
}

# ---------------------------------------------------------------------------
# Data collection wrappers
# ---------------------------------------------------------------------------

# collect_prometheus <start_ts> <end_ts> <step> <out_dir>
collect_prometheus() {
    local start="$1" end="$2" step="$3" out_dir="$4"
    mkdir -p "$out_dir"
    python3 "$LIB_DIR/collect_prometheus.py" \
        --url "$PROM_URL" \
        --start "$start" \
        --end   "$end" \
        --step  "$step" \
        --out   "$out_dir"
}

# collect_jaeger <start_ts> <end_ts> <out_dir>
collect_jaeger() {
    local start="$1" end="$2" out_dir="$3"
    mkdir -p "$out_dir"
    python3 "$LIB_DIR/collect_jaeger.py" \
        --url   "$JAEGER_URL" \
        --start "$start" \
        --end   "$end" \
        --out   "$out_dir"
}

# collect_loki <start_ts> <end_ts> <out_dir>
collect_loki() {
    local start="$1" end="$2" out_dir="$3"
    mkdir -p "$out_dir"
    python3 "$LIB_DIR/collect_loki.py" \
        --url   "$LOKI_URL" \
        --start "$start" \
        --end   "$end" \
        --out   "$out_dir"
}

# collect_events <start_ts> <end_ts> <out_dir>
collect_events() {
    local start="$1" end="$2" out_dir="$3"
    mkdir -p "$out_dir"
    python3 "$LIB_DIR/collect_events.py" \
        --namespace open5gs \
        --start "$start" \
        --end   "$end" \
        --out   "$out_dir"
}

# collect_nrf <out_dir> — snapshots current NRF instance counts (no time window)
collect_nrf() {
    local out_dir="$1"
    mkdir -p "$out_dir"
    python3 "$LIB_DIR/collect_nrf.py" \
        --namespace open5gs \
        --out   "$out_dir"
}

# ---------------------------------------------------------------------------
# Experiment metadata
# ---------------------------------------------------------------------------

log_experiment_start() {
    local name="$1" out_dir="$2"
    mkdir -p "$out_dir"
    echo "{\"experiment\": \"$name\", \"started_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
        > "$out_dir/meta.json"
    echo "[meta] $name started"
}

log_experiment_end() {
    local out_dir="$1"
    local meta="$out_dir/meta.json"
    if [[ -f "$meta" ]]; then
        python3 -c "
import json, datetime
with open('$meta') as f: d = json.load(f)
d['ended_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
with open('$meta', 'w') as f: json.dump(d, f, indent=2)
" 2>/dev/null || true
    fi
}
