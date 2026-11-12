#!/usr/bin/env python3
"""Sincronizador M3U (canais ao vivo corrigidos, VOD 100% intacto, ordem original preservada).

Regras:
  * Passagem ÚNICA, na ordem original do arquivo. Nada é reordenado, nada vai para "o fim".
  * VOD (Filmes/Séries): a linha #EXTINF, as linhas auxiliares e a URL são copiadas byte a byte.
  * Canal ao vivo: só mudam (a) o tvg-id, resolvido pelo NOME contra o epg.xml.gz, e (b) o group-title,
    apenas quando o canal está num grupo "agregador" (ex.: CANAIS COMPLETO) e a API Xtream informa
    uma categoria real, de forma inequívoca (por stream_id, ou por nome que só existe em UMA categoria).
  * Sem correspondência no EPG o tvg-id vira o nome limpo do canal (como no script anterior): um ID errado do provedor
    não amarra o canal à grade de outro. UNMATCHED_TVG_ID=name|keep|clear muda isso.
  * Relatório de conservação: entradas lidas = escritas + adultos removidos + duplicadas removidas.

Variáveis de ambiente: M3U_URL_1..5, EPG_PUBLIC_URL, REMOVE_ADULT, BR_PROXY, GIST_TOKEN, GIST_ID,
  UNMATCHED_TVG_ID (name), AGGREGATE_GROUP_PATTERN (COMPLETO|TODOS OS CANAIS|ALL CHANNELS|\\(SD,\\s*HD).
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import requests
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    from merge_epg import dedupe_display_names, name_key, normalizar_canal, strip_qualities_keep_case
except ImportError:  # merge_epg antigo (sem dedupe) ou ausente: usa versões locais equivalentes
    try:
        from merge_epg import name_key, normalizar_canal, strip_qualities_keep_case

        def dedupe_display_names(names_by_id):  # type: ignore[no-redef]
            return names_by_id
    except ImportError:
        def name_key(value: str) -> str:  # type: ignore[no-redef]
            s = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
            s = "".join(c for c in s if not unicodedata.combining(c)).upper()
            s = re.sub(r"\[[^\]]*\]|[¹²³⁴*]+", " ", s)
            s = re.sub(r"[()\-–—_./|,:;&]", " ", s)
            s = re.sub(r"\b(SD|HD|FHD|UHD|4K|8K|H\.?26[45]|HEVC|BR2)\b", " ", s)
            return " ".join(re.sub(r"[^A-Z0-9+ ]", " ", s).split())

        def normalizar_canal(value: str) -> str:  # type: ignore[no-redef]
            return name_key(value)

        def strip_qualities_keep_case(value: str) -> str:  # type: ignore[no-redef]
            return name_key(value)

        def dedupe_display_names(names_by_id):  # type: ignore[no-redef]
            return names_by_id

EPG_GZ_PATH = Path("epg.xml.gz")
REMOVE_ADULT = os.environ.get("REMOVE_ADULT", "1").strip().lower() in {"1", "true", "yes"}
UNMATCHED_TVG_ID = os.environ.get("UNMATCHED_TVG_ID", "name").strip().lower()
AGGREGATE_RE = re.compile(
    os.environ.get("AGGREGATE_GROUP_PATTERN", r"COMPLETO|TODOS OS CANAIS|ALL CHANNELS|\(SD,\s*HD"), re.IGNORECASE
)

BR_RESIDENTIAL_IPS = ["177.37.142.88", "179.218.44.19", "187.19.204.112", "189.40.92.201", "200.155.80.45", "201.17.110.73"]
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
_RE_ATTR = re.compile(r'([\w:.-]+)="([^"]*)"')
_RE_STREAM_ID_FROM_URL = re.compile(r"/(\d+)(?:\.[a-z0-9]+)?(?:\?.*)?$", re.IGNORECASE)
_RE_24H = re.compile(r"\b24\s*H(?:S|ORAS)?\b", re.IGNORECASE)


# ----------------------------------------------------------------------------- utilidades de rede
def norm_lookup_key(title: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(title or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s).split())


def build_br_headers(ua: str, host_url: str = "") -> dict[str, str]:
    fake_ip = random.choice(BR_RESIDENTIAL_IPS)
    headers = {
        "User-Agent": ua, "Accept": "*/*", "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "X-Forwarded-For": fake_ip, "X-Real-IP": fake_ip, "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip, "Client-IP": fake_ip, "Connection": "keep-alive",
    }
    if host_url:
        parsed = urllib.parse.urlparse(host_url)
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


def create_http_client() -> requests.Session:
    if cloudscraper is not None:
        session = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
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


def download_raw_m3u_to_disk(url: str, target_path: Path) -> bool:
    """Baixa a URL M3U original exata sem adulterar os parâmetros da query string."""
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    for ua in IPTV_USER_AGENTS:
        fake_ip = random.choice(BR_RESIDENTIAL_IPS)
        cmd = [
            "curl", "-sS", "-L", "--http1.1", "--max-time", "300", "--connect-timeout", "25", "--retry", "2",
            "-A", ua, "-H", "Accept: */*", "-H", "Accept-Language: pt-BR,pt;q=0.9,en-US;q=0.8",
            "-H", f"X-Forwarded-For: {fake_ip}", "-H", f"X-Real-IP: {fake_ip}", "-H", f"CF-Connecting-IP: {fake_ip}",
            "-o", str(target_path), url,
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


# ----------------------------------------------------------------------------- categorias (API Xtream)
def fetch_live_category_maps(url: str) -> tuple[dict[str, str], dict[str, str]]:
    """Devolve (por stream_id, por nome ÚNICO). Nome que existe em mais de uma categoria é descartado:
    antes o último vencia e variantes SD/HD/FHD de categorias diferentes colapsavam na mesma chave."""
    by_id: dict[str, str] = {}
    groups_by_name: dict[str, set[str]] = defaultdict(set)

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        print("  -> Aviso: URL sem username/password; categorias reais indisponíveis (grupos ficam como no original).")
        return by_id, {}

    user, pwd = query["username"][0], query["password"][0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    try:
        c_live = request_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_categories", timeout=30).json()
        live_cats = {
            str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
            for c in (c_live if isinstance(c_live, list) else []) if isinstance(c, dict) and c.get("category_name")
        }
        s_live = request_with_bypass(f"{base_url}/player_api.php?username={user}&password={pwd}&action=get_live_streams", timeout=45).json()
        for ch in (s_live if isinstance(s_live, list) else []):
            if not isinstance(ch, dict):
                continue
            cat_id = ch.get("category_id")
            if cat_id in (None, "") and isinstance(ch.get("category_ids"), list) and ch["category_ids"]:
                cat_id = ch["category_ids"][0]
            grp = live_cats.get(str(cat_id), "")
            if not grp:
                continue
            if ch.get("stream_id"):
                by_id[str(ch["stream_id"])] = grp
            if ch.get("name"):
                groups_by_name[norm_lookup_key(str(ch["name"]))].add(grp)
        by_name = {k: next(iter(v)) for k, v in groups_by_name.items() if len(v) == 1}
        print(f"  -> ✓ Categorias ao vivo: {len(live_cats)} grupos | {len(by_id)} por ID | {len(by_name)} por nome único "
              f"({len(groups_by_name) - len(by_name)} nomes ambíguos ignorados).")
        return by_id, by_name
    except Exception as e:
        print(f"  -> Nota: mapa de categorias indisponível ({e}); grupos ficam como no original.")
        return {}, {}


# ----------------------------------------------------------------------------- EPG
class EpgIndex:
    """nome -> id do EPG, só quando o nome aponta para UM canal (ambíguo não decide)."""

    def __init__(self, names_by_id: dict[str, list[str]]) -> None:
        self.ids = set(names_by_id)
        self.exact: dict[str, set[str]] = defaultdict(set)
        self.tokens: dict[frozenset, set[str]] = defaultdict(set)
        for cid, names in dedupe_display_names(names_by_id).items():
            for n in [cid, *names]:
                k = name_key(n)
                if k:
                    self.exact[k].add(cid)
                    t = frozenset(w for w in k.split() if w not in {"TV", "DE", "DO", "DA", "E"})
                    if t:
                        self.tokens[t].add(cid)

    def __len__(self) -> int:
        return len(self.exact)

    def resolve(self, candidates: list[str]) -> str | None:
        for cand in candidates:
            hit = self.exact.get(name_key(cand), set())
            if len(hit) == 1:
                return next(iter(hit))
        for cand in candidates:
            k = name_key(cand)
            t = frozenset(w for w in k.split() if w not in {"TV", "DE", "DO", "DA", "E"})
            hit = self.tokens.get(t, set()) if t else set()
            if len(hit) == 1:
                return next(iter(hit))
        return None


def load_epg_id_index(epg_path: Path) -> EpgIndex:
    names: dict[str, list[str]] = {}
    if not epg_path.exists():
        print(f"Aviso: {epg_path} não encontrado!")
        return EpgIndex(names)
    with gzip.open(epg_path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag.rsplit("}", 1)[-1] == "channel":
                cid = (elem.attrib.get("id") or "").strip()
                if cid:
                    names[cid] = [c.text.strip() for c in elem if c.tag.rsplit("}", 1)[-1] == "display-name" and c.text]
                elem.clear()
    idx = EpgIndex(names)
    print(f"✓ Índice EPG: {len(names)} canais, {len(idx)} nomes distintos.")
    return idx


def resolve_best_tvg_id(item_name: str, tvg_name: str, original_tvg_id: str, epg: EpgIndex) -> tuple[str, bool]:
    """(tvg-id final, casou_no_epg)."""
    shown = item_name or tvg_name
    if _RE_24H.search(shown):
        return "", False
    candidates: list[str] = []
    for c in (item_name, tvg_name, strip_qualities_keep_case(shown), normalizar_canal(shown)):
        c = html.unescape(c or "").strip()
        if c and c not in candidates:
            candidates.append(c)
    hit = epg.resolve(candidates)
    if hit:
        return hit, True
    if UNMATCHED_TVG_ID == "clear":
        return "", False
    if UNMATCHED_TVG_ID == "name":
        return strip_qualities_keep_case(shown) or original_tvg_id, False
    return original_tvg_id, False


# ----------------------------------------------------------------------------- parsing de linhas
def parse_extinf(line: str) -> tuple[dict[str, str], str]:
    """(atributos em minúsculas, nome exibido). O nome é tudo depois da 1ª vírgula APÓS o último atributo,
    então vírgulas no nome ('24HS Alf, O ETeimoso') ou no grupo ('(SD,HD,FHD,4K)') não quebram nada."""
    matches = list(_RE_ATTR.finditer(line))
    attrs = {m.group(1).lower(): m.group(2) for m in matches}
    start = matches[-1].end() if matches else 0
    comma = line.find(",", start)
    return attrs, (line[comma + 1:].strip() if comma != -1 else "")


def set_attr(extinf: str, key: str, value: str) -> str:
    safe = value.replace('"', "'")
    pat = re.compile(rf'(?<![\w.-]){re.escape(key)}="[^"]*"', re.IGNORECASE)
    if pat.search(extinf):
        return pat.sub(lambda _m: f'{key}="{safe}"', extinf, count=1)   # lambda: sem interpretar '\' do valor
    return re.sub(r"^(#EXTINF:[^\s,]+)", lambda m: f'{m.group(1)} {key}="{safe}"', extinf, count=1)


def is_vod_stream(stream_url: str) -> bool:
    path = stream_url.split("?", 1)[0].lower()
    return path.endswith(VOD_URL_EXTENSIONS) or any(seg in path for seg in ("/movie/", "/series/", "/vod/"))


@dataclass
class Stats:
    lidas: int = 0
    live: int = 0
    vod: int = 0
    adultos: int = 0
    duplicadas: int = 0
    orfas: int = 0            # #EXTINF sem URL ou URL sem #EXTINF (descartadas: inválidas)
    com_epg: int = 0
    regrupados: int = 0
    grupos_antes: Counter = field(default_factory=Counter)
    grupos_depois: Counter = field(default_factory=Counter)
    sem_epg: list[str] = field(default_factory=list)

    def conserva(self) -> bool:
        return self.lidas == self.live + self.vod + self.adultos + self.duplicadas


def iter_entries(lines: Iterator[str], stats: Stats) -> Iterator[tuple[list[str], str]]:
    """Agrupa (linhas '#...', URL). Qualquer linha que não comece com '#' é URL (udp://, rtp://, http...).
    Um #EXTINF novo sem URL para o anterior descarta o anterior em vez de misturar metadados."""
    pending: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.upper().startswith("#EXTM3U"):
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXTINF") and any(p.upper().startswith("#EXTINF") for p in pending):
                stats.orfas += 1
                pending = []
            pending.append(line)
            continue
        if any(p.upper().startswith("#EXTINF") for p in pending):
            yield pending, line
        else:
            stats.orfas += 1
        pending = []
    if any(p.upper().startswith("#EXTINF") for p in pending):
        stats.orfas += 1


def process_m3u_lines(lines: Iterator[str], epg: EpgIndex, live_by_id: dict[str, str], live_by_name: dict[str, str],
                      out) -> Stats:
    stats = Stats()
    seen: set[tuple[str, str]] = set()
    for pending, url_raw in iter_entries(lines, stats):
        url = url_raw.strip()
        stats.lidas += 1
        idx = next(i for i, p in enumerate(pending) if p.upper().startswith("#EXTINF"))
        extinf = pending[idx]
        attrs, item_name = parse_extinf(extinf)
        item_name = html.unescape(item_name or attrs.get("tvg-name", ""))
        curr_grp = attrs.get("group-title", "").strip()

        if REMOVE_ADULT and (ADULT_FILTER_RE.search(curr_grp) or ADULT_FILTER_RE.search(item_name)):
            stats.adultos += 1
            continue

        if is_vod_stream(url):
            stats.vod += 1
            out.write("\n".join(pending) + "\n" + url_raw + "\n")    # VOD: nada é alterado
            continue

        stream_id_m = _RE_STREAM_ID_FROM_URL.search(url.split("?", 1)[0])
        stream_id = stream_id_m.group(1) if stream_id_m else ""
        real_grp = live_by_id.get(stream_id, "") or live_by_name.get(norm_lookup_key(item_name), "")
        if REMOVE_ADULT and real_grp and ADULT_FILTER_RE.search(real_grp):
            stats.adultos += 1
            continue

        final_grp = curr_grp
        if real_grp and real_grp != curr_grp and (not curr_grp or AGGREGATE_RE.search(curr_grp)) \
                and not AGGREGATE_RE.search(real_grp):
            final_grp = real_grp
            extinf = set_attr(extinf, "group-title", real_grp)
            stats.regrupados += 1

        key = (url, final_grp)                  # mesmo canal duplicado no mesmo grupo (ex.: grupão -> categoria real)
        if key in seen:
            stats.duplicadas += 1
            continue
        seen.add(key)

        new_id, matched = resolve_best_tvg_id(item_name, attrs.get("tvg-name", ""), attrs.get("tvg-id", ""), epg)
        if new_id != attrs.get("tvg-id", ""):
            extinf = set_attr(extinf, "tvg-id", new_id)
        if matched:
            stats.com_epg += 1
        elif len(stats.sem_epg) < 400:
            stats.sem_epg.append(item_name)

        pending = [*pending[:idx], extinf, *pending[idx + 1:]]
        out.write("\n".join(pending) + "\n" + url_raw + "\n")
        stats.live += 1
        stats.grupos_antes[curr_grp or "(sem grupo)"] += 1
        stats.grupos_depois[final_grp or "(sem grupo)"] += 1
    return stats


def process_m3u(url: str, epg: EpgIndex, epg_public_url: str, out_file_path: Path) -> Stats:
    tmp_raw = tempfile.NamedTemporaryFile(delete=False, suffix=".raw.m3u")
    tmp_raw_path = Path(tmp_raw.name)
    tmp_raw.close()
    try:
        live_by_id, live_by_name = fetch_live_category_maps(url)
        print("  -> Baixando arquivo M3U original completo...")
        if not download_raw_m3u_to_disk(url, tmp_raw_path):
            raise RuntimeError("O servidor bloqueou o download do arquivo M3U a partir do IP do GitHub.")
        print(f"  -> ✓ M3U baixada ({tmp_raw_path.stat().st_size / (1024 * 1024):.2f} MB). Processando em passagem única...")

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"
        # surrogateescape: bytes inválidos em UTF-8 voltam idênticos (VOD "100% intocado" de verdade)
        with tmp_raw_path.open("r", encoding="utf-8", errors="surrogateescape", newline=None) as raw_in, \
                out_file_path.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as out:
            out.write(header)
            stats = process_m3u_lines(raw_in, epg, live_by_id, live_by_name, out)
        return stats
    finally:
        tmp_raw_path.unlink(missing_ok=True)


def print_report(i: int, stats: Stats, size_mb: float) -> None:
    print(f"  -> ✓ [lista_{i}.m3u] {stats.live} canais ({stats.com_epg} com EPG) + {stats.vod} VOD intactos | {size_mb:.2f} MB")
    print(f"     regrupados pela API: {stats.regrupados} | adultos removidos: {stats.adultos} | duplicadas removidas: {stats.duplicadas} | "
          f"entradas inválidas descartadas: {stats.orfas}")
    print(f"     conservação (lidas = ao vivo + VOD + adultos + duplicadas): {'OK' if stats.conserva() else '⚠ DIVERGENTE'} "
          f"({stats.lidas} = {stats.live + stats.vod + stats.adultos + stats.duplicadas})")
    print(f"     grupos ao vivo: antes {len(stats.grupos_antes)} -> depois {len(stats.grupos_depois)}")
    for g, n in stats.grupos_depois.most_common(12):
        print(f"       {n:5d}  {g}")
    if stats.sem_epg:
        print(f"     sem correspondência no EPG ({len(stats.sem_epg)}+): {', '.join(stats.sem_epg[:10])} ...")


# ----------------------------------------------------------------------------- gist
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
                print(f"  -> Nota: {fpath.name} tem {target_m3u.stat().st_size / (1024*1024):.1f} MB (>95MB). "
                      f"Enviando versão compactada {target_gz.name} ({target_gz.stat().st_size / (1024*1024):.1f} MB)!")
                target_m3u.unlink()
        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U (VODs 100% Originais + Canais com EPG)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "push", "--force", gist_repo_url, "HEAD:main"], cwd=work_dir, check=True)
        print(f"\n✓ Gist Secreto ({gist_id}) atualizado via Git Push com sucesso!")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    epg_public_url = os.environ.get("EPG_PUBLIC_URL", "").strip()
    epg = load_epg_id_index(EPG_GZ_PATH)

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
            stats = process_m3u(url, epg, epg_public_url, out_file)
            if stats.live == 0 and stats.vod == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue
            print_report(i, stats, out_file.stat().st_size / (1024 * 1024))
            if not stats.conserva():
                raise RuntimeError("Conservação divergente: lista NÃO publicada para não perder canais.")
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
