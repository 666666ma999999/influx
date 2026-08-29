# リサーチ運用規約 — influx 固有の具体化（SSoT）

> **思想・Why は global skill [`research-isolation`] が正本**。本書はその **influx 具体化**
> （接頭辞・パス・gitignore 実装・コマンドの固有 SSoT）。Why を二重に書かない。
>
> influx は X インフルエンサーのツイート収集 + 2 段分類（keyword + Claude LLM）。`scripts/` には
> 収集・分類の production と、`measure_*` / `btc_*_analysis` / `test_*` 系の **探索・計測スクリプトが
> 無印で混在**している。本規約はこの探索系を main から隔離し、確定だけ残すためのもの。

## influx での 3 原則の当てはまり

| 原則 | influx の状況 | 本規約の役割 |
|---|---|---|
| ① worktree 隔離 | 現状 worktree 未使用 | 実験・計測は隔離 worktree で回す（下記） |
| ② 再生成データ | **概ね対処済**（`output/` は gitignore 済・学習データは tracked 正本） | 既存の置き場マッピングを明文化するだけ |
| ③ ファイル数 | **主な gap**。46 scripts に探索/計測が無印 tracked 混在 | 探索は `_tmp_` 接頭辞で追跡外に分ける |

---

## ① git worktree でリサーチ（具体）

few-shot 改善・gold_set 実験・分類ルール試作・`measure_*` 系の計測など、**試行錯誤を伴う作業**は
隔離 worktree で行い、確定するまで main に足さない。

```bash
# repo root（main 側）から。ブランチ analysis/<topic>、ディレクトリ ../influx-<topic>
git worktree add ../influx-<topic> -b analysis/<topic>
cd ../influx-<topic>
```

- **1 worktree = 1 テーマ**（例 `analysis/fewshot-v2` `analysis/btc-divergence`）。
- 確定知見だけ main へ昇格（下表 3 種）。試行の残骸（`_tmp_*.py`・`output/` 中間物）は戻さない。
- 完了したら撤去: `git worktree remove ../influx-<topic>` → `git branch -d analysis/<topic>`（マージ済のみ）。

### 昇格パス（main へ戻すのは 3 種だけ）

| 戻すもの | 戻し方 |
|---|---|
| 確定した分析・計測スクリプト | `_tmp_` を外し正式名へ（再利用するなら tracked tool として）→ コミット |
| 確定した学習データ更新 | `data/few_shot_examples.json` を更新（旧版は `*.backup.json`）→ コミット |
| 確定知見・実験結果 | `tasks/research_pipeline.md` 追記 → コミット |

---

## ② データの格納方法（具体）

| 種別 | 置き場所 | git | 備考 |
|---|---|---|---|
| 実験・計測の出力（json/csv/jsonl/png/html） | `output/` | **gitignore 済**（`output/*.json` 等） | 再生成可能・コミットしない |
| 学習・チューニングデータ（正本） | `data/few_shot_examples.json` / `data/gold_set/` / `data/writing_style/` | **tracked** | 再生成不可の SSoT なので版管理する |
| 一時集計・スコア確認 | **標準出力のみ** | （ファイルに書かない） | 中間 json/csv を量産しない |
| ルート直下のスクショ | `/*.png` | **gitignore 済** | UI 検証の作業画像 |

- **再生成可能なら git に入れない**: 実験出力は `output/` 配下に出せば自動で追跡外。
- **学習データだけは別**: few_shot / gold_set / writing_style は手作りの正本なので tracked のまま。
- **PII / プロフィール**: `x_profile*` はログイン情報を含むため gitignore 済（触らない）。生のツイート
  本文・収集結果を外部 LLM の context に大量に貼らない。

---

## ③ ファイル数を増やさない（具体）

`scripts/`（現状 46 本）で探索/計測スクリプトが production と無印で混在するのを、接頭辞で
git 追跡可否を分けて止める。

| 種別 | 形式 | git | 例 |
|---|---|---|---|
| **探索・使い捨て** | `_tmp_<name>.py` / `_scratch/` 配下 | **追跡外**（下記 gitignore 追加） | `_tmp_btc_corr_check.py` |
| **再利用ツール / production** | `<name>.py`（無印） | 追跡 | `audit_routing.py` `fetch_bookmarks.py` |
| **定常計測ツール（繰り返し回す）** | `measure_*.py` 等（無印・tracked のまま） | 追跡 | `measure_f1.py` `measure_human_accuracy.py` |

判断基準: **「もう一度回すか」**。一度きりの探索・相関チェック・データ覗き見は `_tmp_`。
繰り返し回す計測（`measure_*`）や production は無印 tracked。`_tmp_` で始めて、再利用すると
分かったら `_tmp_` を外して昇格する。

### `.gitignore` への追加（denylist・1 回だけ）

influx は denylist 方式。末尾に以下を足す:

```gitignore
# 探索・使い捨てスクリプト（research-isolation ③）
_tmp_*.py
scripts/_tmp_*.py
scripts/_scratch/
```

- **昇格**: `_tmp_foo.py` → `foo.py` にリネームして `git add` ＝この時はじめて履歴に乗る。
- **破棄**: `rm _tmp_foo.py`。gitignore 済みなので git status / 履歴は汚れない。
- 既存の無印 `measure_*` / `btc_*_analysis` は **無理に改名しない**。次に触る時、一度きりと判明
  したものだけ `_tmp_` 化 or 削除する（既存挙動を壊さない）。

---

## 関連

- global skill [`research-isolation`] — 本規約の**思想・Why の正本**（本書はその influx 具体化）
- `tasks/research_pipeline.md` — リサーチの進行・確定知見の置き場（③ 昇格先）
- influx denylist `.gitignore` — ②③ の追跡外パターンの実装
