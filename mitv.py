#!/usr/bin/env python3
"""Coletor Unificado MiTV Brasil (Substitui mitv.py + pull_afiliadas_locais.py):
- Descobre TODOS os canais disponíveis em /br/sitemap, /br/programacao e /br/canais.
- Mescla o catálogo de afiliadas regionais com seus aliases (<display-name>).
- Usa slug como channel id no XMLTV para evitar colisões/duplicações de grade.
- Puxa de ontem (-1) até +4 dias para cobrir 100% da madrugada (00h-05h59 BRT).
- Preserva metadados ricos: categorias traduzidas, sub-title, ano, nota IMDB, sinopse e tag Ao vivo.
"""
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

DAY_OFFSETS = (-1, 0, 1, 2, 3, 4)  # Ontem (madrugada atual) até +4 dias
MAX_WORKERS = 10
BRT_TZ = timezone(timedelta(hours=-3))

DISCOVERY_URLS = (
    "https://mi.tv/br/sitemap",
    "https://mi.tv/br/programacao",
    "https://mi.tv/br/canais",
)

# Slugs garantidos + aliases regionais para maximizar o match no merge_epg.py
AFILIADAS_CATALOG = [
    # Nordeste
    ("tv-asa-branca-hd", "TV Asa Branca", ["Globo Asa Branca", "Globo TV Asa Branca", "Globo Caruaru", "Globo Asa Branca Caruaru"]),
    ("globo-nordeste-hd", "Globo Nordeste", ["Globo Recife", "Globo Pernambuco", "Globo Nordeste Recife"]),
    ("tv-gazeta-de-alagoas", "TV Gazeta de Alagoas", ["Globo TV Gazeta Alagoas", "Globo Alagoas", "Globo Maceió", "TV Gazeta Alagoas"]),
    ("tv-bahia", "TV Bahia", ["Globo Bahia", "Globo Salvador", "Globo TV Bahia", "Rede Bahia"]),
    ("tv-verdes-mares-hd", "TV Verdes Mares", ["Globo TV Verdes Mares", "Globo Fortaleza", "Globo Ceará", "Globo Verdes Mares"]),
    ("tv-cabo-branco", "TV Cabo Branco", ["Globo TV Cabo Branco", "Globo João Pessoa", "Globo Paraíba", "TV Paraíba", "Globo Campina Grande"]),
    ("tv-sergipe-hd", "TV Sergipe", ["Globo TV Sergipe", "Globo Aracaju", "Globo Sergipe"]),
    ("tv-mirante-s-o-luis-hd", "TV Mirante", ["Globo TV Mirante", "Globo São Luís", "Globo Maranhão", "TV Mirante São Luís", "Globo Imperatriz"]),
    ("inter-tv-cabugi", "Inter TV Cabugi", ["Globo Inter TV Cabugi", "Globo Natal", "Globo Rio Grande do Norte"]),
    ("tv-clube", "TV Clube", ["Globo TV Clube", "Globo Teresina", "Globo Piauí"]),
    # Norte
    ("tv-liberal", "TV Liberal", ["Globo TV Liberal", "Globo Belém", "Globo Pará", "TV Liberal Belém"]),
    ("rede-amazonica-manaus-hd", "Rede Amazônica Manaus", ["Globo Rede Amazônica Manaus", "Globo Manaus", "Globo Amazonas", "TV Amazonas", "Rede Amazônica"]),
    ("rede-amazonica-rio-branco", "Rede Amazônica Rio Branco", ["Globo Rede Amazônica Rio Branco", "Globo Rio Branco", "Globo Acre"]),
    ("rede-amazonica-porto-velho", "Rede Amazônica Porto Velho", ["Globo Rede Amazônica Porto Velho", "Globo Porto Velho", "Globo Rondônia"]),
    ("rede-amazonica-macapa", "Rede Amazônica Macapá", ["Globo Rede Amazônica Macapá", "Globo Macapá", "Globo Amapá"]),
    ("rede-amazonica-boa-vista", "Rede Amazônica Boa Vista", ["Globo Rede Amazônica Boa Vista", "Globo Boa Vista", "Globo Roraima"]),
    # Centro-Oeste
    ("tv-anhanguera-hd", "TV Anhanguera", ["Globo TV Anhanguera", "Globo Anhanguera", "Globo Goiânia", "Globo Goiás", "Globo TV Anhanguera Goiânia"]),
    ("tv-morena", "TV Morena", ["Globo TV Morena", "Globo Campo Grande", "Globo Mato Grosso do Sul"]),
    ("tv-centro-america", "TV Centro América", ["Globo TV Centro América", "Globo Cuiabá", "Globo Mato Grosso"]),
    ("globo-brasilia", "Globo Brasília", ["Globo Distrito Federal"]),
    # Sul
    ("globo-rbs-tv-poa", "RBS TV Porto Alegre", ["Globo RBS Porto Alegre", "Globo RBS", "Globo Porto Alegre", "Globo Rio Grande do Sul", "RBS TV", "RBS Passo Fundo Globo"]),
    ("rpc-curitiba", "RPC Curitiba", ["Globo RPC Curitiba", "Globo RPC", "Globo Curitiba", "Globo Paraná", "RPC TV", "Globo Londrina", "Globo Maringá", "Globo Foz do Iguaçu", "Globo Cascavel", "Globo Ponta Grossa"]),
    ("nsc-tv-florianopolis-hd", "NSC TV Florianópolis", ["Globo NSC Florianópolis", "Globo NSC", "Globo Florianópolis", "Globo Santa Catarina", "NSC TV", "Globo Joinville", "Globo Blumenau", "Globo Chapecó", "Globo Criciúma"]),
    # Sudeste
    ("eptv-sul-de-minas-hd", "EPTV Sul de Minas", ["Globo EPTV Sul de Minas", "Globo Sul de Minas", "Globo Varginha"]),
    ("eptv-campinas", "EPTV Campinas", ["Globo EPTV Campinas", "Globo Campinas"]),
    ("eptv-ribeirao", "EPTV Ribeirão", ["Globo EPTV Ribeirão Preto", "Globo Ribeirão Preto", "Globo Franca", "Globo São Carlos", "EPTV Central"]),
    ("tv-gazeta-es", "TV Gazeta ES", ["Globo TV Gazeta Vitória", "Globo Vitória", "Globo Espírito Santo", "TV Gazeta Vitória"]),
    ("globo-minas", "Globo Minas", ["Globo Belo Horizonte", "Globo Minas Gerais", "Globo Juiz de Fora", "Globo Uberlândia", "TV Integração"]),
    ("globo-rj", "Globo RJ", ["Globo Rio de Janeiro", "Globo Rio"]),
    ("rede-globo", "Globo SP", ["Globo São Paulo", "TV Globo"]),
    # Afiliadas SBT / Record / Band Regionais
    ("tv-aratu-hd", "TV Aratu", ["SBT TV Aratu", "SBT Bahia", "SBT Salvador"]),
    ("sbt-goiania-tv-serra-dourada", "TV Serra Dourada", ["SBT TV Serra Dourada", "SBT Goiânia", "SBT Goiás"]),
    ("tv-jangadeiro", "TV Jangadeiro", ["SBT TV Jangadeiro", "SBT Fortaleza", "SBT Ceará"]),
    ("tv-jornal", "TV Jornal", ["SBT TV Jornal Recife", "SBT Recife", "SBT Pernambuco"]),
    ("tv-alterosa", "TV Alterosa", ["SBT TV Alterosa", "SBT Minas", "SBT Belo Horizonte"]),
    ("rede-massa", "Rede Massa", ["SBT Rede Massa", "SBT Curitiba", "SBT Paraná", "SBT Londrina", "SBT Maringá"]),
    ("rede-record-maceio-hd", "Record Maceió", ["TV Pajuçara", "Record Alagoas", "Record TV Pajuçara"]),
    ("tv-itapoan", "Record Itapoan", ["TV Itapoan", "Record Bahia", "Record Salvador"]),
    ("tv-tribuna-1", "TV Tribuna PE", ["Band Pernambuco", "Band Recife"]),
]

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

_CANAL_HREF_RE = re.compile(r"/br/canais/([^/?#\"']+)")


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Referer": "https://mi.tv/br/programacao",
    })
    return s


def descobrir_todos_canais(session: requests.Session) -> dict[str, dict]:
    """Retorna {slug: {'name': nome_principal, 'aliases': [display_names...]}} deduplicado por slug."""
    canais: dict[str, dict] = {}

    def _add(slug: str, nome: str, aliases: list[str] | None = None) -> None:
        slug = slug.strip().lower()
        nome = " ".join((nome or "").split()).strip()
        if not slug or slug in {"canais", "programacao"} or nome == "$nameFromProvider":
            return
        if slug not in canais:
            canais[slug] = {"name": nome or slug, "aliases": []}
        elif nome and canais[slug]["name"] == slug:
            canais[slug]["name"] = nome
        for cand in [nome, *(aliases or [])]:
            cand = " ".join((cand or "").split()).strip()
            if cand and cand != "$nameFromProvider" and cand not in canais[slug]["aliases"]:
                canais[slug]["aliases"].append(cand)

    # 1. Garante o catálogo de afiliadas + aliases regionais
    for slug, name, aliases in AFILIADAS_CATALOG:
        _add(slug, name, aliases)

    # 2. Varre todas as páginas de descoberta do MiTV Brasil
    for url in DISCOVERY_URLS:
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            for a in soup.find_all("a", href=True):
                m = _CANAL_HREF_RE.search(a["href"])
                if not m:
                    continue
                slug = m.group(1)
                txt = a.get_text(" ", strip=True)
                if not txt:
                    img = a.find("img", alt=True)
                    txt = img["alt"].strip() if img else ""
                _add(slug, txt or slug)
        except Exception as exc:
            print(f"  ⚠ Falha na descoberta em {url}: {exc}")

    return canais


def extrair_programacao(session: requests.Session, slug: str, hoje: datetime, data_limite: datetime) -> tuple[str, list[dict]]:
    eventos: list[dict] = []

    for offset in DAY_OFFSETS:
        data_base = (hoje + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        data_str = data_base.strftime("%Y-%m-%d")
        urls = [
            f"https://mi.tv/br/async/channel/{slug}/{data_str}/-180",
            f"https://mi.tv/br/canais/{slug}/{data_str}",
        ]
        if offset == 0:
            urls.insert(0, f"https://mi.tv/br/async/channel/{slug}/-180")

        html_content = None
        for url in urls:
            try:
                r = session.get(url, timeout=15)
                if r.status_code == 200 and b'class="time"' in r.content and b"<h2" in r.content:
                    html_content = r.content
                    break
            except Exception:
                continue

        if not html_content:
            continue

        soup = BeautifulSoup(html_content, "html.parser")
        data_cursor = data_base
        ultimo_inicio: datetime | None = None

        itens = soup.find_all("a", class_="program-link") or soup.find_all("li")
        for prog in itens:
            time_elem = prog.find("span", class_="time")
            hora = time_elem.get_text().strip() if time_elem else ""
            h2 = prog.find("h2")
            titulo_bruto = h2.get_text().strip() if h2 else ""
            if not hora or not titulo_bruto or ":" not in hora:
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

    if not eventos:
        return slug, []

    eventos.sort(key=lambda x: x["inicio"])
    dedup: list[dict] = []
    for ev in eventos:
        if not dedup or ev["inicio"] > dedup[-1]["inicio"]:
            dedup.append(ev)

    for idx in range(len(dedup) - 1):
        dedup[idx]["fim"] = min(dedup[idx + 1]["inicio"], dedup[idx]["inicio"] + timedelta(hours=6))
    dedup[-1]["fim"] = dedup[-1]["inicio"] + timedelta(hours=1)
    return slug, dedup


def gerar_xml(caminho_saida: str | Path) -> None:
    hoje = datetime.now(BRT_TZ)
    data_limite = hoje + timedelta(days=max(DAY_OFFSETS) + 1)

    session = criar_sessao()
    canais = descobrir_todos_canais(session)
    print(f"MiTV Unificado -> {len(canais)} slugs únicos descobertos. Extraindo grades...")

    resultados: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(extrair_programacao, session, slug, hoje, data_limite)
            for slug in canais
        ]
        for fut in as_completed(futures):
            slug, evs = fut.result()
            if evs:
                resultados[slug] = evs

    root = ET.Element("tv", {"generator-info-name": "EPG MiTV Brasil Unificado"})
    slugs_ordenados = sorted(resultados.keys(), key=lambda s: (canais[s]["name"].lower(), s))

    for slug in slugs_ordenados:
        info = canais[slug]
        channel = ET.SubElement(root, "channel", id=slug)
        for dn in dict.fromkeys([info["name"], *info["aliases"]]):
            ET.SubElement(channel, "display-name").text = dn

    total_eventos = 0
    for slug in slugs_ordenados:
        for ev in resultados[slug]:
            prog = ET.SubElement(
                root,
                "programme",
                start=ev["inicio"].strftime("%Y%m%d%H%M%S -0300"),
                stop=ev["fim"].strftime("%Y%m%d%H%M%S -0300"),
                channel=slug,
            )
            ET.SubElement(prog, "title", {"lang": "pt"}).text = ev["titulo"]
            if ev["sub_title"]:
                ET.SubElement(prog, "sub-title", {"lang": "pt"}).text = ev["sub_title"]
            for cat in ev["categorias"]:
                ET.SubElement(prog, "category", {"lang": "pt"}).text = cat
            if ev["ano"]:
                ET.SubElement(prog, "date").text = ev["ano"]
            if ev["nota"]:
                rating = ET.SubElement(prog, "rating", system="IMDB")
                ET.SubElement(rating, "value").text = ev["nota"]
            if ev["sinopse"]:
                ET.SubElement(prog, "desc", {"lang": "pt"}).text = ev["sinopse"]
            total_eventos += 1

    tree = ET.ElementTree(root)
    tree.write(caminho_saida, encoding="utf-8", xml_declaration=True)
    print(f"MiTV Unificado -> Canais com grade: {len(resultados)}/{len(canais)} | Eventos: {total_eventos}")


if __name__ == "__main__":
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epg_mitv.xml")
    gerar_xml(caminho)
