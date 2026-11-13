#!/usr/bin/env python3
"""Compila dicionário manual + aprendizado persistente + channels.json atual no catálogo consumido pelo merge_epg.py.
- Preserva tudo o que o learn-m3u.yml extraiu no channels.json / espelho.json (não sobrescreve às cegas).
- Expurga automaticamente aliases contaminados por numeração divergente (ex: remove 'HBO [2]' de dentro de 'HBO').
- Mantém nomes literais originais da M3U (SD, HD, FHD, 4K, [H265], FHD H265) para match Plug & Play.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import normalize_name, stable_channel_id


def extract_significant_digits(text: str) -> list[str]:
    """Extrai números significativos do canal (ignorando H264, H265, 4K, 8K, 1080p, 720p, 60fps, 24h, BR2)."""
    if not text:
        return []
    s = html.unescape(str(text)).upper()
    s = re.sub(
        r"\b(?:H\.?264|H\.?265|1080[PI]?|720P?|60FPS|4K|8K|24H[S]?|24/7|BR\d+|SRV\d+|SERVER\s*\d+|OP[CÇ][AÃ]O\s*\d+)\b",
        " ",
        s,
    )
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", " ", s)
    return re.findall(r"\d+", s)


def has_conflicting_channel_number(base_channel_name: str, candidate_text: str) -> bool:
    """Impede que 'HBO [2]' ou 'HBO 2' entre como alias de 'HBO' (e vice-versa)."""
    base_digits = extract_significant_digits(base_channel_name)
    cand_digits = extract_significant_digits(candidate_text)
    if base_digits != cand_digits:
        b_up = base_channel_name.upper()
        if ("PREMIERE" in b_up or "SPORTV" in b_up or "ESPN" in b_up) and (
            (not base_digits and cand_digits == ["1"]) or (base_digits == ["1"] and not cand_digits)
        ):
            return False
        return True
    return False


def safe_normalize_key(value: str) -> str:
    """Normaliza preservando '[2]' como número '2' antes de chamar normalize_name."""
    s = html.unescape(str(value or "")).strip()
    s = re.sub(r"\[\s*(\d+)\s*\]", r" \1 ", s)
    if s.upper() in {"E!", "E"}:
        return "E!"
    return normalize_name(s) or s.upper()


def alias_index(manual_channels: dict[str, dict]) -> dict[str, str]:
    owners: dict[str, set[str]] = defaultdict(set)
    for key, rec in manual_channels.items():
        base_name = str(rec.get("name") or key)
        for value in [key, base_name, *(rec.get("aliases", []) or [])]:
            if has_conflicting_channel_number(base_name, str(value)):
                continue
            norm = safe_normalize_key(str(value))
            if norm:
                owners[norm].add(key)
    return {a: next(iter(keys)) for a, keys in owners.items() if len(keys) == 1}


def keep_raw_aliases(raw_aliases: list[str], base_name: str, max_items: int = 120) -> list[str]:
    """Preserva os nomes literais originais da M3U (SD, HD, FHD, 4K, [H265]) filtrando colisões de número."""
    seen = set()
    cleaned = []
    for alias in sorted(raw_aliases, key=len):
        if not alias:
            continue
        a_str = html.unescape(str(alias)).strip()
        if not a_str or has_conflicting_channel_number(base_name, a_str):
            continue
        lower_alias = a_str.lower()
        if lower_alias not in seen:
            seen.add(lower_alias)
            cleaned.append(a_str)
            if len(cleaned) >= max_items:
                break
    return cleaned


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dictionary", type=Path, default=Path("channel_dictionary.json"))
    ap.add_argument("--learned", type=Path, default=Path("learned_ids.json"))
    ap.add_argument("--catalog", type=Path, default=Path("channels.json"))
    args = ap.parse_args()

    manual = json.loads(args.dictionary.read_text("utf-8")) if args.dictionary.exists() else {"channels": {}}
    learned = json.loads(args.learned.read_text("utf-8")) if args.learned.exists() else {"channels": {}, "ambiguous_ids": {}}
    existing_cat = json.loads(args.catalog.read_text("utf-8")) if args.catalog.exists() else {"channels": []}

    manual_channels = manual.get("channels", {}) or {}
    learned_channels = learned.get("channels", {}) or {}
    existing_list = existing_cat.get("channels", []) if isinstance(existing_cat, dict) else []
    idx = alias_index(manual_channels)

    buckets = defaultdict(
        lambda: {
            "names": Counter(),
            "aliases": set(),
            "tvg_names": set(),
            "ids": Counter(),
            "groups": Counter(),
            "blank": 0,
            "observations": 0,
            "fingerprints": set(),
            "learned_keys": set(),
        }
    )

    # 1. Carrega dicionário manual
    for key, rec in manual_channels.items():
        b = buckets[key]
        pref_name = str(rec.get("name") or key)
        b["names"][pref_name] += 10_000
        for x in rec.get("aliases", []) or []:
            if x and not has_conflicting_channel_number(pref_name, str(x)):
                b["aliases"].add(str(x))
        b["aliases"].add(pref_name)

    # 2. Carrega learned_ids.json (filtrando contaminações antigas como HBO [2] em HBO)
    for learned_key, rec in learned_channels.items():
        norm_lk = safe_normalize_key(learned_key)
        canonical = idx.get(norm_lk, norm_lk or learned_key)
        b = buckets[canonical]
        b["learned_keys"].add(learned_key)
        name = str(rec.get("name") or learned_key)
        if not has_conflicting_channel_number(canonical, name):
            b["names"][name] += max(1, int(rec.get("observations", 1) or 1))

        for a in list(rec.get("aliases", []))[:120]:
            if a and not has_conflicting_channel_number(canonical, str(a)):
                b["aliases"].add(str(a))
        for tn in list(rec.get("tvg_names", []))[:120]:
            if tn and not has_conflicting_channel_number(canonical, str(tn)):
                b["aliases"].add(str(tn))
                b["tvg_names"].add(str(tn))

        for item in rec.get("tvg_ids", []) or []:
            if isinstance(item, dict):
                tid = str(item.get("id", "")).strip()
                count = int(item.get("observations", 1) or 1)
                trusted = bool(item.get("trusted", True))
            else:
                tid = str(item).strip()
                count = 1
                trusted = True
            if tid and trusted and not has_conflicting_channel_number(canonical, tid):
                b["ids"][tid] += count

        for g in rec.get("groups", []) or []:
            if g:
                b["groups"][str(g)] += 1

        b["blank"] += int(rec.get("blank_id_observations", 0) or 0)
        b["observations"] += int(rec.get("observations", 0) or 0)
        b["fingerprints"].update(rec.get("source_fingerprints", []) or [])

    # 3. Preserva o que o learn-m3u.yml acabou de extrair no channels.json atual
    for ch in existing_list:
        if not isinstance(ch, dict):
            continue
        raw_name = str(ch.get("name") or ch.get("id") or "").strip()
        if not raw_name:
            continue
        norm_k = safe_normalize_key(ch.get("canonical_key") or raw_name)
        canonical = idx.get(norm_k, norm_k or raw_name)
        b = buckets[canonical]
        if not has_conflicting_channel_number(canonical, raw_name):
            b["names"][raw_name] += max(5, int(ch.get("playlist_entries", 5) or 5))

        for field in ("aliases", "original_names", "blank_tvg_id_names", "blank_tvg_id_tvg_names"):
            for a in ch.get(field, []) or []:
                if a and not has_conflicting_channel_number(canonical, str(a)):
                    b["aliases"].add(str(a))
                    if field == "blank_tvg_id_tvg_names":
                        b["tvg_names"].add(str(a))

        for id_field in ("old_tvg_ids", "tvg_ids_to_export", "compatibility_ids"):
            for tid in ch.get(id_field, []) or []:
                tid_s = str(tid).strip()
                if tid_s and not has_conflicting_channel_number(canonical, tid_s):
                    b["ids"][tid_s] += 2

        grp = ch.get("group")
        if grp:
            b["groups"][str(grp)] += 1
        for g in ch.get("groups", []) or []:
            if g:
                b["groups"][str(g)] += 1

        b["observations"] = max(b["observations"], int(ch.get("playlist_entries", 1) or 1))

    unresolved_ambiguous: dict[str, list[str]] = {}
    for tid, owners in (learned.get("ambiguous_ids", {}) or {}).items():
        canonical_owners = {idx.get(safe_normalize_key(str(owner)), safe_normalize_key(str(owner))) for owner in owners}
        if len(canonical_owners) == 1:
            only_owner = next(iter(canonical_owners))
            if not has_conflicting_channel_number(only_owner, str(tid)):
                buckets[only_owner]["ids"][str(tid)] += 1
        else:
            unresolved_ambiguous[str(tid)] = sorted(canonical_owners)

    id_owners: dict[str, set[str]] = defaultdict(set)
    for key, b in buckets.items():
        for tid in b["ids"]:
            id_owners[tid].add(key)

    for tid, owners in id_owners.items():
        if len(owners) > 1:
            unresolved_ambiguous[tid] = sorted(owners)

    used_ids: set[str] = set()
    entries = []

    for key in sorted(buckets, key=lambda k: (-buckets[k]["observations"], k)):
        b = buckets[key]
        manual_rec = manual_channels.get(key, {}) or {}
        preferred = str(manual_rec.get("name") or (b["names"].most_common(1)[0][0] if b["names"] else key))
        target_id = stable_channel_id(key, used_ids)
        used_ids.add(target_id)

        raw_aliases = set(b["aliases"])
        raw_aliases.add(preferred)
        raw_aliases.update(str(x) for x in manual_rec.get("aliases", []) or [] if x)

        clean_aliases = keep_raw_aliases(list(raw_aliases), preferred, max_items=120)
        clean_tvg_names = keep_raw_aliases(list(b["tvg_names"]), preferred, max_items=120)

        trusted_ids = []
        for tid, _ in b["ids"].most_common():
            if (
                len(id_owners.get(tid, {key})) == 1
                and tid not in unresolved_ambiguous
                and not has_conflicting_channel_number(preferred, tid)
            ):
                trusted_ids.append(tid)

        entries.append(
            {
                "id": target_id,
                "canonical_key": key,
                "name": preferred,
                "aliases": clean_aliases,
                "original_names": clean_aliases,
                "old_tvg_ids": trusted_ids[:40],
                "tvg_ids_to_export": trusted_ids[:40],
                "blank_tvg_id_names": clean_aliases,
                "blank_tvg_id_tvg_names": clean_tvg_names,
                "groups": [g for g, _ in b["groups"].most_common(5)],
                "source_playlists": sorted(b["fingerprints"]),
                "playlist_entries": b["observations"],
                "blank_tvg_id_entries": b["blank"],
                "identity_basis": "persistent_dictionary_channel_name",
            }
        )

    payload = {
        "description": "Catálogo compilado. Otimizado com nomes literais para compatibilidade Plug & Play.",
        "identity_basis": "channel_name_dictionary",
        "channel_count": len(entries),
        "trusted_compatibility_ids": sum(len(x["old_tvg_ids"]) for x in entries),
        "ambiguous_ids_skipped": len(unresolved_ambiguous),
        "channels": entries,
    }

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"Catálogo compilado e higienizado: {len(entries)} canais; {payload['trusted_compatibility_ids']} IDs confiáveis")


if __name__ == "__main__":
    main()
