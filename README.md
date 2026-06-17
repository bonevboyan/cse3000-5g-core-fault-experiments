# Fault-detection reproduction

Reproduces the fault-detection dataset: a cloud-native Open5GS 5G core run under
22 chaos faults, each observed across six signal sources (Prometheus, Jaeger, Loki,
Kubernetes events, the NRF API, and UE round-trip time).

For each fault the cluster is recreated from scratch, brought up under readiness gates,
exercised with traffic, faulted, and recovered. Every signal is collected per phase
(`pre`, `during`, `post`).

The folder also ships the analysis code that turns the collected signals into the
paper's fault atlas and its robustness checks (see [Analysis](#analysis)). No recorded
data is included.

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
analysis/
  common.py                        Signal definitions, anomaly detectors, paths
  a01_fault_atlas.py               Fault x signal atlas heatmap
  a02_temporal.py                  First-layer / onset temporal heatmap
  a03_temporal_delta.py            Onset delta between two runs
  a04_propagation.py               Per-fault cross-NF propagation chains
  a05_nf_impact.py                 Per-NF impact heatmap (needs a04)
  a06_sensitivity.py               Threshold / estimator robustness sweep
  a07_multirun.py                  Cross-run agreement (consensus atlas)
```

## Prerequisites

- Linux with Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl`, and `helm`
- `python3` (the collectors use only the standard library)
- `sudo` for the one-time Docker iptables-chain fix
- `curl`, `lsof`, `awk`

The analysis code (not the collectors) additionally needs `pandas`, `numpy`, and
`matplotlib`:

```bash
pip install pandas numpy matplotlib
```

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

## Analysis

The scripts under `analysis/` read a collected dataset and produce the paper's figures
and robustness checks. Run them as modules from this folder, after collecting (or
restoring) a dataset under `data/experiments/<dataset>/`.

The analysis reads the dataset named by `FAULT_DATASET` (default `fault-detection`,
matching the collection default). If you collected under a different name, export it
first so the analysis matches. Outputs land in `data/analysis/` for the default dataset,
or `data/analysis-<dataset>/` otherwise, with plots under `plots/`.

```bash
# from the repo root (set FAULT_DATASET only if you collected under another name)
python -m analysis.a01_fault_atlas     # -> plots/atlas_heatmap.png      + fault_atlas.csv
python -m analysis.a02_temporal        # -> plots/temporal_heatmap.png   + temporal_layers.csv
python -m analysis.a04_propagation     # -> propagation/<fault>.json     + propagation_summary.csv
python -m analysis.a05_nf_impact       # -> plots/nf_impact_heatmap.png  + nf_fault_matrix.csv
python -m analysis.a06_sensitivity     # -> plots/sensitivity.png        + sensitivity.csv
```

`a05_nf_impact` reads the per-fault chains written by `a04_propagation`, so run `a04`
first.

Two scripts compare runs and take dataset/analysis directories as arguments:

```bash
# onset delta between two collected datasets (base, other)
python -m analysis.a03_temporal_delta <base-dataset> <other-dataset>

# cross-run consensus atlas over >=2 runs (each must already have fault_atlas.csv
# from a01); with no arguments it auto-discovers analysis directories
python -m analysis.a07_multirun <analysis-dir-1> <analysis-dir-2>
```

`a06_sensitivity.py` is the threshold/estimator robustness sweep (including PRISM's
median/MAD estimator) and `a07_multirun.py` is the cross-run reproducibility check.

## Contribution

This repository was made for the course *CSE3000: Research Project* at TU Delft. It was developed by Boyan Bonev, David Ghergut, Yana Mihaylova, Stoyan Kutsarov and Victor Ilchev for the topic of *Observability for Intelligent Fault Management in Cloud-native Beyond 5G Networks*.
