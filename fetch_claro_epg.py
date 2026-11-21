#!/usr/bin/env python3
"""Extrator oficial da Claro TV+ (API Gatekeeper Solr) -> claro_epg.xml.gz
- Consulta /canal/select e /exibicao/select direto em programacao.claro.com.br/gatekeeper.
- Cobre todos os ~270 canais do lineup completo (sem limite de 48 canais do mfwkweb).
- Converte horários UTC para Brasília (-0300) e formata <desc> no padrão visual do pipeline.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_CLARO_GZ = Path("claro_epg.xml.gz")
GATEKEEPER_BASE = "https://programacao.claro.com.br/gatekeeper"
CLARO_CITY_ID = os.environ.get("CLARO_CITY_ID", "1").strip() or "1"  # 1 = São Paulo (lineup completo)

RATING_SHORT_MAP = {
    "L": "🟢 LIVRE", "LIVRE": "🟢 LIVRE", "0": "🟢 LIVRE", "G": "🟢 LIVRE", "ALL": "🟢 LIVRE",
    "10": "🔵 +10", "PG-10": "🔵 +10", "A10": "🔵 +10",
    "12": "🟡 +12", "PG-12": "🟡 +12", "A12": "🟡 +12",
    "14": "🟠 +14", "PG-14": "🟠 +14", "A14": "🟠 +14",
    "16": "🔴 +16", "PG-16": "🔴 +16", "A16": "🔴 +16",
    "18": "⚫ +18", "PG-18": "⚫ +18", "A18": "⚫ +18", "R": "⚫ +18",
}


def format_rating_badge(raw_rating: Any) -> tuple[str, str]:
    if raw_rating is None:
        return "", ""
    r = str(raw_rating).strip().upper()
    if not r:
        return "", ""
    if "LIVRE" in r or r in {"L", "0", "G", "ALL"}:
        return "🟢 LIVRE", "L"
    m = re.search(r"(?:\+|\b)(10|12|14|16|18)\b", r)
    if m:
        age = m.group(1)
        return RATING_SHORT_MAP[age], f"PG-{age}"
    return "", ""


def make_channel_id(name: str) -> str:
    s = html.unescape(name or "")
    s = re.sub(r"\.(?:br|us|uk|pt|ar|mx|cl|co|pe|ca|latam|int)$", " ", s.strip(), flags=re.I)
    s = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", " ", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    if re.search(r"\bLOVE\s*NATURE\b", s, flags=re.I):
        return "LoveNature.br"
    words = re.findall(r"[A-Za-z0-9]+", s)
    camel = "".join(w.capitalize() if not w.isupper() else w for w in words)
    return f"{camel or 'Canal'}.br"


def parse_iso_utc_to_brt(iso_str: Any) -> tuple[str, datetime | None]:
    if not iso_str:
        return "", None
    try:
        dt = datetime.fromisoformat(str(iso_str).strip().replace("Z", "+00:00")).astimezone(BRT_TZ)
        return dt.strftime("%Y%m%d%H%M%S -0300"), dt
    except Exception:
        return "", None


def build_clean_visual_desc(
    synopsis: str,
    rating_badge: str = "",
    year: str = "",
    duration_mins: int | None = None,
    categories: list[str] | None = None,
    sub_title: str = "",
    se_badge: str = "",
    director: str = "",
    cast: str = "",
) -> str:
    meta_parts: list[str] = []
    if rating_badge:
        meta_parts.append(rating_badge)
    if year and str(year).isdigit() and 1900 <= int(year) <= 2035:
        meta_parts.append(f"📅 {year}")
    if duration_mins and 1 <= duration_mins <= 720:
        meta_parts.append(f"⏱ {duration_mins} mins")
    if categories:
        clean_cats = [c.strip() for c in categories if c and c.strip()]
        if clean_cats:
            meta_parts.append(f"🎭 {', '.join(dict.fromkeys(clean_cats[:4]))}")

    header_lines: list[str] = []
    if meta_parts:
        header_lines.append(" | ".join(meta_parts))

    clean_sub = (sub_title or "").strip()
    clean_syn = (synopsis or "").strip()
    if clean_sub and clean_syn.lower().startswith(clean_sub.lower()):
        clean_syn = clean_syn[len(clean_sub):].lstrip(" -–—:|.,")

    ep_parts = [p for p in (se_badge, clean_sub) if p]
    if ep_parts:
        ep_str = " ".join(ep_parts)
        header_lines.append(ep_str if se_badge else f"📝 {ep_str}")

    body_parts: list[str] = []
    if clean_syn:
        body_parts.append(clean_syn)
    if director:
        body_parts.append(f"Direção: {director}")
    if cast:
        body_parts.append(f"Elenco: {cast}")
    body = "\n".join(body_parts)

    if header_lines and body:
        return "\n".join(header_lines) + "\n\n" + body
    if header_lines:
        return "\n".join(header_lines)
    return body


def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://www.claro.com.br/",
        "Origin": "https://www.claro.com.br",
    })
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        s.proxies = {"http": br_proxy, "https": br_proxy}
    return s


def fetch_channels(session: requests.Session) -> dict[str, dict[str, str]]:
    """Retorna mapa id_canal -> {name, xml_cid, logo, slug}."""
    r = session.get(
        f"{GATEKEEPER_BASE}/canal/select",
        params={"q": f"id_cidade:{CLARO_CITY_ID}", "wt": "json", "rows": "1000", "start": "0"},
        timeout=30,
    )
    r.raise_for_status()
    docs = r.json().get("response", {}).get("docs") or []
    out: dict[str, dict[str, str]] = {}
    for d in docs:
        cid_raw = str(d.get("id_canal") or "").strip()
        name = html.unescape(str(d.get("nome") or "")).strip()
        if not cid_raw or not name:
            continue
        out[cid_raw] = {
            "name": name,
            "xml_cid": make_channel_id(name),
            "logo": str(d.get("url_imagem") or "").strip(),
            "slug": str(d.get("st_canal") or "").strip(),
        }
    return out


def fetch_exibicoes_window(session: requests.Session, dt_from_utc: datetime, dt_to_utc: datetime) -> list[dict[str, Any]]:
    """Puxa todas as exibições da janela UTC [dt_from_utc, dt_to_utc] com paginação."""
    s_iso = dt_from_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    e_iso = dt_to_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    fq = f"dh_inicio:[{s_iso} TO {e_iso}]"

    all_docs: list[dict[str, Any]] = []
    start = 0
    page_size = 2000
    while True:
        params = {
            "q": f"id_cidade:{CLARO_CITY_ID}",
            "fq": fq,
            "wt": "json",
            "rows": str(page_size),
            "start": str(start),
            "sort": "dh_inicio asc",
        }
        r = session.get(f"{GATEKEEPER_BASE}/exibicao/select", params=params, timeout=45)
        if r.status_code != 200:
            break
        resp = r.json().get("response", {})
        docs = resp.get("docs") or []
        all_docs.extend(docs)
        num_found = int(resp.get("numFound") or 0)
        start += len(docs)
        if not docs or start >= num_found:
            break
    return all_docs


def generate_claro_epg(days_ahead: int = 3, out_gz_path: Path = OUT_CLARO_GZ) -> int:
    session = create_session()
    channel_map = fetch_channels(session)
    if not channel_map:
        raise RuntimeError("Nenhum canal retornado pelo Gatekeeper da Claro.")

    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_utc = now_utc - timedelta(hours=3)
    end_utc = now_utc + timedelta(days=days_ahead)

    windows: list[tuple[datetime, datetime]] = []
    cur = start_utc
    while cur < end_utc:
        nxt = min(cur + timedelta(hours=6), end_utc)
        windows.append((cur, nxt))
        cur = nxt

    tv_root = ET.Element("tv", {"generator-info-name": "claro-gatekeeper-extractor"})
    channels_by_id: dict[str, ET.Element] = {}
    progs_by_channel: dict[str, dict[str, tuple[datetime, ET.Element]]] = {}

    for info in channel_map.values():
        xml_cid = info["xml_cid"]
        if xml_cid not in channels_by_id:
            ch_el = ET.Element("channel", {"id": xml_cid})
            ET.SubElement(ch_el, "display-name", {"lang": "pt"}).text = info["name"]
            if info["logo"]:
                ET.SubElement(ch_el, "icon", {"src": info["logo"]})
            channels_by_id[xml_cid] = ch_el

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_exibicoes_window, session, w_f, w_t) for w_f, w_t in windows]
        for fut in concurrent.futures.as_completed(futures):
            try:
                docs = fut.result()
            except Exception as exc:
                print(f"  - [Claro Gatekeeper] erro em janela: {exc}")
                continue

            for ev in docs:
                cid_raw = str(ev.get("id_canal") or "").strip()
                ch_info = channel_map.get(cid_raw)
                if not ch_info:
                    continue
                xml_cid = ch_info["xml_cid"]

                title = html.unescape(str(ev.get("titulo") or "")).strip()
                start_str, dt_start = parse_iso_utc_to_brt(ev.get("dh_inicio"))
                stop_str, dt_stop = parse_iso_utc_to_brt(ev.get("dh_fim"))
                if not (title and start_str and stop_str and dt_start and dt_stop) or dt_stop <= dt_start:
                    continue

                synopsis = html.unescape(str(ev.get("sinopse") or ev.get("descricao") or "")).strip()
                sub_title = html.unescape(str(ev.get("subtitulo") or "")).strip()
                director = html.unescape(str(ev.get("diretor") or "")).strip()
                cast = html.unescape(str(ev.get("elenco") or "")).strip()
                year = str(ev.get("ano") or "").strip()[:4]
                genero = html.unescape(str(ev.get("genero") or "")).strip()
                cats = [genero] if genero else []

                rating_badge, rating_code = format_rating_badge(ev.get("classificacao"))
                dur_mins = int((dt_stop - dt_start).total_seconds() // 60)

                se_badge = ""
                s_num, e_num = ev.get("temporada"), ev.get("episodio")
                if s_num and e_num and str(s_num).isdigit() and str(e_num).isdigit():
                    s_i, e_i = int(s_num), int(e_num)
                    if 0 < s_i <= 60 and 0 < e_i <= 999:
                        se_badge = f"[T{s_i:02d}E{e_i:02d}]"

                p_el = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": xml_cid})
                ET.SubElement(p_el, "title", {"lang": "pt"}).text = title
                if sub_title and sub_title.lower() != title.lower():
                    ET.SubElement(p_el, "sub-title", {"lang": "pt"}).text = sub_title

                desc_text = build_clean_visual_desc(
                    synopsis=synopsis,
                    rating_badge=rating_badge,
                    year=year,
                    duration_mins=dur_mins if 1 <= dur_mins <= 720 else None,
                    categories=cats,
                    sub_title=sub_title if sub_title.lower() != title.lower() else "",
                    se_badge=se_badge,
                    director=director,
                    cast=cast,
                )
                if desc_text:
                    ET.SubElement(p_el, "desc", {"lang": "pt"}).text = desc_text
                for c_name in cats:
                    ET.SubElement(p_el, "category", {"lang": "pt"}).text = c_name
                if rating_code:
                    r_node = ET.SubElement(p_el, "rating", {"system": "age"})
                    ET.SubElement(r_node, "value").text = rating_code

                progs_by_channel.setdefault(xml_cid, {})[start_str] = (dt_start, p_el)

    valid_channels = 0
    for cid in sorted(channels_by_id.keys()):
        if len(progs_by_channel.get(cid, {})) >= 2:
            tv_root.append(channels_by_id[cid])
            valid_channels += 1

    prog_count = 0
    for cid in sorted(progs_by_channel.keys()):
        if len(progs_by_channel[cid]) < 2:
            continue
        for _, p_elem in sorted(progs_by_channel[cid].values(), key=lambda x: x[0]):
            tv_root.append(p_elem)
            prog_count += 1

    if prog_count == 0:
        raise RuntimeError("Nenhum programa obtido do Gatekeeper da Claro.")

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Claro TV+ EPG (Gatekeeper) salvo em {out_gz_path} ({valid_channels} canais válidos, {prog_count} programas em -0300).")
    return prog_count


if __name__ == "__main__":
    generate_claro_epg()
