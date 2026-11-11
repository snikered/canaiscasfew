#!/usr/bin/env python3
"""Coletor otimizado TVPlus (paralelo, cache de metadados de filmes e fuso -0300)."""
from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DIAS_PARA_FRENTE = 4
MAX_WORKERS = 8
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    "https://www.tvplus.com.br/categoria/documentario",
    "https://www.tvplus.com.br/categoria/esportes",
    "https://www.tvplus.com.br/categoria/filmes",
    "https://www.tvplus.com.br/categoria/kids",
    "https://www.tvplus.com.br/categoria/musica",
    "https://www.tvplus.com.br/categoria/noticias",
    "https://www.tvplus.com.br/categoria/series",
    "https://www.tvplus.com.br/categoria/tvaberta",
    "https://www.tvplus.com.br/categoria/variedades",
]

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

_RE_DIA = re.compile(r"^([A-Za-zÀ-ÿ\-]+),\s*(\d+)\s*de\s*([A-Za-zÀ-ÿ]+)")
_RE_HORA = re.compile(r"(\d{2})h(\d{2})\s*-\s*(\d{2})h(\d{2})\s*\|\s*(.+)")
_RE_META_FILME = re.compile(r"([^/]+)\s*/\s*([^/]+)\s*/\s*(\d{4})\s*/\s*(\d+)\s*Min", re.IGNORECASE)
_RE_CLASSIF = re.compile(r"(\d{1,2})\.png")

_FILME_CACHE: dict[str, tuple[str, str, str, str]] = {}
_CACHE_LOCK = threading.Lock()


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    })
    return s


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    canais: dict[str, str] = {}
    for url_cat in CATEGORIAS:
        try:
            r = session.get(url_cat, timeout=15)
            soup = BeautifulSoup(r.content, "html.parser")
            for bloco in soup.find_all("div", class_="program"):
                link = bloco.find("a", href=re.compile(r"/programacao/"))
                if link:
                    nome_elem = link.find("img")
                    nome = (nome_elem.get("alt", "") if nome_elem else link.get_text()).strip()
                    url_canal = link["href"].strip()
                    if nome and nome not in canais:
                        canais[nome] = url_canal
        except Exception:
            pass
    return canais


def buscar_detalhes_filme(session: requests.Session, url_prog: str) -> tuple[str, str, str, str]:
    with _CACHE_LOCK:
        if url_prog in _FILME_CACHE:
            return _FILME_CACHE[url_prog]

    titulo_original, ano_lancamento, pais, duracao = "", "", "", ""
    try:
        r_prog = session.get(url_prog, timeout=8)
        if r_prog.status_code == 200:
            soup_prog = BeautifulSoup(r_prog.content, "html.parser")
            main_info = soup_prog.find("div", class_="main-info-sinopse")
            if main_info:
                linhas_meta = [l.strip() for l in main_info.get_text().split("\n") if l.strip()]
                if len(linhas_meta) >= 2:
                    titulo_original = linhas_meta[1]
                if len(linhas_meta) >= 3:
                    match_meta = _RE_META_FILME.match(linhas_meta[2])
                    if match_meta:
                        pais = match_meta.group(2).strip()
                        ano_lancamento = match_meta.group(3)
                        duracao = match_meta.group(4)
    except Exception:
        pass

    res = (titulo_original, ano_lancamento, pais, duracao)
    with _CACHE_LOCK:
        _FILME_CACHE[url_prog] = res
    return res


def extrair_programacao(session: requests.Session, nome: str, url_canal: str, data_limite: datetime, hoje: datetime) -> tuple[str, list[dict]]:
    if url_canal.startswith("/"):
        url_canal = "https://www.tvplus.com.br" + url_canal

    try:
        r = session.get(url_canal, timeout=15)
        if r.status_code != 200:
            return nome, []
    except Exception:
        return nome, []

    soup = BeautifulSoup(r.content, "html.parser")
    eventos: list[dict] = []
    data_atual: datetime | None = None

    for bloco in soup.find_all("div", class_="program"):
        info = bloco.find("div", class_="program-info")
        if not info:
            continue

        texto_info = info.get_text().strip()
        match_dia = _RE_DIA.match(texto_info)
        if match_dia:
            mes_nome = match_dia.group(3).lower()
            mes_num = MESES.get(mes_nome, 0)
            if mes_num:
                ano = hoje.year + 1 if (mes_num == 1 and hoje.month == 12) else hoje.year
                try:
                    data_atual = datetime(ano, mes_num, int(match_dia.group(2)), tzinfo=BRT_TZ)
                except ValueError:
                    data_atual = None
            continue

        titulo_elem = info.find("a", class_="black")
        if not titulo_elem or not data_atual:
            continue

        titulo = titulo_elem.get_text().strip()
        url_prog = titulo_elem.get("href", "").strip()
        if url_prog.startswith("/"):
            url_prog = "https://www.tvplus.com.br" + url_prog

        tem_ao_vivo = info.find("span", class_="live") is not None
        classificacao = ""
        img_class = info.find("img", src=re.compile(r"/classificacao/"))
        if img_class and (match_class := _RE_CLASSIF.search(img_class["src"])):
            classificacao = match_class.group(1)

        details = info.find("div", class_="details")
        details_texto = details.get_text().strip() if details else ""
        match_hora = _RE_HORA.match(details_texto)
        if not match_hora:
            continue

        h1, m1, h2, m2 = map(int, match_hora.groups()[:4])
        genero_bruto = match_hora.group(5).strip()

        inicio = data_atual.replace(hour=h1, minute=m1, second=0, microsecond=0)
        fim = data_atual.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if fim <= inicio:
            fim += timedelta(days=1)

        if inicio > data_limite:
            continue

        categorias = [p.strip() for p in genero_bruto.split(",") if p.strip()][:2] if genero_bruto else []
        titulo_original, ano_lancamento, pais, duracao = "", "", "", ""
        if "Filme" in categorias and url_prog:
            titulo_original, ano_lancamento, pais, duracao = buscar_detalhes_filme(session, url_prog)

        titulo_final = f"Ao vivo - {titulo}" if tem_ao_vivo and not titulo.lower().startswith("ao vivo") else titulo
        eventos.append({
            "inicio": inicio,
            "fim": fim,
            "titulo": titulo_final,
            "classificacao": classificacao,
            "categorias": categorias,
            "titulo_original": titulo_original,
            "ano": ano_lancamento,
            "pais": pais,
            "duracao": duracao,
        })

    eventos.sort(key=lambda x: x["inicio"])
    return nome, eventos


def gerar_xml(caminho_saida: str | Path) -> None:
    hoje = datetime.now(BRT_TZ)
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    canais = descobrir_canais(session)

    resultados: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(extrair_programacao, session, nome, url, data_limite, hoje)
            for nome, url in canais.items()
        ]
        for fut in as_completed(futures):
            nome, evs = fut.result()
            if evs:
                resultados[nome] = evs

    root = ET.Element("tv", {"generator-info-name": "EPG TVPlus"})
    nomes_ordenados = sorted(resultados.keys())

    for nome in nomes_ordenados:
        channel = ET.SubElement(root, "channel", id=nome)
        ET.SubElement(channel, "display-name").text = nome

    total_eventos = 0
    for nome in nomes_ordenados:
        for ev in resultados[nome]:
            prog = ET.SubElement(
                root,
                "programme",
                start=ev["inicio"].strftime("%Y%m%d%H%M%S -0300"),
                stop=ev["fim"].strftime("%Y%m%d%H%M%S -0300"),
                channel=nome,
            )
            ET.SubElement(prog, "title").text = ev["titulo"]
            if ev["titulo_original"] and ev["titulo_original"].upper() != ev["titulo"].upper():
                ET.SubElement(prog, "sub-title").text = ev["titulo_original"]
            for cat in ev["categorias"]:
                ET.SubElement(prog, "category").text = cat
            if ev["ano"]:
                ET.SubElement(prog, "date").text = ev["ano"]
            if ev["duracao"]:
                ET.SubElement(prog, "length", units="minutes").text = ev["duracao"]
            if ev["pais"]:
                ET.SubElement(prog, "country").text = ev["pais"]
            if ev["classificacao"]:
                rating = ET.SubElement(prog, "rating")
                ET.SubElement(rating, "value").text = ev["classificacao"]
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"TVPlus -> Canais: {len(resultados)}/{len(canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_tvplus.xml")
    gerar_xml(caminho)
