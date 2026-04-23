#!/bin/bash
# influx プロジェクト移行スクリプト
# FROM: /Users/masaaki_nagasawa/Desktop/prm/influx
# TO:   /Users/masaaki_nagasawa/Desktop/biz/influx
#
# Usage:
#   bash scripts/migrate_to_biz.sh dry-run   # 何が起こるか表示
#   bash scripts/migrate_to_biz.sh execute   # 実際に実行

set -u

MODE="${1:-dry-run}"
SRC="/Users/masaaki_nagasawa/Desktop/prm/influx"
DST="/Users/masaaki_nagasawa/Desktop/biz/influx"
BACKUP_DIR="/Users/masaaki_nagasawa/Desktop/influx_migration_backup_$(date +%Y%m%d_%H%M%S)"
HOME_DIR="/Users/masaaki_nagasawa"
MAKE_ARTICLE="$HOME_DIR/Desktop/biz/make_article"

log()  { echo "[$MODE] $*"; }
run()  {
  if [[ "$MODE" == "execute" ]]; then
    echo "[EXEC] $*"
    eval "$@"
    local rc=$?
    if [[ $rc -ne 0 ]]; then echo "[ERROR] rc=$rc: $*"; exit $rc; fi
  else
    echo "[DRY]  $*"
  fi
}
phase(){ echo ""; echo "====== $* ======"; }

# ========================================
# Phase 0: Pre-flight Check
# ========================================
phase "Phase 0: Pre-flight Check"

if [[ ! -d "$SRC" ]]; then echo "[FATAL] Source not found"; exit 1; fi
if [[ -d "$DST" ]]; then echo "[FATAL] Target exists"; exit 1; fi
if [[ ! -d "$(dirname "$DST")" ]]; then echo "[FATAL] Target parent missing"; exit 1; fi

log "Source size: $(du -sh "$SRC" 2>/dev/null | awk '{print $1}')"
log "make_article: $([[ -d $MAKE_ARTICLE ]] && echo EXISTS || echo MISSING)"

log "Running Docker containers (influx/xstock):"
docker ps --filter "name=xstock" --filter "name=influx" --format "  {{.Names}}: {{.Status}}" 2>/dev/null || true

log "Python processes referencing influx:"
ps aux | grep -E "prm/influx" | grep -v grep | grep -v "migrate_to_biz" | head -5 || echo "  (none)"

log "Git HEAD: $(cd "$SRC" && git log -1 --oneline 2>/dev/null)"
log "Uncommitted: $(cd "$SRC" && git status --short 2>/dev/null | wc -l | tr -d ' ') files"

# ========================================
# Phase 1: Safety Backup
# ========================================
phase "Phase 1: Safety Backup (cookies & profiles)"

run "mkdir -p $BACKUP_DIR"
if [[ -f "$SRC/x_profile/cookies.json" ]]; then
  run "cp -a '$SRC/x_profile/cookies.json' '$BACKUP_DIR/cookies.json'"
fi
if [[ -d "$SRC/x_profiles" ]]; then
  run "cp -a '$SRC/x_profiles' '$BACKUP_DIR/x_profiles'"
fi
log "Backup: $BACKUP_DIR"

# ========================================
# Phase 2: Stop Docker
# ========================================
phase "Phase 2: Stop Docker (old location)"

run "cd '$SRC' && docker compose down --remove-orphans 2>/dev/null || true"
run "cd '$SRC' && docker compose -f docker-compose.vnc.yml down --remove-orphans 2>/dev/null || true"

# ========================================
# Phase 3: Update hook FIRST
# ========================================
phase "Phase 3: Update ~/.claude/hooks/restrict-cwd-edits.sh"

HOOK="$HOME_DIR/.claude/hooks/restrict-cwd-edits.sh"
if grep -q 'BIZ_DIR="\$HOME/Desktop/biz/"' "$HOOK" 2>/dev/null; then
  log "Hook already has BIZ_DIR - skip"
else
  if [[ "$MODE" == "execute" ]]; then
    python3 <<PYEOF
import pathlib
p = pathlib.Path("$HOOK")
s = p.read_text()
if 'BIZ_DIR="$HOME/Desktop/biz/"' in s:
    print("already patched"); raise SystemExit(0)
old = 'PRM_DIR="$HOME/Desktop/prm/"\n'
new = 'PRM_DIR="$HOME/Desktop/prm/"\nBIZ_DIR="$HOME/Desktop/biz/"\n'
s = s.replace(old, new, 1)
old2 = 'if [[ "$FILE_PATH" == "$PRM_DIR"* ]]; then'
new2 = 'if [[ "$FILE_PATH" == "$PRM_DIR"* ]] || [[ "$FILE_PATH" == "$BIZ_DIR"* ]]; then'
s = s.replace(old2, new2, 1)
p.write_text(s)
print("hook updated")
PYEOF
  else
    echo "[DRY]  Would add BIZ_DIR exemption to $HOOK"
  fi
fi

# ========================================
# Phase 4: Move directory
# ========================================
phase "Phase 4: Move $SRC -> $DST"

run "mv '$SRC' '$DST'"

# ========================================
# Phase 5: Fix internal absolute paths
# ========================================
phase "Phase 5: Fix internal absolute paths"

if [[ "$MODE" == "execute" ]]; then
python3 <<PYEOF
import pathlib

DST = "$DST"

# 5a: scripts/merge_all_dates.py
p = pathlib.Path(f"{DST}/scripts/merge_all_dates.py")
s = p.read_text()
old = 'output_dir = Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output")'
new = 'output_dir = Path(__file__).resolve().parent.parent / "output"'
if old in s:
    p.write_text(s.replace(old, new))
    print("merge_all_dates.py: updated")

# 5b: scripts/merge_codex_batches.py
p = pathlib.Path(f"{DST}/scripts/merge_codex_batches.py")
s = p.read_text()
s2 = s.replace(
    'Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/2026-02-19/tweets.json")',
    'Path(__file__).resolve().parent.parent / "output/2026-02-19/tweets.json"'
).replace(
    'Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/2026-02-19/classified_llm.json")',
    'Path(__file__).resolve().parent.parent / "output/2026-02-19/classified_llm.json"'
).replace(
    'Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/viewer.html")',
    'Path(__file__).resolve().parent.parent / "output/viewer.html"'
)
if s != s2:
    p.write_text(s2)
    print("merge_codex_batches.py: updated")

# 5c: .claude/settings.local.json
p = pathlib.Path(f"{DST}/.claude/settings.local.json")
s = p.read_text()
old_p = "/Users/masaaki_nagasawa/Desktop/prm/influx/.envrc"
new_p = "/Users/masaaki_nagasawa/Desktop/biz/influx/.envrc"
if old_p in s:
    count = s.count(old_p)
    p.write_text(s.replace(old_p, new_p))
    print(f"settings.local.json: {count} replacements")

print("Phase 5 done")
PYEOF
else
  echo "[DRY]  Would fix absolute paths in 3 files (merge_all_dates.py, merge_codex_batches.py, .claude/settings.local.json)"
  echo "[DRY]  btc_excel_table_chart.py: Desktop direct file ref - no change"
fi

# ========================================
# Phase 6: Update global skills
# ========================================
phase "Phase 6: Update global skills"

if [[ "$MODE" == "execute" ]]; then
python3 <<PYEOF
import pathlib
HOME = "$HOME_DIR"
targets = [
    pathlib.Path(f"{HOME}/.claude/skills/fetch-bookmarks/SKILL.md"),
    pathlib.Path(f"{HOME}/.claude/skills/max-scroll-scrape/SKILL.md"),
]
for p in targets:
    if not p.exists():
        print(f"{p.name}: NOT FOUND"); continue
    s = p.read_text()
    c = s.count("Desktop/prm/influx")
    if c:
        p.write_text(s.replace("Desktop/prm/influx", "Desktop/biz/influx"))
        print(f"{p.name}: {c} replacements")
    else:
        print(f"{p.name}: no match")
PYEOF
else
  echo "[DRY]  Would update fetch-bookmarks/SKILL.md and max-scroll-scrape/SKILL.md"
fi

# ========================================
# Phase 7: Update make_article
# ========================================
phase "Phase 7: Update make_article dependencies"

if [[ -d "$MAKE_ARTICLE" ]]; then
  if [[ "$MODE" == "execute" ]]; then
python3 <<PYEOF
import pathlib
base = pathlib.Path("$MAKE_ARTICLE")
targets = [
    base / "scripts/fetch_and_ingest.sh",
    base / "scripts/fetch_bookmarks_for_influx.py",
    base / "scripts/post_to_x.py",
    base / ".claude/skills/generate-x-article/SKILL.md",
    base / ".claude/skills/post-article/SKILL.md",
    base / "CLAUDE.md",
]
for p in targets:
    if not p.exists():
        print(f"{p.name}: NOT FOUND"); continue
    s = p.read_text()
    s2 = s.replace("Desktop/prm/influx", "Desktop/biz/influx")
    s2 = s2.replace('"prm", "influx"', '"biz", "influx"')
    s2 = s2.replace("'prm', 'influx'", "'biz', 'influx'")
    s2 = s2.replace('"prm" / "influx"', '"biz" / "influx"')
    if s != s2:
        p.write_text(s2)
        print(f"{p.name}: updated")
    else:
        print(f"{p.name}: no changes")
PYEOF
  else
    echo "[DRY]  Would update 6 files in make_article"
  fi
else
  log "make_article not found - skip"
fi

# ========================================
# Phase 8: Docker cleanup
# ========================================
phase "Phase 8: Docker network cleanup"

run "docker network rm influx_default 2>/dev/null || true"
log "Docker image rebuild is manual: cd $DST && docker compose build"

# ========================================
# Phase 9: Claude memory (copy, not rename)
# ========================================
phase "Phase 9: Claude memory directory (copy)"

OLD_MEM="$HOME_DIR/.claude/projects/-Users-masaaki-nagasawa-Desktop-prm-influx"
NEW_MEM="$HOME_DIR/.claude/projects/-Users-masaaki-nagasawa-Desktop-biz-influx"

if [[ -d "$OLD_MEM" ]] && [[ ! -d "$NEW_MEM" ]]; then
  run "cp -R '$OLD_MEM' '$NEW_MEM'"
  log "Old mem preserved at: $OLD_MEM (delete manually later)"
elif [[ -d "$NEW_MEM" ]]; then
  log "New memory dir already exists - skip"
else
  log "Old memory dir not found - skip"
fi

# ========================================
# Phase 10: Post-check
# ========================================
phase "Phase 10: Post-check"

if [[ -d "$DST" ]]; then
  log "New location OK: $DST"
  log "Git: $(cd "$DST" && git log -1 --oneline 2>/dev/null)"

  log "Old-path references in new location (should be 0):"
  grep -rI "prm/influx" "$DST" \
    --include="*.py" --include="*.sh" --include="*.json" \
    --include="*.yaml" --include="*.yml" --include="*.md" \
    2>/dev/null | grep -v "\.git/" | grep -v "migrate_to_biz" | head -5 || echo "  (none)"
fi

if [[ -d "$MAKE_ARTICLE" ]]; then
  log "Old-path references in make_article (should be 0):"
  grep -rI "prm/influx" "$MAKE_ARTICLE" 2>/dev/null | grep -v "\.git/" | head -5 || echo "  (none)"
fi

echo ""
echo "======================================="
echo "Migration $MODE complete."
if [[ "$MODE" == "execute" ]]; then
  echo "Backup: $BACKUP_DIR"
  echo ""
  echo "Next steps (manual):"
  echo "  1. cd $DST"
  echo "  2. docker compose build   # (optional, rebuild)"
  echo "  3. python3 -c 'from extensions.tier3_posting.x_poster.post_store import PostStore; print(PostStore)'"
  echo "  4. Start a NEW Claude Code session from $DST"
fi
echo "======================================="
