#!/usr/bin/env python3
"""EPG Universal Consolidado — Ultra Detalhado (Ano, Classificação, Gênero, TxxExx e Web Fallback)
- Formato Filmes: "2015 Livre Comédia/Família. Sinopse..."
- Formato Séries: "2022 14 Anos Drama/Crime. T02 E05 - Subtítulo. Sinopse..."
- Consulta automática na Wikipedia PT / iTunes BR API com cache persistente (metadata_cache.json).
- Compatibilidade total com as chaves reais do channels.json (old_tvg_ids, groups, blank_tvg_id_names).
"""
from __future__ import annotations

import copy
import glob
import gzip
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from statistics import mean
from xml.sax.saxutils import quoteattr
from typing import Iterable, Any

try:
    from epg_utils import digit_signature, normalize_name as original_normalize_name, normalize_source_id
except ImportError:
    print("epg_utils.py não encontrado. Baixando do repositório base...")
    urllib.request.urlretrieve("https://raw.githubusercontent.com/Limaalef/M3uEpg/main/epg_utils.py", "epg_utils.py")
    from epg_utils import digit_signature, normalize_name as original_normalize_name, normalize_source_id

CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
ESPELHO_PATH = Path("espelho.json")
METADATA_CACHE_PATH = Path("metadata_cache.json")

M3U_URLS = []
for i in range(1, 6):
    url = os.environ.get(f"M3U_URL_{i}")
    if url and url.startswith("http"):
        M3U_URLS.append(url.strip())

FUZZY_THRESHOLD = float(os.environ.get("FUZZY_THRESHOLD", "0.82"))
FUZZY_MARGIN = float(os.environ.get("FUZZY_MARGIN", "0.03"))
AGREEMENT_THRESHOLD = float(os.environ.get("AGREEMENT_THRESHOLD", "0.55"))
MIN_COMPARABLE_PROGRAMMES = int(os.environ.get("MIN_COMPARABLE_PROGRAMMES", "3"))

DOWNLOAD_WORKERS = max(1, min(4, int(os.environ.get("DOWNLOAD_WORKERS", "4"))))
GZIP_LEVEL = max(1, min(9, int(os.environ.get("GZIP_LEVEL", "9"))))
OUTPUT_PAST_HOURS = max(0, int(os.environ.get("OUTPUT_PAST_HOURS", "6")))
OUTPUT_FUTURE_HOURS = max(24, int(os.environ.get("OUTPUT_FUTURE_HOURS", "72")))
WRITE_PLAIN_XML = os.environ.get("WRITE_PLAIN_XML", "0").strip().lower() in {"1", "true", "yes"}
MAX_WEB_LOOKUPS_PER_RUN = int(os.environ.get("MAX_WEB_LOOKUPS_PER_RUN", "180"))

IPTV_USER_AGENTS = [
    "TiviMate/4.7.0 (Android 12)",
    "IPTVSmartersPlayer",
    "VLC/3.0.18 LibVLC/3.0.18",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]

EPG_SOURCES_CONFIG = [
    ("GuiaDeTV", "epg_guiadetv.xml", "guiadetv", 2.5),
    ("Meu Guia", "epg_meuguia.xml", "meuguia", 2.5),
    ("MiTV", "epg_mitv.xml", "mitv", 2.0),
    ("TV Plus", "epg_tvplus.xml", "tvplus", 1.8),
    ("Open-EPG BR", "https://www.open-epg.com/files/brazil.xml", "openepg", 1.5),
    ("Open-EPG BR 4", "https://www.open-epg.com/files/brazil4.xml", "openepg", 1.4),
    ("Claro", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/claro.xml", "git_operadora", 1.5),
    ("VivoPlay", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/vivoplay.xml", "git_operadora", 1.4),
    ("Globo", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/globo.xml", "git_extra", 1.2),
    ("Mais SBT", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/maissbt.xml", "git_extra", 1.0),
    ("XSports", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/xsports.xml", "git_extra", 1.0),
    ("EPGShare BR1", "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz", "epgshare", 1.3),
    ("EPGShare BR2", "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz", "epgshare", 1.3),
    ("IPTV-EPG BR", "https://iptv-epg.org/files/epg-br.xml", "iptvepg", 1.2),
    ("TV Map", "epg_tvmap.xml", "tvmap", 1.0),
    ("Genius", os.environ.get("PRIMARY_EPG_URL", "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz"), "genius", 1.0),
    ("EPG pw BR", "https://epg.pw/xmltv/epg_BR.xml", "epgpw", 0.8),
]

PLACEHOLDER_TITLES = {
    "",
    "SEM PROGRAMACAO",
    "SEM PROGRAMAÇÃO",
    "PROGRAMACAO NAO DISPONIVEL",
    "PROGRAMAÇÃO NÃO DISPONÍVEL",
    "NAO DISPONIVEL",
    "NÃO DISPONÍVEL",
    "A DEFINIR",
    "TBA",
    "TO BE ANNOUNCED",
    "NO INFORMATION",
    "OFF AIR",
    "ENCERRAMENTO",
}

NON_SERIES_CHANNEL_KEYWORDS = (
    "TELECINE",
    "TC ACTION",
    "TC PREMIUM",
    "TC PIPOCA",
    "TC TOUCH",
    "TC FUN",
    "TC CULT",
    "MEGAPIX",
    "CINEMAX",
    "PARAMOUNT",
    "STUDIO UNIVERSAL",
    "UNIVERSAL STUDIO",
    "PRIME BOX",
    "CINEBRASIL",
    "CURTA",
    "SPORTV",
    "ESPN",
    "PREMIERE",
    "COMBATE",
    "BANDSPORTS",
    "BAND SPORTS",
    "FOX SPORTS",
    "TNT SPORTS",
    "CAZETV",
    "XSPORTS",
    "GLOBO NEWS",
    "GLOBONEWS",
    "GB NEWS",
    "CNN",
    "JOVEM PAN",
    "JOVEN PAN",
    "JP NEWS",
    "RECORD NEWS",
    "BAND NEWS",
    "BANDNEWS",
    "SBT NEWS",
    "CNBC",
    "BLOOMBERG",
    "BM&C",
    "TV SENADO",
    "TV CAMARA",
    "TV JUSTICA",
    "AGROMAIS",
    "CANAL RURAL",
    "CANAL DO BOI",
    "TERRA VIVA",
)

SPORT_OR_NEWS_CHANNEL_KEYWORDS = (
    "SPORTV",
    "ESPN",
    "PREMIERE",
    "COMBATE",
    "BANDSPORTS",
    "BAND SPORTS",
    "FOX SPORTS",
    "TNT SPORTS",
    "CAZETV",
    "XSPORTS",
    "DAZN",
    "GOAT",
    "SPORTYNET",
    "UFC",
    "CONMEBOL",
    "GLOBO NEWS",
    "GLOBONEWS",
    "GB NEWS",
    "CNN",
    "JOVEM PAN",
    "JOVEN PAN",
    "JP NEWS",
    "RECORD NEWS",
    "BAND NEWS",
    "BANDNEWS",
    "SBT NEWS",
    "CNBC",
    "BLOOMBERG",
    "TV SENADO",
    "TV CAMARA",
    "AGROMAIS",
    "CANAL RURAL",
    "CANAL DO BOI",
)

NON_SERIES_CATEGORY_KEYWORDS = (
    "FILME",
    "MOVIE",
    "CINEMA",
    "LONGA",
    "FEATURE FILM",
    "ESPORTE",
    "SPORT",
    "FUTEBOL",
    "SOCCER",
    "LUTAS",
    "BOXE",
    "JORNALISMO",
    "NOTICIAS",
    "NOTÍCIAS",
    "NEWS",
    "TELEJORNAL",
)

SERIES_CATEGORY_KEYWORDS = (
    "SERIE",
    "SÉRIE",
    "SERIES",
    "SITCOM",
    "NOVELA",
    "TELENOVELA",
    "MINISSERIE",
    "MINISSÉRIE",
    "EPISODIO",
    "EPISÓDIO",
    "ANIME",
    "DESENHO",
    "ANIMACAO",
    "ANIMAÇÃO",
    "INFANTIL",
)

# Tradução e padronização de gêneros em inglês/português para exibição limpa
GENRE_TRANSLATION = {
    "ACTION": "Ação",
    "ACAO": "Ação",
    "AÇÃO": "Ação",
    "ADVENTURE": "Aventura",
    "AVENTURA": "Aventura",
    "ANIMATION": "Animação",
    "ANIMACAO": "Animação",
    "ANIMAÇÃO": "Animação",
    "COMEDY": "Comédia",
    "COMEDIA": "Comédia",
    "COMÉDIA": "Comédia",
    "CRIME": "Crime",
    "POLICIAL": "Policial",
    "DOCUMENTARY": "Documentário",
    "DOCUMENTARIO": "Documentário",
    "DOCUMENTÁRIO": "Documentário",
    "DRAMA": "Drama",
    "FAMILY": "Família",
    "FAMILIA": "Família",
    "FAMÍLIA": "Família",
    "FANTASY": "Fantasia",
    "FANTASIA": "Fantasia",
    "HISTORY": "História",
    "HISTORIA": "História",
    "HISTÓRIA": "História",
    "HORROR": "Terror",
    "TERROR": "Terror",
    "MUSIC": "Música",
    "MUSICA": "Música",
    "MÚSICA": "Música",
    "MUSICAL": "Musical",
    "MYSTERY": "Mistério",
    "MISTERIO": "Mistério",
    "MISTÉRIO": "Mistério",
    "ROMANCE": "Romance",
    "SCI-FI": "Ficção Científica",
    "SCIENCE FICTION": "Ficção Científica",
    "FICCAO CIENTIFICA": "Ficção Científica",
    "FICÇÃO CIENTÍFICA": "Ficção Científica",
    "FICCAO": "Ficção",
    "FICÇÃO": "Ficção",
    "THRILLER": "Suspense",
    "SUSPENSE": "Suspense",
    "WAR": "Guerra",
    "GUERRA": "Guerra",
    "WESTERN": "Faroeste",
    "FAROESTE": "Faroeste",
    "KIDS": "Infantil",
    "CHILDREN": "Infantil",
    "INFANTIL": "Infantil",
    "REALITY": "Reality",
    "REALITY-TV": "Reality",
    "REALITY SHOW": "Reality",
    "CULINARIA": "Culinária",
    "CULINÁRIA": "Culinária",
    "GASTRONOMIA": "Culinária",
    "BIOGRAPHY": "Biografia",
    "BIOGRAFIA": "Biografia",
    "SPORTS": "Esporte",
    "SPORT": "Esporte",
    "ESPORTE": "Esporte",
    "ESPORTES": "Esporte",
    "FUTEBOL": "Futebol",
    "NEWS": "Jornalismo",
    "JORNALISMO": "Jornalismo",
    "NOTICIAS": "Jornalismo",
    "NOTÍCIAS": "Jornalismo",
    "VARIETY": "Variedades",
    "VARIEDADES": "Variedades",
    "TALK SHOW": "Talk Show",
    "NOVELA": "Novela",
    "TELENOVELA": "Novela",
}

IGNORED_GENERIC_CATEGORIES = {
    "FILME", "FILMES", "MOVIE", "MOVIES", "SERIE", "SÉRIE", "SERIES", "SÉRIES",
    "TV SHOW", "SHOW", "PROGRAMA", "OUTROS", "OTHER", "GENERAL", "GERAL", "HD", "FHD",
    "2024", "2025", "2026", "CANAL", "AO VIVO", "LIVE",
}

@dataclass(frozen=True)
class SourceSpec:
    key: str; label: str; url: str; priority: int; family: str; vote_weight: float
    default_offset: str = "+0000"; timeshift_hours: float = 0.0

SOURCE_SPECS = []
for idx, (lbl, link, family, weight) in enumerate(EPG_SOURCES_CONFIG, start=1):
    safe_key = re.sub(r'[^a-z0-9]', '', lbl.lower()) or f"src{idx}"
    SOURCE_SPECS.append(SourceSpec(key=safe_key, label=lbl, url=link, priority=idx, family=family, vote_weight=weight))

@dataclass(frozen=True)
class ProgrammeRow:
    start: datetime; stop: datetime; title: str; normalized_title: str; element: ET.Element

@dataclass
class EpgSource:
    spec: SourceSpec; channels: dict[str, ET.Element]; names: dict[str, list[str]]
    normalized_names: dict[str, list[str]]; programs: dict[str, list[ET.Element]]
    rows: dict[str, list[ProgrammeRow]]; exact_index: dict[str, set[str]]
    fuzzy_index: dict[tuple[str, ...], dict[str, set[str]]]; normalized_id_index: dict[str, set[str]]
    @property
    def label(self) -> str: return self.spec.label

@dataclass
class ChannelMatch:
    source: EpgSource; channel_id: str; method: str; match_score: float

@dataclass
class CandidateEvaluation:
    match: ChannelMatch; support_votes: int; vote_sources: int; support_weight: float; vote_weight: float
    support_families: int; weighted_ratio: float; avg_support_agreement: float; health_score: float
    richness_score: float; health: dict[str, object]; confidence: int; comparisons: list[dict[str, object]]

CHANNEL_SYNONYMS = {
    "UNIVERSAL CHANNEL": ["UNIVERSAL TV", "UNIVERSAL"],
    "UNIVERSAL TV": ["UNIVERSAL CHANNEL", "UNIVERSAL"],
    "UNIVERSAL STUDIO": ["STUDIO UNIVERSAL"],
    "STUDIO UNIVERSAL": ["UNIVERSAL STUDIO"],
    "WARNER": ["WARNER TV", "WARNER CHANNEL"],
    "WARNER TV": ["WARNER", "WARNER CHANNEL"],
    "WARNER CHANNEL": ["WARNER TV", "WARNER"],
    "SONY CHANNEL": ["SONY", "SONY ENTERTAINMENT TELEVISION"],
    "SONY": ["SONY CHANNEL"],
    "USA": ["USA NETWORK", "SYFY"],
    "USA NETWORK": ["USA", "SYFY"],
    "SYFY": ["USA", "USA NETWORK"],
    "TNT SERIES": ["TNT SERIES", "TNT SÉRIES", "TNT SERIE"],
    "AGROMAIS": ["AGRO+", "AGRO MAIS"],
    "AGRO+": ["AGROMAIS", "AGRO MAIS"],
    "REDE TV": ["REDETV!", "REDETV"],
    "REDETV": ["REDE TV", "REDETV!"],
    "TV GAZETA SP": ["TV GAZETA", "GAZETA"],
    "TV GAZETA": ["TV GAZETA SP", "GAZETA"],
    "TV PAMPA RS": ["TV PAMPA", "PAMPA"],
    "TV PAMPA": ["TV PAMPA RS", "PAMPA"],
    "CNN INTERNACIONAL": ["CNN INTERNATIONAL"],
    "CNN INTERNATIONAL": ["CNN INTERNACIONAL"],
    "JOVEM PAN": ["JOVEM PAN NEWS", "JP NEWS", "JOVEN PAN NEWS"],
    "JOVEM PAN NEWS": ["JOVEM PAN", "JP NEWS", "JOVEN PAN NEWS"],
    "JOVEN PAN NEWS": ["JOVEM PAN NEWS", "JOVEM PAN", "JP NEWS"],
    "PRIME BOX BRAZIL": ["PRIME BOX BRASIL"],
    "PRIME BOX BRASIL": ["PRIME BOX BRAZIL"],
    "CARTOONITO": ["CARTOONITO", "BOOMERANG"],
    "SPORTTV": ["SPORTV"],
    "SPORTV": ["SPORTTV"],
    "SPORTTV 2": ["SPORTV 2"],
    "SPORTV 2": ["SPORTTV 2"],
    "SPORTTV 3": ["SPORTV 3"],
    "SPORTV 3": ["SPORTTV 3"],
    "GLOBOPLAY NOVELAS": ["VIVA", "CANAL VIVA"],
    "VIVA": ["GLOBOPLAY NOVELAS", "CANAL VIVA"],
}

def strip_qualities_keep_case(name: str) -> str:
    if not name: return ""
    s = re.sub(r'\[.*?\]|\(.*?\)|\|.*?\|', ' ', name)
    s = re.sub(r'[\¹\²\³\⁴]', '', s)
    tags = r'\b(SD|HD|FHD|UHD|4K|8K|H\.?264|H\.?265|HEVC|HDR\+?|LEG|LEGENDADO|DUB|DUBLADO|HQ|VIP|VOD|24H[S]?|24/7|1080P|720P|60FPS|PPV|EVENTOS?|ALTERNATIVO|BACKUP|ALT)\b[\*\²\³\¹\⁴\+]*'
    s = re.sub(tags, ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'[*=_\-]', ' ', s)
    return ' '.join(s.split()).strip()

def normalizar_canal(nome: str) -> str:
    if not nome: return ""
    original_nome = nome
    nome = strip_qualities_keep_case(nome)
    
    nome = re.sub(r'\bJP NEWS\b', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bJOVEN PAN\b', 'JOVEM PAN', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bJOVEM PAN\b(?! NEWS)', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX SPORTS\b', 'ESPN 4', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bBOOMERANG\b', 'CARTOONITO', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bMAX PRIME\b', 'HBO POP', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX PREMIUM\b', 'STAR PREMIUM', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX CHANNEL\b', 'STAR CHANNEL', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bFOX LIFE\b', 'STAR LIFE', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+ACTION\b', 'TELECINE ACTION', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+PREMIUM\b', 'TELECINE PREMIUM', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+PIPOCA\b', 'TELECINE PIPOCA', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+TOUCH\b', 'TELECINE TOUCH', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+FUN\b', 'TELECINE FUN', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bTC\s+CULT\b', 'TELECINE CULT', nome, flags=re.IGNORECASE)
    nome = re.sub(r'^GB\b', 'GLOBO', nome, flags=re.IGNORECASE)
    nome = re.sub(r'^RECORDTV\b', 'RECORD', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bREDE\s+TV!?\b', 'REDETV', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bAGRO\s*\+\b', 'AGROMAIS', nome, flags=re.IGNORECASE)
    nome = re.sub(r'\bSPORTTV\b', 'SPORTV', nome, flags=re.IGNORECASE)
    
    nome_limpo = ' '.join(nome.split()).strip().upper()
    return nome_limpo if len(nome_limpo) > 1 else original_normalize_name(original_nome)

def normalize_name(value: str) -> str:
    return original_normalize_name(normalizar_canal(value))

def inferir_emissora_pelo_grupo(nome_normalizado: str, nome_grupo: str) -> str:
    if not nome_grupo: return nome_normalizado
    grupo = nome_grupo.upper()
    if "GLOBO" in grupo and not nome_normalizado.startswith("GLOBO") and " NEWS" not in nome_normalizado:
        return f"GLOBO {nome_normalizado}"
    if "SBT" in grupo and not nome_normalizado.startswith("SBT"): return f"SBT {nome_normalizado}"
    if "RECORD" in grupo and not nome_normalizado.startswith("RECORD"): return f"RECORD {nome_normalizado}"
    if "BAND" in grupo and not nome_normalizado.startswith("BAND"): return f"BAND {nome_normalizado}"
    if "PREMIERE" in grupo and not nome_normalizado.startswith("PREMIERE") and len(nome_normalizado) <= 4: return f"PREMIERE {nome_normalizado}"
    return nome_normalizado

def is_valid_provider_tvid(channel_name: str, tvid: str) -> bool:
    if not tvid or len(tvid) < 2: return False
    tvid_clean = re.sub(r'\.(br|us|uk|pt|ar|mx|cl|co|pe|latam|int|bt)$', '', tvid.lower())
    tvid_clean = re.sub(r'^(auto\.|yt\.|br\.|canais\.)', '', tvid_clean)
    tvid_clean = re.sub(r'[^a-z0-9]', '', tvid_clean)
    if not tvid_clean: return False

    base_clean = normalizar_canal(channel_name)
    candidates = [base_clean] + CHANNEL_SYNONYMS.get(base_clean, [])
    
    for cand in candidates:
        cand_alnum = re.sub(r'[^a-z0-9]', '', normalize_name(cand).lower())
        if not cand_alnum: continue
        cand_digits = re.findall(r'\d+', cand_alnum)
        tvid_digits = re.findall(r'\d+', tvid_clean)
        if cand_digits != tvid_digits:
            continue
        if tvid_clean in cand_alnum or cand_alnum in tvid_clean:
            return True
        if SequenceMatcher(None, tvid_clean, cand_alnum).ratio() >= 0.65:
            return True
    return False

def is_meaningful_programme(norm_title: str) -> bool:
    if not norm_title or len(norm_title) < 2:
        return False
    if norm_title in PLACEHOLDER_TITLES:
        return False
    for bad in ("SEM PROGRAMACAO", "NAO DISPONIVEL", "PROGRAMACAO INDISPONIVEL"):
        if bad in norm_title:
            return False
    return True

def local_tag(element: ET.Element) -> str: return element.tag.rsplit("}", 1)[-1]
def format_xmltv_datetime(dt: datetime) -> str: return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")

@lru_cache(maxsize=500000)
def parse_xmltv_datetime(value: str, default_offset: str = "+0000") -> datetime | None:
    if not value: return None
    value = value.strip()
    match = re.match(r"^(\d{14}|\d{12})(?:\s*([+-]\d{4}))?(?:\s+.*)?$", value)
    if match:
        digits, offset = match.groups()
        try:
            second = int(digits[12:14]) if len(digits) == 14 else 0
            if not offset: offset = default_offset or "+0000"
            sign = 1 if offset[0] == "+" else -1
            delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
            tz = timezone(sign * delta)
            dt = datetime(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10]), int(digits[10:12]), second, tzinfo=tz)
            return dt.astimezone(timezone.utc)
        except (ValueError, IndexError): pass
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z"):
        try: return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError: pass
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            sign = 1 if default_offset[0] == "+" else -1
            delta = timedelta(hours=int(default_offset[1:3]), minutes=int(default_offset[3:5]))
            tz = timezone(sign * delta)
            return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except (ValueError, IndexError): pass
    return None

def fetch_iptv_json(url: str, timeout: int = 45) -> Any:
    last_err = None
    for ua in IPTV_USER_AGENTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as exc:
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"Todos os User-Agents falharam: {last_err}")

def download_to_temp_file(url: str, attempts: int = 4) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    tmp_path = Path(tmp.name)
    tmp.close()
    last_error = None
    for attempt in range(1, attempts + 1):
        ua = IPTV_USER_AGENTS[(attempt - 1) % len(IPTV_USER_AGENTS)]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp_path.open("wb") as out:
                while chunk := resp.read(1024 * 512): out.write(chunk)
            return tmp_path
        except Exception as exc:
            last_error = exc
            if attempt < attempts: time.sleep(attempt * 2) 
    if tmp_path.exists(): tmp_path.unlink()
    raise RuntimeError(f"Falha ao baixar ({last_error})")

# =====================================================================
# EXTRAÇÃO DE METADADOS + CACHE + FALLBACK WEB (WIKIPEDIA PT & ITUNES BR)
# =====================================================================
_METADATA_CACHE: dict[str, dict[str, str]] = {}
_WEB_LOOKUPS_COUNT = 0

def load_metadata_cache() -> None:
    global _METADATA_CACHE
    if METADATA_CACHE_PATH.exists():
        try:
            _METADATA_CACHE = json.loads(METADATA_CACHE_PATH.read_text("utf-8"))
        except Exception:
            _METADATA_CACHE = {}

def save_metadata_cache() -> None:
    try:
        METADATA_CACHE_PATH.write_text(json.dumps(_METADATA_CACHE, ensure_ascii=False, indent=2) + "\n", "utf-8")
    except Exception:
        pass

_RE_XMLTV_NS = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*\.")
_RE_SE_PATTERNS = [
    re.compile(r"\bS(\d{1,2})\s*[:\-]?\s*E(?:P)?(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\bT(?:EMP(?:ORADA)?)?\s*\.?\s*(\d{1,2})\s*[\-:/, ]+\s*E(?:P(?:IS[OÓ]DIO)?)?\s*\.?\s*(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})\s*[ªº]\s*T(?:EMP(?:ORADA)?)?\s*[\-:/, ]*\s*E(?:P(?:IS[OÓ]DIO)?)?\s*\.?\s*(\d{1,3})\b", re.IGNORECASE),
]
_RE_EXPLICIT_EPISODE = re.compile(r"\b(?:EPIS[OÓ]DIO|EP\.)\s*(\d{1,3})\b", re.IGNORECASE)
_RE_TRAILING_FAKE_EP = re.compile(r"(?:\s*[\-\|:]\s*|\s+)(?:\[|\()?E(?:P)?\.?\s*0*1(?:\]|\))?\s*$", re.IGNORECASE)
_RE_YEAR_IN_TEXT = re.compile(r"(?:[\(\[\-\|]\s*|\b(?:ANO|LANCAMENTO|LANÇAMENTO)\s*[:\-]?\s*)((?:19[3-9]\d|20[0-2]\d))(?:\s*[\)\]\-\|]|\b)", re.IGNORECASE)
_RE_RATING_IN_TEXT = re.compile(
    r"(?:\bCLASSIFICA[CÇ][AÃ]O(?:\s+INDICATIVA)?\s*[:\-]?\s*|\[\s*|\(\s*)(LIVRE|L|10|12|14|16|18|A10|A12|A14|A16|A18)(?:\s*ANOS?)?(?:\s*\]|\s*\)|\b)",
    re.IGNORECASE,
)

def normalize_rating_label(raw_rating: str) -> str:
    """Padroniza a classificação indicativa (ex: 'Livre', '10 Anos', '12 Anos', '14 Anos', '16 Anos', '18 Anos')."""
    if not raw_rating:
        return ""
    r = raw_rating.strip().upper()
    if r in {"L", "LIVRE", "AL", "ALL", "G", "TV-G", "TV-Y", "TV-Y7", "0", "PARA TODOS"} or "LIVRE" in r:
        return "Livre"
    if r in {"PG", "10", "A10", "+10", "10 ANOS"} or re.search(r"\b10\b", r):
        return "10 Anos"
    if r in {"PG-13", "TV-PG", "12", "A12", "+12", "12 ANOS"} or re.search(r"\b12\b", r):
        return "12 Anos"
    if r in {"TV-14", "14", "A14", "+14", "14 ANOS"} or re.search(r"\b14\b", r):
        return "14 Anos"
    if r in {"R", "16", "A16", "+16", "16 ANOS"} or re.search(r"\b16\b", r):
        return "16 Anos"
    if r in {"NC-17", "TV-MA", "18", "A18", "+18", "18 ANOS"} or re.search(r"\b18\b", r):
        return "18 Anos"
    return ""

def normalize_genres_list(raw_categories: Iterable[str]) -> str:
    """Limpa, traduz e formata até 2 gêneros no padrão 'Comédia/Família'."""
    cleaned: list[str] = []
    for raw in raw_categories:
        if not raw:
            continue
        for part in re.split(r"[/,|•\-]+", str(raw)):
            p = part.strip()
            if not p:
                continue
            p_up = p.upper()
            if p_up in IGNORED_GENERIC_CATEGORIES or len(p_up) <= 2:
                continue
            mapped = GENRE_TRANSLATION.get(p_up)
            if not mapped:
                # Tenta sem acentos
                p_no_acc = "".join(c for c in unicodedata.normalize("NFKD", p_up) if not unicodedata.combining(c))
                mapped = GENRE_TRANSLATION.get(p_no_acc, p.title())
            if mapped and mapped.upper() not in IGNORED_GENERIC_CATEGORIES and mapped not in cleaned:
                cleaned.append(mapped)
            if len(cleaned) >= 2:
                break
        if len(cleaned) >= 2:
            break
    return "/".join(cleaned[:2])

def is_non_series_channel(channel_name: str) -> bool:
    norm_ch = normalizar_canal(channel_name)
    return any(kw in norm_ch for kw in NON_SERIES_CHANNEL_KEYWORDS)

def is_sport_or_news_channel(channel_name: str) -> bool:
    norm_ch = normalizar_canal(channel_name)
    return any(kw in norm_ch for kw in SPORT_OR_NEWS_CHANNEL_KEYWORDS)

def is_likely_movie_or_sport(categories: Iterable[str], desc: str, duration_minutes: float, channel_name: str) -> bool:
    if is_non_series_channel(channel_name):
        return True

    cats_upper = " | ".join(c.upper() for c in categories if c)
    desc_upper = (desc or "").upper()

    if any(skw in cats_upper for skw in SERIES_CATEGORY_KEYWORDS):
        return False

    if any(mkw in cats_upper for mkw in NON_SERIES_CATEGORY_KEYWORDS):
        return True

    if duration_minutes >= 85:
        if any(w in desc_upper for w in ("DIREÇÃO:", "DIRECAO:", "SINOPSE:", "LONGA-METRAGEM", "FILME DE ")):
            return True
        if not any(w in desc_upper for w in ("TEMPORADA", "EPISÓDIO", "EPISODIO", "SÉRIE", "SERIE", "CAPÍTULO", "CAPITULO")):
            return True

    return False

def query_web_metadata(clean_title: str, is_movie: bool) -> dict[str, str]:
    """Consulta iTunes Search API (BR) e Wikipedia PT API para obter Ano, Classificação, Gênero e Sinopse."""
    global _WEB_LOOKUPS_COUNT
    cache_key = f"{'M' if is_movie else 'S'}:{normalize_programme_title(clean_title)}"
    if not clean_title or len(clean_title) < 2:
        return {}
    if cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]
    if _WEB_LOOKUPS_COUNT >= MAX_WEB_LOOKUPS_PER_RUN:
        return {}

    _WEB_LOOKUPS_COUNT += 1
    found = {"year": "", "rating": "", "genre": "", "desc": ""}

    # 1. Consulta rápida na API aberta do iTunes / Apple TV Brasil (Retorna Ano, Classificação BR, Gênero em PT e Sinopse)
    try:
        entity = "movie" if is_movie else "tvSeason"
        q = urllib.parse.quote(clean_title)
        itunes_url = f"https://itunes.apple.com/search?term={q}&country=BR&language=pt_br&entity={entity}&limit=3"
        req = urllib.request.Request(itunes_url, headers={"User-Agent": IPTV_USER_AGENTS[-1]})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            results = data.get("results", [])
            norm_target = normalize_programme_title(clean_title)
            for item in results:
                cand_name = item.get("trackName") or item.get("collectionName") or ""
                norm_cand = normalize_programme_title(cand_name)
                if not norm_cand:
                    continue
                if norm_target == norm_cand or norm_target in norm_cand or SequenceMatcher(None, norm_target, norm_cand).ratio() >= 0.78:
                    rel_date = str(item.get("releaseDate", ""))
                    if len(rel_date) >= 4 and rel_date[:4].isdigit():
                        found["year"] = rel_date[:4]
                    rating_raw = str(item.get("contentAdvisoryRating", ""))
                    if rating_raw:
                        found["rating"] = normalize_rating_label(rating_raw)
                    genre_raw = str(item.get("primaryGenreName", ""))
                    if genre_raw:
                        found["genre"] = normalize_genres_list([genre_raw])
                    long_desc = str(item.get("longDescription") or item.get("shortDescription") or "").strip()
                    if long_desc and len(long_desc) > 25:
                        found["desc"] = long_desc
                    break
    except Exception:
        pass

    # 2. Fallback na API da Wikipedia em Português caso ainda falte Ano ou Gênero
    if not found["year"] or not found["genre"]:
        try:
            q_wiki = urllib.parse.quote(f"{clean_title} {'filme' if is_movie else 'série'}")
            wiki_url = f"https://pt.wikipedia.org/w/api.php?action=query&list=search&srsearch={q_wiki}&utf8=&format=json&srlimit=2"
            req = urllib.request.Request(wiki_url, headers={"User-Agent": "EPGUniversalBot/1.0"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                wdata = json.loads(resp.read().decode("utf-8", errors="ignore"))
                search_hits = wdata.get("query", {}).get("search", [])
                if search_hits:
                    snippet = re.sub(r"<.*?>", " ", search_hits[0].get("snippet", ""))
                    if not found["year"]:
                        ym = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", snippet)
                        if ym:
                            found["year"] = ym.group(1)
                    if not found["genre"]:
                        found_genres = []
                        for kw, pt_g in GENRE_TRANSLATION.items():
                            if len(kw) >= 5 and re.search(rf"\b{re.escape(kw)}\b", snippet, re.IGNORECASE):
                                if pt_g not in found_genres:
                                    found_genres.append(pt_g)
                            if len(found_genres) >= 2:
                                break
                        if found_genres:
                            found["genre"] = "/".join(found_genres[:2])
        except Exception:
            pass

    _METADATA_CACHE[cache_key] = found
    return found

def clean_redundant_title_subtitle(title: str, sub_title: str) -> tuple[str, str]:
    """Remove subtítulos repetidos como 'Boca Juniors x Defensa y Justicia - Campeonato Argentino : Boca Juniors x Defensa y Justicia'."""
    t = title.strip()
    s = sub_title.strip()
    if " : " in t and not s:
        parts = [p.strip() for p in t.split(" : ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            t, s = parts[0], parts[1]

    if s:
        norm_t = normalize_programme_title(t)
        norm_s = normalize_programme_title(s)
        if not norm_s or norm_s == norm_t or norm_s in norm_t:
            s = ""
        elif norm_t in norm_s and len(s) > len(t):
            # Se o subtítulo for mais completo que o título e já contiver o título
            s = ""
    return t, s

def extract_program_metadata(el: ET.Element, channel_name: str = "", duration_minutes: float = 0.0) -> dict[str, Any]:
    """Extrai Título, Subtítulo, Sinopse, Ano, Classificação Indicativa, Gênero, Temporada e Episódio."""
    title = ""
    sub_title = ""
    desc = ""
    date_str = ""
    rating_str = ""
    categories: list[str] = []
    season: int | None = None
    episode: int | None = None

    for child in el:
        tag = local_tag(child)
        txt = (child.text or "").strip()
        if tag == "rating":
            val_node = child.find("value")
            r_val = (val_node.text or "").strip() if val_node is not None else txt
            if r_val and not rating_str:
                rating_str = normalize_rating_label(r_val)
            continue
        if not txt:
            continue
        if tag == "title" and not title:
            title = txt
        elif tag == "sub-title" and not sub_title:
            sub_title = txt
        elif tag == "desc" and len(txt) > len(desc):
            desc = txt
        elif tag == "date" and not date_str:
            m_yr = re.search(r"\b(19\d\d|20\d\d)\b", txt)
            if m_yr:
                date_str = m_yr.group(1)
        elif tag == "category" and txt not in categories:
            categories.append(txt)
        elif tag == "episode-num":
            sys_attr = (child.attrib.get("system") or "").lower()
            if sys_attr == "xmltv_ns":
                m = _RE_XMLTV_NS.match(txt)
                if m:
                    s_val = int(m.group(1)) + 1
                    e_val = int(m.group(2)) + 1
                    if not (s_val == 1 and e_val == 1 and "0.0." in txt):
                        season, episode = s_val, e_val
            else:
                for pat in _RE_SE_PATTERNS:
                    m = pat.search(txt)
                    if m:
                        season, episode = int(m.group(1)), int(m.group(2))
                        break

    title, sub_title = clean_redundant_title_subtitle(title, sub_title)
    title = _RE_TRAILING_FAKE_EP.sub("", title).strip()

    blob = f"{title} | {sub_title} | {desc}"

    # Extrai Ano do texto caso não haja tag <date>
    if not date_str:
        m_y = _RE_YEAR_IN_TEXT.search(blob)
        if m_y:
            date_str = m_y.group(1)
            # Remove o ano do final do título se estiver entre parênteses ex: "Minions (2015)" -> "Minions"
            title = re.sub(r"\s*[\(\[]\s*(?:19\d\d|20\d\d)\s*[\)\]]\s*$", "", title).strip()

    # Extrai Classificação Indicativa do texto caso não haja tag <rating>
    if not rating_str:
        m_r = _RE_RATING_IN_TEXT.search(blob)
        if m_r:
            rating_str = normalize_rating_label(m_r.group(1))

    # Extrai Temporada e Episódio via Regex estrita
    if season is None and episode is None:
        for pat in _RE_SE_PATTERNS:
            m = pat.search(blob)
            if m:
                s_val, e_val = int(m.group(1)), int(m.group(2))
                if 0 < s_val <= 60 and 0 < e_val <= 999:
                    season, episode = s_val, e_val
                    break
        if episode is None:
            m_ep = _RE_EXPLICIT_EPISODE.search(f"{sub_title} | {desc}")
            if m_ep:
                e_val = int(m_ep.group(1))
                if 0 < e_val <= 1500:
                    episode = e_val

    if is_likely_movie_or_sport(categories, desc, duration_minutes, channel_name):
        season = None
        episode = None

    for pat in _RE_SE_PATTERNS:
        title = pat.sub("", title).strip(" -:|")
        if sub_title:
            sub_title = pat.sub("", sub_title).strip(" -:|")

    if sub_title and (sub_title.upper() == title.upper() or _RE_TRAILING_FAKE_EP.fullmatch(sub_title)):
        sub_title = ""

    return {
        "title": title,
        "sub_title": sub_title,
        "desc": desc,
        "season": season,
        "episode": episode,
        "categories": categories,
        "date": date_str,
        "rating": rating_str,
    }

def read_source_from_file(spec: SourceSpec, file_path: Path) -> EpgSource:
    channels: dict[str, ET.Element] = {}
    names, normalized_names = {}, {}
    programs, rows = defaultdict(list), defaultdict(list)
    exact_index, fuzzy_index, normalized_id_index = defaultdict(set), defaultdict(lambda: defaultdict(set)), defaultdict(set)

    with file_path.open("rb") as f:
        magic = f.read(2)
        f.seek(0)
        file_obj = gzip.GzipFile(fileobj=f) if magic == b"\x1f\x8b" else f
        tree = ET.parse(file_obj)
        root = tree.getroot()

    for child in root:
        tag = local_tag(child)
        if tag == "channel":
            channel_id = child.attrib.get("id", "").strip()
            if not channel_id: continue
            channels[channel_id] = child
            display_names = [(node.text or "").strip() for node in child if local_tag(node) == "display-name" and (node.text or "").strip()]
            names[channel_id] = display_names
            norm_candidates: list[str] = []
            for candidate in [*display_names, channel_id, normalize_source_id(channel_id)]:
                normalized = normalize_name(candidate)
                if not normalized or normalized in norm_candidates: continue
                norm_candidates.append(normalized)
                exact_index[normalized].add(channel_id)
                fuzzy_index[digit_signature(normalized)][normalized].add(channel_id)
            normalized_names[channel_id] = norm_candidates
            normalized_id = normalize_source_id(channel_id)
            if normalized_id: normalized_id_index[normalized_id].add(channel_id)

        elif tag == "programme":
            channel_id = child.attrib.get("channel", "").strip()
            if not channel_id: continue
            start = parse_xmltv_datetime(child.attrib.get("start", ""), spec.default_offset)
            stop = parse_xmltv_datetime(child.attrib.get("stop", ""), spec.default_offset)
            if start is None: continue
            title = programme_title(child)
            norm_title = normalize_programme_title(title)
            if not is_meaningful_programme(norm_title):
                continue
            programs[channel_id].append(child)
            rows[channel_id].append(ProgrammeRow(start=start, stop=stop or start, title=title, normalized_title=norm_title, element=child))

    for channel_id, channel_rows in rows.items():
        channel_rows.sort(key=lambda item: item.start)
        fixed_rows: list[ProgrammeRow] = []
        for idx, row in enumerate(channel_rows):
            next_start = channel_rows[idx + 1].start if idx + 1 < len(channel_rows) else None
            stop = row.stop
            if stop <= row.start:
                if next_start and next_start > row.start:
                    stop = min(next_start, row.start + timedelta(hours=3))
                else:
                    stop = row.start + timedelta(minutes=45)
            elif next_start and stop > next_start:
                stop = next_start
            if (stop - row.start) > timedelta(hours=8):
                stop = next_start if (next_start and next_start > row.start and (next_start - row.start) <= timedelta(hours=4)) else row.start + timedelta(hours=2)
            fixed_rows.append(ProgrammeRow(start=row.start, stop=stop, title=row.title, normalized_title=row.normalized_title, element=row.element))
        rows[channel_id] = fixed_rows
    root.clear()
    return EpgSource(spec=spec, channels=channels, names=names, normalized_names=normalized_names, programs=dict(programs), rows=dict(rows), exact_index=dict(exact_index), fuzzy_index={sig: dict(items) for sig, items in fuzzy_index.items()}, normalized_id_index=dict(normalized_id_index))

def read_optional_source(spec: SourceSpec) -> tuple[EpgSource | None, str | None]:
    tmp_path = None
    try:
        if spec.url.startswith("http"):
            tmp_path = download_to_temp_file(spec.url)
            src = read_source_from_file(spec, tmp_path)
        else:
            local_path = Path(spec.url)
            if local_path.exists():
                src = read_source_from_file(spec, local_path)
            else:
                return None, f"Arquivo local não encontrado: {spec.url}"
        return src, None
    except Exception as exc: 
        return None, str(exc)
    finally:
        if tmp_path and tmp_path.exists() and str(tmp_path).endswith(".tmp"):
            try: tmp_path.unlink()
            except Exception: pass

def aliases_for(entry: dict[str, object]) -> list[str]:
    values: list[str] = [str(entry.get("name", ""))]
    values.extend(str(x) for x in entry.get("aliases", []) or [])
    grupo = str(entry.get("group", entry.get("group-title", entry.get("group_title", ""))))
    output, seen = [], set()
    
    def add_norm(val: str):
        n = original_normalize_name(val)
        if n and n not in seen:
            output.append(n)
            seen.add(n)

    for value in values:
        nome_limpo = normalizar_canal(value)
        nome_com_grupo = inferir_emissora_pelo_grupo(nome_limpo, grupo)
        
        add_norm(nome_com_grupo)
        add_norm(nome_limpo)
        
        for syn in CHANNEL_SYNONYMS.get(nome_limpo, []):
            add_norm(syn)
            
        sem_uf = re.sub(r'\s+(SP|RJ|RS|MG|BA|PR|PE|CE|PA|MA|SC|GO|PB|ES|AM|RN|AL|PI|MT|DF|MS|SE|RO|TO|AC|AP|RR)$', '', nome_limpo)
        if sem_uf != nome_limpo:
            add_norm(sem_uf)
            for syn in CHANNEL_SYNONYMS.get(sem_uf, []):
                add_norm(syn)

    return output

def choose_exact(source: EpgSource, aliases: Iterable[str]) -> tuple[str | None, str]:
    for alias in aliases:
        candidates = source.exact_index.get(alias, set())
        if candidates: return max(candidates, key=lambda cid: len(source.rows.get(cid, []))), f"nome exato: {alias}"
    return None, ""

def _sequence_ratio_upper_bound(left: str, right: str, left_counter: Counter[str] | None = None) -> float:
    if not left or not right: return 0.0
    length_bound = (2.0 * min(len(left), len(right))) / (len(left) + len(right))
    if length_bound < (FUZZY_THRESHOLD - FUZZY_MARGIN): return length_bound
    lc = left_counter or Counter(left)
    rc = Counter(right)
    common = sum(min(count, rc.get(ch, 0)) for ch, count in lc.items())
    return min(length_bound, (2.0 * common) / (len(left) + len(right)))

def best_fuzzy(source: EpgSource, aliases: list[str]) -> tuple[str | None, float, float, str]:
    if not aliases: return None, 0.0, 0.0, ""
    relevant_floor = FUZZY_THRESHOLD - FUZZY_MARGIN
    best_by_channel: dict[str, tuple[float, str, str]] = {}
    for alias in aliases:
        if len(alias) < 2: continue 
        alias_counter = Counter(alias)
        bucket = source.fuzzy_index.get(digit_signature(alias), {})
        for normalized_candidate, channel_ids in bucket.items():
            if _sequence_ratio_upper_bound(alias, normalized_candidate, alias_counter) < relevant_floor: continue
            ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
            if ratio < relevant_floor: continue
            for channel_id in channel_ids:
                old = best_by_channel.get(channel_id)
                if old is None or ratio > old[0]: best_by_channel[channel_id] = (ratio, alias, normalized_candidate)
    if not best_by_channel: return None, 0.0, 0.0, ""
    scored = sorted(((score, cid, a, cand) for cid, (score, a, cand) in best_by_channel.items()), reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second_score, f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"

def find_match(source: EpgSource, aliases: list[str], source_ids: list[str] = None) -> ChannelMatch | None:
    for raw_sid in (source_ids or []):
        norm_sid = normalize_source_id(raw_sid)
        if norm_sid and norm_sid in source.normalized_id_index:
            cands = source.normalized_id_index[norm_sid]
            best_cid = max(cands, key=lambda cid: len(source.rows.get(cid, [])))
            return ChannelMatch(source, best_cid, f"id do dicionário: {raw_sid}", 1.0)

    exact_id, reason = choose_exact(source, aliases)
    if exact_id: return ChannelMatch(source, exact_id, reason, 1.0)
    
    fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, aliases)
    if fuzzy_id and best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN:
        return ChannelMatch(source, fuzzy_id, fuzzy_reason, best_score)
        
    return None

def manual_override(overrides: dict[str, object], target_id: str) -> tuple[str | None, str | None]:
    item = (overrides.get("by_target_id", {}) or {}).get(target_id)
    if not isinstance(item, dict): return None, None
    source = str(item.get("source", "")).lower().strip()
    channel_id = str(item.get("channel_id", "")).strip()
    valid = {s.key for s in SOURCE_SPECS}
    return (source, channel_id) if source in valid and channel_id else (None, None)

def programme_title(programme: ET.Element | None) -> str:
    if programme is None: return ""
    for child in programme:
        if local_tag(child) == "title" and (child.text or "").strip(): return (child.text or "").strip()
    return ""

@lru_cache(maxsize=200000)
def normalize_programme_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).upper()
    if " : " in value:
        value = value.split(" : ", 1)[0]
    value = re.sub(r"\b(?:HD|FHD|AO VIVO|LIVE|ESTREIA|INEDITO|INÉDITO|VT)\b", " ", value)
    value = re.sub(r"\bT\d+\s*E(?:P)?\d+\b", " ", value)
    value = re.sub(r"\bS\d+\s*E(?:P)?\d+\b", " ", value)
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def programme_rows(source: EpgSource, channel_id: str) -> list[ProgrammeRow]: return source.rows.get(channel_id, [])

def now_next_for(source: EpgSource, channel_id: str, now: datetime) -> tuple[dict | None, dict | None]:
    current, next_item = None, None
    for row in programme_rows(source, channel_id):
        clean_t, _ = clean_redundant_title_subtitle(row.title, "")
        if row.start <= now < row.stop: current = {"start": row.start, "stop": row.stop, "title": clean_t}
        elif row.start > now and next_item is None:
            next_item = {"start": row.start, "stop": row.stop, "title": clean_t}
            if current is not None: break
    return current, next_item

_AGREEMENT_CACHE: dict[tuple[str, str, str, str], dict[str, object]] = {}
_HEALTH_CACHE: dict[tuple[str, str], dict[str, object]] = {}

def _calc_agreement(a_rows: list[ProgrammeRow], b_rows: list[ProgrammeRow], offset: timedelta) -> dict[str, object]:
    similarities = []
    b_floor = 0
    tolerance = timedelta(minutes=20)
    for a_row in a_rows:
        a_start, a_stop = a_row.start + offset, a_row.stop + offset
        while b_floor < len(b_rows) and b_rows[b_floor].stop < a_start - tolerance: b_floor += 1
        best = None
        idx = b_floor
        while idx < len(b_rows):
            b_row = b_rows[idx]
            if b_row.start > a_stop + tolerance: break
            overlap = max(0.0, (min(a_stop, b_row.stop) - max(a_start, b_row.start)).total_seconds())
            shorter = max(1.0, min((a_stop - a_start).total_seconds(), (b_row.stop - b_row.start).total_seconds()))
            overlap_ratio = overlap / shorter
            start_delta = abs((b_row.start - a_start).total_seconds())
            if overlap_ratio >= 0.35 or start_delta <= 20 * 60:
                time_score = max(overlap_ratio, max(0.0, 1.0 - start_delta / (20 * 60)))
                title_score = 0.0
                if a_row.normalized_title and b_row.normalized_title:
                    if a_row.normalized_title == b_row.normalized_title or a_row.normalized_title in b_row.normalized_title or b_row.normalized_title in a_row.normalized_title:
                        title_score = 1.0
                    else:
                        title_score = SequenceMatcher(None, a_row.normalized_title, b_row.normalized_title).ratio()
                cand = (time_score, title_score)
                if best is None or cand > best: best = cand
            idx += 1
        if best is not None: similarities.append(best[1])

    if not similarities: return {"comparable": 0, "agreement": None, "average_similarity": None, "detected_shift": 0}
    agreed = sum(1 for s in similarities if s >= 0.68)
    return {"comparable": len(similarities), "agreement": round(agreed / len(similarities), 4), "average_similarity": round(mean(similarities), 4), "detected_shift": int(offset.total_seconds() // 3600)}

def cross_source_programme_agreement(a_source: EpgSource, a_id: str, b_source: EpgSource, b_id: str, now: datetime) -> dict[str, object]:
    cache_key = (a_source.spec.key, a_id, b_source.spec.key, b_id)
    if (cached := _AGREEMENT_CACHE.get(cache_key)) is not None: return cached
    window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=36)
    a_rows = [r for r in programme_rows(a_source, a_id) if r.stop >= window_start and r.start <= window_end]
    b_rows = [r for r in programme_rows(b_source, b_id) if r.stop >= window_start and r.start <= window_end]
    result = _calc_agreement(a_rows, b_rows, timedelta(0))
    if result["comparable"] >= 3 and (result["agreement"] or 0) < AGREEMENT_THRESHOLD:
        for offset_h in (-3, 3, -1, 1, -2, 2, -4, 4):
            res_test = _calc_agreement(b_rows, a_rows, timedelta(hours=offset_h))
            if res_test["comparable"] >= MIN_COMPARABLE_PROGRAMMES and (res_test["agreement"] or 0) > (result["agreement"] or 0):
                result = res_test
                if result["agreement"] >= 0.8: break
    _AGREEMENT_CACHE[cache_key] = result
    return result

def schedule_health(source: EpgSource, channel_id: str, now: datetime) -> dict[str, object]:
    cache_key = (source.spec.key, channel_id)
    if (cached := _HEALTH_CACHE.get(cache_key)) is not None: return cached
    rows = programme_rows(source, channel_id)
    current, next_item = now_next_for(source, channel_id, now)
    invalid, overlaps, gaps = 0, 0, 0
    previous_stop = None
    
    window_end = now + timedelta(hours=24)
    near_rows = [r for r in rows if r.stop > now and r.start < window_end]
    near_covered_sec = 0.0
    for row in near_rows:
        s = max(now, row.start)
        e = min(window_end, row.stop)
        if e > s:
            near_covered_sec += (e - s).total_seconds()
    near_coverage_ratio = min(1.0, near_covered_sec / (24.0 * 3600.0))

    sample_rows = near_rows[:35] if near_rows else rows[:35]
    rich_points = 0.0
    for r in sample_rows:
        dur_m = (r.stop - r.start).total_seconds() / 60.0
        meta = extract_program_metadata(r.element, channel_id, dur_m)
        pts = 0.0
        if meta["desc"] and len(meta["desc"]) >= 30: pts += 0.45
        if meta["categories"]: pts += 0.20
        if meta["date"] or meta["rating"]: pts += 0.20
        if meta["sub_title"] or meta["season"] is not None or meta["episode"] is not None: pts += 0.15
        rich_points += min(1.0, pts)
    richness_score = round(rich_points / len(sample_rows), 4) if sample_rows else 0.0

    for row in rows:
        start, stop = row.start, row.stop
        duration = stop - start
        if duration.total_seconds() <= 0 or duration > timedelta(hours=12): invalid += 1
        if previous_stop is not None:
            if start < previous_stop - timedelta(seconds=30): overlaps += 1
            elif start > previous_stop + timedelta(minutes=15): gaps += 1
        if previous_stop is None or stop > previous_stop: previous_stop = stop
        
    future_end = max((row.stop for row in rows if row.stop > now), default=None)
    future_hours = max(0.0, (future_end - now).total_seconds() / 3600) if future_end else 0.0
    score = (
        (0.25 if current else 0.0)
        + (0.10 if next_item else 0.0)
        + (0.35 * near_coverage_ratio)
        + (0.20 * min(future_hours / 36.0, 1.0))
        + (0.10 * min(len(rows) / 12.0, 1.0))
    )
    score -= min(0.35, invalid * 0.08 + overlaps * 0.02 + gaps * 0.03)
    if future_hours < 8.0:
        score = min(score, 0.35)
    result = {
        "score": round(max(0.0, min(1.0, score)), 4),
        "richness_score": richness_score,
        "near_coverage_ratio": round(near_coverage_ratio, 4),
        "rows": len(rows),
        "current": current,
        "next": next_item,
        "future_hours": round(future_hours, 1),
        "invalid_durations": invalid,
        "overlaps": overlaps,
        "gaps": gaps,
    }
    _HEALTH_CACHE[cache_key] = result
    return result

def evaluate_candidates(matches: list[ChannelMatch], now: datetime, max_active_vote_weight: float) -> tuple[CandidateEvaluation, list[CandidateEvaluation]]:
    health_by_source = {m.source.spec.key: schedule_health(m.source, m.channel_id, now) for m in matches}
    evaluations: list[CandidateEvaluation] = []

    for match in matches:
        supporters = [match.source]
        support_votes, vote_sources = 1, 1
        support_weight, vote_weight = match.source.spec.vote_weight, match.source.spec.vote_weight
        support_agreements, comparisons = [], []
        for other in matches:
            if other.source is match.source: continue
            result = cross_source_programme_agreement(match.source, match.channel_id, other.source, other.channel_id, now)
            comparable = int(result.get("comparable") or 0)
            agreement = result.get("agreement")
            vote = "sem_dados"
            if comparable >= MIN_COMPARABLE_PROGRAMMES and isinstance(agreement, float):
                vote_sources += 1
                vote_weight += other.source.spec.vote_weight
                if agreement >= AGREEMENT_THRESHOLD:
                    vote = "concorda"
                    support_votes += 1
                    support_weight += other.source.spec.vote_weight
                    supporters.append(other.source)
                    support_agreements.append(agreement)
                else: vote = "diverge"
            comparisons.append({
                "other_source": other.source.label,
                "other_source_key": other.source.spec.key,
                "other_channel_id": other.channel_id,
                "other_match_method": other.method,
                "comparable": comparable,
                "agreement": agreement,
                "average_similarity": result.get("average_similarity"),
                "detected_shift": result.get("detected_shift", 0),
                "vote": vote,
            })
        support_families = len({src.spec.family for src in supporters})
        weighted_ratio = support_weight / vote_weight if vote_weight else 0.0
        avg_support = mean(support_agreements) if support_agreements else 0.0
        h_info = health_by_source[match.source.spec.key]
        health_score = float(h_info["score"])
        richness_score = float(h_info.get("richness_score", 0.0))

        quality = 0.45 * weighted_ratio + 0.30 * health_score + 0.10 * richness_score + 0.15 * avg_support
        evidence_factor = 0.55 + 0.45 * min(vote_weight / max(max_active_vote_weight, 0.5), 1.0)
        confidence = round(100 * max(0.0, min(1.0, quality * evidence_factor)))
        evaluations.append(CandidateEvaluation(
            match=match, support_votes=support_votes, vote_sources=vote_sources,
            support_weight=round(support_weight, 3), vote_weight=round(vote_weight, 3),
            support_families=support_families, weighted_ratio=round(weighted_ratio, 4),
            avg_support_agreement=round(avg_support, 4), health_score=round(health_score, 4),
            richness_score=richness_score, health=h_info, confidence=confidence, comparisons=comparisons
        ))

    def rank(ev: CandidateEvaluation) -> tuple:
        has_programmes = int(bool(ev.match.source.rows.get(ev.match.channel_id)))
        fut_h = float(ev.health.get("future_hours", 0.0))
        is_healthy_full_day = int(fut_h >= 18.0 and ev.health_score >= 0.65)
        consensus_tier = round(ev.support_weight / 1.5)
        return (
            has_programmes,
            is_healthy_full_day,
            consensus_tier,
            round(ev.health_score, 2),
            round(ev.richness_score, 2),
            ev.support_weight,
            ev.avg_support_agreement,
            -ev.match.source.spec.priority,
        )

    evaluations.sort(key=rank, reverse=True)
    return evaluations[0], evaluations

def serialize_now_next(item: dict | None) -> dict | None:
    if item is None: return None
    return {"title": item["title"], "start": item["start"].isoformat(), "stop": item["stop"].isoformat()}

def make_validation(entry: dict[str, object], chosen: CandidateEvaluation, all_evaluations: list[CandidateEvaluation], now: datetime) -> dict[str, object]:
    health = chosen.health
    warnings = []
    if not health["rows"]: warnings.append("nenhum programa na fonte escolhida")
    if health["current"] is None: warnings.append("sem programa cobrindo o horário atual")
    if health["next"] is None: warnings.append("sem próximo programa disponível")
    if float(health["future_hours"]) < 6: warnings.append(f"pouca programação futura ({health['future_hours']} h)")

    first_by_priority = min(all_evaluations, key=lambda ev: ev.match.source.spec.priority) if all_evaluations else chosen
    switched = first_by_priority.match.source is not chosen.match.source
    if switched: warnings.append(f"fonte base alterada de {first_by_priority.match.source.label} para {chosen.match.source.label} por maior consenso/qualidade")

    if chosen.support_families >= 3 and chosen.confidence >= 75: status = "ok"
    elif chosen.confidence >= 55 and chosen.support_families >= 2: status = "atencao"
    else: status = "suspeito"
    if float(health["score"]) < 0.45: status = "suspeito"

    candidates = [
        {
            "source": ev.match.source.label,
            "source_key": ev.match.source.spec.key,
            "channel_id": ev.match.channel_id,
            "match_method": ev.match.method,
            "match_score": round(ev.match.match_score, 4),
            "confidence": ev.confidence,
            "health_score": ev.health_score,
            "richness_score": ev.richness_score,
        }
        for ev in sorted(all_evaluations, key=lambda x: x.match.source.spec.priority)
    ]
    return {
        "target_id": str(entry.get("id", "")), "name": str(entry.get("name", "")),
        "source": chosen.match.source.label, "source_key": chosen.match.source.spec.key,
        "source_channel_id": chosen.match.channel_id, "status": status, "confidence": chosen.confidence,
        "warnings": warnings, "selection_changed_from_priority": switched,
        "now": serialize_now_next(health["current"]), "next": serialize_now_next(health["next"]),
        "future_hours": health["future_hours"], "health_score": health["score"], "richness_score": chosen.richness_score,
        "consensus": {"supporting_votes": chosen.support_votes, "vote_sources": chosen.vote_sources, "support_weight": chosen.support_weight, "vote_weight": chosen.vote_weight, "support_families": chosen.support_families, "weighted_ratio": chosen.weighted_ratio, "confidence": chosen.confidence},
        "cross_sources": chosen.comparisons, "candidates": candidates,
    }

def generate_quality_variants(base_names: Iterable[str]) -> list[str]:
    suffixes = [
        "",
        " SD",
        " HD",
        " FHD",
        " FHD [H265]",
        " FHD H265",
        " HD [H265]",
        " HD H265",
        " [H265]",
        " H265",
        " 4K",
    ]
    result = []
    seen = set()
    for base in base_names:
        clean_base = strip_qualities_keep_case(base)
        for root in (base.strip(), clean_base):
            if not root or len(root) > 160:
                continue
            for suf in suffixes:
                cand = f"{root}{suf}".strip()
                if cand and cand.upper() not in seen:
                    result.append(cand)
                    seen.add(cand.upper())
    return result

def get_clean_aliases(entry: dict[str, object]) -> list[str]:
    values = []
    raw_name = str(entry.get("name", "")).strip()
    if raw_name:
        values.append(raw_name)
    if entry.get("tvg_names"):
        values.extend(str(x).strip() for x in entry.get("tvg_names", []) if x)
    if entry.get("aliases"):
        values.extend(str(x).strip() for x in entry.get("aliases", []) if x)
        
    norm_clean = normalizar_canal(raw_name)
    for syn in CHANNEL_SYNONYMS.get(norm_clean, []):
        values.append(syn)
        values.append(syn.title())

    return generate_quality_variants(values)

def get_primary_export_ids(entry: dict[str, object]) -> list[str]:
    ids = []
    seen = set()
    raw_name = str(entry.get("name", "")).strip()
    target_id = str(entry.get("id", "")).strip()
    clean_base = strip_qualities_keep_case(raw_name) or target_id

    def add_id(val: str):
        v = val.strip()
        if v and v not in seen and len(v) <= 180:
            ids.append(v)
            seen.add(v)

    for cid in entry.get("compatibility_ids", []) or []:
        if is_valid_provider_tvid(raw_name, str(cid)):
            add_id(str(cid))
    for t_id in entry.get("tvg_ids_to_export", []) or []:
        if is_valid_provider_tvid(raw_name, str(t_id)):
            add_id(str(t_id))

    add_id(clean_base)
    return ids[:2]

def apply_rich_metadata_to_element(
    el: ET.Element, meta_pool: list[dict[str, Any]], channel_name: str, duration_minutes: float
) -> None:
    """Formata a descrição no modelo exato:
    - Filme: '2015 Livre Comédia/Família. Sinopse...'
    - Série: '2019 14 Anos Drama/Crime. T02 E05 - Episódio. Sinopse...'
    """
    base_meta = extract_program_metadata(el, channel_name, duration_minutes)
    all_metas = [base_meta, *(meta_pool or [])]

    best_sub = ""
    best_desc = ""
    best_season: int | None = None
    best_episode: int | None = None
    best_date = ""
    best_rating = ""
    merged_cats: list[str] = []

    for m in all_metas:
        if m.get("sub_title") and len(m["sub_title"]) > len(best_sub):
            best_sub = m["sub_title"]
        if m.get("desc") and len(m["desc"]) > len(best_desc):
            best_desc = m["desc"]
        if best_season is None and m.get("season") is not None:
            best_season = m["season"]
        if best_episode is None and m.get("episode") is not None:
            best_episode = m["episode"]
        if not best_date and m.get("date"):
            best_date = m["date"]
        if not best_rating and m.get("rating"):
            best_rating = m["rating"]
        for c in m.get("categories", []):
            if c not in merged_cats:
                merged_cats.append(c)

    is_movie_or_sport = is_likely_movie_or_sport(merged_cats, best_desc, duration_minutes, channel_name)
    is_sport_news = is_sport_or_news_channel(channel_name)

    if is_movie_or_sport:
        best_season = None
        best_episode = None

    for old_ep in list(el.findall("episode-num")):
        el.remove(old_ep)

    title_node = el.find("title")
    raw_title = base_meta["title"] or (title_node.text.strip() if title_node is not None and title_node.text else "")
    clean_title, best_sub = clean_redundant_title_subtitle(raw_title, best_sub)
    clean_title = _RE_TRAILING_FAKE_EP.sub("", clean_title).strip()
    if title_node is not None and clean_title:
        title_node.text = clean_title

    genre_str = normalize_genres_list(merged_cats)

    # Se faltar Ano ou Gênero (e não for telejornal/jogo ao vivo), consulta Wikipedia PT / iTunes BR!
    is_live_match = " X " in clean_title.upper() or clean_title.upper().startswith(("AO VIVO", "VT -", "TELEJORNAL", "JORNAL "))
    if not is_sport_news and not is_live_match and (not best_date or not genre_str or not best_rating):
        web_info = query_web_metadata(clean_title, is_movie=is_movie_or_sport)
        if web_info:
            if not best_date and web_info.get("year"):
                best_date = web_info["year"]
            if not best_rating and web_info.get("rating"):
                best_rating = web_info["rating"]
            if not genre_str and web_info.get("genre"):
                genre_str = web_info["genre"]
            if not best_desc and web_info.get("desc"):
                best_desc = web_info["desc"]

    # Etiqueta de Temporada e Episódio (apenas para Séries)
    se_badge = ""
    if not is_movie_or_sport:
        if best_season is not None and best_episode is not None:
            se_badge = f"T{best_season:02d} E{best_episode:02d}"
        elif best_episode is not None and best_episode > 1:
            se_badge = f"Ep. {best_episode}"

    sub_node = el.find("sub-title")
    if is_movie_or_sport:
        if sub_node is not None:
            el.remove(sub_node)
        best_sub = ""
    else:
        if best_sub and best_sub.upper() != clean_title.upper():
            if sub_node is None:
                sub_node = ET.SubElement(el, "sub-title")
            sub_node.text = f"{se_badge} - {best_sub}" if se_badge and se_badge not in best_sub else best_sub
        elif se_badge:
            if sub_node is None:
                sub_node = ET.SubElement(el, "sub-title")
            sub_node.text = se_badge
        elif sub_node is not None:
            el.remove(sub_node)

    # Monta o cabeçalho no formato pedido: "2015 Livre Comédia/Família." + (Se série: "T01 E02 - Subtítulo.") + "Sinopse..."
    meta_tokens = []
    if best_date:
        meta_tokens.append(best_date)
    if best_rating:
        meta_tokens.append(best_rating)
    if genre_str:
        meta_tokens.append(genre_str)

    lead_info = " ".join(meta_tokens).strip()

    series_info = ""
    if se_badge and best_sub:
        series_info = f"{se_badge} - {best_sub}"
    elif se_badge:
        series_info = se_badge
    elif best_sub and not is_movie_or_sport:
        series_info = best_sub

    # Limpa da sinopse original caso ela já comece repetindo o mesmo ano/gênero
    clean_desc = (best_desc or "").strip()
    if best_date and clean_desc.startswith(f"({best_date})"):
        clean_desc = clean_desc[len(f"({best_date})"):].strip(" -•.")

    prefix_blocks = []
    if lead_info:
        prefix_blocks.append(f"{lead_info}.")
    if series_info and (not clean_desc or series_info.lower() not in clean_desc.lower()):
        prefix_blocks.append(f"{series_info}.")

    prefix_text = " ".join(prefix_blocks).strip()
    if prefix_text and clean_desc:
        final_desc = f"{prefix_text} {clean_desc}"
    elif prefix_text:
        final_desc = prefix_text
    else:
        final_desc = clean_desc

    if final_desc:
        desc_node = el.find("desc")
        if desc_node is None:
            desc_node = ET.SubElement(el, "desc")
        desc_node.text = final_desc

    if not is_movie_or_sport and best_season is not None and best_episode is not None:
        s_idx = max(0, best_season - 1)
        e_idx = max(0, best_episode - 1)
        ep_ns = ET.SubElement(el, "episode-num", {"system": "xmltv_ns"})
        ep_ns.text = f"{s_idx}.{e_idx}."

def stitch_multi_source_schedule(
    all_evaluations: list[CandidateEvaluation], target_channel_id: str, channel_name: str, cutoff: datetime, max_future: datetime, overrides: dict[str, object]
) -> list[ET.Element]:
    if not all_evaluations: return []

    leader_ev = all_evaluations[0]
    shift_by_source_key: dict[str, int] = {leader_ev.match.source.spec.key: 0}
    for comp in leader_ev.comparisons:
        other_key = str(comp.get("other_source_key", ""))
        if comp.get("agreement") and float(comp["agreement"]) >= 0.50:
            shift_by_source_key[other_key] = int(comp.get("detected_shift") or 0)

    all_aligned_progs_by_source: list[list[dict[str, Any]]] = []
    metadata_by_norm_title: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for ev in all_evaluations:
        src = ev.match.source
        cid = ev.match.channel_id
        fixed_rows = src.rows.get(cid, [])
        if not fixed_rows: continue

        ch_shift = float(overrides.get("channel_timeshifts", {}).get(target_channel_id, 0.0))
        src_shift = float(overrides.get("source_timeshifts", {}).get(src.spec.key, 0.0))
        auto_shift = float(shift_by_source_key.get(src.spec.key, 0))
        total_shift = timedelta(hours=ch_shift + src_shift + auto_shift)

        cleaned_source_progs = []
        for idx, row in enumerate(fixed_rows):
            if not is_meaningful_programme(row.normalized_title):
                continue
            s_dt = row.start + total_shift
            sp_dt = row.stop + total_shift
            if sp_dt <= s_dt:
                continue
            if idx + 1 < len(fixed_rows):
                next_s = fixed_rows[idx + 1].start + total_shift
                if sp_dt > next_s and next_s > s_dt:
                    sp_dt = next_s
            if sp_dt <= cutoff or s_dt >= max_future:
                continue

            dur_m = (sp_dt - s_dt).total_seconds() / 60.0
            meta = extract_program_metadata(row.element, channel_name, dur_m)
            prog_item = {
                "start": s_dt,
                "stop": sp_dt,
                "el": row.element,
                "norm_title": row.normalized_title,
                "meta": meta,
            }
            cleaned_source_progs.append(prog_item)
            if row.normalized_title:
                metadata_by_norm_title[row.normalized_title].append(meta)

        if cleaned_source_progs:
            all_aligned_progs_by_source.append(cleaned_source_progs)

    if not all_aligned_progs_by_source:
        return []

    merged_timeline: list[dict[str, Any]] = list(all_aligned_progs_by_source[0])
    merged_timeline.sort(key=lambda x: x["start"])

    MIN_GAP_SECONDS = 10 * 60
    MAX_EDGE_TRIM_SECONDS = 15 * 60

    for cleaned_source_progs in all_aligned_progs_by_source[1:]:
        added_any = False
        for cand in cleaned_source_progs:
            c_start, c_stop = cand["start"], cand["stop"]
            c_dur = (c_stop - c_start).total_seconds()
            if c_dur < 300:
                continue

            overlapping = [
                ex for ex in merged_timeline
                if ex["stop"] > c_start and ex["start"] < c_stop
            ]

            if not overlapping:
                merged_timeline.append(copy.deepcopy(cand))
                added_any = True
                continue

            overlapping.sort(key=lambda x: x["start"])
            free_cursor = c_start
            best_gap = None
            for ex in overlapping:
                if ex["start"] > free_cursor:
                    gap_sec = (ex["start"] - free_cursor).total_seconds()
                    if gap_sec >= MIN_GAP_SECONDS:
                        if best_gap is None or gap_sec > (best_gap[1] - best_gap[0]).total_seconds():
                            best_gap = (free_cursor, ex["start"])
                if ex["stop"] > free_cursor:
                    free_cursor = ex["stop"]
            if c_stop > free_cursor:
                gap_sec = (c_stop - free_cursor).total_seconds()
                if gap_sec >= MIN_GAP_SECONDS:
                    if best_gap is None or gap_sec > (best_gap[1] - best_gap[0]).total_seconds():
                        best_gap = (free_cursor, c_stop)

            if best_gap is not None:
                g_start, g_stop = best_gap
                gap_dur = (g_stop - g_start).total_seconds()
                left_trim = (g_start - c_start).total_seconds()
                right_trim = (c_stop - g_stop).total_seconds()
                if (gap_dur / c_dur) >= 0.70 or (left_trim <= MAX_EDGE_TRIM_SECONDS and right_trim <= MAX_EDGE_TRIM_SECONDS):
                    new_cand = copy.deepcopy(cand)
                    new_cand["start"] = g_start
                    new_cand["stop"] = g_stop
                    merged_timeline.append(new_cand)
                    added_any = True

        if added_any:
            merged_timeline.sort(key=lambda x: x["start"])

    merged_timeline.sort(key=lambda x: x["start"])

    final_elements = []
    for i, prog in enumerate(merged_timeline):
        s_dt, sp_dt = prog["start"], prog["stop"]
        if i + 1 < len(merged_timeline) and sp_dt > merged_timeline[i + 1]["start"]:
            sp_dt = merged_timeline[i + 1]["start"]
        dur_sec = (sp_dt - s_dt).total_seconds()
        if dur_sec < 60:
            continue
        dur_min = dur_sec / 60.0
            
        el = copy.deepcopy(prog["el"])
        el.attrib["start"] = format_xmltv_datetime(s_dt)
        el.attrib["stop"] = format_xmltv_datetime(sp_dt)
        
        norm_title = prog.get("norm_title", "")
        matching_time_metas: list[dict[str, Any]] = []
        for src_progs in all_aligned_progs_by_source:
            for other_p in src_progs:
                if other_p["norm_title"] == norm_title and abs((other_p["start"] - s_dt).total_seconds()) <= 25 * 60:
                    matching_time_metas.append(other_p["meta"])

        if not matching_time_metas and norm_title in metadata_by_norm_title:
            matching_time_metas = [
                {
                    "title": m["title"],
                    "sub_title": "",
                    "desc": m["desc"],
                    "season": None,
                    "episode": None,
                    "categories": m["categories"],
                    "date": m["date"],
                    "rating": m.get("rating", ""),
                }
                for m in metadata_by_norm_title[norm_title]
            ]

        apply_rich_metadata_to_element(el, matching_time_metas, channel_name, dur_min)
        final_elements.append(el)
    return final_elements

def write_epg_stream(
    selected: list[tuple[dict[str, object], CandidateEvaluation, list[CandidateEvaluation], bool]],
    now: datetime,
    overrides: dict[str, object] = None,
) -> tuple[int, int]:
    overrides = overrides or {}
    gzip_path, plain_path = OUTPUT_DIR / "epg.xml.gz", OUTPUT_DIR / "epg.xml"
    plain = plain_path.open("wb") if WRITE_PLAIN_XML else None
    programme_count = 0
    cutoff = now - timedelta(hours=OUTPUT_PAST_HOURS)
    max_future = now + timedelta(hours=OUTPUT_FUTURE_HOURS)

    xml_id_to_data: dict[str, tuple[CandidateEvaluation, list[ET.Element], list[str]]] = {}
    
    for entry, chosen, all_evaluations, _forced in selected:
        target_id = str(entry.get("id", ""))
        channel_name = str(entry.get("name", target_id))
        stitched = stitch_multi_source_schedule(all_evaluations, target_id, channel_name, cutoff, max_future, overrides)
        all_names = get_clean_aliases(entry)
        export_ids = get_primary_export_ids(entry)
        
        for out_id in export_ids:
            if out_id not in xml_id_to_data:
                xml_id_to_data[out_id] = (chosen, stitched, list(all_names))
            else:
                existing_names = xml_id_to_data[out_id][2]
                for n in all_names:
                    if n not in existing_names:
                        existing_names.append(n)

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None: plain.write(data)

            generator = "EPG Universal Otimizado (Ano/Classificação/Gênero + Séries TxxExx)"
            source_name = "Consolidado Inteligente de Múltiplas Fontes"
            emit((f"<?xml version='1.0' encoding='utf-8'?>\n<tv generator-info-name={quoteattr(generator)} source-info-name={quoteattr(source_name)}>\n").encode("utf-8"))

            for out_id, (chosen, _, display_names) in xml_id_to_data.items():
                channel = ET.Element("channel", {"id": out_id})
                
                primary_dn = ET.Element("display-name")
                primary_dn.text = out_id
                channel.append(primary_dn)
                
                for dn in display_names:
                    if dn != out_id:
                        dn_node = ET.Element("display-name")
                        dn_node.text = dn
                        channel.append(dn_node)
                
                source_channel = chosen.match.source.channels.get(chosen.match.channel_id)
                if source_channel is not None:
                    for node in source_channel:
                        if local_tag(node) == "icon":
                            icon_node = copy.deepcopy(node)
                            channel.append(icon_node)
                            
                emit(ET.tostring(channel, encoding="utf-8"))
                emit(b"\n")

            for out_id, (_, stitched_programmes, _) in xml_id_to_data.items():
                for original in stitched_programmes:
                    prog_copy = copy.deepcopy(original)
                    prog_copy.attrib["channel"] = out_id
                    emit(ET.tostring(prog_copy, encoding="utf-8"))
                    emit(b"\n")
                    programme_count += 1
                    
            emit(b"</tv>\n")

    if plain is not None: plain.close()
    elif plain_path.exists(): plain_path.unlink()
    
    size_mb = gzip_path.stat().st_size / (1024 * 1024)
    print(f"Tamanho final do epg.xml.gz: {size_mb:.2f} MB (Limite do GitHub: 100 MB)")
    return programme_count, len(xml_id_to_data)

def main() -> None:
    print("Iniciando geração de EPG (Ano + Classificação + Gênero + Séries TxxExx)...")
    run_start = time.perf_counter()
    timings = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    load_metadata_cache()
    stage_start = time.perf_counter()
    
    entries = []
    grouped_by_norm: dict[str, dict[str, Any]] = {}
    VOD_BLACKLIST = {"VOD", "PLAYBACK", "XXX", "ADULTO", "EVENTO", "LANCAMENTO", "LANÇAMENTO", "FILMES", "SERIES", "SÉRIES"}

    def merge_channel_record(ch_id: str, name: str, aliases: Iterable[str] = (), tvg_ids: Iterable[str] = (), comp_ids: Iterable[str] = (), source_ids: Iterable[str] = (), group: str = "") -> None:
        base_name = name or ch_id
        if not base_name: return
        norm_key = normalize_name(base_name) or base_name.strip().upper()
        if norm_key not in grouped_by_norm:
            grouped_by_norm[norm_key] = {
                "id": ch_id or base_name,
                "name": strip_qualities_keep_case(base_name) or base_name,
                "aliases": set(),
                "tvg_ids_to_export": set(),
                "compatibility_ids": set(),
                "source_ids": set(),
                "group": group or "",
            }
        rec = grouped_by_norm[norm_key]
        if base_name: rec["aliases"].add(base_name)
        for a in aliases:
            if a: rec["aliases"].add(str(a).strip())
        for t in tvg_ids:
            if t: rec["tvg_ids_to_export"].add(str(t).strip())
        for c in comp_ids:
            if c: rec["compatibility_ids"].add(str(c).strip())
        for s in source_ids:
            if s: rec["source_ids"].add(str(s).strip())
        if group and not rec["group"]:
            rec["group"] = group

    def parse_m3u_file_into_records(m3u_file: Path) -> int:
        count = 0
        with m3u_file.open("r", encoding="utf-8", errors="ignore") as f:
            current_extinf = None
            for line in f:
                line = line.strip()
                if not line: continue
                if line.startswith("#EXTINF:"):
                    current_extinf = line
                elif (line.startswith("http") or line.startswith("rtmp")) and current_extinf:
                    lower_url = line.split("?")[0].lower()
                    if lower_url.endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm')):
                        current_extinf = None
                        continue

                    tvg_id_match = re.search(r'tvg-id="(.*?)"', current_extinf, re.IGNORECASE)
                    tvg_name_match = re.search(r'tvg-name="(.*?)"', current_extinf, re.IGNORECASE)
                    group_match = re.search(r'group-title="(.*?)"', current_extinf, re.IGNORECASE)
                    name_match = re.search(r',([^,]*?)$', current_extinf)
                    
                    tvg_id = tvg_id_match.group(1).strip() if tvg_id_match else ""
                    tvg_name = tvg_name_match.group(1).strip() if tvg_name_match else ""
                    group = group_match.group(1).strip() if group_match else ""
                    name = name_match.group(1).strip() if name_match else ""
                    current_extinf = None
                    
                    if any(blacklisted in group.upper() for blacklisted in VOD_BLACKLIST):
                        continue
                        
                    target_id = tvg_name or name
                    if not target_id: continue
                    
                    name_upper = target_id.upper()
                    if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b|\bT\d{1,3}\s*EP?\d{1,3}\b|\b24HS?\b|^24\s*H\b', name_upper):
                        continue
                    
                    merge_channel_record(
                        ch_id=target_id,
                        name=name or tvg_name,
                        aliases=[x for x in (tvg_name, name) if x],
                        tvg_ids=[tvg_id] if tvg_id else [],
                        group=group,
                    )
                    count += 1
        return count

    # 1. Lê TODAS as chaves reais do channels.json (incluindo old_tvg_ids, original_names, blank_tvg_id_names e groups)
    if CATALOG_PATH.exists():
        try:
            print(f"Lendo Dicionário Persistente ({CATALOG_PATH})...")
            cat_data = json.loads(CATALOG_PATH.read_text("utf-8"))
            cat_list = cat_data.get("channels", []) if isinstance(cat_data, dict) else (cat_data if isinstance(cat_data, list) else [])
            for ch in cat_list:
                if not isinstance(ch, dict): continue
                ch_groups = ch.get("groups", []) or []
                grp_str = " | ".join(str(g) for g in ch_groups) if isinstance(ch_groups, list) else str(ch.get("group", ch.get("group-title", "")))
                all_ch_aliases = [
                    *(ch.get("aliases", []) or []),
                    *(ch.get("tvg_names", []) or []),
                    *(ch.get("original_names", []) or []),
                    *(ch.get("blank_tvg_id_names", []) or []),
                    *(ch.get("blank_tvg_id_tvg_names", []) or []),
                ]
                all_ch_tvg_ids = [
                    *(ch.get("tvg_ids_to_export", []) or []),
                    *(ch.get("old_tvg_ids", []) or []),
                ]
                merge_channel_record(
                    ch_id=str(ch.get("id", ch.get("name", ""))).strip(),
                    name=str(ch.get("name", ch.get("id", ""))).strip(),
                    aliases=all_ch_aliases,
                    tvg_ids=all_ch_tvg_ids,
                    comp_ids=ch.get("compatibility_ids", []) or [],
                    source_ids=[*(ch.get("source_ids", []) or []), *(ch.get("old_tvg_ids", []) or [])],
                    group=grp_str,
                )
            print(f"  -> {len(grouped_by_norm)} emissoras base carregadas do dicionário.")
        except Exception as e:
            print(f"Aviso ao ler {CATALOG_PATH}: {e}")

    local_m3us = sorted(set(
        glob.glob("*.m3u") + glob.glob("*.m3u8") +
        glob.glob("learning/*.m3u") + glob.glob("learning/*.m3u8")
    ))
    for m3u_file_str in local_m3us:
        try:
            c = parse_m3u_file_into_records(Path(m3u_file_str))
            print(f"Lido arquivo M3U local [{m3u_file_str}]: {c} canais extraídos.")
        except Exception as e:
            print(f"Aviso ao ler M3U local {m3u_file_str}: {e}")

    if ESPELHO_PATH.exists():
        try:
            print("Lendo Espelho de Segurança...")
            espelho_data = json.loads(ESPELHO_PATH.read_text("utf-8"))
            for ch in espelho_data.get("channels", []):
                ch_name = str(ch.get("name", "")).strip()
                if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b|\bT\d{1,3}\s*EP?\d{1,3}\b|\b24HS?\b|^24\s*H\b', ch_name.upper()):
                    continue
                merge_channel_record(
                    ch_id=str(ch.get("id", "")).strip(),
                    name=ch_name,
                    aliases=ch.get("aliases", []) or [],
                    tvg_ids=[*(ch.get("tvg_ids_to_export", []) or []), *(ch.get("old_tvg_ids", []) or [])],
                    comp_ids=ch.get("compatibility_ids", []) or [],
                    source_ids=ch.get("source_ids", []) or [],
                    group=str(ch.get("group", "")),
                )
            print(f"  -> Total após mesclar espelho: {len(grouped_by_norm)} emissoras base.")
        except Exception as e:
            print(f"Aviso: Não foi possível carregar o espelho: {e}")
    
    for idx, url_raw in enumerate(M3U_URLS):
        url = url_raw.strip()
        if not url.startswith("http"): continue
        
        parsed_url = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed_url.query)
        
        is_xtream = "get.php" in parsed_url.path and "username" in query and "password" in query
        success_json = False
        
        print(f"\nProcessando Lista Secreta {idx+1}...")
        
        if is_xtream:
            user = query["username"][0]
            pwd = query["password"][0]
            api_url = f"{parsed_url.scheme}://{parsed_url.netloc}/player_api.php?username={user}&password={pwd}&action=get_live_streams"
            print("  -> Link Xtream Codes detectado! Extraindo API JSON...")
            try:
                data = fetch_iptv_json(api_url, timeout=45)
                if isinstance(data, list):
                    print(f"  -> Sucesso! {len(data)} canais ao vivo obtidos via API.")
                    for ch in data:
                        name = str(ch.get("name", "")).strip()
                        epg_id = str(ch.get("epg_channel_id", "")).strip()
                        cat_id = str(ch.get("category_id", ""))
                        if not name: continue
                        
                        name_upper = name.upper()
                        if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b|\bT\d{1,3}\s*EP?\d{1,3}\b|\b24HS?\b|^24\s*H\b', name_upper):
                            continue
                        
                        merge_channel_record(
                            ch_id=name,
                            name=name,
                            aliases=[name],
                            tvg_ids=[epg_id] if epg_id else [],
                            group=cat_id,
                        )
                    success_json = True
            except Exception as e:
                print(f"  -> Aviso na API JSON ({e}). Tentando M3U tradicional...")

        if not success_json:
            print("  -> Baixando arquivo M3U tradicional...")
            try:
                tmp_m3u = download_to_temp_file(url)
                added_m3u = parse_m3u_file_into_records(tmp_m3u)
                print(f"  -> Sucesso via M3U! {added_m3u} canais processados.")
                try: tmp_m3u.unlink()
                except Exception: pass
            except Exception as e:
                print(f"  -> Lista {idx+1} bloqueada para o IP do GitHub ({e}). Usando Dicionário/M3U Local!")

    for v in grouped_by_norm.values():
        v["aliases"] = sorted(v["aliases"])
        v["tvg_ids_to_export"] = sorted(v["tvg_ids_to_export"])
        v["compatibility_ids"] = sorted(v["compatibility_ids"])
        v["source_ids"] = sorted(v["source_ids"])
        entries.append(v)
        
    print(f"\nCatálogo unificado pronto: {len(entries)} emissoras base únicas.")

    try: overrides = json.loads(OVERRIDES_PATH.read_text("utf-8")) if OVERRIDES_PATH.exists() else {}
    except Exception: overrides = {}

    timings["catalogo"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    print(f"\nBaixando e indexando {len(SOURCE_SPECS)} fontes (máx {DOWNLOAD_WORKERS} workers)...")
    loaded: dict[int, tuple[EpgSource | None, str | None]] = {}
    
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        future_map = {executor.submit(read_optional_source, spec): spec for spec in SOURCE_SPECS}
        for future in as_completed(future_map):
            spec = future_map[future]
            try: source, error = future.result()
            except Exception as exc: source, error = None, str(exc)
            loaded[spec.priority] = (source, error)
            if source is not None: print(f"✓ [{spec.label}] {len(source.channels)} canais | {sum(len(v) for v in source.rows.values())} programas carregados.")
            else: print(f"⚠ [{spec.label}] Falhou: {error}")

    active_sources, source_errors = [], {}
    for spec in SOURCE_SPECS:
        source, error = loaded.get(spec.priority, (None, "fonte sem retorno"))
        if source is not None: active_sources.append(source)
        else: source_errors[spec.label] = error or "falha desconhecida"
    timings["download_e_indexacao_fontes"] = round(time.perf_counter() - stage_start, 3)

    if not active_sources: raise RuntimeError("Nenhuma das fontes configuradas pôde ser carregada com sucesso.")

    source_by_key = {s.spec.key: s for s in active_sources}
    max_active_vote_weight = sum(s.spec.vote_weight for s in active_sources)
    now = datetime.now(timezone.utc)

    stage_start = time.perf_counter()
    selected, report_rows, validations = [], [], []

    for entry in entries:
        target_id = str(entry.get("id", ""))
        visible_name = str(entry.get("name", ""))
        aliases = aliases_for(entry)
        source_ids = [str(x) for x in entry.get("source_ids", []) or []]
        matches = [m for source in active_sources if (m := find_match(source, aliases, source_ids)) is not None]

        override_source_key, override_id = manual_override(overrides, target_id)
        forced = False
        if override_source_key and override_id and (src := source_by_key.get(override_source_key)) and override_id in src.channels:
            override_match = ChannelMatch(src, override_id, "override manual", 1.0)
            matches = [m for m in matches if m.source is not src]
            matches.append(override_match)
            forced = True

        if not matches:
            report_rows.append({"target_id": target_id, "name": visible_name, "status": "missing", "source": None, "source_key": None, "source_channel_id": None, "method": "sem correspondência nas fontes ativas", "confidence": 0})
            continue

        chosen, evaluations = evaluate_candidates(matches, now, max_active_vote_weight)
        if forced and override_source_key:
            if forced_ev := next((ev for ev in evaluations if ev.match.source.spec.key == override_source_key and ev.match.channel_id == override_id), None):
                chosen = forced_ev

        selected.append((entry, chosen, evaluations, forced))
        first_match = min(evaluations, key=lambda ev: ev.match.source.spec.priority) if evaluations else chosen
        changed = first_match.match.source is not chosen.match.source
        
        report_rows.append({"target_id": target_id, "name": visible_name, "status": "matched", "source": chosen.match.source.label, "source_key": chosen.match.source.spec.key, "source_channel_id": chosen.match.channel_id, "method": chosen.match.method, "confidence": chosen.confidence, "changed_from_first_available": changed, "matched_sources_count": len(evaluations)})
        validations.append(make_validation(entry, chosen, evaluations, now))

    matched_ids = {str(e.get("id", "")) for e, _, _, _ in selected}
    clean_espelho_entries = [e for e in entries if str(e.get("id", "")) in matched_ids]
    espelho_to_save = {"updated_at": datetime.now(timezone.utc).isoformat(), "channels": clean_espelho_entries}
    (OUTPUT_DIR / "espelho.json").write_text(json.dumps(espelho_to_save, ensure_ascii=False, indent=2), "utf-8")

    timings["matching_consenso_validacao"] = round(time.perf_counter() - stage_start, 3)

    stage_start = time.perf_counter()
    programme_count, ids_count = write_epg_stream(selected, now, overrides)
    save_metadata_cache()
    timings["serializacao_e_gzip"] = round(time.perf_counter() - stage_start, 3)

    source_counts = {source.label: sum(1 for row in report_rows if row.get("source") == source.label) for source in active_sources}
    counts = {
        "playlist_channels": len(entries),
        "matched": sum(1 for row in report_rows if row["status"] == "matched"),
        "missing": sum(1 for row in report_rows if row["status"] == "missing"),
        "programmes": programme_count,
        "exported_ids": ids_count,
        "web_metadata_cached": len(_METADATA_CACHE),
        "timing_seconds": timings, "selected_by_source": source_counts,
    }

    (OUTPUT_DIR / "report.json").write_text(json.dumps({"summary": counts, "source_errors": source_errors, "channels": report_rows}, ensure_ascii=False, indent=2) + "\n", "utf-8")
    (OUTPUT_DIR / "validation.json").write_text(json.dumps({"generated_at": now.isoformat(), "active_sources": [s.label for s in active_sources], "summary": counts, "channels": validations}, ensure_ascii=False, indent=2) + "\n", "utf-8")

    report_md = [
        "# Relatório do EPG Universal — Metadados Completos (Ano, Classificação e Gênero)", "",
        f"- Emissoras base consolidadas: **{counts['playlist_channels']}**",
        f"- Emissoras com grade encontrada: **{counts['matched']}**",
        f"- Canais XML gerados: **{counts['exported_ids']}**",
        f"- Total de programas gravados: **{counts['programmes']}**",
        f"- Títulos no Cache Web (Wikipedia/iTunes BR): **{counts['web_metadata_cached']}**",
        "", "## Fonte base utilizada por canal", ""
    ]
    for source in active_sources: report_md.append(f"- **{source.label}:** {source_counts[source.label]} canal(is)")
    report_md.extend(["", "## Desempenho", ""])
    for label, seconds in timings.items(): report_md.append(f"- **{label.replace('_', ' ').title()}:** {seconds:.2f} s")
    report_md.append(f"- **Tempo Total:** {time.perf_counter() - run_start:.2f} s")
    (OUTPUT_DIR / "report.md").write_text("\n".join(report_md) + "\n", "utf-8")
    
    val_md = ["# Validação e Consenso de Grade\n"]
    for v in validations:
        now_title = v['now']['title'] if v.get('now') else 'Sem programação'
        val_md.append(f"- **{v['name']}** ({v['source']}): Agora **{now_title}** - Confiança **{v['confidence']}%**")
    (OUTPUT_DIR / "validation.md").write_text("\n".join(val_md) + "\n", "utf-8")

    print(f"Sucesso! EPG gerado com {programme_count} programas em {ids_count} canais base.")

if __name__ == "__main__":
    main()
