"""04 - Cross-NF propagation: how a fault spreads from the target NF outward.

Per fault, collect first-signal onset per NF (across Prometheus container
metrics, Jaeger services, Loki apps, K8s event objects), order them in time,
and measure lag from the chaos target NF to each downstream NF.

Outputs:
  data/analysis/propagation_summary.csv   (fault: target, blast radius, depth)
  data/analysis/propagation/<slug>.json   (ordered NF onset chain)
  data/analysis/plots/propagation_<slug>.png
"""
from __future__ import annotations

import json

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import analysis.common as c

# infra NFs that are not Open5GS signalling peers (kept but flagged)
NON_NF = {"_cluster", "_all", "_error", "ue"}


def _nf_onsets(res) -> dict[str, dict]:
    """nf -> {first_t, first_signal, signals:{key:t}, layers:set}."""
    agg: dict[str, dict] = {}
    for key, h in res.items():
        if not h.manifested:
            continue
        sig = c.SIGNAL_BY_KEY[key]
        if sig.caveat:
            continue
        for nf, t in h.nf_onset.items():
            if nf in NON_NF or t < 0:
                continue
            a = agg.setdefault(nf, {"first_t": t, "first_signal": key,
                                    "signals": {}, "layers": set()})
            a["signals"][key] = round(t, 1)
            a["layers"].add(sig.layer)
            if t < a["first_t"]:
                a["first_t"], a["first_signal"] = t, key
    return agg


def run() -> pd.DataFrame:
    c.ensure_dirs()
    (c.OUT_DIR / "propagation").mkdir(exist_ok=True)
    rows = []
    for slug in c.fault_slugs():
        meta = c.FAULTS[slug]
        _, res = c.get_detection(slug)
        agg = _nf_onsets(res)
        chain = sorted(agg.items(), key=lambda kv: kv[1]["first_t"])
        target = meta.target_nf
        t_target = agg.get(target, {}).get("first_t")
        out = {"fault": slug, "target_nf": target, "chaos": meta.chaos,
               "chain": []}
        for nf, a in chain:
            out["chain"].append({
                "nf": nf, "first_t": round(a["first_t"], 1),
                "first_signal": a["first_signal"],
                "lag_from_target": (None if t_target is None
                                    else round(a["first_t"] - t_target, 1)),
                "layers": sorted(a["layers"]),
                "signals": a["signals"],
            })
        (c.OUT_DIR / "propagation" / f"{slug}.json").write_text(json.dumps(out, indent=2))

        affected = [nf for nf, _ in chain]
        rows.append({
            "fault": slug, "target_nf": target,
            "target_detected": target in agg,
            "blast_radius": len(affected),
            "affected_nfs": ";".join(affected),
            "propagation_depth_s": (round(chain[-1][1]["first_t"] - chain[0][1]["first_t"], 1)
                                    if chain else 0.0),
            "first_nf": chain[0][0] if chain else "none",
        })
        _plot(slug, chain, target)
    summ = pd.DataFrame(rows).set_index("fault")
    summ.to_csv(c.OUT_DIR / "propagation_summary.csv")
    print(f"[04] propagation_summary.csv  mean blast radius "
          f"{summ['blast_radius'].mean():.1f} NFs")
    return summ


def _plot(slug, chain, target):
    if not chain:
        return
    nfs = [nf for nf, _ in chain]
    ts = [a["first_t"] for _, a in chain]
    colors = ["tab:red" if nf == target else "tab:blue" for nf in nfs]
    fig, ax = plt.subplots(figsize=(8, max(2.2, len(nfs) * 0.4)))
    ax.barh(range(len(nfs)), ts, color=colors)
    ax.set_yticks(range(len(nfs)))
    ax.set_yticklabels(nfs)
    ax.invert_yaxis()
    ax.set_xlabel("first-signal onset (s after t0)")
    ax.set_title(f"{slug}  (red = chaos target: {target})")
    for i, (nf, a) in enumerate(chain):
        ax.text(ts[i], i, f"  {a['first_signal']}", va="center", fontsize=6)
    fig.tight_layout()
    fig.savefig(c.PLOT_DIR / f"propagation_{slug}.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    run()
