#!/usr/bin/env python3
"""Coletor otimizado MiTV (paralelo, correção de meia-noite e fuso -0300)."""
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
BRT_TZ = timezone(timedelta(hours=-3))

TRADUCOES = {
    "Action": "Ação", "Adventure": "Aventura", "Biography": "Biografia", "Bus./Financial": "Negócios/Finanças",
    "Children": "Infantil", "Comedy": "Comédia", "Comedy Drama": "Comédia Dramática", "Community": "Comunidade",
    "Consumer": "Consumo", "Dark Comedy": "Comédia Negra", "Documentary": "Documentário", "Drama": "Drama",
    "Educational": "Educativo", "Entertainment": "Entretenimento", "Health": "Saúde", "History": "História",
    "Horror": "Terror", "Interview": "Entrevista", "Music": "Música", "Nature": "Natureza", "News": "Notícias",
    "Newsmagazine": "Revista de Notícias", "Politics": "Política", "Public Affairs": "Assuntos Públicos",
    "Reality": "Reality Show", "Religious": "Religioso", "Romance": "Romance", "Science Fiction": "Ficção Científica",
    "Shopping": "Compras", "Soap": "Novela", "Special": "Especial", "Sports Talk": "Debate Esportivo",
    "Talk": "Talk Show", "Thriller": "Suspense", "Variety": "Variedades",
}


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"})
    return s


def descobrir_canais(session: requests.Session) -> dict[str, str]:
    try:
        r = session.get("https://mi.tv/br/sitemap", timeout=15)
        soup = BeautifulSoup(r.content, "html.parser")
        canais: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            txt = a.get_text().strip()
            if "/br/canais/" in a["href"] and txt and txt != "$nameFromProvider":
                canais[txt] = a["href"].rstrip("/").split("/")[-1]
        return canais
    except Exception:
        return {}


def extrair_programacao(session: requests.Session, nome: str, codigo: str, data_limite: datetime, hoje: datetime) -> tuple[str, list[dict]]:
    eventos: list[dict] = []
    for dias in range(DIAS_PARA_FRENTE + 1):
        data_base = (hoje + timedelta(days=dias)).replace(hour=0, minute=0, second=0, microsecond=0)
        data_str = data_base.strftime("%Y-%m-%d")
        url = (
            f"https://mi.tv/br/async/channel/{codigo}/-180"
            if dias == 0
            else f"https://mi.tv/br/async/channel/{codigo}/{data_str}/-180"
        )

        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            data_cursor = data_base
            ultimo_inicio: datetime | None = None

            for prog in soup.find_all("a", class_="program-link"):
                time_elem = prog.find("span", class_="time")
                hora = time_elem.get_text().strip() if time_elem else ""
                h2 = prog.find("h2")
                titulo_bruto = h2.get_text().strip() if h2 else ""
                if not hora or not titulo_bruto:
                    continue

                tem_ao_vivo = h2.find("img", class_="vivo") is not None if h2 else False
                sub = prog.find("span", class_="sub-title")
                sub_texto = sub.get_text().strip() if sub else ""
                sinopse = prog.find("p", class_="synopsis")
                sinopse_texto = sinopse.get_text().strip() if sinopse else ""

                nota = ""
                if sub and (rating_elem := sub.find("span", class_="rating")):
                    nota = rating_elem.get_text().strip()
                    sub_texto = sub_texto.replace(nota, "").rstrip("/").strip()

                try:
                    h, m = map(int, hora.split(":"))
                    inicio = data_cursor.replace(hour=h, minute=m)
                    # A página diária do MiTV começa às 06:00 e termina após a meia-noite (00:00..05:59 do dia seguinte)
                    if ultimo_inicio and inicio < ultimo_inicio - timedelta(hours=4):
                        data_cursor += timedelta(days=1)
                        inicio = data_cursor.replace(hour=h, minute=m)
                    ultimo_inicio = inicio
                    if inicio > data_limite:
                        continue
                except Exception:
                    continue

                categorias, sub_title, ano = [], "", ""
                if "Temporada" in sub_texto or "Season" in sub_texto:
                    sub_title, categorias = sub_texto, ["Séries"]
                elif "/" in sub_texto:
                    partes = sub_texto.split("/")
                    if len(partes) >= 1 and partes[0].strip():
                        categorias.extend(["Filme", TRADUCOES.get(partes[0].strip(), partes[0].strip())])
                    if len(partes) >= 2 and (ano_match := re.search(r"\d{4}", partes[1])):
                        ano = ano_match.group(0)
                elif sub_texto:
                    categorias.append(TRADUCOES.get(sub_texto, sub_texto))

                eventos.append({
                    "inicio": inicio,
                    "titulo": f"Ao vivo - {titulo_bruto}" if tem_ao_vivo and not titulo_bruto.lower().startswith("ao vivo") else titulo_bruto,
                    "categorias": categorias,
                    "sub_title": sub_title,
                    "ano": ano,
                    "nota": nota,
                    "sinopse": sinopse_texto,
                })
        except Exception:
            pass

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
            executor.submit(extrair_programacao, session, nome, codigo, data_limite, hoje)
            for nome, codigo in canais.items()
        ]
        for fut in as_completed(futures):
            nome, evs = fut.result()
            if evs:
                resultados[nome] = evs

    root = ET.Element("tv", {"generator-info-name": "EPG MiTV"})
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
            if ev["sub_title"]:
                ET.SubElement(prog, "sub-title").text = ev["sub_title"]
            for cat in ev["categorias"]:
                ET.SubElement(prog, "category").text = cat
            if ev["ano"]:
                ET.SubElement(prog, "date").text = ev["ano"]
            if ev["nota"]:
                rating = ET.SubElement(prog, "rating", system="IMDB")
                ET.SubElement(rating, "value").text = ev["nota"]
            if ev["sinopse"]:
                ET.SubElement(prog, "desc").text = ev["sinopse"]
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"MiTV -> Canais: {len(resultados)}/{len(canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_mitv.xml")
    gerar_xml(caminho)
