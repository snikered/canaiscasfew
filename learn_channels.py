#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import normalize_name, strip_quality_markers

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
QUALITY_RE = re.compile(r'(?:^|[\s\[\]_.()\-])(?:FHD|FULL\s*HD|HD|SD|UHD|4K|8K|HDR\+?|HDR10\+?|H\.?26[456]|HEVC)(?:$|[\s\[\]_.()\-])', re.I)

# Grupos claramente sob demanda. Entradas com marcador de qualidade ainda podem ser canais lineares.
VOD_RE = re.compile(r'\b(?:FILMES?|MOVIES?|S[ÉE]RIES?|NOVELAS?|DORAMAS?|NETFLIX|AMAZON\s*PRIME|PRIME\s*VIDEO|GLOBOPLAY|DISNEY\s*(?:PLUS|\+)|APPLE(?:\s*TV)?\s*(?:PLUS|\+)|HBO\s*MAX|MAX|PARAMOUNT\s*(?:PLUS|\+)|DISCOVERY\s*(?:PLUS|\+)|SBT\s*\+|STAR\s*\+|CURSOS?|ANIMES?|TEMPORADA|EPIS[ÓO]DIO)\b', re.I)
LIVE_RE = re.compile(r'\b(?:CANAIS?|ABERTOS?|GLOBOS?|RECORD(?:TV)?|SBT|BAND|VARIEDADES?|ESPORTES?|SPORTS?|SPORTV|ESPN|PREMIERE|PPV|NOT[ÍI]CIAS?|INFANTIS?|DOCUMENT[ÁA]RIOS?|HBO|TELECINE|MAX|WARNER|DISCOVERY|PARAMOUNT|RELIGIOSOS?|M[ÚU]SICA|MUSIC|LEGENDADOS?|TV)\b', re.I)
PSEUDO_RE = re.compile(r'\b(?:24\s*H(?:ORAS?|RS)?|CINE\s+(?:FILMES|S[ÉE]RIES|DESENHOS|NOVELAS)|JOGOS?\s+DO\s+DIA)\b', re.I)


def parse_extinf(line: str):
    attrs = dict(ATTR_RE.findall(line))
    in_quote = False
    separator = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            separator = i
            break
    name = line[separator + 1:].strip() if separator >= 0 else attrs.get('tvg-name', '').strip()
    return attrs, name


def safe_display(name: str) -> str:
    return strip_quality_markers(name).strip() or name.strip()


def is_candidate(name: str, group: str, tvg_id: str, manual_keys: set[str], group_count: int = 0) -> bool:
    norm = normalize_name(name)
    if norm in manual_keys:
        return True
    quality = bool(QUALITY_RE.search(name))
    group_probe = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹]', '', group)
    live_group = bool(LIVE_RE.search(group_probe))
    pseudo = bool(PSEUDO_RE.search(group_probe))
    vod = bool(VOD_RE.search(group_probe))
    group_u = group_probe.upper()
    compact_group = re.sub(r'\s+', ' ', group_u).strip()
    if any(token in compact_group for token in ('PARAMOUNT+', 'DISCOVERY+', 'SBT+', 'STAR+', 'APPLE+')) and 'PPV' not in compact_group and 'CANAIS' not in compact_group:
        vod = True
    if compact_group in {'LEGENDADOS', 'LEGENDADO'}:
        vod = True
    live_exception = any(x in group_u for x in ('GLOBOSAT FILMES', 'FILMES E SERIES', 'FILMES E SÉRIES', 'CANAIS LEGENDADOS'))
    explicit_live = any(x in compact_group for x in ('CANAIS', 'ABERTOS', 'GLOBOS', 'PPV', 'PAY-PER-VIEW', 'PAY PER VIEW', 'ESPORTES', 'SPORTV', 'ESPN', 'PREMIERE', 'RECORDTV'))
    # Grupo com centenas/milhares de entradas costuma ser catálogo VOD, não canal linear.
    if group_count > 600 and not explicit_live:
        if not (tvg_id and quality):
            return False
    # Nunca aprenda catálogos VOD/24H só porque o título contém HD/4K.
    if pseudo and not live_exception:
        return False
    if vod and not live_exception and 'CANAIS' not in group_u and 'PPV' not in group_u:
        return False
    if live_group or live_exception:
        return True
    if tvg_id:
        return True
    return False


def load_manual(path: Path) -> dict:
    return json.loads(path.read_text('utf-8')) if path.exists() else {"channels": {}}


def load_learned(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text('utf-8'))
    return {"version": 1, "channels": {}, "ambiguous_ids": {}, "stats": {}}


def build_manual_alias_index(manual_channels: dict) -> dict[str, str]:
    candidates = defaultdict(set)
    for key, rec in manual_channels.items():
        for value in [key, rec.get('name', ''), *(rec.get('aliases', []) or [])]:
            norm = normalize_name(str(value))
            if norm:
                candidates[norm].add(key)
    return {alias: next(iter(keys)) for alias, keys in candidates.items() if len(keys) == 1}


def file_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description='Aprende aliases e tvg-id de M3Us sem armazenar URLs de stream.')
    ap.add_argument('playlists', nargs='+', type=Path)
    ap.add_argument('--dictionary', type=Path, default=Path('channel_dictionary.json'))
    ap.add_argument('--learned', type=Path, default=Path('learned_ids.json'))
    ap.add_argument('--import-out', type=Path, default=None, help='Gera pacote sanitizado em vez de alterar learned_ids.json')
    args = ap.parse_args()

    manual = load_manual(args.dictionary)
    manual_channels = manual.get('channels', {}) or {}
    manual_alias_index = build_manual_alias_index(manual_channels)
    manual_keys = set(manual_alias_index)

    learned = load_learned(args.learned)
    existing = learned.get('channels', {}) or {}
    existing_ambiguous = learned.get('ambiguous_ids', {}) or {}

    # Acumula apenas metadados seguros. Nunca lê/escreve a linha seguinte (URL do stream).
    channels = defaultdict(lambda: {
        'names': Counter(), 'aliases': Counter(), 'tvg_names': Counter(), 'ids': Counter(),
        'groups': Counter(), 'playlists': set(), 'observations': 0, 'blank_id_observations': 0,
    })
    id_owners = defaultdict(Counter)
    file_stats = []
    seen_hashes = set()

    # Traz histórico para o acumulador.
    known_fingerprints = set()
    for tid, owners in existing_ambiguous.items():
        for key in owners:
            id_owners[str(tid)][str(key)] += 1
    for key, rec in existing.items():
        slot = channels[key]
        slot['names'].update({rec.get('name', key): int(rec.get('observations', 1) or 1)})
        slot['aliases'].update({a: 1 for a in rec.get('aliases', []) or []})
        slot['tvg_names'].update({a: 1 for a in rec.get('tvg_names', []) or []})
        for item in rec.get('tvg_ids', []) or []:
            if isinstance(item, dict):
                tid = str(item.get('id', '')).strip(); count = int(item.get('observations', 1) or 1)
            else:
                tid = str(item).strip(); count = 1
            if tid:
                slot['ids'][tid] += count
                id_owners[tid][key] += count
        slot['groups'].update({g: 1 for g in rec.get('groups', []) or []})
        slot['playlists'].update(rec.get('source_fingerprints', []) or [])
        known_fingerprints.update(rec.get('source_fingerprints', []) or [])
        slot['observations'] += int(rec.get('observations', 0) or 0)
        slot['blank_id_observations'] += int(rec.get('blank_id_observations', 0) or 0)

    for path in args.playlists:
        if not path.exists():
            print(f'AVISO: não existe: {path}')
            continue
        fp = file_fingerprint(path)
        if fp in seen_hashes or fp in known_fingerprints:
            print(f'Ignorando lista já aprendida/duplicata exata: {path.name}')
            continue
        seen_hashes.add(fp)
        total = kept = blank = 0
        group_counts = Counter()
        with path.open('r', encoding='utf-8', errors='replace', buffering=1024*1024) as f:
            for line in f:
                if not line.startswith('#EXTINF'):
                    continue
                attrs = dict(ATTR_RE.findall(line))
                group_counts[attrs.get('group-title', '').strip()] += 1
        with path.open('r', encoding='utf-8', errors='replace', buffering=1024*1024) as f:
            for line in f:
                if not line.startswith('#EXTINF'):
                    continue
                total += 1
                attrs, original_name = parse_extinf(line)
                tvg_id = attrs.get('tvg-id', '').strip()
                tvg_name = attrs.get('tvg-name', '').strip()
                group = attrs.get('group-title', '').strip()
                basis = original_name or tvg_name
                if not basis or not is_candidate(basis, group, tvg_id, manual_keys, group_counts.get(group, 0)):
                    continue
                key = normalize_name(basis)
                key = manual_alias_index.get(key, key)
                if not key:
                    continue
                kept += 1
                if not tvg_id:
                    blank += 1
                slot = channels[key]
                display = safe_display(basis)
                slot['names'][display] += 1
                slot['aliases'][original_name] += 1
                if tvg_name:
                    slot['tvg_names'][tvg_name] += 1
                    slot['aliases'][tvg_name] += 1
                if group:
                    slot['groups'][group] += 1
                slot['playlists'].add(fp)
                slot['observations'] += 1
                if not tvg_id:
                    slot['blank_id_observations'] += 1
                else:
                    slot['ids'][tvg_id] += 1
                    id_owners[tvg_id][key] += 1
        file_stats.append({'file': path.name, 'sha256': fp, 'entries': total, 'candidates': kept, 'blank_id_candidates': blank})
        print(f'{path.name}: {total} entradas, {kept} candidatas a EPG, {blank} sem tvg-id')

    # Um tvg-id só é confiável se não estiver associado a identidades diferentes.
    ambiguous = {tid: sorted(owners) for tid, owners in id_owners.items() if len(owners) > 1}

    output_channels = {}
    for key, slot in channels.items():
        manual_rec = manual_channels.get(key, {}) or {}
        preferred = manual_rec.get('name')
        if not preferred:
            preferred = slot['names'].most_common(1)[0][0] if slot['names'] else key
        aliases = set(manual_rec.get('aliases', []) or [])
        aliases.update(a for a, _ in slot['aliases'].most_common(40) if a)
        # Inclui nomes limpos frequentes, mas limita tamanho do JSON.
        aliases.update(a for a, _ in slot['names'].most_common(20) if a)
        ids = []
        for tid, count in slot['ids'].most_common():
            owners = id_owners.get(tid, {})
            if len(owners) == 1:
                ids.append({'id': tid, 'observations': count, 'trusted': True})
        output_channels[key] = {
            'name': preferred,
            'aliases': sorted(aliases, key=lambda s: (len(s), s.lower()))[:80],
            'tvg_names': [a for a, _ in slot['tvg_names'].most_common(30)],
            'tvg_ids': ids[:80],
            'groups': [g for g, _ in slot['groups'].most_common(20)],
            'observations': slot['observations'],
            'blank_id_observations': slot['blank_id_observations'],
            'source_count': len(slot['playlists']),
            'source_fingerprints': sorted(slot['playlists']),
        }

    all_fingerprints = sorted({fp for rec in output_channels.values() for fp in rec.get('source_fingerprints', [])})
    payload = {
        'version': 1,
        'description': 'Banco aprendido de M3Us; sem URLs de stream. IDs ambíguos não são promovidos.',
        'channels': dict(sorted(output_channels.items())),
        'ambiguous_ids': ambiguous,
        'stats': {
            'channels': len(output_channels),
            'trusted_ids': sum(len(v.get('tvg_ids', [])) for v in output_channels.values()),
            'ambiguous_ids': len(ambiguous),
            'source_count': len(all_fingerprints),
            'source_fingerprints': all_fingerprints,
            'files_in_this_import': file_stats,
        },
    }
    out = args.import_out or args.learned
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(f'Dicionário aprendido: {len(output_channels)} canais; IDs confiáveis: {payload["stats"]["trusted_ids"]}; ambíguos: {len(ambiguous)}')
    print(f'Saída: {out}')

if __name__ == '__main__':
    main()
