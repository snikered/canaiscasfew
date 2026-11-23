#!/usr/bin/env python3
"""Coletor otimizado MeuGuia (lxml, paralelo, sem buracos de 4h, virada de ano e fuso -0300)."""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
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
MAX_WORKERS = 8
MAX_DURACAO_HORAS = 12
BASE_URL = "https://meuguia.tv"
OUTPUT_FILE = Path(
    os.environ.get(
        "MEUGUIA_OUTPUT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_meuguia.xml"),
    )
)
BRT_TZ = timezone(timedelta(hours=-3))

CATEGORIAS = [
    f"{BASE_URL}/programacao/categoria/Filmes",
    f"{BASE_URL}/programacao/categoria/Series",
    f"{BASE_URL}/programacao/categoria/Esportes",
    f"{BASE_URL}/programacao/categoria/Infantil",
    f"{BASE_URL}/programacao/categoria/Variedades",
    f"{BASE_URL}/programacao/categoria/Documentarios",
    f"{BASE_URL}/programacao/categoria/Noticias",
    f"{BASE_URL}/programacao/categoria/Aberta",
]

_RE_DIA = re.compile(r"^(\w+(?:-feira)?),\s*(\d+)/(\d+)", re.IGNORECASE)
_RE_HORA = re.compile(r"^(\d{2}:\d{2})$")


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS + 2,
        pool_maxsize=(MAX_WORKERS + 2) * 2,
        max_retries=retry,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    })
    return s


def _fetch_categoria(session: requests.Session, url_cat: str) -> list[tuple[str, str]]:
    encontrados: list[tuple[str, str]] = []
    try:
        r = session.get(url_cat, timeout=(6, 15))
        if r.status_code != 200:
            return encontrados
        soup = BeautifulSoup(r.content, HTML_PARSER)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/programacao/canal/" not in href:
                continue
            primeira_linha = a.get_text("\n").strip().split("\n")[0]
            nome = " ".join(primeira_linha.split())
            if nome:
                if href.startswith("/"):
                    href = BASE_URL + href
                encontrados.append((nome, href))
    except Exception:
        pass
    return encontrados


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    canais: dict[str, str] = {}
    urls_vistas: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(CATEGORIAS))) as executor:
        futures = [executor.submit(_fetch_categoria, session, url) for url in CATEGORIAS]
        for fut in as_completed(futures):
            for nome, href in fut.result():
                if nome not in canais and href not in urls_vistas:
                    canais[nome] = href
                    urls_vistas.add(href)
    return canais


def _resolver_ano(mes_num: int, hoje: datetime) -> int:
    if mes_num == 1 and hoje.month == 12:
        return hoje.year + 1
    if mes_num == 12 and hoje.month == 1:
        return hoje.year - 1
    return hoje.year


def extrair_programacao(
    session: requests.Session,
    nome: str,
    url_canal: str,
    inicio_min: datetime,
    data_limite: datetime,
    hoje: datetime,
) -> tuple[str, list[dict], str]:
    try:
        r = session.get(url_canal, timeout=(6, 15))
        if r.status_code != 200:
            return nome, [], f"HTTP {r.status_code}"
    except requests.Timeout:
        return nome, [], "timeout"
    except Exception as e:
        return nome, [], type(e).__name__

    soup = BeautifulSoup(r.content, HTML_PARSER)
    linhas = [
        " ".join(l.split())
        for l in soup.get_text("\n").splitlines()
        if l.strip()
    ]

    eventos: list[dict] = []
    data_atual: datetime | None = None
    ultimo_inicio: datetime | None = None

    for i, linha in enumerate(linhas):
        match_dia = _RE_DIA.match(linha)
        if match_dia:
            dia_num, mes_num = int(match_dia.group(2)), int(match_dia.group(3))
            ano = _resolver_ano(mes_num, hoje)
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
                if i + 2 < len(linhas)
                and "/" in linhas[i + 2]
                and not linhas[i + 2].startswith("Publicidade")
                and not _RE_HORA.match(linhas[i + 2])
                else ""
            )
            if titulo and not titulo.startswith("Publicidade") and not _RE_HORA.match(titulo):
                eventos.append({"inicio": inicio, "titulo": titulo, "genero": genero})

    if not eventos:
        return nome, [], "sem_eventos"

    eventos.sort(key=lambda x: x["inicio"])
    dedup: list[dict] = []
    for ev in eventos:
        if not dedup or ev["inicio"] > dedup[-1]["inicio"]:
            dedup.append(ev)

    max_delta = timedelta(hours=MAX_DURACAO_HORAS)
    for idx in range(len(dedup) - 1):
        dedup[idx]["fim"] = min(dedup[idx + 1]["inicio"], dedup[idx]["inicio"] + max_delta)
    dedup[-1]["fim"] = dedup[-1]["inicio"] + timedelta(hours=1)

    filtrados = [ev for ev in dedup if ev["fim"] >= inicio_min]
    return nome, filtrados, "ok" if filtrados else "sem_eventos"


def gerar_xml(caminho_saida: Path) -> None:
    t0 = time.perf_counter()
    hoje = datetime.now(BRT_TZ)
    inicio_min = hoje - timedelta(hours=8)
    data_limite = hoje + timedelta(days=DIAS_PARA_FRENTE)

    session = criar_sessao()
    canais = descobrir_canais(session)

    resultados: dict[str, list[dict]] = {}
    erros: Counter[str] = Counter()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                extrair_programacao, session, nome, url, inicio_min, data_limite, hoje
            )
            for nome, url in canais.items()
        ]
        for fut in as_completed(futures):
            nome, evs, status = fut.result()
            if evs:
                resultados[nome] = evs
            else:
                erros[status] += 1

    root = ET.Element("tv", {"generator-info-name": "EPG MeuGuia"})
    nomes_ordenados = sorted(resultados.keys(), key=str.lower)

    for nome in nomes_ordenados:
        channel = ET.SubElement(root, "channel", {"id": nome})
        ET.SubElement(channel, "display-name").text = nome

    total_eventos = 0
    for nome in nomes_ordenados:
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
            if ev["genero"]:
                for p in ev["genero"].split("/"):
                    cat = p.strip()
                    if cat:
                        ET.SubElement(prog, "category", {"lang": "pt"}).text = cat
            duracao_min = max(1, int((ev["fim"] - ev["inicio"]).total_seconds() // 60))
            ET.SubElement(prog, "length", {"units": "minutes"}).text = str(duracao_min)
            total_eventos += 1

    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)

    elapsed = time.perf_counter() - t0
    resumo_erros = f" | Falhas: {dict(erros)}" if erros else ""
    print(
        f"MeuGuia ({HTML_PARSER}) -> Canais: {len(resultados)}/{len(canais)} | "
        f"Eventos: {total_eventos} | Tempo: {elapsed:.1f}s{resumo_erros}"
    )


if __name__ == "__main__":
    gerar_xml(OUTPUT_FILE)
