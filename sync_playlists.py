#!/usr/bin/env python3
"""Sincronizador Completo M3U + Categorias Reais da API Xtream:
- Baixa os mapas de categorias reais da API em apenas 6 chamadas rápidas (Live, VOD e Series).
- Lê a M3U completa (Canais + Filmes + Séries) e substitui os grupos genéricos
  ('CANAIS COMPLETO(SD,HD,FHD,4K)', 'FILMES', 'SÉRIES') pelas categorias reais do servidor!
- Corrige o tvg-id APENAS dos Canais ao Vivo usando o seu epg.xml.gz e posiciona os canais no topo.
- Remove a categoria ADULTOS (opcional via variável REMOVE_ADULT=1, padrão ativo).
"""
from __future__ import annotations

import gzip
import html
import json
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
_RE_STREAM_ID_FROM_URL = re.compile(r"/(\d+)(?:\.[a-z0-9]+)?(?:\?.*)?$", re.IGNORECASE)
_RE_SERIES_EP_SPLIT = re.compile(r"\s*[\-\|:]?\s*\b(?:S\d{1,2}\s*E\d{1,3}|T\d{1,2}\s*EP?\d{1,3})\b.*$", re.IGNORECASE)


def norm_series_title(title: str) -> str:
    s = html.unescape(title or "")
    s = _RE_SERIES_EP_SPLIT.sub("", s)
    s = re.sub(r"\(\d{4}\)|\[\d{4}\]", " ", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


def ensure_m3u_plus_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "get.php" not in parsed.path:
        return url
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    q["type"] = ["m3u_plus"]
    if "output" not in q:
        q["output"] = ["ts"]
    new_query = urllib.parse.urlencode(q, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


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


def fetch_xtream_category_maps(url: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Consulta a API Xtream em apenas 6 chamadas rápidas para mapear:
    1. {stream_id: categoria_real_canal}
    2. {stream_id: categoria_real_filme}
    3. {nome_normalizado_da_serie: categoria_real_serie}
    """
    live_id_to_group: dict[str, str] = {}
    vod_id_to_group: dict[str, str] = {}
    series_name_to_group: dict[str, str] = {}

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        return live_id_to_group, vod_id_to_group, series_name_to_group

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
            if isinstance(ch, dict) and ch.get("stream_id"):
                grp = live_cats.get(str(ch.get("category_id", "")), "")
                if grp:
                    live_id_to_group[str(ch["stream_id"])] = grp
        print(f"  -> ✓ Categorias de Canais mapeadas via API: {len(live_cats)} grupos ({len(live_id_to_group)} canais).")
    except Exception as e:
        print(f"  -> Nota: Mapa de categorias Live indisponível ({e}).")

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
            if isinstance(v, dict) and v.get("stream_id"):
                grp = vod_cats.get(str(v.get("category_id", "")), "")
                if grp:
                    vod_id_to_group[str(v["stream_id"])] = grp
        print(f"  -> ✓ Categorias de Filmes mapeadas via API: {len(vod_cats)} grupos ({len(vod_id_to_group)} filmes).")
    except Exception as e:
        print(f"  -> Nota: Mapa de categorias VOD indisponível ({e}).")

    # 3. Séries (Apenas 2 chamadas: get_series_categories + get_series!)
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
                    series_name_to_group[s_key] = grp
        print(f"  -> ✓ Categorias de Séries mapeadas via API: {len(ser_cats)} grupos ({len(series_name_to_group)} séries).")
    except Exception as e:
        print(f"  -> Nota: Mapa de categorias de Séries indisponível ({e}).")

    return live_id_to_group, vod_id_to_group, series_name_to_group


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


def is_generic_flattened_group(grp: str) -> bool:
    """Detecta se o group-title que veio na M3U é um daqueles 4 grupões achatados do painel."""
    g = (grp or "").strip().upper()
    if not g:
        return True
    if "CANAIS COMPLETO" in g or "CANAL COMPLETO" in g:
        return True
    if g in {"CANAIS", "GERAL", "FILMES", "SERIES", "SÉRIES", "VOD", "FILMES E SERIES", "FILMES E SÉRIES", "CANAIS INTERNACIONAIS"}:
        return True
    return False


def apply_real_group_title(extinf_line: str, real_group: str) -> str:
    if not real_group:
        return extinf_line
    safe_grp = real_group.replace('"', "'")
    m = _RE_GROUP_TITLE.search(extinf_line)
    if m:
        curr_grp = m.group(1).strip()
        if is_generic_flattened_group(curr_grp):
            return _RE_GROUP_TITLE.sub(f'group-title="{safe_grp}"', extinf_line, count=1)
        return extinf_line
    return re.sub(r"^(#EXTINF:[^\s,]+)", rf'\1 group-title="{safe_grp}"', extinf_line, count=1)


def is_vod_stream(stream_url: str, extinf_line: str) -> bool:
    clean_path = stream_url.split("?", 1)[0].lower()
    if clean_path.endswith(VOD_URL_EXTENSIONS):
        return True
    if "/movie/" in clean_path or "/series/" in clean_path or "/vod/" in clean_path:
        return True
    if re.search(r"\bS\d{1,2}\s*E\d{1,3}\b|\bT\d{1,2}\s*EP?\d{1,3}\b", extinf_line, re.I):
        return True
    return False


def download_raw_m3u_to_disk(url: str, target_path: Path) -> bool:
    plus_url = ensure_m3u_plus_url(url)
    urls_to_try = [plus_url] if plus_url == url else [plus_url, url]
    br_proxy = os.environ.get("BR_PROXY", "").strip()

    for target_url in urls_to_try:
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
                target_url,
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
            headers = build_br_headers(ua, target_url)
            try:
                with session.get(target_url, headers=headers, stream=True, timeout=240, allow_redirects=True) as resp:
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


def process_m3u_with_real_groups(
    url: str, epg_index: dict[str, str], epg_public_url: str, out_file_path: Path
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
        # 1. Puxa os mapas de categorias reais da API Xtream em 6 requisições rápidas
        live_id_to_group, vod_id_to_group, series_name_to_group = fetch_xtream_category_maps(url)

        # 2. Baixa a M3U completa (Canais + Filmes + Séries)
        print("  -> Baixando arquivo M3U completo...")
        if not download_raw_m3u_to_disk(url, tmp_raw_path):
            raise RuntimeError("O servidor bloqueou o download do arquivo M3U a partir do IP do GitHub.")

        raw_size_mb = tmp_raw_path.stat().st_size / (1024 * 1024)
        print(f"  -> ✓ M3U baixada ({raw_size_mb:.2f} MB). Aplicando categorias reais da API e corrigindo TVG-IDs...")

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"

        with (
            tmp_raw_path.open("r", encoding="utf-8", errors="ignore") as raw_in,
            out_file_path.open("w", encoding="utf-8") as live_out,
            tmp_vod_path.open("w", encoding="utf-8") as vod_out,
        ):
            live_out.write(header)
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

                    tvg_id_m = re.search(r'tvg-id="(.*?)"', extinf_line, re.I)
                    tvg_name_m = re.search(r'tvg-name="(.*?)"', extinf_line, re.I)
                    grp_m = _RE_GROUP_TITLE.search(extinf_line)
                    comma_name_m = re.search(r',([^,]*)$', extinf_line)

                    orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                    curr_grp = grp_m.group(1).strip() if grp_m else ""
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
                        # É Filme ou Série (VOD): descobre o subgrupo real daquele Filme ou daquela Série!
                        real_vod_grp = ""
                        if "/movie/" in line.lower() and stream_id in vod_id_to_group:
                            real_vod_grp = vod_id_to_group[stream_id]
                        elif "/series/" in line.lower() or re.search(r"\bS\d{1,2}\s*E\d{1,3}\b", item_name, re.I):
                            s_key = norm_series_title(item_name)
                            real_vod_grp = series_name_to_group.get(s_key, "")
                        elif stream_id in vod_id_to_group:
                            real_vod_grp = vod_id_to_group[stream_id]

                        if REMOVE_ADULT and real_vod_grp and ADULT_FILTER_RE.search(real_vod_grp):
                            pending_lines.clear()
                            continue

                        if real_vod_grp:
                            pending_lines[extinf_idx] = apply_real_group_title(extinf_line, real_vod_grp)

                        for pl in pending_lines:
                            vod_out.write(pl + "\n")
                        vod_out.write(line + "\n")
                        vod_count += 1
                    else:
                        # É Canal ao Vivo: aplica a categoria real do canal (ex: 'CANAIS | ESPORTES') + corrige o tvg-id!
                        real_live_grp = live_id_to_group.get(stream_id, "") if stream_id else ""
                        if REMOVE_ADULT and real_live_grp and ADULT_FILTER_RE.search(real_live_grp):
                            pending_lines.clear()
                            continue

                        if real_live_grp:
                            extinf_line = apply_real_group_title(extinf_line, real_live_grp)

                        best_tvg_id = resolve_best_tvg_id(item_name, orig_tvg_id, epg_index)
                        if best_tvg_id and name_key(best_tvg_id) in epg_index:
                            matched_epg += 1

                        pending_lines[extinf_idx] = replace_only_tvg_id_in_extinf(extinf_line, best_tvg_id)
                        for pl in pending_lines:
                            live_out.write(pl + "\n")
                        live_out.write(line + "\n")
                        live_count += 1

                    pending_lines.clear()

        if vod_count > 0:
            with out_file_path.open("ab") as live_append, tmp_vod_path.open("rb") as vod_in:
                shutil.copyfileobj(vod_in, live_append, length=1024 * 1024)

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

            target_gz = work_dir / f"{fpath.name}.gz"
            with fpath.open("rb") as f_in, gzip.open(target_gz, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

            if target_m3u.stat().st_size > 95 * 1024 * 1024:
                print(
                    f"  -> Nota: {fpath.name} tem {target_m3u.stat().st_size / (1024*1024):.1f} MB (>95MB). "
                    f"Enviando versão compactada {target_gz.name} ({target_gz.stat().st_size / (1024*1024):.1f} MB)!"
                )
                target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (Categorias Reais + TVG-ID)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
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
        out_file = out_dir / f"lista_{i}.m3u"
        try:
            live_cnt, matched_cnt, vod_cnt = process_m3u_with_real_groups(url, epg_index, epg_public_url, out_file)
            if live_cnt == 0 and vod_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            size_mb = out_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] {live_cnt} Canais ao Vivo no topo ({matched_cnt} com EPG) + "
                f"{vod_cnt} Filmes/Séries abaixo | Tamanho total: {size_mb:.2f} MB"
            )
            generated_files.append(out_file)
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
