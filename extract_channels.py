#!/usr/bin/env python3
"""Prepara QUALQUER M3U automaticamente para o EPG universal.

Não confia no tvg-id original. O canal é identificado pelo nome e recebe um ID
interno automático. Também gera uma M3U corrigida, sem edição manual.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import normalize_name, remove_superscript_markers, stable_channel_id, strip_quality_markers

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


def set_epg_url(lines: list[str], epg_url: str | None) -> list[str]:
    if not epg_url:
        return lines
    result = list(lines)
    if result and result[0].lstrip("\ufeff").startswith("#EXTM3U"):
        header = result[0].lstrip("\ufeff")
        header = re.sub(r'\s+(?:url-tvg|x-tvg-url)="[^"]*"', "", header, flags=re.I)
        result[0] = header.rstrip() + f' url-tvg="{epg_url}"'
    else:
        result.insert(0, f'#EXTM3U url-tvg="{epg_url}"')
    return result


def clean_display_name(name: str) -> str:
    return strip_quality_markers(remove_superscript_markers(name)).strip() or name.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Converte qualquer M3U automaticamente para o EPG universal.")
    parser.add_argument("playlist", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("channels.json"))
    parser.add_argument("--fixed-playlist", type=Path, default=Path("playlist-fixed.m3u"))
    parser.add_argument("--epg-url", default="", help="URL do epg.xml.gz a inserir na M3U corrigida")
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
            "tvg_name": attrs.get("tvg-name", "").strip(),
            "group": attrs.get("group-title", "").strip(),
        })

    # Agrupa SOMENTE pelo nome normalizado. tvg-id não participa da identidade.
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        key = rec["norm"] or f"SEM_NOME_{rec['line_index']}"
        groups[key].append(rec)

    used_ids: set[str] = set()
    line_to_id: dict[int, str] = {}
    catalog: list[dict[str, object]] = []

    for items in sorted(groups.values(), key=lambda xs: min(int(x["line_index"]) for x in xs)):
        name_counts = Counter(x["cleaned_name"] for x in items if x["cleaned_name"])
        canonical = name_counts.most_common(1)[0][0] if name_counts else items[0]["original_name"]
        target_id = stable_channel_id(canonical, used_ids)
        used_ids.add(target_id)
        aliases = sorted(
            {x["cleaned_name"] for x in items if x["cleaned_name"]} |
            {x["tvg_name"] for x in items if x["tvg_name"]},
            key=lambda s: (len(s), s),
        )
        original_names = sorted({x["original_name"] for x in items if x["original_name"]})
        old_ids = sorted({x["old_tvg_id"] for x in items if x["old_tvg_id"]})
        channel_groups = sorted({x["group"] for x in items if x["group"]})
        for rec in items:
            line_to_id[int(rec["line_index"])] = target_id
        catalog.append({
            "id": target_id,
            "name": canonical,
            "aliases": aliases,
            "original_names": original_names,
            "old_tvg_ids": old_ids,
            "groups": channel_groups,
            "playlist_entries": len(items),
            "identity_basis": "channel_name",
        })

    fixed_lines = [replace_tvg_id(line, line_to_id[i]) if i in line_to_id else line for i, line in enumerate(lines)]
    fixed_lines = set_epg_url(fixed_lines, args.epg_url or None)

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.fixed_playlist.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(json.dumps({
        "description": "Catálogo universal baseado no nome; URLs de stream não são armazenadas aqui.",
        "identity_basis": "channel_name",
        "old_tvg_id_policy": "compatibility_only_never_trusted_over_name",
        "channel_count": len(catalog),
        "channels": catalog,
    }, ensure_ascii=False, indent=2) + "\n", "utf-8")
    args.fixed_playlist.write_text("\n".join(fixed_lines) + "\n", "utf-8")

    blank_before = sum(1 for x in records if not x["old_tvg_id"])
    print(f"Entradas M3U: {len(records)}")
    print(f"Canais identificados pelo nome: {len(catalog)}")
    print(f"Entradas antes sem tvg-id: {blank_before}")
    print(f"Catálogo: {args.catalog}")
    print(f"M3U corrigida automaticamente: {args.fixed_playlist}")


if __name__ == "__main__":
    main()
