#!/usr/bin/env python3
"""EPG universal adaptável (Zero-Dictionary, Multi-URL Segura)
com Interceptador Xtream Codes Nativo, Filtro VOD por URL e
Força Bruta Baseada EXCLUSIVAMENTE em Nomes (Ignorando tvg-id).
"""
from __future__ import annotations

import copy
import gzip
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from statistics import mean
from xml.sax.saxutils import quoteattr
from typing import Iterable, Any

from epg_utils import digit_signature, normalize_name as original_normalize_name, normalize_source_id

CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
ESPELHO_PATH = Path("espelho.json")

M3U_URLS = []
for i in range(1, 6):
    url = os.environ.get(f"M3U_URL_{i}")
    if url and url.startswith("http"):
        M3U_URLS.append(url.strip())

FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.84"))
FUZZY_MARGIN = float(os.environ.get("FUZZY_MARGIN", "0.035"))
AGREEMENT_THRESHOLD = float(os.environ.get("AGREEMENT_THRESHOLD", "0.55"))
MIN_COMPARABLE_PROGRAMMES = int(os.environ.get("MIN_COMPARABLE_PROGRAMMES", "3"))

DOWNLOAD_WORKERS = max(1, min(4, int(os.environ.get("DOWNLOAD_WORKERS", "3"))))
GZIP_LEVEL = max(1, min(9, int(os.environ.get("GZIP_LEVEL", "6"))))
OUTPUT_PAST_HOURS = max(0, int(os.environ.get("OUTPUT_PAST_HOURS", "12")))
WRITE_PLAIN_XML = os.environ.get("WRITE_PLAIN_XML", "0").strip().lower() in {"1", "true", "yes"}

EPG_SOURCES_CONFIG = [
    ("Meu Guia", "epg_meuguia.xml", "meuguia", 3.0),
    ("GuiaDeTV", "epg_guiadetv.xml", "guiadetv", 3.0),
    ("TV Plus", "epg_tvplus.xml", "tvplus", 1.5),
    ("MiTV", "epg_mitv.xml", "mitv", 1.5),
    ("TV Map", "epg_tvmap.xml", "tvmap", 1.5),
    ("UrbanTV", "epg_urbantv.xml", "urbantv", 1.5),
    ("Genius", os.environ.get("PRIMARY_EPG_URL", "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz"), "genius", 1.5),
    ("Open-EPG BR 4", "https://www.open-epg.com/files/brazil4.xml", "openepg", 1.0),
    ("Open-EPG BR", "https://www.open-epg.com/files/brazil.xml", "openepg", 1.0),
    ("EPG pw BR", "https://epg.pw/xmltv/epg_BR.xml", "epgpw", 1.0),
    ("Claro", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/claro.xml", "git_extra", 1.0),
    ("Globo", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/globo.xml", "git_extra", 1.0),
    ("VivoPlay", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/vivoplay.xml", "git_extra", 1.0),
    ("XSports", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/xsports.xml", "git_extra", 1.0),
    ("EPGShare BR1", "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz", "epgshare", 1.0),
    ("IPTV-EPG BR", "https://iptv-epg.org/files/epg-br.xml", "iptvepg", 1.0),
    ("EPGShare BR2", "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz", "epgshare", 1.0),
]

@dataclass(frozen=True)
class SourceSpec:
    key: str; label: str; url: str; priority: int; family: str; vote_weight: float
    default_offset: str = "+0000"; timeshift_hours: float = 0.0

SOURCE_SPECS = []
for idx, (lbl, link, family, weight) in enumerate(EPG_SOURCES_CONFIG, start=1):
    safe_key = re.sub(r'[^a-z0-9]', '', lbl.lower()) or f"src{idx}"
    SOURCE_SPECS.append(SourceSpec(key=safe_key, label=lbl, url=link, priority=idx, family=family, vote_weight=weight))

@dataclass(frozen=True)
class ProgrammeRow:
    start: datetime; stop: datetime; title: str; normalized_title: str; element: ET.Element

@dataclass
class EpgSource:
    spec: SourceSpec; channels: dict[str, ET.Element]; names: dict[str, list[str]]
    normalized_names: dict[str, list[str]]; programs: dict[str, list[ET.Element]]
    rows: dict[str, list[ProgrammeRow]]; exact_index: dict[str, set[str]]
    fuzzy_index: dict[tuple[str, ...], dict[str, set[str]]]; normalized_id_index: dict[str, set[str]]
    @property
    def label(self) -> str: return self.spec.label

@dataclass
class ChannelMatch:
    source: EpgSource; channel_id: str; method: str; match_score: float

@dataclass
class CandidateEvaluation:
    match: ChannelMatch; support_votes: int; vote_sources: int; support_weight: float; vote_weight: float
    support_families: int; weighted_ratio: float; avg_support_agreement: float; health_score: float
    health: dict[str, object]; confidence: int; comparisons: list[dict[str, object]]

def normalizar_canal(nome: str) -> str:
    if not nome: return ""
    original_nome = nome
    nome = re.sub(r'\[.*?\]|\(.*?\)|\|.*?\|', ' ', nome)
    nome = re.sub(r'[\¹\²\³\⁴]', '', nome) 
    nome = re.sub(r'\bJP NEWS\b', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bJOVEM PAN\b(?! NEWS)', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX SPORTS\b', 'ESPN 4', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bBOOMERANG\b', 'CARTOONITO', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bMAX PRIME\b', 'HBO POP', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX PREMIUM\b', 'STAR PREMIUM', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX CHANNEL\b', 'STAR CHANNEL', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX LIFE\b', 'STAR LIFE', nome, flags=re.IGNORECASE)
    nome = re.sub(r'^GB\b', 'GLOBO', nome, flags=re.IGNORECASE)
    nome = re.sub(r'^RECORDTV\b', 'RECORD', nome, flags=re.IGNORECASE)
    
    tags = r'\b(SD|HD|FHD|UHD|4K|8K|H264|H265|HEVC|HDR\+?|LEG|LEGENDADO|DUB|DUBLADO|HQ|VIP|VOD|24H[S]?|24/7|1080P|720P|60FPS|PPV|EVENTOS?)\b[\*\²\³\¹\⁴\+]*'
    nome = re.sub(tags, '', nome, flags=re.IGNORECASE)
    nome = re.sub(r'[*=_\-]', ' ', nome)
    nome_limpo = ' '.join(nome.split()).strip().upper()
    return nome_limpo if len(nome_limpo) > 1 else original_normalize_name(original_nome)

def normalize_name(value: str) -> str:
    return original_normalize_name(normalizar_canal(value))

def inferir_emissora_pelo_grupo(nome_normalizado: str, nome_grupo: str) -> str:
    if not nome_grupo: return nome_normalizado
    grupo = nome_grupo.upper()
    if "GLOBO" in grupo and not nome_normalizado.startswith("GLOBO"): return f"GLOBO {nome_normalizado}"
    if "SBT" in grupo and not nome_normalizado.startswith("SBT"): return f"SBT {nome_normalizado}"
    if "RECORD" in grupo and not nome_normalizado.startswith("RECORD"): return f"RECORD {nome_normalizado}"
    if "BAND" in grupo and not nome_normalizado.startswith("BAND"): return f"BAND {nome_normalizado}"
    if "PREMIERE" in grupo and not nome_normalizado.startswith("PREMIERE") and len(nome_normalizado) <= 4: return f"PREMIERE {nome_normalizado}"
    return nome_normalizado

def local_tag(element: ET.Element) -> str: return element.tag.rsplit("}", 1)[-1]
def format_xmltv_datetime(dt: datetime) -> str: return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

@lru_cache(maxsize=500000)
def parse_xmltv_datetime(value: str, default_offset: str = "+0000") -> datetime | None:
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
            dt = datetime(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10]), int(digits[10:12]), second, tzinfo=tz)
            return dt.astimezone(timezone.utc)
        except (ValueError, IndexError): pass
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

def download_to_temp_file(url: str, attempts: int = 4) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    tmp_path = Path(tmp.name)
    tmp.close()
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp_path.open("wb") as out:
                while chunk := resp.read(1024 * 512): out.write(chunk)
            return tmp_path
        except Exception as exc:
            last_error = exc
            if attempt < attempts: time.sleep(attempt * 4) 
    if tmp_path.exists(): tmp_path.unlink()
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")

def read_source_from_file(spec: SourceSpec, tmp_path: Path) -> EpgSource:
    channels: dict[str, ET.Element] = {}
    names, normalized_names = {}, {}
    programs, rows = defaultdict(list), defaultdict(list)
    exact_index, fuzzy_index, normalized_id_index = defaultdict(set), defaultdict(lambda: defaultdict(set)), defaultdict(set)

    with tmp_path.open("rb") as f:
        magic = f.read(2)
        f.seek(0)
        file_obj = gzip.GzipFile(fileobj=f) if magic == b"\x1f\x8b" else f
        tree = ET.parse(file_obj)
        root = tree.getroot()

    for child in root:
        tag = local_tag(child)
        if tag == "channel":
            channel_id = child.attrib.get("id", "").strip()
            if not channel_id: continue
            channels[channel_id] = child
            display_names = [(node.text or "").strip() for node in child if local_tag(node) == "display-name" and (node.text or "").strip()]
            names[channel_id] = display_names
            norm_candidates: list[str] = []
            for candidate in [*display_names, channel_id, normalize_source_id(channel_id)]:
                normalized = normalize_name(candidate)
                if not normalized or normalized in norm_candidates: continue
                norm_candidates.append(normalized)
                exact_index[normalized].add(channel_id)
                fuzzy_index[digit_signature(normalized)][normalized].add(channel_id)
            normalized_names[channel_id] = norm_candidates
            normalized_id = normalize_source_id(channel_id)
            if normalized_id: normalized_id_index[normalized_id].add(channel_id)

        elif tag == "programme":
            channel_id = child.attrib.get("channel", "").strip()
            if not channel_id: continue
            programs[channel_id].append(child)
            start = parse_xmltv_datetime(child.attrib.get("start", ""), spec.default_offset)
            stop = parse_xmltv_datetime(child.attrib.get("stop", ""), spec.default_offset)
            if start is None: continue
            title = programme_title(child)
            rows[channel_id].append(ProgrammeRow(start=start, stop=stop or start, title=title, normalized_title=normalize_programme_title(title), element=child))

    for channel_id, channel_rows in rows.items():
        channel_rows.sort(key=lambda item: item.start)
        fixed_rows: list[ProgrammeRow] = []
        for idx, row in enumerate(channel_rows):
            stop = row.stop
            if stop <= row.start:
                next_start = channel_rows[idx + 1].start if idx + 1 < len(channel_rows) else None
                stop = next_start if (next_start and next_start > row.start) else row.start + timedelta(hours=2)
            fixed_rows.append(ProgrammeRow(start=row.start, stop=stop, title=row.title, normalized_title=row.normalized_title, element=row.element))
        rows[channel_id] = fixed_rows
    root.clear()
    return EpgSource(spec=spec, channels=channels, names=names, normalized_names=normalized_names, programs=dict(programs), rows=dict(rows), exact_index=dict(exact_index), fuzzy_index={sig: dict(items) for sig, items in fuzzy_index.items()}, normalized_id_index=dict(normalized_id_index))

def read_optional_source(spec: SourceSpec) -> tuple[EpgSource | None, str | None]:
    tmp_path = None
    try:
        tmp_path = download_to_temp_file(spec.url)
        src = read_source_from_file(spec, tmp_path)
        return src, None
    except Exception as exc: return None, str(exc)
    finally:
        if tmp_path and tmp_path.exists() and str(tmp_path).endswith(".tmp"):
            try: tmp_path.unlink()
            except Exception: pass

def aliases_for(entry: dict[str, object]) -> list[str]:
    values: list[str] = [str(entry.get("name", ""))]
    values.extend(str(x) for x in entry.get("aliases", []) or [])
    grupo = str(entry.get("group", entry.get("group-title", entry.get("group_title", ""))))
    output, seen = [], set()
    for value in values:
        nome_limpo = normalizar_canal(value)
        nome_com_grupo = inferir_emissora_pelo_grupo(nome_limpo, grupo)
        normalized = normalize_name(nome_com_grupo)
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
        norm_fallback = normalize_name(nome_limpo)
        if norm_fallback and norm_fallback not in seen:
            output.append(norm_fallback)
            seen.add(norm_fallback)
    return output

def choose_exact(source: EpgSource, aliases: Iterable[str]) -> tuple[str | None, str]:
    for alias in aliases:
        candidates = source.exact_index.get(alias, set())
        if candidates: return max(candidates, key=lambda cid: len(source.programs.get(cid, []))), f"nome exato: {alias}"
    return None, ""

def _sequence_ratio_upper_bound(left: str, right: str, left_counter: Counter[str] | None = None) -> float:
    if not left or not right: return 0.0
    length_bound = (2.0 * min(len(left), len(right))) / (len(left) + len(right))
    if length_bound < (FUZZY_THRESHOLD - FUZZY_MARGIN): return length_bound
    lc = left_counter or Counter(left)
    rc = Counter(right)
    common = sum(min(count, rc.get(ch, 0)) for ch, count in lc.items())
    return min(length_bound, (2.0 * common) / (len(left) + len(right)))

def best_fuzzy(source: EpgSource, aliases: list[str]) -> tuple[str | None, float, float, str]:
    if not aliases: return None, 0.0, 0.0, ""
    relevant_floor = FUZZY_THRESHOLD - FUZZY_MARGIN
    best_by_channel: dict[str, tuple[float, str, str]] = {}
    for alias in aliases:
        if len(alias) < 2: continue 
        alias_counter = Counter(alias)
        bucket = source.fuzzy_index.get(digit_signature(alias), {})
        for normalized_candidate, channel_ids in bucket.items():
            if _sequence_ratio_upper_bound(alias, normalized_candidate, alias_counter) < relevant_floor: continue
            ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
            if ratio < relevant_floor: continue
            for channel_id in channel_ids:
                old = best_by_channel.get(channel_id)
                if old is None or ratio > old[0]: best_by_channel[channel_id] = (ratio, alias, normalized_candidate)
    if not best_by_channel: return None, 0.0, 0.0, ""
    scored = sorted(((score, cid, a, cand) for cid, (score, a, cand) in best_by_channel.items()), reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second_score, f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"

def find_match(source: EpgSource, aliases: list[str]) -> ChannelMatch | None:
    exact_id, reason = choose_exact(source, aliases)
    if exact_id: return ChannelMatch(source, exact_id, reason, 1.0)
    
    fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
    if fuzzy_id and best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN:
        return ChannelMatch(source, fuzzy_id, fuzzy_reason, best_score)
        
    return None

def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None]:
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if not isinstance(item, dict): return None, None
    source = str(item.get("source", "")).lower().strip()
    channel_id = str(item.get("channel_id", "")).strip()
    valid = {s.key for s in SOURCE_SPECS}
    return (source, channel_id) if source in valid and channel_id else (None, None)

def programme_title(programme: ET.Element | None) -> str:
    if programme is None: return ""
    for child in programme:
        if local_tag(child) == "title" and (child.text or "").strip(): return (child.text or "").strip()
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

def programme_rows(source: EpgSource, channel_id: str) -> list[ProgrammeRow]: return source.rows.get(channel_id, [])

def now_next_for(source: EpgSource, channel_id: str, now: datetime) -> tuple[dict | None, dict | None]:
    current, next_item = None, None
    for row in programme_rows(source, channel_id):
        if row.start <= now < row.stop: current = {"start": row.start, "stop": row.stop, "title": row.title}
        elif row.start > now and next_item is None:
            next_item = {"start": row.start, "stop": row.stop, "title": row.title}
            if current is not None: break
    return current, next_item

_AGREEMENT_CACHE: dict[tuple[str, str, str, str], dict[str, object]] = {}
_HEALTH_CACHE: dict[tuple[str, str], dict[str, object]] = {}

def _calc_agreement(a_rows: list[ProgrammeRow], b_rows: list[ProgrammeRow], offset: timedelta) -> dict[str, object]:
    similarities = []
    b_floor = 0
    tolerance = timedelta(minutes=20)
    for a_row in a_rows:
        a_start, a_stop = a_row.start + offset, a_row.stop + offset
        while b_floor < len(b_rows) and b_rows[b_floor].stop < a_start - tolerance: b_floor += 1
        best = None
        idx = b_floor
        while idx < len(b_rows):
            b_row = b_rows[idx]
            if b_row.start > a_stop + tolerance: break
            overlap = max(0.0, (min(a_stop, b_row.stop) - max(a_start, b_row.start)).total_seconds())
            shorter = max(1.0, min((a_stop - a_start).total_seconds(), (b_row.stop - b_row.start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_row.start - a_start).total_seconds())
            if overlap_ratio >= 0.35 or start_delta <= 20 * 60:
                time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (20 * 60)))
                title_score = 0.0
                if a_row.normalized_title and b_row.normalized_title:
                    title_score = 1.0 if a_row.normalized_title == b_row.normalized_title else SequenceMatcher(None, a_row.normalized_title, b_row.normalized_title).ratio()
                cand = (time_score, title_score)
                if best is None or cand > best: best = cand
            idx += 1
        if best is not None: similarities.append(best[1])

    if not similarities: return {"comparable": 0, "agreement": None, "average_similarity": None, "detected_shift": 0}
    agreed = sum(1 for s in similarities if s >= 0.68)
    return {"comparable": len(similarities), "agreement": round(agreed / len(similarities), 4), "average_similarity": round(mean(similarities), 4), "detected_shift": int(offset.total_seconds() // 3600)}

def cross_source_programme_agreement(a_source: EpgSource, a_id: str, b_source: EpgSource, b_id: str, now: datetime) -> dict[str, object]:
    left_key, right_key = (a_source.spec.key, a_id), (b_source.spec.key, b_id)
    cache_key = (*left_key, *right_key) if left_key <= right_key else (*right_key, *left_key)
    if (cached := _AGREEMENT_CACHE.get(cache_key)) is not None: return cached
    window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=36)
    a_rows = [r for r in programme_rows(a_source, a_id) if r.stop >= window_start and r.start <= window_end]
    b_rows = [r for r in programme_rows(b_source, b_id) if r.stop >= window_start and r.start <= window_end]
    result = _calc_agreement(a_rows, b_rows, timedelta(0))
    if result["comparable"] >= 3 and (result["agreement"] or 0) < AGREEMENT_THRESHOLD:
        for offset_h in (-3, 3, -1, 1, -2, 2, -4, 4):
            res_test = _calc_agreement(a_rows, b_rows, timedelta(hours=offset_h))
            if res_test["comparable"] >= MIN_COMPARABLE_PROGRAMMES and (res_test["agreement"] or 0) > (result["agreement"] or 0):
                result = res_test
                if result["agreement"] >= 0.8: break
    _AGREEMENT_CACHE[cache_key] = result
    return result

def schedule_health(source: EpgSource, channel_id: str, now: datetime) -> dict[str, object]:
    cache_key = (source.spec.key, channel_id)
    if (cached := _HEALTH_CACHE.get(cache_key)) is not None: return cached
    rows = programme_rows(source, channel_id)
    current, next_item = now_next_for(source, channel_id, now)
    invalid, overlaps, gaps = 0, 0, 0
    previous_stop = None
    for row in rows:
        start, stop = row.start, row.stop
        duration = stop - start
        if duration.total_seconds() <= 0 or duration > timedelta(hours=12): invalid += 1
        if previous_stop is not None:
            if start < previous_stop - timedelta(seconds=30): overlaps += 1
            elif start > previous_stop + timedelta(minutes=30): gaps += 1
        if previous_stop is None or stop > previous_stop: previous_stop = stop
    future_end = max((row.stop for row in rows if row.stop > now), default=None)
    future_hours = max(0.0, (future_end - now).total_seconds() / 3600) if future_end else 0.0
    score = ((0.35 if current else 0.0) + (0.15 if next_item else 0.0) + (0.30 * min(future_hours / 24.0, 1.0)) + (0.20 * min(len(rows) / 10.0, 1.0)))
    score -= min(0.25, invalid * 0.08 + overlaps * 0.02 + max(0, gaps - 3) * 0.01)
    result = {"score": round(max(0.0, min(1.0, score)), 4), "rows": len(rows), "current": current, "next": next_item, "future_hours": round(future_hours, 1), "invalid_durations": invalid, "overlaps": overlaps, "gaps": gaps}
    _HEALTH_CACHE[cache_key] = result
    return result

def evaluate_candidates(matches: list[ChannelMatch], now: datetime, max_active_vote_weight: float) -> tuple[CandidateEvaluation, list[CandidateEvaluation]]:
    health_by_source = {m.source.spec.key: schedule_health(m.source, m.channel_id, now) for m in matches}
    sorted_matches = sorted(matches, key=lambda m: (health_by_source[m.source.spec.key]["score"], m.source.spec.vote_weight), reverse=True)
    leader = sorted_matches[0]
    pairwise: dict[tuple[str, str], dict[str, object]] = {}
    for other in sorted_matches[1:]:
        res = cross_source_programme_agreement(leader.source, leader.channel_id, other.source, other.channel_id, now)
        pairwise[(leader.source.spec.key, other.source.spec.key)] = res
        pairwise[(other.source.spec.key, leader.source.spec.key)] = res
    evaluations: list[CandidateEvaluation] = []
    for match in matches:
        supporters = [match.source]
        support_votes, vote_sources = 1, 1
        support_weight, vote_weight = match.source.spec.vote_weight, match.source.spec.vote_weight
        support_agreements, comparisons = [], []
        for other in matches:
            if other.source is match.source: continue
            pair_key = (match.source.spec.key, other.source.spec.key)
            result = pairwise.get(pair_key)
            if not result:
                result = cross_source_programme_agreement(match.source, match.channel_id, other.source, other.channel_id, now)
                pairwise[pair_key] = result
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
                else: vote = "diverge"
            comparisons.append({"other_source": other.source.label, "other_source_key": other.source.spec.key, "other_channel_id": other.channel_id, "other_match_method": other.method, "comparable": comparable, "agreement": agreement, "average_similarity": result.get("average_similarity"), "vote": vote})
        support_families = len({src.spec.family for src in supporters})
        weighted_ratio = support_weight / vote_weight if vote_weight else 0.0
        avg_support = mean(support_agreements) if support_agreements else 0.0
        health_score = float(health_by_source[match.source.spec.key]["score"])
        quality = 0.55 * weighted_ratio + 0.20 * health_score + 0.10 * match.match_score + 0.15 * avg_support
        evidence_factor = 0.55 + 0.45 * min(vote_weight / max(max_active_vote_weight, 0.5), 1.0)
        confidence = round(100 * max(0.0, min(1.0, quality * evidence_factor)))
        evaluations.append(CandidateEvaluation(match=match, support_votes=support_votes, vote_sources=vote_sources, support_weight=round(support_weight, 3), vote_weight=round(vote_weight, 3), support_families=support_families, weighted_ratio=round(weighted_ratio, 4), avg_support_agreement=round(avg_support, 4), health_score=round(health_score, 4), health=health_by_source[match.source.spec.key], confidence=confidence, comparisons=comparisons))

    def rank(ev: CandidateEvaluation) -> tuple[int, float, int, float, float, float, float, int]:
        has_programmes = int(bool(ev.match.source.programs.get(ev.match.channel_id)))
        return (has_programmes, ev.support_weight, ev.support_families, ev.weighted_ratio, ev.avg_support_agreement, ev.health_score, ev.match.match_score, -ev.match.source.spec.priority)

    evaluations.sort(key=rank, reverse=True)
    return evaluations[0], evaluations

def serialize_now_next(item: dict | None) -> dict | None:
    if item is None: return None
    return {"title": item["title"], "start": item["start"].isoformat(), "stop": item["stop"].isoformat()}

def make_validation(entry: dict[str, object], chosen: CandidateEvaluation, all_evaluations: list[CandidateEvaluation], now: datetime) -> dict[str, object]:
    health = chosen.health
    warnings = []
    if not health["rows"]: warnings.append("nenhum programa na fonte escolhida")
    if health["current"] is None: warnings.append("sem programa cobrindo o horário atual")
    if health["next"] is None: warnings.append("sem próximo programa disponível")
    if float(health["future_hours"]) < 6: warnings.append(f"pouca programação futura ({health['future_hours']} h)")

    first_by_priority = min(all_evaluations, key=lambda ev: ev.match.source.spec.priority) if all_evaluations else chosen
    switched = first_by_priority.match.source is not chosen.match.source
    if switched: warnings.append(f"fonte base alterada de {first_by_priority.match.source.label} para {chosen.match.source.label} por maior consenso")

    if chosen.support_families >= 3 and chosen.confidence >= 75: status = "ok"
    elif chosen.confidence >= 55 and chosen.support_families >= 2: status = "atencao"
    else: status = "suspeito"
    if float(health["score"]) < 0.45: status = "suspeito"

    candidates = [
        {"source": ev.match.source.label, "source_key": ev.match.source.spec.key, "channel_id": ev.match.channel_id, "match_method": ev.match.method, "match_score": round(ev.match.match_score, 4), "confidence": ev.confidence, "health_score": ev.health_score}
        for ev in sorted(all_evaluations, key=lambda x: x.match.source.spec.priority)
    ]
    return {
        "target_id": str(entry.get("id", "")), "name": str(entry.get("name", "")),
        "source": chosen.match.source.label, "source_key": chosen.match.source.spec.key,
        "source_channel_id": chosen.match.channel_id, "status": status, "confidence": chosen.confidence,
        "warnings": warnings, "selection_changed_from_priority": switched,
        "now": serialize_now_next(health["current"]), "next": serialize_now_next(health["next"]),
        "future_hours": health["future_hours"], "health_score": health["score"],
        "consensus": {"supporting_votes": chosen.support_votes, "vote_sources": chosen.vote_sources, "support_weight": chosen.support_weight, "vote_weight": chosen.vote_weight, "support_families": chosen.support_families, "weighted_ratio": chosen.weighted_ratio, "confidence": chosen.confidence},
        "cross_sources": chosen.comparisons, "candidates": candidates,
    }

def get_clean_aliases(entry: dict[str, object]) -> list[str]:
    """Retorna uma lista de nomes limpos e expandidos do canal para atuar como IDs no XML."""
    values = []
    if entry.get("tvg_names"):
        values.extend(str(x).strip() for x in entry.get("tvg_names", []) if x)
    if entry.get("aliases"):
        values.extend(str(x).strip() for x in entry.get("aliases", []) if x)
        
    raw_name = str(entry.get("name", "")).strip()
    if raw_name:
        values.append(raw_name)
        
    unique_values = []
    for v in values:
        if v and v not in unique_values:
            unique_values.append(v)
            
    expanded = list(unique_values)
    for v in unique_values:
        # Remove colchetes ou parenteses ao final (ex: "HBO HD [2]" -> "HBO HD")
        no_brackets = re.sub(r'\s*[\[\(].*?[\]\)]\s*$', '', v).strip()
        if no_brackets and no_brackets not in expanded:
            expanded.append(no_brackets)
            
        # Remove tags de qualidade e status (ex: "HBO HD" -> "HBO")
        tags = r'\b(SD|HD|FHD|UHD|4K|8K|H264|H265|HEVC|HDR\+?|LEG|LEGENDADO|DUB|DUBLADO|HQ|VIP|VOD|24H[S]?|24/7|1080P|720P|60FPS|PPV|EVENTOS?)\b[\*\²\³\¹\⁴\+]*'
        no_tags = re.sub(tags, '', no_brackets, flags=re.IGNORECASE)
        no_tags = re.sub(r'[*=_\-]', ' ', no_tags)
        no_tags = ' '.join(no_tags.split()).strip()
        
        if no_tags and no_tags not in expanded:
            expanded.append(no_tags)
            
    return [v for v in expanded if v and len(v) <= 220]

def build_name_based_ids(entries: list[dict[str, object]]) -> dict[str, list[str]]:
    """Gera dicionário de exportação ignorando COMPLETAMENTE o tvg-id."""
    by_target = {}
    for entry in entries:
        target_id = str(entry.get("id", ""))
        ids_to_export = []
        
        for alias in get_clean_aliases(entry):
            if alias not in ids_to_export:
                ids_to_export.append(alias)
                
        by_target[target_id] = ids_to_export
        
    return by_target

def stitch_multi_source_schedule(
    all_evaluations: list[CandidateEvaluation], target_channel_id: str, cutoff: datetime, overrides: dict[str, object]
) -> list[ET.Element]:
    if not all_evaluations: return []

    best_descriptions: dict[str, str] = {}
    for ev in all_evaluations:
        for prog_row in ev.match.source.rows.get(ev.match.channel_id, []):
            desc_node = prog_row.element.find("desc")
            if desc_node is not None and desc_node.text:
                desc_text = desc_node.text.strip()
                norm_title = prog_row.normalized_title
                if len(desc_text) > len(best_descriptions.get(norm_title, "")):
                    best_descriptions[norm_title] = desc_text

    merged_timeline: list[dict[str, Any]] = []

    for ev in all_evaluations:
        src = ev.match.source
        cid = ev.match.channel_id
        raw_elements = src.programs.get(cid, [])
        if not raw_elements: continue

        ch_shift = float(overrides.get("channel_timeshifts", {}).get(target_channel_id, 0.0))
        src_shift = float(overrides.get("source_timeshifts", {}).get(src.spec.key, 0.0))
        total_shift = timedelta(hours=ch_shift + src_shift)

        parsed = []
        for el in raw_elements:
            start_dt = parse_xmltv_datetime(el.attrib.get("start", ""), src.spec.default_offset)
            stop_dt = parse_xmltv_datetime(el.attrib.get("stop", ""), src.spec.default_offset)
            if not start_dt: continue
            start_dt += total_shift
            if stop_dt: stop_dt += total_shift
            parsed.append({"start": start_dt, "stop": stop_dt, "el": el})
        parsed.sort(key=lambda x: x["start"])

        cleaned_source_progs = []
        for i, prog in enumerate(parsed):
            s_dt, sp_dt = prog["start"], prog["stop"]
            if sp_dt is None or sp_dt <= s_dt:
                sp_dt = parsed[i + 1]["start"] if i + 1 < len(parsed) else s_dt + timedelta(hours=2)
            if i + 1 < len(parsed) and sp_dt > parsed[i + 1]["start"]: sp_dt = parsed[i + 1]["start"]
            if sp_dt < cutoff: continue
            cleaned_source_progs.append({"start": s_dt, "stop": sp_dt, "el": prog["el"]})

        if not merged_timeline:
            merged_timeline = cleaned_source_progs
            continue

        for cand in cleaned_source_progs:
            c_start, c_stop = cand["start"], cand["stop"]
            
            valid_segments = [(c_start, c_stop)]
            for ex in merged_timeline:
                new_segments = []
                for seg_start, seg_stop in valid_segments:
                    if seg_stop <= ex["start"] or seg_start >= ex["stop"]:
                        new_segments.append((seg_start, seg_stop))
                    else:
                        if seg_start < ex["start"]: new_segments.append((seg_start, ex["start"]))
                        if seg_stop > ex["stop"]: new_segments.append((ex["stop"], seg_stop))
                valid_segments = new_segments

            for seg_start, seg_stop in valid_segments:
                if (seg_stop - seg_start).total_seconds() >= 180:
                    new_cand = copy.deepcopy(cand)
                    new_cand["start"] = seg_start
                    new_cand["stop"] = seg_stop
                    merged_timeline.append(new_cand)
                    
    merged_timeline.sort(key=lambda x: x["start"])

    final_elements = []
    for i, prog in enumerate(merged_timeline):
        s_dt, sp_dt = prog["start"], prog["stop"]
        if i + 1 < len(merged_timeline) and sp_dt > merged_timeline[i + 1]["start"]:
            sp_dt = merged_timeline[i + 1]["start"]
            
        el = copy.deepcopy(prog["el"])
        el.attrib["start"] = format_xmltv_datetime(s_dt)
        el.attrib["stop"] = format_xmltv_datetime(sp_dt)
        
        title_node = el.find("title")
        if title_node is not None and title_node.text:
            norm_title = normalize_programme_title(title_node.text)
            best_desc = best_descriptions.get(norm_title)
            if best_desc:
                desc_node = el.find("desc")
                if desc_node is None: desc_node = ET.SubElement(el, "desc")
                desc_node.text = best_desc

        final_elements.append(el)
    return final_elements

def write_epg_stream(
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]],
    compatibility_ids: dict[str, list[str]],
    now: datetime,
    overrides: dict[str, object] = None,
) -> tuple[int, int]:
    overrides = overrides or {}
    gzip_path, plain_path = OUTPUT_DIR / "epg.xml.gz", OUTPUT_DIR / "epg.xml"
    plain = plain_path.open("wb") if WRITE_PLAIN_XML else None
    programme_count, compatibility_alias_count = 0, 0
    cutoff = now - timedelta(hours=OUTPUT_PAST_HOURS)

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None: plain.write(data)

            generator = "EPG Universal Otimizado (Name-Based Match)"
            source_name = "Consolidado Inteligente de Múltiplas Fontes"
            emit((f"<?xml version='1.0' encoding='utf-8'?>\n<tv generator-info-name={quoteattr(generator)} source-info-name={quoteattr(source_name)}>\n").encode("utf-8"))

            for entry, chosen, _evaluations, _forced in selected:
                target_id = str(entry.get("id", ""))
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                compatibility_alias_count += max(0, len(ids_for_channel) - 1)
                
                for output_id in ids_for_channel:
                    channel = ET.Element("channel", {"id": output_id})
                    exact_name = ET.Element("display-name")
                    exact_name.text = output_id
                    channel.append(exact_name)
                    
                    source_channel = chosen.match.source.channels[chosen.match.channel_id]
                    for node in source_channel:
                        if local_tag(node) == "icon":
                            icon_node = copy.deepcopy(node)
                            channel.append(icon_node)
                        
                    emit(ET.tostring(channel, encoding="utf-8"))
                    emit(b"\n")

            for entry, chosen, all_evaluations, _forced in selected:
                target_id = str(entry.get("id", ""))
                ids_for_channel = compatibility_ids.get(target_id, [target_id])
                stitched_programmes = stitch_multi_source_schedule(all_evaluations, target_id, cutoff, overrides)
                
                for original in stitched_programmes:
                    for output_id in ids_for_channel:
                        prog_copy = copy.deepcopy(original)
                        prog_copy.attrib["channel"] = output_id
                        emit(ET.tostring(prog_copy, encoding="utf-8"))
                        emit(b"\n")
                        programme_count += 1
            emit(b"</tv>\n")

    if plain is not None: plain.close()
    elif plain_path.exists(): plain_path.unlink()
    return programme_count, compatibility_alias_count

def main() -> None:
    print("Iniciando geração de EPG (Ignorando TVG-ID dos Provedores, Apenas Nomes)...")
    run_start = time.perf_counter()
    timings = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stage_start = time.perf_counter()
    
    entries = []
    unique_entries = {}
    
    if ESPELHO_PATH.exists():
        try:
            print("Lendo Espelho de Segurança...")
            espelho_data = json.loads(ESPELHO_PATH.read_text("utf-8"))
            for ch in espelho_data.get("channels", []):
                unique_entries[ch["id"]] = {
                    "id": ch["id"],
                    "name": ch["name"],
                    "aliases": set(ch["aliases"]),
                    "group": ch.get("group", "")
                }
            print(f"Espelho recuperado com {len(unique_entries)} canais de backup.")
        except Exception as e:
            print(f"Aviso: Não foi possível carregar o espelho: {e}")
            
    VOD_BLACKLIST = {"VOD", "PLAYBACK", "XXX", "ADULTO", "EVENTO", "LANCAMENTO", "LANÇAMENTO"}
    
    for idx, url_raw in enumerate(M3U_URLS):
        url = url_raw.strip()
        if not url.startswith("http"): continue
        
        parsed_url = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed_url.query)
        
        is_xtream = "get.php" in parsed_url.path and "username" in query and "password" in query
        success_json = False
        
        print(f"\nProcessando Lista Secreta {idx+1}...")
        
        if is_xtream:
            user = query["username"][0]
            pwd = query["password"][0]
            api_url = f"{parsed_url.scheme}://{parsed_url.netloc}/player_api.php?username={user}&password={pwd}&action=get_live_streams"
            print("  -> Link Xtream Codes detectado! Acionando Extração via API JSON (Apenas Live TV)...")
            try:
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    if isinstance(data, list):
                        print(f"  -> Sucesso! {len(data)} canais ao vivo obtidos. VODs ignorados nativamente na fonte.")
                        for ch in data:
                            name = str(ch.get("name", "")).strip()
                            if not name: continue
                            
                            name_upper = name.upper()
                            if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b', name_upper) or \
                               re.search(r'\bT\d{1,3}\s*EP?\d{1,3}\b', name_upper) or \
                               re.search(r'\b24HS?\b', name_upper) or \
                               re.search(r'^24\s*H\b', name_upper):
                                continue
                            
                            target_id = name
                            if target_id not in unique_entries:
                                unique_entries[target_id] = {
                                    "id": target_id,
                                    "name": name,
                                    "aliases": set(),
                                    "group": str(ch.get("category_id", ""))
                                }
                            unique_entries[target_id]["aliases"].add(name)
                        success_json = True
            except Exception as e:
                print(f"  -> Falha ao usar API JSON ({e}). Tentando M3U tradicional...")

        if not success_json:
            print("  -> Baixando arquivo M3U tradicional e aplicando filtro anti-VOD por Extensão de URL...")
            try:
                tmp_m3u = download_to_temp_file(url)
                with tmp_m3u.open("r", encoding="utf-8", errors="ignore") as f:
                    current_extinf = None
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        
                        if line.startswith("#EXTINF:"):
                            current_extinf = line
                            
                        elif line.startswith("http") and current_extinf:
                            stream_url = line
                            
                            parsed_stream = urllib.parse.urlparse(stream_url)
                            path_lower = parsed_stream.path.lower()
                            if path_lower.endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm')):
                                current_extinf = None
                                continue

                            tvg_name_match = re.search(r'tvg-name="(.*?)"', current_extinf, re.IGNORECASE)
                            group_match = re.search(r'group-title="(.*?)"', current_extinf, re.IGNORECASE)
                            name_match = re.search(r',(.*?)$', current_extinf)
                            
                            tvg_name = tvg_name_match.group(1).strip() if tvg_name_match else ""
                            group = group_match.group(1).strip() if group_match else ""
                            name = name_match.group(1).strip() if name_match else ""
                            
                            current_extinf = None
                            
                            group_upper = group.upper()
                            if any(blacklisted in group_upper for blacklisted in VOD_BLACKLIST):
                                continue
                                
                            target_id = tvg_name or name
                            if not target_id: continue
                            
                            name_upper = target_id.upper()
                            if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b', name_upper) or \
                               re.search(r'\bT\d{1,3}\s*EP?\d{1,3}\b', name_upper) or \
                               re.search(r'\b24HS?\b', name_upper) or \
                               re.search(r'^24\s*H\b', name_upper):
                                continue
                            
                            if target_id not in unique_entries:
                                unique_entries[target_id] = {
                                    "id": target_id,
                                    "name": name or tvg_name,
                                    "aliases": set(),
                                    "group": group
                                }
                            
                            if tvg_name: unique_entries[target_id]["aliases"].add(tvg_name)
                            if name: unique_entries[target_id]["aliases"].add(name)
                try: tmp_m3u.unlink()
                except Exception: pass
            except Exception as e:
                print(f"  -> Erro ao processar M3U: {e}")

    for v in unique_entries.values():
        v["aliases"] = list(v["aliases"])
        entries.append(v)
        
    if not entries:
        print("Nenhuma lista e nenhum espelho disponíveis. Verifique seus links no GitHub Secrets.")
        
    espelho_to_save = {"updated_at": datetime.now(timezone.utc).isoformat(), "channels": entries}
    (OUTPUT_DIR / "espelho.json").write_text(json.dumps(espelho_to_save, ensure_ascii=False, indent=2), "utf-8")

    try: overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}
    except Exception: overrides = {}

    compatibility_ids = build_name_based_ids(entries)
    timings["catalogo"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    print(f"\nBaixando e indexando {len(SOURCE_SPECS)} fontes (máx {DOWNLOAD_WORKERS} workers)...")
    loaded: dict[int, tuple[EpgSource | None, str | None]] = {}
    
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        future_map = {executor.submit(read_optional_source, spec): spec for spec in SOURCE_SPECS}
        for future in as_completed(future_map):
            spec = future_map[future]
            try: source, error = future.result()
            except Exception as exc: source, error = None, str(exc)
            loaded[spec.priority] = (source, error)
            if source is not None: print(f"✓ [{spec.label}] {len(source.channels)} canais | {sum(len(v) for v in source.rows.values())} programas carregados.")
            else: print(f"⚠ [{spec.label}] Falhou: {error}")

    active_sources, source_errors = [], {}
    for spec in SOURCE_SPECS:
        source, error = loaded.get(spec.priority, (None, "fonte sem retorno"))
        if source is not None: active_sources.append(source)
        else: source_errors[spec.label] = error or "falha desconhecida"
    timings["download_e_indexacao_fontes"] = round(time.perf_counter() - stage_start, 3)

    if not active_sources: raise RuntimeError("Nenhuma das fontes configuradas pôde ser carregada com sucesso.")

    source_by_key = {s.spec.key: s for s in active_sources}
    max_active_vote_weight = sum(s.spec.vote_weight for s in active_sources)
    now = datetime.now(timezone.utc)

    stage_start = time.perf_counter()
    selected, report_rows, validations = [], [], []

    for entry in entries:
        target_id = str(entry.get("id", ""))
        visible_name = str(entry.get("name", ""))
        aliases = aliases_for(entry)
        matches = [m for source in active_sources if (m := find_match(source, aliases)) is not None]

        override_source_key, override_id = manual_override(overrides, target_id)
        forced = False
        if override_source_key and override_id and (src := source_by_key.get(override_source_key)) and override_id in src.channels:
            override_match = ChannelMatch(src, override_id, "override manual", 1.0)
            matches = [m for m in matches if m.source is not src]
            matches.append(override_match)
            forced = True

        if not matches:
            report_rows.append({"target_id": target_id, "name": visible_name, "status": "missing", "source": None, "source_key": None, "source_channel_id": None, "method": "sem correspondência nas fontes ativas", "confidence": 0})
            continue

        chosen, evaluations = evaluate_candidates(matches, now, max_active_vote_weight)
        if forced and override_source_key:
            if forced_ev := next((ev for ev in evaluations if ev.match.source.spec.key == override_source_key and ev.match.channel_id == override_id), None):
                chosen = forced_ev

        selected.append((entry, chosen, evaluations, forced))
        first_match = min(evaluations, key=lambda ev: ev.match.source.spec.priority) if evaluations else chosen
        changed = first_match.match.source is not chosen.match.source
        
        report_rows.append({"target_id": target_id, "name": visible_name, "status": "matched", "source": chosen.match.source.label, "source_key": chosen.match.source.spec.key, "source_channel_id": chosen.match.channel_id, "method": chosen.match.method, "confidence": chosen.confidence, "changed_from_first_available": changed, "matched_sources_count": len(evaluations)})
        validations.append(make_validation(entry, chosen, evaluations, now))

    timings["matching_consenso_validacao"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    programme_count, compatibility_alias_count = write_epg_stream(selected, compatibility_ids, now, overrides)
    timings["serializacao_e_gzip"] = round(time.perf_counter() - stage_start, 3)

    source_counts = {source.label: sum(1 for row in report_rows if row.get("source") == source.label) for source in active_sources}
    counts = {
        "playlist_channels": len(entries),
        "matched": sum(1 for row in report_rows if row["status"] == "matched"),
        "missing": sum(1 for row in report_rows if row["status"] == "missing"),
        "programmes": programme_count,
        "compatibility_ids_and_name_aliases": compatibility_alias_count,
        "timing_seconds": timings, "selected_by_source": source_counts,
    }

    (OUTPUT_DIR / "report.json").write_text(json.dumps({"summary": counts, "source_errors": source_errors, "channels": report_rows}, ensure_ascii=False, indent=2) + "\n", "utf-8")
    (OUTPUT_DIR / "validation.json").write_text(json.dumps({"generated_at": now.isoformat(), "active_sources": [s.label for s in active_sources], "summary": counts, "channels": validations}, ensure_ascii=False, indent=2) + "\n", "utf-8")

    report_md = [
        "# Relatório do EPG Universal — Filtro Baseado em Nome Ativo", "",
        f"- Canais lidos nas listas M3U (Após VOD Inteligente): **{counts['playlist_channels']}**",
        f"- Canais com grade encontrada: **{counts['matched']}**",
        f"- Total de programas gravados: **{counts['programmes']}**",
        "", "## Fonte base utilizada por canal", ""
    ]
    for source in active_sources: report_md.append(f"- **{source.label}:** {source_counts[source.label]} canal(is)")
    report_md.extend(["", "## Desempenho", ""])
    for label, seconds in timings.items(): report_md.append(f"- **{label.replace('_', ' ').title()}:** {seconds:.2f} s")
    report_md.append(f"- **Tempo Total:** {time.perf_counter() - run_start:.2f} s")
    (OUTPUT_DIR / "report.md").write_text("\n".join(report_md) + "\n", "utf-8")
    
    val_md = ["# Validação e Consenso de Grade\n"]
    for v in validations:
        now_title = v['now']['title'] if v.get('now') else 'Sem programação'
        val_md.append(f"- **{v['name']}** ({v['source']}): Agora **{now_title}** - Confiança **{v['confidence']}%**")
    (OUTPUT_DIR / "validation.md").write_text("\n".join(val_md) + "\n", "utf-8")

    print(f"Sucesso! EPG gerado com {programme_count} programas unificados de {len(active_sources)} fontes.")

if __name__ == "__main__":
    main()
