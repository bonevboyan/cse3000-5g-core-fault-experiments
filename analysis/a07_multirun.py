"""07 - Multi-run aggregation: cross-run stability of the fault atlas.

Each independent run of the 22-fault batch produces a binary atlas
(fault x signal, 0/1) via a01. This script stacks the per-run atlases and
turns the single-run binary atlas into a *confidence* atlas:

  * fire frequency  - fraction of runs in which each (fault, signal) cell fired
  * consensus       - majority vote (>= 0.5) binary atlas
  * cell agreement  - fraction of runs matching the consensus, averaged over
                      all cells (the cross-run analogue of a06's threshold
                      sensitivity; the headline reproducibility number)
  * unstable cells  - cells that fire in some runs but not others (the
                      near-threshold ones that motivate repeated runs)

Run after each run's own atlas (a01) has been generated, e.g.:
    FAULT_DATASET=fault-detection         python3 -m analysis.a01_fault_atlas
    FAULT_DATASET=fault-detection-run2    python3 -m analysis.a01_fault_atlas
    python3 -m analysis.a07_multirun      # auto-discovers run dirs

Or pass analysis dirs explicitly (each must contain fault_atlas.csv):
    python3 -m analysis.a07_multirun data/analysis data/analysis-fault-detection-run2

Outputs (always written to the canonical data/analysis/):
    multirun_atlas.csv         (fault x signal fire frequency, 0..1)
    multirun_stability.csv     (one row per unstable cell)
    plots/multirun_atlas.png   (confidence-shaded heatmap)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import analysis.common as c

# Always aggregate into the canonical analysis dir, regardless of FAULT_DATASET.
DATA_ROOT_DIR = c.REPRO_DIR / "data"
OUT = DATA_ROOT_DIR / "analysis"
PLOT = OUT / "plots"


def _discover() -> list[Path]:
    """Canonical run + any data/analysis-fault-detection-run* dirs."""
    dirs = [OUT] + sorted(DATA_ROOT_DIR.glob("analysis-fault-detection-run*"))
    return [d for d in dirs if (d / "fault_atlas.csv").exists()]


def _label(d: Path) -> str:
    return "canonical" if d.name == "analysis" else d.name.replace("analysis-", "")


def run(run_dirs: list[Path] | None = None) -> pd.DataFrame | None:
    PLOT.mkdir(parents=True, exist_ok=True)
    dirs = run_dirs if run_dirs else _discover()
    mats, labels = [], []
    for d in dirs:
        f = d / "fault_atlas.csv"
        if not f.exists():
            print(f"  ! {d}: no fault_atlas.csv, skipping")
            continue
        mats.append(pd.read_csv(f, index_col=0))
        labels.append(_label(d))

    if len(mats) < 2:
        print(f"[07] multirun: found {len(mats)} run(s) ({', '.join(labels) or 'none'}); "
              f"need >=2 to aggregate. Re-run after another batch completes, "
              f"or pass run dirs explicitly.")
        return None

    # Align on the cells common to every run, in canonical layout order.
    layer_of = {s.key: s.layer for s in c.SCORED_SIGNALS}
    rows = [s for s in c.fault_slugs_by_family() if all(s in m.index for m in mats)]
    if not rows:  # fall back to plain intersection if DATA_ROOT is a different run
        rows = sorted(set.intersection(*[set(m.index) for m in mats]),
                      key=lambda s: (c.FAULT_FAMILY_ORDER.index(c.FAULTS[s].family), s))
    cols = sorted(set.intersection(*[set(m.columns) for m in mats]),
                  key=lambda k: (c.LAYERS.index(layer_of.get(k, "application")), k))

    A = np.stack([m.loc[rows, cols].to_numpy(dtype=float) for m in mats])  # R x F x S
    n = A.shape[0]
    freq = A.mean(axis=0)
    consensus = (freq >= 0.5).astype(int)
    agree = np.maximum(freq, 1.0 - freq)            # per-cell fraction with majority
    overall = float(agree.mean()) * 100.0

    freq_df = pd.DataFrame(freq, index=rows, columns=cols)
    freq_df.to_csv(OUT / "multirun_atlas.csv")

    # Unstable cells: fired in some runs but not all.
    unstable = []
    for i, fault in enumerate(rows):
        for j, sig in enumerate(cols):
            fired = int(round(freq[i, j] * n))
            if 0 < fired < n:
                unstable.append({
                    "fault": fault, "signal": sig,
                    "layer": layer_of.get(sig, "?"),
                    "family": c.FAULTS[fault].family,
                    "fired": fired, "n_runs": n,
                    "freq": round(freq[i, j], 3),
                })
    pd.DataFrame(unstable).to_csv(OUT / "multirun_stability.csv", index=False)

    _plot(freq_df, layer_of, n, overall)

    n_cells = freq.size
    n_unstable = len(unstable)
    # Per-fault least-stable summary
    inst_per_fault = (pd.DataFrame(unstable).groupby("fault").size()
                      if unstable else pd.Series(dtype=int))
    worst = inst_per_fault.sort_values(ascending=False).head(3).to_dict()
    print(f"[07] multirun_atlas.csv  {n} runs ({', '.join(labels)})")
    print(f"     cross-run cell agreement: {overall:.1f}%  "
          f"({n_cells - n_unstable}/{n_cells} cells unanimous, {n_unstable} unstable)")
    if worst:
        print(f"     least-stable faults: " +
              ", ".join(f"{k} ({v})" for k, v in worst.items()))
    return freq_df


def _plot(freq: pd.DataFrame, layer_of: dict, n: int, overall: float) -> None:
    cols, rows = list(freq.columns), list(freq.index)
    fig, ax = plt.subplots(figsize=(max(20, len(cols) * 0.70),
                                    max(11, len(rows) * 0.62)))
    ax.imshow(freq.values, aspect="auto", cmap="Greys", vmin=0, vmax=1,
              interpolation="nearest")
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(cols, rotation=45, ha="right",
                       rotation_mode="anchor", fontsize=16)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows, fontsize=16)

    # layer separators + labels
    spans: list[tuple[str, int, int]] = []
    for i, k in enumerate(cols):
        L = layer_of.get(k, "application")
        if spans and spans[-1][0] == L:
            spans[-1] = (L, spans[-1][1], i)
        else:
            spans.append((L, i, i))
    for L, lo, hi in spans:
        if lo > 0:
            ax.axvline(lo - 0.5, color="tab:red", lw=2.0)
        ax.text((lo + hi) / 2, -1.4, L.capitalize(), color="tab:red",
                fontsize=18, weight="bold", ha="center", va="bottom")

    # family separators + labels
    fspans: list[tuple[str, int, int]] = []
    for i, s in enumerate(rows):
        f = c.FAULTS[s].family
        if fspans and fspans[-1][0] == f:
            fspans[-1] = (f, fspans[-1][1], i)
        else:
            fspans.append((f, i, i))
    x_right = len(cols) - 0.4
    for f, lo, hi in fspans:
        if lo > 0:
            ax.axhline(lo - 0.5, color="tab:blue", lw=1.8)
        ax.text(x_right, (lo + hi) / 2, f.replace("_", " ").title(),
                color="tab:blue", fontsize=13, weight="bold",
                ha="left", va="center")

    # No title: the figure caption (in the paper) carries the description, and the
    # ceiling-agreement % is intentionally not shown (see exact-match metric in text).
    fig.tight_layout()
    fig.savefig(PLOT / "multirun_atlas.png", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    run(args if args else None)
