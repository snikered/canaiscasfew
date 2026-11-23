#!/usr/bin/env python3
"""Coletor otimizado TVMap (Todos os Canais, janelas de 3h em paralelo para 4 dias,
fuso BRT -0300 e XMLTV com lang="pt").
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DIAS_PARA_FRENTE = 4
MAX_WORKERS = 6
BRT_TZ = timezone(timedelta(hours=-3))

MESES_ABREV = {
    1: "jan", 2: "fev", 3: "mar", 4: "abr",
    5: "mai", 6: "jun", 7: "jul", 8: "ago",
    9: "set", 10: "out", 11: "nov", 12: "dez",
}


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })
    return s


def gerar_urls_janelas(hoje: datetime, dias: int) -> list[str]:
    """Gera URLs de 3 em 3 horas para os próximos `dias` dias."""
    urls: list[str] = []
    for d in range(dias):
        data = hoje + timedelta(days=d)
        data_str = f"{data.day:02d}-{MESES_ABREV[data.month]}-{data.year}"
        for hora in range(0, 24, 3):
            urls.append(f"https://tvmap.com.br/api/Programacao/{data_str}/{hora}.00hs?availWidth=2560")
    return urls


def buscar_janela(session: requests.Session, url: str) -> list[dict]:
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200 or not r.text.strip():
            return []
        return r.json().get("channels", [])
    except Exception:
        return []


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
    urls = gerar_urls_janelas(hoje, DIAS_PARA_FRENTE)
    print(f"TVMap -> Consultando {len(urls)} janelas de 3h ({DIAS_PARA_FRENTE} dias)...")

    todos_canais: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(buscar_janela, session, u) for u in urls]
        for fut in as_completed(futures):
            for ch in fut.result():
                nome = (ch.get("displayName") or "").strip()
                if not nome:
                    continue
                todos_canais.setdefault(nome, []).extend(ch.get("exhibitions", []))

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

    nomes_ordenados = sorted(resultados.keys(), key=str.lower)
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
            ET.SubElement(p, "title", {"lang": "pt"}).text = titulo
            if desc_texto:
                ET.SubElement(p, "desc", {"lang": "pt"}).text = desc_texto
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"TVMap -> Canais: {len(resultados)}/{len(todos_canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_tvmap.xml")
    gerar_xml(caminho)
