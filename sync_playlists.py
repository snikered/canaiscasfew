#!/usr/bin/env python3
"""Sincronizador M3U via API Xtream (Preserva 100% dos Grupos, Subgrupos, Filmes e Séries Originais):
- Conecta diretamente à API do painel (player_api.php).
- Baixa o mapa exato de categorias de Canais Ao Vivo, Filmes VOD e Séries.
- Corrige o tvg-id APENAS dos Canais ao Vivo usando o output/epg.xml.gz.
- Mantém Filmes e Séries nas suas categorias originais exatas.
- Publica no Gist Secreto via Git Push.
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

from merge_epg import fetch_iptv_json, name_key, normalizar_canal, strip_qualities_keep_case

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

# Bloqueia apenas categorias e conteúdos adultos
ADULT_FILTER_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\+\s*18\b|\bONLYFANS\b|"
    r"\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b)",
    re.IGNORECASE,
)


def build_br_headers(ua: str) -> dict[str, str]:
    fake_ip = random.choice(BR_RESIDENTIAL_IPS)
    return {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "X-Forwarded-For": fake_ip,
        "X-Real-IP": fake_ip,
        "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip,
        "Connection": "keep-alive",
    }


def request_api_with_bypass(url: str, timeout: int = 45) -> any:
    session = requests.Session()
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    if br_proxy:
        session.proxies = {"http": br_proxy, "https": br_proxy}

    last_err = None
    for ua in IPTV_USER_AGENTS:
        headers = build_br_headers(ua)
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            last_err = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Falha na API Xtream ({last_err})")


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


def build_playlist_from_xtream_api(
    url: str, epg_index: dict[str, str], epg_public_url: str, out_file_path: Path
) -> tuple[int, int, int]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        raise RuntimeError("URL fornecida não é um link Xtream Codes válido.")

    user = query["username"][0]
    pwd = query["password"][0]
    out_fmt = (query.get("output", ["ts"])[0] or "ts").lower()
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    ext = "m3u8" if out_fmt in {"m3u8", "hls"} else "ts"

    header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"
    live_count, matched_epg, vod_count = 0, 0, 0

    with out_file_path.open("w", encoding="utf-8") as out:
        out.write(header)

        # 1. Canais Ao Vivo
        try:
            live_cats_data = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories")
            live_cat_map = {
                str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Canais")).strip())
                for c in (live_cats_data if isinstance(live_cats_data, list) else [])
                if isinstance(c, dict)
            }
            live_streams = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams")

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

                out.write(f'#EXTINF:-1 tvg-id="{best_tvg_id}" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{stream_url}\n')
                live_count += 1
            print(f"  -> ✓ {live_count} Canais ao Vivo processados com grupos originais.")
        except Exception as e:
            print(f"  -> Aviso ao puxar canais ao vivo via API: {e}")

        # 2. Filmes (VOD) - Mantém 100% as categorias e nomes originais do painel
        try:
            vod_cats_data = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_categories")
            vod_cat_map = {
                str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Filmes")).strip())
                for c in (vod_cats_data if isinstance(vod_cats_data, list) else [])
                if isinstance(c, dict)
            }
            vod_streams = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_vod_streams")

            for v in (vod_streams if isinstance(vod_streams, list) else []):
                if not isinstance(v, dict):
                    continue
                name = html.unescape(str(v.get("name", "")).strip())
                stream_id = v.get("stream_id")
                container_ext = str(v.get("container_extension", "mp4")).strip() or "mp4"
                if not name or not stream_id:
                    continue

                group = vod_cat_map.get(str(v.get("category_id", "")), "Filmes")
                if ADULT_FILTER_RE.search(group) or ADULT_FILTER_RE.search(name):
                    continue

                logo = str(v.get("stream_icon", "")).strip()
                logo_attr = f' tvg-logo="{logo}"' if logo else ""
                vod_url = f"{base_url}/movie/{user}/{pwd}/{stream_id}.{container_ext}"

                out.write(f'#EXTINF:-1 tvg-id="" tvg-name="{name}"{logo_attr} group-title="{group}",{name}\n{vod_url}\n')
                vod_count += 1
            print(f"  -> ✓ {vod_count} Filmes VOD processados com categorias originais.")
        except Exception as e:
            print(f"  -> Aviso ao puxar filmes VOD via API: {e}")

        # 3. Séries - Mantém 100% as categorias e estrutura de séries originais
        try:
            series_cats_data = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_series_categories")
            series_cat_map = {
                str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "Séries")).strip())
                for c in (series_cats_data if isinstance(series_cats_data, list) else [])
                if isinstance(c, dict)
            }
            series_list = request_api_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_series")

            for s in (series_list if isinstance(series_list, list) else []):
                if not isinstance(s, dict):
                    continue
                series_id = s.get("series_id")
                series_name = html.unescape(str(s.get("name", "")).strip())
                if not series_id or not series_name:
                    continue

                group = series_cat_map.get(str(s.get("category_id", "")), "Séries")
                if ADULT_FILTER_RE.search(group) or ADULT_FILTER_RE.search(series_name):
                    continue

                # Puxa as temporadas/episódios de cada série
                info_url = f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_series_info&series_id={series_id}"
                info_data = request_api_with_bypass(info_url, timeout=20)
                if not isinstance(info_data, dict):
                    continue

                episodes = info_data.get("episodes", {})
                cover = str(info_data.get("info", {}).get("cover", "")).strip()
                logo_attr = f' tvg-logo="{cover}"' if cover else ""

                for season_num, eps in episodes.items():
                    if not isinstance(eps, list):
                        continue
                    for ep in eps:
                        if not isinstance(ep, dict):
                            continue
                        ep_num = ep.get("episode_num", 1)
                        container_ext = str(ep.get("container_extension", "mp4")).strip() or "mp4"
                        ep_id = ep.get("id")
                        if not ep_id:
                            continue

                        ep_title = html.unescape(str(ep.get("title", f"S{season_num}E{ep_num}")).strip())
                        full_name = f"{series_name} - S{int(season_num):02d}E{int(ep_num):02d} - {ep_title}"
                        ep_url = f"{base_url}/series/{user}/{pwd}/{ep_id}.{container_ext}"

                        out.write(f'#EXTINF:-1 tvg-id="" tvg-name="{full_name}"{logo_attr} group-title="{group}",{full_name}\n{ep_url}\n')
                        vod_count += 1
            print(f"  -> ✓ Séries processadas com temporadas e grupos originais.")
        except Exception as e:
            print(f"  -> Aviso ao puxar séries via API: {e}")

    return live_count, matched_epg, vod_count


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
                    f"  -> Nota: {fpath.name} ultrapassou 95MB. "
                    f"Enviando versão compactada {target_gz.name} para o Gist!"
                )
                target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U via API Xtream (Grupos Oficiais Restaudados)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "push", "--force", gist_repo_url, "HEAD:main"], cwd=work_dir, check=True)
        print(f"\n✓ Gist Secreto ({gist_id}) atualizado com sucesso!")
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

        print(f"\nProcessando M3U_URL_{i} via API Xtream Oficial...")
        out_file = out_dir / f"lista_{i}.m3u"
        try:
            live_cnt, matched_cnt, vod_cnt = build_playlist_from_xtream_api(url, epg_index, epg_public_url, out_file)
            if live_cnt == 0 and vod_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            size_mb = out_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] {live_cnt} Canais ao Vivo ({matched_cnt} com EPG) + "
                f"{vod_cnt} VODs (Filmes e Séries) | Tamanho: {size_mb:.2f} MB"
            )
            generated_files.append(out_file)
        except Exception as e:
            print(f"  -> ❌ Erro ao processar M3U_URL_{i}: {e}")

    if not generated_files:
        raise RuntimeError("Nenhuma lista pôde ser gerada pela API.")

    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()
    if gist_token and gist_id:
        push_files_to_secret_gist_via_git(generated_files, gist_token, gist_id)
    else:
        print("Aviso: Configure GIST_TOKEN e GIST_ID nos Secrets.")


if __name__ == "__main__":
    main()
