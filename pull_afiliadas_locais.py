#!/usr/bin/env python3
"""Scraper dedicado para puxar a programação regional das Afiliadas da Globo, SBT, Record e Band
diretamente dos endpoints regionais do Mi.TV Brasil e salvar em 'epg_afiliadas.xml'.
- Inclui o dia anterior (-1) para garantir 100% de cobertura na madrugada (00h às 05h BRT).
"""
from __future__ import annotations

import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTPUT_XML = Path("epg_afiliadas.xml")
DAY_OFFSETS = (-1, 0, 1, 2)  # Ontem (cobre madrugada atual), Hoje e próximos 2 dias

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
    ("globo-rbs-tv-poa", "RBS TV Porto Alegre", ["Globo RBS Porto Alegre", "Globo RBS", "Globo Porto Alegre", "Globo Rio Grande do Sul", "RBS TV"]),
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Referer": "https://mi.tv/br/programacao",
}

_BLOCK_RE = re.compile(
    r'<li\b[^>]*>.*?<span\s+class="time"[^>]*>\s*(\d{1,2}:\d{2})\s*</span>.*?'
    r'<h2[^>]*>\s*(.*?)\s*</h2>\s*'
    r'(?:<span\s+class="sub-title"[^>]*>\s*(.*?)\s*</span>)?.*?'
    r'(?:<p\s+class="synopsis"[^>]*>\s*(.*?)\s*</p>)?',
    re.DOTALL | re.IGNORECASE,
)

def clean_html_text(raw: str | None) -> str:
    if not raw:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(html.unescape(no_tags).split()).strip()

def fetch_day_html(slug: str, date_str: str) -> str:
    urls = [
        f"https://mi.tv/br/async/channel/{slug}/{date_str}/-180",
        f"https://mi.tv/br/canais/{slug}/{date_str}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                if 'class="time"' in text and "<h2" in text:
                    return text
        except Exception:
            continue
    return ""

def parse_channel_schedule(slug: str, base_date_brt: datetime) -> list[dict[str, str]]:
    brt_tz = timezone(timedelta(hours=-3))
    raw_items: list[tuple[datetime, str, str, str]] = []

    for day_offset in DAY_OFFSETS:
        target_day = base_date_brt + timedelta(days=day_offset)
        date_str = target_day.strftime("%Y-%m-%d")
        page = fetch_day_html(slug, date_str)
        if not page:
            continue

        last_minutes = -1
        day_rollover = 0
        for m in _BLOCK_RE.finditer(page):
            hm_str, raw_title, raw_sub, raw_desc = m.groups()
            title = clean_html_text(raw_title)
            if not title:
                continue
            sub = clean_html_text(raw_sub)
            desc = clean_html_text(raw_desc)

            hh, mm = (int(x) for x in hm_str.split(":"))
            total_min = hh * 60 + mm
            if last_minutes != -1 and total_min < last_minutes and (last_minutes - total_min) > 180:
                day_rollover = 1
            last_minutes = total_min

            prog_dt = datetime(
                target_day.year, target_day.month, target_day.day, hh, mm, tzinfo=brt_tz
            ) + timedelta(days=day_rollover)
            raw_items.append((prog_dt.astimezone(timezone.utc), title, sub, desc))

    if not raw_items:
        return []

    raw_items.sort(key=lambda x: x[0])
    deduped: list[tuple[datetime, str, str, str]] = []
    for item in raw_items:
        if not deduped or item[0] > deduped[-1][0]:
            deduped.append(item)

    programmes = []
    for i, (start_utc, title, sub, desc) in enumerate(deduped):
        if i + 1 < len(deduped):
            stop_utc = deduped[i + 1][0]
            if (stop_utc - start_utc) > timedelta(hours=6):
                stop_utc = start_utc + timedelta(hours=2)
        else:
            stop_utc = start_utc + timedelta(minutes=60)

        programmes.append({
            "start": start_utc.strftime("%Y%m%d%H%M%S +0000"),
            "stop": stop_utc.strftime("%Y%m%d%H%M%S +0000"),
            "title": title,
            "sub": sub,
            "desc": desc,
        })
    return programmes

def main() -> None:
    brt_now = datetime.now(timezone(timedelta(hours=-3)))
    root = ET.Element("tv", {"generator-info-name": "Scraper Afiliadas Locais BR"})
    total_progs = 0
    ok_channels = 0

    print(f"Buscando programação regional de {len(AFILIADAS_CATALOG)} afiliadas...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_info = {
            pool.submit(parse_channel_schedule, slug, brt_now): (slug, name, aliases)
            for slug, name, aliases in AFILIADAS_CATALOG
        }
        for fut in as_completed(future_to_info):
            slug, name, aliases = future_to_info[fut]
            try:
                progs = fut.result()
            except Exception as exc:
                print(f"  ⚠ [{name}] Erro: {exc}")
                continue

            if not progs:
                print(f"  - [{name}] Sem grade retornada hoje.")
                continue

            ok_channels += 1
            total_progs += len(progs)
            print(f"  ✓ [{name}] {len(progs)} programas locais extraídos.")

            ch_el = ET.SubElement(root, "channel", {"id": slug})
            for dn in [name, *aliases]:
                dn_el = ET.SubElement(ch_el, "display-name")
                dn_el.text = dn

            for p in progs:
                p_el = ET.SubElement(root, "programme", {
                    "channel": slug,
                    "start": p["start"],
                    "stop": p["stop"],
                })
                t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                t_el.text = p["title"]
                if p["sub"]:
                    s_el = ET.SubElement(p_el, "sub-title", {"lang": "pt"})
                    s_el.text = p["sub"]
                if p["desc"]:
                    d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                    d_el.text = p["desc"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(OUTPUT_XML, encoding="utf-8", xml_declaration=True)
    print(f"\nConcluído! {ok_channels} afiliadas salvas em {OUTPUT_XML} ({total_progs} programas).")

if __name__ == "__main__":
    main()
