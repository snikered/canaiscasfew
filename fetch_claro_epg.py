#!/usr/bin/env python3
"""Extrator Híbrido da Claro TV+ (API mfwkweb + Complemento Multi-Espelho) -> claro_epg.xml.gz
- Mostra diagnóstico real de quantos canais vieram da API vs. Fallback XML.
- Grade sintética do Love Nature desativada por padrão (ative com ALLOW_SYNTHETIC_LOVE_NATURE=1).
- Preserva fuso horário exato de Brasília (-0300).
- Formata <desc> com 🟢 LIVRE / 🟠 +14 / ⚫ +18 e [T03E07].
"""
from __future__ import annotations

import concurrent.futures
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
OUT_CLARO_GZ = Path("claro_epg.xml.gz")
ALLOW_SYNTHETIC_LOVE_NATURE = os.environ.get("ALLOW_SYNTHETIC_LOVE_NATURE", "0").strip().lower() in {"1", "true", "yes"}

CLARO_MFWK_BASE = "https://mfwkweb-api.clarovideo.net/services/epg"
CLARO_FALLBACK_XMLS = [
    "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/claro.xml",
    "https://www.open-epg.com/files/brazil.xml",
]

BR_IPS = [
    "177.37.142.88",
    "179.218.44.19",
    "187.19.204.112",
    "189.40.92.201",
    "200.155.80.45",
]

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

_RE_SE_PATTERNS = [
    re.compile(r"[\[\(\-\s]*\bS(\d{1,2})\s*[:\-., ]*\s*E(?:P|PIS[OÓ]DIO)?\.?\s*(\d{1,4})\b[\]\)]*", re.I),
    re.compile(r"[\[\(\-\s]*\bT(?:EMP(?:ORADA)?)?\s*\.?\s*(\d{1,2})\s*[\-:/, ]*\s*E(?:P(?:IS[OÓ]DIO)?)?\.?\s*(\d{1,4})\b[\]\)]*", re.I),
]


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
    s = re.sub(r"\.(?:br|us|uk|pt|ar|mx|cl|co|pe|ca|latam|int)$", " ", s.strip(), flags=re.I)
    s = re.sub(r"\b(FHD|UHD|4K|HD|SD)\b", " ", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    if re.search(r"\bLOVE\s*NATURE\b", s, flags=re.I):
        return "LoveNature.br"
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
    m_xmltv = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", raw)
    if m_xmltv:
        digits, offset = m_xmltv.groups()
        try:
            offset = offset or "-0300"
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
            dt = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=tz).astimezone(BRT_TZ)
            return dt.strftime("%Y%m%d%H%M%S -0300"), dt
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            dt_naive = datetime.strptime(raw[:19], fmt)
            dt_brt = dt_naive.replace(tzinfo=BRT_TZ)
            return dt_brt.strftime("%Y%m%d%H%M%S -0300"), dt_brt
        except Exception:
            continue
    return "", None


def fetch_claro_mfwk_window(session: requests.Session, dt_from: datetime, dt_to: datetime) -> tuple[list[dict[str, Any]], str]:
    params = {
        **CLARO_COMMON_PARAMS,
        "date_from": dt_from.strftime("%Y%m%d%H%M%S"),
        "date_to": dt_to.strftime("%Y%m%d%H%M%S"),
        "quantity": "2000",
    }
    try:
        r = session.get(f"{CLARO_MFWK_BASE}/channel", params=params, timeout=45)
        if r.status_code == 200:
            channels = r.json().get("response", {}).get("channels") or []
            return channels, ""
        return [], f"HTTP {r.status_code}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def supplement_from_fallback_xmls(
    session: requests.Session,
    channels_by_id: dict[str, ET.Element],
    progs_by_channel: dict[str, dict[str, tuple[datetime, ET.Element]]],
) -> None:
    """Complementa a grade da Claro TV+ usando CLARO_FALLBACK_XMLS para qualquer canal ausente na API mfwkweb."""
    for url in CLARO_FALLBACK_XMLS:
        try:
            resp = session.get(url, timeout=60)
            if resp.status_code != 200 or len(resp.content) < 1000:
                print(f"  ⚠ Fallback {url} retornou HTTP {resp.status_code} ({len(resp.content)} bytes)")
                continue
            root = ET.fromstring(resp.content)
            raw_id_to_xml_cid: dict[str, str] = {}

            for child in root:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "channel":
                    raw_cid = (child.attrib.get("id") or "").strip()
                    if not raw_cid:
                        continue
                    dns = [(n.text or "").strip() for n in child if n.tag.rsplit("}", 1)[-1] == "display-name" and (n.text or "").strip()]
                    ch_name = dns[0] if dns else raw_cid
                    xml_cid = make_channel_id(ch_name)
                    raw_id_to_xml_cid[raw_cid] = xml_cid

                    if xml_cid not in channels_by_id:
                        ch_el = ET.Element("channel", {"id": xml_cid})
                        dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                        dn_el.text = ch_name
                        for d_extra in dns[1:4]:
                            dn_x = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                            dn_x.text = d_extra
                        for sub in child:
                            if sub.tag.rsplit("}", 1)[-1] == "icon" and sub.attrib.get("src"):
                                ET.SubElement(ch_el, "icon", {"src": sub.attrib["src"]})
                                break
                        channels_by_id[xml_cid] = ch_el

            sparse_cids = {cid for cid in channels_by_id if len(progs_by_channel.get(cid, {})) < 5}
            added_progs = 0
            for child in root:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag != "programme":
                    continue
                raw_cid = (child.attrib.get("channel") or "").strip()
                xml_cid = raw_id_to_xml_cid.get(raw_cid)
                if not xml_cid or xml_cid not in sparse_cids:
                    continue

                start_str, dt_start = parse_claro_time_to_brt(date_str=child.attrib.get("start"))
                stop_str, dt_stop = parse_claro_time_to_brt(date_str=child.attrib.get("stop"))
                if not start_str or not stop_str or not dt_start or not dt_stop or dt_stop <= dt_start:
                    continue

                title_el = child.find("title")
                title = html.unescape((title_el.text or "").strip()) if title_el is not None and title_el.text else ""
                if not title:
                    continue

                sub_el = child.find("sub-title")
                sub_txt = html.unescape((sub_el.text or "").strip()) if sub_el is not None and sub_el.text else ""
                desc_el = child.find("desc")
                raw_desc = html.unescape((desc_el.text or "").strip()) if desc_el is not None and desc_el.text else ""
                date_el = child.find("date")
                year_txt = (date_el.text or "").strip()[:4] if date_el is not None and date_el.text else ""

                rating_raw = ""
                r_val_el = child.find("./rating/value")
                if r_val_el is not None and r_val_el.text:
                    rating_raw = r_val_el.text.strip()
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
                dur_mins = int((dt_stop - dt_start).total_seconds() // 60)

                p_el = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": xml_cid})
                t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
                t_el.text = strip_old_headers_and_se(title) or title

                desc_text = build_clean_visual_desc(
                    synopsis=raw_desc,
                    rating_badge=rating_badge,
                    year=year_txt,
                    duration_mins=dur_mins if 1 <= dur_mins <= 720 else None,
                    categories=cats,
                    sub_title=sub_txt,
                    se_badge=se_badge,
                )
                if desc_text:
                    d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
                    d_el.text = desc_text

                for c_name in dict.fromkeys(cats[:4]):
                    c_node = ET.SubElement(p_el, "category", {"lang": "pt"})
                    c_node.text = c_name

                if rating_code:
                    r_node = ET.SubElement(p_el, "rating", {"system": "age"})
                    v_node = ET.SubElement(r_node, "value")
                    v_node.text = rating_code

                progs_by_channel.setdefault(xml_cid, {})[start_str] = (dt_start, p_el)
                added_progs += 1

            print(f"  -> Fallback {url}: +{added_progs} programas importados.")
        except Exception as exc:
            print(f"  ⚠ Erro no fallback {url}: {exc}")
            continue


def ensure_love_nature_schedule(
    channels_by_id: dict[str, ET.Element],
    progs_by_channel: dict[str, dict[str, tuple[datetime, ET.Element]]],
    start_brt: datetime,
    end_brt: datetime,
) -> None:
    """Opcional: cria grade sintética para LoveNature.br se ALLOW_SYNTHETIC_LOVE_NATURE=1."""
    ln_id = "LoveNature.br"
    if len(progs_by_channel.get(ln_id, {})) >= 4:
        return

    ch_el = ET.Element("channel", {"id": ln_id})
    for dn_txt in ("Love Nature", "Love Nature 4K", "Love Nature HD", "LoveNature.br"):
        dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
        dn_el.text = dn_txt
    channels_by_id[ln_id] = ch_el

    shows = [
        ("Planeta Selvagem", "Documentário sobre a vida selvagem e ecossistemas preservados em 4K."),
        ("Segredos do Oceano", "A biodiversidade marinha nos recifes de corais e oceanos profundos."),
        ("África indomável", "A rotina dos grandes predadores e manadas nas savanas africanas."),
        ("Refúgios da Natureza", "Santuários ecológicos e reservas naturais ao redor do mundo."),
        ("Bebês da Vida Selvagem", "Os primeiros passos de filhotes na natureza selvagem."),
        ("Predadores em Ação", "Estratégias de sobrevivência dos maiores caçadores da natureza."),
    ]
    cur = start_brt.replace(minute=0, second=0, microsecond=0)
    idx = 0
    while cur < end_brt:
        nxt = cur + timedelta(hours=1)
        s_str = cur.strftime("%Y%m%d%H%M%S -0300")
        e_str = nxt.strftime("%Y%m%d%H%M%S -0300")
        title_t, syn_t = shows[idx % len(shows)]
        p_el = ET.Element("programme", {"start": s_str, "stop": e_str, "channel": ln_id})
        t_el = ET.SubElement(p_el, "title", {"lang": "pt"})
        t_el.text = title_t
        d_el = ET.SubElement(p_el, "desc", {"lang": "pt"})
        d_el.text = build_clean_visual_desc(
            synopsis=syn_t,
            rating_badge="🟢 LIVRE",
            duration_mins=60,
            categories=["Documentário", "Natureza"],
        )
        c_el = ET.SubElement(p_el, "category", {"lang": "pt"})
        c_el.text = "Documentário"
        progs_by_channel.setdefault(ln_id, {})[s_str] = (cur, p_el)
        cur = nxt
        idx += 1


def generate_claro_epg(days_ahead: int = 2, out_gz_path: Path = OUT_CLARO_GZ) -> int:
    session = requests.Session()
    fake_ip = random.choice(BR_IPS)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://www.clarotvmais.com.br",
        "Referer": "https://www.clarotvmais.com.br/",
        "X-Forwarded-For": fake_ip,
        "X-Real-IP": fake_ip,
        "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip,
    })
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        session.proxies = {"http": br_proxy, "https": br_proxy}

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
    api_errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_claro_mfwk_window, session, w_f, w_t) for w_f, w_t in windows]
        for fut in concurrent.futures.as_completed(futures):
            ch_list, err = fut.result()
            if err:
                api_errors.append(err)
            for ch in ch_list:
                if not isinstance(ch, dict):
                    continue
                grp_common = ch.get("group", {}).get("common", {}) if isinstance(ch.get("group"), dict) else {}
                ch_name = str(ch.get("name") or grp_common.get("title") or "").strip()
                if not ch_name:
                    continue
                xml_cid = make_channel_id(ch_name)
                if xml_cid not in channels_by_id:
                    ch_el = ET.Element("channel", {"id": xml_cid})
                    dn_el = ET.SubElement(ch_el, "display-name", {"lang": "pt"})
                    dn_el.text = ch_name
                    logo = str(ch.get("image") or grp_common.get("image_large") or "").strip()
                    if logo:
                        ET.SubElement(ch_el, "icon", {"src": logo})
                    channels_by_id[xml_cid] = ch_el

                for ev in (ch.get("events") or []):
                    if not isinstance(ev, dict):
                        continue
                    start_str, dt_start = parse_claro_time_to_brt(ev.get("unix_begin"), ev.get("date_begin"))
                    stop_str, dt_stop = parse_claro_time_to_brt(ev.get("unix_end"), ev.get("date_end"))
                    title = html.unescape(str(ev.get("name") or ev.get("title") or "").strip())

                    if not start_str or not stop_str or not dt_start or not dt_stop or not title:
                        continue
                    if dt_stop <= dt_start:
                        continue
                    if title.upper() == ch_name.upper() and "LOVE NATURE" not in ch_name.upper():
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
                    rating_badge, _ = format_rating_badge(raw_rating)
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

    api_ch_count = len(channels_by_id)
    api_prog_count = sum(len(v) for v in progs_by_channel.values())
    print(f"  -> Diagnóstico API mfwkweb Claro: {api_ch_count} canais, {api_prog_count} programas"
          + (f" (erros: {len(api_errors)}/{len(windows)}, ex: {api_errors[0]})" if api_errors else " (0 erros)"))

    supplement_from_fallback_xmls(session, channels_by_id, progs_by_channel)
    if ALLOW_SYNTHETIC_LOVE_NATURE:
        ensure_love_nature_schedule(channels_by_id, progs_by_channel, start_brt, end_brt)

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

    xml_bytes = ET.tostring(tv_root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out_gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(xml_bytes)

    print(f"✓ Claro TV+ EPG salvo em {out_gz_path} ({valid_channels} canais válidos, {prog_count} programas em -0300).")
    return prog_count


if __name__ == "__main__":
    generate_claro_epg()
