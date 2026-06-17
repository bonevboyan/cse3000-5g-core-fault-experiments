"""
Shared core for the fault-atlas analysis suite.

One detection pass per fault is computed here and consumed by every analysis
script, so the atlas / temporal / propagation / nf-impact / etc. views never
diverge in how they decide a signal "manifested".

Detection backbone (confirmed methodology):
  * continuous signals (Prometheus gauges/rates, Loki rates, Jaeger error/p95,
    RTT)  -> rolling z-score vs the fault's own `pre`-phase baseline, sliding
    ~30 s window, flagged when |window_mean - base_mean| crosses Z*sigma, with a
    flat-baseline guard (relative floor) so a dead-quiet signal does not
    false-positive on noise; a signal counts only after PERSIST consecutive
    crossing windows; the first such window gives time-to-detect.
  * zero-suppressed failure counters (*_fail / *_reject / gtp_node_failed)
    -> first sustained non-zero sample.
  * pod restarts -> step increase over the pre-phase level.
  * pod ready/running -> drop to zero.
  * K8s events -> presence of a Warning reason in the fault window.
  * NRF snapshot -> pre-vs-during registration delta (no intra-phase timing).

t0 (injection instant) = the Chaos Mesh `Applied` event time in
events/during/k8s_events.json (exact), falling back to timeline.fault.start.

All thresholds are module-level constants documented in the generated
data/analysis/README.md.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

ANALYSIS_DIR = Path(__file__).resolve().parent
REPRO_DIR = ANALYSIS_DIR.parent
import os as _os
_DATASET = _os.environ.get("FAULT_DATASET", "fault-detection")
DATA_ROOT = REPRO_DIR / "data" / "experiments" / _DATASET
OUT_DIR = REPRO_DIR / "data" / ("analysis" if _DATASET == "fault-detection" else f"analysis-{_DATASET}")
PLOT_DIR = OUT_DIR / "plots"

PHASES = ("pre", "during", "post")  # on-disk dir names; timeline key for during == "fault"

# --------------------------------------------------------------------------- #
# Detection constants (cite these in the paper)
# --------------------------------------------------------------------------- #

Z = 3.0                 # z-score crossing threshold
WINDOW_S = 30           # rolling window length (seconds)
BIN_S = 5               # Prometheus scrape / analysis bin (seconds)
WIN = WINDOW_S // BIN_S  # samples per window
PERSIST = 2             # consecutive anomalous windows required

FLAT_REL = 0.30         # flat-baseline guard: |delta| must also exceed 30% of |base_mean|
FLAT_EPS = 1e-9
# Scalars on the per-signal flat-baseline guard, applied at detection time so
# a06 can sweep them (the floor/ratio constants are the real decision boundary
# for quiet signals, more so than Z). 1.0 = use the values as declared.
FLOOR_MULT = 1.0
RATIO_MULT = 1.0
REL_MULT = 1.0          # scales the 30% relative guard (FLAT_REL) for a06's sweep

# Baseline location/scale estimator for the continuous z-score detectors.
# "mean": (mean, std) -- our default. "mad": (median, 1.4826*MAD), the robust
# "modified z-score" used by PRISM. a06 sweeps this to show the atlas is
# insensitive to the choice of estimator.
ESTIMATOR = "mean"

COUNTER_EPS = 1e-6      # "non-zero" threshold for failure-counter detector
COUNTER_PERSIST = 2     # consecutive non-zero samples for a counter to count

JAEGER_ERR_DELTA = 0.05   # absolute error-rate rise over pre baseline
JAEGER_P95_RATIO = 1.5    # p95 latency multiple over pre baseline
LOKI_MIN_EXTRA = 3        # min extra log lines/bin over baseline (flat guard)
RTT_MIN_DELTA_MS = 5.0    # flat guard for RTT z-score (absolute ms floor)

LAYERS = ("infrastructure", "orchestration", "application")

# K8s event reasons that count as an orchestration signal
K8S_WARN_REASONS = {
    "OOMKilling", "Killing", "BackOff", "Failed", "FailedKillPod",
    "Evicted", "Unhealthy", "FailedScheduling", "FailedCreatePodSandBox",
    "NodeNotReady", "ContainerGCFailed",
}
# Chaos-Mesh / noise reasons that are NOT signals (used for t0, not detection)
K8S_IGNORE_REASONS = {
    "Applied", "Recovered", "FinalizerInited", "FinalizerRemoved",
    "Updated", "Started", "Created", "Pulled", "Pulling", "Scheduled",
    "SuccessfulCreate", "SuccessfulDelete", "FailedGetScale",
}

# --------------------------------------------------------------------------- #
# Fault metadata  (curated from kind/chaos/*.yaml)
#   target_nf      : NF the chaos selector hits
#   chaos          : Chaos-Mesh kind / action
#   silva          : Silva 2022 fault code
#   zhou           : Zhou 2018 root-cause class
#   origin         : origin layer
#   family         : grouping for per-class profiles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FaultMeta:
    slug: str
    target_nf: str
    chaos: str
    silva: str
    zhou: str
    origin: str
    family: str


FAULTS: dict[str, FaultMeta] = {f.slug: f for f in [
    FaultMeta("01-cpu-stress-amf", "amf", "StressChaos/cpu", "PF31 CPU Hog", "Environment", "infrastructure", "cpu_stress"),
    FaultMeta("02-memory-pressure-upf", "upf", "StressChaos/mem", "PF26 Memory Alloc", "Environment", "infrastructure", "memory_pressure"),
    FaultMeta("03-pod-crash-amf", "amf", "PodChaos/pod-kill", "PF03 Process Crash", "Environment", "orchestration", "pod_crash"),
    FaultMeta("04-network-delay-gnb-amf", "amf", "NetworkChaos/delay", "PF15 Network Delay", "Interaction", "orchestration", "network_delay"),
    FaultMeta("05-network-partition-amf-scp", "amf", "NetworkChaos/partition", "PF22 Network Fault", "Interaction", "orchestration", "network_partition"),
    FaultMeta("06-packet-loss-upf", "upf", "NetworkChaos/loss", "PF22 Packet Loss", "Interaction", "application", "packet_loss"),
    FaultMeta("07-pod-crash-smf", "smf", "PodChaos/pod-kill", "PF03 Process Crash", "Environment", "orchestration", "pod_crash"),
    FaultMeta("08-cpu-stress-scp", "scp", "StressChaos/cpu", "PF31 CPU Hog", "Environment", "infrastructure", "cpu_stress"),
    FaultMeta("09-network-delay-nrf", "nrf", "NetworkChaos/delay", "PF15 Network Delay", "Interaction", "orchestration", "network_delay"),
    FaultMeta("10-pfcp-session-establishment-flood-upf", "upf", "NetworkChaos/bandwidth", "PF22/PF31 Overload", "Interaction", "application", "pfcp_attack"),
    FaultMeta("11-pfcp-session-deletion-upf", "upf", "NetworkChaos/loss", "PF22 Network Fault", "Interaction", "application", "pfcp_attack"),
    FaultMeta("12-pfcp-session-modification-drop-upf", "upf", "NetworkChaos/corrupt", "PF22 Network Fault", "Interaction", "application", "pfcp_attack"),
    FaultMeta("13-pfcp-session-modification-dupl-upf", "gnb", "NetworkChaos/duplicate", "PF22 Network Fault", "Interaction", "application", "pfcp_attack"),
    FaultMeta("14-upf-infrastructure-packet-loss", "upf", "NetworkChaos/loss", "PF22 Network Fault", "Interaction", "application", "packet_loss"),
    FaultMeta("15-nrf-cascade", "nrf", "PodChaos/pod-kill", "RF12+CF03 Svc Unavail", "Interaction", "application", "dependency_failure"),
    FaultMeta("16-cpu-stress-ausf", "ausf", "StressChaos/cpu", "PF31 CPU Hog", "Environment", "infrastructure", "cpu_stress"),
    FaultMeta("17-network-delay-scp", "scp", "NetworkChaos/delay", "PF15 Network Delay", "Interaction", "orchestration", "network_delay"),
    FaultMeta("18-cpu-stress-nrf", "nrf", "StressChaos/cpu", "PF31 CPU Hog", "Environment", "infrastructure", "cpu_stress"),
    FaultMeta("19-udm-pod-crash", "udm", "PodChaos/pod-kill", "PF03/RF12 Crash", "Environment", "orchestration", "pod_crash"),
    FaultMeta("20-mongodb-pod-kill", "mongodb", "PodChaos/pod-kill", "RF12 DB Unavailable", "Environment", "orchestration", "dependency_failure"),
    FaultMeta("21-n2-partition-amf-gnb", "amf", "NetworkChaos/partition", "PF22 Network Fault", "Interaction", "orchestration", "network_partition"),
    FaultMeta("22-memory-pressure-amf", "amf", "StressChaos/mem", "PF26 Memory Alloc", "Environment", "infrastructure", "memory_pressure"),
]}


def fault_slugs() -> list[str]:
    return [d.name for d in sorted(DATA_ROOT.iterdir()) if d.is_dir() and d.name in FAULTS]


# Canonical order of the 8 fault classes used for grouped heatmaps.
FAULT_FAMILY_ORDER = [
    "cpu_stress", "memory_pressure", "pod_crash",
    "network_delay", "network_partition", "packet_loss",
    "pfcp_attack", "dependency_failure",
]


def fault_slugs_by_family() -> list[str]:
    """Slugs ordered by fault class then slug — keeps each class contiguous."""
    fam_idx = {f: i for i, f in enumerate(FAULT_FAMILY_ORDER)}
    return sorted(fault_slugs(),
                  key=lambda s: (fam_idx.get(FAULTS[s].family, 999), s))


# --------------------------------------------------------------------------- #
# Signal registry  (signal -> layer + how to detect it)
#   Metrics flagged caveat=True (over-counting) are
#   loaded for reference but NOT scored in the atlas.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Signal:
    key: str            # atlas column
    layer: str
    modality: str       # metric | log | trace | event | nrf | rtt
    kind: str           # zscore | counter | podstep | poddrop | event | nrf | jaeger | loki
    source: str = ""    # prometheus metric file stem / loki file / etc.
    caveat: bool = False
    floor: float = 0.0          # absolute min |Δ| (defeats near-zero noise)
    ratio: float = 0.0          # require >=ratio x or <=1/ratio x baseline (0 = off)


SIGNALS: list[Signal] = [
    # ---- infrastructure (Prometheus container / node + RTT) ----
    Signal("cpu_usage", "infrastructure", "metric", "zscore", "container_cpu_usage_rate", floor=0.05, ratio=2.0),
    Signal("cpu_throttle", "infrastructure", "metric", "zscore", "container_cpu_throttled_rate", floor=0.05),
    Signal("mem_working_set", "infrastructure", "metric", "zscore", "container_memory_working_set_bytes", floor=2e7, ratio=1.3),
    Signal("net_rx", "infrastructure", "metric", "zscore", "network_rx_bytes_rate", floor=1e4, ratio=3.0),
    Signal("net_tx", "infrastructure", "metric", "zscore", "network_tx_bytes_rate", floor=1e4, ratio=3.0),
    Signal("node_cpu", "infrastructure", "metric", "zscore", "node_cpu_usage", floor=0.1, ratio=1.5),
    Signal("node_mem_avail", "infrastructure", "metric", "zscore", "node_memory_available", floor=5e7, ratio=1.15),
    Signal("rtt", "infrastructure", "rtt", "zscore", "ue_rtt"),
    # ---- orchestration (Kubernetes) ----
    Signal("pod_restart", "orchestration", "metric", "podstep", "pod_restarts"),
    Signal("pod_ready_drop", "orchestration", "metric", "poddrop", "pod_ready"),
    Signal("pod_running_drop", "orchestration", "metric", "poddrop", "pod_running"),
    Signal("k8s_warning", "orchestration", "event", "event", "k8s_events"),
    # ---- application: Beyla eBPF SBI ----
    Signal("beyla_srv_latency", "application", "metric", "zscore", "beyla_http_server_duration", floor=0.01, ratio=1.5),
    Signal("beyla_cli_latency", "application", "metric", "zscore", "beyla_http_client_duration", floor=0.01, ratio=1.5),
    Signal("beyla_srv_error", "application", "metric", "zscore", "beyla_http_server_error_rate", floor=0.01),
    Signal("beyla_cli_error", "application", "metric", "zscore", "beyla_http_client_error_rate", floor=0.01),
    Signal("beyla_srv_reqrate", "application", "metric", "zscore", "beyla_http_server_request_rate", floor=0.5, ratio=2.0),
    # ---- application: Open5GS NF state gauges/rates ----
    Signal("amf_registered_sub", "application", "metric", "zscore", "open5gs_amf_registered_subscribers", floor=1.0),
    Signal("amf_ran_ue", "application", "metric", "zscore", "open5gs_amf_ran_ue_count", floor=1.0),
    Signal("amf_gnb", "application", "metric", "zscore", "open5gs_amf_gnb_count", floor=0.9),
    Signal("amf_sessions", "application", "metric", "zscore", "open5gs_amf_sessions", floor=1.0),
    Signal("pfcp_sessions_active", "application", "metric", "zscore", "open5gs_pfcp_sessions_active", floor=1.0),
    Signal("pfcp_peers_active", "application", "metric", "zscore", "open5gs_pfcp_peers_active", floor=0.9),
    Signal("smf_ues_active", "application", "metric", "zscore", "open5gs_smf_ues_active", floor=1.0),
    Signal("smf_bearers_active", "application", "metric", "zscore", "open5gs_smf_bearers_active", floor=1.0),
    Signal("smf_pdu_succ", "application", "metric", "zscore", "open5gs_smf_pdu_session_succ", floor=0.02, ratio=2.0),
    Signal("smf_n4_estab", "application", "metric", "zscore", "open5gs_smf_n4_session_estab", floor=0.02, ratio=2.0),
    Signal("smf_n4_report_succ", "application", "metric", "zscore", "open5gs_smf_n4_session_report_succ", floor=0.02, ratio=2.0),
    Signal("upf_n4_estab", "application", "metric", "zscore", "open5gs_upf_n4_session_estab", floor=0.02, ratio=2.0),
    # ---- application: Open5GS failure counters (zero-suppressed) ----
    Signal("amf_reg_fail", "application", "metric", "counter", "open5gs_amf_reg_init_fail"),
    Signal("amf_auth_fail", "application", "metric", "counter", "open5gs_amf_auth_fail"),
    Signal("amf_auth_reject", "application", "metric", "counter", "open5gs_amf_auth_reject"),
    Signal("gtp_node_failed", "application", "metric", "counter", "open5gs_gtp_node_failed"),
    # ---- application: Loki log streams ----
    Signal("log_errors", "application", "log", "loki", "errors"),
    Signal("log_ue_failures", "application", "log", "loki", "ue_failures"),
    Signal("log_scp_routing", "application", "log", "loki", "scp_routing"),
    Signal("log_nrf_lifecycle", "application", "log", "loki", "nrf_lifecycle"),
    # ---- application: Jaeger traces ----
    Signal("trace_error_rate", "application", "trace", "jaeger", "error_rate"),
    Signal("trace_p95_latency", "application", "trace", "jaeger", "p95"),
    # ---- application: NRF dependency snapshot ----
    Signal("nrf_dereg", "application", "nrf", "nrf", "nrf_registrations"),
    # ---- caveat metrics: loaded, NOT scored ----
    Signal("upf_session_nbr", "application", "metric", "zscore", "open5gs_upf_session_nbr", caveat=True),
    Signal("upf_qos_flows", "application", "metric", "zscore", "open5gs_upf_qos_flows", caveat=True),
    Signal("smf_session_nbr", "application", "metric", "zscore", "open5gs_smf_session_nbr", caveat=True),
]

SCORED_SIGNALS = [s for s in SIGNALS if not s.caveat]
SIGNAL_BY_KEY = {s.key: s for s in SIGNALS}


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_POD_NF = re.compile(r"^open5gs-([a-z0-9]+?)-[0-9a-f]{6,}")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s) if isinstance(s, str) else s


def pod_to_nf(pod: str) -> Optional[str]:
    """open5gs-amf-5988b9d456-jc8tr -> 'amf'; ueransim/mongodb handled; else None."""
    if not isinstance(pod, str):
        return None
    m = _POD_NF.match(pod)
    if m:
        return m.group(1)
    if pod.startswith("open5gs-mongodb") or pod.startswith("mongodb"):
        return "mongodb"
    if "gnb" in pod or "ueransim-gnb" in pod:
        return "gnb"
    if pod.startswith("ueransim-ues") or pod.startswith("ue-"):
        return "ue"
    return None


def _iso_to_epoch(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


# --------------------------------------------------------------------------- #
# Per-fault context (timeline + t0)
# --------------------------------------------------------------------------- #

@dataclass
class Ctx:
    slug: str
    root: Path
    t0: float            # injection instant (epoch s)
    fault_end: float     # fault stop instant (epoch s)
    pre: tuple[float, float]
    during: tuple[float, float]
    post: tuple[float, float]


def load_ctx(slug: str) -> Ctx:
    root = DATA_ROOT / slug
    tl = json.loads((root / "timeline.json").read_text())
    pre = (tl["pre"]["start"], tl["pre"]["end"])
    during = (tl["fault"]["start"], tl["fault"]["end"])
    post = (tl["post"]["start"], tl["post"]["end"])

    t0, fend = float(during[0]), float(during[1])
    ev = root / "events" / "during" / "k8s_events.json"
    if ev.exists():
        try:
            events = json.loads(ev.read_text())
            applied = [_iso_to_epoch(e["time"]) for e in events if e.get("reason") == "Applied"]
            recovered = [_iso_to_epoch(e["time"]) for e in events if e.get("reason") == "Recovered"]
            if applied:
                t0 = min(applied)
            if recovered:
                fend = max(recovered)
        except Exception:
            pass
    return Ctx(slug, root, t0, fend, pre, during, post)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

def _read_prom(root: Path, phase: str, stem: str) -> Optional[pd.DataFrame]:
    f = root / "prometheus" / phase / f"{stem}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, dtype=str)
    except Exception:
        return None
    if df.empty or "timestamp" not in df or "value" not in df:
        return None
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["timestamp", "value"])
    return df if not df.empty else None


def _series_by_nf(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Aggregate to one (timestamp,value) series per NF (mean across label rows)."""
    out: dict[str, pd.DataFrame] = {}
    if "pod" in df.columns:
        df = df.copy()
        df["nf"] = df["pod"].map(pod_to_nf)
        grp_key = "nf"
        if df["nf"].isna().all():
            df["nf"] = "_cluster"
    else:
        df = df.copy()
        df["nf"] = "_cluster"
        grp_key = "nf"
    for nf, g in df.groupby(grp_key, dropna=False):
        if nf is None or (isinstance(nf, float) and math.isnan(nf)):
            nf = "_cluster"
        s = g.groupby("timestamp")["value"].mean().sort_index()
        out[str(nf)] = s.reset_index()
    return out


def _beyla_series_by_nf(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    col = "service_name" if "service_name" in df.columns else None
    df = df.copy()
    df["nf"] = df[col] if col else "_all"
    out = {}
    for nf, g in df.groupby("nf", dropna=False):
        nf = str(nf) if nf is not None else "_all"
        out[nf] = g.groupby("timestamp")["value"].mean().sort_index().reset_index()
    return out


# --------------------------------------------------------------------------- #
# Detection result types
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    manifested: bool = False
    t_detect: Optional[float] = None      # seconds after t0
    nf_onset: dict[str, float] = field(default_factory=dict)  # nf -> seconds after t0


# --------------------------------------------------------------------------- #
# Core detectors
# --------------------------------------------------------------------------- #

def _center_spread(values) -> tuple[float, float]:
    """Baseline location and scale for the continuous z-score detectors.

    ESTIMATOR="mean" -> (mean, std), our default. ESTIMATOR="mad" ->
    (median, 1.4826*MAD), the robust modified-z-score estimator used by PRISM;
    the 1.4826 factor makes MAD a consistent estimator of sigma for normal
    data, so the same Z keeps its "~sigmas" meaning. A constant baseline gives
    spread 0 under either estimator, which is exactly when the flat-baseline
    floors take over."""
    v = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if len(v) == 0:
        return 0.0, 0.0
    if ESTIMATOR == "mad":
        med = float(v.median())
        mad = float((v - med).abs().median())
        return med, 1.4826 * mad
    return float(v.mean()), float(v.std(ddof=0))


def _zscore_onset(series: pd.DataFrame, base_vals, t0: float,
                  floor: float = 0.0, ratio: float = 0.0) -> Optional[float]:
    """Rolling-window z-score with flat-baseline guard + PERSIST persistence.

    A window is anomalous when |wmean - bmean| exceeds max(Z*sigma,
    FLAT_REL*|bmean|, floor) AND, when `ratio` is set, the window mean is a
    real multiple of the baseline (>= ratio*b or <= b/ratio) — this kills the
    near-zero bursty-CPU false positives. Returns seconds-after-t0 of first
    sustained crossing, else None."""
    base = pd.to_numeric(pd.Series(base_vals), errors="coerce").dropna()
    if len(base) < 3:
        return None
    bmean, bstd = _center_spread(base)
    s = series[series["timestamp"] >= t0].sort_values("timestamp")
    vals = s["value"].to_numpy()
    ts = s["timestamp"].to_numpy()
    if len(vals) < WIN + PERSIST:
        return None
    floor = floor * FLOOR_MULT
    ratio = ratio * RATIO_MULT if ratio else 0.0
    thresh = max(Z * bstd, FLAT_REL * REL_MULT * abs(bmean), floor, FLAT_EPS)
    ab = abs(bmean)
    streak, streak_start = 0, None
    for i in range(len(vals) - WIN + 1):
        wmean = float(vals[i:i + WIN].mean())
        ok = abs(wmean - bmean) > thresh
        if ok and ratio:
            ok = (abs(wmean) >= ratio * ab) or (ab > FLAT_EPS and abs(wmean) <= ab / ratio)
        if ok:
            if streak == 0:
                streak_start = ts[i]
            streak += 1
            if streak >= PERSIST:
                return max(0.0, float(streak_start) - t0)
        else:
            streak, streak_start = 0, None
    return None


def _detect_metric_zscore(ctx: Ctx, sig: Signal) -> Hit:
    pre = _read_prom(ctx.root, "pre", sig.source)
    dur = _read_prom(ctx.root, "during", sig.source)
    if pre is None or dur is None:
        return Hit()
    splitter = _beyla_series_by_nf if sig.source.startswith("beyla_") else _series_by_nf
    pre_nf = splitter(pre)
    dur_nf = splitter(dur)
    hit = Hit()
    for nf, dseries in dur_nf.items():
        base = pre_nf.get(nf, pd.DataFrame({"value": []}))["value"]
        on = _zscore_onset(dseries, base, ctx.t0, sig.floor, sig.ratio)
        if on is not None:
            hit.manifested = True
            hit.nf_onset[nf] = on
            hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
    return hit


def _detect_counter(ctx: Ctx, sig: Signal) -> Hit:
    pre = _read_prom(ctx.root, "pre", sig.source)
    dur = _read_prom(ctx.root, "during", sig.source)
    if dur is None:
        return Hit()
    pre_max = 0.0
    if pre is not None:
        pre_agg = pre.groupby("timestamp")["value"].sum()
        pre_max = float(pre_agg.max()) if len(pre_agg) else 0.0
    hit = Hit()
    # per-NF onset
    dd = dur.copy()
    dd["nf"] = dd["pod"].map(pod_to_nf) if "pod" in dd.columns else "_cluster"
    for nf, g in dd.groupby("nf", dropna=False):
        agg = g[g["timestamp"] >= ctx.t0].groupby("timestamp")["value"].sum().sort_index()
        streak, start = 0, None
        for t, v in agg.items():
            if v > pre_max + COUNTER_EPS:
                if streak == 0:
                    start = t
                streak += 1
                if streak >= COUNTER_PERSIST:
                    on = max(0.0, float(start) - ctx.t0)
                    hit.manifested = True
                    hit.nf_onset[str(nf)] = on
                    hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
                    break
            else:
                streak, start = 0, None
    return hit


def _detect_podstep(ctx: Ctx, sig: Signal) -> Hit:
    pre = _read_prom(ctx.root, "pre", sig.source)
    dur = _read_prom(ctx.root, "during", sig.source)
    if dur is None:
        return Hit()
    hit = Hit()
    for df, _ in ((pre, "pre"), (dur, "during")):
        if df is not None and "pod" in df.columns:
            df["nf"] = df["pod"].map(pod_to_nf)
    pre_max = {}
    if pre is not None and "pod" in pre.columns:
        for nf, g in pre.assign(nf=pre["pod"].map(pod_to_nf)).groupby("nf"):
            pre_max[nf] = float(g["value"].max())
    if "pod" not in dur.columns:
        return hit
    dd = dur.assign(nf=dur["pod"].map(pod_to_nf))
    for nf, g in dd.groupby("nf", dropna=False):
        if nf is None:
            continue
        base = pre_max.get(nf, 0.0)
        g2 = g[g["timestamp"] >= ctx.t0].sort_values("timestamp")
        over = g2[g2["value"] > base + 0.5]
        if not over.empty:
            on = max(0.0, float(over["timestamp"].iloc[0]) - ctx.t0)
            hit.manifested = True
            hit.nf_onset[str(nf)] = on
            hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
    return hit


def _detect_poddrop(ctx: Ctx, sig: Signal) -> Hit:
    dur = _read_prom(ctx.root, "during", sig.source)
    if dur is None or "pod" not in dur.columns:
        return Hit()
    hit = Hit()
    dd = dur.assign(nf=dur["pod"].map(pod_to_nf))
    for nf, g in dd.groupby("nf", dropna=False):
        if nf is None:
            continue
        g2 = g[g["timestamp"] >= ctx.t0].groupby("timestamp")["value"].min().sort_index()
        zero = g2[g2 < 0.5]
        if len(zero) >= PERSIST:
            on = max(0.0, float(zero.index[0]) - ctx.t0)
            hit.manifested = True
            hit.nf_onset[str(nf)] = on
            hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
    return hit


def _detect_event(ctx: Ctx, sig: Signal) -> Hit:
    f = ctx.root / "events" / "during" / "k8s_events.json"
    if not f.exists():
        return Hit()
    try:
        events = json.loads(f.read_text())
    except Exception:
        return Hit()
    hit = Hit()
    for e in events:
        reason = e.get("reason", "")
        if reason in K8S_WARN_REASONS or (e.get("type") == "Warning" and reason not in K8S_IGNORE_REASONS):
            try:
                t = _iso_to_epoch(e["time"])
            except Exception:
                continue
            on = max(0.0, t - ctx.t0)
            hit.manifested = True
            nf = pod_to_nf(e.get("object", "")) or "_cluster"
            cur = hit.nf_onset.get(nf)
            hit.nf_onset[nf] = on if cur is None else min(cur, on)
            hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
    return hit


def _read_spans(root: Path, phase: str) -> Optional[pd.DataFrame]:
    f = root / "jaeger" / phase / "spans_flat.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, dtype=str)
    except Exception:
        return None
    if df.empty:
        return None
    df["start_us"] = pd.to_numeric(df["start_us"], errors="coerce")
    df["error"] = pd.to_numeric(df["error"], errors="coerce").fillna(0)
    df["duration_us"] = pd.to_numeric(df["duration_us"], errors="coerce")
    return df.dropna(subset=["start_us"])


def _detect_jaeger(ctx: Ctx, sig: Signal) -> Hit:
    pre = _read_spans(ctx.root, "pre")
    dur = _read_spans(ctx.root, "during")
    if dur is None or dur.empty:
        return Hit()
    hit = Hit()
    metric = sig.source  # 'error_rate' | 'p95'
    for svc, g in dur.groupby("service"):
        if pre is not None and svc in set(pre["service"]):
            pg = pre[pre["service"] == svc]
            base_err = float(pg["error"].mean())
            base_p95 = float(pg["duration_us"].quantile(0.95)) if len(pg) else 0.0
        else:
            base_err, base_p95 = 0.0, 0.0
        g = g.copy()
        g["bin"] = ((g["start_us"] / 1e6) // BIN_S) * BIN_S
        streak, start = 0, None
        for b, bg in g.groupby("bin"):
            if metric == "error_rate":
                cur = float(bg["error"].mean())
                anom = cur > base_err + JAEGER_ERR_DELTA and bg["error"].sum() >= 1
            else:
                cur = float(bg["duration_us"].quantile(0.95)) if len(bg) else 0.0
                anom = base_p95 > 0 and cur > JAEGER_P95_RATIO * base_p95
            if anom:
                if streak == 0:
                    start = b
                streak += 1
                if streak >= PERSIST:
                    on = max(0.0, float(start) - ctx.t0)  # `start` is already epoch seconds
                    hit.manifested = True
                    hit.nf_onset[str(svc)] = min(hit.nf_onset.get(str(svc), 1e18), on)
                    hit.t_detect = on if hit.t_detect is None else min(hit.t_detect, on)
                    break
            else:
                streak, start = 0, None
    return hit


def _loki_bins(root: Path, phase: str, stream: str):
    f = root / "loki" / phase / f"{stream}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, dtype=str)
    except Exception:
        return None
    if df.empty or "timestamp_ns" not in df:
        return None
    df["ts"] = pd.to_numeric(df["timestamp_ns"], errors="coerce") / 1e9
    df = df.dropna(subset=["ts"])
    return df


def _detect_loki(ctx: Ctx, sig: Signal) -> Hit:
    pre = _loki_bins(ctx.root, "pre", sig.source)
    dur = _loki_bins(ctx.root, "during", sig.source)
    if dur is None or dur.empty:
        return Hit()
    # baseline bin-count distribution
    if pre is not None and not pre.empty:
        pb = (pre["ts"] // BIN_S).value_counts()
        bmean, bstd = _center_spread(pb)
    else:
        bmean, bstd = 0.0, 0.0
    d = dur[dur["ts"] >= ctx.t0].copy()
    if d.empty:
        return Hit()
    d["bin"] = (d["ts"] // BIN_S) * BIN_S
    counts = d.groupby("bin").size().sort_index()
    thresh = max(Z * bstd, FLAT_REL * REL_MULT * abs(bmean)) + LOKI_MIN_EXTRA
    hit = Hit()
    streak, start = 0, None
    for b, c in counts.items():
        if c - bmean > thresh:
            if streak == 0:
                start = b
            streak += 1
            if streak >= PERSIST:
                hit.manifested = True
                hit.t_detect = max(0.0, float(start) - ctx.t0)
                break
        else:
            streak, start = 0, None
    if hit.manifested:
        # NF attribution: which app produced the surge
        app_col = "app" if "app" in d.columns else ("pod" if "pod" in d.columns else None)
        if app_col:
            top = d[app_col].map(lambda x: pod_to_nf(x) or (x if isinstance(x, str) else None))
            for nf, cnt in top.value_counts().items():
                if nf:
                    hit.nf_onset[str(nf)] = hit.t_detect
    return hit


def _detect_nrf(ctx: Ctx, sig: Signal) -> Hit:
    pre_f = ctx.root / "nrf" / "pre" / "nrf_registrations.json"
    dur_f = ctx.root / "nrf" / "during" / "nrf_registrations.json"
    if not dur_f.exists():
        return Hit()
    try:
        dur = json.loads(dur_f.read_text())
        pre = json.loads(pre_f.read_text()) if pre_f.exists() else {}
    except Exception:
        return Hit()
    hit = Hit()
    if isinstance(dur, dict) and "error" in dur:
        hit.manifested = True
        return hit
    for nf, n in (pre.items() if isinstance(pre, dict) else []):
        if isinstance(n, (int, float)):
            d = dur.get(nf, n)
            if isinstance(d, (int, float)) and d < n:
                hit.manifested = True
    return hit


_DISPATCH = {
    "zscore": _detect_metric_zscore,
    "counter": _detect_counter,
    "podstep": _detect_podstep,
    "poddrop": _detect_poddrop,
    "event": _detect_event,
    "jaeger": _detect_jaeger,
    "loki": _detect_loki,
    "nrf": _detect_nrf,
}


def _detect_rtt(ctx: Ctx, sig: Signal) -> Hit:
    def load(phase):
        f = ctx.root / "rtt" / phase / "ue_rtt.csv"
        if not f.exists():
            return None
        try:
            df = pd.read_csv(f, dtype=str)
        except Exception:
            return None
        if df.empty or "timestamp_ms" not in df:
            return None
        df["timestamp"] = pd.to_numeric(df["timestamp_ms"], errors="coerce") / 1000.0
        df["value"] = pd.to_numeric(df["rtt_ms"], errors="coerce")
        df["loss"] = (df.get("status", "ok") == "loss").astype(float)
        return df.dropna(subset=["timestamp"])

    pre, dur = load("pre"), load("during")
    if dur is None:
        return Hit()
    hit = Hit()
    base = pre["value"].dropna() if pre is not None else pd.Series(dtype=float)
    # RTT spike
    rtt_series = dur.dropna(subset=["value"])[["timestamp", "value"]]
    if len(base) >= 3 and not rtt_series.empty:
        bmean, bstd = _center_spread(base)
        thr = max(Z * bstd, RTT_MIN_DELTA_MS)
        s = rtt_series[rtt_series["timestamp"] >= ctx.t0].sort_values("timestamp")
        v = s["value"].to_numpy(); t = s["timestamp"].to_numpy()
        streak, start = 0, None
        for i in range(len(v) - WIN + 1):
            if abs(v[i:i + WIN].mean() - bmean) > thr:
                if streak == 0:
                    start = t[i]
                streak += 1
                if streak >= PERSIST:
                    hit.manifested = True
                    hit.t_detect = max(0.0, float(start) - ctx.t0)
                    break
            else:
                streak, start = 0, None
    # packet loss onset (loss fraction per bin)
    d = dur[dur["timestamp"] >= ctx.t0].copy()
    if not d.empty:
        d["bin"] = (d["timestamp"] // BIN_S) * BIN_S
        lf = d.groupby("bin")["loss"].mean()
        bad = lf[lf > 0.2]
        if len(bad) >= PERSIST:
            on = max(0.0, float(bad.index[0]) - ctx.t0)
            if not hit.manifested or hit.t_detect is None or on < hit.t_detect:
                hit.manifested = True
                hit.t_detect = on
    if hit.manifested:
        hit.nf_onset["upf"] = hit.t_detect  # RTT path terminates at UPF GW
    return hit


_DISPATCH["rtt_kind"] = _detect_rtt


def detect_fault(ctx: Ctx) -> dict[str, Hit]:
    """Run every signal detector once for a fault. Returns key -> Hit."""
    res: dict[str, Hit] = {}
    for sig in SIGNALS:
        try:
            if sig.modality == "rtt":
                res[sig.key] = _detect_rtt(ctx, sig)
            else:
                res[sig.key] = _DISPATCH[sig.kind](ctx, sig)
        except Exception as exc:  # never let one bad file kill the suite
            res[sig.key] = Hit()
            res[sig.key].nf_onset["_error"] = -1.0
            print(f"  ! {ctx.slug}/{sig.key}: {type(exc).__name__}: {exc}")
    return res


_CACHE: dict[str, tuple[Ctx, dict[str, Hit]]] = {}


def get_detection(slug: str) -> tuple[Ctx, dict[str, Hit]]:
    if slug not in _CACHE:
        ctx = load_ctx(slug)
        _CACHE[slug] = (ctx, detect_fault(ctx))
    return _CACHE[slug]


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
