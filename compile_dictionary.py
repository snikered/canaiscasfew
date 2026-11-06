#!/usr/bin/env python3
"""Compila dicionário manual + aprendizado persistente no catálogo consumido pelo merge_epg.py.
Otimizado para evitar bloqueio por excesso de aliases (Fuzzy Overload).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import normalize_name, stable_channel_id


def alias_index(manual_channels: dict[str, dict]) -> dict[str, str]:
    owners: dict[str, set[str]] = defaultdict(set)
    for key, rec in manual_channels.items():
        for value in [key, rec.get("name", ""), *(rec.get("aliases", []) or [])]:
            norm = normalize_name(str(value))
            if norm: owners[norm].add(key)
    return {a: next(iter(keys)) for a, keys in owners.items() if len(keys) == 1}


def deduplicate_aliases(raw_aliases: list[str], max_items: int = 15) -> list[str]:
    """Remove aliases que, após normalizados, são essencialmente o mesmo nome."""
    unique_norms = set()
    cleaned = []
    
    # Ordena pelos mais curtos primeiro (costumam ser mais limpos)
    for alias in sorted(raw_aliases, key=len):
        if not alias: continue
        norm = normalize_name(alias)
        if norm and norm not in unique_norms:
            unique_norms.add(norm)
            cleaned.append(alias)
            if len(cleaned) >= max_items:
                break
                
    return cleaned


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dictionary', type=Path, default=Path('channel_dictionary.json'))
    ap.add_argument('--learned', type=Path, default=Path('learned_ids.json'))
    ap.add_argument('--catalog', type=Path, default=Path('channels.json'))
    args = ap.parse_args()

    manual = json.loads(args.dictionary.read_text('utf-8')) if args.dictionary.exists() else {'channels': {}}
    learned = json.loads(args.learned.read_text('utf-8')) if args.learned.exists() else {'channels': {}, 'ambiguous_ids': {}}
    
    manual_channels = manual.get('channels', {}) or {}
    learned_channels = learned.get('channels', {}) or {}
    idx = alias_index(manual_channels)

    buckets = defaultdict(lambda: {
        'names': Counter(), 'aliases': set(), 'ids': Counter(), 'groups': Counter(),
        'blank': 0, 'observations': 0, 'fingerprints': set(), 'learned_keys': set(),
    })

    # Regras manuais entram sempre
    for key, rec in manual_channels.items():
        b = buckets[key]
        b['names'][str(rec.get('name') or key)] += 10_000
        b['aliases'].update(str(x) for x in rec.get('aliases', []) or [] if x)
        b['aliases'].add(str(rec.get('name') or key))

    for learned_key, rec in learned_channels.items():
        canonical = idx.get(normalize_name(learned_key), learned_key)
        b = buckets[canonical]
        b['learned_keys'].add(learned_key)
        name = str(rec.get('name') or learned_key)
        b['names'][name] += max(1, int(rec.get('observations', 1) or 1))
        
        # Puxa apenas uma fração do histórico para evitar acumulação de lixo
        b['aliases'].update(list(rec.get('aliases', []))[:15]) 
        
        for item in rec.get('tvg_ids', []) or []:
            if isinstance(item, dict):
                tid = str(item.get('id', '')).strip()
                count = int(item.get('observations', 1) or 1)
                trusted = bool(item.get('trusted', True))
            else:
                tid = str(item).strip(); count = 1; trusted = True
            if tid and trusted: b['ids'][tid] += count
            
        for g in rec.get('groups', []) or []:
            if g: b['groups'][str(g)] += 1
            
        b['blank'] += int(rec.get('blank_id_observations', 0) or 0)
        b['observations'] += int(rec.get('observations', 0) or 0)
        b['fingerprints'].update(rec.get('source_fingerprints', []) or [])

    unresolved_ambiguous: dict[str, list[str]] = {}
    for tid, owners in (learned.get('ambiguous_ids', {}) or {}).items():
        canonical_owners = {idx.get(normalize_name(str(owner)), str(owner)) for owner in owners}
        if len(canonical_owners) == 1: buckets[next(iter(canonical_owners))]['ids'][str(tid)] += 1
        else: unresolved_ambiguous[str(tid)] = sorted(canonical_owners)

    id_owners: dict[str, set[str]] = defaultdict(set)
    for key, b in buckets.items():
        for tid in b['ids']: id_owners[tid].add(key)
        
    for tid, owners in id_owners.items():
        if len(owners) > 1: unresolved_ambiguous[tid] = sorted(owners)

    used_ids: set[str] = set()
    entries = []
    
    for key in sorted(buckets, key=lambda k: (-buckets[k]['observations'], k)):
        b = buckets[key]
        manual_rec = manual_channels.get(key, {}) or {}
        preferred = str(manual_rec.get('name') or (b['names'].most_common(1)[0][0] if b['names'] else key))
        target_id = stable_channel_id(key, used_ids)
        used_ids.add(target_id)
        
        raw_aliases = set(b['aliases'])
        raw_aliases.add(preferred)
        raw_aliases.update(str(x) for x in manual_rec.get('aliases', []) or [] if x)
        
        # AQUI OCORRE A MÁGICA DE OTIMIZAÇÃO: Filtramos para um máximo de 15 aliases hiper-úteis
        clean_aliases = deduplicate_aliases(list(raw_aliases), max_items=15)
        
        trusted_ids = [tid for tid, _ in b['ids'].most_common() if len(id_owners.get(tid, {key})) == 1 and tid not in unresolved_ambiguous]
        blank_aliases = clean_aliases if b['blank'] else []
        
        entries.append({
            'id': target_id,
            'canonical_key': key,
            'name': preferred,
            'aliases': clean_aliases,
            'original_names': clean_aliases,
            'old_tvg_ids': trusted_ids[:20],  # Limite máximo de 20 IDs antigos
            'blank_tvg_id_names': blank_aliases,
            'blank_tvg_id_tvg_names': [],
            'groups': [g for g, _ in b['groups'].most_common(5)], # Máximo 5 grupos
            'source_playlists': sorted(b['fingerprints']),
            'playlist_entries': b['observations'],
            'blank_tvg_id_entries': b['blank'],
            'identity_basis': 'persistent_dictionary_channel_name',
        })

    payload = {
        'description': 'Catálogo compilado. Otimizado para baixa sobrecarga de Fuzzy Matching.',
        'identity_basis': 'channel_name_dictionary',
        'channel_count': len(entries),
        'trusted_compatibility_ids': sum(len(x['old_tvg_ids']) for x in entries),
        'ambiguous_ids_skipped': len(unresolved_ambiguous),
        'channels': entries,
    }
    
    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(f"Catálogo compilado: {len(entries)} canais; {payload['trusted_compatibility_ids']} IDs confiáveis; {len(unresolved_ambiguous)} ambíguos ignorados")

if __name__ == '__main__':
    main()
