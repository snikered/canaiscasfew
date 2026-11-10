#!/usr/bin/env python3
"""Garante que cada nome de canal (display-name) pertença a UM único canal do EPG.

Problema que resolve
--------------------
Players que casam por nome (quando o tvg-id da lista está errado ou vazio) procuram o
tvg-name da lista entre os <display-name> do XMLTV. Se o mesmo nome aparece em dois
canais (ex.: "Globo EPTV Campinas FHD" em "Globo EPTV Campinas" E em "Globo São Paulo"),
o player escolhe um deles de forma arbitrária e a afiliada local recebe a grade errada.

Regra de dono (da mais forte para a mais fraca)
-----------------------------------------------
1. O canal cujo próprio ID/nome é igual ao nome (ignorando HD/FHD/SD/[H265]/(BR2)/²).
2. O canal mais específico (menos nomes distintos). Empate: ordem alfabética do ID.
"""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Iterable

_QUALITY = re.compile(
    r"\b(SD|HD|FHD|UHD|4K|8K|H\.?264|H\.?265|HEVC|BR2|P)\b[\*\+]*", re.IGNORECASE
)


def name_key(value: str) -> str:
    """Chave de comparação: sem acento, sem qualidade/tags, mantém o conteúdo de ( ).

    Diferente de strip_qualities_keep_case(), NÃO apaga o texto entre parênteses:
    "GLOBO PE (ASA BRANCA) HD" -> "GLOBO PE ASA BRANCA" (e não "GLOBO PE").
    """
    s = html.unescape(str(value or ""))
    s = re.sub(r"[¹²³⁴]+", " ", s)  # sobrescritos de variantes (ex.: "HD ²") antes do NFKD
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"\[[^\]]*\]", " ", s)          # [H265], [1], [P] ...
    s = re.sub(r"[¹²³⁴*]+", " ", s)
    s = s.replace("&", " AND ")
    s = re.sub(r"[()\-–—_./|,:;]", " ", s)
    s = _QUALITY.sub(" ", s)
    s = re.sub(r"[^A-Z0-9+ ]", " ", s)
    return " ".join(s.split())


def dedupe_display_names(
    names_by_id: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Devolve novo dict {id: [nomes]} em que cada name_key tem um único dono."""
    owners: dict[str, list[str]] = {}
    bases_per_id: dict[str, set[str]] = {}
    for cid, names in names_by_id.items():
        bases = {name_key(n) for n in names if name_key(n)}
        bases_per_id[cid] = bases
        for b in bases:
            owners.setdefault(b, []).append(cid)

    winner: dict[str, str] = {}
    for base, ids in owners.items():
        if len(ids) == 1:
            winner[base] = ids[0]
            continue
        exact = sorted(i for i in ids if name_key(i) == base)
        if exact:
            winner[base] = exact[0]
            continue
        winner[base] = sorted(ids, key=lambda i: (len(bases_per_id[i]), i))[0]

    out: dict[str, list[str]] = {}
    for cid, names in names_by_id.items():
        kept, seen = [], set()
        for n in names:
            b = name_key(n)
            if not b or winner.get(b) == cid:
                if n not in seen:
                    kept.append(n)
                    seen.add(n)
        out[cid] = kept
    return out


def collisions(names_by_id: dict[str, Iterable[str]]) -> dict[str, list[str]]:
    """Diagnóstico: {name_key: [ids que o reivindicam]} só para os conflitantes."""
    owners: dict[str, set[str]] = {}
    for cid, names in names_by_id.items():
        for n in names:
            b = name_key(n)
            if b:
                owners.setdefault(b, set()).add(cid)
    return {b: sorted(v) for b, v in owners.items() if len(v) > 1}
