#!/usr/bin/env python3
"""Sincronizador Fiel de Listas M3U (Força m3u_plus + Restaura Grupos Reais de Canais, Filmes e Séries):
- Converte automaticamente links get.php para type=m3u_plus (evitando que o painel envie apenas 3 grupos genéricos).
- Consulta as categorias reais da API Xtream (Live + VOD + Series) para restaurar os grupos exatos caso o M3U venha achatado.
- Mantém Canais ao Vivo no topo (com tvg-id sincronizado com o epg.xml.gz) e Filmes/Séries separados nos seus grupos originais.
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

VOD_URL_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx")
_RE_TVG_ID = re.compile(r'tvg-id="[^"]*"', re.IGNORECASE)
_RE_GROUP_TITLE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
_RE_STREAM_ID_FROM_URL = re.compile(r"/(\d+)(?:\.[a-z0-9]+)?(?:\?.*)?$", re.IGNORECASE)

# Nomes de grupos genéricos que indicam que o servidor mandou uma lista achatada sem m3u_plus
GENERIC_BROKEN_GROUPS = {
    "",
    "canais",
    "geral",
    "canal completo",
    "canal completo sd hd fhd",
    "canal completo sd fhd",
    "filmes e series",
    "filmes e séries",
    "vod",
}


def ensure_m3u_plus_url(url: str) -> str:
    """Garante que qualquer link Xtream Codes use type=m3u_plus para o servidor enviar todos os group-title!"""
    parsed = urllib.parse.urlparse(url)
    if "get.php" not in parsed.path:
        return url
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    # Força m3u_plus (se estiver m3u simples, o servidor esmaga todos os grupos em 3 categorias!)
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


def fetch_xtream_category_maps(url: str) -> tuple[dict[str, str], dict[str, str]]:
    """Busca na API Xtream o mapa exato {stream_id: nome_do_grupo} para Canais Ao Vivo e para Filmes VOD,
    garantindo que mesmo se a M3U vier com grupos quebrados, cada canal e filme receba sua categoria real!"""
    live_id_to_group: dict[str, str] = {}
    vod_id_to_group: dict[str, str] = {}

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        return live_id_to_group, vod_id_to_group

    user = query["username"][0]
    pwd = query["password"][0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        # 1. Categorias e Streams de TV ao Vivo
        c_live = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories", timeout=25
        ).json()
        live_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_live if isinstance(c_live, list) else [])
            if isinstance(c, dict) and c.get("category_name")
        }
        s_live = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams", timeout=40
        ).json()
        for ch in (s_live if isinstance(s_live, list) else []):
            if isinstance(ch, dict) and ch.get("stream_id"):
                grp = live_cats.get(str(ch.get("category_id", "")), "")
                if grp:
                    live_id_to_group[str(ch["stream_id"])] = grp
        print(f"  -> ✓ Mapa de grupos de Canais carregado da API: {len(live_cats)} categorias ({len(live_id_to_group)} canais).")
    except Exception as e:
        print(f"  -> Nota: Mapa de categorias Live via API não disponível ({e}).")

    try:
        # 2. Categorias e Streams de Filmes (VOD)
        c_vod = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_categories", timeout=25
        ).json()
        vod_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_vod if isinstance(c_vod, list) else [])
            if isinstance(c, dict) and c.get("category_name")
        }
        s_vod = request_with_bypass(
            f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_streams", timeout=45
        ).json()
        for v in (s_vod if isinstance(s_vod, list) else []):
            if isinstance(v, dict) and v.get("stream_id"):
                grp = vod_cats.get(str(v.get("category_id", "")), "")
                if grp:
                    vod_id_to_group[str(v["stream_id"])] = grp
        print(f"  -> ✓ Mapa de grupos de Filmes carregado da API: {len(vod_cats)} categorias ({len(vod_id_to_group)} filmes).")
    except Exception as e:
        print(f"  -> Nota: Mapa de categorias VOD via API não disponível ({e}).")

    return live_id_to_group, vod_id_to_group


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


def ensure_group_title_in_extinf(extinf_line: str, desired_group: str) -> str:
    if not desired_group:
        return extinf_line
    safe_grp = desired_group.replace('"', "'")
    m = _RE_GROUP_TITLE.search(extinf_line)
    if m:
        curr_grp = m.group(1).strip()
        if not curr_grp or curr_grp.lower() in GENERIC_BROKEN_GROUPS or "canal completo" in curr_grp.lower():
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


def process_m3u_preserving_format(
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
        # Carrega o mapa oficial de categorias da API Xtream para garantir grupos perfeitos
        live_id_to_group, vod_id_to_group = fetch_xtream_category_maps(url)

        print("  -> Baixando arquivo M3U completo (com type=m3u_plus)...")
        if not download_raw_m3u_to_disk(url, tmp_raw_path):
            raise RuntimeError("O servidor bloqueou o download direto do arquivo M3U a partir do IP do GitHub.")

        raw_size_mb = tmp_raw_path.stat().st_size / (1024 * 1024)
        print(f"  -> ✓ Arquivo M3U baixado ({raw_size_mb:.2f} MB). Separando Canais, Filmes e Séries com seus grupos...")

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

                    # Se houver tag #EXTGRP: abaixo do #EXTINF, extrai o grupo dela caso o #EXTINF não tenha group-title
                    extgrp_val = ""
                    for pl in pending_lines:
                        if pl.upper().startswith("#EXTGRP:"):
                            extgrp_val = pl.split(":", 1)[1].strip()
                            break

                    stream_id_m = _RE_STREAM_ID_FROM_URL.search(line)
                    stream_id = stream_id_m.group(1) if stream_id_m else ""

                    if is_vod_stream(line, extinf_line):
                        # É Filme ou Série (VOD): restaura o grupo real se o M3U veio achatado e separa Filmes de Séries!
                        api_vod_grp = vod_id_to_group.get(stream_id, "") if stream_id else ""
                        is_series_ep = (
                            "/series/" in line.lower()
                            or bool(re.search(r"\bS\d{1,2}\s*E\d{1,3}\b|\bT\d{1,2}\s*EP?\d{1,3}\b", extinf_line, re.I))
                        )
                        fallback_vod_grp = api_vod_grp or extgrp_val or ("Séries" if is_series_ep else "Filmes")
                        pending_lines[extinf_idx] = ensure_group_title_in_extinf(extinf_line, fallback_vod_grp)

                        for pl in pending_lines:
                            vod_out.write(pl + "\n")
                        vod_out.write(line + "\n")
                        vod_count += 1
                    else:
                        # É Canal ao Vivo: restaura a categoria real da API (se o M3U veio com 'Canal Completo SD FHD')
                        # e substitui o tvg-id pelo ID exato do seu epg.xml.gz!
                        api_live_grp = live_id_to_group.get(stream_id, "") if stream_id else ""
                        fallback_live_grp = api_live_grp or extgrp_val
                        if fallback_live_grp:
                            extinf_line = ensure_group_title_in_extinf(extinf_line, fallback_live_grp)

                        tvg_id_m = re.search(r'tvg-id="(.*?)"', extinf_line, re.I)
                        tvg_name_m = re.search(r'tvg-name="(.*?)"', extinf_line, re.I)
                        comma_name_m = re.search(r',([^,]*)$', extinf_line)

                        orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                        ch_name = html.unescape(
                            (comma_name_m.group(1).strip() if comma_name_m else "")
                            or (tvg_name_m.group(1).strip() if tvg_name_m else "")
                        )

                        best_tvg_id = resolve_best_tvg_id(ch_name, orig_tvg_id, epg_index)
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
                    f"Enviando como {target_gz.name} ({target_gz.stat().st_size / (1024*1024):.1f} MB)!"
                )
                target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (Grupos Originais + TVG-ID)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
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
            live_cnt, matched_cnt, vod_cnt = process_m3u_preserving_format(url, epg_index, epg_public_url, out_file)
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
        raise RuntimeError("Nenhuma lista M3U completa pôde ser baixada.")

    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()
    if gist_token and gist_id:
        push_files_to_secret_gist_via_git(generated_files, gist_token, gist_id)
    else:
        print("Aviso: Configure GIST_TOKEN e GIST_ID nos Secrets.")


if __name__ == "__main__":
    main()
