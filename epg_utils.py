#!/usr/bin/env python3
"""Funções compartilhadas para normalização de nomes de canais."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
QUALITY_TOKENS = {
    "HD", "FHD", "FULLHD", "FULL", "UHD", "4K", "8K", "SD",
    "H264", "H265", "HEVC", "AVC", "HDR", "HDR10", "DV",
    "FPS", "50FPS", "60FPS", "30FPS", "25FPS",
    "VIP", "BACKUP", "BKP", "ALT", "TESTE", "TEST", "RAW",
}


def remove_superscript_markers(value: str) -> str:
    """Remove apenas algarismos sobrescritos; números normais são preservados."""
    return value.translate(str.maketrans("", "", SUPERSCRIPT_DIGITS))


def strip_quality_markers(value: str) -> str:
    value = remove_superscript_markers(value)
    # Marcadores de cópia no fim, como "Discovery HD (2)".
    value = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value)
    # Remove colchetes quando usados para codec/qualidade.
    value = re.sub(
        r"\[\s*(?:H\.?26[45]|HEVC|AVC|HD|FHD|UHD|4K|8K|HDR\+?|\d+\s*FPS)\s*\]",
        " ", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"HDR\+", " HDR ", value, flags=re.IGNORECASE)
    tokens = re.split(r"\s+", value.strip())
    kept: list[str] = []
    skip_next_hd = False
    for token in tokens:
        clean = re.sub(r"[^A-Za-z0-9]+", "", token).upper()
        if clean == "FULL":
            skip_next_hd = True
            continue
        if skip_next_hd and clean == "HD":
            skip_next_hd = False
            continue
        skip_next_hd = False
        if clean in QUALITY_TOKENS or re.fullmatch(r"\d{2,3}FPS", clean):
            continue
        kept.append(token)
    return re.sub(r"\s+", " ", " ".join(kept)).strip(" -_|:/")


def normalize_name(value: str) -> str:
    """Normalização forte para comparação, preservando números reais como HBO 2."""
    value = strip_quality_markers(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper().replace("&", " E ")
    value = re.sub(r"[^A-Z0-9]+", "", value)
    return value


def normalize_source_id(value: str) -> str:
    value = re.sub(r"\s*\((?:M3U4U|SRC\d+)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"(?:^|[./_-])(?:BR|BRAZIL)(?:$|[./_-])", " ", value, flags=re.I)
    value = re.sub(r"\.(?:BR|COM|NET|ORG)$", "", value, flags=re.I)
    return normalize_name(value)


def digit_signature(value: str) -> tuple[str, ...]:
    """Distingue HBO de HBO 2 e mantém canais numerados separados."""
    return tuple(re.findall(r"\d+", normalize_name(value)))


def similarity(left: str, right: str) -> float:
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b or digit_signature(a) != digit_signature(b):
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Bônus pequeno quando um nome contém o outro, mas sem cruzar numeração.
    if min(len(a), len(b)) >= 5 and (a in b or b in a):
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


def stable_channel_id(name: str, used: set[str] | None = None) -> str:
    norm = normalize_name(name).lower() or "canal"
    slug = re.sub(r"[^a-z0-9]+", ".", norm).strip(".")
    candidate = f"auto.{slug}"
    if used is None or candidate not in used:
        return candidate
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{candidate}.{digest}"
