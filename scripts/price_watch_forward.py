"""price_watch_forward: 値上がり発火の前向き記録と評価.

2026-07-28 の2モデル敵対レビュー C-1（最重要指摘）への対応。それまで発火は print される
だけで記録が残らず、「いつ何が鳴ったか」すら復元できないため、このシステムの土台仮説
（実物価格の変曲 → 受益株の上昇が8〜15週遅行・現在 n=2）を将来検定する手段が
構造的に存在しなかった。

## 凍結する事前宣言（v1・2026-07-28。変更は version を上げて別系列として扱う）
- entry      : 発火日時点で入手できる直近営業日の調整後終値（AdjC）＝ look-ahead なし
- windows    : 40営業日（≒8週）と 75営業日（≒15週）
- metric     : 銘柄リターン − TOPIXリターン（＝超過リターン。市場全体の上下を除く）
- hit        : 超過リターン > 0
- 対象       : configs/price_universe_sources.json の受益銘柄（発火系列のもの全件・選別なし）
- 除外       : 「(受益なし…)」表記の系列は銘柄ゼロで記録のみ（後知恵の銘柄選びを禁止）

## 位置づけ
これは**観察記録**であり、正式レシピのα台帳（data/kpi_trials/trials.jsonl）には登録しない。
監視トリガーを正式レシピと同列に数えると多重比較の粒度が壊れるため（レビュアー懸念への配慮）。

## 使い方
    # 記録は price_universe_check.py から自動（発火時のみ）
    docker compose run --rm xstock python scripts/price_watch_forward.py --eval    # 期日到来分を評価
    docker compose run --rm xstock python scripts/price_watch_forward.py --status  # 蓄積状況
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

import jq_fetch  # noqa: E402  Canonical データローダ
import measure_base_rate as mbr  # noqa: E402  Canonical カレンダー/bars

LOG_PATH = APP / "data/price_watch/forward_log.jsonl"
# v2=帰属プロトコル受益カード（2026-07-28）。v1発火とは別系列として集計する
# v3=トリガー種別の全列挙（週次/4週累積/前月比/前年同月比）＋月次レーンのエピソード規則
#    （2026-08-30 オーナー裁定・敵対レビュー wf_ada84c33-b50）。評価窓と指標は v2 と同じ
SPEC_VERSION = 3
WINDOWS_BD = {"w8": 40, "w15": 75}  # 営業日（≒8週 / ≒15週）
# エピソード重複排除（月次レーン・v3）: 同じ (series_id, code) を直近 N ヶ月以内に記録済みなら
# 再記録しない。前年同月比は1段の値上がりが12ヶ月閾値上に留まるため、checker 側の
# 「新規跨ぎ」規則をすり抜けた再発火（履歴欠落・閾値変更等）でも同じ観測を月ごとに
# 重複記録しないための第2の関所。6 = 評価窓 w15（75営業日≒3.5ヶ月）を跨いで十分な余裕
EPISODE_DEDUP_MONTHS = 6
HYPOTHESIS_V3 = (
    "商品価格の発火から、受益銘柄が8〜15週で TOPIX を超過する。発火の種別は次の4つ: "
    "①週次 weekly>=+5% ②4週累積>=+10%（いずれも configs/price_universe_sources.json の alert 既定・"
    "系列別 alert で上書き可） ③前月比>=閾値（月次系列・系列別 alert.monthly_pct） "
    "④前年同月比>=閾値（月次系列・系列別 alert.yoy_pct を明示した系列のみ）。"
    "③④はエピソード規則: 閾値を新たに跨いだ公表月だけ発火し、直前の公表月が既に閾値以上なら"
    "鳴らさない（閾値未満へ落ちて再び跨いだ時に次のエピソード）。同じ公表月では再発火しない。"
    f"加えて同じ (系列, 銘柄) は直近{EPISODE_DEDUP_MONTHS}ヶ月以内に記録済みなら再記録しない"
    "（skipped_dup_reason=episode）"
)
CODE_RE = re.compile(r"(?<![0-9A-Za-z])([0-9]{4}|[0-9]{3}[A-Z])(?![0-9A-Za-z])")


def to_code5(code4: str) -> str:
    return code4.upper() + "0"


def load_topix() -> dict[str, float]:
    obj = jq_fetch.read_json_gz(jq_fetch.DATA_ROOT / "topix.json.gz")
    return {r["Date"].replace("-", ""): float(r["C"]) for r in obj["data"] if r.get("C")}


def business_days() -> list[str]:
    return mbr.all_business_days(mbr.load_calendar_days())


def latest_bar_day(bdays: list[str], on_or_before: str) -> str | None:
    """指定日以前で bars が実在する直近営業日（look-ahead 防止のため未来は見ない）。"""
    idx = bisect.bisect_right(bdays, on_or_before) - 1
    for i in range(idx, max(idx - 10, -1), -1):
        if (jq_fetch.DATA_ROOT / "bars" / f"{bdays[i]}.json.gz").exists():
            return bdays[i]
    return None


def close_of(code5: str, day: str) -> float | None:
    try:
        rec = mbr.load_bars_day(day).get(code5)
    except SystemExit:
        return None
    if not rec or not rec.get("AdjC"):
        return None
    return float(rec["AdjC"])


def _months_before(day: str, months: int) -> str:
    """day（YYYY-MM-DD）の months ヶ月前の同日（無い日は月末に丸める）を YYYY-MM-DD で返す。"""
    y, m, d = (int(x) for x in day.split("-"))
    total = y * 12 + (m - 1) - months
    y2, m2 = divmod(total, 12)
    m2 += 1
    last = [31, 29 if (y2 % 4 == 0 and (y2 % 100 != 0 or y2 % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m2 - 1]
    return f"{y2:04d}-{m2:02d}-{min(d, last):02d}"


def append(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def ensure_preregistration() -> None:
    """事前宣言を1回だけ書き込む（凍結条件の証跡。以後の変更は version を上げる）。"""
    if any(e.get("type") == "preregistration" and e.get("spec_version") == SPEC_VERSION
           for e in read_log()):
        return
    append({
        "type": "preregistration", "spec_version": SPEC_VERSION,
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hypothesis": HYPOTHESIS_V3,
        "episode_dedup_months": EPISODE_DEDUP_MONTHS,
        "entry_rule": "発火日時点の直近営業日 AdjC（look-aheadなし）",
        "windows_bd": WINDOWS_BD,
        "metric": "銘柄リターン - TOPIXリターン（超過リターン）",
        "hit_rule": "超過リターン > 0",
        "universe_rule": "configs/price_universe_sources.json の当該系列の受益カードのうち "
                         "sign=+ かつ tier in (confirmed, provisional) の全件（後知恵の選別禁止。"
                         "rejected は帰属プロトコルv2の事前棄却＝docs/price-watch-universe.md §0b）",
        "note": "観察記録。trials.jsonl（正式αレーン）には登録しない",
    })
    print(f"[forward] 事前宣言 v{SPEC_VERSION} を記録")


def _pre20_excess(code5: str, px_now: float, bdays: list[str], base_idx: int,
                  topix: dict[str, float]) -> float | None:
    """基準日の20営業日前からの対TOPIX超過（pt）。bars/TOPIX が無ければ None（記録は落とさない）。"""
    if base_idx < 20:
        return None
    day0 = bdays[base_idx - 20]
    px0 = close_of(code5, day0)
    t0, t1 = topix.get(day0), topix.get(bdays[base_idx])
    if not px0 or not t0 or not t1:
        return None
    return round((px_now / px0 - 1) * 100 - (t1 / t0 - 1) * 100, 2)


def record_firings(alerts: list, fire_date: str, lane: str = "rise") -> int:
    """price_universe_check の発火を前向き記録する。alerts=[(series_cfg, row, triggers)]

    lane: "rise"=本発火（既定・n>=100 の対TOPIX検定の分母）／"early"=予兆レーン（2026-10-03 裁定 a・
    低い閾値の早期通知）。レーンごとに別の分母で数え、重複排除（同日・7日・6ヶ月）も同じレーン内でだけ効かせる
    ＝予兆が立った日に本発火へ昇格しても本発火側は通常どおり記録される。
    """
    if not alerts:
        return 0
    ensure_preregistration()
    bdays = business_days()
    base_day = latest_bar_day(bdays, fire_date.replace("-", ""))
    if base_day is None:
        print("[forward] WARN: 基準日の bars が無いため記録をスキップ")
        return 0
    base_idx = bdays.index(base_day)
    eval_days = {k: bdays[base_idx + n] if base_idx + n < len(bdays) else None
                 for k, n in WINDOWS_BD.items()}
    topix = load_topix()

    # 同一日に同じ銘柄を複数系列で二重記録しない（P0-①・2026-07-28）。
    # 例: wti と brent は別系列だが受益銘柄は同じ 1605/1662 で、同日に両方鳴ると
    # 1銘柄が2観測として数えられ、目標 n>=100 の分母と勝率が水増しされる。
    # 先に鳴った系列に帰属させ、後続系列では skipped_dup に落として理由を残す。
    log_firings = [e for e in read_log() if e.get("type") == "firing"
                   and (e.get("lane") or "rise") == lane]
    seen_codes = {s["code"] for e in log_firings if e.get("fire_date") == fire_date
                  for s in e.get("stocks", [])}
    # エピソード重複排除（v3・月次レーンのみ）: 同じ (series_id, code) を直近
    # EPISODE_DEDUP_MONTHS ヶ月以内に記録済みなら skipped_dup（reason=episode）に落とす。
    # 週次レーンの挙動は変えない（オーナー裁定 2026-08-30 は月次レーンが対象）。
    episode_cutoff = _months_before(fire_date, EPISODE_DEDUP_MONTHS)
    recent_pairs = {(e.get("series_id"), s["code"]) for e in log_firings
                    if episode_cutoff <= (e.get("fire_date") or "") < fire_date
                    for s in e.get("stocks", [])}
    # 7日未満の再記録排除（2026-10-03・price-universe を週次→平日日次にした副作用への手当て）:
    # 同じ (series_id, code) を直近7日未満（fire_date-6 〜 fire_date-1）に記録済みなら
    # skipped_dup（reason=repeat_7d）。週次便は実行間隔がちょうど7日なので挙動は不変
    # （2026-08-30 裁定「週次レーンは変えない」と両立）。月次レーンの6ヶ月エピソード排除とは独立。
    # 銘柄が1つも残らない再発火（受益カード無し系列・全銘柄 repeat_7d）は行自体を書かない＝
    # 4週条件が続く間、毎日同じ空行が積まれて前向き台帳が読めなくなるのを防ぐ。
    cutoff7 = (datetime.strptime(fire_date, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    recent7 = [e for e in log_firings if cutoff7 <= (e.get("fire_date") or "") < fire_date]
    recent7_pairs = {(e.get("series_id"), s["code"]) for e in recent7 for s in e.get("stocks", [])}
    recent7_series = {e.get("series_id") for e in recent7}

    n = 0
    for series, row, triggers in alerts:
        # 帰属プロトコルv2: 受益カードの sign=+ かつ confirmed/provisional を全件記録
        # （仮=provisional も記録に入れる裁定 2026-07-28。rejected は買いシグナル禁止で除外）。
        # 旧形式（stocks 自由文字列）の config にも後方互換
        if "beneficiaries" in series:
            # §16w（P-08c 裁定 2026-08-17）: 海外上場カードは**この台帳に入れない**。
            # 本台帳は対TOPIX超過・日本の営業日で事前登録された検定（n>=100）であり、
            # 基準指数も営業日も違う海外株を同じ分母に入れると検定が壊れる。
            # 黙って落とすと「カードを作ったのに何も起きない」になるため理由を残す（fail-closed）。
            positives = [b for b in series["beneficiaries"]
                         if b.get("sign") == "+" and b.get("tier") in ("confirmed", "provisional")]
            skipped_foreign = [str(b.get("ticker") or b.get("code"))
                               for b in positives if (b.get("market") or "JP") != "JP"]
            code_tiers = [(to_code5(b["code"]), b.get("tier", "confirmed"))
                          for b in positives if (b.get("market") or "JP") == "JP"]
        else:
            skipped_foreign = []
            code_tiers = [(to_code5(c), "confirmed")
                          for c in CODE_RE.findall(series.get("stocks", ""))]
        stocks, skipped_dup, skipped_reason = [], [], {}
        monthly = series.get("cadence") == "monthly"
        for c5, tier in code_tiers:
            if c5 in seen_codes:
                skipped_dup.append(c5)
                skipped_reason[c5] = "same_day"
                continue
            if (series["id"], c5) in recent7_pairs:
                skipped_dup.append(c5)
                skipped_reason[c5] = "repeat_7d"
                continue
            if monthly and (series["id"], c5) in recent_pairs:
                skipped_dup.append(c5)
                skipped_reason[c5] = "episode"
                continue
            px = close_of(c5, base_day)
            if px is not None:
                # 直前20営業日の対TOPIX超過（2026-10-03 オーナー提案「予兆が出た時にその株が前に上がって
                # いたかをチェック」）。遡り採点（§16o-3 追記8）では「上がり済み」の方が後20の勝率が高く、
                # 除外フィルタには使わない＝**記録と表示のみ**。将来の採点のため全レーンで残す
                stocks.append({"code": c5, "entry_close": px, "tier": tier,
                               "pre20_excess_pt": _pre20_excess(c5, px, bdays, base_idx, topix)})
                seen_codes.add(c5)
        if not stocks and series["id"] in recent7_series:
            print(f"[forward] 7日未満の再発火で銘柄なし → 記録しない: {series['jp']}（{'/'.join(triggers)}）")
            continue
        append({
            "type": "firing", "spec_version": SPEC_VERSION, "lane": lane,
            "fire_date": fire_date, "series_id": series["id"], "series_jp": series["jp"],
            "driver": series.get("driver", series["id"]),
            "triggers": triggers, "skipped_dup": skipped_dup,
            "skipped_dup_reason": skipped_reason,  # v3: same_day（同日他系列）/ episode（6ヶ月内既記録）
            "skipped_foreign": skipped_foreign,  # §16w: 別レーンで評価する海外カード
            "commodity": {"value": row.get("value"), "weekly_pct": row.get("weekly_pct"),
                          "four_week_pct": row.get("four_week_pct")},
            "base_day": base_day, "topix_entry": topix.get(base_day),
            "eval_days": eval_days, "stocks": stocks,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        n += 1
        dup_txt = f" 二重排除{len(skipped_dup)}件" if skipped_dup else ""
        pre_txt = " 直前20営業日: " + " ".join(
            f"{x['code'][:4]}{x['pre20_excess_pt']:+.1f}pt" for x in stocks
            if x.get("pre20_excess_pt") is not None) if stocks else ""
        print(f"[forward] 記録: {series['jp']}（{'/'.join(triggers)}）銘柄{len(stocks)}件{dup_txt} "
              f"基準={base_day} 評価予定={eval_days['w8']}/{eval_days['w15']}{pre_txt}")
    return n


def evaluate() -> int:
    """期日が到来した firing を評価して evaluation を append する（冪等）。"""
    log = read_log()
    firings = [e for e in log if e.get("type") == "firing"]
    done = {(e.get("spec_version"), e.get("lane") or "rise", e["fire_date"], e["series_id"], e["window"])
            for e in log if e.get("type") == "evaluation"}
    if not firings:
        print("[forward] 発火記録がまだありません（発火時に自動で記録されます）")
        return 0
    topix = load_topix()
    today = datetime.now().strftime("%Y%m%d")
    n_new = 0
    for f in firings:
        for win, eval_day in (f.get("eval_days") or {}).items():
            if not eval_day or eval_day > today:
                continue
            if (f.get("spec_version"), f.get("lane") or "rise", f["fire_date"], f["series_id"], win) in done:
                continue
            tpx_now = topix.get(eval_day)
            tpx_ent = f.get("topix_entry")
            if not tpx_now or not tpx_ent:
                continue
            tpx_ret = (tpx_now / tpx_ent - 1) * 100
            results = []
            for s in f.get("stocks", []):
                px = close_of(s["code"], eval_day)
                if px is None:
                    results.append({"code": s["code"], "status": "no_data"})
                    continue
                ret = (px / s["entry_close"] - 1) * 100
                results.append({"code": s["code"], "ret_pct": round(ret, 2),
                                "excess_pct": round(ret - tpx_ret, 2),
                                "hit": bool(ret - tpx_ret > 0), "status": "ok"})
            scored = [r for r in results if r["status"] == "ok"]
            append({
                # 評価行は**発火行の spec_version を引き継ぐ**（done 判定のキーと一致させる。
                # 現行値で書くと v2 発火の評価が毎回「未評価」扱いになり無限に追記される）
                "type": "evaluation", "spec_version": f.get("spec_version", SPEC_VERSION),
                "lane": f.get("lane") or "rise",
                "fire_date": f["fire_date"], "series_id": f["series_id"], "window": win,
                "eval_day": eval_day, "topix_ret_pct": round(tpx_ret, 2),
                "results": results,
                "n_hit": sum(1 for r in scored if r["hit"]), "n_scored": len(scored),
                "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            n_new += 1
            hits = sum(1 for r in scored if r["hit"])
            lane_tag = "予兆 " if (f.get("lane") or "rise") == "early" else ""
            print(f"[eval] {lane_tag}{f['series_jp']} {win}: {hits}/{len(scored)} hit "
                  f"(TOPIX {tpx_ret:+.1f}%)")
    print(f"[forward] 新規評価 {n_new} 件")
    return n_new


def status() -> None:
    log = read_log()
    for lane in ("rise", "early"):
        _status_lane(log, lane)


def _status_lane(log: list, lane: str) -> None:
    firings = [e for e in log if e.get("type") == "firing" and (e.get("lane") or "rise") == lane]
    evals = [e for e in log if e.get("type") == "evaluation" and (e.get("lane") or "rise") == lane]
    if lane == "early" and not firings:
        return
    label = "予兆レーン（early・別分母）" if lane == "early" else "本発火"
    print(f"=== 前向き記録の蓄積状況（spec v{SPEC_VERSION}・{label}）===")
    print(f"発火記録: {len(firings)} 件 / 評価済み: {len(evals)} 件")
    if firings:
        print(f"期間: {min(f['fire_date'] for f in firings)} 〜 {max(f['fire_date'] for f in firings)}")
        n_stocks = sum(len(f.get("stocks", [])) for f in firings)
        print(f"のべ銘柄観測: {n_stocks} 件（判定に必要な目安 n>=100）")
    if evals:
        hit = sum(e["n_hit"] for e in evals)
        tot = sum(e["n_scored"] for e in evals)
        rate = f"{hit / tot:.1%}" if tot else "-"
        print(f"超過リターン勝率（暫定・検定前）: {hit}/{tot} = {rate}")
        print("※ 勝率は参考値。事前宣言どおり n>=100 かつ独立エピソード複数まで判定しない")
    pending = [f for f in firings
               if not any(e["fire_date"] == f["fire_date"] and e["series_id"] == f["series_id"]
                          for e in evals)]
    if pending:
        print(f"評価待ち: {len(pending)} 件（直近の評価予定日: "
              f"{min(f['eval_days']['w8'] for f in pending if f.get('eval_days', {}).get('w8'))}）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", action="store_true", help="期日到来分を評価")
    parser.add_argument("--status", action="store_true", help="蓄積状況を表示")
    args = parser.parse_args()
    if args.eval:
        evaluate()
    status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
