#!/usr/bin/env python3
"""Extrator Direto da Claro TV+ (Claro Video) -> XMLTV (claro_epg.xml.gz)
- Ordena estritamente todos os programas por canal e horário (corrige o bug onde 2489 programas viravam 41 no merge_epg.py).
- Blindado contra bug de Fuso Horário: Prioriza Epoch Unix (unix_begin/unix_end) convertido para America/Sao_Paulo (-0300).
- Formata <desc> no padrão enxuto:
    🟢 LIVRE | 📅 2024 | ⏱ 45 mins | 🎭 Esportes, Futebol
    🟠 +14 | 📅 2021 | ⏱ 60 mins | 🎭 Séries, Policial
    [T03E07] Nome do Episódio
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_CLARO_GZ = Path("claro_epg.xml.gz")

CLARO_MFWK_BASE = "https://mfwkweb-api.clarovideo.net/services/epg"

CLARO_COMMON_PARAMS = {
    "device_id": "web",
    "device_category": "web",
    "device_model": "web",
    "device_type": "web",
    "device_so": "Chrome",
    "format": "json",
    "device_manufacturer": "generic",
    "authpn": "webclient",
    "authpt": "tfg1h3j4k6fd7",
    "api_version": "v5.93",
    "region": "brasil",
    "HKS": "web65a1b2c3d4e5f",
}

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


def build_clean_visual_desc(
    synopsis: str,
    rating_badge: str = "",
    year: str = "",
    duration_mins: int | None = None,
    categories: list[str] | None = None,
    sub_title: str = "",
    se_badge: str = "",
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

    if header_lines and clean_syn:
        return "\n".join(header_lines) + "\n\n" + clean_syn
    if header_lines:
        return "\n".join(header_lines)
    return clean_syn


def make_channel_id(name: str) -> str:
    s = html.unescape(name or "")
    s = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", " ", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", s)
    camel = "".join(w.capitalize() if not w.isupper() else w for w in words)
    return f"{camel or 'Canal'}.br"


def parse_claro_time_to_brt(
    unix_ts: Any = None,
    date_str: Any = None,
    source_default_tz: timezone = BRT_TZ,
) -> tuple[str, datetime | None]:
    if unix_ts is not None and str(unix_ts).strip() != "":
        try:
            val = float(str(unix_ts).strip())
            if val > 1e11:
                val /= 1000.0
            if val > 1e8:
                dt_brt = datetime.fromtimestamp(val, tz=BRT_TZ)
                return dt_brt.strftime("%Y%m%d%H%M%S -0300"), dt_brt
        except Exception:
            pass

    if not date_str:
        return "", None

    raw = str(date_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            dt_naive = datetime.strptime(raw[:19], fmt)
            dt_brt = dt_naive.replace(tzinfo=source_default_tz).astimezone(BRT_TZ)
            return dt_brt.strftime("%Y%m%d%H%M%S -0300"), dt_brt
        except Exception:
            continue
    return "", None


def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://www.clarotvmais.com.br",
        "Referer": "https://www.clarotvmais.com.br/",
    })
    return s


def fetch_claro_mfwk_window(
    session: requests.Session,
    dt_from_brt: datetime,
    dt_to_brt: datetime,
) -> list[dict[str, Any]]:
    params = {
        **CLARO_COMMON_PARAMS,
        "date_from": dt_from_brt.strftime("%Y%m%d%H%M%S"),
        "date_to": dt_to_brt.strftime("%Y%m%d%H%M%S"),
        "quantity": "2000",
    }
    try:
        r = session.get(f"{CLARO_MFWK_BASE}/channel", params=params, timeout=45)
        if r.status_code == 200:
            data = r.json()
            channels = data.get("response", {}).get("channels") or []
            if isinstance(channels, list):
                return channels
    except Exception as e:
        print(f"  -> Aviso no bloco {dt_from_brt.strftime('%d/%m %Hh')}: {e}")
    return []


def generate_claro_epg(days_ahead: int = 2, out_gz_path: Path = OUT_CLARO_GZ) -> int:
    session = create_session()

    now_brt = datetime.now(BRT_TZ).replace(minute=0, second=0, microsecond=0)
    start_brt = now_brt - timedelta(hours=3)
    end_brt = now_brt + timedelta(days=days_ahead)

    windows: list[tuple[datetime, datetime]] = []
    curr = start_brt
    while curr < end_brt:
        nxt = min(curr + timedelta(hours=6), end_brt)
        windows.append((curr, nxt))
        curr = nxt

    print(f"Consultando API Claro TV+ em {len(windows)} janelas de tempo (Fuso BRT -0300)...")

    tv_root = ET.Element("tv", {"generator-info-name": "claro-tvmais-extractor"})
    channels_by_id: dict[str, ET.Element] = {}
    progs_by_channel: dict[str, dict[str, tuple[datetime, ET.Element]]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_claro_mfwk_window, session, w_from, w_to) for w_from, w_to in windows]
        for fut in concurrent.futures.as_completed(futures):
            ch_list = fut.result()
            for ch in ch_list:
                if not isinstance(ch, dict):
                    continue
                ch_name = str(ch.get("name") or ch.get("group_id") or "").strip()
                if not ch_name:
                    continue

                xml_cid = make_channel_id(ch_name)
                if xml_cid not in channels_by_id:
                    ch_el = ET.Element("channel", {"id": xml_cid})
                    dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                    dn_el.text = ch_name

                    clean_n = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", "", ch_name, flags=re.I).strip()
                    if clean_n and clean_n != ch_name:
                        dn2 = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                        dn2.text = clean_n

                    logo = str(ch.get("image") or ch.get("logo") or "").strip()
                    if logo:
                        ET.SubElement(ch_el, "icon", {"src": logo})
                    channels_by_id[xml_cid] = ch_el

                events = ch.get("events") or []
                for ev in events:
                    if not isinstance(ev, dict):
                        continue

                    start_str, dt_start = parse_claro_time_to_brt(
                        unix_ts=ev.get("unix_begin"),
                        date_str=ev.get("date_begin"),
                        source_default_tz=BRT_TZ,
                    )
                    stop_str, dt_stop = parse_claro_time_to_brt(
                        unix_ts=ev.get("unix_end"),
                        date_str=ev.get("date_end"),
                        source_default_tz=BRT_TZ,
                    )

                    title = html.unescape(str(ev.get("name") or ev.get("title") or "").strip())
                    if not start_str or not stop_str or not dt_start or not dt_stop or not title:
                        continue
                    if dt_stop <= dt_start:
                        continue

                    ext = ev.get("ext") if isinstance(ev.get("ext"), dict) else {}
                    sub_title = html.unescape(
                        str(ext.get("episode_title") or ev.get("episode_title") or "").strip()
                    )
                    synopsis = html.unescape(
                        str(ev.get("description") or ext.get("description") or "").strip()
                    )
                    year = str(ext.get("year") or ev.get("year") or "").strip()[:4]

                    # Extrai Temporada e Episódio no formato [T03E07]
                    se_badge = ""
                    s_num = ext.get("season_number") or ev.get("season_number")
                    e_num = ext.get("episode_number") or ev.get("episode_number")
                    if s_num and e_num and str(s_num).isdigit() and str(e_num).isdigit():
                        s_i, e_i = int(s_num), int(e_num)
                        if 0 < s_i <= 60 and 0 < e_i <= 999:
                            se_badge = f"[T{s_i:02d}E{e_i:02d}]"

                    raw_rating = (
                        ev.get("parental_rating")
                        or ext.get("parental_rating")
                        or ev.get("rating_code")
                        or ""
                    )
                    rating_badge, rating_code = format_rating_badge(raw_rating)

                    dur_mins = int((dt_stop - dt_start).total_seconds() // 60)

                    cats: list[str] = []
                    raw_genres = ext.get("genres") or ev.get("genres") or ""
                    if isinstance(raw_genres, list):
                        cats = [str(g).strip() for g in raw_genres if str(g).strip()]
                    elif isinstance(raw_genres, str) and raw_genres.strip():
                        cats = [g.strip() for g in re.split(r"[,/]", raw_genres) if g.strip()]

                    p_el = ET.Element(
                        "programme",
                        {"start": start_str, "stop": stop_str, "channel": xml_cid},
                    )

                    t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                    t_el.text = title

                    desc_text = build_clean_visual_desc(
                        synopsis=synopsis,
                        rating_badge=rating_badge,
                        year=year,
                        duration_mins=dur_mins if 1 <= dur_mins <= 720 else None,
                        categories=cats,
                        sub_title=sub_title,
                        se_badge=se_badge,
                    )
                    if desc_text:
                        d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                        d_el.text = desc_text

                    if year and year.isdigit():
                        yr_el = ET.SubElement(p_el, "date")
                        yr_el.text = year

                    for c_name in cats[:5]:
                        c_el = ET.SubElement(p_el, "category", {"lang": "pt"})
                        c_el.text = c_name

                    poster = str(
                        ev.get("image_base_horizontal")
                        or ev.get("image_base_vertical")
                        or ext.get("image_base_horizontal")
                        or ""
                    ).strip()
                    if poster:
                        ET.SubElement(p_el, "icon", {"src": poster})

                    if rating_code:
                        r_el = ET.SubElement(p_el, "rating", {"system": "age"})
                        v_el = ET.SubElement(r_el, "value")
                        v_el.text = rating_code

                    progs_by_channel.setdefault(xml_cid, {})[start_str] = (dt_start, p_el)

    # 1. Adiciona todos os canais ordenados
    for cid in sorted(channels_by_id.keys()):
        tv_root.append(channels_by_id[cid])

    # 2. Adiciona todos os programas ESTRITAMENTE ordenados por horário de início em cada canal
    prog_count = 0
    for cid in sorted(progs_by_channel.keys()):
        sorted_events = sorted(progs_by_channel[cid].values(), key=lambda x: x[0])
        for _, p_elem in sorted_events:
            tv_root.append(p_elem)
            prog_count += 1

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Claro TV+ EPG salvo e ordenado em {out_gz_path} ({len(channels_by_id)} canais, {prog_count} programas em -0300).")
    return prog_count


if __name__ == "__main__":
    generate_claro_epg()
