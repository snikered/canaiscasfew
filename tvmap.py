#!/usr/bin/env python3
"""Coletor otimizado TVMap (preserva fuso ISO da API, deduplica grades e ordena XMLTV)."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DIAS_PARA_FRENTE = 4
BRT_TZ = timezone(timedelta(hours=-3))


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"})
    return s


def pegar_grade(session: requests.Session, url_grid: str | None = None) -> dict:
    url = (
        f"https://tvmap.com.br/api/Programacao?availWidth=2560&grid={url_grid}"
        if url_grid
        else "https://tvmap.com.br/api/Programacao?availWidth=2560"
    )
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def extrair_todos(session: requests.Session) -> dict[str, list[dict]]:
    todos_canais: dict[str, list[dict]] = {}
    url_grid = None
    seen_grids: set[str] = set()
    paginas = 0

    while paginas < 50:
        try:
            data = pegar_grade(session, url_grid)
        except Exception:
            break

        for ch in data.get("channels", []):
            nome = (ch.get("displayName") or "").strip()
            if not nome:
                continue
            todos_canais.setdefault(nome, []).extend(ch.get("exhibitions", []))

        next_grid = (data.get("nextGridUrl") or "").strip()
        if not next_grid or next_grid in seen_grids:
            break
        seen_grids.add(next_grid)
        url_grid = next_grid
        paginas += 1

    return todos_canais


def parse_iso_to_brt(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BRT_TZ)
        return dt.astimezone(BRT_TZ)
    except Exception:
        return None


def gerar_xml(caminho_saida: str | Path) -> None:
    hoje = datetime.now(BRT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    todos_canais = extrair_todos(session)

    root = ET.Element("tv", {"generator-info-name": "EPG TVMap"})
    resultados: dict[str, list[tuple[datetime, datetime, str, str]]] = {}

    for nome, exhibitions in todos_canais.items():
        seen_starts: set[datetime] = set()
        evs: list[tuple[datetime, datetime, str, str]] = []
        for ex in exhibitions:
            inicio = parse_iso_to_brt(ex.get("exhibitionStartDate", ""))
            fim = parse_iso_to_brt(ex.get("exhibitionCalculatedEndDate", ""))
            titulo = (ex.get("title") or "").strip()
            if not inicio or not fim or not titulo or fim <= inicio:
                continue
            if inicio < hoje or inicio > data_limite or inicio in seen_starts:
                continue
            seen_starts.add(inicio)
            evs.append((inicio, fim, titulo, (ex.get("description") or "").strip()))
        if evs:
            evs.sort(key=lambda x: x[0])
            resultados[nome] = evs

    nomes_ordenados = sorted(resultados.keys())
    for nome in nomes_ordenados:
        channel = ET.SubElement(root, "channel", id=nome)
        ET.SubElement(channel, "display-name").text = nome

    total_eventos = 0
    for nome in nomes_ordenados:
        for inicio, fim, titulo, desc_texto in resultados[nome]:
            p = ET.SubElement(
                root,
                "programme",
                start=inicio.strftime("%Y%m%d%H%M%S -0300"),
                stop=fim.strftime("%Y%m%d%H%M%S -0300"),
                channel=nome,
            )
            ET.SubElement(p, "title").text = titulo
            if desc_texto:
                ET.SubElement(p, "desc").text = desc_texto
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"TVMap -> Canais: {len(resultados)}/{len(todos_canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_tvmap.xml")
    gerar_xml(caminho)
