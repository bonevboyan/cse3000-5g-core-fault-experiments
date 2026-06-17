"""02 - Temporal manifestation: when does each layer first show a signal.

For every fault, time-to-first-signal (seconds after injection t0) per layer,
and which layer manifests first ("never" if a layer stays silent).

Outputs:
  data/analysis/temporal_layers.csv       (fault x {infra,orch,app} first-time + which)
  data/analysis/first_signal_per_fault.csv (fault -> earliest signal overall)
  data/analysis/plots/temporal_heatmap.png
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import analysis.common as c

NEVER = np.inf

# Reporting-resolution bucket per signal (seconds). This is the smallest
# time-step the detector can distinguish in its first-crossing report — the
# *step* between consecutive reportable timestamps, not the window length.
# Width of the [t, t+res) range shown for each cell on the heatmap.
#
#   K8s events ......................................... sub-second (no bucket)
#   NRF snapshot ....................................... n/a (snapshot, never timed)
#   Z-rtt: detector slides by one ping (~1 Hz cadence)    1 s
#   All other Z / Z-loki / Z-jaeger / CTR / STEP / DROP   BIN_S (= 5 s; bin step)
_SUBSECOND_KINDS = {"event"}
_RTT_BUCKET_S = 1.0                  # ping cadence; window length is 6 s but step is 1 s

def _native_res_s(sig: c.Signal) -> float:
    if sig.kind in _SUBSECOND_KINDS or sig.kind == "nrf":
        return 0.0
    if sig.modality == "rtt":
        return _RTT_BUCKET_S
    return float(c.BIN_S)


def _native_res_for_key(k: str) -> float:
    sig = c.SIGNAL_BY_KEY.get(k)
    return 0.0 if sig is None else _native_res_s(sig)


# Short cell labels (kept tight so two lines fit per heatmap cell)
SIG_ABBR = {
    "cpu_usage": "CPU", "cpu_throttle": "CPUthr", "mem_working_set": "MEM",
    "net_rx": "NETrx", "net_tx": "NETtx", "node_cpu": "nCPU", "node_mem_avail": "nMEM",
    "rtt": "RTT",
    "pod_restart": "podRst", "pod_ready_drop": "podRdy", "pod_running_drop": "podRun",
    "k8s_warning": "k8sEvt",
    "beyla_srv_latency": "BeylaS", "beyla_cli_latency": "BeylaC",
    "beyla_srv_error": "BeylaSE", "beyla_cli_error": "BeylaCE", "beyla_srv_reqrate": "BeylaRQ",
    "amf_registered_sub": "AMFsub", "amf_ran_ue": "AMFue",
    "amf_gnb": "AMFgnb", "amf_sessions": "AMFses",
    "pfcp_sessions_active": "PFCPses", "pfcp_peers_active": "PFCPpr",
    "smf_ues_active": "SMFues", "smf_bearers_active": "SMFbr",
    "smf_pdu_succ": "SMFpdu", "smf_n4_estab": "SMFn4", "smf_n4_report_succ": "SMFn4r",
    "upf_n4_estab": "UPFn4",
    "amf_reg_fail": "AMFreg!", "amf_auth_fail": "AMFaut!",
    "amf_auth_reject": "AMFrej!", "gtp_node_failed": "GTPf!",
    "log_errors": "logErr", "log_ue_failures": "logUE",
    "log_scp_routing": "logSCP", "log_nrf_lifecycle": "logNRF",
    "trace_error_rate": "trErr", "trace_p95_latency": "trP95",
    "nrf_dereg": "NRFdrg",
}


def run() -> pd.DataFrame:
    c.ensure_dirs()
    by_key = {s.key: s for s in c.SCORED_SIGNALS}
    layer_of = {k: s.layer for k, s in by_key.items()}
    rows, first_rows = [], []
    for slug in c.fault_slugs_by_family():
        _, res = c.get_detection(slug)
        per_layer = {L: (NEVER, None) for L in c.LAYERS}
        overall = (NEVER, None)
        for k, h in res.items():
            if k not in layer_of or not h.manifested or h.t_detect is None:
                continue
            L = layer_of[k]
            if h.t_detect < per_layer[L][0]:
                per_layer[L] = (h.t_detect, k)
            if h.t_detect < overall[0]:
                overall = (h.t_detect, k)
        row = {"fault": slug}
        for L in c.LAYERS:
            t, k = per_layer[L]
            row[f"{L}_t"] = "" if t is NEVER else round(t, 1)
            row[f"{L}_signal"] = k or ""
            row[f"{L}_floored"] = (k is not None
                                   and t is not NEVER
                                   and t < _native_res_s(by_key[k]))
        ranked = [L for L in c.LAYERS if per_layer[L][0] is not NEVER]
        ranked.sort(key=lambda L: per_layer[L][0])
        row["first_layer"] = ranked[0] if ranked else "none"
        row["layer_order"] = " -> ".join(ranked) if ranked else "none"
        rows.append(row)
        overall_floored = (overall[1] is not None
                           and overall[0] is not NEVER
                           and overall[0] < _native_res_s(by_key[overall[1]]))
        first_rows.append({"fault": slug,
                           "first_signal": overall[1] or "none",
                           "first_layer": (layer_of.get(overall[1]) if overall[1] else "none"),
                           "t_detect_s": "" if overall[0] is NEVER else round(overall[0], 1),
                           "t_floored": overall_floored,
                           "family": c.FAULTS[slug].family})
    tl = pd.DataFrame(rows).set_index("fault")
    tl.to_csv(c.OUT_DIR / "temporal_layers.csv")
    pd.DataFrame(first_rows).set_index("fault").to_csv(c.OUT_DIR / "first_signal_per_fault.csv")
    _heatmap(tl)
    print(f"[02] temporal_layers.csv  first-layer dist: "
          f"{tl['first_layer'].value_counts().to_dict()}")
    return tl


def _heatmap(tl: pd.DataFrame) -> None:
    M, Sigs, Floored = [], [], []
    for L in c.LAYERS:
        M.append([np.nan if tl.loc[i, f"{L}_t"] == "" else float(tl.loc[i, f"{L}_t"])
                  for i in tl.index])
        Sigs.append([tl.loc[i, f"{L}_signal"] for i in tl.index])
        Floored.append([bool(tl.loc[i, f"{L}_floored"]) for i in tl.index])
    M = np.array(M)
    fig, ax = plt.subplots(figsize=(max(18, len(tl) * 0.95), 6.6))
    im = ax.imshow(M, aspect="auto", cmap="viridis_r",
                   interpolation="nearest", rasterized=True)
    ax.set_yticks(range(len(c.LAYERS)))
    ax.set_yticklabels([L.capitalize() for L in c.LAYERS], fontsize=14)
    ax.set_xticks(range(len(tl)))
    ax.set_xticklabels(tl.index, rotation=45, ha="right",
                       rotation_mode="anchor", fontsize=12)
    vmax = float(np.nanmax(M)) if np.isfinite(np.nanmax(M)) else 1.0
    for y in range(M.shape[0]):
        for x in range(M.shape[1]):
            v = M[y, x]
            if np.isnan(v):
                ax.add_patch(plt.Rectangle((x - 0.5, y - 0.5), 1, 1,
                                           facecolor="#e8e8e8",
                                           edgecolor="none", zorder=1))
                ax.text(x, y, "never", ha="center", va="center",
                        fontsize=10, color="#777", style="italic", zorder=2)
                continue
            res = _native_res_for_key(Sigs[y][x])
            if res > 0:
                # Bin/sample-step signals: render as [floor(t), floor(t)+res).
                # No grid-snap — recorded t already encodes the bucket start.
                lo = int(np.floor(v))
                label = f"{lo}–{lo + int(res)}"
            else:
                label = f"{v:.0f}"      # sub-second signals (K8s events) — point
            abbr = SIG_ABBR.get(Sigs[y][x], Sigs[y][x][:6])
            color = "white" if v > vmax / 2 else "black"
            ax.text(x, y - 0.18, label, ha="center", va="center",
                    fontsize=11, color=color, fontweight="bold")
            ax.text(x, y + 0.22, abbr, ha="center", va="center",
                    fontsize=8, color=color)
    # family separators + horizontally-centered class labels along the top
    fams = [c.FAULTS[s].family for s in tl.index]
    spans: list[tuple[str, int, int]] = []
    for i, f in enumerate(fams):
        if spans and spans[-1][0] == f:
            spans[-1] = (f, spans[-1][1], i)
        else:
            spans.append((f, i, i))
    for f, lo, hi in spans:
        if lo > 0:
            ax.axvline(lo - 0.5, color="tab:red", lw=1.5)
        ax.text((lo + hi) / 2, -0.85, f.replace("_", " ").title(),
                color="tab:red", fontsize=10, weight="bold",
                ha="center", va="bottom")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("time to first signal (s after t0)", fontsize=13)
    cbar.ax.tick_params(labelsize=11)
    # Caption strip removed: the figure caption in the paper carries the description.
    fig.tight_layout()
    fig.savefig(c.PLOT_DIR / "temporal_heatmap.png", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    run()
