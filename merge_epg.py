#!/usr/bin/env python3
"""Mescla EPG Genius + Open-EPG por nome, com o primeiro como prioridade."""
from __future__ import annotations

import copy
import gzip
import io
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
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
    "https://www.open-epg.com/files/brazil1.xml",
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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog_data = json.loads(CATALOG_PATH.read_text("utf-8"))
    entries = catalog_data.get("channels", [])
    overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}

    print("Baixando EPG principal...")
    primary = read_source("Genius", PRIMARY_URL)
    print(f"Genius: {len(primary.channels)} canais")
    print("Baixando EPG secundário...")
    secondary = read_source("Open-EPG", SECONDARY_URL)
    print(f"Open-EPG: {len(secondary.channels)} canais")

    output_root = ET.Element("tv", {
        "generator-info-name": "EPG automático por nome",
        "source-info-name": "Genius principal + Open-EPG fallback",
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
            # Ordem intencional: exato no principal, exato no secundário,
            # aproximação no principal, aproximação no secundário.
            exact_id, exact_reason = choose_exact(primary, aliases)
            if exact_id:
                chosen_source, chosen_id = primary, exact_id
                method, score = exact_reason, 1.0
            else:
                exact_id, exact_reason = choose_exact(secondary, aliases)
                if exact_id:
                    chosen_source, chosen_id = secondary, exact_id
                    method, score = exact_reason, 1.0

        if chosen_id is None:
            for source in (primary, secondary):
                fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
                if (
                    fuzzy_id
                    and best_score >= FUZZY_THRESHOLD
                    and (best_score - second_score) >= FUZZY_MARGIN
                ):
                    chosen_source, chosen_id = source, fuzzy_id
                    method, score = fuzzy_reason, best_score
                    break

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
    }
    (OUTPUT_DIR / "report.json").write_text(
        json.dumps({"summary": counts, "channels": report}, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )

    markdown = [
        "# Relatório do EPG automático",
        "",
        f"- Canais do catálogo: **{counts['playlist_channels']}**",
        f"- Encontrados no Genius: **{counts['matched_primary']}**",
        f"- Adicionados pelo Open-EPG: **{counts['matched_fallback']}**",
        f"- Sem correspondência segura: **{counts['missing']}**",
        f"- Programas gravados: **{counts['programmes']}**",
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
