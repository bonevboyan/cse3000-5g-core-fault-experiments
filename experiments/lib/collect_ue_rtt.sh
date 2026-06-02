#!/usr/bin/env bash
# collect_ue_rtt.sh
#
# Runs continuous ping from the UE pod through uesimtun0 to the UPF data
# network gateway (10.45.0.1) for a given duration. Captures per-second
# RTT and packet loss with Unix timestamps.
#
# Output format (one line per second):
#   <unix_ts_ms> <rtt_ms|"loss">
#
# Usage:
#   bash collect_ue_rtt.sh <duration_s> <out_file>
#
# Called from run_fault.sh — runs in background during pre, fault, and post
# windows, then the output file is copied to the experiment directory.

set -euo pipefail

DURATION="${1:-300}"
OUT_FILE="${2:-/tmp/ue_rtt.csv}"
UPF_GW="10.45.0.1"
IFACE="uesimtun0"

# Find a UE pod that has uesimtun0 active
_find_ue_pod_with_tunnel() {
    for pod in $(kubectl get pods -n open5gs \
            -l app.kubernetes.io/instance=ueransim-gnb,app.kubernetes.io/component=ues \
            --no-headers 2>/dev/null | awk '{print $1}'); do
        kubectl exec -n open5gs "$pod" -- ip link show uesimtun0 >/dev/null 2>&1 && echo "$pod" && return 0
    done
    for pod in $(kubectl get pods -n open5gs \
            -l app.kubernetes.io/component=ues \
            --no-headers 2>/dev/null | awk '{print $1}'); do
        kubectl exec -n open5gs "$pod" -- ip link show uesimtun0 >/dev/null 2>&1 && echo "$pod" && return 0
    done
    return 1
}

UE_POD=$(_find_ue_pod_with_tunnel || true)

if [[ -z "$UE_POD" ]]; then
    echo "  [ue_rtt] WARNING: no UE pod with uesimtun0 found, skipping UE RTT collection" >&2
    echo "timestamp_ms,rtt_ms,status" > "$OUT_FILE"
    exit 0
fi

echo "  [ue_rtt] starting ${DURATION}s RTT collection from $UE_POD via $IFACE -> $UPF_GW"
echo "timestamp_ms,rtt_ms,status" > "$OUT_FILE"

END=$(($(date +%s) + DURATION))
while [[ $(date +%s) -lt $END ]]; do
    TS_MS=$(date +%s%3N)
    RESULT=$(kubectl exec -n open5gs "$UE_POD" -- \
        ping -I "$IFACE" -c 1 -W 2 "$UPF_GW" 2>/dev/null \
        | grep -oP 'time=\K[\d.]+' || echo "")
    if [[ -n "$RESULT" ]]; then
        echo "${TS_MS},${RESULT},ok" >> "$OUT_FILE"
    else
        echo "${TS_MS},,loss" >> "$OUT_FILE"
    fi
done
echo "  [ue_rtt] collection complete → $OUT_FILE"
