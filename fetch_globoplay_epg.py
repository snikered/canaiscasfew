#!/usr/bin/env python3
"""Extrator oficial do Globoplay (GraphQL Jarvis) -> globoplay_epg.xml.gz
- Consulta broadcasts + epgByDate(date) direto em cloud-jarvis.globo.com/graphql (sem login).
- Aplica formatação rica (🟢 LIVRE / 🟠 +14 / ⏱ / 🎭) e ordena cronologicamente por canal.
"""
from __future__ import annotations

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
OUT_GLOBOPLAY_GZ = Path("globoplay_epg.xml.gz")
JARVIS_URL = "https://cloud-jarvis.globo.com/graphql"

RATING_SHORT_MAP = {
    "L": "🟢 LIVRE",
    "LIVRE": "🟢 LIVRE",
    "0": "🟢 LIVRE",
    "10": "🔵 +10",
    "12": "🟡 +12",
    "14": "🟠 +14",
    "16": "🔴 +16",
    "18": "⚫ +18",
}

EPG_QUERY = """
query GetGloboplayEpg($date: String!) {
  broadcasts {
    mediaId
    name
    slug
    logo
    channel {
      id
      name
      logo(format: PNG)
    }
    epgByDate(date: $date) {
      entries {
        name
        metadata
        description
        startTime
        endTime
        durationInMinutes
        liveBroadcast
        tags
        contentRating
      }
    }
  }
}
"""


def format_rating_badge(raw_rating: Any) -> tuple[str, str]:
    if not raw_rating:
        return "", ""
    r = str(raw_rating).strip().upper()
    if not r or "NONE" in r or r in {"UNRATED", "N/A"}:
        return "", ""
    if "LIVRE" in r or r in {"L", "0", "G", "AL"}:
        return "🟢 LIVRE", "L"
    m = re.search(r"(?:^|[\s_+:\-])(10|12|14|16|18)(?:$|\b)", r)
    if m:
        age = m.group(1)
        return RATING_SHORT_MAP[age], f"PG-{age}"
    return "", ""


def make_channel_id(name: str) -> str:
    s = html.unescape(name or "")
    s = re.sub(r"\b(FHD|UHD|HD|SD)\b", " ", s, flags=re.I)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", s)
    camel = "".join(w.capitalize() if not w.isupper() else w for w in words)
    return f"{camel or 'Globoplay'}.br"


def _to_xmltv_brt(ts_val: Any) -> tuple[str, datetime] | tuple[None, None]:
    if ts_val is None:
        return None, None
    try:
        if isinstance(ts_val, (int, float)):
            sec = ts_val / 1000.0 if ts_val > 10_000_000_000 else float(ts_val)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(BRT_TZ)
        else:
            s = str(ts_val).strip()
            if s.isdigit():
                num = float(s)
                sec = num / 1000.0 if num > 10_000_000_000 else num
                dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(BRT_TZ)
            else:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(BRT_TZ)
        return dt.strftime("%Y%m%d%H%M%S -0300"), dt
    except Exception:
        return None, None


def build_clean_visual_desc(
    synopsis: str,
    rating_badge: str = "",
    duration_mins: int | None = None,
    categories: list[str] | None = None,
    sub_title: str = "",
) -> str:
    meta_parts: list[str] = []
    if rating_badge:
        meta_parts.append(rating_badge)
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
    if clean_sub:
        header_lines.append(f"📝 {clean_sub}")

    if header_lines and clean_syn:
        return "\n".join(header_lines) + "\n\n" + clean_syn
    if header_lines:
        return "\n".join(header_lines)
    return clean_syn


def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://globoplay.globo.com",
        "Referer": "https://globoplay.globo.com/",
        "x-tenant-id": "globo-play",
        "x-platform-id": "web",
        "x-device-id": "desktop",
        "x-client-version": "2026.11-7",
    })
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        s.proxies = {"http": br_proxy, "https": br_proxy}
    return s


def generate_globoplay_epg(days_ahead: int = 3, out_gz_path: Path = OUT_GLOBOPLAY_GZ) -> int:
    session = create_session()
    tv_out = ET.Element("tv", {"generator-info-name": "globoplay-jarvis-extractor"})
    channels_by_id: dict[str, ET.Element] = {}
    progs_by_ch: dict[str, dict[str, ET.Element]] = {}

    today = datetime.now(BRT_TZ).date()
    dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days_ahead + 1)]

    for date_str in dates:
        try:
            resp = session.post(
                JARVIS_URL,
                json={"query": EPG_QUERY, "variables": {"date": date_str}},
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"  - [Globoplay {date_str}] HTTP {resp.status_code}: {resp.text[:160]}")
                continue
            payload = resp.json()
        except Exception as exc:
            print(f"  - [Globoplay {date_str}] erro: {exc}")
            continue

        broadcasts = payload.get("data", {}).get("broadcasts") or []
        for bc in broadcasts:
            if not isinstance(bc, dict):
                continue
            ch_obj = bc.get("channel") or {}
            name = html.unescape(str(bc.get("name") or ch_obj.get("name") or "")).strip()
            slug = str(bc.get("slug") or "").strip()
            if not name:
                continue

            cid = make_channel_id(name)
            if cid not in channels_by_id:
                ch_el = ET.Element("channel", {"id": cid})
                for dn in dict.fromkeys([name, ch_obj.get("name") or "", slug]):
                    if dn:
                        ET.SubElement(ch_el, "display-name", {"lang": "pt"}).text = str(dn).strip()
                logo_url = ch_obj.get("logo") or bc.get("logo") or ""
                if logo_url:
                    ET.SubElement(ch_el, "icon", {"src": str(logo_url).strip()})
                channels_by_id[cid] = ch_el

            epg_block = bc.get("epgByDate") or {}
            entries = epg_block.get("entries") or []
            for ev in entries:
                if not isinstance(ev, dict):
                    continue
                title = html.unescape(str(ev.get("name") or "")).strip()
                start_str, dt_s = _to_xmltv_brt(ev.get("startTime"))
                stop_str, dt_e = _to_xmltv_brt(ev.get("endTime"))
                if not (title and start_str and stop_str and dt_s and dt_e) or dt_e <= dt_s:
                    continue

                raw_desc = html.unescape(str(ev.get("description") or "")).strip()
                sub_title = html.unescape(str(ev.get("metadata") or "")).strip()
                rating_badge, rating_code = format_rating_badge(ev.get("contentRating"))

                dur_raw = ev.get("durationInMinutes")
                dur_mins = int(dur_raw) if str(dur_raw).isdigit() else int((dt_e - dt_s).total_seconds() // 60)
                dur_mins = dur_mins if 1 <= dur_mins <= 720 else None

                tags_raw = ev.get("tags") or []
                cats = [str(t).strip() for t in tags_raw if t and str(t).strip()]

                p_out = ET.Element("programme", {"start": start_str, "stop": stop_str, "channel": cid})
                ET.SubElement(p_out, "title", {"lang": "pt"}).text = title
                if sub_title and sub_title.lower() != title.lower():
                    ET.SubElement(p_out, "sub-title", {"lang": "pt"}).text = sub_title

                clean_desc = build_clean_visual_desc(
                    synopsis=raw_desc,
                    rating_badge=rating_badge,
                    duration_mins=dur_mins,
                    categories=cats,
                    sub_title=sub_title if sub_title.lower() != title.lower() else "",
                )
                if clean_desc:
                    ET.SubElement(p_out, "desc", {"lang": "pt"}).text = clean_desc
                for c_name in dict.fromkeys(cats[:5]):
                    ET.SubElement(p_out, "category", {"lang": "pt"}).text = c_name
                if rating_code:
                    r_node = ET.SubElement(p_out, "rating", {"system": "age"})
                    ET.SubElement(r_node, "value").text = rating_code

                sort_key = start_str.split()[0][:14]
                progs_by_ch.setdefault(cid, {})[sort_key] = p_out

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
        print(f"✓ Globoplay EPG salvo em {out_gz_path} ({valid_channels} canais, {total_progs} programas).")
        return total_progs

    raise RuntimeError("Nenhum programa obtido da API Globoplay (Jarvis).")


if __name__ == "__main__":
    generate_globoplay_epg()