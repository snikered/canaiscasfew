#!/usr/bin/env python3
"""Coletor completo e otimizado WebFinal (esportes-e-futebol, logos, gêneros, elenco/diretor, lxml e BRT -0300)."""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

DIAS_PARA_FRENTE = 4
MAX_WORKERS = 16
BASE_URL = "https://webfinal.com.br"
OUTPUT_FILE = Path(
    os.environ.get(
        "WEBFINAL_OUTPUT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_webfinal.xml"),
    )
)
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    f"{BASE_URL}/programacao/tv-aberta/",
    f"{BASE_URL}/programacao/filmes/",
    f"{BASE_URL}/programacao/series/",
    f"{BASE_URL}/programacao/esportes-e-futebol/",
    f"{BASE_URL}/programacao/variedades/",
    f"{BASE_URL}/programacao/noticias/",
    f"{BASE_URL}/programacao/infantil/",
    f"{BASE_URL}/programacao/documentarios/",
    f"{BASE_URL}/programacao/catolica/",
    f"{BASE_URL}/programacao/evangelica/",
    f"{BASE_URL}/programacao/agronegocio/",
    f"{BASE_URL}/programacao/publicos/",
    f"{BASE_URL}/programacao/internacional/",
]

_RE_ANO = re.compile(r"^(\d{4})\.\s*(.+)", re.DOTALL)
_RE_PROG_CARD = re.compile(r"program-card")
_RE_GENEROS = re.compile(r"^g[eê]neros?\s*:\s*(.+)$", re.IGNORECASE)
_RE_ELENCO = re.compile(r"^elenco\s*:\s*(.+)$", re.IGNORECASE)
_RE_DIRETOR = re.compile(r"^diretor(?:es|a|as)?\s*:\s*(.+)$", re.IGNORECASE)
_SINOPSES_IGNORAR = {
    "sinopse não disponível",
    "sinopse nao disponivel",
    "sem sinopse",
    "sem descrição",
    "sem descricao",
}


def criar_sessao() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS * 2,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    })
    return session


def _normalizar_url(url: str) -> str:
    url = url.strip()
    if not url or url.startswith("data:"):
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def _extrair_lista_csv(texto: str, limite: int = 8) -> list[str]:
    itens: list[str] = []
    vistos: set[str] = set()
    for parte in texto.split(","):
        item = " ".join(parte.split()).strip(" .;")
        if not item:
            continue
        chave = item.lower()
        if chave not in vistos:
            vistos.add(chave)
            itens.append(item)
            if len(itens) >= limite:
                break
    return itens


def _fetch_categoria(session: requests.Session, url_cat: str) -> list[tuple[str, str, str]]:
    encontrados: list[tuple[str, str, str]] = []
    try:
        r = session.get(url_cat, timeout=(5, 12))
        if r.status_code != 200:
            return encontrados
        soup = BeautifulSoup(r.content, HTML_PARSER)
        for card in soup.find_all("article", class_="pfcx-card"):
            h3 = card.find("h3", class_="pfcx-canal")
            link = card.find("a", class_="pfcx-link")
            nome = " ".join(h3.get_text().split()) if h3 else ""
            href = _normalizar_url(link.get("href", "")) if link else ""
            if not nome or not href:
                continue

            icon = ""
            img = card.find("img")
            if img:
                for attr in ("data-src", "data-lazy-src", "src"):
                    cand = _normalizar_url(str(img.get(attr, "")))
                    if cand:
                        icon = cand
                        break

            encontrados.append((nome, href, icon))
    except Exception:
        pass
    return encontrados


def descobrir_canais(session: requests.Session) -> dict[str, dict[str, str]]:
    canais: dict[str, dict[str, str]] = {}
    urls_vistas: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(10, len(CATEGORIAS))) as executor:
        futures = [executor.submit(_fetch_categoria, session, url) for url in CATEGORIAS]
        for fut in as_completed(futures):
            for nome, href, icon in fut.result():
                if nome not in canais and href not in urls_vistas:
                    canais[nome] = {"url": href, "icon": icon}
                    urls_vistas.add(href)
                elif nome in canais and not canais[nome]["icon"] and icon:
                    canais[nome]["icon"] = icon
    return canais


def _extrair_metadados_card(card) -> tuple[list[str], list[str], list[str]]:
    generos: list[str] = []
    elenco: list[str] = []
    diretores: list[str] = []

    for elem in card.find_all(["p", "div", "li"]):
        if elem.find(["p", "div", "li"]):
            continue
        txt = " ".join(elem.get_text(" ").split())
        if not txt or ":" not in txt:
            continue

        if not generos and (m := _RE_GENEROS.match(txt)):
            generos = _extrair_lista_csv(m.group(1), limite=4)
        elif not elenco and (m := _RE_ELENCO.match(txt)):
            elenco = _extrair_lista_csv(m.group(1), limite=6)
        elif not diretores and (m := _RE_DIRETOR.match(txt)):
            diretores = _extrair_lista_csv(m.group(1), limite=3)

    return generos, elenco, diretores


def extrair_programacao(
    session: requests.Session,
    nome: str,
    url_canal: str,
    inicio_min: datetime,
    data_limite: datetime,
) -> tuple[str, list[dict]]:
    try:
        r = session.get(url_canal, timeout=(5, 15))
        if r.status_code != 200:
            return nome, []
    except Exception:
        return nome, []

    soup = BeautifulSoup(r.content, HTML_PARSER)
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

        if fim < inicio_min or inicio > data_limite:
            continue

        h3 = card.find("h3")
        titulo = " ".join(((h3.get_text() if h3 else "") or str(titulo_attr)).split())
        if not titulo:
            continue

        seen_starts.add(start_ms)

        desc_elem = card.find("p", class_="descricao")
        descricao = " ".join(desc_elem.get_text().split()) if desc_elem else ""

        ano = ""
        descricao_limpa = descricao
        if match_ano := _RE_ANO.match(descricao):
            ano = match_ano.group(1)
            descricao_limpa = match_ano.group(2).strip()

        if descricao_limpa.rstrip(".").lower() in _SINOPSES_IGNORAR or descricao_limpa.rstrip(".").lower() == titulo.rstrip(".").lower():
            descricao_limpa = ""

        generos, elenco, diretores = _extrair_metadados_card(card)
        duracao_min = max(1, (end_ms - start_ms) // 60000)

        eventos.append({
            "inicio": inicio,
            "fim": fim,
            "titulo": titulo,
            "ano": ano,
            "descricao": descricao_limpa,
            "generos": generos,
            "elenco": elenco,
            "diretores": diretores,
            "duracao": str(duracao_min),
        })

    eventos.sort(key=lambda x: x["inicio"])
    return nome, eventos


def gerar_xml(caminho_saida: Path) -> None:
    t0 = time.perf_counter()
    agora_brt = datetime.now(BRT_TZ)
    inicio_min = agora_brt - timedelta(hours=8)
    data_limite = agora_brt + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    canais = descobrir_canais(session)
    print(f"WebFinal ({HTML_PARSER}) -> Canais descobertos: {len(canais)}")

    resultados: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                extrair_programacao, session, nome, info["url"], inicio_min, data_limite
            ): nome
            for nome, info in canais.items()
        }
        for fut in as_completed(futures):
            nome, evs = fut.result()
            if evs:
                resultados[nome] = evs

    root = ET.Element("tv", {"generator-info-name": "EPG WebFinal"})
    canais_ordenados = sorted(resultados.keys(), key=str.lower)

    for nome in canais_ordenados:
        channel = ET.SubElement(root, "channel", {"id": nome})
        ET.SubElement(channel, "display-name").text = nome
        icon_url = canais[nome].get("icon")
        if icon_url:
            ET.SubElement(channel, "icon", {"src": icon_url})

    total_eventos = 0
    com_genero = 0
    com_creditos = 0

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
            ET.SubElement(prog, "title", {"lang": "pt"}).text = ev["titulo"]

            if ev["descricao"]:
                ET.SubElement(prog, "desc", {"lang": "pt"}).text = ev["descricao"]

            if ev["diretores"] or ev["elenco"]:
                credits_el = ET.SubElement(prog, "credits")
                for d in ev["diretores"]:
                    ET.SubElement(credits_el, "director").text = d
                for a in ev["elenco"]:
                    ET.SubElement(credits_el, "actor").text = a
                com_creditos += 1

            if ev["ano"]:
                ET.SubElement(prog, "date").text = ev["ano"]

            if ev["generos"]:
                for g in ev["generos"]:
                    ET.SubElement(prog, "category", {"lang": "pt"}).text = g
                com_genero += 1

            ET.SubElement(prog, "length", {"units": "minutes"}).text = ev["duracao"]
            total_eventos += 1

    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)

    elapsed = time.perf_counter() - t0
    print(
        f"WebFinal -> Canais com grade: {len(resultados)}/{len(canais)} | "
        f"Eventos: {total_eventos} | Com gênero: {com_genero} | "
        f"Com créditos: {com_creditos} | Tempo: {elapsed:.1f}s"
    )
    print(f"XML salvo em: {caminho_saida.resolve()}")


if __name__ == "__main__":
    gerar_xml(OUTPUT_FILE)
