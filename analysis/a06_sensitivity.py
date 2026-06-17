"""06 - Threshold sensitivity analysis.

Answers the reviewer question "why these constants?" by showing the atlas and
the layer-ordering conclusion are stable under perturbation of the detector
hyper-parameters. One-factor-at-a-time sweep around the baseline
(Z=3, persistence=2, window=30s); for each config we recompute the full
scored atlas and compare to the baseline:

  * cell_agreement  - % of (fault x signal) atlas cells unchanged vs baseline
  * first_layer_same - # faults whose first-manifesting layer is unchanged
  * total_fires / mean_signals_per_fault / first-layer distribution

If the qualitative findings hold across the grid, the exact constants are not
load-bearing - that is the scientific defense of the thresholds.

Outputs:
  data/analysis/sensitivity.csv
  data/analysis/plots/sensitivity.png
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
np.seterr(all="ignore")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import analysis.common as c

BASE = {"Z": 3.0, "PERSIST": 2, "WIN": c.WINDOW_S // c.BIN_S,
        "FLOOR": 1.0, "RATIO": 1.0}  # = (3, 2, 6, 1.0, 1.0)

# (name, Z, persist, win, floor_mult, ratio_mult). The floor/ratio multipliers
# scale the per-signal flat-baseline guard — for quiet signals that guard, not
# Z, is the binding gate, so this is the sensitivity check that actually
# matters for the hand-tuned constants.
_B = (BASE["Z"], BASE["PERSIST"], BASE["WIN"], BASE["FLOOR"], BASE["RATIO"])
GRID = [
    ("baseline", *_B),
    ("Z=2.5", 2.5, *_B[1:]),
    ("Z=3.5", 3.5, *_B[1:]),
    ("persist=1", _B[0], 1, *_B[2:]),
    ("persist=3", _B[0], 3, *_B[2:]),
    ("window=15s", _B[0], _B[1], 3, *_B[3:]),
    ("window=45s", _B[0], _B[1], 9, *_B[3:]),
    ("floor x0.5", *_B[:3], 0.5, _B[4]),
    ("floor x2.0", *_B[:3], 2.0, _B[4]),
    ("ratio x0.5", *_B[:4], 0.5),
    ("ratio x2.0", *_B[:4], 2.0),
    ("floor+ratio x0.5", *_B[:3], 0.5, 0.5),
    ("floor+ratio x2.0", *_B[:3], 2.0, 2.0),
    # Relative (30%) guard sweep: FLAT_REL x0.5 (->15%) and x2.0 (->60%). 8th
    # field = REL_MULT; 7th field (ESTIMATOR) held at "mean".
    ("rel-guard x0.5", *_B, "mean", 0.5),
    ("rel-guard x2.0", *_B, "mean", 2.0),
    # Robust estimator: median + 1.4826*MAD in place of mean + std (PRISM's
    # modified z-score). Shows the atlas does not depend on the choice of
    # baseline location/scale estimator. 7th field = ESTIMATOR.
    ("estimator=median/MAD", *_B, "mad"),
]

LAYER_OF = {s.key: s.layer for s in c.SCORED_SIGNALS}
SCORED = [s.key for s in c.SCORED_SIGNALS]


def _atlas_and_firstlayer(Z, persist, win, floor_m=1.0, ratio_m=1.0, est="mean",
                          rel_m=1.0):
    """Recompute scored atlas + first-manifesting layer per fault for one config."""
    c.Z, c.PERSIST, c.WIN = Z, persist, win  # detectors read these at call time
    c.FLOOR_MULT, c.RATIO_MULT = floor_m, ratio_m
    c.ESTIMATOR = est
    c.REL_MULT = rel_m
    slugs = c.fault_slugs()
    atlas = np.zeros((len(slugs), len(SCORED)), dtype=int)
    first_layer = {}
    for i, slug in enumerate(slugs):
        ctx = c.load_ctx(slug)
        res = c.detect_fault(ctx)        # bypasses the get_detection cache
        best = {L: np.inf for L in c.LAYERS}
        for j, k in enumerate(SCORED):
            h = res[k]
            if h.manifested:
                atlas[i, j] = 1
                if h.t_detect is not None and h.t_detect < best[LAYER_OF[k]]:
                    best[LAYER_OF[k]] = h.t_detect
        ranked = sorted((L for L in c.LAYERS if np.isfinite(best[L])),
                        key=lambda L: best[L])
        first_layer[slug] = ranked[0] if ranked else "none"
    return atlas, first_layer, slugs


def run() -> pd.DataFrame:
    c.ensure_dirs()
    saved = (c.Z, c.PERSIST, c.WIN, c.FLOOR_MULT, c.RATIO_MULT, c.ESTIMATOR,
             c.REL_MULT)
    try:
        base_atlas, base_fl, slugs = _atlas_and_firstlayer(
            BASE["Z"], BASE["PERSIST"], BASE["WIN"])
        rows = []
        for row in GRID:
            name, Z, p, w, fm, rm = row[:6]
            est = row[6] if len(row) > 6 else "mean"
            rel = row[7] if len(row) > 7 else 1.0
            atlas, fl, _ = _atlas_and_firstlayer(Z, p, w, fm, rm, est, rel)
            agree = (atlas == base_atlas).mean() * 100
            fl_same = sum(1 for s in slugs if fl[s] == base_fl[s])
            dist = pd.Series(list(fl.values())).value_counts().to_dict()
            rows.append({
                "config": name, "Z": Z, "persistence": p,
                "window_s": w * c.BIN_S, "floor_mult": fm, "ratio_mult": rm,
                "rel_mult": rel, "estimator": est,
                "total_fires": int(atlas.sum()),
                "mean_signals_per_fault": round(atlas.sum() / len(slugs), 2),
                "cell_agreement_pct": round(agree, 1),
                "first_layer_same": f"{fl_same}/{len(slugs)}",
                "first_layer_dist": str({k: dist.get(k, 0) for k in
                                         ("infrastructure", "orchestration",
                                          "application", "none")}),
            })
        df = pd.DataFrame(rows).set_index("config")
        df.to_csv(c.OUT_DIR / "sensitivity.csv")
        _plot(df)
        worst = df.loc[df.index != "baseline", "cell_agreement_pct"].min()
        print(f"[06] sensitivity.csv  worst-case atlas agreement vs baseline: "
              f"{worst:.1f}%  (>~85% => conclusions threshold-robust)")
        return df
    finally:
        (c.Z, c.PERSIST, c.WIN, c.FLOOR_MULT, c.RATIO_MULT, c.ESTIMATOR,
         c.REL_MULT) = saved
        c._CACHE.clear()  # drop any non-baseline detections from the shared cache


def _plot(df):
    d = df.loc[df.index != "baseline"]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = range(len(d))
    ax.bar(x, d["cell_agreement_pct"], color="tab:blue")
    ax.axhline(85, color="tab:red", ls="--", lw=1, label="85% robustness ref")
    for i, (_, r) in enumerate(d.iterrows()):
        ax.text(i, r["cell_agreement_pct"] + 0.5,
                f'{r["cell_agreement_pct"]:.0f}%\n{r["first_layer_same"]}',
                ha="center", fontsize=7)
    ax.set_xticks(list(x))
    ax.set_xticklabels(d.index, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("atlas cell agreement vs baseline (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Threshold sensitivity (label: agreement / first-layer matches)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(c.PLOT_DIR / "sensitivity.png", dpi=140)
    plt.close(fig)


if __name__ == "__main__":
    run()
