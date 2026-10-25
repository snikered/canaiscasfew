#!/usr/bin/env python3
"""EPG universal multi-fonte com seleção automática por consenso por canal,
normalização temporal estrita (UTC), correção de fuso/timeshift e anti-sobreposição.

Fontes padrão (ordem de preferência/desempate):
1. Genius / Curated
2. Open-EPG Brazil 4
3. EPGShare BR1
4. IPTV-EPG BR
5. EPGShare BR2
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

# Compatibilidade com IDs legados
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
    default_offset: str = "+0000"  # Offset padrão caso o feed omita fuso (+0000 ou -0300)
    timeshift_hours: float = 0.0   # Deslocamento horário fixo para a fonte


SOURCE_SPECS = [
    SourceSpec(
        "primary", "Genius",
        os.environ.get(
            "PRIMARY_EPG_URL",
            "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz",
        ),
        1, "genius", 1.0,
        default_offset=os.environ.get("TIMESHIFT_PRIMARY_OFFSET", "+0000"),
        timeshift_hours=float(os.environ.get("TIMESHIFT_PRIMARY_HOURS", "0.0")),
    ),
    SourceSpec(
        "secondary", "Open-EPG Brazil 4",
        os.environ.get("SECONDARY_EPG_URL", "https://www.open-epg.com/files/brazil4.xml"),
        2, "open-epg", 1.0,
        default_offset=os.environ.get("TIMESHIFT_SECONDARY_OFFSET", "+0000"),
        timeshift_hours=float(os.environ.get("TIMESHIFT_SECONDARY_HOURS", "0.0")),
    ),
    SourceSpec(
        "tertiary", "EPGShare BR1",
        os.environ.get(
            "TERTIARY_EPG_URL",
            "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
        ),
        3, "epgshare", 0.5,
        default_offset=os.environ.get("TIMESHIFT_TERTIARY_OFFSET", "+0000"),
        timeshift_hours=float(os.environ.get("TIMESHIFT_TERTIARY_HOURS", "0.0")),
    ),
    SourceSpec(
        "quaternary", "IPTV-EPG BR",
        os.environ.get("QUATERNARY_EPG_URL", "https://iptv-epg.org/files/epg-br.xml"),
        4, "iptv-epg", 1.0,
        default_offset=os.environ.get("TIMESHIFT_QUATERNARY_OFFSET", "+0000"),
        timeshift_hours=float(os.environ.get("TIMESHIFT_QUATERNARY_HOURS", "0.0")),
    ),
    SourceSpec(
        "quinary", "EPGShare BR2",
        os.environ.get(
            "QUINARY_EPG_URL",
            "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz",
        ),
        5, "epgshare", 0.5,
        default_offset=os.environ.get("TIMESHIFT_QUINARY_OFFSET", "+0000"),
        timeshift_hours=float(os.environ.get("TIMESHIFT_QUINARY_HOURS", "0.0")),
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
    names: dict[str, str]
    normalized_names: dict[str, str]
    name_index: dict[str, set[str]]
    normalized_id_index: dict[str, set[str]]
    programs: dict[str, list[ET.Element]]
    rows: dict[str, list[ProgrammeRow]]

    @property
    def label(self) -> str:
        return self.spec.label


@dataclass(frozen=True)
class ChannelMatch:
    source: EpgSource
    channel_id: str
    method: str
    match_score: float


@dataclass(frozen=True)
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


def local_tag(element: ET.Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def download_source(spec: SourceSpec) -> tuple[SourceSpec, bytes]:
    req = urllib.request.Request(
        spec.url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; EpgUniversal/2.0)"},
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        content = response.read()
    return spec, content


def parse_xmltv_datetime(value: str, default_offset: str = "+0000") -> datetime | None:
    """Converte datas XMLTV para UTC absoluto, aplicando fuso padrão caso omitido."""
    if not value:
        return None
    value = value.strip()

    match = re.match(r"^(\d{14}|\d{12})(?:\s*([+-]\d{4}))?(?:\s+.*)?$", value)
    if match:
        digits, offset = match.groups()
        try:
            second = int(digits[12:14]) if len(digits) == 14 else 0
            if not offset:
                offset = default_offset or "+0000"

            sign = 1 if offset[0] == "+" else -1
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
            tz = timezone(sign * delta)

            dt = datetime(
                int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
                int(digits[8:10]), int(digits[10:12]), second, tzinfo=tz,
            )
            return dt.astimezone(timezone.utc)
        except (ValueError, IndexError):
            pass

    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            pass

    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            sign = 1 if default_offset[0] == "+" else -1
            delta = timedelta(hours=int(default_offset[1:3]), minutes=int(default_offset[3:5]))
            tz = timezone(sign * delta)
            return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except (ValueError, IndexError):
            pass
    return None


def format_xmltv_datetime(dt: datetime) -> str:
    """Exporta estritamente no padrão universal XMLTV UTC: YYYYMMDDHHMMSS +0000."""
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y%m%d%H%M%S +0000")


def parse_epg_xml(spec: SourceSpec, payload: bytes, overrides_data: dict[str, object] | None = None) -> EpgSource:
    source_shifts = (overrides_data or {}).get("source_timeshifts", {})
    extra_shift_hours = float(source_shifts.get(spec.key, spec.timeshift_hours))
    total_delta = timedelta(hours=extra_shift_hours)

    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)

    root = ET.fromstring(payload)
    channels: dict[str, ET.Element] = {}
    names: dict[str, str] = {}
    normalized_names: dict[str, str] = {}
    name_index: dict[str, set[str]] = defaultdict(set)
    normalized_id_index: dict[str, set[str]] = defaultdict(set)
    programs: dict[str, list[ET.Element]] = defaultdict(list)
    rows: dict[str, list[ProgrammeRow]] = defaultdict(list)

    for child in root:
        tag = local_tag(child)
        if tag == "channel":
            channel_id = child.attrib.get("id", "").strip()
            if not channel_id:
                continue
            channels[channel_id] = child
            display_name = ""
            for sub in child:
                if local_tag(sub) == "display-name" and (sub.text or "").strip():
                    display_name = sub.text.strip()
                    break
            names[channel_id] = display_name
            norm_name = normalize_name(display_name)
            normalized_names[channel_id] = norm_name
            if norm_name:
                name_index[norm_name].add(channel_id)
            normalized_id = normalize_source_id(channel_id)
            if normalized_id:
                normalized_id_index[normalized_id].add(channel_id)

        elif tag == "programme":
            channel_id = child.attrib.get("channel", "").strip()
            if not channel_id:
                continue

            programs[channel_id].append(child)

            start = parse_xmltv_datetime(child.attrib.get("start", ""), spec.default_offset)
            stop = parse_xmltv_datetime(child.attrib.get("stop", ""), spec.default_offset)
            if start is None:
                continue

            if total_delta:
                start += total_delta
                if stop is not None:
                    stop += total_delta

            title = programme_title(child)
            rows[channel_id].append(ProgrammeRow(
                start=start,
                stop=stop or start,
                title=title,
                normalized_title=normalize_programme_title(title),
                element=child,
            ))

    # Preenche lacunas de stop e ordena cronologicamente
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

    root.clear()
    return EpgSource(
        spec=spec,
        channels=channels,
        names=names,
        normalized_names=normalized_names,
        name_index=name_index,
        normalized_id_index=normalized_id_index,
        programs=programs,
        rows=rows,
    )


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


def programme_rows(source: EpgSource, channel_id: str) -> list[ProgrammeRow]:
    return source.rows.get(channel_id, [])


def now_next_for(source: EpgSource, channel_id: str, now: datetime) -> tuple[dict | None, dict | None]:
    current = None
    next_item = None
    rows = programme_rows(source, channel_id)
    for idx, row in enumerate(rows):
        if row.start <= now < row.stop:
            current = {"title": row.title, "start": row.start, "stop": row.stop}
            if idx + 1 < len(rows):
                nxt = rows[idx + 1]
                next_item = {"title": nxt.title, "start": nxt.start, "stop": nxt.stop}
            break
        elif row.start > now and next_item is None:
            next_item = {"title": row.title, "start": row.start, "stop": row.stop}
            break
    return current, next_item


_AGREEMENT_CACHE: dict[tuple[str, str, str, str], dict[str, object]] = {}
_HEALTH_CACHE: dict[tuple[str, str], dict[str, object]] = {}


def _calculate_overlap_agreement(a_rows: list[ProgrammeRow], b_rows: list[ProgrammeRow], time_offset: timedelta = timedelta(0)) -> tuple[int, float, float]:
    """Calcula concordância de títulos com possível compensação de deslocamento de fuso."""
    if not a_rows or not b_rows:
        return 0, 0.0, 0.0

    similarities: list[float] = []
    b_floor = 0
    tolerance = timedelta(minutes=25)

    for a_row in a_rows:
        target_start = a_row.start + time_offset
        target_stop = a_row.stop + time_offset

        while b_floor < len(b_rows) and b_rows[b_floor].stop < target_start - tolerance:
            b_floor += 1

        best = None
        idx = b_floor
        while idx < len(b_rows):
            b_row = b_rows[idx]
            if b_row.start > target_stop + tolerance:
                break
            overlap = max(0.0, (min(target_stop, b_row.stop) - max(target_start, b_row.start)).total_seconds())
            shorter = max(1.0, min((target_stop - target_start).total_seconds(), (b_row.stop - b_row.start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_row.start - target_start).total_seconds())

            if overlap_ratio >= 0.30 or start_delta <= 25 * 60:
                time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (25 * 60)))
                if a_row.normalized_title and b_row.normalized_title:
                    if a_row.normalized_title == b_row.normalized_title:
                        title_score = 1.0
                    else:
                        title_score = SequenceMatcher(None, a_row.normalized_title, b_row.normalized_title).ratio()
                else:
                    title_score = 0.0
                cand = (time_score, title_score)
                if best is None or cand > best:
                    best = cand
            idx += 1
        if best is not None:
            similarities.append(best[1])

    if not similarities:
        return 0, 0.0, 0.0
    agreed = sum(1 for s in similarities if s >= 0.68)
    return len(similarities), round(agreed / len(similarities), 4), round(mean(similarities), 4)


def cross_source_programme_agreement(
    a_source: EpgSource,
    a_id: str,
    b_source: EpgSource,
    b_id: str,
    now: datetime,
) -> dict[str, object]:
    """Compara concordância e testa automaticamente divergências de fuso (+/- 3h, +/- 2h, etc)."""
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
        result = {"comparable": 0, "agreement": None, "average_similarity": None, "detected_shift_hours": 0}
        _AGREEMENT_CACHE[cache_key] = result
        return result

    # 1. Teste no alinhamento direto
    comp, agr, sim = _calculate_overlap_agreement(a_rows, b_rows, timedelta(0))
    detected_shift = 0

    # 2. Heurística de auto-detecção de divergência de fuso se não houver acordo direto
    if agr < AGREEMENT_THRESHOLD and len(a_rows) >= 3 and len(b_rows) >= 3:
        for offset_h in (3, -3, 1, -1, 2, -2, 4, -4):
            c_test, a_test, s_test = _calculate_overlap_agreement(a_rows, b_rows, timedelta(hours=offset_h))
            if c_test >= MIN_COMPARABLE_PROGRAMMES and a_test >= 0.70 and a_test > agr:
                comp, agr, sim = c_test, a_test, s_test
                detected_shift = offset_h
                break

    if comp == 0:
        result = {"comparable": 0, "agreement": None, "average_similarity": None, "detected_shift_hours": 0}
    else:
        result = {
            "comparable": comp,
            "agreement": agr,
            "average_similarity": sim,
            "detected_shift_hours": detected_shift,
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


def find_candidates_for_channel(
    entry: dict[str, object],
    sources: list[EpgSource],
) -> list[ChannelMatch]:
    matches: list[ChannelMatch] = []
    canonical_key = str(entry.get("canonical_key", "")).strip()
    name = str(entry.get("name", "")).strip()
    norm_name = normalize_name(name)
    aliases = [str(x) for x in entry.get("aliases", []) or []]
    old_tvg_ids = [str(x) for x in entry.get("old_tvg_ids", []) or []]

    target_digits = digit_signature(name)

    for source in sources:
        found_id: str | None = None
        method = ""
        score = 0.0

        if canonical_key and canonical_key in source.name_index:
            found_id = next(iter(source.name_index[canonical_key]))
            method = "canonical_key"
            score = 1.0

        if not found_id and norm_name and norm_name in source.name_index:
            found_id = next(iter(source.name_index[norm_name]))
            method = "exact_name"
            score = 1.0

        if not found_id:
            for alias in aliases:
                norm_alias = normalize_name(alias)
                if norm_alias in source.name_index:
                    found_id = next(iter(source.name_index[norm_alias]))
                    method = "alias_match"
                    score = 0.98
                    break

        if not found_id:
            for tvg_id in old_tvg_ids:
                if tvg_id in source.channels:
                    ch_name = source.names.get(tvg_id, "")
                    if normalize_name(ch_name) == norm_name or SequenceMatcher(None, normalize_name(ch_name), norm_name).ratio() >= TVG_ID_NAME_THRESHOLD:
                        found_id = tvg_id
                        method = "tvg_id_verified"
                        score = 0.95
                        break

        if not found_id:
            best_id, best_score = None, 0.0
            for cand_norm, ch_ids in source.name_index.items():
                if digit_signature(cand_norm) != target_digits:
                    continue
                ratio = SequenceMatcher(None, norm_name, cand_norm).ratio()
                if ratio > best_score:
                    best_score = ratio
                    best_id = next(iter(ch_ids))
            if best_id and best_score >= FUZZY_THRESHOLD:
                found_id = best_id
                method = "fuzzy_name"
                score = round(best_score, 3)

        if found_id:
            matches.append(ChannelMatch(source, found_id, method, score))

    return matches


def evaluate_channel_matches(
    entry: dict[str, object],
    matches: list[ChannelMatch],
    now: datetime,
) -> tuple[CandidateEvaluation, list[CandidateEvaluation]]:
    health_by_source = {}
    for match in matches:
        health_by_source[match.source.spec.key] = schedule_health(match.source, match.channel_id, now)

    pairwise: dict[tuple[str, str], dict[str, object]] = {}
    for a in matches:
        for b in matches:
            if a.source is b.source:
                continue
            pair_key = (a.source.spec.key, b.source.spec.key)
            if pair_key not in pairwise:
                res = cross_source_programme_agreement(a.source, a.channel_id, b.source, b.channel_id, now)
                pairwise[pair_key] = res

    evaluations: list[CandidateEvaluation] = []
    max_active_vote_weight = sum(m.source.spec.vote_weight for m in matches)

    for match in matches:
        supporters: list[EpgSource] = [match.source]
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
                "detected_shift_hours": result.get("detected_shift_hours", 0),
                "vote": vote,
            })

        support_families = len({src.spec.family for src in supporters})
        weighted_ratio = support_weight / vote_weight if vote_weight else 0.0
        avg_support = mean(support_agreements) if support_agreements else 0.0
        health_score = float(health_by_source[match.source.spec.key]["score"])

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

    def rank(ev: CandidateEvaluation) -> tuple[int, float, int, float, float, float, float, int]:
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


def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None, float]:
    """Retorna (source_key, channel_id, timeshift_hours) definidos manualmente."""
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if isinstance(item, dict):
        shift = float(item.get("timeshift_hours", 0.0))
        return item.get("source"), item.get("channel_id"), shift
    return None, None, 0.0


def add_display_name_first(channel: ET.Element, name: str) -> None:
    if not name:
        return
    for child in channel:
        if local_tag(child) == "display-name" and (child.text or "").strip() == name:
            return
    display = ET.Element("display-name")
    display.text = name
    channel.insert(0, display)


def sanitize_and_prepare_programmes(
    rows: list[ProgrammeRow],
    target_channel_id: str,
    channel_timeshift_hours: float,
    cutoff: datetime,
) -> list[ET.Element]:
    """
    Higieniza bordas temporais:
    1. Aplica timeshift específico do canal (se houver).
    2. Garante fim pelo início do próximo (anti-lacunas / anti-stop-vazio).
    3. Trunca sobreposições: se prog[i].stop > prog[i+1].start, ajusta stop = prog[i+1].start.
    4. Atualiza os atributos XML 'start' e 'stop' estritamente em UTC '+0000'.
    """
    if not rows:
        return []

    shift_delta = timedelta(hours=channel_timeshift_hours)
    adjusted_items: list[dict] = []

    for r in rows:
        st = r.start + shift_delta
        sp = r.stop + shift_delta
        adjusted_items.append({"start": st, "stop": sp, "element": r.element})

    adjusted_items.sort(key=lambda x: x["start"])
    final_elements: list[ET.Element] = []

    for idx, item in enumerate(adjusted_items):
        st = item["start"]
        sp = item["stop"]

        if sp <= st:
            if idx + 1 < len(adjusted_items):
                sp = adjusted_items[idx + 1]["start"]
            else:
                sp = st + timedelta(hours=2)

        # Anti-sobreposição com o próximo programa
        if idx + 1 < len(adjusted_items):
            nxt_st = adjusted_items[idx + 1]["start"]
            if sp > nxt_st:
                sp = nxt_st

        # Descarta programas anteriores à janela de retenção
        if sp < cutoff:
            continue

        el = copy.deepcopy(item["element"])
        el.attrib["channel"] = target_channel_id
        el.attrib["start"] = format_xmltv_datetime(st)
        el.attrib["stop"] = format_xmltv_datetime(sp)
        final_elements.append(el)

    return final_elements


def build_compatibility_ids(entries: list[dict[str, object]]) -> tuple[dict[str, list[str]], set[str]]:
    compatibility_ids: dict[str, list[str]] = {}
    ambiguous_old_ids: set[str] = set()
    old_id_to_targets: dict[str, set[str]] = defaultdict(set)

    for entry in entries:
        target_id = str(entry["id"])
        candidates = [target_id]
        candidates.extend(LEGACY_INTERNAL_IDS.get(target_id, ()))
        for old_id in entry.get("old_tvg_ids", []) or []:
            old_id_to_targets[str(old_id)].add(target_id)
            candidates.append(str(old_id))
        compatibility_ids[target_id] = list(dict.fromkeys(candidates))

    for old_id, targets in old_id_to_targets.items():
        if len(targets) > 1:
            ambiguous_old_ids.add(old_id)

    # Remove tvg-ids ambíguos para evitar colisão entre canais distintos
    for target_id, cands in compatibility_ids.items():
        compatibility_ids[target_id] = [c for c in cands if c == target_id or c not in ambiguous_old_ids]

    return compatibility_ids, ambiguous_old_ids


def write_combined_epg(
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]],
    compatibility_ids: dict[str, list[str]],
    overrides_data: dict[str, object],
    now: datetime,
) -> tuple[int, int]:
    gzip_path = OUTPUT_DIR / "epg.xml.gz"
    plain_path = OUTPUT_DIR / "epg.xml"
    plain = plain_path.open("wb") if WRITE_PLAIN_XML else None
    cutoff = now - timedelta(hours=OUTPUT_PAST_HOURS)

    channel_timeshifts = (overrides_data or {}).get("channel_timeshifts", {})
    programme_count = 0
    compatibility_alias_count = 0

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(
            filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL,
        ) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None:
                    plain.write(data)

            generator = "EPG universal otimizado por consenso e normalização UTC"
            source_name = "Seleção canal a canal por consenso + anti-sobreposição de grade"
            header = (
                "<?xml version='1.0' encoding='utf-8'?>\n"
                f"<tv generator-info-name={quoteattr(generator)} source-info-name={quoteattr(source_name)}>\n"
            ).encode("utf-8")
            emit(header)

            # 1. Canais
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

            # 2. Programação normalizada, sem sobreposição e com timeshift ajustado
            for entry, chosen, _evaluations, _forced in selected:
                target_id = str(entry["id"])
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                ch_rows = programme_rows(chosen.match.source, chosen.match.channel_id)

                _ov_src, _ov_id, ch_shift = manual_override(overrides_data, target_id)
                if not ch_shift:
                    ch_shift = float(channel_timeshifts.get(target_id, 0.0))

                for output_id in ids_for_channel:
                    sanitized_elements = sanitize_and_prepare_programmes(
                        rows=ch_rows,
                        target_channel_id=output_id,
                        channel_timeshift_hours=ch_shift,
                        cutoff=cutoff,
                    )
                    for prog_el in sanitized_elements:
                        emit(ET.tostring(prog_el, encoding="utf-8"))
                        emit(b"\n")
                        programme_count += 1

            emit(b"</tv>\n")

    if plain is not None:
        plain.close()
    elif plain_path.exists():
        plain_path.unlink()
    return programme_count, compatibility_alias_count


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
        warnings.append(f"{health['overlaps']} sobreposições na fonte original (higienizadas na exportação)")

    first_by_priority = min(all_evaluations, key=lambda ev: ev.match.source.spec.priority)
    switched = first_by_priority.match.source is not chosen.match.source
    if switched:
        warnings.append(
            f"fonte alterada de {first_by_priority.match.source.label} "
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
        "future_hours": health["future_hours"],
        "health_score": health["score"],
        "consensus": {
            "supporting_votes": chosen.support_votes,
            "vote_sources": chosen.vote_sources,
            "support_weight": chosen.support_weight,
            "vote_weight": chosen.vote_weight,
            "support_families": chosen.support_families,
            "weighted_ratio": chosen.weighted_ratio,
        },
    }


def main() -> None:
    run_start = time.perf_counter()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    catalog_data = json.loads(CATALOG_PATH.read_text("utf-8"))
    entries = catalog_data.get("channels", [])
    overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}
    compatibility_ids, _ambiguous = build_compatibility_ids(entries)

    print(f"Baixando {len(SOURCE_SPECS)} fontes de EPG em paralelo...")
    sources: list[EpgSource] = []
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(download_source, spec) for spec in SOURCE_SPECS]
        for f in as_completed(futures):
            spec, payload = f.result()
            epg_src = parse_epg_xml(spec, payload, overrides)
            sources.append(epg_src)
            print(f" -> [{spec.label}] {len(epg_src.channels)} canais, {len(epg_src.rows)} grades carregadas.")

    sources.sort(key=lambda s: s.spec.priority)
    source_by_key = {s.spec.key: s.spec for s in SOURCE_SPECS}
    now = datetime.now(timezone.utc)

    selected = []
    validations = []

    for entry in entries:
        target_id = str(entry["id"])
        matches = find_candidates_for_channel(entry, sources)

        ov_src_key, ov_chan_id, _ch_shift = manual_override(overrides, target_id)
        forced = False
        if ov_src_key and ov_chan_id:
            src_obj = next((s for s in sources if s.spec.key == ov_src_key), None)
            if src_obj and ov_chan_id in src_obj.channels:
                matches = [ChannelMatch(src_obj, ov_chan_id, "manual_override", 1.0)]
                forced = True

        if not matches:
            continue

        chosen, all_evs = evaluate_channel_matches(entry, matches, now)
        selected.append((entry, chosen, all_evs, forced))
        validations.append(make_validation(entry, chosen, all_evs, now))

    print(f"Escrevendo EPG combinado e normalizado...")
    prog_count, alias_count = write_combined_epg(selected, compatibility_ids, overrides, now)

    # Exporta validação
    (OUTPUT_DIR / "validation.json").write_text(json.dumps(validations, indent=2, ensure_ascii=False), "utf-8")
    
    elapsed = time.perf_counter() - run_start
    print(f"Concluído em {elapsed:.1f}s: {len(selected)} canais indexados, {prog_count} programas gerados.")


if __name__ == "__main__":
    main()
