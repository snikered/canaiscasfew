#!/usr/bin/env python3
"""Coletor otimizado de EPG (paralelo com Session Keep-Alive e UTC exato)."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DIAS_PARA_FRENTE = 4
MAX_WORKERS = 10
OUTPUT_FILE = Path(os.environ.get("WEBFINAL_OUTPUT", "epg_webfinal.xml"))
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    "https://webfinal.com.br/programacao/tv-aberta",
    "https://webfinal.com.br/programacao/filmes",
    "https://webfinal.com.br/programacao/series",
    "https://webfinal.com.br/programacao/futebol",
    "https://webfinal.com.br/programacao/esportes",
    "https://webfinal.com.br/programacao/variedades",
    "https://webfinal.com.br/programacao/noticias",
    "https://webfinal.com.br/programacao/infantil",
    "https://webfinal.com.br/programacao/documentarios",
    "https://webfinal.com.br/programacao/catolica",
    "https://webfinal.com.br/programacao/evangelica",
    "https://webfinal.com.br/programacao/agronegocio",
    "https://webfinal.com.br/programacao/publicos",
    "https://webfinal.com.br/programacao/internacional",
]

_RE_ANO = re.compile(r"^(\d{4})\.\s*(.+)")
_RE_DUR_H_M = re.compile(r"(\d+)h\s*(\d+)min")
_RE_DUR_M = re.compile(r"(\d+)\s*min")
_RE_PROG_CARD = re.compile(r"program-card")


def criar_sessao() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    })
    return session


def _fetch_categoria(session: requests.Session, url_cat: str) -> list[tuple[str, str]]:
    encontrados: list[tuple[str, str]] = []
    try:
        r = session.get(url_cat, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for card in soup.find_all("article", class_="pfcx-card"):
            h3 = card.find("h3", class_="pfcx-canal")
            link = card.find("a", class_="pfcx-link")
            nome = h3.get_text(strip=True) if h3 else ""
            href = link.get("href", "").strip() if link else ""
            if nome and href:
                encontrados.append((nome, href))
    except Exception:
        pass
    return encontrados


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    canais: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(CATEGORIAS))) as executor:
        futures = [executor.submit(_fetch_categoria, session, url) for url in CATEGORIAS]
        for fut in as_completed(futures):
            for nome, href in fut.result():
                if nome not in canais:
                    canais[nome] = href
    return canais


def extrair_programacao(session: requests.Session, nome: str, url_canal: str, data_limite: datetime) -> tuple[str, list[dict]]:
    if url_canal.startswith("/"):
        url_canal = "https://webfinal.com.br" + url_canal

    try:
        r = session.get(url_canal, timeout=15)
        r.raise_for_status()
    except Exception:
        return nome, []

    soup = BeautifulSoup(r.content, "html.parser")
    eventos: list[dict] = []
    seen_starts: set[int] = set()

    for card in soup.find_all("div", class_=_RE_PROG_CARD):
        data_start = card.get("data-start", "")
        data_end = card.get("data-end", "")
        titulo_attr = card.get("data-title", "")

        if not data_start or not data_end:
            continue

        try:
            start_ms = int(data_start)
            end_ms = int(data_end)
            if end_ms <= start_ms or start_ms in seen_starts:
                continue
            inicio = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).astimezone(BRT_TZ)
            fim = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).astimezone(BRT_TZ)
        except Exception:
            continue

        if inicio > data_limite:
            continue

        h3 = card.find("h3")
        titulo = (h3.get_text(strip=True) if h3 else "") or titulo_attr.strip()
        if not titulo:
            continue

        seen_starts.add(start_ms)

        desc_elem = card.find("p", class_="descricao")
        descricao = desc_elem.get_text(strip=True) if desc_elem else ""

        ano = ""
        descricao_limpa = descricao
        match_ano = _RE_ANO.match(descricao)
        if match_ano:
            ano = match_ano.group(1)
            descricao_limpa = match_ano.group(2).strip()

        duracao_elem = card.find("span", class_="duration-badge")
        duracao_texto = duracao_elem.get_text(strip=True) if duracao_elem else ""

        duracao_minutos = ""
        if duracao_texto:
            match_duracao = _RE_DUR_H_M.match(duracao_texto)
            if match_duracao:
                duracao_minutos = str(int(match_duracao.group(1)) * 60 + int(match_duracao.group(2)))
            else:
                match_min = _RE_DUR_M.match(duracao_texto)
                if match_min:
                    duracao_minutos = match_min.group(1)

        eventos.append({
            "inicio": inicio,
            "fim": fim,
            "titulo": titulo,
            "ano": ano,
            "descricao": descricao_limpa,
            "duracao": duracao_minutos,
        })

    eventos.sort(key=lambda x: x["inicio"])
    return nome, eventos


def gerar_xml(caminho_saida: Path) -> None:
    agora_brt = datetime.now(BRT_TZ)
    data_limite = agora_brt + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    canais = descobrir_canais(session)
    print(f"Canais descobertos: {len(canais)}")

    resultados: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(extrair_programacao, session, nome, url, data_limite): nome
            for nome, url in canais.items()
        }
        for fut in as_completed(futures):
            nome, evs = fut.result()
            if evs:
                resultados[nome] = evs

    root = ET.Element("tv")
    root.set("generator-info-name", "EPG WebFinal")

    canais_ordenados = sorted(resultados.keys())

    # 1. Emite todos os <channel> primeiro (padrão XMLTV)
    for nome in canais_ordenados:
        channel = ET.SubElement(root, "channel", {"id": nome})
        display = ET.SubElement(channel, "display-name")
        display.text = nome

    # 2. Emite todos os <programme> depois
    total_eventos = 0
    for nome in canais_ordenados:
        for ev in resultados[nome]:
            prog = ET.SubElement(
                root,
                "programme",
                {
                    "start": ev["inicio"].strftime("%Y%m%d%H%M%S -0300"),
                    "stop": ev["fim"].strftime("%Y%m%d%H%M%S -0300"),
                    "channel": nome,
                },
            )
            title = ET.SubElement(prog, "title")
            title.text = ev["titulo"]

            if ev["ano"]:
                date = ET.SubElement(prog, "date")
                date.text = ev["ano"]

            if ev["descricao"]:
                desc = ET.SubElement(prog, "desc")
                desc.text = ev["descricao"]

            if ev["duracao"]:
                length = ET.SubElement(prog, "length", {"units": "minutes"})
                length.text = ev["duracao"]

            total_eventos += 1

    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)

    print(f"Canais processados com grade: {len(resultados)}/{len(canais)}")
    print(f"Eventos extraídos: {total_eventos}")
    print(f"XML salvo em: {caminho_saida.resolve()}")


if __name__ == "__main__":
    gerar_xml(OUTPUT_FILE)