#!/usr/bin/env python3
"""Extrator Direto da API Vivo Play (GVP Telefonica) -> XMLTV (vivo_epg.xml.gz)
- Puxa direto da fonte oficial usada por https://www.vivotv.com.br/tv-guide/epg
- Formata <desc> no padrão enxuto:
    🟢 LIVRE | 📅 2024 | ⏱ 45 mins | 🎭 Ação, Séries
    🟠 +14 | 📅 2018 | ⏱ 60 mins | 🎭 Policial, Documentário
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# Endpoints da API GVP Telefonica (Vivo Play Brasil)
VIVO_API_BASES = [
    "https://contentapi-br.cdn.telefonica.com/25/default/pt-BR",
    "https://api.vivoplay.com.br/25/default/pt-BR",
]
SPOTLIGHT_IMG_BASE = "https://spotlight-br.cdn.telefonica.com/customer/v1/source"

BRT_TZ = timezone(timedelta(hours=-3))
OUT_VIVO_GZ = Path("vivo_epg.xml.gz")

# Classificação enxuta exatamente como pedido: Bolinha + (+10, +12, +14, +16, +18) e LIVRE no L
RATING_SHORT_MAP = {
    "L": "🟢 LIVRE",
    "LIVRE": "🟢 LIVRE",
    "0": "🟢 LIVRE",
    "G": "🟢 LIVRE",
    "ALL": "🟢 LIVRE",
    "10": "🔵 +10",
    "PG-10": "🔵 +10",
    "A10": "🔵 +10",
    "12": "🟡 +12",
    "PG-12": "🟡 +12",
    "A12": "🟡 +12",
    "14": "🟠 +14",
    "PG-14": "🟠 +14",
    "A14": "🟠 +14",
    "16": "🔴 +16",
    "PG-16": "🔴 +16",
    "A16": "🔴 +16",
    "18": "⚫ +18",
    "PG-18": "⚫ +18",
    "A18": "⚫ +18",
    "R": "⚫ +18",
}


def format_rating_badge(raw_rating: str) -> tuple[str, str]:
    """Retorna (badge_curto, codigo_xmltv). Ex: ('🟠 +14', 'PG-14')."""
    if not raw_rating:
        return "", ""
    r = str(raw_rating).strip().upper()
    # Se já vier com texto longo de outra fonte ("Não recomendado para menores de 14 anos")
    if "LIVRE" in r:
        return "🟢 LIVRE", "L"
    m = re.search(r"\b(10|12|14|16|18)\b", r)
    if m:
        age = m.group(1)
        return RATING_SHORT_MAP[age], f"PG-{age}"
    if r in RATING_SHORT_MAP:
        badge = RATING_SHORT_MAP[r]
        code = "L" if "LIVRE" in badge else f"PG-{badge.split('+')[1]}"
        return badge, code
    return "", ""


def build_clean_visual_desc(
    synopsis: str,
    rating_badge: str = "",
    year: str = "",
    duration_mins: int | None = None,
    categories: list[str] | None = None,
    sub_title: str = "",
) -> str:
    """Monta a descrição limpa com bolinha +14/+18 ou LIVRE na barra de metadados."""
    meta_parts: list[str] = []
    if rating_badge:
        meta_parts.append(rating_badge)
    if year and str(year).isdigit():
        meta_parts.append(f"📅 {year}")
    if duration_mins and 1 <= duration_mins <= 720:
        meta_parts.append(f"⏱ {duration_mins} mins")
    if categories:
        clean_cats = [c.strip() for c in categories if c and c.strip()]
        if clean_cats:
            meta_parts.append(f"🎭 {', '.join(dict.fromkeys(clean_cats))}")

    header_lines: list[str] = []
    if meta_parts:
        header_lines.append(" | ".join(meta_parts))
    if sub_title:
        header_lines.append(f"📝 {sub_title.strip()}")

    clean_syn = (synopsis or "").strip()
    if header_lines and clean_syn:
        return "\n".join(header_lines) + "\n\n" + clean_syn
    if header_lines:
        return "\n".join(header_lines)
    return clean_syn


def enrich_programme_element_short(prog_elem: ET.Element) -> None:
    """Função universal para usar no merge_epg.py (formata Claro, Vivo, Mi.TV, etc.).
    Mesmo que um XML externo venha com '🟠 Não recomendado para menores de 14 anos',
    converte automaticamente para '🟠 +14'!
    """
    desc_elem = prog_elem.find("desc")
    raw_desc = (desc_elem.text or "").strip() if desc_elem is not None else ""

    # Se veio com o texto longo do padrão antigo, limpa o cabeçalho antigo para reformatar curto
    if "Não recomendado para menores de" in raw_desc or "Livre para todos" in raw_desc:
        lines = raw_desc.splitlines()
        cleaned_lines = [
            ln for ln in lines
            if not any(tag in ln for tag in ("Não recomendado", "Livre para todos", "📅", "🎭", "⏱", "📝"))
        ]
        raw_desc = "\n".join(cleaned_lines).strip()
    elif any(em in raw_desc for em in ("🟢 LIVRE", "🔵 +10", "🟡 +12", "🟠 +14", "🔴 +16", "⚫ +18", "📅", "🎭")):
        return

    # 1. Classificação
    rating_val = ""
    r_el = prog_elem.find("./rating/value")
    if r_el is not None and r_el.text:
        rating_val = r_el.text.strip()
    rating_badge, _ = format_rating_badge(rating_val)

    # 2. Ano
    d_el = prog_elem.find("date")
    year_str = (d_el.text or "").strip()[:4] if d_el is not None and d_el.text else ""

    # 3. Duração calculada pelo start/stop
    dur_mins: int | None = None
    try:
        s_raw = prog_elem.attrib.get("start", "").strip().split()[0][:14]
        e_raw = prog_elem.attrib.get("stop", "").strip().split()[0][:14]
        dt_s = datetime.strptime(s_raw, "%Y%m%d%H%M%S")
        dt_e = datetime.strptime(e_raw, "%Y%m%d%H%M%S")
        diff = int((dt_e - dt_s).total_seconds() // 60)
        if 1 <= diff <= 720:
            dur_mins = diff
    except Exception:
        pass

    # 4. Categorias e Subtítulo
    cats = [c.text.strip() for c in prog_elem.findall("category") if c.text and c.text.strip()][:5]
    sub_el = prog_elem.find("sub-title")
    sub_title = (sub_el.text or "").strip() if sub_el is not None and sub_el.text else ""

    new_desc = build_clean_visual_desc(
        synopsis=raw_desc,
        rating_badge=rating_badge,
        year=year_str,
        duration_mins=dur_mins,
        categories=cats,
        sub_title=sub_title,
    )
    if new_desc:
        if desc_elem is None:
            desc_elem = ET.SubElement(prog_elem, "desc", {"lang": "pt"})
        desc_elem.text = new_desc


def make_channel_id(name: str) -> str:
    """Converte 'Discovery Home & Health HD' -> 'DiscoveryHomeHealth.br'."""
    s = html.unescape(name or "")
    s = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", " ", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", s)
    camel = "".join(w.capitalize() if not w.isupper() else w for w in words)
    return f"{camel or 'Canal'}.br"


def build_spotlight_url(raw_img_url: str, height: int = 480) -> str:
    if not raw_img_url:
        return ""
    if raw_img_url.startswith("https://spotlight-br"):
        return raw_img_url
    if raw_img_url.startswith("/"):
        raw_img_url = f"http://media.gvp.telefonica.com{raw_img_url}"
    encoded = urllib.parse.quote(raw_img_url, safe="")
    return f"{SPOTLIGHT_IMG_BASE}?image={encoded}&height={height}&resize=RATIO&format=WEBP"


def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://www.vivotv.com.br",
        "Referer": "https://www.vivotv.com.br/tv-guide/epg",
    })
    return s


def fetch_vivo_channels(session: requests.Session) -> tuple[str, list[dict[str, Any]]]:
    """Busca a lista completa de canais ao vivo na API GVP da Vivo."""
    endpoints = [
        "/contents/all?contentType=LIVE_CHANNEL&limit=1000",
        "/epg/channels?limit=1000",
        "/schedules/channels?limit=1000",
    ]
    for base in VIVO_API_BASES:
        for ep in endpoints:
            try:
                r = session.get(f"{base}{ep}", timeout=25)
                if r.status_code == 200:
                    data = r.json()
                    items = (
                        data.get("Content", {}).get("List")
                        or data.get("Result")
                        or data.get("Content")
                        or data.get("channels")
                        or (data if isinstance(data, list) else [])
                    )
                    if isinstance(items, list) and len(items) > 0:
                        return base, items
            except Exception:
                continue
    raise RuntimeError("Não foi possível acessar a API da Vivo Play (verifique se precisa de IP/Proxy BR).")


def fetch_schedules_chunk(
    session: requests.Session,
    api_base: str,
    channel_ids: list[str],
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    ch_param = ",".join(channel_ids)
    urls_to_try = [
        f"{api_base}/schedules?channels={ch_param}&startTime={start_ts}&endTime={end_ts}",
        f"{api_base}/epg?channels={ch_param}&start={start_ts}&end={end_ts}",
    ]
    for u in urls_to_try:
        try:
            r = session.get(u, timeout=35)
            if r.status_code == 200:
                data = r.json()
                items = (
                    data.get("Content", {}).get("List")
                    or data.get("Result")
                    or data.get("Content")
                    or data.get("schedules")
                    or (data if isinstance(data, list) else [])
                )
                if isinstance(items, list):
                    return items
        except Exception:
            continue
    return []


def ts_to_xmltv(ts_val: Any) -> tuple[str, datetime | None]:
    """Converte timestamp Unix (segundos ou ms) ou ISO-8601 para 'YYYYMMDDHHMMSS -0300'."""
    try:
        if isinstance(ts_val, (int, float)) or (isinstance(ts_val, str) and ts_val.isdigit()):
            v = float(ts_val)
            if v > 1e11:
                v /= 1000.0
            dt = datetime.fromtimestamp(v, tz=BRT_TZ)
            return dt.strftime("%Y%m%d%H%M%S -0300"), dt
        if isinstance(ts_val, str):
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(BRT_TZ)
            return dt.strftime("%Y%m%d%H%M%S -0300"), dt
    except Exception:
        pass
    return "", None


def generate_vivo_epg(days_ahead: int = 2, out_gz_path: Path = OUT_VIVO_GZ) -> int:
    session = create_session()
    api_base, raw_channels = fetch_vivo_channels(session)
    print(f"✓ {len(raw_channels)} canais encontrados na API Vivo Play ({api_base}).")

    tv_root = ET.Element("tv", {"generator-info-name": "vivo-gvp-extractor"})
    ch_map: dict[str, str] = {}

    for ch in raw_channels:
        if not isinstance(ch, dict):
            continue
        vivo_cid = str(ch.get("Id") or ch.get("id") or ch.get("CallLetter") or "").strip()
        ch_name = str(ch.get("Name") or ch.get("Title") or ch.get("name") or "").strip()
        if not vivo_cid or not ch_name:
            continue

        xml_cid = make_channel_id(ch_name)
        ch_map[vivo_cid] = xml_cid

        ch_el = ET.SubElement(tv_root, "channel", {"id": xml_cid})
        dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
        dn_el.text = ch_name

        # Adiciona também nome sem HD/FHD como segundo display-name para casar fácil
        clean_n = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", "", ch_name, flags=re.I).strip()
        if clean_n and clean_n != ch_name:
            dn2 = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
            dn2.text = clean_n

        # Logo do canal
        imgs = ch.get("Images") or ch.get("images") or []
        logo_url = ""
        if isinstance(imgs, list) and imgs:
            first_img = imgs[0]
            logo_url = first_img.get("Url") or first_img.get("url") or ""
        elif isinstance(ch.get("Logo"), str):
            logo_url = ch["Logo"]
        if logo_url:
            ET.SubElement(ch_el, "icon", {"src": build_spotlight_url(logo_url, height=240)})

    now_utc = datetime.now(timezone.utc)
    start_ts = int((now_utc - timedelta(hours=4)).timestamp())
    end_ts = int((now_utc + timedelta(days=days_ahead)).timestamp())

    vivo_ids = list(ch_map.keys())
    chunk_size = 25
    chunks = [vivo_ids[i : i + chunk_size] for i in range(0, len(vivo_ids), chunk_size)]

    prog_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(fetch_schedules_chunk, session, api_base, chunk, start_ts, end_ts)
            for chunk in chunks
        ]
        for fut in concurrent.futures.as_completed(futures):
            sched_list = fut.result()
            for item in sched_list:
                if not isinstance(item, dict):
                    continue
                # Alguns endpoints retornam lista de programas dentro do canal, outros retornam eventos planos
                events = item.get("Programs") or item.get("programs") or item.get("Schedules") or [item]
                ch_ref = str(item.get("ChannelId") or item.get("channelId") or item.get("Id") or "")

                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    cid_raw = str(ev.get("ChannelId") or ev.get("channelId") or ch_ref).strip()
                    xml_cid = ch_map.get(cid_raw)
                    if not xml_cid:
                        continue

                    start_str, dt_start = ts_to_xmltv(ev.get("Start") or ev.get("startTime") or ev.get("StartDate"))
                    stop_str, dt_stop = ts_to_xmltv(ev.get("End") or ev.get("endTime") or ev.get("EndDate"))
                    title = str(ev.get("Title") or ev.get("Name") or ev.get("title") or "").strip()
                    if not start_str or not stop_str or not title:
                        continue

                    sub_title = str(ev.get("EpisodeTitle") or ev.get("SubTitle") or ev.get("subTitle") or "").strip()
                    synopsis = str(ev.get("Description") or ev.get("ShortDescription") or ev.get("Synopsis") or "").strip()
                    year = str(ev.get("Year") or ev.get("ReleaseYear") or "").strip()[:4]

                    raw_rating = ""
                    age_obj = ev.get("AgeRating") or ev.get("Rating") or ev.get("ParentalRating")
                    if isinstance(age_obj, dict):
                        raw_rating = str(age_obj.get("Name") or age_obj.get("Value") or age_obj.get("Code") or "")
                    elif age_obj is not None:
                        raw_rating = str(age_obj)

                    rating_badge, rating_code = format_rating_badge(raw_rating)

                    dur_mins: int | None = None
                    if dt_start and dt_stop:
                        diff = int((dt_stop - dt_start).total_seconds() // 60)
                        if 1 <= diff <= 720:
                            dur_mins = diff

                    genres_raw = ev.get("Genres") or ev.get("Categories") or ev.get("genres") or []
                    cats: list[str] = []
                    if isinstance(genres_raw, list):
                        for g in genres_raw:
                            if isinstance(g, dict):
                                g_name = str(g.get("Name") or g.get("Title") or "").strip()
                                if g_name:
                                    cats.append(g_name)
                            elif isinstance(g, str) and g.strip():
                                cats.append(g.strip())

                    p_el = ET.SubElement(
                        tv_root,
                        "programme",
                        {"start": start_str, "stop": stop_str, "channel": xml_cid},
                    )

                    t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                    t_el.text = f"{title} : {sub_title}" if sub_title and sub_title.lower() not in title.lower() else title

                    if sub_title:
                        st_el = ET.SubElement(p_el, "sub-title", {"lang": "pt"})
                        st_el.text = sub_title

                    desc_text = build_clean_visual_desc(
                        synopsis=synopsis,
                        rating_badge=rating_badge,
                        year=year,
                        duration_mins=dur_mins,
                        categories=cats,
                        sub_title=sub_title if not rating_badge else "",
                    )
                    if desc_text:
                        d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                        d_el.text = desc_text

                    if year and year.isdigit():
                        yr_el = ET.SubElement(p_el, "date")
                        yr_el.text = year

                    for c_name in cats:
                        c_el = ET.SubElement(p_el, "category", {"lang": "pt"})
                        c_el.text = c_name

                    # Imagem / Pôster do programa via Spotlight CDN
                    ev_imgs = ev.get("Images") or ev.get("images") or []
                    poster_url = ""
                    if isinstance(ev_imgs, list) and ev_imgs:
                        poster_url = ev_imgs[0].get("Url") or ev_imgs[0].get("url") or ""
                    elif isinstance(ev.get("Image"), str):
                        poster_url = ev["Image"]
                    if poster_url:
                        ET.SubElement(p_el, "icon", {"src": build_spotlight_url(poster_url, height=480)})

                    if rating_code:
                        r_el = ET.SubElement(p_el, "rating", {"system": "age"})
                        v_el = ET.SubElement(r_el, "value")
                        v_el.text = rating_code

                    prog_count += 1

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Vivo EPG salvo em {out_gz_path} ({prog_count} programas com visual enriquecido).")
    return prog_count


if __name__ == "__main__":
    generate_vivo_epg()