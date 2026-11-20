#!/usr/bin/env python3
"""Extrator oficial da grade da TV Globo (epg-api.video.globo.com) -> globo_epg.xml.gz
- Puxa direto da API oficial da Globo por praça/afiliada (sem depender de XML manual).
- Inclui ontem (-1), hoje (0) e próximos 2 dias (+1, +2) em fuso -0300.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_GLOBO_GZ = Path("globo_epg.xml.gz")
DAY_OFFSETS = (-1, 0, 1, 2)
GLOBO_EPG_URL = "https://epg-api.video.globo.com/programmes/{epg_id}?date={date_str}"

# (epg_id_oficial, channel_id_xml, display_name_principal, aliases)
GLOBO_AFFILIATES: list[tuple[int, str, str, list[str]]] = [
    (1337, "globo.sao.paulo", "Globo SP", ["Globo São Paulo", "TV Globo", "GB SP"]),
    (1338, "globo.rio", "Globo RJ", ["Globo Rio", "Globo Rio de Janeiro", "GB RJ"]),
    (1339, "gb.minas", "Globo Minas", ["Globo Belo Horizonte", "Globo Minas Gerais"]),
    (1340, "globo.brasilia", "Globo Brasília", ["Globo Brasilia", "Globo Distrito Federal"]),
    (1341, "globo.nordeste", "Globo Nordeste", ["Globo Recife", "Globo Pernambuco", "GB Nordeste"]),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://redeglobo.globo.com/",
}


def fetch_affiliate_day(session: requests.Session, epg_id: int, date_str: str) -> list[dict[str, Any]]:
    url = GLOBO_EPG_URL.format(epg_id=epg_id, date_str=date_str)
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json().get("programme", {}).get("entries") or []
    except Exception:
        pass
    return []


def generate_globo_epg(out_gz_path: Path = OUT_GLOBO_GZ) -> int:
    session = requests.Session()
    now_brt = datetime.now(BRT_TZ)
    dates = [(now_brt + timedelta(days=d)).strftime("%Y-%m-%d") for d in DAY_OFFSETS]

    tv_root = ET.Element("tv", {"generator-info-name": "globo-official-epg-extractor"})
    total_progs = 0
    ok_channels = 0

    for epg_id, cid, main_name, aliases in GLOBO_AFFILIATES:
        entries_by_start: dict[int, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(fetch_affiliate_day, session, epg_id, d) for d in dates]
            for fut in concurrent.futures.as_completed(futs):
                for ev in fut.result():
                    st = ev.get("start_time")
                    et = ev.get("end_time")
                    if isinstance(st, (int, float)) and isinstance(et, (int, float)) and et > st:
                        entries_by_start[int(st)] = ev

        if not entries_by_start:
            print(f"  - [{main_name}] (ID {epg_id}) Sem grade retornada.")
            continue

        ok_channels += 1
        ch_el = ET.SubElement(tv_root, "channel", {"id": cid})
        for dn in [main_name, *aliases]:
            dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
            dn_el.text = dn

        ch_progs = 0
        for st_ts in sorted(entries_by_start.keys()):
            ev = entries_by_start[st_ts]
            et_ts = int(ev["end_time"])
            title = html.unescape(str(ev.get("title") or "").strip())
            if not title:
                continue

            desc = html.unescape(str(ev.get("description") or "").strip())
            dt_start = datetime.fromtimestamp(st_ts, tz=BRT_TZ)
            dt_stop = datetime.fromtimestamp(et_ts, tz=BRT_TZ)

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

            custom = ev.get("custom_info") if isinstance(ev.get("custom_info"), dict) else {}
            classif = custom.get("Classificacao") if isinstance(custom.get("Classificacao"), dict) else {}
            idade = str(classif.get("Idade") or "").strip()
            if idade:
                r_el = ET.SubElement(p_el, "rating", {"system": "age"})
                v_el = ET.SubElement(r_el, "value")
                v_el.text = idade

            ch_progs += 1

        total_progs += ch_progs
        print(f"  ✓ [{main_name}] (ID {epg_id}) {ch_progs} programas extraídos.")

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"\n✓ Globo Oficial EPG salvo em {out_gz_path} ({ok_channels} canais, {total_progs} programas em -0300).")
    return total_progs


if __name__ == "__main__":
    generate_globo_epg()