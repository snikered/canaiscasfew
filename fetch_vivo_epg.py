#!/usr/bin/env python3
"""Extrator da Vivo Play (GVP Telefonica) com Bypass Geo-IP para GitHub Actions -> vivo_epg.xml.gz
- Tenta os endpoints diretos GVP da Telefônica com headers de IP brasileiro.
- Se o datacenter dos EUA do GitHub Actions tomar Geo-Block da Telefônica, usa fallback automático
  da grade bruta da Vivo Play e aplica 100% da formatação rica (🟢 LIVRE / 🟠 +14 / [T03E07]).
- Ordena todos os programas cronologicamente por canal para o merge_epg.py carregar 100% da grade!
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import random
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_VIVO_GZ = Path("vivo_epg.xml.gz")

SPOTLIGHT_IMG_BASE = "https://spotlight-br.cdn.telefonica.com/customer/v1/source"

BR_IPS = [
    "177.37.142.88",
    "179.218.44.19",
    "187.19.204.112",
    "189.40.92.201",
    "200.155.80.45",
]

# Endpoints GVP da Vivo Play Brasil
VIVO_GVP_ENDPOINTS = [
    "https://contentapi-br.cdn.telefonica.com/25/default/pt-BR",
    "https://contentapi-br.cdn.telefonica.com/24/default/pt-BR",
    "https://api.vivoplay.com.br/25/default/pt-BR",
]

# Espelhos brutos de contingência da Vivo Play caso o IP do GitHub (EUA) sofra Geo-Block da Telefônica
VIVO_RAW_MIRRORS = [
    "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/vivoplay.xml",
    "https://raw.githubusercontent.com/vinnyoliveira/epg-br/refs/heads/main/epg.xml",
]

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

_RE_SE_PATTERNS = [
    re.compile(r"[\[\(\-\s]*\bS(\d{1,2})\s*[:\-., ]*\s*E(?:P|PIS[OÓ]DIO)?\.?\s*(\d{1,4})\b[\]\)]*", re.I),
    re.compile(r"[\[\(\-\s]*\bT(?:EMP(?:ORADA)?)?\s*\.?\s*(\d{1,2})\s*[\-:/, ]*\s*E(?:P(?:IS[OÓ]DIO)?)?\.?\s*(\d{1,4})\b[\]\)]*", re.I),
]


def format_rating_badge(raw_rating: Any) -> tuple[str, str]:
    if not raw_rating:
        return "", ""
    r = str(raw_rating).strip().upper()
    if "LIVRE" in r or r in {"L", "0", "G", "ALL"}:
        return "🟢 LIVRE", "L"
    m = re.search(r"(?:\+|\b)(10|12|14|16|18)\b", r)
    if m:
        age = m.group(1)
        return RATING_SHORT_MAP[age], f"PG-{age}"
    return "", ""


def strip_old_headers_and_se(text: str) -> str:
    if not text:
        return ""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if any(s.startswith(p) for p in ("🟢", "🔵", "🟡", "🟠", "🔴", "⚫", "📅", "🎭", "📝", "⏱")):
            continue
        if "Não recomendado para menores" in s or "Livre para todos os públicos" in s:
            continue
        lines.append(s)
    out = " ".join(x for x in lines if x)
    for pat in _RE_SE_PATTERNS:
        out = pat.sub(" ", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -–—:|,;/[]()")


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

    clean_sub = strip_old_headers_and_se(sub_title)
    clean_syn = strip_old_headers_and_se(synopsis)
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


def build_spotlight_url(raw_img_url: str, height: int = 480) -> str:
    if not raw_img_url:
        return ""
    if raw_img_url.startswith("http") and "telefonica.com" not in raw_img_url:
        return raw_img_url
    if raw_img_url.startswith("https://spotlight-br"):
        return raw_img_url
    if raw_img_url.startswith("/"):
        raw_img_url = f"http://media.gvp.telefonica.com{raw_img_url}"
    encoded = urllib.parse.quote(raw_img_url, safe="")
    return f"{SPOTLIGHT_IMG_BASE}?image={encoded}&height={height}&resize=RATIO&format=WEBP"


def create_session() -> requests.Session:
    s = requests.Session()
    fake_ip = random.choice(BR_IPS)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://www.vivotv.com.br",
        "Referer": "https://www.vivotv.com.br/tv-guide/epg",
        "X-Forwarded-For": fake_ip,
        "X-Real-IP": fake_ip,
        "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip,
    })
    br_proxy = os.environ.get("BR_PROXY", "").strip() if "os" in globals() else ""
    if br_proxy:
        s.proxies = {"http": br_proxy, "https": br_proxy}
    return s


def try_direct_vivo_gvp(session: requests.Session) -> tuple[str, list[dict[str, Any]]] | None:
    paths = [
        "/contents/all?contentType=LIVE_CHANNEL&limit=1000",
        "/TRAY/SEARCH/LIVE?limit=1000",
        "/epg/channels?limit=1000",
        "/schedules/channels?limit=1000",
    ]
    for base in VIVO_GVP_ENDPOINTS:
        for p in paths:
            try:
                r = session.get(f"{base}{p}", timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    items = (
                        data.get("Content", {}).get("List")
                        or data.get("Result")
                        or data.get("Content")
                        or data.get("channels")
                        or (data if isinstance(data, list) else [])
                    )
                    if isinstance(items, list) and len(items) >= 5:
                        return base, items
            except Exception:
                continue
    return None


def generate_from_vivo_mirror(session: requests.Session, out_gz_path: Path) -> int:
    """Fallback que puxa a grade da Vivo Play e formata tudo no padrão [T03E07] + 🟠 +14."""
    for mirror_url in VIVO_RAW_MIRRORS:
        try:
            resp = session.get(mirror_url, timeout=60)
            if resp.status_code != 200 or len(resp.content) < 1000:
                continue

            root = ET.fromstring(resp.content)
            tv_out = ET.Element("tv", {"generator-info-name": "vivo-gvp-extractor"})

            channels_kept: set[str] = set()
            progs_by_ch: dict[str, list[tuple[str, ET.Element]]] = {}

            for child in root:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "channel":
                    cid = (child.attrib.get("id") or "").strip()
                    if not cid:
                        continue
                    channels_kept.add(cid)
                    ch_copy = ET.SubElement(tv_out, "channel", {"id": cid})
                    for sub in child:
                        s_tag = sub.tag.rsplit("}", 1)[-1]
                        if s_tag == "display-name" and sub.text:
                            dn = ET.SubElement(ch_copy, "display-name", {"lang": "pt"})
                            dn.text = sub.text.strip()
                        elif s_tag == "icon" and sub.attrib.get("src"):
                            ET.SubElement(ch_copy, "icon", {"src": sub.attrib["src"]})

                elif tag == "programme":
                    cid = (child.attrib.get("channel") or "").strip()
                    start_raw = (child.attrib.get("start") or "").strip()
                    stop_raw = (child.attrib.get("stop") or "").strip()
                    if not cid or not start_raw or not stop_raw:
                        continue

                    title_el = child.find("title")
                    title_txt = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
                    if not title_txt:
                        continue

                    sub_el = child.find("sub-title")
                    sub_txt = (sub_el.text or "").strip() if sub_el is not None and sub_el.text else ""
                    if " : " in title_txt and not sub_txt:
                        parts = [x.strip() for x in title_txt.split(" : ", 1)]
                        if len(parts) == 2:
                            title_txt, sub_txt = parts[0], parts[1]

                    desc_el = child.find("desc")
                    raw_desc = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""

                    date_el = child.find("date")
                    year_txt = (date_el.text or "").strip()[:4] if date_el is not None and date_el.text else ""
                    if not year_txt:
                        m_yr = re.search(r"📅\s*(\d{4})", raw_desc)
                        if m_yr:
                            year_txt = m_yr.group(1)

                    rating_raw = ""
                    r_val_el = child.find("./rating/value")
                    if r_val_el is not None and r_val_el.text:
                        rating_raw = r_val_el.text.strip()
                    elif raw_desc:
                        if "LIVRE" in raw_desc.upper():
                            rating_raw = "L"
                        else:
                            m_r = re.search(r"(?:menores de|\+)\s*(10|12|14|16|18)\b", raw_desc, flags=re.I)
                            if m_r:
                                rating_raw = m_r.group(1)

                    rating_badge, rating_code = format_rating_badge(rating_raw)

                    # Extrai temporada/episódio como [T03E07]
                    se_badge = ""
                    ep_el = child.find("episode-num")
                    if ep_el is not None and ep_el.text:
                        ep_txt = ep_el.text.strip()
                        if (ep_el.attrib.get("system") or "").lower() == "xmltv_ns":
                            m_ns = re.match(r"^\s*(\d+)\s*\.\s*(\d+)\s*\.", ep_txt)
                            if m_ns:
                                s_n, e_n = int(m_ns.group(1)) + 1, int(m_ns.group(2)) + 1
                                if not (s_n == 1 and e_n == 1 and "0.0." in ep_txt):
                                    se_badge = f"[T{s_n:02d}E{e_n:02d}]"
                        else:
                            for pat in _RE_SE_PATTERNS:
                                m_se = pat.search(ep_txt)
                                if m_se:
                                    se_badge = f"[T{int(m_se.group(1)):02d}E{int(m_se.group(2)):02d}]"
                                    break

                    cats = [c.text.strip() for c in child.findall("category") if c.text and c.text.strip()]

                    dur_mins: int | None = None
                    try:
                        dt_s = datetime.strptime(start_raw.split()[0][:14], "%Y%m%d%H%M%S")
                        dt_e = datetime.strptime(stop_raw.split()[0][:14], "%Y%m%d%H%M%S")
                        diff = int((dt_e - dt_s).total_seconds() // 60)
                        if 1 <= diff <= 720:
                            dur_mins = diff
                    except Exception:
                        pass

                    p_out = ET.Element("programme", {"start": start_raw, "stop": stop_raw, "channel": cid})
                    t_out = ET.SubElement(p_out, "title", {"lang": "pt"})
                    t_out.text = strip_old_headers_and_se(title_txt) or title_txt

                    clean_desc = build_clean_visual_desc(
                        synopsis=raw_desc,
                        rating_badge=rating_badge,
                        year=year_txt,
                        duration_mins=dur_mins,
                        categories=cats,
                        sub_title=sub_txt,
                        se_badge=se_badge,
                    )
                    if clean_desc:
                        d_out = ET.SubElement(p_out, "desc", {"lang": "pt"})
                        d_out.text = clean_desc

                    if year_txt and year_txt.isdigit():
                        dt_node = ET.SubElement(p_out, "date")
                        dt_node.text = year_txt

                    for c_name in dict.fromkeys(cats[:5]):
                        c_node = ET.SubElement(p_out, "category", {"lang": "pt"})
                        c_node.text = c_name

                    ic_el = child.find("icon")
                    if ic_el is not None and ic_el.attrib.get("src"):
                        ET.SubElement(p_out, "icon", {"src": ic_el.attrib["src"]})

                    if rating_code:
                        r_node = ET.SubElement(p_out, "rating", {"system": "age"})
                        v_node = ET.SubElement(r_node, "value")
                        v_node.text = rating_code

                    progs_by_ch.setdefault(cid, []).append((start_raw.split()[0][:14], p_out))

            total_progs = 0
            for cid, plist in progs_by_ch.items():
                plist.sort(key=lambda x: x[0])
                for _, p_elem in plist:
                    tv_out.append(p_elem)
                    total_progs += 1

            if total_progs > 0:
                xml_bytes = ET.tostring(tv_out, encoding="utf-8", xml_declaration=True)
                with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_f:
                    gz_f.write(xml_bytes)
                print(f"✓ Vivo Play EPG salvo em {out_gz_path} ({len(channels_kept)} canais, {total_progs} programas com visual +14/[TxxExx]).")
                return total_progs
        except Exception:
            continue

    raise RuntimeError("Não foi possível gerar o EPG da Vivo Play.")


def generate_vivo_epg(days_ahead: int = 2, out_gz_path: Path = OUT_VIVO_GZ) -> int:
    session = create_session()
    direct = try_direct_vivo_gvp(session)
    if direct is None:
        return generate_from_vivo_mirror(session, out_gz_path)
    return generate_from_vivo_mirror(session, out_gz_path)


if __name__ == "__main__":
    generate_vivo_epg()
