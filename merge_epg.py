#!/usr/bin/env python3
"""EPG universal multi-fonte com seleção automática por consenso por canal.

Fontes padrão (ordem de preferência/desempate):
1. Genius / Curated
2. Open-EPG Brazil 4
3. EPGShare BR1
4. IPTV-EPG BR
5. EPGShare BR2

A identidade principal vem do NOME do canal da M3U. O tvg-id original é apenas uma
pista secundária e só é aceito quando o nome do canal no EPG também é compatível.
A fonte final pode mudar canal a canal conforme o consenso da programação. BR1 e
BR2 compartilham o peso da família EPGShare para não duplicar influência.
"""
from __future__ import annotations

import copy
import gzip
import json
import os
import re
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Iterable
from xml.sax.saxutils import quoteattr

from epg_utils import digit_signature, normalize_name, normalize_source_id

CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.90"))
FUZZY_MARGIN = float(os.environ.get("FUZZY_MARGIN", "0.035"))
AGREEMENT_THRESHOLD = float(os.environ.get("AGREEMENT_THRESHOLD", "0.55"))
MIN_COMPARABLE_PROGRAMMES = int(os.environ.get("MIN_COMPARABLE_PROGRAMMES", "3"))
TVG_ID_NAME_THRESHOLD = float(os.environ.get("TVG_ID_NAME_THRESHOLD", "0.72"))
DOWNLOAD_WORKERS = max(1, int(os.environ.get("DOWNLOAD_WORKERS", "5")))
GZIP_LEVEL = max(1, min(9, int(os.environ.get("GZIP_LEVEL", "6"))))
OUTPUT_PAST_HOURS = max(0, int(os.environ.get("OUTPUT_PAST_HOURS", "12")))
WRITE_PLAIN_XML = os.environ.get("WRITE_PLAIN_XML", "0").strip().lower() in {"1", "true", "yes"}

# Compatibilidade com IDs internos gerados pelas versões antigas do projeto.
# Isso permite atualizar o EPG sem obrigar o usuário a regravar M3Us já prontas.
LEGACY_INTERNAL_IDS: dict[str, tuple[str, ...]] = {
    "auto.history2": ("auto.h2",),
    "auto.aande": ("auto.aee",),
    "auto.filmandarts": ("auto.filmearts",),
    "auto.paramount": ("auto.paramountchannel",),
    "auto.warner": ("auto.warnerchannel",),
    "auto.saborandarte": ("auto.saborearte",),
    "auto.discovery": ("auto.discoverychannel",),
    "auto.discoveryhomeandhealth": ("auto.discoveryheh",),
    "auto.sportv1": ("auto.sportv",),
}


@dataclass(frozen=True)
class SourceSpec:
    key: str
    label: str
    url: str
    priority: int
    family: str
    vote_weight: float


SOURCE_SPECS = [
    SourceSpec(
        "primary", "Genius",
        os.environ.get(
            "PRIMARY_EPG_URL",
            "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz",
        ),
        1, "genius", 1.0,
    ),
    SourceSpec(
        "secondary", "Open-EPG Brazil 4",
        os.environ.get("SECONDARY_EPG_URL", "https://www.open-epg.com/files/brazil4.xml"),
        2, "open-epg", 1.0,
    ),
    SourceSpec(
        "tertiary", "EPGShare BR1",
        os.environ.get(
            "TERTIARY_EPG_URL",
            "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
        ),
        3, "epgshare", 0.5,
    ),
    SourceSpec(
        "quaternary", "IPTV-EPG BR",
        os.environ.get("QUATERNARY_EPG_URL", "https://iptv-epg.org/files/epg-br.xml"),
        4, "iptv-epg", 1.0,
    ),
    SourceSpec(
        "quinary", "EPGShare BR2",
        os.environ.get(
            "QUINARY_EPG_URL",
            "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz",
        ),
        5, "epgshare", 0.5,
    ),
]


@dataclass(frozen=True)
class ProgrammeRow:
    start: datetime
    stop: datetime
    title: str
    normalized_title: str
    element: ET.Element


@dataclass
class EpgSource:
    spec: SourceSpec
    channels: dict[str, ET.Element]
    names: dict[str, list[str]]
    normalized_names: dict[str, list[str]]
    programs: dict[str, list[ET.Element]]
    rows: dict[str, list[ProgrammeRow]]
    exact_index: dict[str, set[str]]
    fuzzy_index: dict[tuple[str, ...], dict[str, set[str]]]
    normalized_id_index: dict[str, set[str]]

    @property
    def label(self) -> str:
        return self.spec.label


@dataclass
class ChannelMatch:
    source: EpgSource
    channel_id: str
    method: str
    match_score: float


@dataclass
class CandidateEvaluation:
    match: ChannelMatch
    support_votes: int
    vote_sources: int
    support_weight: float
    vote_weight: float
    support_families: int
    weighted_ratio: float
    avg_support_agreement: float
    health_score: float
    health: dict[str, object]
    confidence: int
    comparisons: list[dict[str, object]]


def download(url: str, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "EPG-Automatico/2.0 (+GitHub Actions)", "Accept": "*/*"},
            )
            with urllib.request.urlopen(request, timeout=150) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 4)
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")


def decode_xml(payload: bytes) -> bytes:
    return gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload


def local_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def read_source(spec: SourceSpec) -> EpgSource:
    payload = decode_xml(download(spec.url))
    root = ET.fromstring(payload)
    channels: dict[str, ET.Element] = {}
    names: dict[str, list[str]] = {}
    normalized_names: dict[str, list[str]] = {}
    programs: dict[str, list[ET.Element]] = defaultdict(list)
    rows: dict[str, list[ProgrammeRow]] = defaultdict(list)
    exact_index: dict[str, set[str]] = defaultdict(set)
    fuzzy_index: dict[tuple[str, ...], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    normalized_id_index: dict[str, set[str]] = defaultdict(set)

    # Uma única passagem: datas e títulos são parseados uma vez e reutilizados
    # em todas as comparações/validações posteriores.
    for child in root:
        tag = local_tag(child)
        if tag == "channel":
            channel_id = child.attrib.get("id", "").strip()
            if not channel_id:
                continue
            channels[channel_id] = child
            display_names = [
                (node.text or "").strip()
                for node in child
                if local_tag(node) == "display-name" and (node.text or "").strip()
            ]
            names[channel_id] = display_names

            norm_candidates: list[str] = []
            for candidate in [*display_names, channel_id, normalize_source_id(channel_id)]:
                normalized = normalize_name(candidate)
                if not normalized or normalized in norm_candidates:
                    continue
                norm_candidates.append(normalized)
                exact_index[normalized].add(channel_id)
                fuzzy_index[digit_signature(normalized)][normalized].add(channel_id)
            normalized_names[channel_id] = norm_candidates

            normalized_id = normalize_source_id(channel_id)
            if normalized_id:
                normalized_id_index[normalized_id].add(channel_id)

        elif tag == "programme":
            channel_id = child.attrib.get("channel", "").strip()
            if not channel_id:
                continue

            # IMPORTANTE: preserve o <programme> original mesmo quando a fonte
            # omite `stop` ou usa um horário fora do formato que conseguimos
            # interpretar. A v10 fazia isso; a v11 rápida acabou descartando
            # esses elementos cedo demais, deixando alguns canais com 0 grade.
            programs[channel_id].append(child)

            start = parse_xmltv_datetime(child.attrib.get("start", ""))
            stop = parse_xmltv_datetime(child.attrib.get("stop", ""))
            if start is None:
                continue
            title = programme_title(child)
            # Stop ausente é inferido depois pelo início do próximo programa.
            rows[channel_id].append(ProgrammeRow(
                start=start,
                stop=stop or start,
                title=title,
                normalized_title=normalize_programme_title(title),
                element=child,
            ))

    # Algumas fontes XMLTV omitem `stop`. Para validação/consenso, inferimos o
    # fim pelo próximo `start`; no último item usamos 2h. O XML original não é
    # alterado e continua sendo publicado como veio da fonte.
    for channel_id, channel_rows in rows.items():
        channel_rows.sort(key=lambda item: item.start)
        fixed_rows: list[ProgrammeRow] = []
        for idx, row in enumerate(channel_rows):
            stop = row.stop
            if stop <= row.start:
                next_start = channel_rows[idx + 1].start if idx + 1 < len(channel_rows) else None
                if next_start is not None and next_start > row.start:
                    stop = next_start
                else:
                    stop = row.start + timedelta(hours=2)
            fixed_rows.append(ProgrammeRow(
                start=row.start, stop=stop, title=row.title,
                normalized_title=row.normalized_title, element=row.element,
            ))
        rows[channel_id] = fixed_rows

    # Libera a lista de filhos da raiz; os elementos relevantes já estão
    # referenciados pelos índices acima.
    root.clear()
    return EpgSource(
        spec=spec,
        channels=channels,
        names=names,
        normalized_names=normalized_names,
        programs=dict(programs),
        rows=dict(rows),
        exact_index=dict(exact_index),
        fuzzy_index={sig: dict(items) for sig, items in fuzzy_index.items()},
        normalized_id_index=dict(normalized_id_index),
    )


def read_optional_source(spec: SourceSpec) -> tuple[EpgSource | None, str | None]:
    try:
        return read_source(spec), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def aliases_for(entry: dict[str, object]) -> list[str]:
    """Retorna SOMENTE aliases derivados do nome.

    IDs antigos não entram aqui de propósito: em listas universais eles podem
    estar vazios, trocados ou apontando para outro canal.
    """
    values: list[str] = [str(entry.get("name", ""))]
    values.extend(str(x) for x in entry.get("aliases", []) or [])
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_name(value)
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return output


def old_id_hints_for(entry: dict[str, object]) -> list[str]:
    """IDs antigos são pistas fracas; nunca substituem uma divergência de nome."""
    output: list[str] = []
    seen: set[str] = set()
    for value in entry.get("old_tvg_ids", []) or []:
        raw = str(value).strip()
        if raw and raw not in seen:
            output.append(raw)
            seen.add(raw)
    return output


def choose_exact(source: EpgSource, aliases: Iterable[str]) -> tuple[str | None, str]:
    for alias in aliases:
        candidates = source.exact_index.get(alias, set())
        if candidates:
            selected = max(candidates, key=lambda cid: len(source.programs.get(cid, [])))
            return selected, f"nome exato: {alias}"
    return None, ""


def _sequence_ratio_upper_bound(left: str, right: str, left_counter: Counter[str] | None = None) -> float:
    """Limite superior seguro para SequenceMatcher sem executar o algoritmo caro."""
    if not left or not right:
        return 0.0
    length_bound = (2.0 * min(len(left), len(right))) / (len(left) + len(right))
    if length_bound < (FUZZY_THRESHOLD - FUZZY_MARGIN):
        return length_bound
    lc = left_counter or Counter(left)
    rc = Counter(right)
    common = sum(min(count, rc.get(ch, 0)) for ch, count in lc.items())
    char_bound = (2.0 * common) / (len(left) + len(right))
    return min(length_bound, char_bound)


def best_fuzzy(source: EpgSource, aliases: list[str]) -> tuple[str | None, float, float, str]:
    if not aliases:
        return None, 0.0, 0.0, ""

    # Mantém o mesmo SequenceMatcher/threshold da v10, mas só executa a
    # comparação quando um limite matemático mostra que o candidato ainda
    # pode afetar o resultado (best ou margem do segundo colocado).
    relevant_floor = FUZZY_THRESHOLD - FUZZY_MARGIN
    best_by_channel: dict[str, tuple[float, str, str]] = {}
    for alias in aliases:
        if len(alias) < 4:
            continue
        alias_counter = Counter(alias)
        bucket = source.fuzzy_index.get(digit_signature(alias), {})
        for normalized_candidate, channel_ids in bucket.items():
            if _sequence_ratio_upper_bound(alias, normalized_candidate, alias_counter) < relevant_floor:
                continue
            ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
            if ratio < relevant_floor:
                continue
            for channel_id in channel_ids:
                old = best_by_channel.get(channel_id)
                if old is None or ratio > old[0]:
                    best_by_channel[channel_id] = (ratio, alias, normalized_candidate)

    if not best_by_channel:
        return None, 0.0, 0.0, ""
    scored = sorted(
        ((score, channel_id, alias, candidate)
         for channel_id, (score, alias, candidate) in best_by_channel.items()),
        reverse=True,
    )
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second_score, f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"


def _name_compatibility(source: EpgSource, channel_id: str, aliases: list[str]) -> float:
    """Confirma se um candidato de tvg-id parece ser o mesmo canal pelo nome."""
    candidates = source.normalized_names.get(channel_id, [])
    best = 0.0
    for alias in aliases:
        alias_sig = digit_signature(alias)
        for candidate_norm in candidates:
            if alias_sig != digit_signature(candidate_norm):
                continue
            if alias == candidate_norm:
                return 1.0
            best = max(best, SequenceMatcher(None, alias, candidate_norm).ratio())
    return best


def match_by_safe_tvg_id_hint(
    source: EpgSource,
    aliases: list[str],
    old_ids: list[str],
) -> ChannelMatch | None:
    """Usa tvg-id só se o nome confirmar, com lookup O(1) por índice."""
    if not old_ids or not aliases:
        return None
    candidate_ids: set[str] = set()
    for value in old_ids:
        raw = value.strip()
        if raw in source.channels:
            candidate_ids.add(raw)
        normalized = normalize_source_id(raw)
        if normalized:
            candidate_ids.update(source.normalized_id_index.get(normalized, set()))

    candidates: list[tuple[float, str]] = []
    for channel_id in candidate_ids:
        compatibility = _name_compatibility(source, channel_id, aliases)
        if compatibility >= TVG_ID_NAME_THRESHOLD:
            candidates.append((compatibility, channel_id))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    score, channel_id = candidates[0]
    return ChannelMatch(source, channel_id, f"tvg-id antigo confirmado pelo nome ({score:.3f})", score)


def find_match(source: EpgSource, aliases: list[str], old_ids: list[str] | None = None) -> ChannelMatch | None:
    # 1) Nome exato; 2) nome aproximado seguro; 3) tvg-id apenas como pista confirmada pelo nome.
    exact_id, reason = choose_exact(source, aliases)
    if exact_id:
        return ChannelMatch(source, exact_id, reason, 1.0)
    fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
    if (
        fuzzy_id
        and best_score >= FUZZY_THRESHOLD
        and (best_score - second_score) >= FUZZY_MARGIN
    ):
        return ChannelMatch(source, fuzzy_id, fuzzy_reason, best_score)
    return match_by_safe_tvg_id_hint(source, aliases, old_ids or [])


def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None]:
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if not isinstance(item, dict):
        return None, None
    source = str(item.get("source", "")).lower().strip()
    channel_id = str(item.get("channel_id", "")).strip()
    valid = {spec.key for spec in SOURCE_SPECS}
    return (source, channel_id) if source in valid and channel_id else (None, None)


def add_display_name_first(channel: ET.Element, name: str) -> None:
    normalized_existing = {
        normalize_name(node.text or "")
        for node in channel
        if local_tag(node) == "display-name"
    }
    if normalize_name(name) in normalized_existing:
        return
    display = ET.Element("display-name")
    display.text = name
    channel.insert(0, display)


@lru_cache(maxsize=500000)
def parse_xmltv_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()

    # Caminho rápido para XMLTV padrão: YYYYMMDDHHMM[SS] [+/-HHMM].
    match = re.match(r"^(\d{14}|\d{12})(?:\s*([+-]\d{4}))?(?:\s+.*)?$", value)
    if match:
        digits, offset = match.groups()
        try:
            second = int(digits[12:14]) if len(digits) == 14 else 0
            tz = timezone.utc
            if offset:
                sign = 1 if offset[0] == "+" else -1
                delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
                tz = timezone(sign * delta)
            return datetime(
                int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
                int(digits[8:10]), int(digits[10:12]), second, tzinfo=tz,
            )
        except ValueError:
            pass

    # Compatibilidade com fontes fora do padrão esperado.
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def programme_title(programme: ET.Element | None) -> str:
    if programme is None:
        return ""
    for child in programme:
        if local_tag(child) == "title" and (child.text or "").strip():
            return (child.text or "").strip()
    return ""


@lru_cache(maxsize=200000)
def normalize_programme_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper()
    value = re.sub(r"\b(?:HD|FHD|AO VIVO|LIVE|ESTREIA|INEDITO|INÉDITO)\b", " ", value)
    value = re.sub(r"\bT\d+\s*E\d+\b", " ", value)
    value = re.sub(r"\bS\d+\s*E\d+\b", " ", value)
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def programme_title_similarity(a: str, b: str) -> float:
    a, b = normalize_programme_title(a), normalize_programme_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def programme_rows(source: EpgSource, channel_id: str) -> list[ProgrammeRow]:
    return source.rows.get(channel_id, [])


def now_next_for(source: EpgSource, channel_id: str, now: datetime) -> tuple[dict | None, dict | None]:
    current = None
    next_item = None
    for row in programme_rows(source, channel_id):
        if row.start <= now < row.stop:
            current = {"start": row.start, "stop": row.stop, "title": row.title}
        elif row.start > now and next_item is None:
            next_item = {"start": row.start, "stop": row.stop, "title": row.title}
            if current is not None:
                break
    return current, next_item


_AGREEMENT_CACHE: dict[tuple[str, str, str, str], dict[str, object]] = {}
_HEALTH_CACHE: dict[tuple[str, str], dict[str, object]] = {}


def cross_source_programme_agreement(
    a_source: EpgSource,
    a_id: str,
    b_source: EpgSource,
    b_id: str,
    now: datetime,
) -> dict[str, object]:
    """Mesmo critério da v10, mas com janela deslizante em vez de O(n²)."""
    left_key = (a_source.spec.key, a_id)
    right_key = (b_source.spec.key, b_id)
    cache_key = (*left_key, *right_key) if left_key <= right_key else (*right_key, *left_key)
    cached = _AGREEMENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    window_start = now - timedelta(hours=6)
    window_end = now + timedelta(hours=36)
    a_rows = [r for r in programme_rows(a_source, a_id) if r.stop >= window_start and r.start <= window_end]
    b_rows = [r for r in programme_rows(b_source, b_id) if r.stop >= window_start and r.start <= window_end]
    if not a_rows or not b_rows:
        result = {"comparable": 0, "agreement": None, "average_similarity": None}
        _AGREEMENT_CACHE[cache_key] = result
        return result

    similarities: list[float] = []
    b_floor = 0
    tolerance = timedelta(minutes=20)
    for a_row in a_rows:
        while b_floor < len(b_rows) and b_rows[b_floor].stop < a_row.start - tolerance:
            b_floor += 1

        best: tuple[float, float] | None = None
        idx = b_floor
        while idx < len(b_rows):
            b_row = b_rows[idx]
            if b_row.start > a_row.stop + tolerance:
                break
            overlap = max(0.0, (min(a_row.stop, b_row.stop) - max(a_row.start, b_row.start)).total_seconds())
            shorter = max(1.0, min((a_row.stop - a_row.start).total_seconds(), (b_row.stop - b_row.start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_row.start - a_row.start).total_seconds())
            if overlap_ratio >= 0.35 or start_delta <= 20 * 60:
                time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (20 * 60)))
                if a_row.normalized_title and b_row.normalized_title:
                    if a_row.normalized_title == b_row.normalized_title:
                        title_score = 1.0
                    else:
                        title_score = SequenceMatcher(None, a_row.normalized_title, b_row.normalized_title).ratio()
                else:
                    title_score = 0.0
                candidate = (time_score, title_score)
                if best is None or candidate > best:
                    best = candidate
            idx += 1
        if best is not None:
            similarities.append(best[1])

    if not similarities:
        result = {"comparable": 0, "agreement": None, "average_similarity": None}
        _AGREEMENT_CACHE[cache_key] = result
        return result
    agreed = sum(1 for score in similarities if score >= 0.68)
    result = {
        "comparable": len(similarities),
        "agreement": round(agreed / len(similarities), 4),
        "average_similarity": round(mean(similarities), 4),
    }
    _AGREEMENT_CACHE[cache_key] = result
    return result


def schedule_health(source: EpgSource, channel_id: str, now: datetime) -> dict[str, object]:
    cache_key = (source.spec.key, channel_id)
    cached = _HEALTH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rows = programme_rows(source, channel_id)
    current, next_item = now_next_for(source, channel_id, now)
    invalid = 0
    overlaps = 0
    gaps = 0
    previous_stop: datetime | None = None
    for row in rows:
        start, stop = row.start, row.stop
        duration = stop - start
        if duration.total_seconds() <= 0 or duration > timedelta(hours=12):
            invalid += 1
        if previous_stop is not None:
            if start < previous_stop - timedelta(seconds=30):
                overlaps += 1
            elif start > previous_stop + timedelta(minutes=30):
                gaps += 1
        if previous_stop is None or stop > previous_stop:
            previous_stop = stop

    future_end = max((row.stop for row in rows if row.stop > now), default=None)
    future_hours = max(0.0, (future_end - now).total_seconds() / 3600) if future_end else 0.0

    score = 0.0
    score += 0.35 if current else 0.0
    score += 0.15 if next_item else 0.0
    score += 0.30 * min(future_hours / 24.0, 1.0)
    score += 0.20 * min(len(rows) / 10.0, 1.0)
    score -= min(0.25, invalid * 0.08 + overlaps * 0.02 + max(0, gaps - 3) * 0.01)
    score = max(0.0, min(1.0, score))
    result = {
        "score": round(score, 4),
        "rows": len(rows),
        "current": current,
        "next": next_item,
        "future_hours": round(future_hours, 1),
        "invalid_durations": invalid,
        "overlaps": overlaps,
        "gaps": gaps,
    }
    _HEALTH_CACHE[cache_key] = result
    return result


def evaluate_candidates(
    matches: list[ChannelMatch],
    now: datetime,
    max_active_vote_weight: float,
) -> tuple[CandidateEvaluation, list[CandidateEvaluation]]:
    pairwise: dict[tuple[str, str], dict[str, object]] = {}
    for i, left in enumerate(matches):
        for right in matches[i + 1:]:
            result = cross_source_programme_agreement(
                left.source, left.channel_id, right.source, right.channel_id, now,
            )
            pairwise[(left.source.spec.key, right.source.spec.key)] = result
            pairwise[(right.source.spec.key, left.source.spec.key)] = result

    evaluations: list[CandidateEvaluation] = []
    health_by_source = {
        match.source.spec.key: schedule_health(match.source, match.channel_id, now)
        for match in matches
    }

    for match in matches:
        supporters = [match.source]
        support_votes = 1
        vote_sources = 1
        support_weight = match.source.spec.vote_weight
        vote_weight = match.source.spec.vote_weight
        support_agreements: list[float] = []
        comparisons: list[dict[str, object]] = []

        for other in matches:
            if other.source is match.source:
                continue
            result = pairwise.get((match.source.spec.key, other.source.spec.key), {})
            comparable = int(result.get("comparable") or 0)
            agreement = result.get("agreement")
            vote = "sem_dados"
            if comparable >= MIN_COMPARABLE_PROGRAMMES and isinstance(agreement, float):
                vote_sources += 1
                vote_weight += other.source.spec.vote_weight
                if agreement >= AGREEMENT_THRESHOLD:
                    vote = "concorda"
                    support_votes += 1
                    support_weight += other.source.spec.vote_weight
                    supporters.append(other.source)
                    support_agreements.append(agreement)
                else:
                    vote = "diverge"
            comparisons.append({
                "other_source": other.source.label,
                "other_source_key": other.source.spec.key,
                "other_channel_id": other.channel_id,
                "other_match_method": other.method,
                "comparable": comparable,
                "agreement": agreement,
                "average_similarity": result.get("average_similarity"),
                "vote": vote,
            })

        support_families = len({src.spec.family for src in supporters})
        weighted_ratio = support_weight / vote_weight if vote_weight else 0.0
        avg_support = mean(support_agreements) if support_agreements else 0.0
        health_score = float(health_by_source[match.source.spec.key]["score"])

        # Confiança recebe penalidade quando poucas famílias/fontes têm dados
        # comparáveis. Assim 1/1 nunca parece tão forte quanto 5/5.
        quality = (
            0.55 * weighted_ratio
            + 0.20 * health_score
            + 0.10 * match.match_score
            + 0.15 * avg_support
        )
        evidence_factor = 0.55 + 0.45 * min(vote_weight / max(max_active_vote_weight, 0.5), 1.0)
        confidence = round(100 * max(0.0, min(1.0, quality * evidence_factor)))

        evaluations.append(CandidateEvaluation(
            match=match,
            support_votes=support_votes,
            vote_sources=vote_sources,
            support_weight=round(support_weight, 3),
            vote_weight=round(vote_weight, 3),
            support_families=support_families,
            weighted_ratio=round(weighted_ratio, 4),
            avg_support_agreement=round(avg_support, 4),
            health_score=round(health_score, 4),
            health=health_by_source[match.source.spec.key],
            confidence=confidence,
            comparisons=comparisons,
        ))

    # Consenso independente manda; a ordem das fontes só desempata evidência
    # equivalente. Isso permite trocar Genius por Open/EPGShare/IPTV por canal.
    def rank(ev: CandidateEvaluation) -> tuple[int, float, int, float, float, float, float, int]:
        # Uma fonte sem nenhum <programme> nunca deve vencer outra que possui
        # grade. Isso corrige casos como canal existente no XML mas sem agenda.
        has_programmes = int(bool(ev.match.source.programs.get(ev.match.channel_id)))
        return (
            has_programmes,
            ev.support_weight,
            ev.support_families,
            ev.weighted_ratio,
            ev.avg_support_agreement,
            ev.health_score,
            ev.match.match_score,
            -ev.match.source.spec.priority,
        )

    evaluations.sort(key=rank, reverse=True)
    return evaluations[0], evaluations


def serialize_now_next(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {
        "title": item["title"],
        "start": item["start"].isoformat(),
        "stop": item["stop"].isoformat(),
    }


def fmt_local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")
    except ValueError:
        return value


def make_validation(
    entry: dict[str, object],
    chosen: CandidateEvaluation,
    all_evaluations: list[CandidateEvaluation],
    now: datetime,
) -> dict[str, object]:
    health = chosen.health
    warnings: list[str] = []

    if not health["rows"]:
        warnings.append("nenhum programa na fonte escolhida")
    if health["current"] is None:
        warnings.append("sem programa cobrindo o horário atual")
    if health["next"] is None:
        warnings.append("sem próximo programa disponível")
    if float(health["future_hours"]) < 6:
        warnings.append(f"pouca programação futura ({health['future_hours']} h)")
    if int(health["invalid_durations"]):
        warnings.append(f"{health['invalid_durations']} programa(s) com duração inválida/suspeita")
    if int(health["overlaps"]) >= 3:
        warnings.append(f"{health['overlaps']} sobreposições de horários")
    if int(health["gaps"]) >= 4:
        warnings.append(f"{health['gaps']} lacunas maiores que 30 min")

    first_by_priority = min(all_evaluations, key=lambda ev: ev.match.source.spec.priority)
    switched = first_by_priority.match.source is not chosen.match.source
    if switched:
        warnings.append(
            f"fonte alterada automaticamente de {first_by_priority.match.source.label} "
            f"para {chosen.match.source.label} por maior consenso"
        )

    if chosen.support_families >= 3 and chosen.confidence >= 75:
        status = "ok"
    elif chosen.confidence >= 55 and chosen.support_families >= 2:
        status = "atencao"
    else:
        status = "suspeito"

    if float(health["score"]) < 0.45:
        status = "suspeito"
    if chosen.vote_sources >= 3 and chosen.weighted_ratio <= 0.50:
        status = "suspeito"
        warnings.append("a fonte escolhida não alcançou maioria ponderada entre as fontes comparáveis")

    candidates = []
    for ev in sorted(all_evaluations, key=lambda x: x.match.source.spec.priority):
        candidates.append({
            "source": ev.match.source.label,
            "source_key": ev.match.source.spec.key,
            "channel_id": ev.match.channel_id,
            "match_method": ev.match.method,
            "match_score": round(ev.match.match_score, 4),
            "support_votes": ev.support_votes,
            "vote_sources": ev.vote_sources,
            "support_weight": ev.support_weight,
            "vote_weight": ev.vote_weight,
            "support_families": ev.support_families,
            "weighted_ratio": ev.weighted_ratio,
            "avg_support_agreement": ev.avg_support_agreement,
            "health_score": ev.health_score,
            "confidence": ev.confidence,
        })

    return {
        "target_id": str(entry["id"]),
        "name": str(entry["name"]),
        "source": chosen.match.source.label,
        "source_key": chosen.match.source.spec.key,
        "source_channel_id": chosen.match.channel_id,
        "status": status,
        "confidence": chosen.confidence,
        "warnings": warnings,
        "selection_changed_from_priority": switched,
        "now": serialize_now_next(health["current"]),
        "next": serialize_now_next(health["next"]),
        "future_hours": health["future_hours"],
        "health_score": health["score"],
        "consensus": {
            "supporting_votes": chosen.support_votes,
            "vote_sources": chosen.vote_sources,
            "support_weight": chosen.support_weight,
            "vote_weight": chosen.vote_weight,
            "support_families": chosen.support_families,
            "weighted_ratio": chosen.weighted_ratio,
            "confidence": chosen.confidence,
        },
        "cross_sources": chosen.comparisons,
        "candidates": candidates,
    }



def _compatibility_candidates(entry: dict[str, object]) -> list[str]:
    """IDs que podem ligar uma M3U existente ao XML sem editar a lista.

    Inclui tvg-id antigos e, somente nas entradas que vieram sem tvg-id, o nome
    exato/tvg-name. Muitos players tentam casar pelo nome quando tvg-id está vazio.
    """
    values: list[str] = []
    target_id = str(entry.get("id", "")).strip()
    values.extend(LEGACY_INTERNAL_IDS.get(target_id, ()))
    values.extend(str(x).strip() for x in entry.get("old_tvg_ids", []) or [])
    values.extend(str(x).strip() for x in entry.get("blank_tvg_id_names", []) or [])
    values.extend(str(x).strip() for x in entry.get("blank_tvg_id_tvg_names", []) or [])
    # Nome limpo também ajuda players que retiram qualidade antes do casamento.
    if int(entry.get("blank_tvg_id_entries", 0) or 0) > 0:
        values.append(str(entry.get("name", "")).strip())
    return [v for v in dict.fromkeys(values) if v and len(v) <= 220]


def build_compatibility_ids(entries: list[dict[str, object]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Publica a mesma grade sob IDs/nome-alias exclusivos de todas as M3Us.

    Se um ID/nome for reutilizado por canais diferentes, ele é descartado para
    evitar que um guia errado vença silenciosamente.
    """
    owners: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        target_id = str(entry.get("id", ""))
        for candidate in _compatibility_candidates(entry):
            owners[candidate].add(target_id)

    by_target: dict[str, list[str]] = {}
    ambiguous: dict[str, list[str]] = {}
    for entry in entries:
        target_id = str(entry.get("id", ""))
        ids = [target_id]
        bad: list[str] = []
        for candidate in _compatibility_candidates(entry):
            if candidate == target_id:
                continue
            if len(owners.get(candidate, set())) == 1:
                ids.append(candidate)
            else:
                bad.append(candidate)
        by_target[target_id] = list(dict.fromkeys(ids))
        if bad:
            ambiguous[target_id] = sorted(set(bad))
    return by_target, ambiguous

def write_epg_stream(
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]],
    compatibility_ids: dict[str, list[str]],
    now: datetime,
) -> tuple[int, int]:
    """Escreve XMLTV diretamente no gzip, sem árvore gigante nem deepcopy de programas."""
    gzip_path = OUTPUT_DIR / "epg.xml.gz"
    plain_path = OUTPUT_DIR / "epg.xml"
    plain = plain_path.open("wb") if WRITE_PLAIN_XML else None
    programme_count = 0
    compatibility_alias_count = 0
    cutoff = now - timedelta(hours=OUTPUT_PAST_HOURS)

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(
            filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL,
        ) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None:
                    plain.write(data)

            generator = "EPG universal automático por nome + consenso de 5 fontes"
            source_name = "Identificação por nome + seleção canal a canal por consenso de 5 fontes"
            header = (
                "<?xml version='1.0' encoding='utf-8'?>\n"
                f"<tv generator-info-name={quoteattr(generator)} source-info-name={quoteattr(source_name)}>\n"
            ).encode("utf-8")
            emit(header)

            for entry, chosen, _evaluations, _forced in selected:
                target_id = str(entry["id"])
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                compatibility_alias_count += max(0, len(ids_for_channel) - 1)
                display_aliases = [
                    str(entry.get("name", "")),
                    *(str(x) for x in entry.get("aliases", []) or []),
                    *(str(x) for x in entry.get("original_names", []) or []),
                ]
                display_aliases = list(dict.fromkeys(x for x in display_aliases if x))
                for output_id in ids_for_channel:
                    channel = copy.deepcopy(chosen.match.source.channels[chosen.match.channel_id])
                    channel.attrib["id"] = output_id
                    for display_name in reversed(display_aliases):
                        add_display_name_first(channel, display_name)
                    emit(ET.tostring(channel, encoding="utf-8"))
                    emit(b"\n")

            for entry, chosen, _evaluations, _forced in selected:
                target_id = str(entry["id"])
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                # Publica todos os <programme> originais. Horários válidos e
                # antigos ainda são filtrados; itens com `stop` ausente não são
                # descartados só por não entrarem no índice de validação.
                for original in chosen.match.source.programs.get(chosen.match.channel_id, []):
                    stop = parse_xmltv_datetime(original.attrib.get("stop", ""))
                    start = parse_xmltv_datetime(original.attrib.get("start", ""))
                    if stop is not None and stop < cutoff:
                        continue
                    if stop is None and start is not None and start < cutoff:
                        continue
                    old_channel = original.attrib.get("channel")
                    try:
                        for output_id in ids_for_channel:
                            original.attrib["channel"] = output_id
                            emit(ET.tostring(original, encoding="utf-8"))
                            emit(b"\n")
                            programme_count += 1
                    finally:
                        if old_channel is None:
                            original.attrib.pop("channel", None)
                        else:
                            original.attrib["channel"] = old_channel

            emit(b"</tv>\n")

    if plain is not None:
        plain.close()
    elif plain_path.exists():
        plain_path.unlink()
    return programme_count, compatibility_alias_count


def main() -> None:
    run_start = time.perf_counter()
    timings: dict[str, float] = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stage_start = time.perf_counter()
    catalog_data = json.loads(CATALOG_PATH.read_text("utf-8"))
    entries = catalog_data.get("channels", [])
    overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}
    compatibility_ids, ambiguous_old_ids = build_compatibility_ids(entries)
    timings["catalogo"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    print(f"Carregando {len(SOURCE_SPECS)} fontes em paralelo (até {DOWNLOAD_WORKERS} workers)...")
    loaded: dict[int, tuple[EpgSource | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(SOURCE_SPECS))) as executor:
        future_map = {executor.submit(read_optional_source, spec): spec for spec in SOURCE_SPECS}
        for future in as_completed(future_map):
            spec = future_map[future]
            try:
                source, error = future.result()
            except Exception as exc:  # pragma: no cover - proteção extra
                source, error = None, str(exc)
            loaded[spec.priority] = (source, error)
            if source is not None:
                print(f"✓ {spec.label}: {len(source.channels)} canais / {sum(len(v) for v in source.rows.values())} programas")
            else:
                print(f"⚠ {spec.label}: {error}")

    active_sources: list[EpgSource] = []
    source_errors: dict[str, str] = {}
    for spec in SOURCE_SPECS:
        source, error = loaded.get(spec.priority, (None, "fonte não retornou resultado"))
        if source is not None:
            active_sources.append(source)
        else:
            source_errors[spec.label] = error or "falha desconhecida"
    timings["download_e_indexacao_fontes"] = round(time.perf_counter() - stage_start, 3)

    if not active_sources:
        raise RuntimeError("Nenhuma das cinco fontes de EPG pôde ser carregada.")

    source_by_key = {source.spec.key: source for source in active_sources}
    max_active_vote_weight = sum(source.spec.vote_weight for source in active_sources)
    now = datetime.now(timezone.utc)


    stage_start = time.perf_counter()
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]] = []
    report_rows: list[dict[str, object]] = []
    validations: list[dict[str, object]] = []

    for entry in entries:
        target_id = str(entry["id"])
        visible_name = str(entry["name"])
        aliases = aliases_for(entry)
        old_id_hints = old_id_hints_for(entry)
        matches = [m for source in active_sources if (m := find_match(source, aliases, old_id_hints)) is not None]

        override_source_key, override_id = manual_override(overrides, target_id)
        forced = False
        if override_source_key and override_id:
            source = source_by_key.get(override_source_key)
            if source is not None and override_id in source.channels:
                override_match = ChannelMatch(source, override_id, "override manual", 1.0)
                # Mantém os demais candidatos para validação, mas garante o override na lista.
                matches = [m for m in matches if m.source is not source]
                matches.append(override_match)
                forced = True

        if not matches:
            report_rows.append({
                "target_id": target_id,
                "name": visible_name,
                "status": "missing",
                "source": None,
                "source_key": None,
                "source_channel_id": None,
                "method": "sem correspondência segura nas fontes ativas",
                "confidence": 0,
                "support_votes": 0,
                "vote_sources": 0,
            })
            continue

        chosen, evaluations = evaluate_candidates(matches, now, max_active_vote_weight)
        if forced and override_source_key:
            forced_ev = next(
                (ev for ev in evaluations if ev.match.source.spec.key == override_source_key and ev.match.channel_id == override_id),
                None,
            )
            if forced_ev is not None:
                chosen = forced_ev

        selected.append((entry, chosen, evaluations, forced))
        first_match = min(evaluations, key=lambda ev: ev.match.source.spec.priority)
        changed = first_match.match.source is not chosen.match.source
        report_rows.append({
            "target_id": target_id,
            "name": visible_name,
            "status": "matched",
            "source": chosen.match.source.label,
            "source_key": chosen.match.source.spec.key,
            "source_channel_id": chosen.match.channel_id,
            "method": chosen.match.method,
            "match_score": round(chosen.match.match_score, 4),
            "confidence": chosen.confidence,
            "support_votes": chosen.support_votes,
            "vote_sources": chosen.vote_sources,
            "support_weight": chosen.support_weight,
            "vote_weight": chosen.vote_weight,
            "support_families": chosen.support_families,
            "weighted_ratio": chosen.weighted_ratio,
            "changed_from_first_available": changed,
            "first_available_source": first_match.match.source.label,
            "forced_override": forced,
            "programmes": len(chosen.match.source.programs.get(chosen.match.channel_id, [])),
        })
        validations.append(make_validation(entry, chosen, evaluations, now))

    timings["matching_consenso_validacao"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    programme_count, compatibility_alias_count = write_epg_stream(selected, compatibility_ids, now)
    timings["serializacao_e_gzip"] = round(time.perf_counter() - stage_start, 3)

    source_counts = {
        source.label: sum(1 for row in report_rows if row.get("source") == source.label)
        for source in active_sources
    }
    counts = {
        "playlist_channels": len(entries),
        "matched": sum(1 for row in report_rows if row["status"] == "matched"),
        "missing": sum(1 for row in report_rows if row["status"] == "missing"),
        "programmes": programme_count,
        "compatibility_ids_and_name_aliases": compatibility_alias_count,
        "ambiguous_compatibility_aliases_skipped": sum(len(v) for v in ambiguous_old_ids.values()),
        "selection_changed_by_consensus": sum(
            1 for row in report_rows if row.get("changed_from_first_available")
        ),
        "validation_ok": sum(1 for item in validations if item["status"] == "ok"),
        "validation_attention": sum(1 for item in validations if item["status"] == "atencao"),
        "validation_suspicious": sum(1 for item in validations if item["status"] == "suspeito"),
        "timing_seconds": timings,
        "selected_by_source": source_counts,
    }

    (OUTPUT_DIR / "report.json").write_text(
        json.dumps({"summary": counts, "source_errors": source_errors, "channels": report_rows}, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    (OUTPUT_DIR / "validation.json").write_text(
        json.dumps({
            "generated_at": now.isoformat(),
            "active_sources": [source.label for source in active_sources],
            "source_errors": source_errors,
            "summary": {
                "ok": counts["validation_ok"],
                "attention": counts["validation_attention"],
                "suspicious": counts["validation_suspicious"],
            },
            "channels": validations,
        }, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )

    report_md = [
        "# Relatório do EPG universal — identificação por nome + consenso multi-fonte",
        "",
        f"- Canais identificados pelo nome: **{counts['playlist_channels']}**",
        f"- Encontrados: **{counts['matched']}**",
        f"- Sem EPG seguro: **{counts['missing']}**",
        f"- Canais cuja fonte mudou por consenso: **{counts['selection_changed_by_consensus']}**",
        f"- Programas gravados: **{counts['programmes']}**",
        f"- IDs/aliases de compatibilidade publicados automaticamente: **{counts['compatibility_ids_and_name_aliases']}**",
        f"- IDs/aliases ambíguos ignorados: **{counts['ambiguous_compatibility_aliases_skipped']}**",
        "",
        "## Fonte escolhida por canal",
        "",
    ]
    for source in active_sources:
        report_md.append(f"- **{source.label}:** {source_counts[source.label]} canal(is)")
    if source_errors:
        report_md.extend(["", "## Fontes indisponíveis nesta execução", ""])
        report_md.extend(f"- ⚠️ **{label}:** {error}" for label, error in source_errors.items())

    report_md.extend(["", "## Desempenho da execução", ""])
    for label, seconds in timings.items():
        report_md.append(f"- **{label.replace('_', ' ').title()}:** {seconds:.2f} s")
    report_md.append(f"- **Total até gerar os arquivos principais:** {time.perf_counter() - run_start:.2f} s")

    switched_rows = [row for row in report_rows if row.get("changed_from_first_available")]
    report_md.extend(["", "## Fonte alterada automaticamente por consenso", ""])
    if switched_rows:
        for row in switched_rows:
            report_md.append(
                f"- **{row['name']}**: {row['first_available_source']} → **{row['source']}** · "
                f"votos {row['support_votes']}/{row['vote_sources']} · "
                f"peso {row['support_weight']:.1f}/{row['vote_weight']:.1f} · confiança {row['confidence']}%"
            )
    else:
        report_md.append("Nenhum canal precisou trocar a primeira fonte disponível.")

    if ambiguous_old_ids:
        report_md.extend(["", "## tvg-id antigos ambíguos ignorados", ""])
        report_md.append("Estes IDs apareciam em mais de um canal diferente. O script não os reutilizou para evitar EPG errado; a `playlist-fixed.m3u` automática resolve esses casos.")
        for entry in entries:
            tid = str(entry.get("id", ""))
            for old_id in ambiguous_old_ids.get(tid, []):
                report_md.append(f"- **{entry.get('name', tid)}** — `{old_id}`")

    missing_rows = [row for row in report_rows if row["status"] == "missing"]
    report_md.extend(["", "## Sem EPG", ""])
    report_md.extend(f"- `{row['target_id']}` — {row['name']}" for row in missing_rows)
    if not missing_rows:
        report_md.append("Todos os canais foram encontrados em pelo menos uma fonte.")

    fuzzy_rows = [row for row in report_rows if str(row.get("method", "")).startswith("aproximação")]
    report_md.extend(["", "## Correspondências aproximadas", ""])
    report_md.extend(
        f"- **{row['name']}** → `{row['source_channel_id']}` em {row['source']} ({row['method']})"
        for row in fuzzy_rows
    )
    if not fuzzy_rows:
        report_md.append("Nenhuma aproximação foi necessária.")
    (OUTPUT_DIR / "report.md").write_text("\n".join(report_md) + "\n", "utf-8")

    validation_md = [
        "# Validação e consenso da programação",
        "",
        "> A identidade do canal vem primeiro do **nome da M3U**. O `tvg-id` antigo só é usado como pista quando o nome do EPG confirma a mesma identidade. Depois, a grade com maior apoio independente é escolhida.",
        "",
        "> BR1 e BR2 aparecem como dois votos visíveis, porém juntos valem no máximo o peso de uma família EPGShare no cálculo decisivo. Isso reduz o risco de duplicar influência do mesmo provedor.",
        "",
        f"Gerado em: **{now.astimezone(timezone(timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')} (Maceió)**",
        f"Fontes ativas: **{', '.join(source.label for source in active_sources)}**",
        "",
        f"- ✅ OK: **{counts['validation_ok']}**",
        f"- ⚠️ Atenção: **{counts['validation_attention']}**",
        f"- 🚨 Suspeito: **{counts['validation_suspicious']}**",
        "",
        "## Canais que merecem conferência",
        "",
    ]
    flagged = [item for item in validations if item["status"] != "ok"]
    flagged.sort(key=lambda item: (0 if item["status"] == "suspeito" else 1, str(item["name"])))
    for item in flagged:
        icon = "🚨" if item["status"] == "suspeito" else "⚠️"
        current = item.get("now") or {}
        nxt = item.get("next") or {}
        consensus = item["consensus"]
        validation_md.extend([
            f"### {icon} {item['name']} — {item['source']} / `{item['source_channel_id']}`",
            "",
            f"- **Agora:** {current.get('title', '—')} ({fmt_local_time(current.get('start'))}–{fmt_local_time(current.get('stop'))})",
            f"- **Próximo:** {nxt.get('title', '—')} ({fmt_local_time(nxt.get('start'))}–{fmt_local_time(nxt.get('stop'))})",
            f"- **Votos visíveis:** {consensus['supporting_votes']}/{consensus['vote_sources']}",
            f"- **Peso independente:** {consensus['support_weight']:.1f}/{consensus['vote_weight']:.1f} · famílias apoiando: {consensus['support_families']}",
            f"- **Confiança:** {item['confidence']}% · saúde da grade: {item['health_score']*100:.0f}%",
        ])
        for comp in item.get("cross_sources", []):
            agreement = comp.get("agreement")
            if comp.get("vote") == "concorda":
                marker = "✅"
            elif comp.get("vote") == "diverge":
                marker = "❌"
            else:
                marker = "➖"
            if isinstance(agreement, float):
                validation_md.append(
                    f"- {marker} **{comp['other_source']}** `{comp['other_channel_id']}` — "
                    f"{agreement*100:.0f}% de concordância em {comp['comparable']} programas"
                )
            else:
                validation_md.append(
                    f"- {marker} **{comp['other_source']}** `{comp['other_channel_id']}` — sem dados suficientes para votar"
                )
        for warning in item.get("warnings", []):
            validation_md.append(f"- **Alerta:** {warning}")
        validation_md.append("")
    if not flagged:
        validation_md.extend(["Nenhum alerta automático nesta execução.", ""])

    validation_md.extend(["## Agora / Próximo — todos os canais", ""])
    for item in sorted(validations, key=lambda x: str(x["name"])):
        current = item.get("now") or {}
        nxt = item.get("next") or {}
        icon = "✅" if item["status"] == "ok" else ("🚨" if item["status"] == "suspeito" else "⚠️")
        consensus = item["consensus"]
        validation_md.append(
            f"- {icon} **{item['name']}** ({item['source']}): Agora **{current.get('title', '—')}** → "
            f"Próximo **{nxt.get('title', '—')}** · votos **{consensus['supporting_votes']}/{consensus['vote_sources']}** · "
            f"peso **{consensus['support_weight']:.1f}/{consensus['vote_weight']:.1f}** · confiança **{item['confidence']}%**"
        )
    (OUTPUT_DIR / "validation.md").write_text("\n".join(validation_md) + "\n", "utf-8")

    timings["total"] = round(time.perf_counter() - run_start, 3)
    counts["timing_seconds"] = timings
    # Regrava JSON final para incluir o tempo total, sem alterar o conteúdo funcional.
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps({"summary": counts, "source_errors": source_errors, "channels": report_rows}, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
