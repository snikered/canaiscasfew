#!/usr/bin/env python3
"""Extrator oficial da grade da Rede Globo (redeglobo.globo.com/.../programacao/) -> globo_epg.xml.gz
- Raspa o JSON SSR oficial (escapado em HTML) de todas as emissoras/afiliadas brasileiras da Globo.
- Extrai data/hora exata (scheduledDate), duração (duration), título, sinopse e categoria.
- Mantém TV Asa Branca Alagoas e TV Asa Branca Pernambuco separadas.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_GLOBO_GZ = Path("globo_epg.xml.gz")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Referer": "https://redeglobo.globo.com/",
}

# (channel_id_xml, url_programacao, display_name_principal, aliases)
GLOBO_AFFILIATES: list[tuple[str, str, str, list[str]]] = [
    # Regionais / Cabeças de rede
    ("globo.sao.paulo", "https://redeglobo.globo.com/sao-paulo/programacao/", "Globo SP", ["Globo São Paulo", "TV Globo", "GB SP"]),
    ("globo.rio", "https://redeglobo.globo.com/rio/programacao/", "Globo RJ", ["Globo Rio", "Globo Rio de Janeiro", "GB RJ"]),
    ("gb.minas", "https://redeglobo.globo.com/globominas/programacao/", "Globo Minas", ["Globo Belo Horizonte", "Globo Minas Gerais", "GB MINAS"]),
    ("globo.brasilia", "https://redeglobo.globo.com/globobrasilia/programacao/", "Globo Brasília", ["Globo Brasilia", "Globo Distrito Federal", "GB BRASILIA"]),
    ("gb.nordeste", "https://redeglobo.globo.com/globonordeste/programacao/", "Globo Nordeste", ["Globo Recife", "Globo Pernambuco", "GB NORDESTE"]),
    ("tv.globo", "https://redeglobo.globo.com/tvglobo/programacao/", "TV Globo", ["Globo"]),
    # Nordeste
    ("tv.asa.branca.al", "https://redeglobo.globo.com/al/asa-branca-alagoas/programacao/", "TV Asa Branca Alagoas", ["Globo Alagoas", "Globo Maceió", "Globo TV Alagoas", "Asa Branca Alagoas"]),
    ("tv.asa.branca", "https://redeglobo.globo.com/pe/tvasabranca/programacao/", "TV Asa Branca", ["TV Asa Branca Pernambuco", "Globo PE Asa Branca", "Globo Asa Branca", "Globo Caruaru"]),
    ("globo.inter.tv.costa.branca", "https://redeglobo.globo.com/rn/intertvrn/costa-branca/programacao/", "Inter TV Costa Branca", ["Globo Inter TV Costa Branca", "Inter TV RN Costa Branca"]),
    ("globo.inter.tv.cabugi", "https://redeglobo.globo.com/rn/intertvrn/natal/programacao/", "Inter TV Cabugi", ["Inter TV RN Natal", "Globo Natal", "Globo Inter TV Cabugi", "Inter TV Cabugi Natal"]),
    ("gb.tv.verdes.mares", "https://redeglobo.globo.com/tvverdesmares/programacao/", "TV Verdes Mares", ["Globo Verdes Mares", "Globo Ceará", "Globo Fortaleza", "GB TV VERDES MARES"]),
    ("globo.tv.verdes.mares.cariri", "https://redeglobo.globo.com/tvverdesmares/tv-cariri/programacao/", "TV Verdes Mares Cariri", ["Globo TV Verdes Mares Cariri", "TV Cariri"]),
    ("globo.rede.bahia", "https://redeglobo.globo.com/redebahia/programacao/", "Rede Bahia", ["TV Bahia", "Globo Bahia", "Globo Salvador", "GB TV BAHIA"]),
    ("tv.clube.teresina", "https://redeglobo.globo.com/pi/redeclube/programacao/", "Rede Clube", ["TV Clube", "TV Clube Teresina", "Globo Teresina", "Globo Piauí"]),
    ("gb.tv.cabo.branco", "https://redeglobo.globo.com/tvcabobranco/programacao/", "TV Cabo Branco", ["Globo Cabo Branco", "Globo João Pessoa", "GB TV CABO BRANCO"]),
    ("tv.paraiba.br", "https://redeglobo.globo.com/tvparaiba/programacao/", "TV Paraíba", ["TV Paraiba", "Globo Campina Grande", "Globo Paraíba"]),
    ("tv.grande.rio", "https://redeglobo.globo.com/pe/tvgranderio/programacao/", "TV Grande Rio", ["TV Grande Rio Petrolina", "Globo Petrolina"]),
    ("tv.mirante.sao.luis", "https://redeglobo.globo.com/ma/tvmirante/programacao/", "TV Mirante", ["TV Mirante São Luís", "Globo Maranhão", "Globo Mirante São Luís"]),
    ("gb.tv.sergipe", "https://redeglobo.globo.com/se/tvsergipe/programacao/", "TV Sergipe", ["Globo Sergipe", "Globo Aracaju", "GB TV SERGIPE"]),
    # Sudeste
    ("eptv.campinas", "https://redeglobo.globo.com/sp/eptv/campinas-e-regiao/programacao/", "EPTV Campinas", ["Globo EPTV Campinas", "Globo Campinas"]),
    ("eptv.ribeirao.preto", "https://redeglobo.globo.com/sp/eptv/ribeirao-preto-franca/programacao/", "EPTV Ribeirão Preto", ["Globo EPTV Ribeirão Preto", "EPTV Ribeirão", "Globo Ribeirão Preto"]),
    ("eptv.sul.de.minas", "https://redeglobo.globo.com/sp/eptv/sul-de-minas/programacao/", "EPTV Sul de Minas", ["Globo EPTV Sul de Minas", "Globo Sul de Minas"]),
    ("eptv.central.sao.carlos", "https://redeglobo.globo.com/sp/eptv/sao-carlos-regiao/programacao/", "EPTV São Carlos", ["EPTV Central São Carlos", "Globo EPTV São Carlos", "EPTV Araraquara"]),
    ("tv.tem.bauru.br", "https://redeglobo.globo.com/sp/tvtem/bauru/programacao/", "TV TEM Bauru", ["Globo TV TEM Bauru", "Globo TEM Bauru"]),
    ("tv.tem.itapetininga.br", "https://redeglobo.globo.com/sp/tvtem/itapetininga/programacao/", "TV TEM Itapetininga", ["Globo TV TEM Itapetininga"]),
    ("tv.tem.sorocaba.br", "https://redeglobo.globo.com/sp/tvtem/sorocaba-e-regiao/programacao/", "TV TEM Sorocaba", ["Globo TV TEM Sorocaba", "Globo TEM Sorocaba"]),
    ("tv.tem.s.j.rio.preto", "https://redeglobo.globo.com/sp/tvtem/sao-jose-do-rio-preto/programacao/", "TV TEM São José do Rio Preto", ["TV TEM S J Rio Preto", "Globo SJRP"]),
    ("intertv.grande.minas.br", "https://redeglobo.globo.com/mg/intertvmg/programacao/", "Inter TV MG", ["Inter TV Grande Minas", "Inter TV dos Vales"]),
    ("inter.tv.alto.litoral", "https://redeglobo.globo.com/rj/intertvrj/programacao/", "Inter TV RJ", ["Inter TV Alto Litoral", "Inter TV Serra Mar", "Inter TV Planície"]),
    ("tv.diario.mogi.das.cruzes.br", "https://redeglobo.globo.com/sp/tvdiario/programacao/", "TV Diário Mogi das Cruzes", ["Globo TV Diário Mogi das Cruzes", "Globo Diário Mogi das Cruzes"]),
    ("gb.tv.gazeta.vitoria", "https://redeglobo.globo.com/tvgazetaes/programacao/", "TV Gazeta ES", ["Globo Vitória", "Globo TV Gazeta Vitória", "GB TV GAZETA VITORIA"]),
    ("tv.integracao.uberlandia.br", "https://redeglobo.globo.com/mg/tvintegracao/programacao/", "TV Integração Uberlândia", ["TV Integração", "Globo Integ Uberlândia"]),
    ("tv.integracao.araxa.br", "https://redeglobo.globo.com/mg/tvintegracao/araxa/programacao/", "TV Integração Araxá", ["Globo Integ Araxá"]),
    ("tv.integracao.ituiutaba.br", "https://redeglobo.globo.com/mg/tvintegracao/ituiutaba/programacao/", "TV Integração Ituiutaba", []),
    ("tv.integracao.juiz.de.fora.br", "https://redeglobo.globo.com/mg/tvintegracao/juiz-de-fora/programacao/", "TV Integração Juiz de Fora", ["Globo Integ Juiz de Fora"]),
    ("tv.integracao.uberaba.br", "https://redeglobo.globo.com/mg/tvintegracao/uberaba/programacao/", "TV Integração Uberaba", []),
    ("TVRioSul.br", "https://redeglobo.globo.com/rj/tvriosul/programacao/", "TV Rio Sul", ["Globo TV Rio Sul"]),
    ("tv.tribuna.santos.br", "https://redeglobo.globo.com/sp/tvtribuna/programacao/", "TV Tribuna Santos", ["Globo Tribuna Santos"]),
    ("tv.vanguarda.s.j.campos", "https://redeglobo.globo.com/sp/tvvanguarda/programacao/", "TV Vanguarda", ["TV Vanguarda S J Campos", "TV Vanguarda Taubaté", "Globo Vang S J Campos"]),
    # Sul
    ("gb.rpc.curitiba", "https://redeglobo.globo.com/rpc/programacao/", "RPC Curitiba", ["Globo RPC Curitiba", "RPC TV", "GB RPC CURITIBA"]),
    ("rpc.cascavel", "https://redeglobo.globo.com/rpc/cascavel/programacao/", "RPC Cascavel", ["Globo RPC Cascavel"]),
    ("rpc.foz.do.iguacu", "https://redeglobo.globo.com/rpc/foz-do-iguacu/programacao/", "RPC Foz do Iguaçu", ["Globo RPC Foz do Iguaçu"]),
    ("rpc.guarapuava", "https://redeglobo.globo.com/rpc/guarapuava/programacao/", "RPC Guarapuava", ["Globo RPC Guarapuava"]),
    ("rpc.londrina", "https://redeglobo.globo.com/rpc/londrina/programacao/", "RPC Londrina", ["Globo RPC Londrina"]),
    ("rpc.maringa", "https://redeglobo.globo.com/rpc/maringa/programacao/", "RPC Maringá", ["Globo RPC Maringá"]),
    ("rpc.tv.paranavai", "https://redeglobo.globo.com/rpc/noroeste/programacao/", "RPC Noroeste", ["RPC TV Paranavaí", "Globo RPC Paranavaí"]),
    ("rpc.tv.ponta.grossa", "https://redeglobo.globo.com/rpc/ponta-grossa/programacao/", "RPC Ponta Grossa", ["RPC TV Ponta Grossa", "Globo RPC Ponta Grossa"]),
    ("gb.nsc.florianopolis", "https://redeglobo.globo.com/sc/nsctv/programacao/", "NSC TV Florianópolis", ["NSC TV", "Globo NSC Florianópolis", "GB NSC FLORIANOPOLIS"]),
    ("gb.rbs.porto.alegre", "https://redeglobo.globo.com/rs/rbstvrs/programacao/", "RBS TV Porto Alegre", ["RBS TV", "Globo RBS Porto Alegre", "GB RBS PORTO ALEGRE"]),
    # Centro-Oeste
    ("gb.tv.anhanguera", "https://redeglobo.globo.com/tvanhanguera/goias/programacao/", "TV Anhanguera Goiás", ["TV Anhanguera", "Globo Anhanguera Goiânia", "GB TV ANHANGUERA"]),
    ("globo.tv.anhanguera.palmas.tocantins", "https://redeglobo.globo.com/tvanhanguera/tocantins/programacao/", "TV Anhanguera Tocantins", ["Globo TV Anhanguera Palmas Tocantins", "Globo TV Anhanguera Araguaína"]),
    ("gb.tv.centro.america", "https://redeglobo.globo.com/tvcentroamerica/programacao/", "TV Centro América", ["Globo Centro América", "GB TV CENTRO AMERICA"]),
    ("gb.tv.morena", "https://redeglobo.globo.com/tvmorena/programacao/", "TV Morena", ["TV Morena Campo Grande", "Globo Morena", "GB TV MORENA"]),
    ("tv.morena.corumba.br", "https://redeglobo.globo.com/tvmorena/corumba/programacao/", "TV Morena Corumbá", ["TV Morena Corumba"]),
    ("tv.morena.ponta.pora.hd.tv.globo.br", "https://redeglobo.globo.com/tvmorena/ponta-pora/programacao/", "TV Morena Ponta Porã", ["TV Morena Ponta Pora"]),
    # Norte
    ("globo.rede.amazonica.rio.branco", "https://redeglobo.globo.com/redeamazonica/acre/programacao/", "Rede Amazônica Acre", ["Rede Amazônica Rio Branco", "Globo Amazônica Rio Branco"]),
    ("globo.amapa", "https://redeglobo.globo.com/redeamazonica/amapa/programacao/", "Rede Amazônica Amapá", ["Globo Amapá", "Rede Amazônica Macapá"]),
    ("gb.rede.amazonica.manaus", "https://redeglobo.globo.com/redeamazonica/amazonas/programacao/", "Rede Amazônica Amazonas", ["Rede Amazônica Manaus", "Globo Amazônica Manaus"]),
    ("globo.rede.amazonica.rondonia", "https://redeglobo.globo.com/redeamazonica/rondonia/programacao/", "Rede Amazônica Rondônia", ["Rede Amazônica Porto Velho", "Globo Amazônica Porto Velho"]),
    ("globo.roraima", "https://redeglobo.globo.com/redeamazonica/roraima/programacao/", "Rede Amazônica Roraima", ["Globo Roraima", "Rede Amazônica Boa Vista"]),
    ("gb.tv.liberal.belem", "https://redeglobo.globo.com/pa/tvliberal/programacao/", "TV Liberal", ["TV Liberal Belém", "Globo Belém", "GB TV LIBERAL BELEM"]),
    ("tv.tapajos", "https://redeglobo.globo.com/pa/tvtapajos/programacao/", "TV Tapajós", ["TV Tapajos"]),
]

_SLOT_MARKER_RE = re.compile(r'\{\s*"contentType"\s*:')


def parse_duration(dur_str: str) -> timedelta | None:
    if not dur_str or ":" not in dur_str:
        return None
    parts = dur_str.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        td = timedelta(hours=h, minutes=m, seconds=s)
        return td if td.total_seconds() > 0 else None
    except ValueError:
        return None


def fetch_affiliate_schedule(session: requests.Session, url: str) -> tuple[int, list[tuple[datetime, datetime | None, str, str, str]]]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=25)
        if resp.status_code != 200:
            return resp.status_code, []
        text = html.unescape(resp.text)
    except Exception:
        return 0, []

    decoder = json.JSONDecoder()
    by_start: dict[datetime, tuple[datetime | None, str, str, str]] = {}

    for m in _SLOT_MARKER_RE.finditer(text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except Exception:
            continue
        if not isinstance(obj, dict) or "scheduledDate" not in obj:
            continue

        sched = str(obj.get("scheduledDate") or "").strip()
        title = str(obj.get("showName") or obj.get("name") or "").strip()
        if not sched or not title:
            continue

        try:
            dt_start = datetime.fromisoformat(sched).astimezone(BRT_TZ)
        except Exception:
            continue

        dur = parse_duration(str(obj.get("duration") or ""))
        dt_stop = (dt_start + dur) if dur else None

        prog_obj = obj.get("program") if isinstance(obj.get("program"), dict) else {}
        title_obj = obj.get("title") if isinstance(obj.get("title"), dict) else {}
        desc = str(title_obj.get("synopsis") or prog_obj.get("synopsis") or "").strip()

        genre_obj = title_obj.get("genre") if isinstance(title_obj.get("genre"), dict) else {}
        category = str(genre_obj.get("name") or obj.get("contentType") or "").strip()

        if dt_start not in by_start or (desc and not by_start[dt_start][2]):
            by_start[dt_start] = (dt_stop, title, desc, category)

    items = [(dt, st, ti, de, ca) for dt, (st, ti, de, ca) in sorted(by_start.items(), key=lambda x: x[0])]
    return 200, items


def generate_globo_epg(out_gz_path: Path = OUT_GLOBO_GZ) -> int:
    session = requests.Session()
    tv_root = ET.Element("tv", {"generator-info-name": "redeglobo-official-ssr-extractor"})
    total_progs = 0
    ok_channels = 0

    print(f"Buscando grade oficial de {len(GLOBO_AFFILIATES)} emissoras/afiliadas da Rede Globo...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        future_map = {
            pool.submit(fetch_affiliate_schedule, session, url): (cid, main_name, aliases)
            for cid, url, main_name, aliases in GLOBO_AFFILIATES
        }
        for fut in concurrent.futures.as_completed(future_map):
            cid, main_name, aliases = future_map[fut]
            try:
                status, items = fut.result()
            except Exception as exc:
                print(f"  ⚠ [{main_name}] Erro: {exc}")
                continue

            if len(items) < 2:
                print(f"  - [{main_name}] Sem grade (HTTP {status}).")
                continue

            ok_channels += 1
            ch_el = ET.SubElement(tv_root, "channel", {"id": cid})
            for dn in [main_name, *aliases]:
                dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                dn_el.text = dn

            for i, (dt_start, dt_stop, title, desc, category) in enumerate(items):
                if dt_stop is None:
                    if i + 1 < len(items):
                        dt_stop = items[i + 1][0]
                        if (dt_stop - dt_start) > timedelta(hours=6):
                            dt_stop = dt_start + timedelta(hours=2)
                    else:
                        dt_stop = dt_start + timedelta(minutes=60)

                if dt_stop <= dt_start:
                    continue

                p_el = ET.SubElement(tv_root, "programme", {
                    "start": dt_start.strftime("%Y%m%d%H%M%S -0300"),
                    "stop": dt_stop.strftime("%Y%m%d%H%M%S -0300"),
                    "channel": cid,
                })
                t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                t_el.text = title
                if desc:
                    d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                    d_el.text = desc
                if category:
                    c_el = ET.SubElement(p_el, "category", {"lang": "pt"})
                    c_el.text = category
                total_progs += 1

            print(f"  ✓ [{main_name}] {len(items)} programas extraídos.")

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"\n✓ Rede Globo Oficial EPG salvo em {out_gz_path} ({ok_channels} emissoras, {total_progs} programas em -0300).")
    return total_progs


if __name__ == "__main__":
    generate_globo_epg()
