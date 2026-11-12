#!/usr/bin/env python3
"""Sincronizador Completo de Listas M3U (Canais no Topo com TVG-ID Corrigido + VODs Abaixo):
- Separa Canais ao Vivo e VODs (Filmes e Séries).
- Aplica correção precisa de tvg-id APENAS nos Canais ao Vivo usando o seu epg.xml.gz.
- Mantém todos os VODs (Filmes e Séries) intactos logo abaixo dos canais ao vivo.
- Remove apenas conteúdo Adulto (+18 / XXX).
- Publica via Git Push direto no Gist Secreto para suportar listas grandes (100MB~150MB+) + versão .m3u.gz.
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

# Bloqueia apenas conteúdo Adulto (Preserva Filmes, Séries e 24h!)
ADULT_FILTER_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\+\s*18\b|\bONLYFANS\b|"
    r"\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b)",
    re.IGNORECASE,
)

# Identifica grupos ou nomes que são VOD ou 24h (não devem receber tvg-id de canal linear)
VOD_GROUP_RE = re.compile(
    r"(\bVOD\b|\bFILMES?\b|\bSERIES?\b|\bSÉRIES?\b|\bLAN[CÇ]AMENTOS?\b|\bNOVELAS?\b|"
    r"\bCOLET[AÂ]NEA\b|\bNETFLIX\b|\bDISNEY\+?\b|\bPRIME\s*VIDEO\b|\bMAX\b|\bGLOBOPLAY\b|"
    r"\bAPPLE\s*TV\b|\bPARAMOUNT\+\b|\bCRUNCHYROLL\b|\bDORAMAS?\b|\bANIMES?\b|\bCINEMA\b)",
    re.IGNORECASE,
)
CHANNEL_24H_OR_EP_RE = re.compile(
    r"(\b24\s*H(?:S|ORAS)?\b|\bS\d{1,2}\s*E\d{1,3}\b|\bT\d{1,2}\s*EP?\d{1,3}\b)",
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
    if CHANNEL_24H_OR_EP_RE.search(channel_name):
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

    return strip_qualities_keep_case(channel_name) or channel_name


def is_vod_entry(stream_url: str, group: str, name: str) -> bool:
    clean_path = stream_url.split("?", 1)[0].lower()
    if clean_path.endswith(VOD_URL_EXTENSIONS) or "/movie/" in clean_path or "/series/" in clean_path:
        return True
    if re.search(r"\bS\d{1,2}\s*E\d{1,3}\b|\bT\d{1,2}\s*EP?\d{1,3}\b", name, re.I):
        return True
    if VOD_GROUP_RE.search(group) and not any(
        ch_kw in group.upper() for ch_kw in ("CANAIS", "TV ABERTA", "ESPORTES", "TELECINE", "HBO", "DOCUMENTARIOS", "INFANTIL", "NOTICIAS", "VARIEDADES")
    ):
        return True
    return False


def build_combined_playlist(
    url: str, epg_index: dict[str, str], epg_public_url: str, out_file_path: Path
) -> tuple[int, int, int]:
    """Gera o arquivo M3U com TODOS os Canais ao Vivo no começo (com tvg-id corrigido)
    e TODOS os VODs (Filmes e Séries) logo depois!
    """
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
        # 1. Tenta baixar o arquivo M3U completo em streaming (que contém Canais + Filmes + Séries)
        m3u_downloaded = False
        try:
            print("  -> Baixando M3U completa (Canais + VODs) via stream...")
            resp = request_with_bypass(url, stream=True, timeout=240)
            with tmp_raw_path.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
            if tmp_raw_path.stat().st_size > 1024:
                m3u_downloaded = True
        except Exception as e:
            print(f"  -> Download direto da M3U completa falhou ({e}). Usando API Xtream separada (Canais + Filmes)...")

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"

        if m3u_downloaded:
            # Separa em disco: grava Canais direto no out_file_path (topo) e VODs no tmp_vod_path
            with (
                tmp_raw_path.open("r", encoding="utf-8", errors="ignore") as raw_in,
                out_file_path.open("w", encoding="utf-8") as live_out,
                tmp_vod_path.open("w", encoding="utf-8") as vod_out,
            ):
                live_out.write(header)
                current_extinf: str | None = None

                for raw_line in raw_in:
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("#EXTINF"):
                        current_extinf = line
                        continue
                    if line.startswith(("http://", "https://", "rtmp://")) and current_extinf:
                        extinf = current_extinf
                        current_extinf = None

                        group_m = re.search(r'group-title="(.*?)"', extinf, re.I)
                        tvg_id_m = re.search(r'tvg-id="(.*?)"', extinf, re.I)
                        logo_m = re.search(r'tvg-logo="(.*?)"', extinf, re.I)
                        name_m = re.search(r',([^,]*)$', extinf)

                        group = html.unescape(group_m.group(1).strip()) if group_m else "Geral"
                        orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                        logo = logo_m.group(1).strip() if logo_m else ""
                        name = html.unescape(name_m.group(1).strip()) if name_m else ""
                        if not name:
                            continue

                        # Bloqueia apenas conteúdo Adulto
                        if ADULT_FILTER_RE.search(group) or ADULT_FILTER_RE.search(name):
                            continue

                        logo_attr = f' tvg-logo="{logo}"' if logo else ""

                        if is_vod_entry(line, group, name):
                            # É VOD (Filme ou Série): mantém intacto no bloco de VODs (com tvg-id vazio para não pesar no EPG)
                            vod_out.write(
                                f'#EXTINF:-1 tvg-id="" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{line}\n'
                            )
                            vod_count += 1
                        else:
                            # É Canal ao Vivo: corrige o tvg-id com precisão e grava no topo!
                            best_tvg_id = resolve_best_tvg_id(name, orig_tvg_id, epg_index)
                            if best_tvg_id and name_key(best_tvg_id) in epg_index:
                                matched_epg += 1
                            live_out.write(
                                f'#EXTINF:-1 tvg-id="{best_tvg_id}" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{line}\n'
                            )
                            live_count += 1

            # Concatena todos os VODs logo abaixo dos Canais ao Vivo!
            if vod_count > 0:
                with out_file_path.open("ab") as live_append, tmp_vod_path.open("rb") as vod_in:
                    shutil.copyfileobj(vod_in, live_append, length=1024 * 1024)

            return live_count, matched_epg, vod_count

        # 2. Fallback via API Xtream (Baixa Canais Ao Vivo + Filmes VOD separados e junta!)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "username" not in query or "password" not in query:
            raise RuntimeError("URL não é Xtream Codes e download direto falhou.")

        user = query["username"][0]
        pwd = query["password"][0]
        out_fmt = (query.get("output", ["ts"])[0] or "ts").lower()
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        ext = "m3u8" if out_fmt in {"m3u8", "hls"} else "ts"

        # 2A. Busca Categorias e Canais Ao Vivo
        cat_live_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories"
        cats_live = request_with_bypass(cat_live_url, timeout=30).json()
        live_cat_map = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Canais")).strip())
            for c in (cats_live if isinstance(cats_live, list) else [])
            if isinstance(c, dict)
        }

        streams_live_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams"
        live_streams = request_with_bypass(streams_live_url, timeout=45).json()

        with out_file_path.open("w", encoding="utf-8") as out:
            out.write(header)

            # Grava TODOS os Canais ao Vivo no começo do arquivo
            for ch in (live_streams if isinstance(live_streams, list) else []):
                if not isinstance(ch, dict):
                    continue
                name = html.unescape(str(ch.get("name", "")).strip())
                stream_id = ch.get("stream_id")
                if not name or not stream_id:
                    continue
                group = live_cat_map.get(str(ch.get("category_id", "")), "Canais")
                if ADULT_FILTER_RE.search(group) or ADULT_FILTER_RE.search(name):
                    continue

                orig_tvg_id = str(ch.get("epg_channel_id", "")).strip()
                best_tvg_id = resolve_best_tvg_id(name, orig_tvg_id, epg_index)
                if best_tvg_id and name_key(best_tvg_id) in epg_index:
                    matched_epg += 1

                logo = str(ch.get("stream_icon", "")).strip()
                logo_attr = f' tvg-logo="{logo}"' if logo else ""
                stream_url = f"{base_url}/live/{user}/{pwd}/{stream_id}.{ext}"

                out.write(
                    f'#EXTINF:-1 tvg-id="{best_tvg_id}" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{stream_url}\n'
                )
                live_count += 1

            # 2B. Busca Categorias e Filmes VOD separadamente na API e junta abaixo dos canais!
            try:
                cat_vod_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_categories"
                cats_vod = request_with_bypass(cat_vod_url, timeout=30).json()
                vod_cat_map = {
                    str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Filmes")).strip())
                    for c in (cats_vod if isinstance(cats_vod, list) else [])
                    if isinstance(c, dict)
                }

                streams_vod_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_streams"
                vod_streams = request_with_bypass(streams_vod_url, timeout=60).json()

                for vod in (vod_streams if isinstance(vod_streams, list) else []):
                    if not isinstance(vod, dict):
                        continue
                    name = html.unescape(str(vod.get("name", "")).strip())
                    stream_id = vod.get("stream_id")
                    container_ext = str(vod.get("container_extension", "mp4")).strip() or "mp4"
                    if not name or not stream_id:
                        continue
                    group = vod_cat_map.get(str(vod.get("category_id", "")), "Filmes")
                    if ADULT_FILTER_RE.search(group) or ADULT_FILTER_RE.search(name):
                        continue

                    logo = str(vod.get("stream_icon", "")).strip()
                    logo_attr = f' tvg-logo="{logo}"' if logo else ""
                    vod_url = f"{base_url}/movie/{user}/{pwd}/{stream_id}.{container_ext}"

                    out.write(
                        f'#EXTINF:-1 tvg-id="" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{vod_url}\n'
                    )
                    vod_count += 1
            except Exception as e_vod:
                print(f"  -> Aviso ao puxar VODs via API ({e_vod}).")

        return live_count, matched_epg, vod_count

    finally:
        if tmp_raw_path.exists():
            tmp_raw_path.unlink()
        if tmp_vod_path.exists():
            tmp_vod_path.unlink()


def push_files_to_secret_gist_via_git(generated_files: list[Path], token: str, gist_id: str) -> None:
    """Usa Git Push direto para o repositório do Gist Secreto (suporta arquivos grandes sem erro na API JSON)."""
    work_dir = Path(tempfile.mkdtemp(prefix="gist_push_"))
    try:
        gist_repo_url = f"https://x-access-token:{token}@gist.github.com/{gist_id}.git"
        subprocess.run(["git", "init", "-b", "main"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "http.postBuffer", "524288000"], cwd=work_dir, check=True)

        for fpath in generated_files:
            # Copia o arquivo .m3u completo
            target_m3u = work_dir / fpath.name
            shutil.copy2(fpath, target_m3u)

            # Gera também a versão compactada .m3u.gz (10x menor, ideal caso a lista passe de 100 MB)
            target_gz = work_dir / f"{fpath.name}.gz"
            with fpath.open("rb") as f_in, gzip.open(target_gz, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

            # Se o arquivo .m3u descompactado passar de 95 MB (limite hard do Git do GitHub é 100 MB),
            # mantém apenas a versão .m3u.gz no Gist para o push nunca ser rejeitado!
            if target_m3u.stat().st_size > 95 * 1024 * 1024:
                print(
                    f"  -> Nota: {fpath.name} tem {target_m3u.stat().st_size / (1024*1024):.1f} MB (>95MB). "
                    f"Enviando versão compactada {target_gz.name} ({target_gz.stat().st_size / (1024*1024):.1f} MB)!"
                )
                target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (Canais + VOD)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
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
            live_cnt, matched_cnt, vod_cnt = build_combined_playlist(url, epg_index, epg_public_url, out_file)
            if live_cnt == 0 and vod_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            size_mb = out_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] {live_cnt} Canais ao Vivo no topo ({matched_cnt} com EPG) + "
                f"{vod_cnt} VODs abaixo | Tamanho total: {size_mb:.2f} MB"
            )
            generated_files.append(out_file)
        except Exception as e:
            print(f"  -> ❌ Erro/Bloqueio na M3U_URL_{i}: {e}")

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
