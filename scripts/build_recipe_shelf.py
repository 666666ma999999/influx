#!/usr/bin/env python3
"""Build the recipe shelf from its machine-readable sources."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
META_PATH = REPO / "config/recipe_shelf_meta.json"
MATURITY_PATH = REPO / "output/kpi_maturity/maturity.csv"
SCOREBOARD_PATH = REPO / "output/paper_scoreboard.md"
TRIALS_PATH = REPO / "data/kpi_trials/trials.jsonl"
FINGERPRINTS_PATH = REPO / "data/kpi_trials/trial_fingerprints.json"
TOUCH67_PATH = REPO / "output/kpi_touch67/report.md"
OUTPUT_PATH = REPO / "output/recipe_shelf.md"
VAULT_PATH = Path(
    "/Users/masaaki_nagasawa/Documents/Obsidian Vault/02_Ai/influx/"
    "influx-recipe-shelf.md"
)

DEAD_VERDICTS = {"fail", "confirm_fail", "rejected", "hoos_rejected", "invalidated"}
DEAD_SOURCES = {"avoidance", "misokuri", "excluded_family"}
ACTIVE_TIERS = {"promising", "observing"}
TABLE_TIERS = ("usable", "promising", "observing", "dead")
TIMESTAMP_PREFIX = "生成時刻: "


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_trials(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: JSON object required")
            rows.append(row)
    return rows


def load_maturity(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"kpi_name", "freq_monthly", "maturity_date", "alpha_one_sided"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing columns: {sorted(missing)}")
        result: dict[str, dict[str, str]] = {}
        for row in reader:
            name = row["kpi_name"]
            if not name or name in result:
                raise ValueError(f"{path}: empty or duplicate kpi_name: {name!r}")
            result[name] = row
        return result


def load_scoreboard(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    result: dict[str, int] = {}
    header_seen = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\|\s*KPI\s*\|", line):
            cells = [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]
            # 見出しは「確定n」「**確定n(nostop)**」等の表記揺れを許容する（完全一致だと
            # 表示だけの改名で沈黙クラッシュする: 2026-07-30 98bd169 の実害）。
            # 「参考:確定n(stop8)」は前置ラベル付きのため前方一致に掛からない。
            n_candidates = [
                index for index, cell in enumerate(cells)
                if cell == "確定n" or cell.startswith("確定n(")
            ]
            if not n_candidates:
                raise ValueError(f"{path}: KPI table has no 確定n column")
            name_index, n_index = cells.index("KPI"), n_candidates[0]
            header_seen = True
            continue
        if not header_seen or not line.startswith("|") or re.match(r"^\|[-: |]+\|$", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) <= max(name_index, n_index):
            raise ValueError(f"{path}: malformed KPI table row: {line}")
        try:
            result[cells[name_index]] = int(cells[n_index])
        except ValueError as exc:
            raise ValueError(f"{path}: invalid 確定n: {line}") from exc
    return result


def touch67_reference(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 全体（全KPIプール） |"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"\d+(?:\.\d+)?%", cells[2]):
            raise ValueError(f"{path}: malformed all-KPI touch-rate row")
        return cells[2]
    raise ValueError(f"{path}: all-KPI touch-rate row is missing")


def format_freq(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def previous_timestamp(path: Path, body_without_timestamp: str) -> str | None:
    if not path.exists():
        return None
    old = path.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(TIMESTAMP_PREFIX)}(.+)$", old)
    if not match:
        return None
    old_without = old[: match.start(1)] + "__TIMESTAMP__" + old[match.end(1) :]
    if old_without == body_without_timestamp:
        return match.group(1)
    return None


def build() -> str:
    meta = load_json(META_PATH)
    classifications = meta.get("kpi_classification")
    tier_labels = meta.get("tier_labels")
    profile_labels = meta.get("bet_profile_labels")
    overlays = meta.get("overlay_candidates")
    if not all(isinstance(item, dict) for item in (classifications, tier_labels, profile_labels, overlays)):
        raise ValueError(f"{META_PATH}: required mapping is missing")

    maturity = load_maturity(MATURITY_PATH)
    scoreboard = load_scoreboard(SCOREBOARD_PATH)
    trials = load_trials(TRIALS_PATH)
    fingerprints = load_json(FINGERPRINTS_PATH).get("entries")
    if not isinstance(fingerprints, list):
        raise ValueError(f"{FINGERPRINTS_PATH}: entries array is missing")

    allowed_tiers = set(tier_labels)
    allowed_profiles = set(profile_labels)
    family_members: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    display_notes: dict[str, set[str]] = defaultdict(set)
    controls: set[str] = set()
    for kpi, item in classifications.items():
        if not isinstance(item, dict):
            raise ValueError(f"{META_PATH}: classification {kpi!r} must be an object")
        tier, profile, family = item.get("tier"), item.get("bet_profile"), item.get("family")
        if tier not in allowed_tiers or profile not in allowed_profiles or not family:
            raise ValueError(f"{META_PATH}: invalid classification for {kpi!r}")
        if tier == "control":
            controls.add(family)
        elif tier != "dead":
            family_members[(tier, profile, family)].append(kpi)
            if item.get("display_note"):
                display_notes[family].add(str(item["display_note"]))

    tier_rank = {"usable": 0, "promising": 1, "observing": 2}
    best_tier = {
        family: min(
            (tier for tier, _profile, candidate in family_members if candidate == family),
            key=tier_rank.__getitem__,
        )
        for _tier, _profile, family in family_members
    }
    family_members = defaultdict(
        list,
        {
            key: members
            for key, members in family_members.items()
            if key[0] == best_tier[key[2]]
        },
    )
    active_families = set(best_tier)
    controls.difference_update(active_families)

    active_kpis = {kpi for key, members in family_members.items() if key[0] in ACTIVE_TIERS for kpi in members}
    missing_maturity = sorted(active_kpis - maturity.keys())
    if missing_maturity:
        raise ValueError(f"{MATURITY_PATH}: missing active KPI rows: {missing_maturity}")

    fp_by_name: dict[str, dict[str, Any]] = {}
    for entry in fingerprints:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("family"):
            raise ValueError(f"{FINGERPRINTS_PATH}: every entry needs name and family")
        if entry["name"] in fp_by_name:
            raise ValueError(f"{FINGERPRINTS_PATH}: duplicate name: {entry['name']}")
        fp_by_name[entry["name"]] = entry

    dead_names: dict[str, set[str]] = defaultdict(set)
    for trial in trials:
        if trial.get("verdict") not in DEAD_VERDICTS:
            continue
        name = trial.get("kpi_name")
        fingerprint = fp_by_name.get(name)
        if fingerprint is None:
            raise ValueError(f"dead trial {name!r} has no fingerprint family")
        dead_names[fingerprint["family"]].add(name)
    for entry in fingerprints:
        if entry.get("source") in DEAD_SOURCES:
            dead_names[entry["family"]].add(entry["name"])
    for family in active_families:
        dead_names.pop(family, None)

    usable_families = {family for (tier, _profile, family) in family_members if tier == "usable"}
    competing_families = {family for (tier, _profile, family) in family_members if tier in ACTIVE_TIERS}
    active_dates = [maturity[kpi]["maturity_date"] for kpi in active_kpis]
    if active_dates and any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in active_dates):
        raise ValueError(f"{MATURITY_PATH}: active maturity_date must be YYYY-MM-DD")
    earliest = min(active_dates)[:7] if active_dates else "未定"

    # A formal pass must identify itself as such; loose words such as "survived"
    # are deliberately not promoted to a pass by the honesty guard.
    formal_pass = any(
        re.fullmatch(r"(?:formal_|confirm_)?pass(?:ed)?", str(row.get("verdict", "")).lower())
        for row in trials
    )
    warning = ""
    if usable_families and not formal_pass:
        warning = "\n\n> WARNING: usable family が存在しますが、trials.jsonl に正式合格 verdict がありません。"

    def family_display(family: str) -> str:
        notes = sorted(display_notes.get(family, ()))
        return f"{family}（{'・'.join(notes)}）" if notes else family

    def family_text(tier: str, profile: str) -> str:
        keys = sorted(
            (key for key in family_members if key[0] == tier and key[1] == profile),
            key=lambda key: key[2],
        )
        if not keys:
            return "—"
        pieces = []
        for key in keys:
            family = key[2]
            displayed_family = family_display(family)
            members = family_members[key]
            if tier in ACTIVE_TIERS:
                date = min(maturity[kpi]["maturity_date"] for kpi in members)
                frequency = sum(float(maturity[kpi]["freq_monthly"]) for kpi in members)
                forward_n = sum(scoreboard.get(kpi, 0) for kpi in members)
                pieces.append(f"{displayed_family} →{date}・月{format_freq(frequency)}件（前向き確定n={forward_n}）")
            else:
                pieces.append(displayed_family)
        return "<br>".join(pieces)

    dead_text = "<br>".join(
        f"{family}（{'・'.join(sorted(names))}）" for family, names in sorted(dead_names.items())
    ) or "—"
    compound_text = meta.get("compound_column_cell_text") or "診断中(0)"
    if not isinstance(compound_text, str):
        raise ValueError(f"{META_PATH}: compound_column_cell_text must be a string")

    tier_rows = []
    for tier in TABLE_TIERS:
        snipe = dead_text if tier == "dead" else family_text(tier, "snipe")
        tier_rows.append(f"| {tier_labels[tier]} | {snipe} | {compound_text} |")

    overlay_lines = []
    for name, item in sorted(overlays.items(), key=lambda pair: pair[1].get("family", "")):
        if item.get("bet_profile") != "overlay" or not item.get("family"):
            raise ValueError(f"{META_PATH}: invalid overlay {name!r}")
        overlay_lines.append(f"- {item['family']} — 採用0（{item.get('note', '単独では張らない')}）")
    if not overlay_lines:
        overlay_lines.append("- なし — 採用0")

    source_names = [
        META_PATH.relative_to(REPO).as_posix(),
        MATURITY_PATH.relative_to(REPO).as_posix(),
        SCOREBOARD_PATH.relative_to(REPO).as_posix(),
        TRIALS_PATH.relative_to(REPO).as_posix(),
        FINGERPRINTS_PATH.relative_to(REPO).as_posix(),
    ]
    if TOUCH67_PATH.exists():
        source_names.append(TOUCH67_PATH.relative_to(REPO).as_posix())

    body = f"""# 勝ちレシピ棚

> 毎朝自動更新・手書き禁止・正本=この生成物

# 実戦投入可 **{len(usable_families)}本**（正式合格）／ 初回判定 最短 {earliest} ／ 候補 {len(competing_families)} family が本番競走中{warning}

## 主表

| 段 | {profile_labels['snipe']} | {profile_labels['compound']} |
|---|---|---|
{chr(10).join(tier_rows)}

## 対照・参照（棚のレシピ数に数えない）

{('・'.join(sorted(controls)) if controls else '（対照は全て有力/観察中のfamilyに含まれるため単独表示なし）')}

## 部品棚（単独では張らない部品）

{chr(10).join(overlay_lines)}

---

{TIMESTAMP_PREFIX}__TIMESTAMP__

データ源: {' / '.join(source_names)}
"""
    timestamp = previous_timestamp(OUTPUT_PATH, body)
    if timestamp is None:
        timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return body.replace("__TIMESTAMP__", timestamp)


def main() -> None:
    content = build()
    # Write the repository source of truth first, then atomically mirror the exact bytes.
    atomic_write(OUTPUT_PATH, content)
    atomic_write(VAULT_PATH, content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    print(f"generated {OUTPUT_PATH.relative_to(REPO)} sha256(16)={digest}")
    print(f"mirrored {VAULT_PATH}")


if __name__ == "__main__":
    main()
