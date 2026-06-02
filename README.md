# Fault-detection reproduction

Reproduces the fault-detection dataset: a cloud-native Open5GS 5G core run under
22 chaos faults, each observed across six signal sources (Prometheus, Jaeger, Loki,
Kubernetes events, the NRF API, and UE round-trip time).

This folder holds the reproduction path only, with no analysis code or recorded data.
For each fault the cluster is recreated from scratch, brought up under readiness gates,
exercised with traffic, faulted, and recovered. Every signal is collected per phase
(`pre`, `during`, `post`).

## Layout

```
cluster-start.sh                   Recreate the kind cluster and deploy the full stack
kind/
  kind-config.yaml                 kind node config
  open5gs-values.yaml              Open5GS + UERANSIM Helm values
  monitoring/beyla-daemonset.yaml  eBPF (Beyla) span/metric collection
  chaos/*.yaml                     22 Chaos Mesh fault manifests
experiments/
  fault-detection/run_all.sh       Entry point; runs all 22 faults in sequence
  lib/
    common.sh                      Paths, port-forwards, collection wrappers
    run_fault.sh                   One fault: pre, inject, post, with collection
    traffic.sh                     Background UE traffic during a run
    health_check.sh                Pre/post readiness verification
    provision_ues.sh               Subscriber provisioning
    collect_*.py, collect_ue_rtt.sh  Per-signal collectors
    hooks/<fault>.sh               Optional per-fault setup/teardown hooks
```

## Prerequisites

- Linux with Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl`, and `helm`
- `python3` (the collectors use only the standard library)
- `sudo` for the one-time Docker iptables-chain fix
- `curl`, `lsof`, `awk`

### Docker Hub auth (required)

Recreating the cluster per fault makes many image pulls and will hit Docker Hub's
anonymous limit of 100 pulls per 6 hours. Create a gitignored auth file with a
read-only personal access token:

```
kind/.dockerhub-auth      line 1: Docker Hub username
                          line 2: read-only PAT
```

`cluster-start.sh` injects it into the kind containerd config at runtime
(`kind/.kind-config.runtime.yaml`, also gitignored); the token is never committed.
Without the file, bring-up still runs but pulls are unauthenticated and may stall.

## Run

```bash
cd experiments/fault-detection

# All 22 faults (long; run inside tmux/screen)
bash run_all.sh

# Resume from fault N (e.g. after a gate failure)
bash run_all.sh --from 7

# Run only specific faults
bash run_all.sh --only 19,20
```

Phase durations are env-overridable (defaults shown):

```bash
PRE_DURATION=600 FAULT_DURATION=300 POST_DURATION=300 bash run_all.sh
```

Each fault recreates the cluster via `cluster-start.sh`, then gates on NF readiness
before injecting. On a gate failure the run aborts and prints the `--from N` command
to resume.

## Output

Data is written under `data/experiments/<dataset>/<fault>/` (gitignored). The dataset
name defaults to `fault-detection` and is overridable, so repeat runs do not clobber
an earlier one:

```bash
FAULT_DATASET=fault-detection-run2 bash run_all.sh
```

Each fault produces `prometheus/`, `jaeger/`, `loki/`, `events/`, `nrf/`, and `rtt/`
subtrees split into `pre/`, `during/`, `post/`, plus `health_pre.json`,
`health_post.json`, and `meta.json`.
