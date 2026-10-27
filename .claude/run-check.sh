#!/bin/bash
# influx の本物の確かめ（P-ENV-239 段 1）。流し方: ~/.claude/scripts/kakunin.sh /Users/masaaki_nagasawa/Desktop/biz/influx
# 何を確かめるか・期待する結果（全部通れば 0・1 つでも違えば 1。所要 15 秒前後＝2026-10-28 実測）:
#   ①構文: 対象= git ls-files --cached --others --exclude-standard（管理済み＋まだ add していない新しいファイル・無視設定の物は除く）
#      .py は py_compile で読み込める／.sh は bash -n を通る／.js は node --check を通る（node がある時）／.json は JSON として読める／.plist は plutil -lint を通る
#      追加で含める: 直下の _tmp_*.py と scripts/_tmp_*.py（.gitignore で無視されているが、CLAUDE.md の決まりで AI が使い捨ての分析を
#        ここに書く＝AI が作ったばかりの壊れた物を拾うため。2026-10-28 時点で 173 本全部が読み込める＝常に落ちる物は無い）
#      外す物: data/history/raw_*/（外のサイトから落とした原本そのまま。.json の名前で中身が HTML の物や、円記号入りのサイトの JS があり
#        常に落ちる・こちらで作った物ではない）
#   ②一括テスト: CLAUDE.md「完了前に回す検証（入口はこの1つ）」の unittest 全件を、既にある Docker の像 influx-xstock で流し、
#      下の KNOWN 以外に落ちる物が無い（KNOWN が直ったら行を消す＝通った事も知らせる）。
#      CLAUDE.md の書き方（docker compose run xstock）から 3 点だけ締めている: --network none（外へ一切送らない）／
#      API の鍵を渡さない／コードもデータも読み取り専用でつなぐ（置き場のファイルを書き換えない）。この形で 891 件全部通る（2026-10-28 実測）。
#      像が無い・Docker が止まっている時は不合格（ここでは build しない＝時間とネットワークを使うため。直し方を表示する）
# 入れていない物: 個別スクリプトの --selftest 7 本（触った時だけ回す決まり・一括ゲートの代わりにならないと CLAUDE.md にある）、
#   ブラウザ・X・外部 API を使う動作（送信・投稿・課金に当たる）。
cd "$(dirname "$0")/.." || exit 2
ng=0
IMAGE=influx-xstock
LIMIT=150   # 一括テストの打ち切り秒（いつもは 10 秒前後）

# ① 構文
list=$( { git -c core.quotepath=false ls-files --cached --others --exclude-standard; ls _tmp_*.py scripts/_tmp_*.py 2>/dev/null; } | grep -v '^data/history/raw_' | sort -u)
pyjson_out=$(printf '%s\n' "$list" | python3 -I -c '
import json, os, py_compile, sys, tempfile
td = tempfile.mkdtemp()
cfile = os.path.join(td, "x.pyc")   # 置き場に __pycache__ を作らない
n = {"py": 0, "json": 0}
for f in sys.stdin.read().splitlines():
    if not os.path.isfile(f):
        continue   # 消したのに git がまだ持っている物
    if f.endswith(".py"):
        n["py"] += 1
        try:
            py_compile.compile(f, cfile=cfile, doraise=True)
        except Exception as e:
            print("NG 読み込み: %s（%s）" % (f, str(e).splitlines()[-1][:120] if str(e) else type(e).__name__))
    elif f.endswith(".json"):
        n["json"] += 1
        try:
            with open(f, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as e:
            print("NG JSON: %s（%s）" % (f, str(e)[:120]))
print("構文: .py %d 本・.json %d 本を確認" % (n["py"], n["json"]))
'); rc=$?
printf '%s\n' "$pyjson_out"
{ [ "$rc" != 0 ] || printf '%s\n' "$pyjson_out" | grep -q '^NG '; } && ng=1
nsh=0; njs=0; nplist=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in
    *.sh) nsh=$((nsh+1)); bash -n "$f" 2>/dev/null || { echo "NG 構文: $f"; ng=1; } ;;
    *.js) if command -v node >/dev/null 2>&1; then njs=$((njs+1)); node --check "$f" >/dev/null 2>&1 || { echo "NG 構文: $f"; ng=1; }; fi ;;
    *.plist) if command -v plutil >/dev/null 2>&1; then nplist=$((nplist+1)); plutil -lint -s "$f" >/dev/null 2>&1 || { echo "NG plist: $f"; ng=1; }; fi ;;
  esac
done <<EOF
$list
EOF
echo "構文: .sh ${nsh} 本・.js ${njs} 本・.plist ${nplist} 本を確認"

# ② 一括テスト（Docker・外へ送らない・読み取り専用）
# 既知の失敗（unittest の FAIL/ERROR 行の名前そのまま・1 行 1 件。2026-10-28 時点で無し。直したら消す）
KNOWN=""
if ! docker info >/dev/null 2>&1; then
  echo "NG Docker が動いていない（Docker Desktop を起動してから流し直す）"; ng=1
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "NG Docker の像 $IMAGE が無い（docker compose build xstock で作ってから流し直す）"; ng=1
else
  P=$(pwd -P); mounts=()
  for d in collector scripts core extensions configs config docs tests data output models; do
    [ -d "$d" ] && mounts+=(-v "$P/$d:/app/$d:ro")
  done
  name="influx-runcheck-$$"
  ( sleep "$LIMIT"; docker kill "$name" >/dev/null 2>&1 ) & dog=$!
  out=$(docker run --rm --label "kakunin=${KAKUNIN_RUN:-1}" --name "$name" --network none -w /app "${mounts[@]}" "$IMAGE" python -m unittest discover -s tests 2>&1); trc=$?
  kill "$dog" 2>/dev/null; wait "$dog" 2>/dev/null
  ran=$(printf '%s\n' "$out" | sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in.*/\1/p' | tail -1)
  if [ -z "$ran" ]; then
    echo "NG 一括テストが最後まで走っていない（rc=$trc・${LIMIT} 秒で打ち切りか途中で落ちた）"; printf '%s\n' "$out" | tail -5; ng=1
  elif [ "$ran" = 0 ]; then
    echo "NG 一括テストが 0 件（テストが見つからない）"; ng=1
  fi
  failed=$(printf '%s\n' "$out" | sed -nE 's/^(FAIL|ERROR): //p' | sort -u)
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if printf '%s\n' "$KNOWN" | grep -qxF "$t"; then echo "既知の失敗（未修理）: $t"; else echo "NG テスト: $t"; ng=1; fi
  done <<EOF
$failed
EOF
  [ "$trc" != 0 ] && [ -n "$ran" ] && [ -z "$failed" ] && { echo "NG 一括テストが失敗で終わったのに落ちたテストの名前が無い（rc=$trc）"; ng=1; }
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    printf '%s\n' "$failed" | grep -qxF "$t" || echo "既知の失敗が通った: $t（run-check.sh の KNOWN から消す）"
  done <<EOF
$KNOWN
EOF
  [ -n "$ran" ] && echo "一括テスト: ${ran} 件を実行（$(printf '%s\n' "$out" | grep -E '^(OK|FAILED)' | tail -1)）"
fi

[ "$ng" = 0 ] && echo "run-check: 合格" || echo "run-check: 不合格"
exit "$ng"
