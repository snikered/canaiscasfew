#!/usr/bin/env python3
"""Normalização universal de nomes de canais.

Regra principal: o NOME é a identidade. tvg-id antigo é somente compatibilidade.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from difflib import SequenceMatcher

SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
QUALITY_TOKENS = {
    "HD", "FHD", "FULLHD", "FULL", "UHD", "4K", "8K", "SD",
    "CHANNEL",  # regra solicitada: tratado como qualidade/complemento
    "H264", "H265", "H266", "HEVC", "AVC",
    "HDR", "HDR10", "HDR10PLUS", "DV", "DOLBYVISION",
    "FPS", "50FPS", "60FPS", "30FPS", "25FPS",
    "VIP", "BACKUP", "BKP", "ALT", "TESTE", "TEST", "RAW",
}


def remove_superscript_markers(value: str) -> str:
    """Remove ¹²³⁴... usados como marcador de cópia; números normais ficam."""
    return (value or "").translate(str.maketrans("", "", SUPERSCRIPT_DIGITS))


def strip_quality_markers(value: str) -> str:
    """Remove marcadores de qualidade/codec sem apagar números reais.

    Remove, entre outros: HDR+, SD, [H265], FHD [H265], H.265, HD, FHD,
    UHD, 4K, 8K, HEVC, AVC e FPS.
    """
    value = html.unescape(remove_superscript_markers(value or ""))
    value = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value)

    # Colchetes: [H265], [H.265], [FHD], [HDR+], [60 FPS] etc.
    value = re.sub(
        r"\[\s*(?:H\s*\.?\s*26[456]|HEVC|AVC|HD|FHD|FULL\s*HD|UHD|4K|8K|SD|HDR(?:10)?\s*\+?|HDR10\s*PLUS|\d+\s*FPS)\s*\]",
        " ", value, flags=re.IGNORECASE,
    )
    # Codecs fora de colchetes: H265 / H.265 / H 265.
    value = re.sub(r"\bH\s*\.?\s*26[456]\b", " H265 ", value, flags=re.IGNORECASE)
    # HDR+ / HDR10+ antes da tokenização.
    value = re.sub(r"\bHDR\s*10\s*\+", " HDR10PLUS ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bHDR\s*\+", " HDR ", value, flags=re.IGNORECASE)

    parts = re.split(r"([\s._/|:+-]+)", value.strip())
    kept: list[str] = []
    skip_next_hd = False
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[\s._/|:+-]+", part):
            kept.append(part)
            continue
        clean = re.sub(r"[^A-Za-z0-9]+", "", part).upper()
        if clean == "FULL":
            skip_next_hd = True
            continue
        if skip_next_hd and clean == "HD":
            skip_next_hd = False
            continue
        skip_next_hd = False
        if clean in QUALITY_TOKENS or re.fullmatch(r"\d{2,3}FPS", clean):
            continue
        kept.append(part)

    value = "".join(kept)
    value = re.sub(r"[\s._/|:+-]+", " ", value)
    return value.strip(" -_|:/.\t\r\n")


def _basic_letters(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.upper().strip()


def normalize_name(value: str) -> str:
    """Gera chave canônica baseada no nome do canal.

    Regras conservadoras:
    - remove ¹²³⁴, HD/FHD/HDR+/SD/[H265]/FHD [H265]/4K etc.;
    - preserva números reais: HBO != HBO 2, SporTV 2 != SporTV 3;
    - & / AND (e 'E' entre palavras) são equivalentes;
    - A&E / A&amp;E.br / A and E são equivalentes;
    - Canal Sony = Sony;
    - SporTV = SporTV 1;
    - H2 = History 2;
    - USA = USA Network.
    """
    value = _basic_letters(strip_quality_markers(value))

    # Limpa sufixo .br que às vezes vem no ID/nome da fonte.
    value = re.sub(r"(?:[._/-]|\s)+BR$", "", value)

    # A&E merece regra dedicada por ser muito curto.
    ae_probe = re.sub(r"[._/-]+", " ", value)
    ae_probe = re.sub(r"\s+", " ", ae_probe).strip()
    if re.fullmatch(r"A\s*(?:&|AND|E)\s*E", ae_probe):
        return "AANDE"

    # Conjunções. 'E' só vira AND quando está entre partes de um nome maior;
    # o canal E!/E isolado não é alterado.
    value = value.replace("&", " AND ")
    value = re.sub(r"\bAND\b", " AND ", value)
    if len(value.split()) >= 3:
        value = re.sub(r"\bE\b", " AND ", value)

    value = re.sub(r"[^A-Z0-9]+", "", value)

    # Aliases universais conhecidos e deliberadamente restritos.
    alias_map = {
        "CANALSONY": "SONY",
        "SONYCHANNEL": "SONY",
        "SPORTV": "SPORTV1",
        "H2": "HISTORY2",
        "HISTORYII": "HISTORY2",
        "USA": "USANETWORK",
        "PARAMOUNTCHANNEL": "PARAMOUNT",
    }
    return alias_map.get(value, value)


def normalize_source_id(value: str) -> str:
    """Normaliza ID de provedor como pista de nome, nunca como verdade absoluta."""
    value = html.unescape(value or "")
    value = re.sub(r"\s*\((?:M3U4U|SRC\d+)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"(?:^|[./_-])(?:BR|BRAZIL)(?:$|[./_-])", " ", value, flags=re.I)
    value = re.sub(r"\.(?:BR|COM|NET|ORG)$", "", value, flags=re.I)
    return normalize_name(value)


def digit_signature(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", normalize_name(value)))


def similarity(left: str, right: str) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b or digit_signature(a) != digit_signature(b):
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


def stable_channel_id(name: str, used: set[str] | None = None) -> str:
    """ID interno determinístico criado do nome, independente do tvg-id original."""
    norm = normalize_name(name).lower() or "canal"
    slug = re.sub(r"[^a-z0-9]+", ".", norm).strip(".")
    candidate = f"auto.{slug}"
    if used is None or candidate not in used:
        return candidate
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{candidate}.{digest}"
