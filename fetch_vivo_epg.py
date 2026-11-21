#!/usr/bin/env python3
"""Extrator da Vivo Play (GVP Telefonica) -> vivo_epg.xml.gz
- Tenta primeiro a API GVP direta da Telefônica com headers de IP brasileiro.
- Usa o espelho vivoplay.xml (limaalef) SOMENTE para canais não obtidos pela API direta.
- Aplica formatação rica (🟢 LIVRE / 🟠 +14 / [T03E07]) e ordena cronologicamente por canal.
"""
from __future__ import annotations

import gzip
import html
import os
import random
import re
import unicodedata
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

VIVO_GVP_ENDPOINTS = [
    "https://contentapi-br.cdn.telefonica.com/25/default/pt-BR",
    "https://contentapi-br.cdn.telefonica.com/24/default/pt-BR",
    "https://api.vivoplay.com.br/25/default/pt-BR",
]

# Apenas espelho real da Vivo Play (sem agregadores genéricos com buracos)
VIVO_FALLBACK_MIRROR = "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/vivoplay.xml"

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
    if re.search(r"\bLOVE\s*NATURE\b", s, flags=re.I):
        return "LoveNature.br"
    words = re.findall(r"[A-Za-z0-9]+", s)
    camel = "".join(w.capitalize() if not w.isupper() else w for w in words)
    return f"{camel or 'Canal'}.br"


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
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        s.proxies = {"http": br_proxy, "https": br_proxy}
    return s


def _epoch_to_xmltv_brt(ts_val: Any) -> tuple[str, datetime] | tuple[None, None]:
    if ts_val is None:
        return None, None
    try:
        if isinstance(ts_val, (int, float)):
            sec = ts_val / 1000.0 if ts_val > 10_000_000_000 else float(ts_val)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(BRT_TZ)
        else:
            s = str(ts_val).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s).astimezone(BRT_TZ)
        return dt.strftime("%Y%m%d%H%M%S -0300"), dt
    except Exception:
        return None, None


def fetch_from_gvp_api(
    session: requests.Session,
    days_ahead: int,
    channels_by_id: dict[str, ET.Element],
    progs_by_ch: dict[str, dict[str, ET.Element]],
) -> int:
    """Tenta buscar canais e grade direto na API GVP da Vivo Play."""
    api_channels: dict[str, str] = {}  # gvp_channel_id -> xml_cid
    working_base = ""

    for base in VIVO_GVP_ENDPOINTS:
        try:
            url = f"{base}/contents/all?ca_deviceTypes=null%7C401&contentTypes=LCH&limit=1000"
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                print(f"  - [Vivo GVP API] {base} -> HTTP {r.status_code}")
                continue
            payload = r.json()
            items = payload.get("Content") or payload.get("contents") or payload.get("items") or []
            if not isinstance(items, list) or not items:
                continue

            working_base = base
            for ch in items:
                gvp_id = str(ch.get("Pid") or ch.get("Id") or ch.get("id") or "").strip()
                name = str(ch.get("Title") or ch.get("Name") or ch.get("title") or "").strip()
                if not gvp_id or not name:
                    continue
                cid = make_channel_id(name)
                api_channels[gvp_id] = cid
                if cid not in channels_by_id:
                    ch_el = ET.Element("channel", {"id": cid})
                    dn = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                    dn.text = name
                    channels_by_id[cid] = ch_el
            break
        except Exception as exc:
            print(f"  - [Vivo GVP API] {base} falhou: {exc}")
            continue

    if not working_base or not api_channels:
        print("  ⚠ [Vivo GVP API] Indisponível ou bloqueada por Geo-IP; seguindo para fallback vivoplay.xml.")
        return 0

    now_brt = datetime.now(BRT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    added_progs = 0

    for d in range(days_ahead + 1):
        day_start = now_brt + timedelta(days=d)
        day_end = day_start + timedelta(days=1)
        st_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
        et_ms = int(day_end.astimezone(timezone.utc).timestamp() * 1000)

        try:
            sched_url = (
                f"{working_base}/schedules?ca_deviceTypes=null%7C401"
                f"&starttime={st_ms}&endtime={et_ms}&filteravailability=false"
            )
            r = session.get(sched_url, timeout=30)
            if r.status_code != 200:
                continue
            data = r.json()
            entries = data.get("Content") or data.get("schedules") or data.get("items") or []
            if not isinstance(entries, list):
                continue

            for ev in entries:
                gvp_ch = str(ev.get("ChannelId") or ev.get("ChannelPid") or "").strip()
                cid = api_channels.get(gvp_ch)
                if not cid:
                    continue

                start_str, dt_s = _epoch_to_xmltv_brt(ev.get("Start") or ev.get("StartTime"))
                stop_str, dt_e = _epoch_to_xmltv_brt(ev.get("End") or ev.get("EndTime"))
                title = str(ev.get("Title") or "").strip()
                if not start_str or not stop_str or not title or not dt_s or not dt_e:
                    continue

                raw_desc = str(ev.get("Description") or ev.get("Synopsis") or "").strip()
                rating_badge, rating_code = format_rating_badge(ev.get("AgeRatingPid") or ev.get("AgeRating"))
                dur_mins = int((dt_e - dt_s).total_seconds() // 60)
                if not (1 <= dur_mins <= 720):
                    dur_mins = None

                cats = []
                for g in ev.get("Genres") or []:
                    g_name = g.get("Name") if isinstance(g, dict) else str(g)
                    if g_name and g_name.strip():
                        cats.append(g_name.strip())

                se_badge = ""
                s_num, e_num = ev.get("SeasonNumber"), ev.get("EpisodeNumber")
                if str(s_num).isdigit() and str(e_num).isdigit():
                    se_badge = f"[T{int(s_num):02d}E{int(e_num):02d}]"

                p_out = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": cid})
                t_out = ET.SubElement(p_out, "title", {"lang": "pt"})
                t_out.text = strip_old_headers_and_se(title) or title

                clean_desc = build_clean_visual_desc(
                    synopsis=raw_desc,
                    rating_badge=rating_badge,
                    duration_mins=dur_mins,
                    categories=cats,
                    se_badge=se_badge,
                )
                if clean_desc:
                    d_out = ET.SubElement(p_out, "desc", {"lang": "pt"})
                    d_out.text = clean_desc

                for c_name in dict.fromkeys(cats[:5]):
                    c_node = ET.SubElement(p_out, "category", {"lang": "pt"})
                    c_node.text = c_name

                if rating_code:
                    r_node = ET.SubElement(p_out, "rating", {"system": "age"})
                    v_node = ET.SubElement(r_node, "value")
                    v_node.text = rating_code

                sort_key = start_str.split()[0][:14]
                progs_by_ch.setdefault(cid, {})[sort_key] = p_out
                added_progs += 1
        except Exception:
            continue

    api_ch_with_progs = sum(1 for c in channels_by_id if progs_by_ch.get(c))
    print(f"  ✓ [Vivo GVP API] {api_ch_with_progs} canais | {added_progs} programas obtidos direto da API.")
    return added_progs


def fill_missing_from_vivo_mirror(
    session: requests.Session,
    channels_by_id: dict[str, ET.Element],
    progs_by_ch: dict[str, dict[str, ET.Element]],
) -> int:
    """Usa vivoplay.xml SOMENTE para canais que a API direta não retornou."""
    try:
        resp = session.get(VIVO_FALLBACK_MIRROR, timeout=60)
        if resp.status_code != 200 or len(resp.content) < 1000:
            print(f"  ⚠ [Vivo Fallback] Falha ao baixar vivoplay.xml (HTTP {resp.status_code}).")
            return 0
        root = ET.fromstring(resp.content)
    except Exception as exc:
        print(f"  ⚠ [Vivo Fallback] Erro ao ler vivoplay.xml: {exc}")
        return 0

    # Canais que já têm grade vinda da API direta ficam protegidos
    already_covered = {c for c, pmap in progs_by_ch.items() if len(pmap) >= 5}
    fallback_channels: set[str] = set()

    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "channel":
            cid = (child.attrib.get("id") or "").strip()
            if not cid or cid in already_covered:
                continue
            if cid not in channels_by_id:
                ch_copy = ET.Element("channel", {"id": cid})
                seen_icons: set[str] = set()
                for sub in child:
                    s_tag = sub.tag.rsplit("}", 1)[-1]
                    if s_tag == "display-name" and sub.text:
                        dn = ET.SubElement(ch_copy, "display-name", {"lang": "pt"})
                        dn.text = sub.text.strip()
                    elif s_tag == "icon" and sub.attrib.get("src"):
                        ic_url = sub.attrib["src"].strip()
                        if ic_url and ic_url not in seen_icons:
                            seen_icons.add(ic_url)
                            ET.SubElement(ch_copy, "icon", {"src": ic_url})
                channels_by_id[cid] = ch_copy
            fallback_channels.add(cid)

    added_progs = 0
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag != "programme":
            continue
        cid = (child.attrib.get("channel") or "").strip()
        if not cid or cid not in fallback_channels or cid in already_covered:
            continue
        start_raw = (child.attrib.get("start") or "").strip()
        stop_raw = (child.attrib.get("stop") or "").strip()
        if not start_raw or not stop_raw:
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

        sort_key = start_raw.split()[0][:14]
        progs_by_ch.setdefault(cid, {})[sort_key] = p_out
        added_progs += 1

    print(f"  ✓ [Vivo Fallback] {len(fallback_channels)} canais complementares | {added_progs} programas via vivoplay.xml.")
    return added_progs


def generate_vivo_epg(days_ahead: int = 2, out_gz_path: Path = OUT_VIVO_GZ) -> int:
    session = create_session()
    tv_out = ET.Element("tv", {"generator-info-name": "vivo-gvp-extractor"})
    channels_by_id: dict[str, ET.Element] = {}
    progs_by_ch: dict[str, dict[str, ET.Element]] = {}

    fetch_from_gvp_api(session, days_ahead, channels_by_id, progs_by_ch)
    fill_missing_from_vivo_mirror(session, channels_by_id, progs_by_ch)

    valid_channels = 0
    for cid in sorted(channels_by_id.keys()):
        if progs_by_ch.get(cid):
            tv_out.append(channels_by_id[cid])
            valid_channels += 1

    total_progs = 0
    for cid in sorted(progs_by_ch.keys()):
        for sort_key in sorted(progs_by_ch[cid].keys()):
            tv_out.append(progs_by_ch[cid][sort_key])
            total_progs += 1

    if total_progs > 0:
        xml_bytes = ET.tostring(tv_out, encoding="utf-8", xml_declaration=True)
        with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_f:
            gz_f.write(xml_bytes)
        print(f"✓ Vivo Play EPG salvo em {out_gz_path} ({valid_channels} canais, {total_progs} programas).")
        return total_progs

    raise RuntimeError("Não foi possível gerar o EPG da Vivo Play.")


if __name__ == "__main__":
    generate_vivo_epg()
