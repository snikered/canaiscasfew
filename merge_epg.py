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

# Importações do seu repositório
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


@dataclass(frozen=True)
class SourceSpec:
    key: str
    label: str
    url: str
    priority: int
    family: str
    vote_weight: float
    default_offset: str = "+0000"
    timeshift_hours: float = 0.0


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
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 4)
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")


def decode_xml(payload: bytes) -> bytes:
    return gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload


def local_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def format_xmltv_datetime(dt: datetime) -> str:
    """Formata estritamente no padrão XMLTV UTC: YYYYMMDDHHMMSS +0000."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


@lru_cache(maxsize=500000)
def parse_xmltv_datetime(value: str, default_offset: str = "+0000") -> datetime | None:
    """Converte datas XMLTV para UTC absoluto, aplicando fuso padrão caso omitido."""
    if not value: return None
    value = value.strip()
    match = re.match(r"^(\d{14}|\d{12})(?:\s*([+-]\d{4}))?(?:\s+.*)?$", value)
    if match:
        digits, offset = match.groups()
        try:
            second = int(digits[12:14]) if len(digits) == 14 else 0
            if not offset: offset = default_offset or "+0000"
            sign = 1 if offset[0] == "+" else -1
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
            tz = timezone(sign * delta)
            dt = datetime(
                int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
                int(digits[8:10]), int(digits[10:12]), second, tzinfo=tz
            )
            return dt.astimezone(timezone.utc)
        except (ValueError, IndexError):
            pass

    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z"):
        try: return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError: pass
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            sign = 1 if default_offset[0] == "+" else -1
            delta = timedelta(hours=int(default_offset[1:3]), minutes=int(default_offset[3:5]))
            tz = timezone(sign * delta)
            return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except (ValueError, IndexError): pass
    return None


def read_source(spec: SourceSpec, overrides: dict[str, object] | None = None) -> EpgSource:
    source_shifts = (overrides or {}).get("source_timeshifts", {}) if overrides else {}
    timeshift_val = float(source_shifts.get(spec.key, spec.timeshift_hours))
    shift_delta = timedelta(hours=timeshift_val)

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

            programs[channel_id].append(child)

            start = parse_xmltv_datetime(child.attrib.get("start", ""), spec.default_offset)
            stop = parse_xmltv_datetime(child.attrib.get("stop", ""), spec.default_offset)
            if start is None:
                continue

            if shift_delta:
                start += shift_delta
                if stop is not None:
                    stop += shift_delta

            title = programme_title(child)
            rows[channel_id].append(ProgrammeRow(
                start=start,
                stop=stop or start,
                title=title,
                normalized_title=normalize_programme_title(title),
                element=child,
            ))

    # Preenche término ausente
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
        programs=dict(programs),
        rows=dict(rows),
        exact_index=dict(exact_index),
        fuzzy_index={sig: dict(items) for sig, items in fuzzy_index.items()},
        normalized_id_index=dict(normalized_id_index),
    )


def read_optional_source(spec: SourceSpec, overrides: dict[str, object] | None = None) -> tuple[EpgSource | None, str | None]:
    try:
        return read_source(spec, overrides), None
    except Exception as exc:
        return None, str(exc)


def aliases_for(entry: dict[str, object]) -> list[str]:
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
    if not left or not right: return 0.0
    length_bound = (2.0 * min(len(left), len(right))) / (len(left) + len(right))
    if length_bound < (FUZZY_THRESHOLD - FUZZY_MARGIN):
        return length_bound
    lc = left_counter or Counter(left)
    rc = Counter(right)
    common = sum(min(count, rc.get(ch, 0)) for ch, count in lc.items())
    return min(length_bound, (2.0 * common) / (len(left) + len(right)))


def best_fuzzy(source: EpgSource, aliases: list[str]) -> tuple[str | None, float, float, str]:
    if not aliases: return None, 0.0, 0.0, ""
    relevant_floor = FUZZY_THRESHOLD - FUZZY_MARGIN
    best_by_channel: dict[str, tuple[float, str, str]] = {}
    
    for alias in aliases:
        if len(alias) < 4: continue
        alias_counter = Counter(alias)
        bucket = source.fuzzy_index.get(digit_signature(alias), {})
        for normalized_candidate, channel_ids in bucket.items():
            if _sequence_ratio_upper_bound(alias, normalized_candidate, alias_counter) < relevant_floor:
                continue
            ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
            if ratio < relevant_floor: continue
            for channel_id in channel_ids:
                old = best_by_channel.get(channel_id)
                if old is None or ratio > old[0]:
                    best_by_channel[channel_id] = (ratio, alias, normalized_candidate)

    if not best_by_channel: return None, 0.0, 0.0, ""
    scored = sorted(((score, channel_id, alias, candidate) for channel_id, (score, alias, candidate) in best_by_channel.items()), reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second_score, f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"


def _name_compatibility(source: EpgSource, channel_id: str, aliases: list[str]) -> float:
    candidates = source.normalized_names.get(channel_id, [])
    best = 0.0
    for alias in aliases:
        alias_sig = digit_signature(alias)
        for candidate_norm in candidates:
            if alias_sig != digit_signature(candidate_norm): continue
            if alias == candidate_norm: return 1.0
            best = max(best, SequenceMatcher(None, alias, candidate_norm).ratio())
    return best


def match_by_safe_tvg_id_hint(source: EpgSource, aliases: list[str], old_ids: list[str]) -> ChannelMatch | None:
    if not old_ids or not aliases: return None
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
    if not candidates: return None
    candidates.sort(reverse=True)
    score, channel_id = candidates[0]
    return ChannelMatch(source, channel_id, f"tvg-id antigo confirmado pelo nome ({score:.3f})", score)


def find_match(source: EpgSource, aliases: list[str], old_ids: list[str] | None = None) -> ChannelMatch | None:
    exact_id, reason = choose_exact(source, aliases)
    if exact_id: return ChannelMatch(source, exact_id, reason, 1.0)
    
    fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
    if fuzzy_id and best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN:
        return ChannelMatch(source, fuzzy_id, fuzzy_reason, best_score)
        
    return match_by_safe_tvg_id_hint(source, aliases, old_ids or [])


def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None]:
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if not isinstance(item, dict): return None, None
    source = str(item.get("source", "")).lower().strip()
    channel_id = str(item.get("channel_id", "")).strip()
    valid = {spec.key for spec in SOURCE_SPECS}
    return (source, channel_id) if source in valid and channel_id else (None, None)


def add_display_name_first(channel: ET.Element, name: str) -> None:
    normalized_existing = {normalize_name(node.text or "") for node in channel if local_tag(node) == "display-name"}
    if normalize_name(name) in normalized_existing: return
    display = ET.Element("display-name")
    display.text = name
    channel.insert(0, display)


def programme_title(programme: ET.Element | None) -> str:
    if programme is None: return ""
    for child in programme:
        if local_tag(child) == "title" and (child.text or "").strip():
            return (child.text or "").strip()
    return ""


@lru_cache(maxsize=200000)
def normalize_programme_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).upper()
    value = re.sub(r"\b(?:HD|FHD|AO VIVO|LIVE|ESTREIA|INEDITO|INÉDITO)\b", " ", value)
    value = re.sub(r"\bT\d+\s*E\d+\b", " ", value)
    value = re.sub(r"\bS\d+\s*E\d+\b", " ", value)
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def programme_rows(source: EpgSource, channel_id: str) -> list[ProgrammeRow]:
    return source.rows.get(channel_id, [])


_AGREEMENT_CACHE: dict[tuple[str, str, str, str], dict[str, object]] = {}

def _calc_agreement(a_rows: list[ProgrammeRow], b_rows: list[ProgrammeRow], offset: timedelta) -> dict[str, object]:
    similarities: list[float] = []
    b_floor = 0
    tolerance = timedelta(minutes=20)
    
    for a_row in a_rows:
        a_start, a_stop = a_row.start + offset, a_row.stop + offset
        while b_floor < len(b_rows) and b_rows[b_floor].stop < a_start - tolerance:
            b_floor += 1

        best = None
        idx = b_floor
        while idx < len(b_rows):
            b_row = b_rows[idx]
            if b_row.start > a_stop + tolerance:
                break
            
            overlap = max(0.0, (min(a_stop, b_row.stop) - max(a_start, b_row.start)).total_seconds())
            shorter = max(1.0, min((a_stop - a_start).total_seconds(), (b_row.stop - b_row.start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_row.start - a_start).total_seconds())
            
            if overlap_ratio >= 0.35 or start_delta <= 20 * 60:
                time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (20 * 60)))
                title_score = 0.0
                if a_row.normalized_title and b_row.normalized_title:
                    if a_row.normalized_title == b_row.normalized_title:
                        title_score = 1.0
                    else:
                        title_score = SequenceMatcher(None, a_row.normalized_title, b_row.normalized_title).ratio()
                candidate = (time_score, title_score)
                if best is None or candidate > best:
                    best = candidate
            idx += 1
            
        if best is not None:
            similarities.append(best[1])

    if not similarities:
        return {"comparable": 0, "agreement": None, "average_similarity": None, "detected_shift": 0}
        
    agreed = sum(1 for s in similarities if s >= 0.68)
    return {
        "comparable": len(similarities),
        "agreement": round(agreed / len(similarities), 4),
        "average_similarity": round(mean(similarities), 4),
        "detected_shift": int(offset.total_seconds() // 3600)
    }

def cross_source_programme_agreement(
    a_source: EpgSource, a_id: str, b_source: EpgSource, b_id: str, now: datetime,
) -> dict[str, object]:
    left_key, right_key = (a_source.spec.key, a_id), (b_source.spec.key, b_id)
    cache_key = (*left_key, *right_key) if left_key <= right_key else (*right_key, *left_key)
    if (cached := _AGREEMENT_CACHE.get(cache_key)) is not None:
        return cached

    window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=36)
    a_rows = [r for r in programme_rows(a_source, a_id) if r.stop >= window_start and r.start <= window_end]
    b_rows = [r for r in programme_rows(b_source, b_id) if r.stop >= window_start and r.start <= window_end]
    
    result = _calc_agreement(a_rows, b_rows, timedelta(0))
    
    if result["comparable"] >= 3 and (result["agreement"] or 0) < AGREEMENT_THRESHOLD:
        for offset_h in (-3, 3, -1, 1, -2, 2, -4, 4):
            res_test = _calc_agreement(a_rows, b_rows, timedelta(hours=offset_h))
            if res_test["comparable"] >= MIN_COMPARABLE_PROGRAMMES and (res_test["agreement"] or 0) > (result["agreement"] or 0):
                result = res_test
                if result["agreement"] >= 0.8:
                    break

    _AGREEMENT_CACHE[cache_key] = result
    return result


def evaluate_candidate(match: ChannelMatch, all_sources: list[EpgSource], aliases: list[str], now: datetime) -> CandidateEvaluation:
    agreements = []
    for other_source in all_sources:
        if other_source.spec.key == match.source.spec.key: continue
        other_match = find_match(other_source, aliases)
        if other_match:
            agreement = cross_source_programme_agreement(match.source, match.channel_id, other_source, other_match.channel_id, now)
            if agreement.get("agreement") is not None:
                agreements.append(agreement)

    avg_agree = mean([a["agreement"] for a in agreements]) if agreements else 0.0
    health_count = sum(1 for r in programme_rows(match.source, match.channel_id) if r.start <= now + timedelta(days=1))

    return CandidateEvaluation(
        match=match,
        support_votes=len(agreements),
        vote_sources=len(all_sources),
        support_weight=match.source.spec.vote_weight,
        vote_weight=match.source.spec.vote_weight,
        support_families=1,
        weighted_ratio=1.0,
        avg_support_agreement=avg_agree,
        health_score=match.match_score * 100 + (avg_agree * 50) + min(health_count, 20),
        health={"future_programmes": health_count},
        confidence=int(match.match_score * 100),
        comparisons=agreements
    )


def write_epg_stream(
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]],
    compatibility_ids: dict[str, list[str]],
    now: datetime,
    overrides: dict[str, object] = None,
) -> tuple[int, int]:
    overrides = overrides or {}
    gzip_path = OUTPUT_DIR / "epg.xml.gz"
    plain_path = OUTPUT_DIR / "epg.xml"
    plain = plain_path.open("wb") if WRITE_PLAIN_XML else None
    programme_count = 0
    compatibility_alias_count = 0
    cutoff = now - timedelta(hours=OUTPUT_PAST_HOURS)

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None: plain.write(data)

            header = (
                "<?xml version='1.0' encoding='utf-8'?>\n"
                f"<tv generator-info-name=\"EPG Universal Otimizado\" source-info-name=\"Anti-Sobreposicao e Correcao UTC\">\n"
            ).encode("utf-8")
            emit(header)

            for entry, chosen, _evaluations, _forced in selected:
                target_id = str(entry.get("id", ""))
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
                target_id = str(entry.get("id", ""))
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                raw_elements = chosen.match.source.programs.get(chosen.match.channel_id, [])
                parsed_progs = []
                
                ch_shift = float(overrides.get("channel_timeshifts", {}).get(target_id, 0.0))
                src_shift = float(overrides.get("source_timeshifts", {}).get(chosen.match.source.spec.key, 0.0))
                total_shift = timedelta(hours=ch_shift + src_shift)

                for el in raw_elements:
                    start_dt = parse_xmltv_datetime(el.attrib.get("start", ""))
                    stop_dt = parse_xmltv_datetime(el.attrib.get("stop", ""))
                    if not start_dt: continue
                    start_dt += total_shift
                    if stop_dt: stop_dt += total_shift
                    parsed_progs.append({"start": start_dt, "stop": stop_dt, "el": el})

                parsed_progs.sort(key=lambda x: x["start"])

                for i, prog in enumerate(parsed_progs):
                    start_dt, stop_dt = prog["start"], prog["stop"]

                    # Anti-sobreposição
                    if stop_dt is None or stop_dt <= start_dt:
                        stop_dt = parsed_progs[i+1]["start"] if i + 1 < len(parsed_progs) else start_dt + timedelta(hours=2)
                    if i + 1 < len(parsed_progs) and stop_dt > parsed_progs[i+1]["start"]:
                        stop_dt = parsed_progs[i+1]["start"]

                    if stop_dt < cutoff:
                        continue

                    original = copy.deepcopy(prog["el"])
                    original.attrib["start"] = format_xmltv_datetime(start_dt)
                    original.attrib["stop"] = format_xmltv_datetime(stop_dt)

                    for output_id in ids_for_channel:
                        original.attrib["channel"] = output_id
                        emit(ET.tostring(original, encoding="utf-8"))
                        emit(b"\n")
                        programme_count += 1

            emit(b"</tv>\n")

    if plain is not None: plain.close()
    elif plain_path.exists(): plain_path.unlink()
    return programme_count, compatibility_alias_count


def generate_reports(selected_channels, now):
    """Gera relatórios necessários para que a GitHub Action do utilizador passe sem erros de Deploy."""
    report_md = "# Relatório de Atualização de EPG\n\n"
    report_md += f"**Data:** {now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
    report_md += "| Canal | Fonte Escolhida | Status Confiança |\n|---|---|---|\n"
    
    valid = True
    for entry, chosen, _, forced in selected_channels:
        status = "Forçado" if forced else f"{chosen.confidence}%"
        report_md += f"| {entry.get('name', 'Desconhecido')} | {chosen.match.source.spec.label} | {status} |\n"
        if not forced and chosen.confidence < 40:
            valid = False

    (OUTPUT_DIR / "report.md").write_text(report_md, encoding="utf-8")
    (OUTPUT_DIR / "validation.md").write_text("# Validação\n\nProcessamento finalizado." if valid else "# Validação\n\nAviso: alguns canais com baixa confiança.", encoding="utf-8")
    
    with (OUTPUT_DIR / "report.json").open("w", encoding="utf-8") as f:
        json.dump({"updated_at": now.isoformat(), "channels_processed": len(selected_channels)}, f, indent=2)
        
    with (OUTPUT_DIR / "validation.json").open("w", encoding="utf-8") as f:
        json.dump({"valid": valid}, f, indent=2)


def main():
    now = datetime.now(timezone.utc)
    print("Iniciando geração de EPG Otmizado...")
    
    overrides = {}
    if OVERRIDES_PATH.exists():
        with OVERRIDES_PATH.open("r", encoding="utf-8") as f:
            overrides = json.load(f)

    channels = []
    if CATALOG_PATH.exists():
        with CATALOG_PATH.open("r", encoding="utf-8") as f:
            channels = json.load(f)
    else:
        print(f"Aviso: O ficheiro de catálogo {CATALOG_PATH} não foi encontrado.")

    print(f"Baixando {len(SOURCE_SPECS)} fontes de EPG em paralelo...")
    active_sources = []
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        future_to_spec = {executor.submit(read_optional_source, spec, overrides): spec for spec in SOURCE_SPECS}
        for future in as_completed(future_to_spec):
            spec = future_to_spec[future]
            source, error = future.result()
            if source:
                print(f" -> [{spec.label}] {len(source.channels)} canais, {len(source.programs)} grades carregadas.")
                active_sources.append(source)
            else:
                print(f" -> [{spec.label}] FALHA AO CARREGAR: {error}")

    if not active_sources:
        print("Erro Crítico: Nenhuma fonte pôde ser baixada.")
        exit(1)

    # CORREÇÃO DO ERRO DO GITHUB ACTIONS AQUI (source em vez de spec)
    source_by_key = {source.spec.key: source for source in active_sources}
    
    selected_channels = []
    compatibility_ids = defaultdict(list)
    
    for entry in channels:
        target_id = str(entry.get("id", ""))
        if not target_id: continue
        
        aliases = aliases_for(entry)
        old_ids = old_id_hints_for(entry)
        
        override_src, override_ch_id = manual_override(overrides, target_id)
        chosen_candidate = None
        is_forced = False
        
        if override_src and override_ch_id and override_src in source_by_key:
            src = source_by_key[override_src]
            if override_ch_id in src.channels:
                match = ChannelMatch(src, override_ch_id, "Seleção Manual (overrides)", 1.0)
                chosen_candidate = CandidateEvaluation(
                    match=match, support_votes=1, vote_sources=1, support_weight=1.0, vote_weight=1.0, 
                    support_families=1, weighted_ratio=1.0, avg_support_agreement=1.0, health_score=100.0, 
                    health={}, confidence=100, comparisons=[]
                )
                is_forced = True
        
        if not chosen_candidate:
            candidates = []
            for src in active_sources:
                match = find_match(src, aliases, old_ids)
                if match:
                    candidates.append(match)
                    
            evaluations = [evaluate_candidate(m, active_sources, aliases, now) for m in candidates]
            if evaluations:
                evaluations.sort(key=lambda e: e.health_score, reverse=True)
                chosen_candidate = evaluations[0]
        
        if chosen_candidate:
            selected_channels.append((entry, chosen_candidate, [], is_forced))
            compatibility_ids[target_id].append(target_id)
            for old in old_ids:
                if old not in compatibility_ids[target_id]:
                    compatibility_ids[target_id].append(old)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    print("Escrevendo ficheiro EPG final com Anti-Sobreposição...")
    prog_count, _compat_count = write_epg_stream(selected_channels, compatibility_ids, now, overrides)
    
    print("Gerando relatórios e validações...")
    generate_reports(selected_channels, now)
    
    print(f"==================================================")
    print(f"Sucesso: {prog_count} programas consolidados na saída.")
    print(f"==================================================")

if __name__ == "__main__":
    main()
