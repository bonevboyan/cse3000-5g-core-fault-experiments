#!/usr/bin/env bash
# Recreate the open5gs kind cluster and redeploy the full stack.
# Run this after every reboot or Docker restart (Option A: always recreate).
#
# Usage: ./cluster-start.sh [--skip-deploy]
#   --skip-deploy   Recreate the cluster only; skip Helm installs (useful if
#                   you want to deploy manually or iterate on values).
set -euo pipefail

CLUSTER=open5gs
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="$SCRIPT_DIR/kind/kind-config.yaml"

SKIP_DEPLOY=false
[[ "${1:-}" == "--skip-deploy" ]] && SKIP_DEPLOY=true

UE_COUNT=10  # must match `ues.count` and provision_ues.sh below

# ── NF readiness probes (ordering gated on real state, not fixed sleeps) ──────
# Open5GS NF metrics bind to the pod eth0 IP (not localhost), so curl the pod
# IP from inside the pod. No port-forward / Prometheus dependency.
nf_metric() {
  # $1=nf (amf|smf|upf) $2=metric -> prints summed integer value, or returns 1
  local nf="$1" metric="$2" pod ip
  pod=$(kubectl get pods -n open5gs --no-headers 2>/dev/null \
        | grep -i "open5gs-${nf}-" | grep -i ' Running ' | awk '{print $1}' | head -1)
  [[ -z "$pod" ]] && return 1
  ip=$(kubectl get pod -n open5gs "$pod" -o jsonpath='{.status.podIP}' 2>/dev/null)
  [[ -z "$ip" ]] && return 1
  kubectl exec -n open5gs "$pod" -c "open5gs-${nf}" -- \
      curl -s --max-time 5 "${ip}:9090/metrics" 2>/dev/null \
    | awk -v m="$metric" '$1==m {s+=$2; seen=1} END{ if (!seen) exit 1; print int(s) }'
}

wait_for_metric() {
  # $1=nf $2=metric $3=test-op(-ge|-eq) $4=target $5=timeout_s $6=label
  local nf="$1" metric="$2" op="$3" target="$4" timeout="$5" label="$6"
  local deadline=$(( $(date +%s) + timeout )) val ok
  while [[ $(date +%s) -lt $deadline ]]; do
    val=$(nf_metric "$nf" "$metric" 2>/dev/null || true)
    if [[ "$val" =~ ^[0-9]+$ ]]; then
      ok=0
      case "$op" in
        -ge) if [[ "$val" -ge "$target" ]]; then ok=1; fi ;;
        -gt) if [[ "$val" -gt "$target" ]]; then ok=1; fi ;;
        -eq) if [[ "$val" -eq "$target" ]]; then ok=1; fi ;;
      esac
      if [[ "$ok" -eq 1 ]]; then
        echo "  [gate] OK — ${label} (${metric}=${val})"
        return 0
      fi
    fi
    sleep 5
  done
  echo "  [gate] TIMEOUT — ${label} (${metric}=${val:-<none>}, need ${op} ${target} after ${timeout}s)" >&2
  return 1
}

# --- 1. Fix Docker iptables chains if missing (Docker 29 nftables bug) --------
# Skipped here — run_all.sh does this once per session before the fault loop.
echo "[1/5] Skipping iptables check (done once per session by run_all.sh)"

# --- 2. Tear down any existing cluster and recreate ---------------------------
echo "[2/5] Recreating kind cluster '$CLUSTER'..."
kind delete cluster --name "$CLUSTER" 2>/dev/null || true
# Inject Docker Hub auth into a runtime config so cluster pulls are
# authenticated (200/6h vs unauth 100/6h). Token lives in a gitignored
# file; the tracked kind-config.yaml stays secret-free.
KIND_EFFECTIVE="$KIND_CONFIG"
AUTH_FILE="$SCRIPT_DIR/kind/.dockerhub-auth"
if [[ -f "$AUTH_FILE" ]]; then
  DH_USER=$(sed -n '1p' "$AUTH_FILE")
  DH_TOKEN=$(sed -n '2p' "$AUTH_FILE")
  KIND_EFFECTIVE="$SCRIPT_DIR/kind/.kind-config.runtime.yaml"
  cp "$KIND_CONFIG" "$KIND_EFFECTIVE"
  cat >> "$KIND_EFFECTIVE" <<EOF
containerdConfigPatches:
  - |-
    [plugins."io.containerd.grpc.v1.cri".registry.configs."registry-1.docker.io".auth]
      username = "${DH_USER}"
      password = "${DH_TOKEN}"
EOF
  echo "  -> Docker Hub auth injected (user: ${DH_USER})"
else
  echo "  -> WARN: no kind/.dockerhub-auth — pulls UNAUTHENTICATED (100/6h)"
fi
kind create cluster --config "$KIND_EFFECTIVE"
echo "  -> Cluster created"
kubectl get nodes

# --- 3. Raise inotify limits (required for Promtail + Chaos Mesh controller) --
echo "[3/5] Checking inotify limits..."
INSTANCES=$(sysctl -n fs.inotify.max_user_instances)
WATCHES=$(sysctl -n fs.inotify.max_user_watches)
if [[ "$INSTANCES" -lt 512 || "$WATCHES" -lt 524288 ]]; then
  echo "  -> Raising limits (current: instances=$INSTANCES watches=$WATCHES)..."
  sudo sysctl fs.inotify.max_user_instances=512
  sudo sysctl fs.inotify.max_user_watches=524288
else
  echo "  -> OK (instances=$INSTANCES watches=$WATCHES)"
fi

# --- 4. Deploy full stack (unless --skip-deploy) ------------------------------
if $SKIP_DEPLOY; then
  echo "[4/5] Skipping deploy (--skip-deploy)"
else
  echo "[4/5] Deploying full stack..."

  # ── Observability ──────────────────────────────────────────────────────────
  # Must come before Open5GS — Open5GS references ServiceMonitor CRDs which
  # are installed by kube-prometheus-stack.
  echo "  [4a] Observability stack..."
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
  helm repo add grafana               https://grafana.github.io/helm-charts             2>/dev/null || true
  helm repo add jaegertracing         https://jaegertracing.github.io/helm-charts       2>/dev/null || true
  helm repo update

  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

  helm install kube-prom prometheus-community/kube-prometheus-stack \
    --namespace monitoring \
    --set grafana.adminPassword=admin \
    --set prometheus.prometheusSpec.scrapeInterval=5s \
    --timeout=10m

  # ── Open5GS ────────────────────────────────────────────────────────────────
  echo "  [4b] Open5GS..."
  kubectl create namespace open5gs --dry-run=client -o yaml | kubectl apply -f -
  for attempt in 1 2 3; do
    echo "  [4b] Open5GS install attempt $attempt/3..."
    helm uninstall open5gs --namespace open5gs 2>/dev/null || true
    helm install open5gs oci://registry-1.docker.io/gradiantcharts/open5gs \
      --version 2.3.4 \
      --namespace open5gs \
      -f "$SCRIPT_DIR/kind/open5gs-values.yaml" \
      --wait --timeout=15m && break
    echo "  [4b] Attempt $attempt failed, retrying..."
    sleep 10
  done
  kubectl delete deployment -n open5gs open5gs-webui --ignore-not-found

  # ── Gate A: SMF↔UPF PFCP association up before bringing up the RAN ──────────
  # helm --wait only guarantees pod-Ready, not that the N4/PFCP control plane
  # is established. Starting the gNB/UEs before this is the root of the race.
  if ! wait_for_metric upf pfcp_peers_active -ge 1 240 "SMF↔UPF PFCP association"; then
    echo "  [gate] FATAL — core PFCP control plane never came up" >&2
    exit 1
  fi

  # ── UERANSIM ───────────────────────────────────────────────────────────────
  echo "  [4c] UERANSIM gNB + UEs..."
  for attempt in 1 2 3; do
    echo "  [4c] ueransim-gnb install attempt $attempt/3..."
    helm uninstall ueransim-gnb --namespace open5gs 2>/dev/null || true
    helm install ueransim-gnb oci://registry-1.docker.io/gradiant/ueransim-gnb \
      --version 0.2.6 --namespace open5gs \
      --values https://gradiant.github.io/5g-charts/docs/open5gs-ueransim-gnb/gnb-ues-values.yaml \
      --set ues.count=10 \
      --wait --timeout=10m && break
    echo "  [4c] Attempt $attempt failed, retrying..."
    sleep 10
  done
  # ── Gate B: gNB completed NG Setup with AMF before UEs register ────────────
  # UERANSIM's gNB does NOT auto-reconnect after an SCTP/NGAP miss, so a lost
  # race here is permanent until the gNB is restarted — hence bounded retry.
  if ! wait_for_metric amf gnb -ge 1 150 "gNB NG Setup with AMF"; then
    gnb_ok=0
    for r in 1 2 3; do
      echo "  [gate] gNB not connected — restarting ueransim-gnb (retry $r/3)..."
      kubectl rollout restart deployment/ueransim-gnb -n open5gs
      kubectl rollout status  deployment/ueransim-gnb -n open5gs --timeout=120s || true
      if wait_for_metric amf gnb -ge 1 120 "gNB NG Setup with AMF (retry $r)"; then
        gnb_ok=1; break
      fi
    done
    [[ "$gnb_ok" -eq 1 ]] || { echo "  [gate] FATAL — gNB never completed NG Setup" >&2; exit 1; }
  fi

  for attempt in 1 2 3; do
    echo "  [4c] ueransim-ues install attempt $attempt/3..."
    helm uninstall ueransim-ues --namespace open5gs 2>/dev/null || true
    helm install ueransim-ues oci://registry-1.docker.io/gradiant/ueransim-ues \
      --version 0.1.2 --namespace open5gs \
      --values https://gradiant.github.io/5g-charts/docs/open5gs-ueransim-gnb/gnb-ues-values.yaml \
      --wait --timeout=10m && break
    echo "  [4c] Attempt $attempt failed, retrying..."
    sleep 10
  done

  echo "  [4d] Provisioning subscribers..."
  bash "$SCRIPT_DIR/experiments/lib/provision_ues.sh" "$UE_COUNT"
  kubectl rollout restart deployment/ueransim-gnb-ues -n open5gs
  kubectl rollout status  deployment/ueransim-gnb-ues -n open5gs --timeout=60s

  # ── Gate C: UE PDU sessions actually established (not just pods Running) ────
  # rollout status only means the pod is Running; it does NOT mean the UEs
  # completed registration + PDU session over PFCP. Probe the real state.
  if ! wait_for_metric smf pfcp_sessions_active -ge "$UE_COUNT" 180 "${UE_COUNT} UE PDU sessions"; then
    ue_ok=0
    for r in 1 2 3; do
      echo "  [gate] <${UE_COUNT} PDU sessions — restarting UEs (retry $r/3)..."
      kubectl rollout restart deployment/ueransim-gnb-ues deployment/ueransim-ues -n open5gs
      kubectl rollout status  deployment/ueransim-gnb-ues -n open5gs --timeout=120s || true
      kubectl rollout status  deployment/ueransim-ues     -n open5gs --timeout=120s || true
      if wait_for_metric smf pfcp_sessions_active -ge "$UE_COUNT" 150 "${UE_COUNT} UE PDU sessions (retry $r)"; then
        ue_ok=1; break
      fi
    done
    [[ "$ue_ok" -eq 1 ]] || { echo "  [gate] FATAL — UEs never established ${UE_COUNT} PDU sessions" >&2; exit 1; }
  fi

  helm install loki grafana/loki-stack \
    --namespace monitoring \
    --set promtail.enabled=true \
    --set loki.persistence.enabled=false \
    --set grafana.enabled=false \
    --set loki.isDefault=false

  # collect_loki.py paginates at 5000/page so the default cap already works;
  # 50000 is just a safety margin (not load-bearing anymore).
  echo "  [loki] Raising max_entries_limit_per_query to 50000..."
  kubectl rollout status statefulset/loki -n monitoring --timeout=300s || true
  LOKI_CFG=$(kubectl get secret loki -n monitoring -o jsonpath='{.data.loki\.yaml}' | base64 -d \
    | sed 's/max_entries_limit_per_query: 5000/max_entries_limit_per_query: 50000/')
  kubectl patch secret loki -n monitoring --type='json' \
    -p="[{\"op\":\"replace\",\"path\":\"/data/loki.yaml\",\"value\":\"$(echo "$LOKI_CFG" | base64 -w 0)\"}]"
  kubectl rollout restart statefulset/loki -n monitoring
  kubectl rollout status statefulset/loki -n monitoring --timeout=300s || true

  helm install jaeger jaegertracing/jaeger \
    --namespace monitoring \
    --set allInOne.enabled=true \
    --set storage.type=memory \
    --set agent.enabled=false --set collector.enabled=false --set query.enabled=false \
    --timeout=5m

  kubectl apply -f "$SCRIPT_DIR/kind/monitoring/beyla-daemonset.yaml"

  # ── Chaos Mesh ─────────────────────────────────────────────────────────────
  echo "  [4d] Chaos Mesh..."
  helm repo add chaos-mesh https://charts.chaos-mesh.org 2>/dev/null || true
  helm repo update
  helm install chaos-mesh chaos-mesh/chaos-mesh \
    --namespace chaos-mesh --create-namespace \
    --version 2.7.2 \
    --set chaosDaemon.runtime=containerd \
    --set chaosDaemon.socketPath=/run/containerd/containerd.sock

  echo "  -> Waiting for Chaos Mesh to be ready..."
  kubectl rollout status deployment/chaos-controller-manager -n chaos-mesh --timeout=7m
  
  # ── Metrics Server (Required for kubectl top) ──────────────────────────────
  echo "  [4e] Metrics Server..."
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  kubectl patch -n kube-system deployment metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

fi

# --- 5. Sanity checks ---------------------------------------------------------
echo "[5/5] Sanity checks..."
echo "  Nodes:"
kubectl get nodes
echo "  open5gs pods:"
kubectl get pods -n open5gs
echo "  monitoring pods:"
kubectl get pods -n monitoring
echo "  chaos-mesh pods:"
kubectl get pods -n chaos-mesh

echo ""
echo "Cluster ready."
echo "  Port-forward Grafana:    kubectl port-forward -n monitoring deployment/kube-prom-grafana 3000:3000"
echo "  Port-forward Prometheus: kubectl port-forward -n monitoring svc/kube-prom-kube-prometheus-prometheus 9090:9090"
echo "  Port-forward Jaeger:     kubectl port-forward -n monitoring svc/jaeger 16686:16686"
