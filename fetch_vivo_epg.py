#!/usr/bin/env python3
"""Extrator da Vivo Play (GVP Telefonica) -> vivo_epg.xml.gz
- Busca canais e grade direto na API GVP da Telefônica (/25/default/pt-BR).
- Usa vivoplay.xml (limaalef) SOMENTE como fallback para canais ausentes na API direta.
- Aplica formatação rica (🟢 LIVRE / 🟠 +14 / [T03E07]) e ordena cronologicamente por canal.
- VIVO_DEBUG=1 grava as respostas brutas em vivo_debug/ para inspeção.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html
import json
import os
import random
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

BRT_TZ = timezone(timedelta(hours=-3))
OUT_VIVO_GZ = Path("vivo_epg.xml.gz")

BR_IPS = [
    "177.37.142.88",
    "179.218.44.19",
    "187.19.204.112",
    "189.40.92.201",
    "200.155.80.45",
]

VIVO_GVP_ENDPOINTS = [
    "https://contentapi-br.cdn.telefonica.com/25/default/pt-BR",
]

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

_RE_TEST_CHANNEL = re.compile(
    r"^(?:canal\s+(?:de\s+)?testes?\b|teste\s*\d+|testes\s+de\s+apps\b|in\s+channel\s+app\b)",
    re.I,
)

SCHEDULE_FIELDS = (
    "Pid,Title,Description,ChannelName,ChannelNumber,CallLetter,Start,End,"
    "EpgNetworkDvr,LiveChannelPid,LiveProgramPid,EpgSerieId,SeriesPid,SeriesId,"
    "SeasonPid,SeasonNumber,EpisodeNumber,images.videoFrame,images.banner,"
    "LiveToVod,AgeRatingPid,forbiddenTechnology,IsSoDisabled"
)
CHANNELS_PER_REQUEST = 10   # a API devolve HTTP 400 com mais canais por chamada
PAGE_SIZE = 1000
API_BASES = [os.environ["VIVO_API_BASE"]] if os.environ.get("VIVO_API_BASE") else VIVO_GVP_ENDPOINTS
DEBUG = os.environ.get("VIVO_DEBUG", "").strip() not in ("", "0", "false")


def format_rating_badge(raw_rating: Any) -> tuple[str, str]:
    if not raw_rating:
        return "", ""
    r = str(raw_rating).strip().upper()
    if not r or "NONE" in r or r in {"UNRATED", "N/A"}:
        return "", ""
    r_clean = re.sub(r"^AGE_", "", r)
    if "LIVRE" in r_clean or r_clean in {"L", "0", "G", "ALL", "AL"}:
        return "🟢 LIVRE", "L"
    m = re.search(r"(?:^|[\s_+:\-])(10|12|14|16|18)(?:$|\b)", r_clean)
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


def _norm_key(name: str) -> str:
    return make_channel_id(name).lower()


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


def _debug_dump(name: str, content: bytes) -> None:
    if DEBUG:
        d = Path("vivo_debug")
        d.mkdir(exist_ok=True)
        (d / name).write_bytes(content)


def get_json(
    session: requests.Session,
    url: str,
    label: str,
    timeout: int = 30,
    params: dict[str, str] | None = None,
    attempts: int = 3,
) -> Any | None:
    backoffs = (2, 5, 10)
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt < attempts:
                fake_ip = random.choice(BR_IPS)
                session.headers.update({"X-Forwarded-For": fake_ip, "X-Real-IP": fake_ip})
                time.sleep(backoffs[attempt - 1])
                continue
            print(f"  - [{label}] erro de rede: {type(exc).__name__}: {exc}")
            return None

        raw = r.content or b""
        _debug_dump(re.sub(r"[^A-Za-z0-9]+", "_", label)[:60] + ".bin", raw)
        ctype = r.headers.get("Content-Type", "")

        if r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 200 and not raw.strip()):
            if attempt < attempts:
                fake_ip = random.choice(BR_IPS)
                session.headers.update({"X-Forwarded-For": fake_ip, "X-Real-IP": fake_ip})
                time.sleep(backoffs[attempt - 1])
                continue

        if r.status_code != 200:
            print(f"  - [{label}] HTTP {r.status_code} ({ctype}) {raw[:120]!r}")
            return None
        if not raw.strip():
            print(f"  - [{label}] resposta vazia (HTTP 200, {ctype})")
            return None
        try:
            return json.loads(raw.decode("utf-8-sig", errors="replace"))
        except ValueError:
            if attempt < attempts:
                time.sleep(backoffs[attempt - 1])
                continue
            print(f"  - [{label}] resposta não é JSON ({ctype}): {raw[:160]!r}")
            return None
    return None


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        lowered = {str(k).lower(): v for k, v in payload.items()}
        for key in ("content", "list", "contents", "items", "schedules", "data", "results"):
            v = lowered.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = extract_items(v)
                if inner:
                    return inner
    return []


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


def list_live_channels(session: requests.Session, base: str) -> list[str]:
    pids: list[str] = []
    seen: set[str] = set()
    offset = 0
    first = True
    while True:
        params = {
            "ca_deviceTypes": "null|401",
            "contentTypes": "LCH",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
        }
        payload = get_json(session, f"{base}/contents/all", f"Vivo GVP canais {base}", 30, params)
        items = extract_items(payload) if payload is not None else []
        if not items:
            break
        if first and DEBUG:
            print(f"    exemplo de canal: {json.dumps(items[0], ensure_ascii=False)[:400]}")
        first = False
        for it in items:
            pid = str(it.get("Pid") or it.get("LiveChannelPid") or it.get("Id") or it.get("id") or "").strip().lower()
            if pid.startswith("lch") and pid not in seen:
                seen.add(pid)
                pids.append(pid)
        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return pids


def fetch_schedule_block(
    session: requests.Session,
    base: str,
    group: list[str],
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "ca_deviceTypes": "null|401",
            "fields": SCHEDULE_FIELDS,
            "includeRelations": "Genre",
            "orderBy": "START_TIME:a",
            "filteravailability": "false",
            "starttime": str(start_ts),
            "endtime": str(end_ts),
            "livechannelpids": ",".join(group),
            "offset": str(offset),
            "limit": str(PAGE_SIZE),
        }
        chmap = os.environ.get("VIVO_CHANNELMAPS", "").strip()
        if chmap:
            params["ca_channelmaps"] = chmap
        data = get_json(session, f"{base}/schedules", f"Vivo GVP grade {group[0]}..+{len(group) - 1}", 60, params)
        records = extract_items(data) if data is not None else []
        out.extend(records)
        if len(records) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def _genre_names(ev: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("Genres", "Genre", "genres"):
        v = ev.get(key)
        if isinstance(v, list):
            for g in v:
                n = g.get("Name") or g.get("Title") if isinstance(g, dict) else str(g)
                if n and str(n).strip():
                    names.append(str(n).strip())
    rel = ev.get("Relations") or ev.get("relations")
    if isinstance(rel, list):
        for r_ in rel:
            if isinstance(r_, dict) and str(r_.get("Type", "")).lower() == "genre":
                n = r_.get("Name") or r_.get("Title")
                if n:
                    names.append(str(n).strip())
    return list(dict.fromkeys(names))


def fetch_from_gvp_api(
    session: requests.Session,
    days_ahead: int,
    channels_by_id: dict[str, ET.Element],
    progs_by_ch: dict[str, dict[str, ET.Element]],
) -> int:
    """Canais (LCH) + grade por grupos de 10 canais em blocos de 24h direto na API Vivo/Telefônica."""
    base, pids = "", []
    for b in API_BASES:
        pids = list_live_channels(session, b)
        if pids:
            base = b
            break
    if not pids:
        print("  ⚠ [Vivo GVP API] Nenhum LiveChannelPid retornado.")
        return 0
    print(f"  ✓ [Vivo GVP API] {len(pids)} canais em {base}")

    day0 = datetime.now(BRT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    blocks = [
        (int((day0 + timedelta(days=d)).timestamp()), int((day0 + timedelta(days=d + 1)).timestamp()))
        for d in range(days_ahead + 1)
    ]
    groups = [pids[i:i + CHANNELS_PER_REQUEST] for i in range(0, len(pids), CHANNELS_PER_REQUEST)]
    jobs = [(g, st, et) for g in groups for st, et in blocks]
    print(f"  Consultando grade: {len(groups)} grupos x {len(blocks)} dias = {len(jobs)} requisições")

    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for res in pool.map(lambda j: fetch_schedule_block(session, base, *j), jobs):
            records.extend(res)
    if DEBUG and records:
        print(f"    exemplo de registro: {json.dumps(records[0], ensure_ascii=False)[:600]}")

    pid_set = set(pids)
    added = 0
    for ev in records:
        pid = str(ev.get("LiveChannelPid") or "").strip().lower()
        if pid not in pid_set:
            continue
        name = html.unescape(str(ev.get("ChannelName") or "")).strip()
        call = html.unescape(str(ev.get("CallLetter") or "")).strip()
        base_name = name or call or pid.upper()
        if _RE_TEST_CHANNEL.search(base_name):
            continue

        cid = make_channel_id(base_name)
        if cid not in channels_by_id:
            ch_el = ET.Element("channel", {"id": cid})
            for dn_txt in dict.fromkeys([base_name, call]):
                if dn_txt:
                    ET.SubElement(ch_el, "display-name", {"lang": "pt"}).text = dn_txt
            channels_by_id[cid] = ch_el

        start_str, dt_s = _epoch_to_xmltv_brt(ev.get("Start"))
        stop_str, dt_e = _epoch_to_xmltv_brt(ev.get("End"))
        title = html.unescape(str(ev.get("Title") or "")).strip()
        if not (start_str and stop_str and title and dt_s and dt_e) or dt_e <= dt_s:
            continue

        sub_title = ""
        if " : " in title:
            parts = [p.strip() for p in title.split(" : ", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                title, sub_title = parts[0], parts[1]

        raw_desc = html.unescape(str(ev.get("Description") or "")).strip()
        rating_badge, rating_code = format_rating_badge(ev.get("AgeRatingPid"))
        dur_mins = int((dt_e - dt_s).total_seconds() // 60)
        dur_mins = dur_mins if 1 <= dur_mins <= 720 else None
        cats = _genre_names(ev)

        s_num = ev.get("SeasonNumber")
        e_num = ev.get("EpisodeNumber")
        se_badge = ""
        if str(s_num).isdigit() and str(e_num).isdigit():
            s_i, e_i = int(s_num), int(e_num)
            if 0 < s_i <= 60 and 0 < e_i <= 999:
                se_badge = f"[T{s_i:02d}E{e_i:02d}]"

        p_out = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": cid})
        ET.SubElement(p_out, "title", {"lang": "pt"}).text = strip_old_headers_and_se(title) or title
        if sub_title:
            ET.SubElement(p_out, "sub-title", {"lang": "pt"}).text = sub_title

        clean_desc = build_clean_visual_desc(
            synopsis=raw_desc,
            rating_badge=rating_badge,
            duration_mins=dur_mins,
            categories=cats,
            sub_title=sub_title,
            se_badge=se_badge,
        )
        if clean_desc:
            ET.SubElement(p_out, "desc", {"lang": "pt"}).text = clean_desc
        for c_name in cats[:5]:
            ET.SubElement(p_out, "category", {"lang": "pt"}).text = c_name
        if rating_code:
            r_node = ET.SubElement(p_out, "rating", {"system": "age"})
            ET.SubElement(r_node, "value").text = rating_code

        sort_key = start_str.split()[0][:14]
        if sort_key not in progs_by_ch.setdefault(cid, {}):
            added += 1
        progs_by_ch[cid][sort_key] = p_out

    n_ch = sum(1 for c in channels_by_id if progs_by_ch.get(c))
    print(f"  ✓ [Vivo GVP API] {n_ch} canais | {added} programas.")
    return added


def fill_missing_from_vivo_mirror(
    session: requests.Session,
    channels_by_id: dict[str, ET.Element],
    progs_by_ch: dict[str, dict[str, ET.Element]],
) -> int:
    """Usa vivoplay.xml (limaalef) SOMENTE para canais não cobertos pela API direta."""
    try:
        resp = session.get(VIVO_FALLBACK_MIRROR, timeout=60)
        if resp.status_code != 200 or len(resp.content) < 1000:
            print(f"  ⚠ [Vivo Fallback] Falha ao baixar vivoplay.xml (HTTP {resp.status_code}).")
            return 0
        root = ET.fromstring(resp.content)
    except Exception as exc:
        print(f"  ⚠ [Vivo Fallback] Erro ao ler vivoplay.xml: {exc}")
        return 0

    covered_keys = {_norm_key(c) for c, pmap in progs_by_ch.items() if len(pmap) >= 5}
    raw_to_cid: dict[str, str] = {}
    fallback_cids: set[str] = set()

    for child in root:
        if child.tag.rsplit("}", 1)[-1] != "channel":
            continue
        raw_cid = (child.attrib.get("id") or "").strip()
        if not raw_cid:
            continue
        dns = [
            (n.text or "").strip()
            for n in child
            if n.tag.rsplit("}", 1)[-1] == "display-name" and (n.text or "").strip()
        ]
        ch_name = dns[0] if dns else raw_cid
        if _RE_TEST_CHANNEL.search(ch_name):
            continue
        if _norm_key(ch_name) in covered_keys or _norm_key(raw_cid) in covered_keys:
            continue

        xml_cid = make_channel_id(ch_name)
        raw_to_cid[raw_cid] = xml_cid
        if xml_cid not in channels_by_id:
            ch_copy = ET.Element("channel", {"id": xml_cid})
            for d_txt in dict.fromkeys(dns or [ch_name]):
                ET.SubElement(ch_copy, "display-name", {"lang": "pt"}).text = d_txt
            for sub in child:
                if sub.tag.rsplit("}", 1)[-1] == "icon" and sub.attrib.get("src"):
                    ET.SubElement(ch_copy, "icon", {"src": sub.attrib["src"].strip()})
                    break
            channels_by_id[xml_cid] = ch_copy
        fallback_cids.add(xml_cid)

    if not fallback_cids:
        print("  ✓ [Vivo Fallback] 0 canais extras necessários (100% coberto pela API direta).")
        return 0

    added_progs = 0
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != "programme":
            continue
        raw_cid = (child.attrib.get("channel") or "").strip()
        cid = raw_to_cid.get(raw_cid)
        if not cid or cid not in fallback_cids:
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
            if len(parts) == 2 and parts[0] and parts[1]:
                title_txt, sub_txt = parts[0], parts[1]

        desc_el = child.find("desc")
        raw_desc = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""

        date_el = child.find("date")
        year_txt = (date_el.text or "").strip()[:4] if date_el is not None and date_el.text else ""

        r_val_el = child.find("./rating/value")
        rating_raw = (r_val_el.text or "").strip() if r_val_el is not None and r_val_el.text else ""
        rating_badge, rating_code = format_rating_badge(rating_raw)

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
        ET.SubElement(p_out, "title", {"lang": "pt"}).text = strip_old_headers_and_se(title_txt) or title_txt
        if sub_txt:
            ET.SubElement(p_out, "sub-title", {"lang": "pt"}).text = sub_txt

        clean_desc = build_clean_visual_desc(
            synopsis=raw_desc,
            rating_badge=rating_badge,
            year=year_txt,
            duration_mins=dur_mins,
            categories=cats,
            sub_title=sub_txt,
        )
        if clean_desc:
            ET.SubElement(p_out, "desc", {"lang": "pt"}).text = clean_desc
        for c_name in dict.fromkeys(cats[:5]):
            ET.SubElement(p_out, "category", {"lang": "pt"}).text = c_name
        if rating_code:
            r_node = ET.SubElement(p_out, "rating", {"system": "age"})
            ET.SubElement(r_node, "value").text = rating_code

        sort_key = start_raw.split()[0][:14]
        progs_by_ch.setdefault(cid, {})[sort_key] = p_out
        added_progs += 1

    print(f"  ✓ [Vivo Fallback] {len(fallback_cids)} canais complementares | {added_progs} programas via vivoplay.xml.")
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
