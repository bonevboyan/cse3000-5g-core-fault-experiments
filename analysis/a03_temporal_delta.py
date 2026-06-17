"""03 - Cross-run temporal delta: per-fault, per-layer time-to-first-signal
difference between two runs (default: run2 - canonical).

Mirrors the a02 temporal heatmap layout (3 layer rows x faults, family-grouped),
but each cell encodes the signed onset difference rather than an absolute time:

    delta = t_first(run2) - t_first(canonical)     [seconds]

  * both runs fired      -> diverging colour (blue = run2 earlier, red = later),
                            big label = signed delta, small label = "Cv->Rv"
  * fired canonical only -> "lost" (run2 went silent on that layer)
  * fired run2 only      -> "new"  (run2 gained a layer)
  * both never           -> grey "--"

Usage:
    python3 -m analysis.a03_temporal_delta \
        [base_dir=data/analysis] [other_dir=data/analysis-fault-detection-run2]

Outputs (written under base_dir):
    plots/temporal_delta_<other>_vs_<base>.png
    temporal_delta_<other>_vs_<base>.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm

import analysis.common as c
from analysis.a02_temporal import SIG_ABBR


def _load(d: Path) -> pd.DataFrame:
    return pd.read_csv(d / "temporal_layers.csv").set_index("fault")


def _t(df: pd.DataFrame, fault: str, layer: str):
    """First-signal time for (fault, layer), or NaN if the layer stayed silent."""
    v = df.loc[fault, f"{layer}_t"]
    return np.nan if (v == "" or pd.isna(v)) else float(v)


def run(base_dir: Path | None = None, other_dir: Path | None = None) -> pd.DataFrame:
    base = base_dir or (c.REPRO_DIR / "data" / "analysis")
    other = other_dir or (c.REPRO_DIR / "data" / "analysis-fault-detection-run2")
    base_lbl = "canonical" if base.name == "analysis" else base.name.replace("analysis-", "")
    other_lbl = other.name.replace("analysis-", "")

    cb, co = _load(base), _load(other)
    faults = [f for f in cb.index if f in co.index]   # keep base (family-grouped) order

    # delta matrix + categorical state per (layer, fault)
    D = np.full((len(c.LAYERS), len(faults)), np.nan)
    state = [[None] * len(faults) for _ in c.LAYERS]   # 'both'|'lost'|'new'|'none'
    rows = []
    for x, f in enumerate(faults):
        rec = {"fault": f, "family": c.FAULTS[f].family}
        for y, L in enumerate(c.LAYERS):
            tb, to = _t(cb, f, L), _t(co, f, L)
            if not np.isnan(tb) and not np.isnan(to):
                D[y, x] = to - tb
                state[y][x] = "both"
            elif not np.isnan(tb):
                state[y][x] = "lost"
            elif not np.isnan(to):
                state[y][x] = "new"
            else:
                state[y][x] = "none"
            rec[f"{L}_base"] = tb
            rec[f"{L}_other"] = to
            rec[f"{L}_delta"] = D[y, x]
        rows.append(rec)
    out = pd.DataFrame(rows).set_index("fault")

    _plot(D, state, cb, co, faults, base_lbl, other_lbl, base)
    out.to_csv(base / f"temporal_delta_{other_lbl}_vs_{base_lbl}.csv")
    n_both = sum(s == "both" for r in state for s in r)
    n_zero = int(np.nansum(np.abs(D) < 1e-9))
    print(f"[03] temporal delta {other_lbl} vs {base_lbl}: "
          f"{n_both} comparable cells, {n_zero} identical onset, "
          f"max |delta|={np.nanmax(np.abs(D)):.0f}s")
    return out


def _plot(D, state, cb, co, faults, base_lbl, other_lbl, base: Path) -> None:
    lim = float(np.nanmax(np.abs(D))) if np.isfinite(np.nanmax(np.abs(D))) else 1.0
    lim = max(lim, 1.0)
    norm = TwoSlopeNorm(vmin=-lim, vcenter=0.0, vmax=lim)
    cmap = plt.get_cmap("RdBu_r").copy()

    fig, ax = plt.subplots(figsize=(max(18, len(faults) * 0.95), 6.6))
    im = ax.imshow(D, aspect="auto", cmap=cmap, norm=norm,
                   interpolation="nearest", rasterized=True)

    CAT = {"lost": "#f4c7a1", "new": "#a9cce3", "none": "#e8e8e8"}
    for y in range(D.shape[0]):
        for x in range(D.shape[1]):
            st = state[y][x]
            L = c.LAYERS[y]
            if st == "both":
                d = D[y, x]
                big = f"{d:+.0f}" if abs(d) >= 1e-9 else "0"
                cv, ov = cb.loc[faults[x], f"{L}_signal"], co.loc[faults[x], f"{L}_signal"]
                ab = SIG_ABBR.get(cv, str(cv)[:6])
                shade = abs(norm(d) - 0.5) * 2  # 0 at centre, 1 at extremes
                color = "white" if shade > 0.6 else "black"
                if cv == ov:
                    ax.text(x, y - 0.18, big, ha="center", va="center",
                            fontsize=11, color=color, fontweight="bold")
                    ax.text(x, y + 0.22, ab, ha="center", va="center",
                            fontsize=8, color=color)
                else:
                    # different first signal per run -> stack on two lines for room
                    ob = SIG_ABBR.get(ov, str(ov)[:6])
                    ax.text(x, y - 0.28, big, ha="center", va="center",
                            fontsize=11, color=color, fontweight="bold")
                    ax.text(x, y + 0.06, ab, ha="center", va="center",
                            fontsize=8, color=color)
                    ax.text(x, y + 0.30, ob, ha="center", va="center",
                            fontsize=8, color=color)
            else:
                ax.add_patch(plt.Rectangle((x - 0.5, y - 0.5), 1, 1,
                                           facecolor=CAT[st], edgecolor="none", zorder=1))
                lbl = {"lost": "lost", "new": "new", "none": "never"}[st]
                ax.text(x, y, lbl, ha="center", va="center", fontsize=9,
                        color="#555", style="italic", zorder=2)

    ax.set_yticks(range(len(c.LAYERS)))
    ax.set_yticklabels([L.capitalize() for L in c.LAYERS], fontsize=14)
    ax.set_xticks(range(len(faults)))
    ax.set_xticklabels(faults, rotation=45, ha="right",
                       rotation_mode="anchor", fontsize=12)

    # family separators + class labels along the top (same as a02)
    fams = [c.FAULTS[s].family for s in faults]
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
                color="tab:red", fontsize=10, weight="bold", ha="center", va="bottom")

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Δ time to first signal (canonical vs rerun), s",
                   fontsize=13)
    cbar.ax.tick_params(labelsize=11)
    fig.tight_layout()
    p = base / "plots" / f"temporal_delta_{other_lbl}_vs_{base_lbl}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    a = sys.argv[1:]
    base = Path(a[0]) if len(a) > 0 else None
    other = Path(a[1]) if len(a) > 1 else None
    run(base, other)
