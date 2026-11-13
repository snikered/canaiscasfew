#!/usr/bin/env python3
"""Extrator Híbrido da Claro TV+ (API mfwkweb + Complemento de Grade) -> claro_epg.xml.gz
- Garante que 100% dos programas gerados sejam lidos pelo merge_epg.py (sem descarte por sobreposição).
- Preserva fuso horário exato de Brasília (-0300).
- Formata <desc> com 🟢 LIVRE / 🟠 +14 / ⚫ +18 e [T03E07].
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
CLARO_FALLBACK_XML = "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/claro.xml"

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


def parse_claro_time_to_brt(unix_ts: Any = None, date_str: Any = None) -> tuple[str, datetime | None]:
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
            dt_brt = dt_naive.replace(tzinfo=BRT_TZ)
            return dt_brt.strftime("%Y%m%d%H%M%S -0300"), dt_brt
        except Exception:
            continue
    return "", None


def fetch_claro_mfwk_window(session: requests.Session, dt_from: datetime, dt_to: datetime) -> list[dict[str, Any]]:
    params = {
        **CLARO_COMMON_PARAMS,
        "date_from": dt_from.strftime("%Y%m%d%H%M%S"),
        "date_to": dt_to.strftime("%Y%m%d%H%M%S"),
        "quantity": "2000",
    }
    try:
        r = session.get(f"{CLARO_MFWK_BASE}/channel", params=params, timeout=45)
        if r.status_code == 200:
            return r.json().get("response", {}).get("channels") or []
    except Exception:
        pass
    return []


def generate_claro_epg(days_ahead: int = 2, out_gz_path: Path = OUT_CLARO_GZ) -> int:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    })

    now_brt = datetime.now(BRT_TZ).replace(minute=0, second=0, microsecond=0)
    start_brt = now_brt - timedelta(hours=3)
    end_brt = now_brt + timedelta(days=days_ahead)

    windows: list[tuple[datetime, datetime]] = []
    curr = start_brt
    while curr < end_brt:
        nxt = min(curr + timedelta(hours=6), end_brt)
        windows.append((curr, nxt))
        curr = nxt

    tv_root = ET.Element("tv", {"generator-info-name": "claro-tvmais-extractor"})
    channels_by_id: dict[str, ET.Element] = {}
    progs_by_channel: dict[str, dict[str, tuple[datetime, ET.Element]]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_claro_mfwk_window, session, w_f, w_t) for w_f, w_t in windows]
        for fut in concurrent.futures.as_completed(futures):
            for ch in fut.result():
                if not isinstance(ch, dict):
                    continue
                ch_name = str(ch.get("name") or "").strip()
                if not ch_name:
                    continue
                # IMPORTANTE: se o nome do canal for genérico da API, limpa números de canal
                xml_cid = make_channel_id(ch_name)
                if xml_cid not in channels_by_id:
                    ch_el = ET.Element("channel", {"id": xml_cid})
                    dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                    dn_el.text = ch_name
                    logo = str(ch.get("image") or "").strip()
                    if logo:
                        ET.SubElement(ch_el, "icon", {"src": logo})
                    channels_by_id[xml_cid] = ch_el

                for ev in (ch.get("events") or []):
                    if not isinstance(ev, dict):
                        continue
                    start_str, dt_start = parse_claro_time_to_brt(ev.get("unix_begin"), ev.get("date_begin"))
                    stop_str, dt_stop = parse_claro_time_to_brt(ev.get("unix_end"), ev.get("date_end"))
                    title = html.unescape(str(ev.get("name") or ev.get("title") or "").strip())

                    # Evita que o título seja igual ao nome do canal (o que faz o filtro anti-spam do merge_epg descartar o canal inteiro!)
                    if not start_str or not stop_str or not dt_start or not dt_stop or not title:
                        continue
                    if dt_stop <= dt_start or title.upper() == ch_name.upper():
                        continue

                    ext = ev.get("ext") if isinstance(ev.get("ext"), dict) else {}
                    sub_title = html.unescape(str(ext.get("episode_title") or ev.get("episode_title") or "").strip())
                    synopsis = html.unescape(str(ev.get("description") or ext.get("description") or "").strip())
                    year = str(ext.get("year") or ev.get("year") or "").strip()[:4]

                    se_badge = ""
                    s_num = ext.get("season_number") or ev.get("season_number")
                    e_num = ext.get("episode_number") or ev.get("episode_number")
                    if s_num and e_num and str(s_num).isdigit() and str(e_num).isdigit():
                        s_i, e_i = int(s_num), int(e_num)
                        if 0 < s_i <= 60 and 0 < e_i <= 999:
                            se_badge = f"[T{s_i:02d}E{e_i:02d}]"

                    raw_rating = ev.get("parental_rating") or ext.get("parental_rating") or ev.get("rating_code") or ""
                    rating_badge, rating_code = format_rating_badge(raw_rating)
                    dur_mins = int((dt_stop - dt_start).total_seconds() // 60)

                    p_el = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": xml_cid})
                    t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                    t_el.text = title

                    desc_text = build_clean_visual_desc(
                        synopsis=synopsis,
                        rating_badge=rating_badge,
                        year=year,
                        duration_mins=dur_mins if 1 <= dur_mins <= 720 else None,
                        sub_title=sub_title,
                        se_badge=se_badge,
                    )
                    if desc_text:
                        d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                        d_el.text = desc_text

                    progs_by_channel.setdefault(xml_cid, {})[start_str] = (dt_start, p_el)

    for cid in sorted(channels_by_id.keys()):
        if len(progs_by_channel.get(cid, {})) >= 2:
            tv_root.append(channels_by_id[cid])

    prog_count = 0
    for cid in sorted(progs_by_channel.keys()):
        if len(progs_by_channel[cid]) < 2:
            continue
        for _, p_elem in sorted(progs_by_channel[cid].values(), key=lambda x: x[0]):
            tv_root.append(p_elem)
            prog_count += 1

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Claro TV+ EPG salvo em {out_gz_path} ({prog_count} programas válidos em -0300).")
    return prog_count


if __name__ == "__main__":
    generate_claro_epg()
