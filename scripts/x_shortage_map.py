"""X品薄レーンの受益銘柄マップ: 読み込み・自己検証・発火時表示.

X の36クエリが品薄/値上がりを検知したとき「どの銘柄が受益か」を引くための対応表
(`configs/x_shortage_map.json`) のローダ。B2B価格レーン（price_universe_check.py の
`beneficiaries_display`）と同じ帰属規律を X レーンにも通すために切り出した共有部品。

設計の核（docs/price-watch-universe.md §0b 帰属プロトコルv2 の X 版）:
- **関門B**: 銘柄コードは TOP1000台帳 `data/center_pin/center_pin.jsonl` に実在するものだけ。
  台帳外の銘柄が発火表示に出ることは機械的に不可能にする。
- **関門A**: 符号を宣言する。sign=+ だけが買い候補。sign=- / sign=0 は「取り違え防止の記録」で
  表示しない（品薄で損する側・関係ありそうで動かない側を明示的に持つ）。
- **actionable ゲート**: 品薄は4+1種類あり、`resale`(定価固定=プレミアムが二次流通に落ちる) と
  `disruption`(供給断絶=復旧で終わる) と `broad`(主題不明) は **銘柄を出さない**。
  Xで最も目立つ品薄（転売プレ値）が株には最も効かない、という取り違えを構造で止める。
- tier: `confirmed` は **E1/E2 の証拠が1つ以上 かつ** center_pin の `pin` が当該生産連鎖を明示しているもののみ（docs §0b と同文・2026-08-31 P-INF-12 Q2 で一本化）。他は `provisional`(仮)。
  `verified` から12ヶ月超は STALE 表示（遅延再検証）。

検証（単体実行）:
    docker compose run --rm xstock python scripts/x_shortage_map.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent

MAP_PATH = APP / "configs/x_shortage_map.json"
CENTER_PIN_PATH = APP / "data/center_pin/center_pin.jsonl"
QUERY_CONFIG_PATH = APP / "configs/x_price_watch.json"

# 品薄は「供給が細って買えない」型と「需要が急増して売り切れる」型で受益者が真逆になる。
# demand 型だけが小売を受益側に置ける（値引き縮小＋数量増）。混ぜると逆方向に買う。
VALID_TYPES = {"price", "capex", "demand", "secondary", "resale", "disruption", "broad"}
RETAIL_OK_TYPES = {"demand"}      # 小売が sign=+ を名乗れる型
SECONDARY_OK_TYPES = {"secondary"}  # 二次流通が受益側に立てる型
# 「銘柄を出さない型」はコード側で固定する。JSON の shortage_types[..].actionable だけを
# 正としていると、設定1行を反転させるだけで転売プレ値が買い候補として出せてしまう
# （Codex CONFIRMED-3: ルールとデータを同一ファイルに置いたことによる自己認可）。
NON_ACTIONABLE_TYPES = {"resale", "disruption", "broad"}
VALID_LAYERS = {"maker", "parts", "equipment", "material", "trader", "secondary", "user", "retail"}
VALID_SIGNS = {"+", "-", "0"}
VALID_TIERS = {"confirmed", "provisional", "rejected"}
STALE_DAYS = 365

# 根拠が「連鎖」でなく「連想」だと自白しているカードは受益として使えない（関門C=1ホップ制限）。
# 2026-07-28 実害: ゲオHD(2681) が中古車の受益に入っていた。根拠欄には
# 「pin=中古品（古着・トレカ・ゲーム機）相場で中古車ではない。リユース相場全般の連想波及としてのみ」
# と書いてあったが、evidence が非空であることしか検査していなかったため通過した。
# 同社は実際には中古車事業を持たない（グループは衣料/時計/宝石/農機具/通信機器）。
# 「推測」は最初この一覧に入れていたが、日鉄鉱業(1515)・三菱マテリアル(5711) を誤検出した。
# 台帳の note の「推測」は〈事業は実在し pin にも載っているが感応度の大きさが未検証〉の意味で、
# それは tier=provisional が既に表している。落とすべきなのは「事業connectionそのものが無い」側だけ。
ASSOCIATION_WORDS = ("連想", "イメージ", "なんとなく", "であろう")
# 以下は「正当な但し書き」なので落とさない。ただし人間が定期的に読み直せるよう一覧表示する
# （例: 2ホップ=反応が遅い / メモリ専用ではない=専業ではないが受益はする / 推測=感応度が未実測）。
CAVEAT_WORDS = ("2ホップ", "ではない", "では無い", "弱い", "限定的", "明示していない", "未明示",
                "のみ", "推測", "憶測")


def load(path: Path = MAP_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_center_pin(path: Path = CENTER_PIN_PATH) -> dict[str, dict]:
    """TOP1000台帳を code -> row で返す（関門Bの照合台）。"""
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["code"]] = r
    return rows


def validate(m: dict | None = None, center_pin: dict | None = None,
             query_ids: set[str] | None = None) -> list[str]:
    """対応表の自己検証。errors のリストを返す（空＝健全）。

    「表示に出る銘柄が台帳外である」ことを起動時に不可能にするのが主目的。
    """
    m = m if m is not None else load()
    center_pin = center_pin if center_pin is not None else load_center_pin()
    if query_ids is None:
        query_ids = {e["id"] for e in json.loads(
            QUERY_CONFIG_PATH.read_text(encoding="utf-8"))["queries"]}

    errors: list[str] = []
    seen_subject_ids: set[str] = set()
    covered: set[str] = set()
    # query_id -> code -> 出現した sign の集合（同一クエリ内の符号矛盾を検出するため）
    query_signs: dict[str, dict[str, set[str]]] = {}

    for stype_name in NON_ACTIONABLE_TYPES:
        decl = m.get("shortage_types", {}).get(stype_name, {})
        if decl.get("actionable"):
            errors.append(f"shortage_types.{stype_name}.actionable=true は不正"
                          f"（この型は銘柄を出さないとコード側で固定されている）")

    for s in m["subjects"]:
        sid = s.get("id", "(id無し)")
        if sid in seen_subject_ids:
            errors.append(f"{sid}: subject id が重複")
        seen_subject_ids.add(sid)

        stype = s.get("shortage_type")
        if stype not in VALID_TYPES:
            errors.append(f"{sid}: 未知の shortage_type={stype}")
        else:
            # 型の actionable は上限。型が対象でも TOP1000内に受益銘柄が居なければ非対象に落ちる
            # （lumber/kome のように「品薄だが日本の大型株に純受益が居ない」主題が実在するため）。
            type_ok = m["shortage_types"][stype]["actionable"]
            expected = bool(type_ok and s.get("beneficiaries"))
            if s.get("actionable") != expected:
                errors.append(f"{sid}: actionable={s.get('actionable')} が不整合"
                              f"（type={stype} の上限={type_ok}・受益カード"
                              f"{len(s.get('beneficiaries', []))}件 → 期待={expected}）")

        if not s.get("reason"):
            errors.append(f"{sid}: reason が空（なぜ対象/非対象かを必ず書く）")

        for q in s.get("queries", []):
            if q not in query_ids:
                errors.append(f"{sid}: 存在しない query_id={q}")
            covered.add(q)

        if not s.get("actionable") and s.get("beneficiaries"):
            errors.append(f"{sid}: actionable=false なのに beneficiaries が空でない"
                          f"（休眠させるなら dormant_beneficiaries へ）")

        for kind in ("beneficiaries", "traps", "dormant_beneficiaries"):
            for b in s.get(kind, []):
                code = b.get("code")
                row = center_pin.get(code)
                if row is None:
                    errors.append(f"{sid}/{kind}: code={code} が TOP1000台帳に不在（関門B違反）")
                elif b.get("name") and b["name"] != row["name"]:
                    errors.append(f"{sid}/{kind}: code={code} の name 不一致 "
                                  f"（表={b['name']} / 台帳={row['name']}）")
                if b.get("layer") not in VALID_LAYERS:
                    errors.append(f"{sid}/{kind}: 未知の layer={b.get('layer')} (code={code})")
                if b.get("sign") not in VALID_SIGNS:
                    errors.append(f"{sid}/{kind}: 未知の sign={b.get('sign')} (code={code})")
                # 正本(center_pin)との tier 整合（2026-08-30: 4385/8035/3436 の tier ズレが表側に残っていた実測）。
                # (a) price/secondary 型の subject で confirmed なのに台帳側が watch=none（数量型/不成立）→NG。
                #     capex/demand 型は「品薄→増産投資→数量で儲かる」設計なので受益者が volume 型でも正常＝免除。
                # (b) 台帳 note が「不成立/却下」なのに rejected でない→NG（全型）。
                if row is not None and kind == "beneficiaries":
                    name = b.get("name") or row.get("name", "")
                    if (b.get("tier") == "confirmed" and row.get("watch") == "none"
                            and stype in ("price", "secondary")):
                        errors.append(f"{code} {name}: tier=confirmed だが center_pin watch=none"
                                      f"（数量型/不成立）＝正本裁定の未反映か")
                    if b.get("tier") != "rejected" and any(
                            w in row.get("note", "") for w in ("不成立", "却下")):
                        errors.append(f"{code} {name}: center_pin note に不成立/却下があるのに "
                                      f"tier={b.get('tier')}（rejected 以外）＝正本裁定の未反映か")
                if not b.get("evidence"):
                    errors.append(f"{sid}/{kind}: code={code} に evidence が無い")
                elif kind == "beneficiaries" and s.get("actionable"):
                    hit = [w for w in ASSOCIATION_WORDS if w in b["evidence"]]
                    if hit:
                        errors.append(f"{sid}: code={code} の根拠が連鎖でなく連想 {hit}"
                                      f"（関門C違反。連想で銘柄を出してはいけない）")
                # 表示時に datetime.strptime へ渡る値。ここで弾かないと「検証は通るのに
                # 発火した瞬間に落ちる」＝一番まずいタイミングで壊れる（Codex PLAUSIBLE-5）
                if b.get("verified"):
                    try:
                        datetime.strptime(b["verified"], "%Y-%m-%d")
                    except ValueError:
                        errors.append(f"{sid}/{kind}: code={code} の verified="
                                      f"{b['verified']!r} が YYYY-MM-DD として不正")
                if kind == "beneficiaries" and s.get("actionable"):
                    for q in s.get("queries", []):
                        query_signs.setdefault(q, {}).setdefault(code, set()).add(b.get("sign"))
                elif kind == "traps":
                    for q in s.get("queries", []):
                        query_signs.setdefault(q, {}).setdefault(code, set()).add(b.get("sign"))
                if kind != "traps":
                    if b.get("tier") not in VALID_TIERS:
                        errors.append(f"{sid}/{kind}: 未知の tier={b.get('tier')} (code={code})")
                    if b.get("sign") == "+" and kind == "beneficiaries":
                        # 符号の取り違え防止: 買う側は常に不可。売る店は需要急増型のみ可
                        # （供給制約の品薄では仕入れられず減益になる）。
                        if b.get("layer") == "user":
                            errors.append(f"{sid}: code={code} は layer=user なのに sign=+ "
                                          f"（買って使う側は品薄で受益しない）")
                        elif b.get("layer") == "retail" and stype not in RETAIL_OK_TYPES:
                            errors.append(f"{sid}: code={code} は layer=retail・type={stype} なのに "
                                          f"sign=+（小売が受益側に立てるのは需要急増型のみ）")
                        elif b.get("layer") == "secondary" and stype not in SECONDARY_OK_TYPES:
                            errors.append(f"{sid}: code={code} は layer=secondary・type={stype} なのに "
                                          f"sign=+（二次流通が受益側に立てるのは secondary 型のみ）")
                elif b.get("sign") == "+":
                    errors.append(f"{sid}/traps: code={code} の sign=+ は traps に置けない")

    # 同一 query_id 内で、ある銘柄が受益(+)と損失(-/0)の両方に現れるのは判定不能を意味する。
    # 発火してもどちらの機序か区別できないため、買い候補として出してはいけない
    # （Codex CONFIRMED-1: 新車納期遅延と中古車相場高が同一クエリに同居していた）。
    for q, codes in query_signs.items():
        for code, signs in codes.items():
            if "+" in signs and len(signs) > 1:
                errors.append(f"query={q}: code={code} が同一クエリ内で sign={sorted(signs)} と矛盾"
                              f"（受益と損失の両方に帰属＝発火時に機序を分離できない）")

    missing = query_ids - covered
    if missing:
        errors.append(f"どの subject にも割り当てられていない query_id: {sorted(missing)}")
    return errors


def subjects_for_query(m: dict, query_id: str) -> list[dict]:
    """1つの query_id に紐づく subject（複数可）。

    多重割当は意図的: 例「ポケカ 高騰」は toreka(非対象=メーカーに届かない)と
    secondary-resale(対象=中古流通が受益)の両方に効く。
    """
    return [s for s in m["subjects"] if query_id in s.get("queries", [])]


def ensure_validated(m: dict) -> None:
    """未検証の対応表で表示経路を動かせないようにする（自己防衛）。

    呼び出し側が `validate()` を通し忘れても、台帳外コードや符号違反が画面に出ることを
    防ぐ。検証済みフラグを対応表に刻んで2回目以降は素通しする
    （Codex CONFIRMED-2: 公開関数が「検証済みmap」という前提を強制していなかった）。
    """
    if m.get("_validated"):
        return
    errors = validate(m)
    if errors:
        raise ValueError(f"未検証・不正な対応表では銘柄を出せません（{len(errors)}件）: {errors[0]}")
    m["_validated"] = True


def cards_for_query(m: dict, query_id: str) -> list[dict]:
    """発火時に買い候補として出せるカードだけを返す。

    actionable な subject の sign=+ かつ confirmed/provisional のみ。
    同一銘柄が複数 subject に出た場合は、より強い tier を残して1件に畳む。
    """
    ensure_validated(m)
    best: dict[str, dict] = {}
    for s in subjects_for_query(m, query_id):
        if not s.get("actionable"):
            continue
        for b in s.get("beneficiaries", []):
            if b.get("sign") != "+" or b.get("tier") not in ("confirmed", "provisional"):
                continue
            cur = best.get(b["code"])
            if cur is None or (cur["tier"] != "confirmed" and b["tier"] == "confirmed"):
                best[b["code"]] = {**b, "subject": s["id"], "shortage_type": s["shortage_type"]}
    return sorted(best.values(), key=lambda b: (b["tier"] != "confirmed", b["code"]))


def display_for_query(m: dict, query_id: str, today: str) -> str:
    """発火時の1行表示。買い候補が無い場合は「なぜ無いか」を返す。

    「銘柄が出ない」を沈黙ではなく理由つきで出すのが要点（Xで一番目立つ品薄＝転売プレ値は
    メーカー収益に届かないため、空であること自体が正しい成果物）。
    """
    ensure_validated(m)
    datetime.strptime(today, "%Y-%m-%d")  # 不正日付は発火時でなくここで即座に落とす
    subs = subjects_for_query(m, query_id)
    if not subs:
        return "対応表に未登録（銘柄なし）"
    cards = cards_for_query(m, query_id)
    if not cards:
        types = "/".join(sorted({s["shortage_type"] for s in subs}))
        reason = subs[0].get("reason", "")
        return f"銘柄なし[{types}] {reason[:60]}"
    t = datetime.strptime(today, "%Y-%m-%d")
    parts = []
    for b in cards:
        tag = "" if b["tier"] == "confirmed" else "(仮)"
        stale = "(STALE要再確認)"
        if b.get("verified"):
            age = (t - datetime.strptime(b["verified"], "%Y-%m-%d")).days
            if age <= STALE_DAYS:
                stale = ""
        parts.append(f"{b['code']}{b.get('name', '')}{tag}{stale}")
    return "/".join(parts)


def main() -> int:
    m = load()
    errors = validate(m)
    n_sub = len(m["subjects"])
    n_act = sum(1 for s in m["subjects"] if s["actionable"])
    n_ben = sum(len(s.get("beneficiaries", [])) for s in m["subjects"])
    n_trap = sum(len(s.get("traps", [])) for s in m["subjects"])
    print(f"=== x_shortage_map v{m['version']} ({m['generated']}) ===")
    print(f"subject {n_sub}（対象 {n_act} / 非対象 {n_sub - n_act}）"
          f"・受益カード {n_ben}・罠 {n_trap}")
    for s in m["subjects"]:
        mark = "◎" if s["actionable"] else "×"
        print(f"  {mark} {s['id']:24s} {s['shortage_type']:11s} "
              f"受益{len(s.get('beneficiaries', [])):3d} 罠{len(s.get('traps', [])):3d} "
              f"{s['label']}")
    # 落とさないが人間が読み直すべきカード（但し書き付き）。放置すると
    # 「弱い根拠のまま確証カードに昇格していた」に気づけない
    caveats = [(s["id"], b["code"], b.get("name", ""), b["tier"],
                [w for w in CAVEAT_WORDS if w in b.get("evidence", "")])
               for s in m["subjects"] if s.get("actionable")
               for b in s.get("beneficiaries", [])
               if any(w in b.get("evidence", "") for w in CAVEAT_WORDS)]
    if caveats:
        print(f"\n[要再読 {len(caveats)}枚] 根拠に但し書きがあるカード（エラーではない）")
        for sid, code, name, tier, ws in caveats:
            print(f"  {sid:20s} {code} {name} ({tier}) {ws}")

    if errors:
        print(f"\nNG: 検証エラー {len(errors)} 件")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nOK: 全カードが TOP1000台帳に実在・符号/層/tier/網羅すべて整合")
    return 0


if __name__ == "__main__":
    sys.exit(main())
