#!/usr/bin/env python3
"""Aprende aliases e tvg-id legítimos de listas M3U sem URLs de stream,
evitando poluição por VOD/filmes e eliminando redundâncias de qualidade (HD/FHD/4K).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from epg_utils import normalize_name, strip_quality_markers, remove_superscript_markers

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
QUALITY_RE = re.compile(
    r'(?:^|[\s\[\]_.()\-])(?:FHD|FULL\s*HD|HD|SD|UHD|4K|8K|HDR\+?|HDR10\+?|H\.?26[456]|HEVC)(?:$|[\s\[\]_.()\-])',
    re.I,
)

# Gêneros e termos exclusivos de catálogos VOD (Filmes, Séries, Desenhos sob demanda)
VOD_RE = re.compile(
    r'\b(?:FILMES?|MOVIES?|S[ÉE]RIES?|NOVELAS?|DORAMAS?|NETFLIX|AMAZON\s*PRIME|PRIME\s*VIDEO|'
    r'GLOBOPLAY|DISNEY\s*(?:PLUS|\+)|APPLE(?:\s*TV)?\s*(?:PLUS|\+)|HBO\s*MAX|MAX|PARAMOUNT\s*(?:PLUS|\+)|'
    r'DISCOVERY\s*(?:PLUS|\+)|SBT\s*\+|STAR\s*\+|CURSOS?|ANIMES?|TEMPORADA|EPIS[ÓO]DIO|LAN[ÇC]AMENTOS?)\b',
    re.I,
)

VOD_GENRES_RE = re.compile(
    r'\b(?:A[ÇC][AÃ]O|COM[ÉE]DIA|DRAMA|TERROR|SUSPENSE|FIC[ÇC][AÃ]O(?:\s*CIENT[ÍI]FICA)?|'
    r'FAROESTE|GUERRA|AVENTURA|POLICIAL|ROMANCE|FAM[ÍI]LIA|BIOGRAFIA|BIOGRAFIAS|FANTASIA|'
    r'MIST[ÉE]RIO|DOCUMENT[ÁA]RIOS?|CL[ÁA]SSICOS?|ANIMA[ÇC][AÃ]O|ADULTOS?|\+18|XXX)\b',
    re.I,
)

# Canais lineares legítimos não terminam com ano de lançamento entre parênteses
YEAR_SUFFIX_RE = re.compile(r'\(\s*(?:19|20)\d{2}\s*\)\s*(?:LEG)?\s*$', re.I)

LIVE_RE = re.compile(
    r'\b(?:CANAIS?|ABERTOS?|GLOBOS?|RECORD(?:TV)?|SBT|BAND|VARIEDADES?|ESPORTES?|SPORTS?|'
    r'SPORTV|ESPN|PREMIERE|PPV|NOT[ÍI]CIAS?|INFANTIS?|HBO|TELECINE|WARNER|DISCOVERY|PARAMOUNT|'
    r'RELIGIOSOS?|M[ÚU]SICA|MUSIC|TV)\b',
    re.I,
)

PSEUDO_RE = re.compile(
    r'\b(?:24\s*H(?:ORAS?|RS)?|CINE\s+(?:FILMES|S[ÉE]RIES|DESENHOS|NOVELAS)|JOGOS?\s+DO\s+DIA)\b',
    re.I,
)

LIVE_EXCEPTIONS = (
    'GLOBOSAT FILMES', 'FILMES E SERIES', 'FILMES E SÉRIES',
    'CANAIS LEGENDADOS', 'CANAIS HBO', 'CANAIS TELECINE',
)


def parse_extinf(line: str) -> tuple[dict[str, str], str]:
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
    cleaned = strip_quality_markers(remove_superscript_markers(name)).strip()
    return cleaned or name.strip()


def is_candidate(name: str, group: str, tvg_id: str, manual_keys: set[str], group_count: int = 0) -> bool:
    norm = normalize_name(name)
    if norm in manual_keys:
        return True

    # Se o nome termina com (2020), (1998), etc., é filme/série avulso
    if YEAR_SUFFIX_RE.search(name):
        return False

    group_probe = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹]', '', group).strip()
    group_u = group_probe.upper()
    compact_group = re.sub(r'\s+', ' ', group_u).strip()

    is_pseudo = bool(PSEUDO_RE.search(group_probe))
    is_live_exc = any(exc in group_u for exc in LIVE_EXCEPTIONS)

    if is_pseudo and not is_live_exc:
        return False

    # Detecção de grupos de VOD
    is_vod = bool(VOD_RE.search(group_probe))
    if any(token in compact_group for token in ('PARAMOUNT+', 'DISCOVERY+', 'SBT+', 'STAR+', 'APPLE+')):
        if 'PPV' not in compact_group and 'CANAIS' not in compact_group:
            is_vod = True

    if compact_group in {'LEGENDADOS', 'LEGENDADO'}:
        is_vod = True

    # Gêneros avulsos de filmes sem palavra-chave de canal ao vivo
    if VOD_GENRES_RE.search(group_probe) and not is_live_exc:
        if 'CANAIS' not in group_u and 'PPV' not in group_u and 'TV' not in group_u:
            return False

    if is_vod and not is_live_exc and 'CANAIS' not in group_u and 'PPV' not in group_u:
        return False

    explicit_live = any(
        x in compact_group for x in (
            'CANAIS', 'ABERTOS', 'GLOBOS', 'PPV', 'PAY-PER-VIEW', 'PAY PER VIEW',
            'ESPORTES', 'SPORTV', 'ESPN', 'PREMIERE', 'RECORDTV', 'SBT', 'BAND',
        )
    )

    if group_count > 400 and not explicit_live:
        return False

    if LIVE_RE.search(group_probe) or is_live_exc:
        return True

    # tvg-id só qualifica canal se não estiver em grupo de VOD
    if tvg_id and not is_vod and not VOD_GENRES_RE.search(group_probe):
        return True

    return False


def load_manual(path: Path) -> dict:
    return json.loads(path.read_text('utf-8')) if path.exists() else {"channels": {}}


def load_learned(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text('utf-8'))
        except Exception:
            pass
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


def purge_vod_from_learned(learned: dict, manual_keys: set[str]) -> int:
    """Remove filmes/séries infiltrados anteriormente no learned_ids.json."""
    channels = learned.get('channels', {})
    keys_to_remove = []

    for key, rec in channels.items():
        if key in manual_keys:
            continue
        name = rec.get('name', '')
        groups = rec.get('groups', []) or []
        groups_str = " ".join(groups).upper()

        # Condições para ser expurgado
        if YEAR_SUFFIX_RE.search(name):
            keys_to_remove.append(key)
        elif any(VOD_GENRES_RE.search(g) for g in groups) and not any(exc in groups_str for exc in LIVE_EXCEPTIONS):
            if 'CANAIS' not in groups_str and 'PPV' not in groups_str and 'TV' not in groups_str:
                keys_to_remove.append(key)

    for k in keys_to_remove:
        channels.pop(k, None)

    return len(keys_to_remove)


def main():
    ap = argparse.ArgumentParser(description='Aprende canais e tvg-id de M3Us eliminando VODs e redundâncias.')
    ap.add_argument('playlists', nargs='*', type=Path, default=[])
    ap.add_argument('--dictionary', type=Path, default=Path('channel_dictionary.json'))
    ap.add_argument('--learned', type=Path, default=Path('learned_ids.json'))
    ap.add_argument('--clean-vod', action='store_true', help='Faz uma limpeza ativa de filmes infiltrados no learned_ids.json')
    args = ap.parse_args()

    manual = load_manual(args.dictionary)
    manual_channels = manual.get('channels', {}) or {}
    manual_alias_index = build_manual_alias_index(manual_channels)
    manual_keys = set(manual_alias_index)

    learned = load_learned(args.learned)

    # Limpeza preventiva de VODs
    if args.clean_vod:
        purged = purge_vod_from_learned(learned, manual_keys)
        print(f"Limpeza de banco: {purged} registros de filmes/VOD removidos do histórico.")

    existing = learned.get('channels', {}) or {}
    existing_ambiguous = learned.get('ambiguous_ids', {}) or {}

    channels = defaultdict(lambda: {
        'names': Counter(), 'aliases': Counter(), 'tvg_names': Counter(), 'ids': Counter(),
        'groups': Counter(), 'playlists': set(), 'observations': 0, 'blank_id_observations': 0,
    })
    id_owners = defaultdict(Counter)
    seen_hashes = set()
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
                tid = str(item.get('id', '')).strip()
                count = int(item.get('observations', 1) or 1)
            else:
                tid = str(item).strip()
                count = 1
            if tid:
                slot['ids'][tid] += count
                id_owners[tid][key] += count
        slot['groups'].update({g: 1 for g in rec.get('groups', []) or []})
        slot['playlists'].update(rec.get('source_fingerprints', []) or [])
        known_fingerprints.update(rec.get('source_fingerprints', []) or [])
        slot['observations'] += int(rec.get('observations', 0) or 0)
        slot['blank_id_observations'] += int(rec.get('blank_id_observations', 0) or 0)

    file_stats = []

    for path in args.playlists:
        if not path.exists():
            print(f'AVISO: arquivo não encontrado: {path}')
            continue
        fp = file_fingerprint(path)
        if fp in seen_hashes or fp in known_fingerprints:
            print(f'Ignorando lista já aprendida: {path.name}')
            continue
        seen_hashes.add(fp)

        # 1. Contagem prévia de grupos para identificar catálogos VOD gigantescos (>400 itens)
        group_counts = Counter()
        with path.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('#EXTINF'):
                    m = re.search(r'group-title="([^"]*)"', line, re.I)
                    if m:
                        group_counts[m.group(1).strip()] += 1

        total = kept = blank = 0

        # 2. Processamento dos canais
        with path.open('r', encoding='utf-8', errors='replace') as f:
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

                # Salva o display limpo nos aliases para não acumular tags de qualidade (FHD, 4K, SD)
                slot['aliases'][display] += 1
                if original_name != display and len(original_name) < 60:
                    slot['aliases'][original_name] += 1

                if tvg_name:
                    clean_tvg = safe_display(tvg_name)
                    slot['tvg_names'][clean_tvg] += 1
                    slot['aliases'][clean_tvg] += 1

                if group:
                    slot['groups'][group] += 1

                slot['playlists'].add(fp)
                slot['observations'] += 1
                if not tvg_id:
                    slot['blank_id_observations'] += 1
                else:
                    slot['ids'][tvg_id] += 1
                    id_owners[tvg_id][key] += 1

        file_stats.append({
            'file': path.name, 'sha256': fp, 'entries': total,
            'candidates': kept, 'blank_id_candidates': blank,
        })
        print(f'{path.name}: {total} entradas -> {kept} canais válidos mantidos ({total - kept} VODs/descartes filtrados).')

    ambiguous = {tid: sorted(owners) for tid, owners in id_owners.items() if len(owners) > 1}

    output_channels = {}
    for key, slot in channels.items():
        manual_rec = manual_channels.get(key, {}) or {}
        preferred = manual_rec.get('name')
        if not preferred:
            preferred = slot['names'].most_common(1)[0][0] if slot['names'] else key

        # Normaliza e colapsa aliases para não salvar redundâncias como CANAL HD, CANAL FHD
        raw_aliases = set(manual_rec.get('aliases', []) or [])
        raw_aliases.update(a for a, _ in slot['aliases'].most_common(20) if a)
        raw_aliases.update(a for a, _ in slot['names'].most_common(10) if a)

        collapsed_aliases = set()
        seen_norms = set()
        for a in sorted(raw_aliases, key=lambda s: (len(s), s.lower())):
            clean = safe_display(a)
            n = normalize_name(clean)
            if n and n not in seen_norms:
                seen_norms.add(n)
                collapsed_aliases.add(clean)
            elif clean in raw_aliases:
                collapsed_aliases.add(clean)

        ids = []
        for tid, count in slot['ids'].most_common():
            owners = id_owners.get(tid, {})
            if len(owners) == 1:
                ids.append({'id': tid, 'observations': count, 'trusted': True})

        output_channels[key] = {
            'name': preferred,
            'aliases': sorted(collapsed_aliases, key=lambda s: (len(s), s.lower()))[:25],
            'tvg_names': [a for a, _ in slot['tvg_names'].most_common(15)],
            'tvg_ids': ids[:40],
            'groups': [g for g, _ in slot['groups'].most_common(10)],
            'observations': slot['observations'],
            'blank_id_observations': slot['blank_id_observations'],
            'source_count': len(slot['playlists']),
            'source_fingerprints': sorted(slot['playlists']),
        }

    all_fingerprints = sorted({fp for rec in output_channels.values() for fp in rec.get('source_fingerprints', [])})
    payload = {
        'version': 1,
        'description': 'Banco aprendido de canais legítimos (sem VOD e sem links de stream).',
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

    args.learned.parent.mkdir(parents=True, exist_ok=True)
    args.learned.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(f'\nDicionário consolidado: {len(output_channels)} canais reais mantidos | {payload["stats"]["trusted_ids"]} IDs mapeados.')


if __name__ == '__main__':
    main()
 # Inclui nomes limpos frequentes, mas limita o tamanho do JSON.

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
