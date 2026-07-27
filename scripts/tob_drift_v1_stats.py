#!/usr/bin/env python3
"""tob_drift_v1 判定統計モジュール（凍結対象・変更禁止）。

`tasks/tob_deal_policy_preregister.md` §2/§5 の実装正本。凍結時に本ファイルの sha256 を仕様書へ記載し、
以後の変更＝仮説放棄。判定・分類・deal束ねはすべて本モジュールの関数のみを使う（Dual-Path禁止）。

bootstrap 統計量の定義（擬似コードの実装・一意）:
  入力: nets_by_month = {YYYYMM: [dealのnet, ...]}（成立dealのみ・空月キーは持たない）
  手順: m = len(months)。各反復で months から復元抽出を m 回行い、抽出された月の deal を
        重複込みで全て pool し、pool の deal加重平均（単純平均）を統計量とする。
        n_boot=10,000・seed=20260727（random.Random(seed)・月リストはキー昇順に固定してから抽出）。
  CI:   percentile法。片側95%下限 = 昇順で floor(0.05*n_boot) 番目（0-indexed）。
"""
from __future__ import annotations

import datetime as _dt
import random
import re
import unicodedata

N_BOOT = 10_000
SEED = 20260727
DEAL_GAP_DAYS = 90          # 暦日差 > 90（=91日以上）で新deal
LATE = (15, 0, 0)           # 15:00:00 ちょうどは「以降」扱い
COST = 0.003
TOP_K_EXCLUDE = 5

TOB_ANY = re.compile(r"公開買付|MBO|ＭＢＯ|マネジメント・バイアウト", re.IGNORECASE)
SELF = re.compile(r"自己株")
WITHDRAW = re.compile(r"撤回|中止|不成立|買付.*行わない|見送り")
PROGRESS = re.compile(r"結果|終了|状況|応募|訂正|変更|延長|経過")
QUALIFY = re.compile(r"開始|意見表明|賛同|実施|MBO|ＭＢＯ|マネジメント・バイアウト", re.IGNORECASE)


def normalize_title(title: str) -> str:
    """NFKC正規化 → 空白・改行(全種)を除去。表題欠損は空文字。"""
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    return "".join(ch for ch in s if not ch.isspace())


def classify_title(title: str) -> str:
    """評価順固定: TOB_ANY→SELF→WITHDRAW→PROGRESS→QUALIFY。
    返り値: not_tob / self / withdraw / progress / qualify / other_tob"""
    s = normalize_title(title)
    if not TOB_ANY.search(s):
        return "not_tob"
    if SELF.search(s):
        return "self"
    if WITHDRAW.search(s):
        return "withdraw"
    if PROGRESS.search(s):
        return "progress"
    if QUALIFY.search(s):
        return "qualify"
    return "other_tob"


def _to_date(d: str) -> _dt.date:
    return _dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))


def build_deals(disclosures: list[tuple[str, str]]) -> list[list[int]]:
    """同一銘柄の TOB_ANY 開示列 [(YYYYMMDD, title), ...]（時系列済み）を deal に分割する。
    窓の更新は TOB_ANY 全開示（self/withdraw/progress 含む）。暦日差 > DEAL_GAP_DAYS で新deal。
    返り値: dealごとの index リスト。"""
    deals: list[list[int]] = []
    cur: list[int] = []
    last: _dt.date | None = None
    for i, (d, title) in enumerate(disclosures):
        if classify_title(title) == "not_tob":
            continue
        dd = _to_date(d)
        if last is None or (dd - last).days > DEAL_GAP_DAYS:
            if cur:
                deals.append(cur)
            cur = []
        cur.append(i)
        last = dd
    if cur:
        deals.append(cur)
    return deals


def signal_index(disclosures: list[tuple[str, str]], deal: list[int]) -> int | None:
    """deal 内で時系列最先行の qualify 開示の index。無ければ None（エントリーしない）。"""
    for i in deal:
        if classify_title(disclosures[i][1]) == "qualify":
            return i
    return None


def signal_day(pub_date: str, pub_time: tuple[int, int, int], is_bday, next_bday) -> str:
    """シグナル日T。pub_time >= 15:00:00（ちょうど含む）または非営業日 → 翌営業日。
    is_bday: YYYYMMDD -> bool / next_bday: YYYYMMDD -> YYYYMMDD（翌営業日）。"""
    if pub_time >= LATE or not is_bday(pub_date):
        return next_bday(pub_date)
    return pub_date


def fill_state(adj_open, volume) -> str:
    """約定状態表（§4）。"""
    if adj_open is None or volume is None:
        return "unfilled_no_bar"
    if volume == 0:
        return "unfilled_no_volume"
    if adj_open <= 0:
        return "unfilled_bad_price"
    return "filled"


def net_return(adj_open: float, adj_close_exit: float) -> float:
    return adj_close_exit / adj_open - 1.0 - COST


def bootstrap_lower(nets_by_month: dict[str, list[float]],
                    n_boot: int = N_BOOT, seed: int = SEED) -> float:
    """月次ブロックbootstrapの片側95%下限（docstringの定義そのまま）。"""
    months = sorted(nets_by_month)
    m = len(months)
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        pool: list[float] = []
        for _ in range(m):
            pool.extend(nets_by_month[months[rng.randrange(m)]])
        stats.append(sum(pool) / len(pool))
    stats.sort()
    return stats[int(0.05 * n_boot)]


def top_k_excluded_mean(nets: list[float], k: int = TOP_K_EXCLUDE) -> float:
    rest = sorted(nets, reverse=True)[k:]
    return sum(rest) / len(rest)


def judge(nets_by_month: dict[str, list[float]]) -> dict:
    """最終判定（§5・唯一・AND条件）。"""
    nets = [x for v in nets_by_month.values() for x in v]
    lo = bootstrap_lower(nets_by_month)
    ex5 = top_k_excluded_mean(nets)
    return {
        "n": len(nets),
        "mean": sum(nets) / len(nets),
        "ci_lower_1s95": lo,
        "top5_excluded_mean": ex5,
        "pass": bool(lo > 0 and ex5 > 0),
    }
