#!/usr/bin/env python3
"""Coletor otimizado MeuGuia (paralelo, correção de meia-noite e fuso -0300)."""
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
MAX_WORKERS = 8
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    "https://meuguia.tv/programacao/categoria/Filmes",
    "https://meuguia.tv/programacao/categoria/Series",
    "https://meuguia.tv/programacao/categoria/Esportes",
    "https://meuguia.tv/programacao/categoria/Infantil",
    "https://meuguia.tv/programacao/categoria/Variedades",
    "https://meuguia.tv/programacao/categoria/Documentarios",
    "https://meuguia.tv/programacao/categoria/Noticias",
    "https://meuguia.tv/programacao/categoria/Aberta",
]

_RE_DIA = re.compile(r"^(\w+(?:-feira)?),\s*(\d+)/(\d+)", re.IGNORECASE)
_RE_HORA = re.compile(r"^(\d{2}:\d{2})$")


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"})
    return s


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    canais: dict[str, str] = {}
    for url_cat in CATEGORIAS:
        try:
            r = session.get(url_cat, timeout=15)
            soup = BeautifulSoup(r.content, "html.parser")
            for a in soup.find_all("a", href=True):
                if "/programacao/canal/" in a["href"]:
                    nome = a.get_text().strip().split("\n")[0].strip()
                    if nome and nome not in canais:
                        canais[nome] = a["href"].strip()
        except Exception:
            pass
    return canais


def extrair_programacao(session: requests.Session, nome: str, url_canal: str, data_limite: datetime, hoje: datetime) -> tuple[str, list[dict]]:
    if url_canal.startswith("/"):
        url_canal = "https://meuguia.tv" + url_canal
    try:
        r = session.get(url_canal, timeout=15)
        if r.status_code != 200:
            return nome, []
    except Exception:
        return nome, []

    linhas = [l.strip() for l in BeautifulSoup(r.content, "html.parser").get_text().split("\n") if l.strip()]
    eventos: list[dict] = []
    data_atual: datetime | None = None
    ultimo_inicio: datetime | None = None

    for i, linha in enumerate(linhas):
        match_dia = _RE_DIA.match(linha)
        if match_dia:
            dia_num, mes_num = int(match_dia.group(2)), int(match_dia.group(3))
            ano = hoje.year + 1 if (mes_num == 1 and hoje.month == 12) else hoje.year
            try:
                data_atual = datetime(ano, mes_num, dia_num, tzinfo=BRT_TZ)
                ultimo_inicio = None
            except ValueError:
                data_atual = None
            continue

        match_hora = _RE_HORA.match(linha)
        if match_hora and data_atual:
            h, m = map(int, match_hora.group(1).split(":"))
            inicio = data_atual.replace(hour=h, minute=m, second=0, microsecond=0)
            # Se o horário recuou na mesma seção do dia (ex: 23:45 -> 00:15), virou a meia-noite
            if ultimo_inicio and inicio < ultimo_inicio - timedelta(hours=4):
                data_atual += timedelta(days=1)
                inicio = data_atual.replace(hour=h, minute=m, second=0, microsecond=0)
            ultimo_inicio = inicio

            if inicio > data_limite:
                continue

            titulo = linhas[i + 1] if i + 1 < len(linhas) else ""
            genero = (
                linhas[i + 2]
                if i + 2 < len(linhas) and "/" in linhas[i + 2] and not linhas[i + 2].startswith("Publicidade")
                else ""
            )
            if titulo and not titulo.startswith("Publicidade") and not _RE_HORA.match(titulo):
                eventos.append({"inicio": inicio, "titulo": titulo, "genero": genero})

    if not eventos:
        return nome, []

    eventos.sort(key=lambda x: x["inicio"])
    dedup: list[dict] = []
    for ev in eventos:
        if not dedup or ev["inicio"] > dedup[-1]["inicio"]:
            dedup.append(ev)

    for idx in range(len(dedup) - 1):
        dedup[idx]["fim"] = min(dedup[idx + 1]["inicio"], dedup[idx]["inicio"] + timedelta(hours=4))
    dedup[-1]["fim"] = dedup[-1]["inicio"] + timedelta(hours=1)
    return nome, dedup


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

    root = ET.Element("tv", {"generator-info-name": "EPG MeuGuia"})
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
            if ev["genero"]:
                for p in ev["genero"].split("/"):
                    if p.strip():
                        ET.SubElement(prog, "category").text = p.strip()
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"MeuGuia -> Canais: {len(resultados)}/{len(canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_meuguia.xml")
    gerar_xml(caminho)
