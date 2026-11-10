#!/usr/bin/env python3
"""Corrige os tvg-id de uma lista M3U usando o tvg-name como verdade.

Para cada canal da lista procura o tvg-name entre os nomes (display-name) do EPG
publicado e grava no tvg-id o ID do canal correspondente. Assim o player não precisa
"decidir" entre ID e nome: o ID já aponta para a grade certa (Globo Asa Branca, SBT,
Record, Band locais etc.).

Uso:
    python fix_m3u_ids.py --m3u lista.m3u --epg epg.xml.gz --out lista_corrigida.m3u
    python fix_m3u_ids.py --m3u lista.m3u --epg https://.../epg.xml.gz

Regras (da mais segura para a menos segura; só aceita se o resultado for ÚNICO):
  1. nome exato (sem HD/FHD/SD/[H265]/(BR2)/²), ignorando acentos e maiúsculas;
  2. mesmo conjunto de palavras em outra ordem ("RBS PASSO FUNDO GLOBO" = "GLOBO RBS TV PASSO FUNDO");
  3. sem correspondência: tvg-id existente é mantido só se existir no EPG; ID que não
     existe no EPG (ex.: globosp.br usado em 179 canais) é limpo, para não forçar a
     grade de São Paulo em afiliadas. Use --keep-unknown-ids para manter.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from name_dedupe import dedupe_display_names, name_key  # noqa: E402

_STOP = {"TV", "DE", "DO", "DA", "DOS", "DAS", "E"}


def token_key(value: str) -> frozenset[str]:
    return frozenset(t for t in name_key(value).split() if t not in _STOP)


def read_bytes(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=120) as r:
            return r.read()
    return Path(src).read_bytes()


def load_epg(src: str) -> dict[str, list[str]]:
    raw = read_bytes(src)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    names: dict[str, list[str]] = {}
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "channel":
            cid = el.get("id") or ""
            names[cid] = [cid] + [d.text for d in el.findall("display-name") if d.text]
            el.clear()
    return names


def build_indexes(names: dict[str, list[str]]):
    ded = dedupe_display_names(names)  # funciona mesmo com EPG antigo, ainda não deduplicado
    exact: dict[str, set[str]] = defaultdict(set)
    tokens: dict[frozenset, set[str]] = defaultdict(set)
    for cid, ns in ded.items():
        for n in ns:
            k = name_key(n)
            if k:
                exact[k].add(cid)
            t = token_key(n)
            if t:
                tokens[t].add(cid)
    return exact, tokens


def resolve(tvg_name: str, exact, tokens) -> tuple[str | None, str]:
    c = exact.get(name_key(tvg_name), set())
    if len(c) == 1:
        return next(iter(c)), "nome exato"
    c = tokens.get(token_key(tvg_name), set())
    if len(c) == 1:
        return next(iter(c)), "mesmas palavras"
    return None, "ambíguo" if c or exact.get(name_key(tvg_name)) else "sem correspondência"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--m3u", required=True)
    ap.add_argument("--epg", required=True, help="epg.xml.gz local ou URL")
    ap.add_argument("--out", help="padrão: <lista>.corrigida.m3u")
    ap.add_argument("--keep-unknown-ids", action="store_true",
                    help="não limpa tvg-id que não existe no EPG quando não há correspondência")
    ap.add_argument("--map-csv", help="grava tvg-name;tvg-id;método para conferência")
    a = ap.parse_args()

    names = load_epg(a.epg)
    exact, tokens = build_indexes(names)
    epg_ids = set(names)

    lines = Path(a.m3u).read_text(encoding="utf-8", errors="ignore").splitlines()
    out, rows = [], []
    stats = defaultdict(int)
    unresolved = []
    for line in lines:
        if not line.startswith("#EXTINF"):
            out.append(line)
            continue
        m_name = re.search(r'tvg-name="([^"]*)"', line)
        disp = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        tvg_name = html.unescape(m_name.group(1)) if m_name else html.unescape(disp)
        m_id = re.search(r'tvg-id="([^"]*)"', line)
        old = html.unescape(m_id.group(1)) if m_id else ""

        new, how = resolve(tvg_name, exact, tokens)
        if new is None:
            if old and old in epg_ids:
                new, how = old, "mantido (existe no EPG)"
            elif old and a.keep_unknown_ids:
                new, how = old, "mantido (--keep-unknown-ids)"
            else:
                new, how = "", f"{how}; id antigo {'limpo' if old else 'vazio'}"
                unresolved.append(tvg_name)
        stats[how.split(";")[0]] += 1
        attr = f'tvg-id="{html.escape(new, quote=True)}"'
        if m_id:
            line = line[: m_id.start()] + attr + line[m_id.end():]
        else:
            line = re.sub(r"^(#EXTINF:[^\s]+)", r"\1 " + attr.replace("\\", "\\\\"), line, count=1)
        out.append(line)
        rows.append((tvg_name, old, new, how))

    dest = Path(a.out) if a.out else Path(a.m3u).with_suffix(".corrigida.m3u")
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    if a.map_csv:
        with open(a.map_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["tvg-name", "tvg-id antigo", "tvg-id novo", "método"])
            w.writerows(rows)

    print(f"Canais na lista: {len(rows)} | canais no EPG: {len(epg_ids)}")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print(f"Arquivo gerado: {dest}")
    if unresolved:
        print(f"\nSem grade encontrada ({len(unresolved)}), exemplos: {', '.join(unresolved[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
