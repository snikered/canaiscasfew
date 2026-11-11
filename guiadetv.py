#!/usr/bin/env python3
"""Coletor otimizado GuiaDeTV (paralelo, fuso -0300 exato e XMLTV estrito)."""
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
BASE = "https://www.guiadetv.com"
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    "https://www.guiadetv.com/categorias/variedades.html",
    "https://www.guiadetv.com/categorias/tv-aberta.html",
    "https://www.guiadetv.com/categorias/noticias.html",
    "https://www.guiadetv.com/categorias/infantil.html",
    "https://www.guiadetv.com/categorias/filmes-e-series.html",
    "https://www.guiadetv.com/categorias/esportes.html",
    "https://www.guiadetv.com/categorias/documentarios.html",
]

_RE_CANAL_HREF = re.compile(r"/canal/")
_RE_DUR_H = re.compile(r"(\d+)\s*h\s*(\d+)?", re.IGNORECASE)
_RE_DUR_M = re.compile(r"(\d+)\s*min", re.IGNORECASE)


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.google.com/",
    })
    return s


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    canais: dict[str, str] = {}
    for url_cat in CATEGORIAS:
        try:
            r = session.get(url_cat, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            for a in soup.find_all("a", href=_RE_CANAL_HREF):
                href = a["href"].strip()
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = BASE + href
                nome = " ".join(a.get_text().split())
                if nome and nome not in canais:
                    canais[nome] = href
        except Exception:
            pass
    return canais


def extrair_programacao(session: requests.Session, nome: str, url_canal: str, data_limite: datetime) -> tuple[str, list[dict]]:
    try:
        r = session.get(url_canal, timeout=15)
        if r.status_code != 200:
            return nome, []
    except Exception:
        return nome, []

    soup = BeautifulSoup(r.content, "html.parser")
    eventos: list[dict] = []
    seen_starts: set[datetime] = set()

    for li in soup.find_all("li"):
        time_tag = li.find("time", attrs={"datetime": True})
        if not time_tag:
            continue

        raw_dt = str(time_tag["datetime"]).strip()
        try:
            if len(raw_dt) >= 19:
                inicio = datetime.strptime(raw_dt[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=BRT_TZ)
            else:
                continue
        except Exception:
            continue

        if inicio > data_limite or inicio in seen_starts:
            continue

        link_prog = li.find("a")
        if not link_prog:
            continue

        titulo = " ".join(link_prog.get_text().split())
        if not titulo:
            continue

        seen_starts.add(inicio)

        sub_titulo = ""
        if " : " in titulo:
            titulo, sub_titulo = [p.strip() for p in titulo.split(" : ", 1)]

        duracao_min = 60
        if time_tag.parent and (span_dur := time_tag.parent.find("span")):
            txt_dur = " ".join(span_dur.get_text().split())
            if match_dur := _RE_DUR_H.match(txt_dur):
                duracao_min = int(match_dur.group(1)) * 60 + int(match_dur.group(2) or 0)
            elif match_dur := _RE_DUR_M.match(txt_dur):
                duracao_min = int(match_dur.group(1))

        badges = [
            " ".join(span.get_text().split())
            for span in li.find_all("span")
            if not span.find("span")
            and len(" ".join(span.get_text().split())) < 30
            and not re.match(r"^\d", " ".join(span.get_text().split()))
        ]
        tem_ao_vivo = any(b.lower() == "ao vivo" for b in badges)

        sinopse = ""
        if p_sinopse := li.find("p"):
            sinopse = " ".join(p_sinopse.get_text().split())
            if "sinopse n" in sinopse.lower()[:12]:
                sinopse = ""

        evento = {
            "inicio": inicio,
            "fim": inicio + timedelta(minutes=max(5, duracao_min)),
            "titulo": f"Ao vivo - {titulo}" if tem_ao_vivo and not titulo.lower().startswith("ao vivo") else titulo,
            "sub_titulo": sub_titulo,
            "desc": sinopse,
        }
        eventos.append(evento)

    eventos.sort(key=lambda x: x["inicio"])
    for i in range(len(eventos) - 1):
        if eventos[i]["fim"] > eventos[i + 1]["inicio"]:
            eventos[i]["fim"] = eventos[i + 1]["inicio"]

    return nome, eventos


def gerar_xml(caminho_saida: str | Path) -> None:
    hoje = datetime.now(BRT_TZ)
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    canais = descobrir_canais(session)
    print(f"GuiaDeTV -> Canais encontrados: {len(canais)}")

    resultados: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(extrair_programacao, session, nome, url, data_limite)
            for nome, url in canais.items()
        ]
        for fut in as_completed(futures):
            nome, evs = fut.result()
            if evs:
                resultados[nome] = evs

    root = ET.Element("tv", {"generator-info-name": "EPG GuiaDeTV"})
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
            if ev.get("sub_titulo"):
                ET.SubElement(prog, "sub-title").text = ev["sub_titulo"]
            if ev.get("desc"):
                ET.SubElement(prog, "desc").text = ev["desc"]
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"GuiaDeTV -> Canais com grade: {len(resultados)}/{len(canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_guiadetv.xml")
    gerar_xml(caminho)
