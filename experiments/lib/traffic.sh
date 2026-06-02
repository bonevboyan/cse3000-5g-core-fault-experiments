#!/usr/bin/env bash
# experiments/lib/traffic.sh
#
# Synthetic traffic generation for fault-injection runs.
# Ported from experiments/run_experiment.sh (Boyan's main pipeline).
#
# Source this file then call start_traffic / stop_traffic.
# Without traffic, faults often produce no observable signal.
#
#   Data plane:    10 parallel pings via uesimtun0..9 -> 10.45.0.1 (UPF ogstun)
#                  at 5 pings/s each, exercising the GTP tunnel.
#   Control plane: 8 UEs deregister/register every 15s exercising the full
#                  NGAP + AMF + AUSF + UDM + NRF + SCP chain.

UE_POD=""
REREGISTER_PID=""

# _find_ue_pod — returns the name of the UE pod that has active uesimtun
# interfaces. Falls back to any pod matching 'ues' in its name.
_find_ue_pod() {
    # Prefer the pod that actually has uesimtun interfaces (gnb-ues after
    # ueransim-gnb is upgraded to 10 UEs). Iterate all pods with component=ues
    # and pick the first one with at least one uesimtun interface up.
    local candidates
    candidates=$(kubectl get pods -n open5gs -l app.kubernetes.io/component=ues \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
    for pod in $candidates; do
        if kubectl exec -n open5gs "$pod" -- \
                ip link show uesimtun0 >/dev/null 2>&1; then
            echo "$pod"
            return 0
        fi
    done
    # Fallback: first pod with 'ues' in its name
    kubectl get pods -n open5gs --no-headers 2>/dev/null \
        | grep -m1 ues | awk '{print $1}' || true
}

# start_traffic — starts both loops in background.
# Caller is responsible for invoking stop_traffic on exit.
start_traffic() {
    UE_POD=$(_find_ue_pod)
    if [[ -z "$UE_POD" ]]; then
        echo "[traffic] WARNING: no UE pod found — traffic generation skipped" >&2
        return 0
    fi
    echo "[traffic] UE pod: $UE_POD"

    # 1. Data plane: continuous pings via every available uesimtun to the UPF
    # ogstun gateway (10.45.0.1). 5 pings/s per tunnel to generate enough GTP
    # traffic for PFCP modification signals to be visible.
    kubectl exec -n open5gs "$UE_POD" -- bash -c '
        for i in $(seq 0 9); do
            ip link show uesimtun$i >/dev/null 2>&1 && \
                ping -i 0.2 -W 1 -I uesimtun$i 10.45.0.1 >/dev/null 2>&1 &
        done
        wait
    ' >/dev/null 2>&1 &
    echo "[traffic] data-plane pings started"

    # 2. Control plane: cycle 3 UEs through deregister/register every 60s.
    # Generates enough AMF/AUSF/UDM/NRF signal without overwhelming GTP state.
    (
        UEs=("imsi-999700000000003" "imsi-999700000000004" "imsi-999700000000005")
        while true; do
            sleep 60
            for ue in "${UEs[@]}"; do
                kubectl exec -n open5gs "$UE_POD" -- \
                    nr-cli "$ue" --exec "deregister normal" 2>/dev/null || true
            done
            sleep 10
            for ue in "${UEs[@]}"; do
                kubectl exec -n open5gs "$UE_POD" -- \
                    nr-cli "$ue" --exec "register" 2>/dev/null || true
            done
        done
    ) &
    REREGISTER_PID=$!
    echo "[traffic] control-plane re-registration loop started (pid=$REREGISTER_PID)"
}

# stop_traffic — kills both loops; safe to call multiple times.
stop_traffic() {
    [[ -n "${REREGISTER_PID:-}" ]] && kill "$REREGISTER_PID" 2>/dev/null || true
    if [[ -n "${UE_POD:-}" ]]; then
        kubectl exec -n open5gs "$UE_POD" -- \
            bash -c 'pkill ping 2>/dev/null; true' 2>/dev/null || true
    fi
}
