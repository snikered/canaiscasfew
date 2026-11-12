#!/usr/bin/env python3
"""Sincronizador de Listas M3U com Anti-GeoBlock (Cloudscraper + Headers BR + API Xtream + Streaming):
- Tenta primeiro a API leve player_api.php (ignora os 150MB de VOD e baixa só canais ao vivo em 2s).
- Usa cloudscraper + cabeçalhos X-Forwarded-For de operadoras brasileiras + User-Agents de apps IPTV.
- Suporta proxy opcional via variável BR_PROXY nos Secrets.
- Casa cada canal com o <channel id="..."> exato do seu epg.xml.gz.
- Publica cada lista limpa (lista_1.m3u, lista_2.m3u...) no seu GitHub Gist Secreto.
"""
from __future__ import annotations

import gzip
import html
import json
import os
import random
import re
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

from merge_epg import name_key, normalizar_canal, strip_qualities_keep_case

EPG_GZ_PATH = Path("epg.xml.gz")

# Faixas de IPs residenciais brasileiros (Vivo, Claro, Oi) para contornar checagem de proxy reverso
BR_RESIDENTIAL_IPS = [
    "177.37.142.88",
    "179.218.44.19",
    "187.19.204.112",
    "189.40.92.201",
    "200.155.80.45",
    "201.17.110.73",
]

IPTV_USER_AGENTS = [
    "TiviMate/4.7.0 (Android 12)",
    "IPTVSmartersPlayer",
    "Lavf/60.3.100",
    "VLC/3.0.20 LibVLC/3.0.20",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BLOCKED_GROUP_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\bONLYFANS\b|"
    r"\bVOD\b|\bFILMES\b|\bSERIES\b|\bSÉRIES\b|\bLAN[CÇ]AMENTOS?\b|\bNOVELAS\s+VOD\b|\b24\s*H(?:S|ORAS)?\b)",
    re.IGNORECASE,
)
BLOCKED_NAME_RE = re.compile(
    r"(\bXXX\b|\bADULTO\b|\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b|"
    r"\bS\d{1,2}\s*E\d{1,3}\b|\bT\d{1,2}\s*EP?\d{1,3}\b|\b24\s*H(?:S|ORAS)?\b)",
    re.IGNORECASE,
)
VOD_URL_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx")


def build_br_headers(ua: str, host_url: str = "") -> dict[str, str]:
    fake_ip = random.choice(BR_RESIDENTIAL_IPS)
    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "X-Forwarded-For": fake_ip,
        "X-Real-IP": fake_ip,
        "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip,
        "Client-IP": fake_ip,
        "Connection": "keep-alive",
    }
    if host_url:
        parsed = urllib.parse.urlparse(host_url)
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def create_http_client() -> requests.Session:
    if cloudscraper is not None:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        session = requests.Session()

    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        session.proxies = {"http": br_proxy, "https": br_proxy}
        print("  -> Usando BR_PROXY configurado nos Secrets.")
    return session


def request_with_bypass(url: str, stream: bool = False, timeout: int = 60) -> requests.Response:
    session = create_http_client()
    last_err: Exception | None = None

    for ua in IPTV_USER_AGENTS:
        headers = build_br_headers(ua, url)
        try:
            resp = session.get(url, headers=headers, stream=stream, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            last_err = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:
            last_err = exc

    raise RuntimeError(f"Todas as tentativas de bypass falharam ({last_err})")


def load_epg_id_index(epg_path: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    if not epg_path.exists():
        print(f"Aviso: {epg_path} não encontrado!")
        return index

    with gzip.open(epg_path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "channel":
                cid = (elem.attrib.get("id") or "").strip()
                if cid:
                    for k in (name_key(cid), name_key(normalizar_canal(cid))):
                        if k:
                            index[k] = cid
                    for child in elem:
                        if child.tag.rsplit("}", 1)[-1] == "display-name" and child.text:
                            dn = child.text.strip()
                            for k_dn in (name_key(dn), name_key(normalizar_canal(dn))):
                                if k_dn and k_dn not in index:
                                    index[k_dn] = cid
                elem.clear()
    print(f"✓ Índice EPG carregado com {len(index)} variações de nomes mapeadas.")
    return index


def resolve_best_tvg_id(channel_name: str, original_tvg_id: str, epg_index: dict[str, str]) -> str:
    for candidate in (
        channel_name,
        strip_qualities_keep_case(channel_name),
        normalizar_canal(channel_name),
    ):
        k = name_key(candidate)
        if k and k in epg_index:
            return epg_index[k]

    if original_tvg_id:
        k_id = name_key(original_tvg_id)
        if k_id and k_id in epg_index:
            return epg_index[k_id]

    return strip_qualities_keep_case(channel_name) or channel_name


def is_vod_stream_url(url: str) -> bool:
    clean_path = url.split("?", 1)[0].lower()
    if clean_path.endswith(VOD_URL_EXTENSIONS):
        return True
    if "/movie/" in clean_path or "/series/" in clean_path:
        return True
    return False


def try_fetch_via_xtream_api(url: str, epg_index: dict[str, str], epg_public_url: str) -> tuple[str, int, int] | None:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        return None

    user = query["username"][0]
    pwd = query["password"][0]
    out_fmt = (query.get("output", ["ts"])[0] or "ts").lower()
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        cat_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories"
        resp_cat = request_with_bypass(cat_url, stream=False, timeout=30)
        cats_data = resp_cat.json()
        cat_map = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Canais")).strip())
            for c in (cats_data if isinstance(cats_data, list) else [])
            if isinstance(c, dict)
        }

        streams_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams"
        resp_str = request_with_bypass(streams_url, stream=False, timeout=45)
        streams = resp_str.json()
        if not isinstance(streams, list) or not streams:
            return None

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"' if epg_public_url else "#EXTM3U"
        out_lines = [header]
        kept, matched_epg = 0, 0
        ext = "m3u8" if out_fmt in {"m3u8", "hls"} else "ts"

        for ch in streams:
            if not isinstance(ch, dict):
                continue
            name = html.unescape(str(ch.get("name", "")).strip())
            stream_id = ch.get("stream_id")
            if not name or not stream_id:
                continue

            group = cat_map.get(str(ch.get("category_id", "")), "Canais")
            if BLOCKED_GROUP_RE.search(group) or BLOCKED_NAME_RE.search(name):
                continue

            orig_tvg_id = str(ch.get("epg_channel_id", "")).strip()
            best_tvg_id = resolve_best_tvg_id(name, orig_tvg_id, epg_index)
            if name_key(best_tvg_id) in epg_index:
                matched_epg += 1

            logo = str(ch.get("stream_icon", "")).strip()
            logo_attr = f' tvg-logo="{logo}"' if logo else ""
            stream_url = f"{base_url}/live/{user}/{pwd}/{stream_id}.{ext}"

            out_lines.append(
                f'#EXTINF:-1 tvg-id="{best_tvg_id}" tvg-name="{name}"{logo_attr} group-title="{group}",{name}'
            )
            out_lines.append(stream_url)
            kept += 1

        print(f"  -> ✓ Bypass via API Xtream funcionou! {kept} canais ao vivo extraídos em segundos.")
        return "\n".join(out_lines) + "\n", kept, matched_epg
    except Exception as e:
        print(f"  -> API Xtream bloqueada ou indisponível ({e}). Tentando streaming do arquivo M3U...")
        return None


def process_large_m3u_stream(url: str, epg_index: dict[str, str], epg_public_url: str) -> tuple[str, int, int]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".m3u")
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        resp = request_with_bypass(url, stream=True, timeout=180)
        with tmp_path.open("wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 512):
                if chunk:
                    out.write(chunk)

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"' if epg_public_url else "#EXTM3U"
        out_lines = [header]
        kept, matched_epg = 0, 0
        current_extinf: str | None = None

        with tmp_path.open("r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("#EXTINF"):
                    current_extinf = line
                    continue
                if line.startswith(("http://", "https://", "rtmp://")) and current_extinf:
                    extinf = current_extinf
                    current_extinf = None

                    if is_vod_stream_url(line):
                        continue

                    group_m = re.search(r'group-title="(.*?)"', extinf, re.I)
                    tvg_id_m = re.search(r'tvg-id="(.*?)"', extinf, re.I)
                    logo_m = re.search(r'tvg-logo="(.*?)"', extinf, re.I)
                    name_m = re.search(r',([^,]*)$', extinf)

                    group = html.unescape(group_m.group(1).strip()) if group_m else "Canais"
                    orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                    logo = logo_m.group(1).strip() if logo_m else ""
                    name = html.unescape(name_m.group(1).strip()) if name_m else ""
                    if not name:
                        continue

                    if BLOCKED_GROUP_RE.search(group) or BLOCKED_NAME_RE.search(name):
                        continue

                    best_tvg_id = resolve_best_tvg_id(name, orig_tvg_id, epg_index)
                    if name_key(best_tvg_id) in epg_index:
                        matched_epg += 1

                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    out_lines.append(
                        f'#EXTINF:-1 tvg-id="{best_tvg_id}" tvg-name="{name}"{logo_attr} group-title="{group}",{name}'
                    )
                    out_lines.append(line)
                    kept += 1

        return "\n".join(out_lines) + "\n", kept, matched_epg
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def update_secret_gist(files_dict: dict[str, dict[str, str]], token: str, gist_id: str) -> None:
    url = f"https://api.github.com/gists/{gist_id}"
    payload = json.dumps({"files": files_dict}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "EPG-M3U-Sync",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status == 200:
            print(f"\n✓ Gist Secreto ({gist_id}) atualizado com {len(files_dict)} lista(s)!")


def main() -> None:
    epg_public_url = os.environ.get("EPG_PUBLIC_URL", "").strip()
    epg_index = load_epg_id_index(EPG_GZ_PATH)

    gist_files: dict[str, dict[str, str]] = {}

    for i in range(1, 6):
        url = os.environ.get(f"M3U_URL_{i}", "").strip()
        if not url or not url.startswith("http"):
            continue

        print(f"\nProcessando M3U_URL_{i}...")
        try:
            res = try_fetch_via_xtream_api(url, epg_index, epg_public_url)
            if res is None:
                res = process_large_m3u_stream(url, epg_index, epg_public_url)

            content, kept_count, matched_count = res
            if kept_count == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou 0 canais. Pulando atualização desta lista.")
                continue

            filename = f"lista_{i}.m3u"
            size_mb = len(content.encode("utf-8")) / (1024 * 1024)
            print(
                f"  -> ✓ [{filename}] {kept_count} canais ao vivo | "
                f"{matched_count} com EPG vinculado | Tamanho: {size_mb:.2f} MB"
            )
            gist_files[filename] = {"content": content}
        except Exception as e:
            print(f"  -> ❌ Bloqueio de IP na M3U_URL_{i}: {e}")

    if not gist_files:
        raise RuntimeError(
            "Nenhuma lista pôde ser baixada pelo IP do GitHub Actions (Geo-Block ativo no servidor IPTV)."
        )

    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()
    if gist_token and gist_id:
        update_secret_gist(gist_files, gist_token, gist_id)
    else:
        print("Aviso: Configure GIST_TOKEN e GIST_ID nos Secrets para salvar no Gist Secreto.")


if __name__ == "__main__":
    main()