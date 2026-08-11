#!/usr/bin/env python3
"""@twittora_ 向けバズ投稿収集スクリプト (Grok x_search 経由)。

Claude Code / AI 活用ジャンルで過去 N 日の高エンゲージメント X 投稿を取得し、
Vault material-bank 互換の jsonl + md 形式で保存する。

実行 (influx Docker):
    docker compose run --rm xstock python scripts/grok_collect_twittora.py
    docker compose run --rm xstock python scripts/grok_collect_twittora.py --days 14 --min-likes 100

出力:
    /app/output/grok_twittora/grok-twittora-YYYY-MM-DD.jsonl
    /app/output/grok_twittora/grok-twittora-YYYY-MM-DD.md
ホスト側 mount: influx/output/grok_twittora/

その後 host から ~/Documents/Obsidian Vault/.raw/ に転記する (skill 側で実行)。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import x_search


JST = timezone(timedelta(hours=9))

DEFAULT_QUERIES = [
    "Claude Code 使い方",
    "Claude Code tips",
    "MCP server 自作",
    "Claude hooks 自動化",
    "AI コーディング ワークフロー",
    "Claude Sonnet 活用",
    "Cursor vs Claude",
    "Anthropic Claude API",
    "AI エージェント 開発",
    "subagent 並列",
    # gen2追加 2026-07-26（本人実ブックマーク182件クラスタリング由来・x_keywords_result_gen2.md）
    "Fable オーケストレーション",  # 3語→2語緩和 2026-07-26 実走0件のため（元: "Fable ループ オーケストレーション"）。c-a8503f59: Fable/Claude Codeのオーケストレーション・トークン節約運用（既存10本は"Fable"を含まない新シグナル）
    "Claude Code 新機能 アップデート",  # c-119763bf: リリース速報・アップデート系（既存は使い方/tipsのみで新機能速報を拾えていない）
    "Claude Skills Routines",  # c-119763bf: Skills/Routinesという新公式用語（既存のMCP検索と重複しない）
    # "Obsidian 第二の脳 Claude",  # 🪦退役 2026-08-09（敵対レビューA実測: 2週稼働で芯命中0件・「第二の脳」は最汚染語=商材46%が単一起点の再梱包。経緯= make_article docs/x-operation/research/obsidian-mgmt-search-adv-review-2026-08-09.md）
    "Obsidian Git 連携",  # 2026-08-09 追加: 芯②repo×vault使い分け。固有識別子Gitで商材語彙と重ならない（退役1+追加2=純増1・在庫制）
    "CLAUDE.md Obsidian",  # 2026-08-09 追加: 芯③AI連携の配線。弁別語で唯一2.4%の母集団を持つ（同上）
    "NotebookLM Codex 文字起こし",  # c-49c1862f: Claude以外のAIツール（非エンジニア向け画像/動画/文字起こし）
    "Opus Fable Codex 使い分け",  # proposals: モデル使い分け・メタプロンプト（既存"Cursor vs Claude"はIDE比較でモデル比較とは別軸）
    # gen3追加 2026-08-03（束台帳の困りごと定義から逆生成）。
    # 同日改訂（敵対レビューA指摘）: 初版は6本中5本が5語ANDで、実行経路（x.com/search 生AND
    # + min_faves + 7日窓 + f=top）では0件級に落ちる公算大＝「束由来は効かない」の誤結論リスク。
    # ≤3語・固有識別子+症状語の契約に短縮し、内輪語「暴発」を除去（4点セット違反の是正）。
    # このリストがクエリの唯一の正本（collection-targets.md の表は表示用ミラー）。
    "launchd 止まってた",              # C09(68) 止まった自動処理に気づく仕組み
    "CLAUDE.md 守らない",              # C06(117) 書いた指示が効かない
    "Claude hooks 誤検知",             # C08(46) hook の誤検知
    "Claude 出力 テンプレ",            # C16(497) 報告形式の毎回ブレ（最大束）
    "AIエージェント 承認 フロー",       # C15(136) 承認の線引き
    "Claude Code 引き継ぎ",            # C07(284) 文脈が消える
    # gen4追加 2026-08-03（ユーザー承認「進めて」・種の接続）:
    # 本人ブックマーク gen3 9クラスタ（keywords_latest.json）の環境系クラスタから2本＋症状語1本。
    # 在庫制の原則（枠固定・退役してから追加）に対し今回は純増22→25——空振り候補5本は
    # query_ledger が3週貯まるまで観察のまま（単発ランで切らない・敵対レビューA指摘）。
    "Claude Code トークン節約",         # bookmark c-a8503f59（オーケストレーション・節約運用）
    "インプレッション 伸びない",         # bookmark c-8dff6bc8（X運用）の症状形・C18(428)にも効く
    "コンテキスト 消える",              # C07 の当事者症状語（「引き継ぎ」は解法側・こちらは症状側）
    # gen4b 2026-08-03: ブックマーク本文の機械抽出（148件 vs 収集477件の特徴語ペア分析）で
    # 最強だった1本のみ追加。共起8投稿・独立著者8人（スケール版M17ゲート通過・単一人物の反復でない）
    "Fable プロンプト",
    # gen5 2026-08-03: シグナル語採掘4ラウンド（2モデル議論→実装検証）の唯一の生存束「公式系」を
    # トピック語×シグナル語の合成で1本だけ投入（E3前向き収率テスト・63語中62語は基準未達で不採用）。
    # 根拠: 定点z+2.83（保存率上位に濃い）＋採用カード3/10 vs 落選0/14（行動変換軸でも正）。
    "Claude Code 公式",
    # gen6 2026-08-10（敵対レビューW1裁定「両方やる」= 入口のドメイン偏り是正）:
    # 実測: 攻め棚 pending 55件中、広告/CRM/株のドメイン道具（Ads/GA4/LINE/株API系）を含む投稿は
    # 1件のみ＝見張り25人と既存クエリが Claude Code 界隈に偏っている写像（レビューA#3・B#7 一致）。
    # ≤3語・R3（銘柄の中身・稼ぐ系）非衝突の配管系のみ3本。正本= make_article
    # docs/x-operation/research/architecture5-completeness-adv-review-2026-08-10.md W1裁定。
    "Google広告 レポート 自動化",       # prime_ad: Ads Scripts/API の代替実例（経路B/C の供給）
    "LINE公式 配信 自動化",             # prime_crm: 配信オペの自動化実例
    "決算 データ取得 自動化",           # influx株: 開示・株データ取得の配管（銘柄の中身は R3 で落とす）
    # gen7 2026-08-11（**勝負レーン初の専用枠**）:
    # ここまでの31本は全部「環境改善レーン」(collection-targets.md G1〜G19) 向けで、
    # 勝負レーン=意図グループ台帳12（vault make_article-buzz-intent-ledger）には
    # **専用クエリが1本も無かった**。計器 make_article `scripts/intent_group_coverage.py`
    # の実測で 12グループ中クエリが当たるのは4つだけ・棚77件の72%が方向性の外側と判明。
    # 採用率の高い3グループから、台帳の代表語をそのまま1本ずつ（≤3語・R3非衝突）。
    # 効くのは「固有名詞×作業の言い回し」（2026-08-11 症状語一周の実測）に沿って、
    # 一般語の組み合わせでなく**台帳の固有の言い回しそのもの**を打つ。
    # 在庫制: 31→34 の純増3。退役候補は下記コメント参照（4週後に intent_coverage で判定）。
    # 語の選定は既存コーパス852件（過去の実収集＋ブックマーク）で実在を確認してから決めた。
    # ⚠️ ただしこのコーパスは「既存クエリが釣った結果」なので、当たらないグループの語は
    #    そもそも入っていない＝**0件は不在の証拠にならない**。プラスの実在だけを採用根拠にした。
    "Claude Code 棚卸し",              # 意図G7 環境の健康診断（棚2件→採用2件＝採用率100%なのにクエリ皆無だった）
                                       #   実在: 「棚卸し」2件/著者1人（3本の中で最も弱い＝4週で0なら真っ先に退役）
    "Graph Engineering",               # 意図G9 構造の地図化（棚4件→採用3件）
                                       #   ⚠️ 台帳の代表語はカタカナ「グラフエンジニアリング」だが実在0件。
                                       #   英語表記は3件/著者3人（0xCodez・MakeAI_CEO・masahirochaen）＝英語で打つ
    "ループエンジニアリング",           # 意図G1 ループ運用（棚2件→採用1件）
                                       #   実在: 3件/著者2人（英語 loop engineering も2件/2人）
    # 退役候補（gen7 と同時に観察開始・4週後に intent_coverage.md の在庫/採用で判定）:
    #   "Cursor vs Claude"（IDE比較＝どの意図グループにも紐づかない）
    #   "Anthropic Claude API"（一般語すぎ・API紹介は R3 寄り）
    #   "MCP server 自作"（2026-08-10 実走で0件）
]

DEFAULT_MIN_LIKES = 50
DEFAULT_DAYS = 7
DEFAULT_PER_QUERY = 8
MODEL = "grok-4-1-fast-non-reasoning"
_QUERY_ERRORS = 0  # 2026-07-18: クレジット切れ等を「0件成功」と偽らないためのカウンタ


class BuzzTweet(BaseModel):
    id: str = Field(description="X post ID (URL の status/<id> 部分の数字)")
    url: str = Field(description="完全な X 投稿 URL https://x.com/<user>/status/<id>")
    author: str = Field(description="X handle、@ は付けない")
    display_name: str = Field(default="", description="表示名")
    content: str = Field(description="投稿本文 (改行は \\n)")
    likes: int = Field(default=0, ge=0)
    impressions: int = Field(default=0, ge=0)
    retweets: int = Field(default=0, ge=0)
    replies: int = Field(default=0, ge=0)
    posted_at: str = Field(default="", description="YYYY-MM-DD")


class BuzzCollectionResult(BaseModel):
    tweets: list[BuzzTweet] = Field(default_factory=list)


def build_prompt(query: str, days: int, min_likes: int, max_results: int) -> str:
    return (
        f"過去 {days} 日以内に X (Twitter) に投稿された、"
        f"「{query}」に関連する高エンゲージメント投稿を最大 {max_results} 件探してください。\n"
        f"- {min_likes} likes 以上\n"
        "- 個人の知見・体験談・チュートリアル・lessons learned を優先\n"
        "- 除外: 広告/宣伝/bot/単なる RT/タイトルだけのリンク投稿\n"
        "- 日本語または英語の投稿\n"
        "- スレッドの 1 投稿目を優先 (返信ぶらさがりは除く)\n\n"
        "各投稿について BuzzTweet スキーマ通りに構造化して返してください。\n"
        "id は URL の status/ の後の数字、url は https://x.com/<author>/status/<id> 形式。"
    )


def search_query(client: Client, query: str, days: int, min_likes: int, per_query: int) -> list[dict[str, Any]]:
    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    chat = client.chat.create(
        model=MODEL,
        tools=[x_search(from_date=from_dt, to_date=to_dt)],
    )
    chat.append(system(
        "あなたは X (Twitter) 上のバズ投稿を発見する分析アナリストです。"
        "実在する投稿のみを返し、URL/ID を捏造しないこと。"
    ))
    chat.append(user(build_prompt(query, days, min_likes, per_query)))
    try:
        _, parsed = chat.parse(BuzzCollectionResult)
    except Exception as exc:
        print(f"  ✗ '{query}' parse 失敗: {exc}", file=sys.stderr)
        global _QUERY_ERRORS
        _QUERY_ERRORS += 1
        return []
    if not parsed:
        return []
    rows = []
    for tw in parsed.tweets:
        d = tw.model_dump()
        if d["likes"] < min_likes:
            continue
        if not d["id"] or not d["url"]:
            continue
        d["query"] = query
        d["captured_at"] = datetime.now(JST).isoformat(timespec="seconds")
        rows.append(d)
    return rows


def dedupe(tweets: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for t in tweets:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        out.append(t)
    return out


def write_jsonl(tweets: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for t in tweets:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


def write_index(tweets: list[dict], jsonl_path: Path, md_path: Path,
                queries: list[str], days: int, min_likes: int) -> None:
    today = datetime.now(JST).date().isoformat()
    by_query: dict[str, int] = {}
    total_likes = 0
    for t in tweets:
        by_query[t["query"]] = by_query.get(t["query"], 0) + 1
        total_likes += t["likes"]

    lines = [
        "---",
        "type: source-index",
        f"source: \"grok_collect_twittora.py (xai_sdk x_search)\"",
        f"imported: {today}",
        f"entries: {len(tweets)}",
        "target_account: \"@twittora_\"",
        "target_genre: \"Claude Code / AI 活用\"",
        f"period_days: {days}",
        f"min_likes: {min_likes}",
        "---",
        "",
        f"# Grok Collect — @twittora_ buzz pool ({today})",
        "",
        f"X (Twitter) で過去 {days} 日に投稿された Claude Code / AI 活用関連の高エンゲージメント投稿プール。",
        "",
        f"- 生データ: [[{jsonl_path.name}]]",
        f"- 件数: {len(tweets)}",
        f"- 合計 likes: {total_likes:,}",
        f"- 検索 query 数: {len(queries)}",
        "",
        "## query 別の収集件数",
        "",
        "| query | 件数 |",
        "|---|---:|",
    ]
    for q in queries:
        lines.append(f"| {q} | {by_query.get(q, 0)} |")
    lines += [
        "",
        "## トップ 10 (likes 順)",
        "",
        "| likes | author | 抜粋 | URL |",
        "|---:|---|---|---|",
    ]
    top = sorted(tweets, key=lambda t: t["likes"], reverse=True)[:10]
    for t in top:
        excerpt = t["content"].replace("\n", " ").replace("|", "\\|")[:80]
        lines.append(f"| {t['likes']:,} | @{t['author']} | {excerpt} | {t['url']} |")
    lines += [
        "",
        "## 取り込み",
        "",
        "Obsidian で本ファイルが [[wiki/sources]] に展開可能 (将来 /ingest 連携)。",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--min-likes", type=int, default=DEFAULT_MIN_LIKES)
    ap.add_argument("--per-query", type=int, default=DEFAULT_PER_QUERY,
                    help="query 1 件あたりの最大取得数")
    ap.add_argument("--output-dir", default="/app/output/grok_twittora",
                    help="出力ディレクトリ (Docker 内パス)")
    args = ap.parse_args()

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("ERROR: XAI_API_KEY 環境変数が未設定", file=sys.stderr)
        return 1

    client = Client(api_key=api_key)

    print(f"=== Grok Collect (@twittora_) ===")
    print(f"queries: {len(args.queries)}, days: {args.days}, min_likes: {args.min_likes}")

    all_tweets: list[dict] = []
    for q in args.queries:
        print(f"  → searching: {q!r}")
        rows = search_query(client, q, args.days, args.min_likes, args.per_query)
        print(f"    got {len(rows)} tweets")
        all_tweets.extend(rows)

    deduped = dedupe(all_tweets)
    print(f"\nTotal: {len(all_tweets)} fetched, {len(deduped)} unique")
    if not deduped:
        if _QUERY_ERRORS:
            print(f"ERROR: 全 {_QUERY_ERRORS} クエリが失敗（API エラー・クレジット切れ等）。空ファイルは書かず異常終了する", file=sys.stderr)
            sys.exit(1)
        print("0件（閾値超えの投稿なし）。空ファイルは書かない")
        sys.exit(0)

    today = datetime.now(JST).date().isoformat()
    out_dir = Path(args.output_dir)
    jsonl_path = out_dir / f"grok-twittora-{today}.jsonl"
    md_path = out_dir / f"grok-twittora-{today}.md"

    write_jsonl(deduped, jsonl_path)
    write_index(deduped, jsonl_path, md_path, args.queries, args.days, args.min_likes)

    print(f"\n✓ {jsonl_path} ({len(deduped)} entries)")
    print(f"✓ {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
