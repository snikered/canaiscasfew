#!/usr/bin/env python3
"""Sincronizador 100% Fiel ao Arquivo .m3u Bruto:
- Baixa o arquivo m3u_plus imitando o navegador do PC para receber a lista completa sem achatar grupos.
- NÃO altera group-title, NÃO altera tvg-name e NÃO reconstrói linhas #EXTINF.
- Copia Filmes e Séries 100% idênticos (caractere por caractere) ao arquivo bruto original.
- Nos Canais ao Vivo: substitui EXCLUSIVAMENTE o valor dentro de tvg-id="..." pelo ID do epg.xml.gz.
"""
from __future__ import annotations

import gzip
import html
import os
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
REMOVE_ADULT = os.environ.get("REMOVE_ADULT", "1").strip().lower() in {"1", "true", "yes"}

ADULT_FILTER_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\+\s*18\b|\bONLYFANS\b|"
    r"\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b)",
    re.IGNORECASE,
)

VOD_URL_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx")
_RE_TVG_ID = re.compile(r'tvg-id="[^"]*"', re.IGNORECASE)
_RE_SERIES_EP_DETECT = re.compile(r"\b(?:S\d{1,2}\s*E\d{1,3}|T\d{1,2}\s*EP?\d{1,3})\b", re.IGNORECASE)

# Mesmo User-Agent de navegador Desktop que entrega o arquivo .m3u perfeito quando você baixa no PC
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}


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
    """Altera EXCLUSIVAMENTE o conteúdo de tvg-id="..." sem tocar em mais nenhum caractere da linha."""
    safe_id = new_tvg_id.replace('"', "'")
    if _RE_TVG_ID.search(extinf_line):
        return _RE_TVG_ID.sub(f'tvg-id="{safe_id}"', extinf_line, count=1)
    return re.sub(r"^(#EXTINF:[^\s,]+)", rf'\1 tvg-id="{safe_id}"', extinf_line, count=1)


def is_vod_stream(stream_url: str, extinf_line: str) -> bool:
    clean_path = stream_url.split("?", 1)[0].lower()
    if clean_path.endswith(VOD_URL_EXTENSIONS):
        return True
    if "/movie/" in clean_path or "/series/" in clean_path or "/vod/" in clean_path:
        return True
    if _RE_SERIES_EP_DETECT.search(extinf_line):
        return True
    return False


def download_raw_m3u_like_browser(url: str, target_path: Path) -> bool:
    """Baixa o arquivo .m3u exatamente como o navegador do PC faz, garantindo type=m3u_plus."""
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "get.php" in parsed.path:
        q["type"] = ["m3u_plus"]
        if "output" not in q:
            q["output"] = ["ts"]
    final_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q, doseq=True)))

    br_proxy = os.environ.get("BR_PROXY", "").strip()

    # 1. Tenta via Cloudscraper / Requests com cabeçalho de Navegador Chrome Desktop
    session = (
        cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
        if cloudscraper is not None
        else requests.Session()
    )
    if br_proxy:
        session.proxies = {"http": br_proxy, "https": br_proxy}

    try:
        with session.get(final_url, headers=BROWSER_HEADERS, stream=True, timeout=240, allow_redirects=True) as resp:
            if resp.status_code == 200:
                with target_path.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
                if target_path.exists() and target_path.stat().st_size > 2048:
                    with target_path.open("r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(8192)
                        if "#EXTM3U" in head.upper() or "#EXTINF" in head.upper():
                            return True
    except Exception as e:
        print(f"  -> Aviso no download via navegador Python ({e}). Tentando curl...")

    # 2. Fallback via curl imitando Chrome Desktop
    cmd = [
        "curl", "-sS", "-L", "--http1.1",
        "--max-time", "300",
        "--connect-timeout", "25",
        "-A", BROWSER_HEADERS["User-Agent"],
        "-H", f"Accept: {BROWSER_HEADERS['Accept']}",
        "-H", f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
        "-o", str(target_path),
        final_url,
    ]
    if br_proxy:
        cmd[1:1] = ["-x", br_proxy]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and target_path.exists() and target_path.stat().st_size > 2048:
        with target_path.open("r", encoding="utf-8", errors="ignore") as f:
            head = f.read(8192)
            if "#EXTM3U" in head.upper() or "#EXTINF" in head.upper():
                return True

    return False


def process_raw_m3u_only_fixing_tvgid(
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
    groups_found: set[str] = set()

    try:
        print("  -> Baixando arquivo .m3u bruto exatamente como no navegador...")
        if not download_raw_m3u_like_browser(url, tmp_raw_path):
            raise RuntimeError("Falha ao baixar o arquivo .m3u bruto.")

        raw_size_mb = tmp_raw_path.stat().st_size / (1024 * 1024)
        print(f"  -> ✓ Arquivo bruto baixado ({raw_size_mb:.2f} MB). Trocando apenas o tvg-id dos canais...")

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
                line = raw_line.rstrip("\r\n")
                if not line.strip() or line.strip().upper().startswith("#EXTM3U"):
                    continue

                if line.lstrip().startswith("#"):
                    pending_lines.append(line)
                    continue

                if line.lstrip().startswith(("http://", "https://", "rtmp://", "rtsp://")) and pending_lines:
                    extinf_idx = next((idx for idx, l in enumerate(pending_lines) if l.lstrip().upper().startswith("#EXTINF")), None)
                    if extinf_idx is None:
                        pending_lines.clear()
                        continue

                    extinf_line = pending_lines[extinf_idx]
                    grp_m = re.search(r'group-title="([^"]*)"', extinf_line, re.I)
                    curr_grp = grp_m.group(1).strip() if grp_m else ""
                    if curr_grp:
                        groups_found.add(curr_grp)

                    if REMOVE_ADULT and ADULT_FILTER_RE.search(curr_grp):
                        pending_lines.clear()
                        continue

                    if is_vod_stream(line.strip(), extinf_line):
                        # É Filme ou Série: COPIA 100% IDÊNTICO AO ARQUIVO BRUTO ORIGINAL!
                        for pl in pending_lines:
                            vod_out.write(pl + "\n")
                        vod_out.write(line + "\n")
                        vod_count += 1
                    else:
                        # É Canal ao Vivo: substitui SOMENTE o tvg-id="..." mantendo a linha original intacta!
                        tvg_id_m = re.search(r'tvg-id="([^"]*)"', extinf_line, re.I)
                        tvg_name_m = re.search(r'tvg-name="([^"]*)"', extinf_line, re.I)
                        comma_name_m = re.search(r',([^,]*)$', extinf_line)

                        orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                        item_name = html.unescape(
                            (comma_name_m.group(1).strip() if comma_name_m else "")
                            or (tvg_name_m.group(1).strip() if tvg_name_m else "")
                        )

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

        # Diagnóstico no log: mostra quantos group-titles diferentes vieram no arquivo bruto baixado!
        sample_groups = sorted(groups_found)[:12]
        print(f"  -> Diagnóstico do arquivo baixado: {len(groups_found)} grupos distintos encontrados na M3U.")
        print(f"  -> Amostra dos grupos presentes no arquivo: {sample_groups}")

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

            if fpath.name.startswith("lista_"):
                target_gz = work_dir / f"{fpath.name}.gz"
                with fpath.open("rb") as f_in, gzip.open(target_gz, "wb", compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

                if target_m3u.stat().st_size > 98 * 1024 * 1024:
                    print(
                        f"  -> Nota: {fpath.name} tem {target_m3u.stat().st_size / (1024*1024):.1f} MB (>98MB). "
                        f"Subindo {target_gz.name} ({target_gz.stat().st_size / (1024*1024):.1f} MB) + canais_{fpath.name.split('_')[1]}!"
                    )
                    target_m3u.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (100% Bruto Original + TVG-ID)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
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
            live_cnt, matched_cnt, vod_cnt = process_raw_m3u_only_fixing_tvgid(
                url, epg_index, epg_public_url, full_file, live_file
            )
            if live_cnt == 0 and vod_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            full_mb = full_file.stat().st_size / (1024 * 1024)
            live_mb = live_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] {live_cnt} Canais ({matched_cnt} com EPG) + {vod_cnt} VODs ({full_mb:.2f} MB)\n"
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
