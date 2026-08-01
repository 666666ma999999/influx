# EV estimand v2 — v1→v2 比較表（生成物・判定不使用）

- 生成時刻: 2026-08-01T11:26:07+09:00
- 事前登録本文SHA256（算出前凍結）: `414947d5fabf79254d8998a879dbc33d5312cf99916a71b15acb7e83c584bc1e`
- watchlist 書込み前SHA256: `99dd4f405d0ab81d…`
- 算出: 16系統 / 未算出: 3系統（理由コード付き）
- 入力hash（returns.csv sha256先頭16桁）: volshock_5x: ca8afb92a8597f18; volshock_x_above200: 02157f99014fbaeb; shortcover_x_bear: d1dc18583d40218a; sue_beat: 0916e203900efb52; sell_reg_trigger_rebound: 525796ff9f0f9e84; sh_dip_reentry: 6ae5b5d46aed9ed8; turnover_rank_surge: 5210acd82527edd6; margin_expand_yoy: 3b62c77391392286; raw_strev_entry: c1906730ca6f34ca; gap_hold_close_strong: 3635d9d45c862486; engulf_reversal_day: 09f2d5e2661ee74c; three_up_ignition: d5340e6b61e21857; sales_beat: 73249023e4ef4e42; guidance_fy_strong: ef0dc5f63024c961; cfo_margin_improve: 0a2800e924cb7fbc; earnings_spillover: c382329dcfc1046a

v1=凍結点推定（プール平均）/ v2=月等ウェイト two-stage / ci1s=片側95%下限（正）。コスト規約: none=0.003控除・stop8=控除済み0。

| KPI | v1 EV(none) | v2 EV(none) | v2 片側95%下限(none) | v1 EV(stop8) | v2 EV(stop8) | v2 片側95%下限(stop8) | n | 月数 |
|---|---|---|---|---|---|---|---|---|
| volshock_5x | +0.57% | -0.03% | -2.06% | -0.75% | -0.88% | -2.40% | 292 | 71 |
| volshock_x_above200 | +1.12% | +1.00% | -1.44% | -0.33% | +0.10% | -1.85% | 180 | 61 |
| volshock_x_above200_quiet | — | 未算出 (no_returns_csv) | — | — | 未算出 | — | — | — |
| shortcover_x_bear | — | -0.28% | -2.10% | +0.04% | -0.02% | -1.22% | 1639 | 38 |
| pead_gap8_vol3 | — | 未算出 (no_returns_csv) | — | — | 未算出 | — | — | — |
| sue_x_above200 | — | 未算出 (no_returns_csv) | — | — | 未算出 | — | — | — |
| sue_beat | +1.46% | +0.95% | -0.82% | — | +1.03% | -0.37% | 818 | 64 |
| sell_reg_trigger_rebound | +0.26% | -0.86% | -2.42% | — | -1.06% | -1.92% | 2943 | 73 |
| sh_dip_reentry | -0.96% | -0.04% | -3.97% | — | -0.82% | -3.79% | 246 | 65 |
| turnover_rank_surge | -0.80% | -0.98% | -2.64% | — | -0.46% | -1.35% | 1237 | 73 |
| margin_expand_yoy | +0.42% | +1.65% | -0.56% | — | +1.13% | -0.44% | 2091 | 65 |
| raw_strev_entry | +0.86% | +0.83% | -1.03% | — | +0.39% | -1.12% | 1417 | 39 |
| gap_hold_close_strong | -0.32% | -0.95% | -2.45% | — | -0.59% | -1.45% | 1172 | 73 |
| engulf_reversal_day | +0.13% | -0.65% | -1.69% | — | -0.77% | -1.47% | 2021 | 73 |
| three_up_ignition | -0.45% | -0.51% | -1.53% | — | -0.30% | -1.04% | 3176 | 73 |
| sales_beat | +2.35% | +0.18% | -2.09% | +1.98% | +0.38% | -1.57% | 282 | 52 |
| guidance_fy_strong | +2.95% | +0.95% | -1.73% | +2.68% | +1.78% | -0.24% | 683 | 69 |
| cfo_margin_improve | +1.44% | +0.76% | -1.30% | +1.42% | +0.71% | -0.96% | 750 | 59 |
| earnings_spillover | +1.55% | +2.44% | +0.52% | +1.05% | +1.29% | -0.30% | 2460 | 53 |
