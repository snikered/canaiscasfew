#!/usr/bin/env python3
"""Extrator Direto da Claro TV+ (Claro Video / NET Gatekeeper) -> XMLTV (claro_epg.xml.gz)
- Blindado contra bug de Fuso Horário: Prioriza Epoch Unix (unix_begin/unix_end) e trata offsets ISO (-03:00 vs UTC)
  convertendo sempre com precisão matemática para America/Sao_Paulo (-0300).
- Formata <desc> com o padrão enxuto:
    🟢 LIVRE | 📅 2024 | ⏱ 45 mins | 🎭 Esportes, Futebol
    🟠 +14 | 📅 2021 | ⏱ 60 mins | 🎭 Séries, Policial
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

# Fuso Horário Oficial do Brasil (BRT = UTC-3)
BRT_TZ = timezone(timedelta(hours=-3))
OUT_CLARO_GZ = Path("claro_epg.xml.gz")

# Endpoints oficiais da Claro TV+ Brasil
CLARO_MFWK_BASE = "https://mfwkweb-api.clarovideo.net/services/epg"
CLARO_GATEKEEPER_BASE = "https://programacao.claro.com.br/gatekeeper"

# Parâmetros padrão exigidos pela API mfwkweb da Claro TV+ Brasil
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
    """Converte classificação da Claro para ('🟢 LIVRE', 'L') ou ('🟠 +14', 'PG-14')."""
    if raw_rating is None:
        return "", ""
    r = str(raw_rating).strip().upper()
    if not r:
        return "", ""
    if "LIVRE" in r or r in {"L", "0", "G", "ALL"}:
        return "🟢 LIVRE", "L"
    m = re.search(r"\b(10|12|14|16|18)\b", r)
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
) -> str:
    """Monta o cabeçalho curto com bolinha +14/+18 ou LIVRE na mesma linha dos metadados."""
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


def make_channel_id(name: str) -> str:
    """Gera ID padronizado ex: 'SporTV 2 HD' -> 'SporTV2.br'."""
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
    """Conversor de Horário Blindado para a Claro TV+:
    1. Se tiver unix_begin / unix_end (Epoch UTC em segundos ou ms), usa ele como verdade absoluta!
    2. Se vier string ISO com fuso ('2026-11-13T18:00:00-03:00' ou 'Z'), respeita o offset e converte para BRT (-0300).
    3. Se vier string sem fuso ('2026/11/13 18:00:00' ou '20261113180000'), aplica source_default_tz e converte para -0300.
    """
    # 1. Prioridade máxima: Timestamp Unix (nunca erra fuso horário)
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

    # 2. Caso já venha no formato XMLTV ex: "20261113150000 -0300" ou "20261113180000 +0000"
    m_xml = re.match(r"^(\d{14})(?:\s*([+-]\d{4}|Z))?$", raw)
    if m_xml:
        digits, tz_part = m_xml.group(1), m_xml.group(2)
        dt_naive = datetime.strptime(digits, "%Y%m%d%H%M%S")
        if tz_part in {"Z", "+0000", "-0000"}:
            dt_aware = dt_naive.replace(tzinfo=timezone.utc).astimezone(BRT_TZ)
        elif tz_part and len(tz_part) == 5:
            sign = -1 if tz_part[0] == "-" else 1
            hh, mm = int(tz_part[1:3]), int(tz_part[3:5])
            custom_tz = timezone(timedelta(minutes=sign * (hh * 60 + mm)))
            dt_aware = dt_naive.replace(tzinfo=custom_tz).astimezone(BRT_TZ)
        else:
            dt_aware = dt_naive.replace(tzinfo=source_default_tz).astimezone(BRT_TZ)
        return dt_aware.strftime("%Y%m%d%H%M%S -0300"), dt_aware

    # 3. Formato ISO 8601 (ex: "2026-11-13T15:30:00-03:00" ou "2026-11-13T18:30:00Z")
    try:
        iso_norm = raw.replace("/", "-")
        if iso_norm.endswith("Z"):
            iso_norm = iso_norm[:-1] + "+00:00"
        dt_parsed = datetime.fromisoformat(iso_norm)
        if dt_parsed.tzinfo is None:
            dt_parsed = dt_parsed.replace(tzinfo=source_default_tz)
        dt_brt = dt_parsed.astimezone(BRT_TZ)
        return dt_brt.strftime("%Y%m%d%H%M%S -0300"), dt_brt
    except Exception:
        pass

    # 4. Outros formatos comuns da Claro ("YYYY/MM/DD HH:MM:SS" ou "DD/MM/YYYY HH:MM:SS")
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
    """Consulta um bloco de horas na API mfwkweb da Claro TV+."""
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

    # Sempre ancorado no horário de Brasília (-03:00), arredondado para o início da hora
    now_brt = datetime.now(BRT_TZ).replace(minute=0, second=0, microsecond=0)
    start_brt = now_brt - timedelta(hours=3)
    end_brt = now_brt + timedelta(days=days_ahead)

    # Divide em janelas de 12 horas para a API da Claro não truncar eventos
    windows: list[tuple[datetime, datetime]] = []
    curr = start_brt
    while curr < end_brt:
        nxt = min(curr + timedelta(hours=12), end_brt)
        windows.append((curr, nxt))
        curr = nxt

    print(f"Consultando API Claro TV+ em {len(windows)} janelas de tempo (Fuso BRT -0300)...")

    tv_root = ET.Element("tv", {"generator-info-name": "claro-tvmais-extractor"})
    seen_channels: set[str] = set()
    seen_programmes: set[tuple[str, str]] = set()
    prog_count = 0

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
                if xml_cid not in seen_channels:
                    seen_channels.add(xml_cid)
                    ch_el = ET.SubElement(tv_root, "channel", {"id": xml_cid})
                    dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                    dn_el.text = ch_name

                    clean_n = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", "", ch_name, flags=re.I).strip()
                    if clean_n and clean_n != ch_name:
                        dn2 = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                        dn2.text = clean_n

                    logo = str(ch.get("image") or ch.get("logo") or "").strip()
                    if logo:
                        ET.SubElement(ch_el, "icon", {"src": logo})

                events = ch.get("events") or []
                for ev in events:
                    if not isinstance(ev, dict):
                        continue

                    # Usa unix_begin / unix_end com prioridade para garantir zero erro de fuso horário!
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
                    if not start_str or not stop_str or not title:
                        continue

                    # Evita programas duplicados na fronteira entre duas janelas de 12h
                    dedup_key = (xml_cid, start_str)
                    if dedup_key in seen_programmes:
                        continue
                    seen_programmes.add(dedup_key)

                    ext = ev.get("ext") if isinstance(ev.get("ext"), dict) else {}
                    sub_title = html.unescape(
                        str(ext.get("episode_title") or ev.get("episode_title") or "").strip()
                    )
                    synopsis = html.unescape(
                        str(ev.get("description") or ext.get("description") or "").strip()
                    )
                    year = str(ext.get("year") or ev.get("year") or "").strip()[:4]

                    raw_rating = (
                        ev.get("parental_rating")
                        or ext.get("parental_rating")
                        or ev.get("rating_code")
                        or ""
                    )
                    rating_badge, rating_code = format_rating_badge(raw_rating)

                    dur_mins: int | None = None
                    if dt_start and dt_stop:
                        diff = int((dt_stop - dt_start).total_seconds() // 60)
                        if 1 <= diff <= 720:
                            dur_mins = diff

                    cats: list[str] = []
                    raw_genres = ext.get("genres") or ev.get("genres") or ev.get("talent") or ""
                    if isinstance(raw_genres, list):
                        cats = [str(g).strip() for g in raw_genres if str(g).strip()]
                    elif isinstance(raw_genres, str) and raw_genres.strip():
                        cats = [g.strip() for g in re.split(r"[,/]", raw_genres) if g.strip()]

                    p_el = ET.SubElement(
                        tv_root,
                        "programme",
                        {"start": start_str, "stop": stop_str, "channel": xml_cid},
                    )

                    t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                    t_el.text = (
                        f"{title} : {sub_title}"
                        if sub_title and sub_title.lower() not in title.lower()
                        else title
                    )

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

                    prog_count += 1

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Claro TV+ EPG salvo em {out_gz_path} ({len(seen_channels)} canais, {prog_count} programas em -0300).")
    return prog_count


if __name__ == "__main__":
    generate_claro_epg()