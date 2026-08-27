#!/usr/bin/env python3
"""EPG multi-fonte com seleção automática por consenso por canal.

Fontes padrão (ordem de preferência/desempate):
1. Genius / Curated
2. Open-EPG Brazil 4
3. EPGShare BR1
4. IPTV-EPG BR
5. EPGShare BR2

A fonte final pode mudar canal a canal. A seleção privilegia a programação que
recebe mais apoio independente das outras fontes. BR1 e BR2 continuam aparecendo
como dois votos no relatório, mas compartilham o peso da família EPGShare para
não contar o mesmo provedor duas vezes como se fosse totalmente independente.
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean
from typing import Iterable

from epg_utils import digit_signature, normalize_name, normalize_source_id

CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.90"))
FUZZY_MARGIN = float(os.environ.get("FUZZY_MARGIN", "0.035"))
AGREEMENT_THRESHOLD = float(os.environ.get("AGREEMENT_THRESHOLD", "0.55"))
MIN_COMPARABLE_PROGRAMMES = int(os.environ.get("MIN_COMPARABLE_PROGRAMMES", "3"))


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


@dataclass
class EpgSource:
    spec: SourceSpec
    root: ET.Element
    channels: dict[str, ET.Element]
    names: dict[str, list[str]]
    programs: dict[str, list[ET.Element]]
    exact_index: dict[str, set[str]]

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
    programs: dict[str, list[ET.Element]] = defaultdict(list)
    exact_index: dict[str, set[str]] = defaultdict(set)

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
            for candidate in [*display_names, channel_id, normalize_source_id(channel_id)]:
                normalized = normalize_name(candidate)
                if normalized:
                    exact_index[normalized].add(channel_id)
        elif tag == "programme":
            channel_id = child.attrib.get("channel", "").strip()
            if channel_id:
                programs[channel_id].append(child)

    return EpgSource(spec, root, channels, names, dict(programs), dict(exact_index))


def read_optional_source(spec: SourceSpec) -> tuple[EpgSource | None, str | None]:
    try:
        return read_source(spec), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def aliases_for(entry: dict[str, object]) -> list[str]:
    values: list[str] = [str(entry.get("name", ""))]
    values.extend(str(x) for x in entry.get("aliases", []) or [])
    values.extend(normalize_source_id(str(x)) for x in entry.get("old_tvg_ids", []) or [])
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_name(value)
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return output


def choose_exact(source: EpgSource, aliases: Iterable[str]) -> tuple[str | None, str]:
    for alias in aliases:
        candidates = source.exact_index.get(alias, set())
        if candidates:
            selected = max(candidates, key=lambda cid: len(source.programs.get(cid, [])))
            return selected, f"nome exato: {alias}"
    return None, ""


def best_fuzzy(source: EpgSource, aliases: list[str]) -> tuple[str | None, float, float, str]:
    if not aliases:
        return None, 0.0, 0.0, ""
    scored: list[tuple[float, str, str, str]] = []
    for channel_id, names in source.names.items():
        candidates = [*names, channel_id, normalize_source_id(channel_id)]
        best_for_channel = (0.0, "", "")
        for alias in aliases:
            # Evita aproximação perigosa para nomes curtos (H2, TNT, TLC, USA etc.).
            if len(alias) < 4:
                continue
            for candidate in candidates:
                normalized_candidate = normalize_name(candidate)
                if not normalized_candidate:
                    continue
                if digit_signature(alias) != digit_signature(normalized_candidate):
                    continue
                ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
                if ratio > best_for_channel[0]:
                    best_for_channel = (ratio, alias, normalized_candidate)
        if best_for_channel[0] > 0:
            scored.append((best_for_channel[0], channel_id, best_for_channel[1], best_for_channel[2]))
    if not scored:
        return None, 0.0, 0.0, ""
    scored.sort(reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return (
        best[1], best[0], second_score,
        f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}",
    )


def find_match(source: EpgSource, aliases: list[str]) -> ChannelMatch | None:
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
    return None


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


def parse_xmltv_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
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


def programme_rows(source: EpgSource, channel_id: str) -> list[tuple[datetime, datetime, str]]:
    rows: list[tuple[datetime, datetime, str]] = []
    for p in source.programs.get(channel_id, []):
        start = parse_xmltv_datetime(p.attrib.get("start", ""))
        stop = parse_xmltv_datetime(p.attrib.get("stop", ""))
        if start is None or stop is None:
            continue
        rows.append((start, stop, programme_title(p)))
    rows.sort(key=lambda x: x[0])
    return rows


def now_next_for(source: EpgSource, channel_id: str, now: datetime) -> tuple[dict | None, dict | None]:
    current = None
    next_item = None
    for start, stop, title in programme_rows(source, channel_id):
        if start <= now < stop:
            current = {"start": start, "stop": stop, "title": title}
        elif start > now and next_item is None:
            next_item = {"start": start, "stop": stop, "title": title}
            if current is not None:
                break
    return current, next_item


def cross_source_programme_agreement(
    a_source: EpgSource,
    a_id: str,
    b_source: EpgSource,
    b_id: str,
    now: datetime,
) -> dict[str, object]:
    """Compara títulos na janela atual/futura, tolerando pequenos deslocamentos."""
    window_start = now - timedelta(hours=6)
    window_end = now + timedelta(hours=36)
    a_rows = [r for r in programme_rows(a_source, a_id) if r[1] >= window_start and r[0] <= window_end]
    b_rows = [r for r in programme_rows(b_source, b_id) if r[1] >= window_start and r[0] <= window_end]
    if not a_rows or not b_rows:
        return {"comparable": 0, "agreement": None, "average_similarity": None}

    similarities: list[float] = []
    for a_start, a_stop, a_title in a_rows:
        best: tuple[float, float] | None = None  # (time score, title similarity)
        for b_start, b_stop, b_title in b_rows:
            if b_start > a_stop + timedelta(minutes=20):
                break
            if b_stop < a_start - timedelta(minutes=20):
                continue
            overlap = max(0.0, (min(a_stop, b_stop) - max(a_start, b_start)).total_seconds())
            shorter = max(1.0, min((a_stop - a_start).total_seconds(), (b_stop - b_start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_start - a_start).total_seconds())
            if overlap_ratio < 0.35 and start_delta > 20 * 60:
                continue
            time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (20 * 60)))
            title_score = programme_title_similarity(a_title, b_title)
            candidate = (time_score, title_score)
            if best is None or candidate > best:
                best = candidate
        if best is not None:
            similarities.append(best[1])

    if not similarities:
        return {"comparable": 0, "agreement": None, "average_similarity": None}
    agreed = sum(1 for score in similarities if score >= 0.68)
    return {
        "comparable": len(similarities),
        "agreement": round(agreed / len(similarities), 4),
        "average_similarity": round(mean(similarities), 4),
    }


def schedule_health(source: EpgSource, channel_id: str, now: datetime) -> dict[str, object]:
    rows = programme_rows(source, channel_id)
    current, next_item = now_next_for(source, channel_id, now)
    invalid = 0
    overlaps = 0
    gaps = 0
    previous_stop: datetime | None = None
    for start, stop, _ in rows:
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

    future_end = max((stop for _start, stop, _ in rows if stop > now), default=None)
    future_hours = max(0.0, (future_end - now).total_seconds() / 3600) if future_end else 0.0

    score = 0.0
    score += 0.35 if current else 0.0
    score += 0.15 if next_item else 0.0
    score += 0.30 * min(future_hours / 24.0, 1.0)
    score += 0.20 * min(len(rows) / 10.0, 1.0)
    score -= min(0.25, invalid * 0.08 + overlaps * 0.02 + max(0, gaps - 3) * 0.01)
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 4),
        "rows": len(rows),
        "current": current,
        "next": next_item,
        "future_hours": round(future_hours, 1),
        "invalid_durations": invalid,
        "overlaps": overlaps,
        "gaps": gaps,
    }


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
            confidence=confidence,
            comparisons=comparisons,
        ))

    # Consenso independente manda; a ordem das fontes só desempata evidência
    # equivalente. Isso permite trocar Genius por Open/EPGShare/IPTV por canal.
    def rank(ev: CandidateEvaluation) -> tuple[float, int, float, float, float, float, int]:
        return (
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
    health = schedule_health(chosen.match.source, chosen.match.channel_id, now)
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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog_data = json.loads(CATALOG_PATH.read_text("utf-8"))
    entries = catalog_data.get("channels", [])
    overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}

    active_sources: list[EpgSource] = []
    source_errors: dict[str, str] = {}
    for spec in SOURCE_SPECS:
        print(f"Baixando {spec.priority}ª fonte: {spec.label}...")
        source, error = read_optional_source(spec)
        if source is not None:
            active_sources.append(source)
            print(f"{spec.label}: {len(source.channels)} canais")
        else:
            source_errors[spec.label] = error or "falha desconhecida"
            print(f"AVISO: {spec.label} indisponível: {error}")

    if not active_sources:
        raise RuntimeError("Nenhuma das cinco fontes de EPG pôde ser carregada.")

    source_by_key = {source.spec.key: source for source in active_sources}
    max_active_vote_weight = sum(source.spec.vote_weight for source in active_sources)
    now = datetime.now(timezone.utc)

    output_root = ET.Element("tv", {
        "generator-info-name": "EPG automático por consenso de 5 fontes",
        "source-info-name": "Seleção canal a canal por consenso: Genius, Open-EPG, EPGShare BR1/BR2 e IPTV-EPG",
    })

    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]] = []
    report_rows: list[dict[str, object]] = []
    validations: list[dict[str, object]] = []

    for entry in entries:
        target_id = str(entry["id"])
        visible_name = str(entry["name"])
        aliases = aliases_for(entry)
        matches = [m for source in active_sources if (m := find_match(source, aliases)) is not None]

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

    # Canais primeiro, programas depois.
    for entry, chosen, _evaluations, _forced in selected:
        target_id = str(entry["id"])
        channel = copy.deepcopy(chosen.match.source.channels[chosen.match.channel_id])
        channel.attrib["id"] = target_id
        add_display_name_first(channel, str(entry["name"]))
        output_root.append(channel)

    programme_count = 0
    for entry, chosen, _evaluations, _forced in selected:
        target_id = str(entry["id"])
        for original in chosen.match.source.programs.get(chosen.match.channel_id, []):
            programme = copy.deepcopy(original)
            programme.attrib["channel"] = target_id
            output_root.append(programme)
            programme_count += 1

    xml_payload = ET.tostring(output_root, encoding="utf-8", xml_declaration=True)
    (OUTPUT_DIR / "epg.xml").write_bytes(xml_payload)
    with (OUTPUT_DIR / "epg.xml.gz").open("wb") as raw_file:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_file, mtime=0, compresslevel=9) as gz_file:
            gz_file.write(xml_payload)

    source_counts = {
        source.label: sum(1 for row in report_rows if row.get("source") == source.label)
        for source in active_sources
    }
    counts = {
        "playlist_channels": len(entries),
        "matched": sum(1 for row in report_rows if row["status"] == "matched"),
        "missing": sum(1 for row in report_rows if row["status"] == "missing"),
        "programmes": programme_count,
        "selection_changed_by_consensus": sum(
            1 for row in report_rows if row.get("changed_from_first_available")
        ),
        "validation_ok": sum(1 for item in validations if item["status"] == "ok"),
        "validation_attention": sum(1 for item in validations if item["status"] == "atencao"),
        "validation_suspicious": sum(1 for item in validations if item["status"] == "suspeito"),
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
        "# Relatório do EPG automático — consenso multi-fonte",
        "",
        f"- Canais do catálogo: **{counts['playlist_channels']}**",
        f"- Encontrados: **{counts['matched']}**",
        f"- Sem EPG seguro: **{counts['missing']}**",
        f"- Canais cuja fonte mudou por consenso: **{counts['selection_changed_by_consensus']}**",
        f"- Programas gravados: **{counts['programmes']}**",
        "",
        "## Fonte escolhida por canal",
        "",
    ]
    for source in active_sources:
        report_md.append(f"- **{source.label}:** {source_counts[source.label]} canal(is)")
    if source_errors:
        report_md.extend(["", "## Fontes indisponíveis nesta execução", ""])
        report_md.extend(f"- ⚠️ **{label}:** {error}" for label, error in source_errors.items())

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
        "> A fonte final pode mudar para cada canal. A grade com maior apoio independente é escolhida; a ordem Genius → Open → EPGShare BR1 → IPTV-EPG → EPGShare BR2 só desempata evidência equivalente.",
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

    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
