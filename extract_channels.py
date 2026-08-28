#!/usr/bin/env python3
"""Cria um catálogo universal a partir de UMA OU VÁRIAS M3Us.

O nome é a identidade. tvg-id antigo é guardado apenas para compatibilidade.
URLs dos streams nunca entram no channels.json.
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


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return value or "playlist"


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria catálogo universal a partir de uma ou várias M3Us.")
    parser.add_argument("playlists", nargs="+", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("channels.json"))
    parser.add_argument("--fixed-playlist", type=Path, default=None,
                        help="Compatibilidade antiga: saída da M3U corrigida quando houver uma só entrada")
    parser.add_argument("--fixed-dir", type=Path, default=None,
                        help="Diretório para M3Us corrigidas quando houver várias listas")
    parser.add_argument("--epg-url", default="", help="URL do epg.xml.gz a inserir nas M3Us corrigidas")
    args = parser.parse_args()

    records: list[dict[str, object]] = []
    playlist_lines: dict[int, list[str]] = {}
    playlist_paths: dict[int, Path] = {}

    for playlist_index, playlist in enumerate(args.playlists):
        raw = playlist.read_text("utf-8", errors="replace")
        lines = raw.splitlines()
        playlist_lines[playlist_index] = lines
        playlist_paths[playlist_index] = playlist
        for line_index, line in enumerate(lines):
            if not line.startswith("#EXTINF"):
                continue
            attrs, original_name = parse_extinf(line)
            cleaned = clean_display_name(original_name)
            old_tvg_id = attrs.get("tvg-id", "").strip()
            records.append({
                "playlist_index": playlist_index,
                "line_index": line_index,
                "original_name": original_name,
                "cleaned_name": cleaned,
                "norm": normalize_name(cleaned),
                "old_tvg_id": old_tvg_id,
                "tvg_name": attrs.get("tvg-name", "").strip(),
                "group": attrs.get("group-title", "").strip(),
            })

    # Agrupa SOMENTE pelo nome normalizado, atravessando todas as playlists.
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for rec in records:
        key = str(rec["norm"]) or f"SEM_NOME_{rec['playlist_index']}_{rec['line_index']}"
        groups[key].append(rec)

    used_ids: set[str] = set()
    line_to_id: dict[tuple[int, int], str] = {}
    catalog: list[dict[str, object]] = []

    for items in sorted(groups.values(), key=lambda xs: min((int(x["playlist_index"]), int(x["line_index"])) for x in xs)):
        name_counts = Counter(str(x["cleaned_name"]) for x in items if x["cleaned_name"])
        canonical = name_counts.most_common(1)[0][0] if name_counts else str(items[0]["original_name"])
        target_id = stable_channel_id(canonical, used_ids)
        used_ids.add(target_id)

        aliases = sorted(
            {str(x["cleaned_name"]) for x in items if x["cleaned_name"]} |
            {str(x["tvg_name"]) for x in items if x["tvg_name"]},
            key=lambda s: (len(s), s),
        )
        original_names = sorted({str(x["original_name"]) for x in items if x["original_name"]})
        old_ids = sorted({str(x["old_tvg_id"]) for x in items if x["old_tvg_id"]})
        channel_groups = sorted({str(x["group"]) for x in items if x["group"]})
        blank_id_names = sorted({str(x["original_name"]) for x in items if not x["old_tvg_id"] and x["original_name"]})
        blank_id_tvg_names = sorted({str(x["tvg_name"]) for x in items if not x["old_tvg_id"] and x["tvg_name"]})
        source_playlists = sorted({playlist_paths[int(x["playlist_index"])].name for x in items})

        for rec in items:
            line_to_id[(int(rec["playlist_index"]), int(rec["line_index"]))] = target_id

        catalog.append({
            "id": target_id,
            "name": canonical,
            "aliases": aliases,
            "original_names": original_names,
            "old_tvg_ids": old_ids,
            "blank_tvg_id_names": blank_id_names,
            "blank_tvg_id_tvg_names": blank_id_tvg_names,
            "groups": channel_groups,
            "source_playlists": source_playlists,
            "playlist_entries": len(items),
            "blank_tvg_id_entries": sum(1 for x in items if not x["old_tvg_id"]),
            "identity_basis": "channel_name",
        })

    # M3Us corrigidas são opcionais; o catálogo/EPG não depende delas.
    fixed_outputs: list[str] = []
    if args.fixed_dir:
        args.fixed_dir.mkdir(parents=True, exist_ok=True)
    for playlist_index, lines in playlist_lines.items():
        fixed_lines = [
            replace_tvg_id(line, line_to_id[(playlist_index, i)]) if (playlist_index, i) in line_to_id else line
            for i, line in enumerate(lines)
        ]
        fixed_lines = set_epg_url(fixed_lines, args.epg_url or None)
        out: Path | None = None
        if args.fixed_playlist and len(args.playlists) == 1:
            out = args.fixed_playlist
        elif args.fixed_dir:
            src = playlist_paths[playlist_index]
            out = args.fixed_dir / f"{safe_filename(src.stem)}-fixed.m3u"
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(fixed_lines) + "\n", "utf-8")
            fixed_outputs.append(str(out))

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(json.dumps({
        "description": "Catálogo universal multi-M3U baseado no nome; URLs de stream não são armazenadas.",
        "identity_basis": "channel_name",
        "old_tvg_id_policy": "compatibility_only_never_trusted_over_name",
        "playlist_count": len(args.playlists),
        "playlist_entry_count": len(records),
        "blank_tvg_id_entry_count": sum(1 for x in records if not x["old_tvg_id"]),
        "channel_count": len(catalog),
        "channels": catalog,
    }, ensure_ascii=False, indent=2) + "\n", "utf-8")

    print(f"Playlists analisadas: {len(args.playlists)}")
    print(f"Entradas M3U: {len(records)}")
    print(f"Canais identificados pelo nome: {len(catalog)}")
    print(f"Entradas sem tvg-id: {sum(1 for x in records if not x['old_tvg_id'])}")
    print(f"Catálogo: {args.catalog}")
    for out in fixed_outputs:
        print(f"M3U corrigida automática: {out}")


if __name__ == "__main__":
    main()
