#!/usr/bin/env python3
"""Mescla Genius + Open-EPG por nome e valida a grade com até 4 fontes.

Genius é a fonte principal, Open-EPG Brazil 4 é o único fallback de saída.
EPGShare BR1 e IPTV-EPG BR são fontes independentes de coerência/validação.
"""
from __future__ import annotations

import copy
import gzip
import io
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from epg_utils import digit_signature, normalize_name, normalize_source_id

PRIMARY_URL = os.environ.get(
    "PRIMARY_EPG_URL",
    "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz",
)
SECONDARY_URL = os.environ.get(
    "SECONDARY_EPG_URL",
    "https://www.open-epg.com/files/brazil4.xml",
)
TERTIARY_URL = os.environ.get(
    "TERTIARY_EPG_URL",
    "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
)
QUATERNARY_URL = os.environ.get(
    "QUATERNARY_EPG_URL",
    "https://iptv-epg.org/files/epg-br.xml",
)
CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.90"))
FUZZY_MARGIN = float(os.environ.get("FUZZY_MARGIN", "0.035"))


@dataclass
class EpgSource:
    label: str
    root: ET.Element
    channels: dict[str, ET.Element]
    names: dict[str, list[str]]
    programs: dict[str, list[ET.Element]]
    exact_index: dict[str, set[str]]


def download(url: str, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "EPG-Automatico/1.0 (+GitHub Actions)",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - queremos registrar a falha real
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 4)
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")


def decode_xml(payload: bytes) -> bytes:
    if payload.startswith(b"\x1f\x8b"):
        return gzip.decompress(payload)
    return payload


def local_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def read_source(label: str, url: str) -> EpgSource:
    payload = decode_xml(download(url))
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

    return EpgSource(label, root, channels, names, dict(programs), dict(exact_index))


def read_optional_source(label: str, url: str) -> tuple[EpgSource | None, str | None]:
    """Baixa uma fonte de validação sem impedir a geração do EPG se ela falhar."""
    try:
        return read_source(label, url), None
    except Exception as exc:  # noqa: BLE001 - registramos a falha no relatório
        return None, str(exc)


def aliases_for(entry: dict[str, object]) -> list[str]:
    values: list[str] = [str(entry.get("name", ""))]
    values.extend(str(x) for x in entry.get("aliases", []) or [])
    # O ID antigo é só uma pista extra, usada depois dos nomes visíveis.
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
        if not candidates:
            continue
        # Em caso de nomes repetidos, prefere o canal com mais programas.
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
            # Canais curtos, como H2, TNT e TLC, só entram por igualdade exata.
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
    reason = f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"
    return best[1], best[0], second_score, reason


def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None]:
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if not isinstance(item, dict):
        return None, None
    source = str(item.get("source", "")).lower().strip()
    channel_id = str(item.get("channel_id", "")).strip()
    if source in {"primary", "secondary"} and channel_id:
        return source, channel_id
    return None, None


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
    """Converte timestamps XMLTV (YYYYmmddHHMMSS +ZZZZ) para datetime ciente de fuso."""
    if not value:
        return None
    value = value.strip()
    # XMLTV normalmente traz segundos, mas algumas fontes omitem.
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
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
    # Remove marcadores comuns que criam divergência sem indicar grade diferente.
    value = re.sub(r"\b(?:HD|FHD|AO VIVO|LIVE|ESTREIA|INEDITO|INÉDITO)\b", " ", value)
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def programme_title_similarity(a: str, b: str) -> float:
    a = normalize_programme_title(a)
    b = normalize_programme_title(b)
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
            continue
        if start > now:
            next_item = {"start": start, "stop": stop, "title": title}
            break
    return current, next_item


def alternate_match(source: EpgSource, aliases: list[str]) -> tuple[str | None, str]:
    exact_id, reason = choose_exact(source, aliases)
    if exact_id:
        return exact_id, reason
    fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
    if fuzzy_id and best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN:
        return fuzzy_id, fuzzy_reason
    return None, ""


def cross_source_programme_agreement(
    chosen_source: EpgSource,
    chosen_id: str,
    other_source: EpgSource,
    other_id: str,
) -> dict[str, object]:
    """Compara títulos em horários equivalentes entre as duas fontes.

    Isto não prova qual fonte está certa; serve para detectar divergências grandes,
    que são úteis para flagrar um canal mapeado para o EPG errado.
    """
    a_rows = programme_rows(chosen_source, chosen_id)
    b_rows = programme_rows(other_source, other_id)
    if not a_rows or not b_rows:
        return {"comparable": 0, "agreement": None, "average_similarity": None}

    pairs: list[float] = []
    # Comparamos somente programas que começam em horários próximos (até 15 min).
    j = 0
    for a_start, _a_stop, a_title in a_rows:
        while j + 1 < len(b_rows) and b_rows[j + 1][0] <= a_start:
            j += 1
        candidates = []
        for idx in (j - 1, j, j + 1):
            if 0 <= idx < len(b_rows):
                b_start, _b_stop, b_title = b_rows[idx]
                delta = abs((b_start - a_start).total_seconds())
                if delta <= 15 * 60:
                    candidates.append((delta, b_title))
        if candidates:
            _, b_title = min(candidates, key=lambda x: x[0])
            pairs.append(programme_title_similarity(a_title, b_title))

    if not pairs:
        return {"comparable": 0, "agreement": None, "average_similarity": None}
    agreed = sum(1 for score in pairs if score >= 0.72)
    return {
        "comparable": len(pairs),
        "agreement": round(agreed / len(pairs), 4),
        "average_similarity": round(sum(pairs) / len(pairs), 4),
    }


def validate_selection(
    entry: dict[str, object],
    source: EpgSource,
    source_id: str,
    validation_sources: list[EpgSource],
    now: datetime,
) -> dict[str, object]:
    aliases = aliases_for(entry)
    rows = programme_rows(source, source_id)
    current, next_item = now_next_for(source, source_id, now)

    invalid_durations = 0
    overlaps = 0
    gaps = 0
    previous_stop: datetime | None = None
    for start, stop, _title in rows:
        duration = stop - start
        if duration.total_seconds() <= 0 or duration > timedelta(hours=12):
            invalid_durations += 1
        if previous_stop is not None:
            if start < previous_stop - timedelta(seconds=30):
                overlaps += 1
            elif start > previous_stop + timedelta(minutes=30):
                gaps += 1
        if previous_stop is None or stop > previous_stop:
            previous_stop = stop

    future_end = max((stop for start, stop, _ in rows if stop > now), default=None)
    future_hours = round(max(0.0, (future_end - now).total_seconds() / 3600), 1) if future_end else 0.0

    comparisons: list[dict[str, object]] = []
    for other_source in validation_sources:
        if other_source is source:
            continue
        other_id, other_method = alternate_match(other_source, aliases)
        comparison: dict[str, object] = {
            "other_source": other_source.label,
            "other_channel_id": other_id,
            "other_match_method": other_method,
            "comparable": 0,
            "agreement": None,
            "average_similarity": None,
            "vote": "sem_dados",
        }
        if other_id:
            comparison.update(cross_source_programme_agreement(source, source_id, other_source, other_id))
            comparable = int(comparison.get("comparable") or 0)
            agreement = comparison.get("agreement")
            if comparable >= 3 and isinstance(agreement, float):
                # O voto é relativamente permissivo; alertas fortes continuam usando
                # faixas mais rígidas abaixo. Isso tolera pequenas diferenças de título.
                comparison["vote"] = "concorda" if agreement >= 0.55 else "diverge"
        comparisons.append(comparison)

    # A própria fonte escolhida conta como um voto. Só entram no denominador as
    # fontes independentes com ao menos 3 horários realmente comparáveis.
    supporting_votes = 1
    opposing_votes = 0
    vote_sources = 1
    comparable_source_agreements: list[float] = []
    for comp in comparisons:
        if comp.get("vote") == "concorda":
            supporting_votes += 1
            vote_sources += 1
            comparable_source_agreements.append(float(comp["agreement"]))
        elif comp.get("vote") == "diverge":
            opposing_votes += 1
            vote_sources += 1
            comparable_source_agreements.append(float(comp["agreement"]))

    available_matches = 1 + sum(1 for comp in comparisons if comp.get("other_channel_id"))
    consensus_ratio = supporting_votes / vote_sources if vote_sources else 1.0
    # 4/4 = 100. Poucas fontes disponíveis recebem leve penalidade, para não
    # tratar 1/1 como tão forte quanto quatro fontes concordando.
    coverage_factor = 0.70 + 0.30 * min(vote_sources, 4) / 4
    confidence = round(100 * consensus_ratio * coverage_factor)

    warnings: list[str] = []
    severity = "ok"
    if not rows:
        warnings.append("nenhum programa na fonte escolhida")
        severity = "suspeito"
    if current is None:
        warnings.append("sem programa cobrindo o horário atual")
        if severity == "ok":
            severity = "atencao"
    if next_item is None:
        warnings.append("sem próximo programa disponível")
        if severity == "ok":
            severity = "atencao"
    if future_hours < 6:
        warnings.append(f"pouca programação futura ({future_hours:.1f} h)")
        if severity == "ok":
            severity = "atencao"
    if invalid_durations:
        warnings.append(f"{invalid_durations} programa(s) com duração inválida/suspeita")
        severity = "suspeito"
    if overlaps >= 3:
        warnings.append(f"{overlaps} sobreposições de horários")
        severity = "suspeito"
    if gaps >= 4:
        warnings.append(f"{gaps} lacunas maiores que 30 min")
        if severity == "ok":
            severity = "atencao"

    # Alertas por fonte individual. Com 3 árbitros independentes, uma divergência
    # isolada vira atenção; duas ou mais fontes contra a escolhida viram suspeita.
    divergent_labels: list[str] = []
    weak_labels: list[str] = []
    for comp in comparisons:
        comparable = int(comp.get("comparable") or 0)
        agreement = comp.get("agreement")
        if comparable < 5 or not isinstance(agreement, float):
            continue
        if agreement < 0.30:
            divergent_labels.append(str(comp.get("other_source")))
        elif agreement < 0.55:
            weak_labels.append(str(comp.get("other_source")))

    if opposing_votes >= 2 or len(divergent_labels) >= 2:
        warnings.append(
            "a fonte escolhida diverge de múltiplas fontes independentes: "
            + ", ".join(sorted(set(divergent_labels or [
                str(c.get("other_source")) for c in comparisons if c.get("vote") == "diverge"
            ])))
        )
        severity = "suspeito"
    elif opposing_votes == 1 or divergent_labels:
        label = (divergent_labels or [
            str(c.get("other_source")) for c in comparisons if c.get("vote") == "diverge"
        ])[0]
        warnings.append(f"grade diverge de {label}; conferir a votação das outras fontes")
        if severity == "ok":
            severity = "atencao"
    elif weak_labels:
        warnings.append("concordância parcial com " + ", ".join(sorted(set(weak_labels))))
        if severity == "ok":
            severity = "atencao"

    # Se temos 3+ fontes votando e a escolhida não tem maioria, é sinal forte.
    if vote_sources >= 3 and supporting_votes <= opposing_votes:
        warnings.append(
            f"sem maioria para a fonte escolhida ({supporting_votes}/{vote_sources} votos favoráveis)"
        )
        severity = "suspeito"

    def serialize_item(item: dict | None) -> dict | None:
        if item is None:
            return None
        return {
            "title": item["title"],
            "start": item["start"].isoformat(),
            "stop": item["stop"].isoformat(),
        }

    return {
        "target_id": str(entry["id"]),
        "name": str(entry["name"]),
        "source": source.label,
        "source_channel_id": source_id,
        "status": severity,
        "warnings": warnings,
        "programmes": len(rows),
        "future_hours": future_hours,
        "overlaps": overlaps,
        "gaps_over_30m": gaps,
        "now": serialize_item(current),
        "next": serialize_item(next_item),
        "consensus": {
            "supporting_votes": supporting_votes,
            "opposing_votes": opposing_votes,
            "vote_sources": vote_sources,
            "available_channel_matches": available_matches,
            "max_sources": len(validation_sources),
            "confidence": confidence,
        },
        "cross_sources": comparisons,
    }

def fmt_local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        # America/Maceio não tem horário de verão atualmente; o relatório usa UTC-03.
        local = dt.astimezone(timezone(timedelta(hours=-3)))
        return local.strftime("%d/%m %H:%M")
    except ValueError:
        return value

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog_data = json.loads(CATALOG_PATH.read_text("utf-8"))
    entries = catalog_data.get("channels", [])
    overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}

    print("Baixando EPG principal...")
    primary = read_source("Genius", PRIMARY_URL)
    print(f"Genius: {len(primary.channels)} canais")
    print("Baixando EPG secundário (Open-EPG Brazil 4)...")
    secondary = read_source("Open-EPG", SECONDARY_URL)
    print(f"Open-EPG: {len(secondary.channels)} canais")

    optional_source_errors: dict[str, str] = {}
    print("Baixando 3ª fonte de validação (EPGShare BR1)...")
    tertiary, tertiary_error = read_optional_source("EPGShare BR1", TERTIARY_URL)
    if tertiary is not None:
        print(f"EPGShare BR1: {len(tertiary.channels)} canais")
    else:
        optional_source_errors["EPGShare BR1"] = tertiary_error or "falha desconhecida"
        print(f"AVISO: EPGShare BR1 indisponível: {tertiary_error}")

    print("Baixando 4ª fonte de validação (IPTV-EPG BR)...")
    quaternary, quaternary_error = read_optional_source("IPTV-EPG BR", QUATERNARY_URL)
    if quaternary is not None:
        print(f"IPTV-EPG BR: {len(quaternary.channels)} canais")
    else:
        optional_source_errors["IPTV-EPG BR"] = quaternary_error or "falha desconhecida"
        print(f"AVISO: IPTV-EPG BR indisponível: {quaternary_error}")

    validation_sources = [primary, secondary]
    if tertiary is not None:
        validation_sources.append(tertiary)
    if quaternary is not None:
        validation_sources.append(quaternary)

    output_root = ET.Element("tv", {
        "generator-info-name": "EPG automático por nome",
        "source-info-name": "Genius principal + Open-EPG Brazil 4 fallback; EPGShare/IPTV-EPG validadores",
    })
    report: list[dict[str, object]] = []
    selected_channels: list[tuple[dict[str, object], EpgSource, str, str, float]] = []

    for entry in entries:
        target_id = str(entry["id"])
        visible_name = str(entry["name"])
        aliases = aliases_for(entry)
        override_source, override_id = manual_override(overrides, target_id)

        chosen_source: EpgSource | None = None
        chosen_id: str | None = None
        method = ""
        score = 0.0

        if override_source and override_id:
            source = primary if override_source == "primary" else secondary
            if override_id in source.channels:
                chosen_source, chosen_id = source, override_id
                method, score = "override manual", 1.0
            else:
                method = f"override inválido: {override_source}/{override_id}"

        if chosen_id is None:
            # O Genius é realmente a fonte principal: tentamos correspondência
            # exata E aproximada nele antes de consultar o Open-EPG. Assim um
            # nome ligeiramente diferente no Genius não perde para um nome exato
            # existente no fallback.
            exact_id, exact_reason = choose_exact(primary, aliases)
            if exact_id:
                chosen_source, chosen_id = primary, exact_id
                method, score = exact_reason, 1.0

        if chosen_id is None:
            fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(primary, aliases)
            if (
                fuzzy_id
                and best_score >= FUZZY_THRESHOLD
                and (best_score - second_score) >= FUZZY_MARGIN
            ):
                chosen_source, chosen_id = primary, fuzzy_id
                method, score = fuzzy_reason, best_score

        if chosen_id is None:
            exact_id, exact_reason = choose_exact(secondary, aliases)
            if exact_id:
                chosen_source, chosen_id = secondary, exact_id
                method, score = exact_reason, 1.0

        if chosen_id is None:
            fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(secondary, aliases)
            if (
                fuzzy_id
                and best_score >= FUZZY_THRESHOLD
                and (best_score - second_score) >= FUZZY_MARGIN
            ):
                chosen_source, chosen_id = secondary, fuzzy_id
                method, score = fuzzy_reason, best_score

        if chosen_source is not None and chosen_id is not None:
            selected_channels.append((entry, chosen_source, chosen_id, method, score))
            status = "principal" if chosen_source is primary else "fallback"
            report.append({
                "target_id": target_id,
                "name": visible_name,
                "status": status,
                "source": chosen_source.label,
                "source_channel_id": chosen_id,
                "method": method,
                "score": round(score, 4),
                "programmes": len(chosen_source.programs.get(chosen_id, [])),
            })
        else:
            report.append({
                "target_id": target_id,
                "name": visible_name,
                "status": "missing",
                "source": None,
                "source_channel_id": None,
                "method": method or "sem correspondência segura",
                "score": 0.0,
                "programmes": 0,
            })

    # Primeiro todas as definições de canal; depois toda a programação.
    for entry, source, source_id, _, _ in selected_channels:
        target_id = str(entry["id"])
        channel = copy.deepcopy(source.channels[source_id])
        channel.attrib["id"] = target_id
        add_display_name_first(channel, str(entry["name"]))
        output_root.append(channel)

    programme_count = 0
    for entry, source, source_id, _, _ in selected_channels:
        target_id = str(entry["id"])
        for original in source.programs.get(source_id, []):
            programme = copy.deepcopy(original)
            programme.attrib["channel"] = target_id
            output_root.append(programme)
            programme_count += 1

    # Validação: estrutura, Agora/Próximo e votação independente entre até 4 fontes.
    validation_now = datetime.now(timezone.utc)
    validations: list[dict[str, object]] = []
    for entry, source, source_id, _, _ in selected_channels:
        validations.append(validate_selection(entry, source, source_id, validation_sources, validation_now))

    xml_payload = ET.tostring(output_root, encoding="utf-8", xml_declaration=True)
    xml_path = OUTPUT_DIR / "epg.xml"
    gz_path = OUTPUT_DIR / "epg.xml.gz"
    xml_path.write_bytes(xml_payload)
    with gz_path.open("wb") as raw_file:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_file, mtime=0, compresslevel=9) as gz_file:
            gz_file.write(xml_payload)

    counts = {
        "playlist_channels": len(entries),
        "matched_primary": sum(1 for x in report if x["status"] == "principal"),
        "matched_fallback": sum(1 for x in report if x["status"] == "fallback"),
        "missing": sum(1 for x in report if x["status"] == "missing"),
        "programmes": programme_count,
        "validation_ok": sum(1 for x in validations if x["status"] == "ok"),
        "validation_attention": sum(1 for x in validations if x["status"] == "atencao"),
        "validation_suspicious": sum(1 for x in validations if x["status"] == "suspeito"),
    }
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps({"summary": counts, "channels": report}, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )

    (OUTPUT_DIR / "validation.json").write_text(
        json.dumps({
            "generated_at": validation_now.isoformat(),
            "summary": {
                "ok": counts["validation_ok"],
                "attention": counts["validation_attention"],
                "suspicious": counts["validation_suspicious"],
                "active_sources": [src.label for src in validation_sources],
                "optional_source_errors": optional_source_errors,
            },
            "channels": validations,
        }, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )

    validation_md = [
        "# Validação da programação",
        "",
        "> A programação escolhida vem apenas de Genius/Open-EPG. EPGShare BR1 e IPTV-EPG BR atuam como árbitros independentes; quanto mais fontes concordarem, maior a confiança.",
        "",
        f"Gerado em: **{validation_now.astimezone(timezone(timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')} (Maceió)**",
        f"Fontes ativas nesta execução: **{', '.join(src.label for src in validation_sources)}**",
        "",
        f"- ✅ OK: **{counts['validation_ok']}**",
        f"- ⚠️ Atenção: **{counts['validation_attention']}**",
        f"- 🚨 Suspeito: **{counts['validation_suspicious']}**",
        "",
    ]
    if optional_source_errors:
        validation_md.append("## Fontes de validação indisponíveis")
        validation_md.append("")
        for label, error in optional_source_errors.items():
            validation_md.append(f"- ⚠️ **{label}:** {error}")
        validation_md.append("")
    validation_md.extend([
        "## Canais que merecem conferência",
        "",
    ])
    flagged = [x for x in validations if x["status"] != "ok"]
    flagged.sort(key=lambda x: (0 if x["status"] == "suspeito" else 1, str(x["name"])))
    for item in flagged:
        icon = "🚨" if item["status"] == "suspeito" else "⚠️"
        current = item.get("now") or {}
        nxt = item.get("next") or {}
        validation_md.append(
            f"### {icon} {item['name']} — {item['source']} / `{item['source_channel_id']}`"
        )
        validation_md.append("")
        validation_md.append(
            f"- **Agora:** {current.get('title', '—')} ({fmt_local_time(current.get('start'))}–{fmt_local_time(current.get('stop'))})"
        )
        validation_md.append(
            f"- **Próximo:** {nxt.get('title', '—')} ({fmt_local_time(nxt.get('start'))}–{fmt_local_time(nxt.get('stop'))})"
        )
        consensus = item.get("consensus") or {}
        validation_md.append(
            f"- **Votação:** {consensus.get('supporting_votes', 1)}/{consensus.get('vote_sources', 1)} fontes comparáveis apoiam a grade escolhida · confiança **{consensus.get('confidence', 0)}%**"
        )
        for comp in item.get("cross_sources", []) or []:
            if comp.get("other_channel_id"):
                agreement = comp.get("agreement")
                vote = comp.get("vote")
                vote_icon = "✅" if vote == "concorda" else ("❌" if vote == "diverge" else "➖")
                if isinstance(agreement, float):
                    validation_md.append(
                        f"- {vote_icon} **{comp.get('other_source')}:** `{comp.get('other_channel_id')}` — {agreement*100:.0f}% em {comp.get('comparable')} horários"
                    )
                else:
                    validation_md.append(
                        f"- ➖ **{comp.get('other_source')}:** `{comp.get('other_channel_id')}` (sem horários suficientes para votar)"
                    )
            else:
                validation_md.append(f"- ➖ **{comp.get('other_source')}:** canal não encontrado com segurança")
        for warning in item.get("warnings", []):
            validation_md.append(f"- **Alerta:** {warning}")
        validation_md.append("")
    if not flagged:
        validation_md.append("Nenhum alerta automático nesta execução.")
        validation_md.append("")

    validation_md.extend(["## Agora / Próximo — todos os canais", ""])
    for item in sorted(validations, key=lambda x: str(x["name"])):
        current = item.get("now") or {}
        nxt = item.get("next") or {}
        icon = "✅" if item["status"] == "ok" else ("🚨" if item["status"] == "suspeito" else "⚠️")
        consensus = item.get("consensus") or {}
        validation_md.append(
            f"- {icon} **{item['name']}** ({item['source']}): "
            f"Agora **{current.get('title', '—')}** → Próximo **{nxt.get('title', '—')}** · "
            f"votos **{consensus.get('supporting_votes', 1)}/{consensus.get('vote_sources', 1)}** · confiança **{consensus.get('confidence', 0)}%**"
        )
    (OUTPUT_DIR / "validation.md").write_text("\n".join(validation_md) + "\n", "utf-8")

    markdown = [
        "# Relatório do EPG automático",
        "",
        f"- Canais do catálogo: **{counts['playlist_channels']}**",
        f"- Encontrados no Genius: **{counts['matched_primary']}**",
        f"- Adicionados pelo Open-EPG: **{counts['matched_fallback']}**",
        f"- Sem correspondência segura: **{counts['missing']}**",
        f"- Programas gravados: **{counts['programmes']}**",
        f"- Validação: ✅ **{counts['validation_ok']}** OK · ⚠️ **{counts['validation_attention']}** atenção · 🚨 **{counts['validation_suspicious']}** suspeitos",
        "",
        "Veja **validation.md** para Agora/Próximo, votação de até 4 fontes e os alertas de programação.",
        "",
        "## Fallback Open-EPG",
        "",
    ]
    fallback_rows = [x for x in report if x["status"] == "fallback"]
    markdown.extend(
        f"- `{x['target_id']}` — {x['name']} → `{x['source_channel_id']}` ({x['method']})"
        for x in fallback_rows
    )
    if not fallback_rows:
        markdown.append("Nenhum canal precisou do fallback.")
    markdown.extend(["", "## Sem EPG", ""])
    missing_rows = [x for x in report if x["status"] == "missing"]
    markdown.extend(f"- `{x['target_id']}` — {x['name']}" for x in missing_rows)
    if not missing_rows:
        markdown.append("Todos os canais foram encontrados.")
    markdown.extend(["", "## Correspondências aproximadas", ""])
    fuzzy_rows = [x for x in report if str(x["method"]).startswith("aproximação")]
    markdown.extend(
        f"- {x['name']} → `{x['source_channel_id']}` no {x['source']} ({x['method']})"
        for x in fuzzy_rows
    )
    if not fuzzy_rows:
        markdown.append("Nenhuma aproximação foi necessária.")
    (OUTPUT_DIR / "report.md").write_text("\n".join(markdown) + "\n", "utf-8")

    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
