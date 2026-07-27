#!/usr/bin/env python3
"""tob_drift_v1 golden tests（凍結対象・12ケース）。実行: python3 tests/test_tob_drift_v1.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tob_drift_v1_stats as S  # noqa: E402

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok {PASS:2d}: {name}")


# 1) 全角ＭＢＯ → NFKC で qualify
check("全角ＭＢＯはqualify", S.classify_title("ＭＢＯの実施に関するお知らせ") == "qualify")
# 2) 表題内の改行・空白を除去してマッチ（公開\n買付）
check("改行を跨ぐ『公開買付』もTOB_ANY", S.classify_title("公開\n買付けの開始に関するお知らせ") == "qualify")
# 3) 自己株TOBは self
check("自己株式の公開買付はself", S.classify_title("自己株式の公開買付けの開始に関するお知らせ") == "self")
# 4) 撤回は withdraw（qualify語を含んでいても評価順で withdraw）
check("撤回はwithdraw優先", S.classify_title("公開買付けの開始の撤回に関するお知らせ") == "withdraw")
# 5) 結果=progress（開始語を含んでいても progress が先）
check("結果はprogress優先", S.classify_title("公開買付けの結果に関するお知らせ") == "progress")
# 6) 開始は qualify
check("開始はqualify", S.classify_title("○○社株式に対する公開買付けの開始に関するお知らせ") == "qualify")
# 7) 15:00:00 ちょうど → 翌営業日
bd = {"20260107": True, "20260108": True}
nb = lambda d: "20260108"
check("15:00:00ちょうどは翌営業日", S.signal_day("20260107", (15, 0, 0), lambda d: bd.get(d, False), nb) == "20260108")
# 8) 14:59:59 → 当日
check("14:59:59は当日", S.signal_day("20260107", (14, 59, 59), lambda d: bd.get(d, False), nb) == "20260107")
# 9) 暦日差90日ちょうど=同一deal / 91日=新deal
ds90 = [("20250101", "公開買付けの開始に関するお知らせ"), ("20250401", "公開買付けの開始に関するお知らせ")]  # 1/1+90日=4/1
check("90日ちょうどは同一deal", len(S.build_deals(ds90)) == 1)
ds91 = [("20250101", "公開買付けの開始に関するお知らせ"), ("20250402", "公開買付けの開始に関するお知らせ")]
check("91日は新deal", len(S.build_deals(ds91)) == 2)
# 10) 除外開示（progress）が窓を更新する: 0日目qualify→80日目progress→160日目qualify は1deal
dsw = [("20250101", "公開買付けの開始に関するお知らせ"),
       ("20250322", "公開買付けの経過に関するお知らせ"),      # +80日
       ("20250610", "公開買付けの開始に関するお知らせ")]      # progressから+80日
check("除外開示が窓を更新（1dealに束なる）", len(S.build_deals(dsw)) == 1)
# 11) dealの先頭が除外開示でも最先行qualifyがシグナル
dsx = [("20250101", "公開買付けの経過に関するお知らせ"), ("20250110", "公開買付けの開始に関するお知らせ")]
deals = S.build_deals(dsx)
check("先頭が除外でも最先行qualifyがシグナル", S.signal_index(dsx, deals[0]) == 1)
# 12) bootstrap の決定性（seed固定で同一値）＋判定AND
nbm = {"202601": [0.02, -0.01, 0.05], "202602": [0.01, 0.03], "202603": [-0.02, 0.04, 0.00, 0.02]}
lo1 = S.bootstrap_lower(nbm, n_boot=2000, seed=S.SEED)
lo2 = S.bootstrap_lower(nbm, n_boot=2000, seed=S.SEED)
check("bootstrapはseed固定で決定的", lo1 == lo2)
# 追加の整合: 約定状態表
assert S.fill_state(100.0, 5000) == "filled"
assert S.fill_state(100.0, 0) == "unfilled_no_volume"
assert S.fill_state(None, None) == "unfilled_no_bar"

print(f"ALL {PASS} golden tests PASSED")
