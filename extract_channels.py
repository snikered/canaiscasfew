#!/usr/bin/env python3
"""Extrai um catálogo seguro de uma M3U e cria uma cópia com IDs internos estáveis."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import (
    digit_signature,
    normalize_name,
    remove_superscript_markers,
    similarity,
    stable_channel_id,
    strip_quality_markers,
)

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_extinf(line: str) -> tuple[dict[str, str], str]:
    attrs = dict(ATTR_RE.findall(line))
    name = line.split(",", 1)[1].strip() if "," in line else ""
    return attrs, name


def replace_tvg_id(line: str, target_id: str) -> str:
    if re.search(r'\btvg-id="[^"]*"', line):
        return re.sub(r'\btvg-id="[^"]*"', f'tvg-id="{target_id}"', line, count=1)
    comma = line.find(",")
    if comma == -1:
        return line + f' tvg-id="{target_id}"'
    return line[:comma] + f' tvg-id="{target_id}"' + line[comma:]


def clean_display_name(name: str) -> str:
    return strip_quality_markers(remove_superscript_markers(name)).strip() or name.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("channels.json"))
    parser.add_argument("--fixed-playlist", type=Path, default=Path("playlist-fixed.m3u"))
    args = parser.parse_args()

    raw = args.playlist.read_text("utf-8", errors="replace")
    lines = raw.splitlines()
    records: list[dict[str, str]] = []

    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        attrs, original_name = parse_extinf(line)
        cleaned = clean_display_name(original_name)
        records.append({
            "line_index": str(index),
            "original_name": original_name,
            "cleaned_name": cleaned,
            "norm": normalize_name(cleaned),
            "old_tvg_id": attrs.get("tvg-id", "").strip(),
            "group": attrs.get("group-title", "").strip(),
        })

    # Agrupamento inicial pelo nome sem os marcadores ¹²³ e sem qualidade/codec.
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        key = rec["norm"] or f"SEM_NOME_{rec['line_index']}"
        groups[key].append(rec)

    # Junta erros de digitação muito próximos somente quando o tvg-id antigo confirma
    # a relação. Ex.: HISTORY / HITORY. Não junta HBO / HBO 2.
    keys = list(groups)
    parent = {key: key for key in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    old_id_to_keys: dict[str, set[str]] = defaultdict(set)
    for key, items in groups.items():
        for old_id in {x["old_tvg_id"] for x in items if x["old_tvg_id"]}:
            old_id_to_keys[old_id].add(key)

    for candidate_keys in old_id_to_keys.values():
        candidate_keys = list(candidate_keys)
        for i, left in enumerate(candidate_keys):
            for right in candidate_keys[i + 1:]:
                if digit_signature(left) == digit_signature(right) and similarity(left, right) >= 0.88:
                    union(left, right)

    merged: dict[str, list[dict[str, str]]] = defaultdict(list)
    for key, items in groups.items():
        merged[find(key)].extend(items)

    used_ids: set[str] = set()
    line_to_id: dict[int, str] = {}
    catalog: list[dict[str, object]] = []

    for items in sorted(merged.values(), key=lambda xs: min(int(x["line_index"]) for x in xs)):
        name_counts = Counter(x["cleaned_name"] for x in items if x["cleaned_name"])
        canonical = name_counts.most_common(1)[0][0] if name_counts else items[0]["original_name"]
        target_id = stable_channel_id(canonical, used_ids)
        used_ids.add(target_id)
        aliases = sorted({x["cleaned_name"] for x in items if x["cleaned_name"]}, key=lambda s: (len(s), s))
        old_ids = sorted({x["old_tvg_id"] for x in items if x["old_tvg_id"]})
        channel_groups = sorted({x["group"] for x in items if x["group"]})
        for rec in items:
            line_to_id[int(rec["line_index"])] = target_id
        catalog.append({
            "id": target_id,
            "name": canonical,
            "aliases": aliases,
            "old_tvg_ids": old_ids,
            "groups": channel_groups,
            "playlist_entries": len(items),
        })

    fixed_lines = [replace_tvg_id(line, line_to_id[i]) if i in line_to_id else line for i, line in enumerate(lines)]
    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.fixed_playlist.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(json.dumps({
        "description": "Catálogo sem URLs dos streams; seguro para repositório público.",
        "channel_count": len(catalog),
        "channels": catalog,
    }, ensure_ascii=False, indent=2) + "\n", "utf-8")
    args.fixed_playlist.write_text("\n".join(fixed_lines) + "\n", "utf-8")

    blank_before = sum(1 for x in records if not x["old_tvg_id"])
    print(f"Entradas M3U: {len(records)}")
    print(f"Canais normalizados: {len(catalog)}")
    print(f"Entradas antes sem tvg-id: {blank_before}")
    print(f"Catálogo: {args.catalog}")
    print(f"M3U corrigida: {args.fixed_playlist}")


if __name__ == "__main__":
    main()
