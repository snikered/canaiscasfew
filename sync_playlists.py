#!/usr/bin/env python3
"""Sincronizador M3U Universal (TV Samsung + TiviMate + Televizo):
- Gera 'lista_1.m3u' (Link Único Completo <90MB sem .gz, compatível com TV Samsung, contendo
  Canais no topo com TVG-ID corrigido + Filmes + Séries com TODAS as categorias reais da API!).
- Gera também 'canais_1.m3u' (Apenas Canais ao Vivo super leve para uso opcional no TiviMate).
"""
from __future__ import annotations

import gzip
import html
import os
import random
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

from merge_epg import name_key, normalizar_canal, strip_qualities_keep_case

EPG_GZ_PATH = Path("epg.xml.gz")
REMOVE_ADULT = os.environ.get("REMOVE_ADULT", "1").strip().lower() in {"1", "true", "yes"}

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

ADULT_FILTER_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\+\s*18\b|\bONLYFANS\b|"
    r"\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b)",
    re.IGNORECASE,
)

VOD_URL_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx")
_RE_TVG_ID = re.compile(r'tvg-id="[^"]*"', re.IGNORECASE)
_RE_GROUP_TITLE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
_RE_TVG_LOGO = re.compile(r'tvg-logo="([^"]*)"', re.IGNORECASE)
_RE_STREAM_ID_FROM_URL = re.compile(r"/(\d+)(?:\.[a-z0-9]+)?(?:\?.*)?$", re.IGNORECASE)
_RE_SERIES_EP_DETECT = re.compile(r"\b(?:S\d{1,2}\s*E\d{1,3}|T\d{1,2}\s*EP?\d{1,3})\b", re.IGNORECASE)
_RE_SERIES_EP_SPLIT = re.compile(r"\s*[\-\|:]?\s*\b(?:S\d{1,2}\s*E\d{1,3}|T\d{1,2}\s*EP?\d{1,3})\b.*$", re.IGNORECASE)


def norm_lookup_key(title: str) -> str:
    s = html.unescape(title or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


def norm_series_title(title: str) -> str:
    s = html.unescape(title or "")
    s = _RE_SERIES_EP_SPLIT.sub("", s)
    s = re.sub(r"\(\d{4}\)|\[\d{4}\]", " ", s)
    return norm_lookup_key(s)


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
    return session


def request_with_bypass(url: str, stream: bool = False, timeout: int = 90) -> requests.Response:
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


def fetch_xtream_category_maps(url: str) -> dict[str, dict[str, str]]:
    """Mapeia todas as categorias reais do servidor (Canais, Filmes e Séries) por ID e por Nome."""
    maps: dict[str, dict[str, str]] = {
        "live_by_id": {},
        "live_by_name": {},
        "vod_by_id": {},
        "vod_by_name": {},
        "series_by_name": {},
    }

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        return maps

    user = query["username"][0]
    pwd = query["password"][0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Canais Ao Vivo
    try:
        c_live = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories", timeout=30
        ).json()
        live_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_live if isinstance(c_live, list) else [])
            if isinstance(c, dict) and c.get("category_name")
        }
        s_live = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams", timeout=45
        ).json()
        for ch in (s_live if isinstance(s_live, list) else []):
            if not isinstance(ch, dict):
                continue
            grp = live_cats.get(str(ch.get("category_id", "")), "")
            if not grp:
                continue
            if ch.get("stream_id"):
                maps["live_by_id"][str(ch["stream_id"])] = grp
            if ch.get("name"):
                raw_n = str(ch["name"])
                maps["live_by_name"][norm_lookup_key(raw_n)] = grp
                maps["live_by_name"][name_key(raw_n)] = grp
        print(f"  -> ✓ Categorias de Canais mapeadas: {len(live_cats)} grupos.")
    except Exception as e:
        print(f"  -> Nota: Mapa Live indisponível ({e}).")

    # 2. Filmes VOD
    try:
        c_vod = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_categories", timeout=30
        ).json()
        vod_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_vod if isinstance(c_vod, list) else [])
            if isinstance(c, dict) and c.get("category_name")
        }
        s_vod = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_streams", timeout=60
        ).json()
        for v in (s_vod if isinstance(s_vod, list) else []):
            if not isinstance(v, dict):
                continue
            grp = vod_cats.get(str(v.get("category_id", "")), "")
            if not grp:
                continue
            if v.get("stream_id"):
                maps["vod_by_id"][str(v["stream_id"])] = grp
            if v.get("name"):
                raw_v = str(v["name"])
                maps["vod_by_name"][norm_lookup_key(raw_v)] = grp
                maps["vod_by_name"][norm_series_title(raw_v)] = grp
        print(f"  -> ✓ Categorias de Filmes mapeadas: {len(vod_cats)} grupos.")
    except Exception as e:
        print(f"  -> Nota: Mapa VOD indisponível ({e}).")

    # 3. Séries
    try:
        c_ser = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_series_categories", timeout=30
        ).json()
        ser_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_ser if isinstance(c_ser, list) else [])
            if isinstance(c, dict) and c.get("category_name")
        }
        s_ser = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_series", timeout=60
        ).json()
        for s in (s_ser if isinstance(s_ser, list) else []):
            if isinstance(s, dict) and s.get("name"):
                grp = ser_cats.get(str(s.get("category_id", "")), "")
                s_key = norm_series_title(str(s["name"]))
                if grp and s_key:
                    maps["series_by_name"][s_key] = grp
        print(f"  -> ✓ Categorias de Séries mapeadas: {len(ser_cats)} grupos.")
    except Exception as e:
        print(f"  -> Nota: Mapa Séries indisponível ({e}).")

    return maps


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
    if re.search(r"\b24\s*H(?:S|ORAS)?\b", channel_name, re.I):
        return ""

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

    return strip_qualities_keep_case(channel_name) or original_tvg_id or channel_name


def replace_only_tvg_id_in_extinf(extinf_line: str, new_tvg_id: str) -> str:
    safe_id = new_tvg_id.replace('"', "'")
    if _RE_TVG_ID.search(extinf_line):
        return _RE_TVG_ID.sub(f'tvg-id="{safe_id}"', extinf_line, count=1)
    return re.sub(r"^(#EXTINF:[^\s,]+)", rf'\1 tvg-id="{safe_id}"', extinf_line, count=1)


def set_group_title_in_extinf(extinf_line: str, new_group: str) -> str:
    if not new_group:
        return extinf_line
    safe_grp = new_group.replace('"', "'")
    if _RE_GROUP_TITLE.search(extinf_line):
        return _RE_GROUP_TITLE.sub(f'group-title="{safe_grp}"', extinf_line, count=1)
    return re.sub(r"^(#EXTINF:[^\s,]+)", rf'\1 group-title="{safe_grp}"', extinf_line, count=1)


def is_vod_stream(stream_url: str, extinf_line: str) -> bool:
    clean_path = stream_url.split("?", 1)[0].lower()
    if clean_path.endswith(VOD_URL_EXTENSIONS):
        return True
    if "/movie/" in clean_path or "/series/" in clean_path or "/vod/" in clean_path:
        return True
    if _RE_SERIES_EP_DETECT.search(extinf_line):
        return True
    return False


def download_raw_m3u_to_disk(url: str, target_path: Path) -> bool:
    br_proxy = os.environ.get("BR_PROXY", "").strip()

    for ua in IPTV_USER_AGENTS:
        fake_ip = random.choice(BR_RESIDENTIAL_IPS)
        cmd = [
            "curl", "-sS", "-L", "--http1.1",
            "--max-time", "300",
            "--connect-timeout", "25",
            "--retry", "2",
            "-A", ua,
            "-H", "Accept: */*",
            "-H", "Accept-Language: pt-BR,pt;q=0.9,en-US;q=0.8",
            "-H", f"X-Forwarded-For: {fake_ip}",
            "-H", f"X-Real-IP: {fake_ip}",
            "-H", f"CF-Connecting-IP: {fake_ip}",
            "-o", str(target_path),
            url,
        ]
        if br_proxy:
            cmd[1:1] = ["-x", br_proxy]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and target_path.exists() and target_path.stat().st_size > 2048:
            with target_path.open("r", encoding="utf-8", errors="ignore") as f:
                head = f.read(8192)
                if "#EXTM3U" in head.upper() or "#EXTINF" in head.upper():
                    return True

    session = create_http_client()
    for ua in IPTV_USER_AGENTS:
        headers = build_br_headers(ua, url)
        try:
            with session.get(url, headers=headers, stream=True, timeout=240, allow_redirects=True) as resp:
                if resp.status_code != 200:
                    continue
                with target_path.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
            if target_path.exists() and target_path.stat().st_size > 2048:
                with target_path.open("r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(8192)
                    if "#EXTM3U" in head.upper() or "#EXTINF" in head.upper():
                        return True
        except Exception:
            continue

    return False


def build_playlists(
    url: str,
    epg_index: dict[str, str],
    epg_public_url: str,
    full_out_path: Path,
    live_only_out_path: Path,
) -> tuple[int, int, int]:
    tmp_raw = tempfile.NamedTemporaryFile(delete=False, suffix=".raw.m3u")
    tmp_raw_path = Path(tmp_raw.name)
    tmp_raw.close()

    tmp_vod = tempfile.NamedTemporaryFile(delete=False, suffix=".vod.m3u")
    tmp_vod_path = Path(tmp_vod.name)
    tmp_vod.close()

    live_count = 0
    matched_epg = 0
    vod_count = 0

    try:
        maps = fetch_xtream_category_maps(url)
        live_by_id = maps["live_by_id"]
        live_by_name = maps["live_by_name"]
        vod_by_id = maps["vod_by_id"]
        vod_by_name = maps["vod_by_name"]
        series_by_name = maps["series_by_name"]

        print("  -> Baixando arquivo M3U completo...")
        if not download_raw_m3u_to_disk(url, tmp_raw_path):
            raise RuntimeError("O servidor bloqueou o download do arquivo M3U a partir do IP do GitHub.")

        raw_size_mb = tmp_raw_path.stat().st_size / (1024 * 1024)
        print(f"  -> ✓ M3U baixada ({raw_size_mb:.2f} MB). Organizando Canais, Filmes e Séries...")

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"

        with (
            tmp_raw_path.open("r", encoding="utf-8", errors="ignore") as raw_in,
            full_out_path.open("w", encoding="utf-8") as full_out,
            live_only_out_path.open("w", encoding="utf-8") as live_only_out,
            tmp_vod_path.open("w", encoding="utf-8") as vod_out,
        ):
            full_out.write(header)
            live_only_out.write(header)
            pending_lines: list[str] = []

            for raw_line in raw_in:
                line = raw_line.strip()
                if not line or line.upper().startswith("#EXTM3U"):
                    continue

                if line.startswith("#"):
                    pending_lines.append(line)
                    continue

                if line.startswith(("http://", "https://", "rtmp://", "rtsp://")) and pending_lines:
                    extinf_idx = next((idx for idx, l in enumerate(pending_lines) if l.upper().startswith("#EXTINF")), None)
                    if extinf_idx is None:
                        pending_lines.clear()
                        continue

                    extinf_line = pending_lines[extinf_idx]

                    grp_m = _RE_GROUP_TITLE.search(extinf_line)
                    curr_grp = grp_m.group(1).strip() if grp_m else ""
                    comma_name_m = re.search(r',([^,]*)$', extinf_line)
                    tvg_name_m = re.search(r'tvg-name="(.*?)"', extinf_line, re.I)
                    item_name = html.unescape(
                        (comma_name_m.group(1).strip() if comma_name_m else "")
                        or (tvg_name_m.group(1).strip() if tvg_name_m else "")
                    )

                    if REMOVE_ADULT and (ADULT_FILTER_RE.search(curr_grp) or ADULT_FILTER_RE.search(item_name)):
                        pending_lines.clear()
                        continue

                    stream_id_m = _RE_STREAM_ID_FROM_URL.search(line)
                    stream_id = stream_id_m.group(1) if stream_id_m else ""

                    if is_vod_stream(line, extinf_line):
                        is_series_item = (
                            "/series/" in line.lower()
                            or curr_grp.upper() in {"SERIES", "SÉRIES"}
                            or bool(_RE_SERIES_EP_DETECT.search(item_name))
                        )

                        if is_series_item:
                            s_key = norm_series_title(item_name)
                            real_vod_grp = series_by_name.get(s_key, "") or curr_grp or "Séries"
                        else:
                            real_vod_grp = (
                                vod_by_id.get(stream_id, "")
                                or vod_by_name.get(norm_lookup_key(item_name), "")
                                or vod_by_name.get(norm_series_title(item_name), "")
                                or curr_grp
                                or "Filmes"
                            )

                        if REMOVE_ADULT and ADULT_FILTER_RE.search(real_vod_grp):
                            pending_lines.clear()
                            continue

                        logo_m = _RE_TVG_LOGO.search(extinf_line)
                        logo_url = logo_m.group(1).strip() if logo_m else ""
                        logo_attr = f' tvg-logo="{logo_url}"' if logo_url else ""
                        safe_grp = real_vod_grp.replace('"', "'")

                        # Formato enxuto (sem repetir tvg-name de 80 caracteres) para toda a lista caber em <90MB (.m3u puro para TV Samsung!)
                        vod_out.write(f'#EXTINF:-1{logo_attr} group-title="{safe_grp}",{item_name}\n{line}\n')
                        vod_count += 1
                    else:
                        real_live_grp = (
                            live_by_id.get(stream_id, "")
                            or live_by_name.get(norm_lookup_key(item_name), "")
                            or live_by_name.get(name_key(item_name), "")
                        )
                        if REMOVE_ADULT and real_live_grp and ADULT_FILTER_RE.search(real_live_grp):
                            pending_lines.clear()
                            continue

                        if real_live_grp:
                            extinf_line = set_group_title_in_extinf(extinf_line, real_live_grp)

                        tvg_id_m = re.search(r'tvg-id="(.*?)"', extinf_line, re.I)
                        orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""

                        best_tvg_id = resolve_best_tvg_id(item_name, orig_tvg_id, epg_index)
                        if best_tvg_id and name_key(best_tvg_id) in epg_index:
                            matched_epg += 1

                        pending_lines[extinf_idx] = replace_only_tvg_id_in_extinf(extinf_line, best_tvg_id)
                        for pl in pending_lines:
                            full_out.write(pl + "\n")
                            live_only_out.write(pl + "\n")
                        full_out.write(line + "\n")
                        live_only_out.write(line + "\n")
                        live_count += 1

                    pending_lines.clear()

        if vod_count > 0:
            with full_out_path.open("ab") as full_append, tmp_vod_path.open("rb") as vod_in:
                shutil.copyfileobj(vod_in, full_append, length=1024 * 1024)

        return live_count, matched_epg, vod_count

    finally:
        if tmp_raw_path.exists():
            tmp_raw_path.unlink()
        if tmp_vod_path.exists():
            tmp_vod_path.unlink()


def push_files_to_secret_gist_via_git(generated_files: list[Path], token: str, gist_id: str) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="gist_push_"))
    try:
        gist_repo_url = f"https://x-access-token:{token}@gist.github.com/{gist_id}.git"
        subprocess.run(["git", "init", "-b", "main"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "http.postBuffer", "524288000"], cwd=work_dir, check=True)

        for fpath in generated_files:
            target_m3u = work_dir / fpath.name
            shutil.copy2(fpath, target_m3u)

            # Se for a lista completa, gera também o .gz como opção extra
            if fpath.name.startswith("lista_"):
                target_gz = work_dir / f"{fpath.name}.gz"
                with fpath.open("rb") as f_in, gzip.open(target_gz, "wb", compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

                if target_m3u.stat().st_size > 98 * 1024 * 1024:
                    print(
                        f"  -> Nota: {fpath.name} ultrapassou 98MB ({target_m3u.stat().st_size / (1024*1024):.1f} MB). "
                        f"Mantendo {target_gz.name} e canais_{fpath.name.split('_')[1]}!"
                    )
                    target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (Completa + Canais)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "push", "--force", gist_repo_url, "HEAD:main"], cwd=work_dir, check=True)
        print(f"\n✓ Gist Secreto ({gist_id}) atualizado via Git Push com sucesso!")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    epg_public_url = os.environ.get("EPG_PUBLIC_URL", "").strip()
    epg_index = load_epg_id_index(EPG_GZ_PATH)

    out_dir = Path("output_lists")
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_files: list[Path] = []

    for i in range(1, 6):
        url = os.environ.get(f"M3U_URL_{i}", "").strip()
        if not url or not url.startswith("http"):
            continue

        print(f"\nProcessando M3U_URL_{i}...")
        full_file = out_dir / f"lista_{i}.m3u"
        live_file = out_dir / f"canais_{i}.m3u"
        try:
            live_cnt, matched_cnt, vod_cnt = build_playlists(url, epg_index, epg_public_url, full_file, live_file)
            if live_cnt == 0 and vod_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            full_mb = full_file.stat().st_size / (1024 * 1024)
            live_mb = live_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] Completa: {live_cnt} Canais ({matched_cnt} com EPG) + {vod_cnt} Filmes/Séries ({full_mb:.2f} MB)\n"
                f"  -> ✓ [canais_{i}.m3u] Só Canais: {live_cnt} Canais ({live_mb:.2f} MB)"
            )
            generated_files.extend([full_file, live_file])
        except Exception as e:
            print(f"  -> ❌ Erro na M3U_URL_{i}: {e}")

    if not generated_files:
        raise RuntimeError("Nenhuma lista pôde ser gerada.")

    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()
    if gist_token and gist_id:
        push_files_to_secret_gist_via_git(generated_files, gist_token, gist_id)
    else:
        print("Aviso: Configure GIST_TOKEN e GIST_ID nos Secrets.")


if __name__ == "__main__":
    main()
