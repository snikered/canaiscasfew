#!/usr/bin/env python3
"""EPG Universal Consolidado — 100% Mérito Dinâmico por Canal e por Período do Dia (Sem Prioridade Fixa)
- Seleciona a melhor fonte de cada canal puramente por Precisão de Horário (Consenso) + Conteúdo (Richness) + Cobertura.
- Recorte e Costura por Janelas de 6h (Time-Window Splicing): se a Fonte A é melhor até metade do dia e a Fonte B é
  melhor no restante do dia/semana, funde os dois períodos automaticamente sem sobreposição.
- Exporta estritamente 1 <channel> por emissora real (elimina a duplicação de exported_ids).
- Mantém todos os aliases reais da M3U (incluindo FHD [H265], FHD H265, FHDR, old_tvg_ids) em <display-name>.
- Bloqueia colisões entre canais estrangeiros (PT/US/UK/CO/ARG/MX/EC/PY) e redes brasileiras.
- Impede falsos positivos regionais (BAND SP vs BAND RJ, RECORD SP vs BAHIA, JI-PARANA vs PARANA, RIO PRETO vs RJ).
"""
from __future__ import annotations

import copy
import glob
import gzip
import html
import json
import os
import re
import shutil
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

_QUALITY = re.compile(
    r"\b(SD|HD|FHD|FHDR|HDR|UHD|4K|8K|H\.?264|H\.?265|HEVC|AVC|1080P|1080I|720P|60FPS|BR\d*|SRV\d*|SERVER\s*\d*|OP[CÇ][AÃ]O\s*\d*|ALT|ALTERNATIVO|BACKUP|BKP|LOW|MED|HIGH|PLUS\s*HD|HQ|P)\b[\*\+]*",
    re.IGNORECASE,
)

_FOREIGN_COUNTRY_PREFIX_RE = re.compile(
    r"^(?:PT|US|USA|UK|CO|ARG|AR|MX|EC|PY|UY|CL|PE|BOL|VEN|DE|ES|FR|IT|CA|JP)\b",
    re.IGNORECASE,
)


def extract_foreign_country_code(text: str) -> str:
    """Retorna o prefixo de país estrangeiro (ex: PT, US, CO, ARG) ou string vazia se for canal brasileiro/geral."""
    if not text:
        return ""
    s = html.unescape(str(text)).strip().upper()
    s = re.sub(r"^[^A-Z0-9]+", "", s)
    if s.startswith(("USA NETWORK", "USA HD", "USA FHD", "GB USA")):
        return ""
    m = _FOREIGN_COUNTRY_PREFIX_RE.match(s)
    if not m:
        return ""
    code = m.group(0).upper()
    if code == "USA":
        return "US"
    if code == "AR":
        return "ARG"
    return code


def name_key(value: str) -> str:
    """Chave de comparação: sem acento, sem sufixo .br, sem BR2 grudado e sem qualidade/tags."""
    s = html.unescape(str(value or ""))
    s = re.sub(r"\.(?:br|us|uk|pt|ar|mx|cl|co|pe|latam|int|bt)$", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"(?<=[A-Za-z0-9])(?:BR\d+|SRV\d+)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", " ", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"\[\s*(\d+)\s*\]", r" \1 ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"[¹²³⁴*]+", " ", s)
    s = s.replace("&", " AND ")
    s = re.sub(r"[()\-–—_./|,:;]", " ", s)
    s = _QUALITY.sub(" ", s)
    s = re.sub(r"[^A-Z0-9+ ]", " ", s)
    s = re.sub(r"\b0+(\d+)\b", r"\1", s)
    return " ".join(s.split())


def extract_significant_digits(text: str) -> list[str]:
    """Extrai números significativos do nome do canal (ignorando H264, H265, 4K, 8K, 1080p, 720p, 60fps, 24h, BR2)."""
    if not text:
        return []
    s = html.unescape(str(text)).upper()
    s = re.sub(r"(?<=[A-Z0-9])(?:BR\d+|SRV\d+)\b", " ", s)
    s = re.sub(r"\b(?:H\.?264|H\.?265|1080[PI]?|720P?|60FPS|4K|8K|24H[S]?|24/7|BR\d+|SRV\d+|SERVER\s*\d+|OP[CÇ][AÃ]O\s*\d+)\b", " ", s)
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", " ", s)
    return [str(int(d)) for d in re.findall(r"\d+", s)]


def has_conflicting_channel_number(base_channel_name: str, candidate_text: str) -> bool:
    """Evita que 'HBO [2]' ou 'HBO 2' colida com 'HBO' principal (e vice-versa)."""
    base_digits = extract_significant_digits(base_channel_name)
    cand_digits = extract_significant_digits(candidate_text)
    if base_digits != cand_digits:
        b_up = base_channel_name.upper()
        if ("PREMIERE" in b_up or "SPORTV" in b_up or "ESPN" in b_up) and (
            (not base_digits and cand_digits == ["1"]) or (base_digits == ["1"] and not cand_digits)
        ):
            return False
        return True
    return False


_STATE_TOKEN_MAP = {
    "SP": "SP", "SAOPAULO": "SP", "CAMPINAS": "SP", "RIBEIRAOPRETO": "SP", "BAURU": "SP", "SOROCABA": "SP", "SANTOS": "SP", "TAUBATE": "SP", "SJCAMPOS": "SP", "RIOPRETO": "SP", "SJRIOPRETO": "SP", "SAOJOSEDORIOPRETO": "SP", "MOGIDASCRUZES": "SP", "ITAPETININGA": "SP", "PRUDENTE": "SP", "FRANCA": "SP", "ARARAQUARA": "SP", "SAOCARLOS": "SP",
    "RJ": "RJ", "RIO": "RJ", "RIODEJANEIRO": "RJ", "RIOSUL": "RJ", "SERRAMAR": "RJ",
    "MG": "MG", "MINAS": "MG", "BELOHORIZONTE": "MG", "JUIZDEFORA": "MG", "UBERLANDIA": "MG", "UBERABA": "MG", "ARAXA": "MG", "ALTEROSA": "MG", "SULDEMINAS": "MG", "GRANDEMINAS": "MG",
    "RS": "RS", "RIOGRANDEDOSUL": "RS", "PORTOALEGRE": "RS", "POA": "RS", "PELOTAS": "RS", "CAXIAS": "RS", "PASSOFUNDO": "RS", "PAMPA": "RS", "SANTAMARIA": "RS", "BAGE": "RS",
    "PR": "PR", "PARANA": "PR", "CURITIBA": "PR", "LONDRINA": "PR", "MARINGA": "PR", "CASCAVEL": "PR", "FOZDOIGUACU": "PR", "PARANAVAI": "PR", "PONTAGROSSA": "PR", "REDEMASSA": "PR",
    "SC": "SC", "SANTACATARINA": "SC", "FLORIANOPOLIS": "SC", "JOINVILLE": "SC", "BLUMENAU": "SC", "CHAPECO": "SC", "CRICIUMA": "SC",
    "BA": "BA", "BAHIA": "BA", "SALVADOR": "BA", "ITAPOAN": "BA", "ARATU": "BA", "SANTACRUZ": "BA", "SUBAE": "BA", "SUDOESTE": "BA",
    "PE": "PE", "PERNAMBUCO": "PE", "RECIFE": "PE", "ASABRANCA": "PE", "GRANDERIO": "PE", "GUARARAPES": "PE", "PETROLINA": "PE", "CARUARU": "PE",
    "CE": "CE", "CEARA": "CE", "FORTALEZA": "CE", "VERDESMARES": "CE", "JANGADEIRO": "CE", "CARIRI": "CE",
    "GO": "GO", "GOIAS": "GO", "GOIANIA": "GO", "ANHANGUERA": "GO", "SERRADOURADA": "GO", "RIOVERDE": "GO", "ANAPOLIS": "GO", "CATALAO": "GO", "ITUMBIARA": "GO", "JATAI": "GO", "LUZIANIA": "GO", "PORANGATU": "GO",
    "DF": "DF", "BRASILIA": "DF",
    "ES": "ES", "ESPIRITOSANTO": "ES", "VITORIA": "ES", "GAZETASUL": "ES",
    "PA": "PA", "PARA": "PA", "BELEM": "PA", "LIBERAL": "PA", "TAPAJOS": "PA",
    "AM": "AM", "AMAZONAS": "AM", "MANAUS": "AM",
    "MA": "MA", "MARANHAO": "MA", "SAOLUIS": "MA", "SAOLUIZ": "MA", "MIRANTE": "MA", "DIFUSORA": "MA", "BALSAS": "MA", "IMPERATRIZ": "MA",
    "MT": "MT", "MATOGROSSO": "MT", "CUIABA": "MT", "CENTROAMERICA": "MT", "TANGARADASERRA": "MT",
    "MS": "MS", "MATOGROSSODOSUL": "MS", "CAMPOGRANDE": "MS", "MORENA": "MS", "CORUMBA": "MS", "PONTAPORA": "MS",
    "PB": "PB", "PARAIBA": "PB", "JOAOPESSOA": "PB", "CABOBRANCO": "PB", "TAMBAU": "PB", "CORREIO": "PB", "MANAIRA": "PB",
    "RN": "RN", "RIOGRANDEDONORTE": "RN", "NATAL": "RN", "CABUGI": "RN", "TROPICAL": "RN",
    "AL": "AL", "ALAGOAS": "AL", "MACEIO": "AL", "PONTAVERDE": "AL",
    "SE": "SE", "SERGIPE": "SE", "ARACAJU": "SE", "ATALAIA": "SE",
    "PI": "PI", "PIAUI": "PI", "TERESINA": "PI", "CIDADEVERDE": "PI", "PICOS": "PI",
    "TO": "TO", "TOCANTINS": "TO", "PALMAS": "TO", "ARAGUAINA": "TO", "GURUPI": "TO",
    "RO": "RO", "RONDONIA": "RO", "PORTOVELHO": "RO", "JIPARANA": "RO", "ARIQUEMES": "RO", "ARQUEMES": "RO", "CACOAL": "RO", "VILHENA": "RO",
    "AC": "AC", "ACRE": "AC", "RIOBRANCO": "AC", "CRUZEIRODOSUL": "AC",
    "AP": "AP", "AMAPA": "AP", "MACAPA": "AP",
    "RR": "RR", "RORAIMA": "RR", "BOAVISTA": "RR", "RORAINOPOLIS": "RR",
}


def extract_state_ufs(text: str) -> set[str]:
    """Identifica a(s) UF(s) explícita(s) de um nome de canal regional para evitar mistura de estados (ex: SP vs RJ/BA/PR, JI-PARANA vs PR, RIO PRETO vs RJ)."""
    nk = name_key(text)
    words = nk.split()
    joined = "".join(words)
    ufs: set[str] = set()

    # Cidades/Estados compostos com "RIO" que NÃO pertencem ao RJ
    if "RIOPRETO" in joined or "SJRIOPRETO" in joined:
        ufs.add("SP")
    if "RIOVERDE" in joined:
        ufs.add("GO")
    if "RIOBRANCO" in joined:
        ufs.add("AC")
    if "RIOGRANDEDOSUL" in joined:
        ufs.add("RS")
    if "RIOGRANDEDONORTE" in joined:
        ufs.add("RN")
    if "GRANDERIO" in joined:
        ufs.add("PE")

    if "JIPARANA" in joined:
        ufs.add("RO")
    elif "PARANAIBA" in joined:
        ufs.add("MG")
    elif "PARANAVAI" in joined or "PARANA" in words:
        ufs.add("PR")

    if "PARAIBA" in joined:
        ufs.add("PB")
    elif "PARAUAPEBAS" in joined or "PARAGOMINAS" in joined or "PARA" in words:
        ufs.add("PA")

    for w in words:
        if w in {"PARA", "PARANA"}:
            continue
        if w == "RIO" and any(x in joined for x in ("RIOPRETO", "RIOVERDE", "RIOBRANCO", "RIOGRANDE", "GRANDERIO")):
            continue
        if w in _STATE_TOKEN_MAP:
            ufs.add(_STATE_TOKEN_MAP[w])

    for k, uf_val in _STATE_TOKEN_MAP.items():
        if len(k) >= 6 and k in joined:
            if k == "PARANA" and ("JIPARANA" in joined or "PARANAIBA" in joined):
                continue
            ufs.add(uf_val)
    return ufs


def has_conflicting_region(base_channel_name: str, candidate_text: str) -> bool:
    """Retorna True se o canal base e o alias/candidato tiverem estados (UFs) conflitantes ou países diferentes."""
    c_base = extract_foreign_country_code(base_channel_name)
    c_cand = extract_foreign_country_code(candidate_text)
    if c_base != c_cand and (c_base or c_cand):
        return True
    uf_base = extract_state_ufs(base_channel_name)
    uf_cand = extract_state_ufs(candidate_text)
    if uf_base and uf_cand and uf_base.isdisjoint(uf_cand):
        return True
    return False


def dedupe_display_names(
    names_by_id: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Devolve novo dict {id: [nomes]} em que cada name_key tem um único dono, mantendo todas as variantes H265/FHD do vencedor."""
    owners: dict[str, list[str]] = {}
    bases_per_id: dict[str, set[str]] = {}
    for cid, names in names_by_id.items():
        bases = {name_key(n) for n in names if name_key(n)}
        bases_per_id[cid] = bases
        for b in bases:
            if cid not in owners.setdefault(b, []):
                owners[b].append(cid)

    winner: dict[str, str] = {}
    for base, ids in owners.items():
        if len(ids) == 1:
            winner[base] = ids[0]
            continue
        exact = sorted(i for i in ids if name_key(i) == base)
        if exact:
            winner[base] = exact[0]
            continue
        winner[base] = sorted(ids, key=lambda i: (len(bases_per_id[i]), i))[0]

    out: dict[str, list[str]] = {}
    for cid, names in names_by_id.items():
        kept, seen = [], set()
        for n in names:
            n_clean = n.strip()
            if not n_clean:
                continue
            b = name_key(n_clean)
            if not b or winner.get(b) == cid:
                if n_clean.upper() not in seen:
                    kept.append(n_clean)
                    seen.add(n_clean.upper())
        out[cid] = kept
    return out


CATALOG_PATH = Path(os.environ.get("CHANNELS_FILE", "channels.json"))
OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_FILE", "overrides.json"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
ESPELHO_PATH = Path("espelho.json")

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

IPTV_USER_AGENTS = [
    "TiviMate/4.7.0 (Android 12)",
    "IPTVSmartersPlayer",
    "VLC/3.0.18 LibVLC/3.0.18",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]

# Pesos nivelados (1.0): nenhuma fonte ganha por "nome/prioridade fixa", mas sim pelo mérito da grade no canal/horário!
EPG_SOURCES_CONFIG = [
    ("Afiliadas Locais", "epg_afiliadas.xml", "afiliadas_locais", 1.0),
    ("Vivo Play API", "vivo_epg.xml.gz", "vivo_api", 1.0),
    ("Claro TV+ API", "claro_epg.xml.gz", "claro_api", 1.0),
    ("Claro Git", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/claro.xml", "git_operadora", 1.0),
    ("GuiaDeTV", "epg_guiadetv.xml", "guiadetv", 1.0),
    ("Meu Guia", "epg_meuguia.xml", "meuguia", 1.0),
    ("MiTV", "epg_mitv.xml", "mitv", 1.0),
    ("TV Plus", "epg_tvplus.xml", "tvplus", 1.0),
    ("WebFinal", "epg_webfinal.xml", "webfinal", 1.0),
    ("Open-EPG BR", "https://www.open-epg.com/files/brazil.xml", "openepg", 1.0),
    ("Open-EPG BR 4", "https://www.open-epg.com/files/brazil4.xml", "openepg", 1.0),
    ("Globo", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/globo.xml", "git_extra", 1.0),
    ("Mais SBT", "https://raw.githubusercontent.com/limaalef/BrazilTVEPG/refs/heads/main/maissbt.xml", "git_extra", 1.0),
    ("EPGShare BR1", "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz", "epgshare", 1.0),
    ("EPGShare BR2", "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz", "epgshare", 1.0),
    ("IPTV-EPG BR", "https://iptv-epg.org/files/epg-br.xml", "iptvepg", 1.0),
    ("Genius", os.environ.get("PRIMARY_EPG_URL", "https://github.com/ferteque/Curated-M3U-Repository/raw/refs/heads/main/epg13.xml.gz"), "genius", 1.0),
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
    "NO DATA", "NODATA", "NO DATA AVAILABLE", "NO EPG", "NO EPG AVAILABLE", "EPG NOT AVAILABLE",
    "NOT AVAILABLE", "UNAVAILABLE", "UNKNOWN", "TBD", "N A", "NA",
    "SEM DADOS", "SEM INFORMACAO", "SEM INFORMACOES", "INFORMACAO INDISPONIVEL",
    "PROGRAMA INDISPONIVEL", "DADOS INDISPONIVEIS", "FORA DO AR", "FORA DE AR",
}

NON_SERIES_CHANNEL_KEYWORDS = (
    "TELECINE",
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
    "CNN",
    "JOVEM PAN",
    "JP NEWS",
    "RECORD NEWS",
    "BAND NEWS",
    "BANDNEWS",
    "SBT NEWS",
    "BM&C",
    "TV SENADO",
    "TV CAMARA",
    "TV JUSTICA",
    "AGROMAIS",
    "CANAL RURAL",
    "CANAL DO BOI",
    "TERRA VIVA",
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
    " EPISODIO",
    " EPISÓDIO",
    "ANIME",
    "DESENHO",
)

RATING_SHORT_MAP = {
    "L": "🟢 LIVRE",
    "LIVRE": "🟢 LIVRE",
    "0": "🟢 LIVRE",
    "G": "🟢 LIVRE",
    "ALL": "🟢 LIVRE",
    "10": "🔵 +10",
    "PG-10": "🔵 +10",
    "A10": "🔵 +10",
    "12": "🟡 +12",
    "PG-12": "🟡 +12",
    "A12": "🟡 +12",
    "14": "🟠 +14",
    "PG-14": "🟠 +14",
    "A14": "🟠 +14",
    "16": "🔴 +16",
    "PG-16": "🔴 +16",
    "A16": "🔴 +16",
    "18": "⚫ +18",
    "PG-18": "⚫ +18",
    "A18": "⚫ +18",
    "R": "⚫ +18",
}

def normalize_rating_badge(raw_val: str) -> str:
    if not raw_val:
        return ""
    r = str(raw_val).strip().upper()
    if "LIVRE" in r or r in {"L", "0", "G", "ALL"}:
        return "🟢 LIVRE"
    m = re.search(r"(?:\+|\b)(10|12|14|16|18)\b", r)
    if m:
        return RATING_SHORT_MAP[m.group(1)]
    return RATING_SHORT_MAP.get(r, "")


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
    source: EpgSource; channel_id: str; method: str; match_score: float; tier: int = 1

@dataclass
class CandidateEvaluation:
    match: ChannelMatch; support_votes: int; vote_sources: int; support_weight: float; vote_weight: float
    support_families: int; weighted_ratio: float; avg_support_agreement: float; health_score: float
    richness_score: float; health: dict[str, object]; confidence: int; comparisons: list[dict[str, object]]

CHANNEL_SYNONYMS = {
    "E": ["E!", "E! ENTERTAINMENT", "E ENTERTAINMENT"],
    "E!": ["E", "E! ENTERTAINMENT", "E ENTERTAINMENT"],
    "HOME & HEALTH": ["DISCOVERY HOME & HEALTH", "DISCOVERY HOME AND HEALTH"],
    "HOME AND HEALTH": ["DISCOVERY HOME & HEALTH", "DISCOVERY HOME AND HEALTH"],
    "DISCOVERY HOME & HEALTH": ["HOME & HEALTH", "HOME AND HEALTH"],
    "DISNEY PLUS 1": ["DISNEY PLUS ESPN", "DISNEY PLUS ESPN 1", "DISNEY+ 1", "DISNEY+ ESPN 1"],
    "DISNEY PLUS 2": ["DISNEY PLUS ESPN 2", "DISNEY+ 2", "DISNEY+ ESPN 2"],
    "DISNEY PLUS 3": ["DISNEY PLUS ESPN 3", "DISNEY+ 3", "DISNEY+ ESPN 3"],
    "DISNEY PLUS 4": ["DISNEY PLUS ESPN 4", "DISNEY+ 4", "DISNEY+ ESPN 4"],
    "DISNEY PLUS 5": ["DISNEY PLUS ESPN 5", "DISNEY+ 5", "DISNEY+ ESPN 5"],
    "DISNEY PLUS 6": ["DISNEY PLUS ESPN 6", "DISNEY+ 6", "DISNEY+ ESPN 6"],
    "DISNEY PLUS 7": ["DISNEY PLUS ESPN 7", "DISNEY+ 7", "DISNEY+ ESPN 7"],
    "RBS CAXIAS": ["RBS CAXIAS DO SUL", "RBS TV CAXIAS DO SUL", "GLOBO RBS TV POA"],
    "RBS PASSO FUNDO GLOBO": ["RBS TV PASSO FUNDO", "GLOBO RBS PASSO FUNDO", "GLOBO RBS TV POA"],
    "TV FRONTEIRA": ["TV FRONTEIRA P PRUDENTE", "GLOBO FRONTEIRA PAULISTA", "GLOBO TV FRONTEIRA", "GLOBO SP"],
    "TV SANTA CRUZ BAHIA": ["TV SANTA CRUZ", "GLOBO SANTA CRUZ", "GLOBO TV SANTA CRUZ ITABUNA BAHIA"],
    "WOHOO": ["WOOHOO", "CANAL WOOHOO"],
    "WOOHOO": ["WOHOO", "CANAL WOOHOO"],
    "CANAL WOOHOO": ["WOOHOO", "WOHOO"],
    "GLOBO SP": ["GLOBO SAO PAULO", "GLOBO SÃO PAULO", "TV GLOBO SP", "TV GLOBO SAO PAULO"],
    "GLOBO SAO PAULO": ["GLOBO SP", "GLOBO SÃO PAULO", "TV GLOBO SP"],
    "GLOBO RJ": ["GLOBO RIO DE JANEIRO", "GLOBO RIO", "TV GLOBO RJ", "TV GLOBO RIO"],
    "GLOBO RIO DE JANEIRO": ["GLOBO RJ", "GLOBO RIO", "TV GLOBO RJ"],
    "GLOBO RIO": ["GLOBO RJ", "GLOBO RIO DE JANEIRO"],
    "GLOBO NORDESTE": ["GLOBO RECIFE", "GLOBO PERNAMBUCO", "GLOBO PE"],
    "GLOBO RECIFE": ["GLOBO NORDESTE", "GLOBO PERNAMBUCO", "GLOBO PE"],
    "GLOBO REDE BAHIA": ["TV BAHIA", "GLOBO BAHIA", "GLOBO SALVADOR", "REDE BAHIA"],
    "TV BAHIA": ["GLOBO REDE BAHIA", "GLOBO BAHIA", "GLOBO SALVADOR", "REDE BAHIA"],
    "GLOBO BAHIA": ["GLOBO REDE BAHIA", "TV BAHIA", "GLOBO SALVADOR"],
    "GLOBO MARANHAO": ["TV MIRANTE", "GLOBO TV MIRANTE", "GLOBO SAO LUIS", "GLOBO MA"],
    "TV MIRANTE": ["GLOBO MARANHAO", "GLOBO MARANHÃO", "GLOBO SAO LUIS"],
    "GLOBO PE ASA BRANCA": ["TV ASA BRANCA", "GLOBO ASA BRANCA", "GLOBO TV ASA BRANCA", "GLOBO CARUARU"],
    "GLOBO ASA BRANCA": ["TV ASA BRANCA", "GLOBO PE ASA BRANCA", "GLOBO TV ASA BRANCA", "GLOBO CARUARU"],
    "TV ASA BRANCA": ["GLOBO ASA BRANCA", "GLOBO PE ASA BRANCA", "GLOBO TV ASA BRANCA"],
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
    "REDE TV": ["REDETV!", "REDETV", "REDE TV!"],
    "REDETV": ["REDE TV", "REDETV!", "REDE TV!"],
    "TV GAZETA SP": ["TV GAZETA", "GAZETA", "REDE GAZETA"],
    "TV GAZETA": ["TV GAZETA SP", "GAZETA"],
    "TV PAMPA RS": ["TV PAMPA", "PAMPA", "REDE PAMPA"],
    "TV PAMPA": ["TV PAMPA RS", "PAMPA", "REDE PAMPA"],
    "CNN INTERNACIONAL": ["CNN INTERNATIONAL"],
    "CNN INTERNATIONAL": ["CNN INTERNACIONAL"],
    "JOVEM PAN": ["JOVEM PAN NEWS", "JP NEWS"],
    "JOVEM PAN NEWS": ["JOVEM PAN", "JP NEWS"],
    "PRIME BOX BRAZIL": ["PRIME BOX BRASIL"],
    "PRIME BOX BRASIL": ["PRIME BOX BRAZIL"],
    "TRAVEL BOX BRAZIL": ["TRAVEL BOX BRASIL"],
    "TRAVEL BOX BRASIL": ["TRAVEL BOX BRAZIL"],
    "MUSIC BOX BRAZIL": ["MUSIC BOX BRASIL"],
    "MUSIC BOX BRASIL": ["MUSIC BOX BRAZIL"],
    "CARTOONITO": ["CARTOONITO", "BOOMERANG"],
    "SPORTTV": ["SPORTV", "SPORTV 1"],
    "SPORTV": ["SPORTV", "SPORTV 1"],
    "SPORTV 1": ["SPORTV", "SPORTTV"],
    "SPORTV 2": ["SPORTV 2"],
    "SPORTV 3": ["SPORTV 3"],
    "ESPN": ["ESPN 1", "ESPN BRASIL"],
    "ESPN 1": ["ESPN", "ESPN BRASIL"],
    "ESPN 4": ["FOX SPORTS"],
    "ESPN 5": ["FOX SPORTS 2"],
    "ESPN 6": ["ESPN EXTRA"],
    "PREMIERE 1": ["PREMIERE CLUBES", "PREMIERE"],
    "PREMIERE CLUBES": ["PREMIERE 1", "PREMIERE"],
    "GLOBOPLAY NOVELAS": ["VIVA", "CANAL VIVA", "GLOBO NOVELAS"],
    "GLOBO NOVELAS": ["VIVA", "CANAL VIVA", "GLOBOPLAY NOVELAS"],
    "VIVA": ["CANAL VIVA", "GLOBOPLAY NOVELAS"],
    "CANAL OFF": ["OFF"],
    "OFF": ["CANAL OFF"],
    "CANAL FUTURA": ["FUTURA"],
    "FUTURA": ["CANAL FUTURA"],
    "TV CULTURA": ["CULTURA"],
    "CULTURA": ["TV CULTURA"],
    "TV BRASIL": ["TV BRASIL", "EBC", "TVE BRASIL"],
    "TV APARECIDA": ["APARECIDA", "REDE APARECIDA"],
    "CANCAO NOVA": ["TV CANCAO NOVA"],
    "REDE VIDA": ["REDEVIDA", "TV VIDA"],
    "REDE GOSPEL": ["TV GOSPEL"],
    "TV PAI ETERNO": ["PAI ETERNO"],
    "PAI ETERNO": ["TV PAI ETERNO"],
    "TV NOVO TEMPO": ["NOVO TEMPO"],
    "NOVO TEMPO": ["TV NOVO TEMPO"],
    "TV EVANGELIZAR": ["EVANGELIZAR", "REDE EVANGELIZAR"],
    "TV DIARIO": ["TV DIARIO FORTALEZA", "TV DIARIO CE"],
    "REDE CNT": ["CNT", "TV CNT"],
    "REDE BRASIL": ["RBTV", "REDE BRASIL DE TELEVISAO"],
    "REDE SUPER": ["TV SUPER"],
    "TVE": ["TVE RS", "TVE BAHIA"],
    "TIMES BRASIL": ["TIMES BRASIL CNBC", "CNBC", "CNBC BRASIL"],
    "TIMES BRASIL CNBC": ["TIMES BRASIL", "CNBC", "CNBC BRASIL"],
    "ADULT SWIM": ["ADULT SWIN"],
    "ADULT SWIN": ["ADULT SWIM"],
}

UF_TO_STATE_AND_CAPITAL = {
    "SP": ["SAO PAULO", "SP"],
    "RJ": ["RIO DE JANEIRO", "RIO", "RJ"],
    "MG": ["MINAS", "MINAS GERAIS", "BELO HORIZONTE", "BH", "MG"],
    "RS": ["RIO GRANDE DO SUL", "PORTO ALEGRE", "POA", "RS"],
    "PR": ["PARANA", "CURITIBA", "PR"],
    "SC": ["SANTA CATARINA", "FLORIANOPOLIS", "SC"],
    "BA": ["BAHIA", "SALVADOR", "TV BAHIA", "REDE BAHIA", "BA"],
    "PE": ["PERNAMBUCO", "RECIFE", "GLOBO NORDESTE", "PE"],
    "CE": ["CEARA", "FORTALEZA", "TV VERDES MARES", "VERDES MARES", "CE"],
    "PA": ["PARA", "BELEM", "TV LIBERAL", "PA"],
    "MA": ["MARANHAO", "SAO LUIS", "TV MIRANTE", "MA"],
    "GO": ["GOIAS", "GOIANIA", "TV ANHANGUERA", "ANHANGUERA", "GO"],
    "DF": ["BRASILIA", "DISTRITO FEDERAL", "DF"],
    "AM": ["AMAZONAS", "MANAUS", "REDE AMAZONICA", "TV AMAZONAS", "AM"],
    "ES": ["ESPIRITO SANTO", "VITORIA", "TV GAZETA ES", "TV GAZETA VITORIA", "ES"],
    "PB": ["PARAIBA", "JOAO PESSOA", "TV CABO BRANCO", "TV PARAIBA", "PB"],
    "RN": ["RIO GRANDE DO NORTE", "NATAL", "INTER TV CABUGI", "RN"],
    "AL": ["ALAGOAS", "MACEIO", "TV GAZETA DE ALAGOAS", "TV GAZETA ALAGOAS", "AL"],
    "SE": ["SERGIPE", "ARACAJU", "TV SERGIPE", "SE"],
    "PI": ["PIAUI", "TERESINA", "TV CLUBE", "PI"],
    "MT": ["MATO GROSSO", "CUIABA", "TV CENTRO AMERICA", "MT"],
    "MS": ["MATO GROSSO DO SUL", "CAMPO GRANDE", "TV MORENA", "MS"],
    "TO": ["TOCANTINS", "PALMAS", "TV ANHANGUERA", "TO"],
    "RO": ["RONDONIA", "PORTO VELHO", "REDE AMAZONICA PORTO VELHO", "RO"],
    "AC": ["ACRE", "RIO BRANCO", "REDE AMAZONICA RIO BRANCO", "AC"],
    "AP": ["AMAPA", "MACAPA", "REDE AMAZONICA MACAPA", "AP"],
    "RR": ["RORAIMA", "BOA VISTA", "REDE AMAZONICA BOA VISTA", "RR"],
}

POISONED_OPEN_TV_IDS = {
    "globo", "tvglobo", "redeglobo", "globosp", "globorj", "globonacional", "globohd", "globofhd",
    "record", "recordtv", "rederecord", "recordsp", "recordtvsp", "recordrj", "recordtvrj",
    "recordtvbrasilia", "recordbrasilia", "recordnacional", "recordhd", "recordtvminas", "recordminas",
    "recordtvrs", "recordrs", "recordtvba", "recordbahia", "recordtvgoias", "recordgoias",
    "sbt", "sbtsp", "sbtrj", "sbtnacional", "tvsbt", "sbthd",
    "band", "bandtv", "tvband", "bandeirantes", "redebandeirantes", "bandsp", "bandrj", "bandhd",
    "redetv", "redetvsp", "tvbrasil", "tvcultura", "cultura",
    "aande", "ae", "agromais", "terraviva", "canalrural", "canaldoboi", "boomerang",
}

def strip_qualities_keep_case(name: str) -> str:
    """Remove apenas tags de qualidade/codec (FHD, FHDR, [H265], 4K, BR2 grudado, etc.), preservando numeração de canal como [2]."""
    if not name: return ""
    s = html.unescape(name)
    s = re.sub(
        r'\[\s*(?:SD|HD|FHD|FHDR|UHD|4K|8K|H\.?264|H\.?265|HEVC|AVC|HDR\+?|BR\d*|SRV\d*|SERVER\s*\d*|OP[CÇ][AÃ]O\s*\d*|BACKUP|BKP|ALT|ALTERNATIVO|1080P|720P|60FPS|DUAL|LEG|DUB|P)\s*\]',
        ' ',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r'\|.*?\|', ' ', s)
    s = re.sub(
        r'\(\s*(?:SD|HD|FHD|FHDR|UHD|4K|8K|H\.?264|H\.?265|HEVC|AVC|HDR\+?|BR\d*|SRV\d*|SERVER\s*\d*|OP[CÇ][AÃ]O\s*\d*|BACKUP|BKP|ALT|ALTERNATIVO|1080P|720P|60FPS|DUAL|LEG|DUB|EVENTOS?|PPV)\s*\)',
        ' ',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r'[()]', ' ', s)
    s = re.sub(r'[\¹\²\³\⁴\⁵\⁶\⁷\⁸\⁹\⁰\~]', ' ', s)
    # Remove BR2/SRV2 mesmo quando estiver colado no final da palavra (ex: GLOBORJBR2)
    s = re.sub(r'(?<=[A-Za-z0-9])(?:BR\d+|SRV\d+)\b', ' ', s, flags=re.IGNORECASE)
    tags = r'\b(SD|HD|FHD|FHDR|UHD|4K|8K|H\.?264|H\.?265|HEVC|AVC|HDR\+?|LEG|LEGENDADO|DUB|DUBLADO|DUAL|HQ|VIP|VOD|24H[S]?|24/7|1080P|1080I|720P|60FPS|PPV|EVENTOS?|ALTERNATIVO|ALT|BACKUP|BKP|BR\d+|SRV\d+|SERVER\s*\d+|OP[CÇ][AÃ]O\s*\d+)\b[\*\²\³\¹\⁴\+]*'
    s = re.sub(tags, ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'\bH\s+D\b', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'\[\s*(\d+)\s*\]', r' \1 ', s)
    s = re.sub(r'\[.*?\]', ' ', s)
    s = re.sub(r'[*=_\-–—/\\]', ' ', s)
    s = re.sub(r'\.\s*$', '', s)
    return ' '.join(s.split()).strip()

def normalizar_canal(nome: str) -> str:
    if not nome: return ""
    original_nome = html.unescape(nome)
    foreign_code = extract_foreign_country_code(original_nome)
    nome = strip_qualities_keep_case(original_nome)

    # Se o canal for estrangeiro (ex: PT, US, CO, ARG), não aplica substituições exclusivas de canais BR
    if not foreign_code:
        nome = re.sub(r'^E!$', 'E!', nome.strip(), flags=re.IGNORECASE)
        nome = re.sub(r'\bADULT SWIN\b', 'ADULT SWIM', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bZOOMOOO\b', 'ZOOMOO', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bWOHOO\b', 'WOOHOO', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bJP NEWS\b', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bJOVEM PAN\b(?! NEWS)', 'JOVEM PAN NEWS', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bFOX SPORTS 2\b', 'ESPN 5', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bFOX SPORTS\b', 'ESPN 4', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bESPN EXTRA\b', 'ESPN 6', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bESPN BRASIL\b', 'ESPN', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bESPN 1\b', 'ESPN', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bSPORTV 1\b', 'SPORTV', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bPREMIERE 1\b', 'PREMIERE CLUBES', nome, flags=re.IGNORECASE)
        nome = re.sub(r'^PREMIERE$', 'PREMIERE CLUBES', nome.strip(), flags=re.IGNORECASE)
        nome = re.sub(r'\bBOOMERANG\b', 'CARTOONITO', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bMAX PRIME\b', 'HBO POP', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bMAX UP\b', 'HBO POP', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bMAX\b(?!\s*(?:PRIME|\d))', 'HBO MUNDI', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bHBO\s*\+\b', 'HBO PLUS', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bH2\b', 'HISTORY 2', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bHISTORY CHANNEL\b', 'HISTORY', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bDISCOVERY H&H\b', 'DISCOVERY HOME & HEALTH', nome, flags=re.IGNORECASE)
        nome = re.sub(r'^HOME\s*(?:&|AND)\s*HEALTH$', 'DISCOVERY HOME & HEALTH', nome.strip(), flags=re.IGNORECASE)
        nome = re.sub(r'\bDISCOVERY HOME\s*(?:&|AND)?\s*HEALT[H]?\b', 'DISCOVERY HOME & HEALTH', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bDISCOVERY TLC\b', 'TLC', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bID\s+INVESTIGA[CÇ][AÃ]O DISCOVERY\b', 'INVESTIGACAO DISCOVERY', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bDISCOVERY ID\b', 'INVESTIGACAO DISCOVERY', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bFOX PREMIUM\b', 'STAR PREMIUM', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bFOX CHANNEL\b', 'STAR CHANNEL', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bFOX LIFE\b', 'STAR LIFE', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+ACTION\b', 'TELECINE ACTION', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+PREMIUM\b', 'TELECINE PREMIUM', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+PIPOCA\b', 'TELECINE PIPOCA', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+TOUCH\b', 'TELECINE TOUCH', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+FUN\b', 'TELECINE FUN', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bTC\s+CULT\b', 'TELECINE CULT', nome, flags=re.IGNORECASE)
        nome = re.sub(r'^GB\s+USA$', 'USA NETWORK', nome.strip(), flags=re.IGNORECASE)
        nome = re.sub(r'^GB\b', 'GLOBO', nome, flags=re.IGNORECASE)
        nome = re.sub(r'^RECORDTV\b', 'RECORD', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bRECORD\s+TV\b', 'RECORD', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bSBT\s+TV\b', 'SBT', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bBAND\s+TV\b', 'BAND', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bREDE\s+TV!?\b', 'REDETV', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bAGRO\s*\+\b', 'AGROMAIS', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\bSPORTTV\b', 'SPORTV', nome, flags=re.IGNORECASE)

    nome_limpo = ' '.join(nome.split()).strip().upper()
    return nome_limpo if len(nome_limpo) >= 1 else original_normalize_name(original_nome)

def normalize_name(value: str) -> str:
    return original_normalize_name(normalizar_canal(value))

def inferir_emissora_pelo_grupo(nome_normalizado: str, nome_grupo: str) -> str:
    if not nome_grupo: return nome_normalizado
    if extract_foreign_country_code(nome_normalizado):
        return nome_normalizado
    grupo = nome_grupo.upper()
    if "GLOBO" in grupo and not nome_normalizado.startswith("GLOBO") and " NEWS" not in nome_normalizado and "GLOOB" not in nome_normalizado:
        return f"GLOBO {nome_normalizado}"
    if "SBT" in grupo and not nome_normalizado.startswith("SBT") and not any(k in nome_normalizado for k in ("REDETV", "CULTURA", "BRASIL", "APARECIDA", "GAZETA")):
        return f"SBT {nome_normalizado}"
    if "RECORD" in grupo and not nome_normalizado.startswith("RECORD"): return f"RECORD {nome_normalizado}"
    if "BAND" in grupo and not nome_normalizado.startswith("BAND"): return f"BAND {nome_normalizado}"
    if "PREMIERE" in grupo and not nome_normalizado.startswith("PREMIERE") and len(nome_normalizado) <= 4: return f"PREMIERE {nome_normalizado}"
    return nome_normalizado

AUTO_POISONED_IDS: set[str] = set()

def clean_tvid_key(tvid: str) -> str:
    t = re.sub(r'\.(br|us|uk|pt|ar|mx|cl|co|pe|latam|int|bt)$', '', str(tvid).lower())
    t = re.sub(r'^(auto\.|yt\.|br\.|canais\.)', '', t)
    return re.sub(r'[^a-z0-9]', '', t)

def detect_poisoned_ids(pairs: Iterable[tuple[str, str]]) -> set[str]:
    bases: dict[str, set[frozenset]] = {}
    for tid, nm in pairs:
        k = clean_tvid_key(tid)
        toks = frozenset(t for t in name_key(nm).split() if t not in {"TV", "DE", "DO", "DA", "E"})
        if k and toks:
            bases.setdefault(k, set()).add(toks)
    out = set()
    for k, sets in bases.items():
        lst = list(sets)
        independent = [a for a in lst if not any(a < b for b in lst)]
        if len(independent) >= 2:
            out.add(k)
    return out

def is_valid_provider_tvid(channel_name: str, tvid: str) -> bool:
    if not tvid or len(tvid) < 1: return False
    if extract_foreign_country_code(channel_name) and str(tvid).lower().endswith(".br"):
        return False
    if has_conflicting_region(channel_name, tvid):
        return False
    tvid_clean = re.sub(r'\.(br|us|uk|pt|ar|mx|cl|co|pe|latam|int|bt)$', '', tvid.lower())
    tvid_clean = re.sub(r'^(auto\.|yt\.|br\.|canais\.)', '', tvid_clean)
    tvid_clean = re.sub(r'[^a-z0-9]', '', tvid_clean)
    if not tvid_clean: return False

    if tvid_clean in POISONED_OPEN_TV_IDS or tvid_clean in AUTO_POISONED_IDS:
        return False

    base_clean = normalizar_canal(channel_name)
    # Bloqueia IDs legados de canais que mudaram de programação (TruTV -> Adult Swim e TBS -> TNT Novelas)
    if "ADULT SWIM" in base_clean and "trutv" in tvid_clean:
        return False
    if "TNT NOVELAS" in base_clean and "tbs" in tvid_clean:
        return False

    if base_clean.startswith(("GLOBO ", "TV GLOBO", "RECORD ", "SBT ", "BAND ", "TV BAHIA", "TV ASA BRANCA", "TV VERDES MARES", "TV GAZETA", "TV ANHANGUERA", "TV LIBERAL", "TV MIRANTE", "RBS ", "RPC ", "NSC ", "EPTV")):
        if "NEWS" not in base_clean and "SPORTS" not in base_clean and "NOVELAS" not in base_clean:
            return False

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
    for bad in ("SEM PROGRAMACAO", "NAO DISPONIVEL", "PROGRAMACAO INDISPONIVEL", "NO DATA AVAILABLE", "NO EPG DATA"):
        if bad in norm_title:
            return False
    return True

def local_tag(element: ET.Element) -> str: return element.tag.rsplit("}", 1)[-1]
def format_xmltv_datetime(dt: datetime) -> str: return dt.astimezone(timezone(timedelta(hours=-3))).strftime("%Y%m%d%H%M%S -0300")

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
# EXTRAÇÃO E ENRIQUECIMENTO DE METADADOS (APENAS [T03E07] + VISUAL RICO)
# =====================================================================
_RE_XMLTV_NS = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*\.")
_RE_SE_PATTERNS = [
    re.compile(r"[\[\(\-\s]*\bS(\d{1,2})\s*[:\-., ]*\s*E(?:P|PIS[OÓ]DIO)?\.?\s*(\d{1,4})\b[\]\)]*", re.IGNORECASE),
    re.compile(r"[\[\(\-\s]*\bT(?:EMP(?:ORADA)?)?\s*\.?\s*(\d{1,2})\s*[\-:/, ]*\s*E(?:P(?:IS[OÓ]DIO)?)?\.?\s*(\d{1,4})\b[\]\)]*", re.IGNORECASE),
    re.compile(r"[\[\(\-\s]*\b(\d{1,2})\s*[ªº]\s*T(?:EMP(?:ORADA)?)?\s*[\-:/, ]*\s*E(?:P(?:IS[OÓ]DIO)?)?\.?\s*(\d{1,4})\b[\]\)]*", re.IGNORECASE),
]
_RE_EXPLICIT_EPISODE = re.compile(r"[\[\(\-\s]*\b(?:EPIS[OÓ]DIO|EP\.?)\s*(\d{1,4})\b[\]\)]*", re.IGNORECASE)
_RE_TRAILING_FAKE_EP = re.compile(r"(?:\s*[\-\|:]\s*|\s+)(?:\[|\()?E(?:P)?\.?\s*0*1(?:\]|\))?\s*$", re.IGNORECASE)

def clean_raw_title(raw_title: str) -> str:
    """Limpa repetições após ' : ' (ex: 'Jogo X - Copa : Jogo X') e sufixos falsos ' - Ep. 1' logo na leitura."""
    if not raw_title:
        return ""
    t = raw_title.strip()
    if " : " in t:
        parts = [p.strip() for p in t.split(" : ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            p0_up, p1_up = parts[0].upper(), parts[1].upper()
            if p0_up == p1_up or p1_up in p0_up:
                t = parts[0]
            elif p0_up in p1_up:
                t = parts[1]
    t = _RE_TRAILING_FAKE_EP.sub("", t).strip()
    return t

def strip_se_tokens(text: str) -> str:
    """Remove qualquer S01E02, T01E02, [T03E07], Ep. 2 ou cabeçalhos antigos duplicados do texto."""
    if not text:
        return ""
    cleaned_lines = []
    for ln in text.splitlines():
        ln_s = ln.strip()
        if any(ln_s.startswith(p) for p in ("🟢", "🔵", "🟡", "🟠", "🔴", "⚫", "📅", "🎭", "📝", "⏱")):
            continue
        if "Não recomendado para menores" in ln_s or "Livre para todos os públicos" in ln_s:
            continue
        cleaned_lines.append(ln_s)
    s = " ".join(x for x in cleaned_lines if x)
    for pat in _RE_SE_PATTERNS:
        s = pat.sub(" ", s)
    s = _RE_EXPLICIT_EPISODE.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—:|,;/[]()")
    return s

def is_non_series_channel(channel_name: str) -> bool:
    norm_ch = normalizar_canal(channel_name)
    return any(kw in norm_ch for kw in NON_SERIES_CHANNEL_KEYWORDS)

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

def extract_program_metadata(el: ET.Element, channel_name: str = "", duration_minutes: float = 0.0) -> dict[str, Any]:
    title = ""
    sub_title = ""
    desc = ""
    date_str = ""
    rating_badge = ""
    poster_icon = ""
    categories: list[str] = []
    season: int | None = None
    episode: int | None = None

    for child in el:
        tag = local_tag(child)
        txt = (child.text or "").strip()
        if tag == "icon":
            src_attr = (child.attrib.get("src") or "").strip()
            if src_attr and not poster_icon:
                poster_icon = src_attr
            continue
        if tag == "rating":
            val_node = child.find("value")
            if val_node is not None and val_node.text:
                b = normalize_rating_badge(val_node.text)
                if b:
                    rating_badge = b
            continue
        if not txt:
            continue
        if tag == "title" and not title:
            title = clean_raw_title(txt)
        elif tag == "sub-title" and not sub_title:
            sub_title = txt
        elif tag == "desc" and len(txt) > len(desc):
            desc = txt
        elif tag == "date" and not date_str:
            date_str = txt[:4]
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

    if not rating_badge and desc:
        if "LIVRE" in desc.upper():
            rating_badge = "🟢 LIVRE"
        else:
            m_age = re.search(r"(?:menores de|\+)\s*(10|12|14|16|18)\b", desc, flags=re.I)
            if m_age:
                rating_badge = RATING_SHORT_MAP.get(m_age.group(1), "")

    if " : " in title and not sub_title:
        parts = [p.strip() for p in title.split(" : ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            p0_up, p1_up = parts[0].upper(), parts[1].upper()
            if p0_up == p1_up or p1_up in p0_up:
                title = parts[0]
            elif p0_up in p1_up:
                title = parts[1]
            else:
                title, sub_title = parts[0], parts[1]

    title = _RE_TRAILING_FAKE_EP.sub("", title).strip()

    if season is None and episode is None:
        search_blob = f"{title} | {sub_title} | {desc}"
        for pat in _RE_SE_PATTERNS:
            m = pat.search(search_blob)
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

    title = strip_se_tokens(title) or title
    if sub_title:
        sub_title = strip_se_tokens(sub_title)
    if desc:
        desc = strip_se_tokens(desc)

    if sub_title and (
        sub_title.upper() == title.upper()
        or sub_title.upper() in title.upper()
        or _RE_TRAILING_FAKE_EP.fullmatch(sub_title)
    ):
        sub_title = ""

    if sub_title and desc and desc.lower().startswith(sub_title.lower()):
        desc = desc[len(sub_title):].lstrip(" -–—:|.,")

    return {
        "title": title,
        "sub_title": sub_title,
        "desc": desc,
        "season": season,
        "episode": episode,
        "categories": categories,
        "date": date_str,
        "rating_badge": rating_badge,
        "icon": poster_icon,
    }

def extract_epgshare_compound_candidates(channel_id: str) -> list[str]:
    extras = []
    if ".." not in channel_id:
        return extras
    loc_part, right_part = channel_id.split("..", 1)
    right_clean = re.sub(r'\.(br|us|uk|pt|ar|mx|cl|co|pe|latam|int)$', '', right_part, flags=re.IGNORECASE)
    right_clean = re.sub(r'[.\-–—_]+', ' ', right_clean).strip()
    if right_clean:
        extras.append(right_clean)
        for net in ("Globo", "SBT", "Record", "Band"):
            if right_clean.lower().startswith(net.lower() + " "):
                sub = right_clean[len(net):].strip()
                if sub:
                    extras.append(sub)
                city_uf = loc_part.replace(".", " ").replace("/", " ").strip()
                city_only = loc_part.split("/")[0].replace(".", " ").strip()
                if city_uf:
                    extras.append(f"{net} {city_uf}")
                    extras.append(f"{sub} {city_only}")
                if city_only:
                    extras.append(f"{net} {city_only}")
    return extras

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
            if re.search(r'\.(us|uk|ar|mx|cl|co|pe|es|fr|de|it)$', channel_id, flags=re.IGNORECASE):
                continue
            channels[channel_id] = child
            display_names = [(node.text or "").strip() for node in child if local_tag(node) == "display-name" and (node.text or "").strip()]
            names[channel_id] = display_names
            norm_candidates: list[str] = []
            clean_id_part = re.sub(r'^.*?\.\.', '', channel_id)
            clean_id_part = re.sub(r'\.(br|us|uk|pt|ar|mx|cl|co|pe|latam|int)$', '', clean_id_part, flags=re.IGNORECASE)
            clean_id_part = re.sub(r'[.\-–—_]+', ' ', clean_id_part)
            compound_extras = extract_epgshare_compound_candidates(channel_id)
            for candidate in [*display_names, channel_id, normalize_source_id(channel_id), clean_id_part, *compound_extras]:
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

    for channel_id in list(rows):
        own_names = {normalize_programme_title(n) for n in [*names.get(channel_id, []), channel_id]}
        own_names.discard("")
        ch_rows = rows[channel_id]
        if len(ch_rows) >= 5 and sum(1 for r in ch_rows if r.normalized_title in own_names) / len(ch_rows) >= 0.8:
            del rows[channel_id]; programs.pop(channel_id, None)
        elif len(ch_rows) < 2:
            del rows[channel_id]; programs.pop(channel_id, None)

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

_AFFILIATE_PATTERNS = (
    r'\b(EPTV|RBS(?:\s+TV)?|RPC(?:\s+TV)?|NSC(?:\s+TV)?|REDE\s+AMAZONICA|AMAZONICA|'
    r'TV\s+ANHANGUERA|ANHANGUERA|TV\s+LIBERAL|TV\s+MIRANTE|TV\s+VERDES\s+MARES|VERDES\s+MARES|'
    r'INTER\s*TV|TV\s+INTEGRACAO|INTEGRACAO|TV\s+TEM|TV\s+VANGUARDA|VANGUARDA|'
    r'TV\s+TRIBUNA|TV\s+GAZETA|TV\s+MORENA|TV\s+CENTRO\s+AMERICA|CENTRO\s+AMERICA|'
    r'TV\s+BAHIA|REDE\s+BAHIA|TV\s+SANTA\s+CRUZ|TV\s+SUBAE|TV\s+SUDOESTE|TV\s+OESTE|TV\s+SAO\s+FRANCISCO|'
    r'TV\s+ASA\s+BRANCA|ASA\s+BRANCA|TV\s+GRANDE\s+RIO|TV\s+CABO\s+BRANCO|TV\s+PARAIBA|'
    r'TV\s+SERGIPE|TV\s+CLUBE|TV\s+TAPAJOS|TV\s+RIO\s+SUL|RIO\s+SUL|TV\s+DIARIO|TV\s+FRONTEIRA|'
    r'REDE\s+MASSA|TV\s+JANGADEIRO|TV\s+JORNAL|TV\s+ALTEROSA|TV\s+ARATU|TV\s+SERRA\s+DOURADA|'
    r'TV\s+TAMBAU|TV\s+DIFUSORA|VTV|TV\s+CIDADE\s+VERDE|'
    r'TV\s+GUAIBA|RECORD\s+ITAPOAN|TV\s+ITAPOAN|RIC\s*TV|TV\s+CORREIO|TV\s+TROPICAL|TV\s+CIDADE|'
    r'TV\s+TAROBA|TV\s+MANAIRA)\b'
)

_NETWORK_PREFIXES = {
    "GLOBO": ("GLOBO", "TV GLOBO", "REDE GLOBO"),
    "SBT": ("SBT",),
    "RECORD": ("RECORD", "REDE RECORD"),
    "BAND": ("BAND", "TV BAND", "BANDEIRANTES", "REDE BANDEIRANTES"),
}
_GLOBO_ONLY_BRANDS = (
    "TV LIBERAL", "TV ASA BRANCA", "TV GRANDE RIO", "TV VERDES MARES", "TV MIRANTE",
    "TV CLUBE", "TV CABO BRANCO", "TV SERGIPE", "TV MORENA", "TV BAHIA", "REDE BAHIA",
    "REDE AMAZONICA", "TV AMAZONAS", "RBS TV", "RPC", "NSC TV", "EPTV", "TV TEM",
    "TV VANGUARDA", "REDE VANGUARDA", "TV INTEGRACAO", "INTER TV", "TV ANHANGUERA",
    "TV TAPAJOS", "TV CENTRO AMERICA", "TV GAZETA", "TV PARAIBA",
)
_UF_CODES = {"SP","RJ","RS","MG","BA","PR","PE","CE","PA","MA","SC","GO","PB","ES","AM","RN",
             "AL","PI","MT","DF","MS","SE","RO","TO","AC","AP","RR"}

def _alias_belongs_to_network(alias: str, network: str) -> bool:
    a = alias.upper().strip()
    for net, prefixes in _NETWORK_PREFIXES.items():
        if net != network and a.startswith(prefixes):
            return False
    if network != "GLOBO" and a.startswith(_GLOBO_ONLY_BRANDS):
        return False
    return True

def _network_of_normalized(value: str) -> str:
    if "GLOOB" in value or "GLOBONEWS" in value or "NOVELA" in value:
        return ""
    for net in ("GLOBO", "SBT", "RECORD", "BAND"):
        if value.startswith(net) or value.startswith(f"TV{net}") or value.startswith(f"REDE{net}"):
            return net
    return ""

_GLOBO_BRAND_COMPACT = tuple(b.replace(" ", "") for b in _GLOBO_ONLY_BRANDS) + ("NSCTV",)

def _is_foreign_prefix(alias: str) -> bool:
    """Identifica se o canal começa com prefixos internacionais (tanto espaçados quanto compactos) que não devem casar com canais do Brasil."""
    a = alias.upper().strip()
    if a.startswith(("PT ", "US ", "USA ", "UK ", "CO ", "ARG ", "AR ", "EC ", "MX ", "PY ", "DE ", "ES ")):
        return True
    if not a.startswith(("USANETWORK", "USAHD", "USAFHD")):
        if re.match(r"^(?:PT|US|USA|UK|CO|ARG|MX|EC|PY|DE|ES)(?:CARTOON|DISCOVERY|DISNEY|FOX|STAR|GLOBO|HISTORIA|HISTORY|HOLLYWOOD|IDINVESTIGATION|NATGEO|NATIONAL|NICK|RTP|SIC|SPORT|TVI|TLC|ANIMAL|LIFETIME|ESPN|H2|CANAL|CARACOL|RCN)", a):
            return True
    return False

def _fuzzy_incompatible(alias: str, candidate: str) -> bool:
    if alias == candidate:
        return False
    if _is_foreign_prefix(alias) != _is_foreign_prefix(candidate):
        return True
    if ("SPORT TV" in alias or "SPORTTV" in alias or "PTSPORT" in alias) and ("SPORTV" in candidate and "SPORT TV" not in candidate):
        return True
    if ("UNIVERSO" in alias and "UNIVERSAL" in candidate) or ("UNIVERSAL" in alias and "UNIVERSO" in candidate):
        return True
    if ("UNIVERSO" in alias and "UNIVESP" in candidate) or ("UNIVESP" in alias and "UNIVERSO" in candidate):
        return True
    if ("DIARIO" in alias and "DIARIO" not in candidate) or ("DIARIO" in candidate and "DIARIO" not in alias):
        return True
    if ("GAZETAES" in alias or "GAZETAVITORIA" in alias or "GAZETASUL" in alias or "GAZETAALAGOAS" in alias) and candidate in {"TVGAZETA", "GAZETA", "REDEGAZETA"}:
        return True
    if ("WSBRASIL" in alias and "WSBRASIL" not in candidate) or ("WSBRASIL" in candidate and "WSBRASIL" not in alias):
        return True
    if ("JIPARANA" in alias and "JIPARANA" not in candidate) or ("JIPARANA" in candidate and "JIPARANA" not in alias):
        return True
    if ("PARAIBA" in alias and "PARAIBA" not in candidate) or ("PARAIBA" in candidate and "PARAIBA" not in alias):
        return True
    if ("GLOOB" in alias) != ("GLOOB" in candidate):
        return True
    if ("NEWS" in alias) != ("NEWS" in candidate):
        return True
    if ("NOVELA" in alias) != ("NOVELA" in candidate):
        return True
    if ("XSPORTS" in alias) != ("XSPORTS" in candidate):
        return True
    if ("GLOBORO" in alias and "GLOBORIO" in candidate) or ("GLOBORIO" in alias and "GLOBORO" in candidate):
        return True
    if ("BMANDC" in candidate and "BMANDC" not in alias) or ("BMANDC" in alias and "BMANDC" not in candidate):
        return True
    if ("CNNBRASIL" in candidate and "CNBC" in alias) or ("CNBC" in candidate and "CNN" in alias):
        return True
    foreign_markers = ("INTERNACIONAL", "PORTUGAL", "AFRICA", "EUROPA", "GLOBOON", "GLOBONOW", "AMERICAS", "CABOVERDE", "MOZAMBIQUE")
    if any(fm in candidate for fm in foreign_markers) and not any(fm in alias for fm in foreign_markers):
        return True
    if alias.replace("BRASILIA", "BRASIL") == candidate.replace("BRASILIA", "BRASIL"):
        return True
    if candidate.startswith(_GLOBO_BRAND_COMPACT) and not alias.startswith(_GLOBO_BRAND_COMPACT) \
            and not (alias.startswith("EP") and candidate.startswith("EPTV")):
        return True
    na, nc = _network_of_normalized(alias), _network_of_normalized(candidate)
    if na and nc:
        if na != nc:
            return True
        ra = re.sub(r"^TV", "", alias[len(na):]); rc = re.sub(r"^TV", "", candidate[len(nc):])
        if (ra in _UF_CODES and not rc) or (rc in _UF_CODES and not ra):
            return True
        if ra != rc and ra in _UF_CODES and rc in _UF_CODES:
            return True
        if ra and rc and ra != rc and ra not in rc and rc not in ra and ra[:4] != rc[:4] \
                and SequenceMatcher(None, ra, rc).ratio() < 0.8:
            return True
    return False

def _compact_upper(value: str) -> str:
    v = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    return re.sub(r"[^A-Z0-9 ]", " ", "".join(c for c in v if not unicodedata.combining(c)).upper())

def match_conflicts_network(entry_name: str, source: "EpgSource", channel_id: str) -> bool:
    entry_country = extract_foreign_country_code(entry_name)
    labels = [channel_id] + list(source.names.get(channel_id, [])[:2])
    if entry_country:
        if not any(extract_foreign_country_code(lab) == entry_country for lab in labels):
            return True
    else:
        if any(extract_foreign_country_code(lab) != "" for lab in labels):
            return True

    if all(has_conflicting_region(entry_name, lab) for lab in labels):
        return True

    compact = re.sub(r"[^A-Z]", "", _compact_upper(entry_name))
    net = next((n for n in ("SBT", "RECORD", "BAND") if compact.startswith(n)), "")
    if not net:
        return False
    for lab in labels:
        n = " ".join(_compact_upper(lab).split())
        for other, prefixes in _NETWORK_PREFIXES.items():
            if other != net and n.startswith(prefixes):
                return True
        if net != "SBT" and any(k in n for k in ("REDE MASSA", "TV JANGADEIRO", "TV ALTEROSA", "TV ARATU", "SERRA DOURADA", "TV JORNAL")):
            return True
        if n.startswith(("TV BRASIL", "REDE TV", "REDETV")) or n.startswith(_GLOBO_ONLY_BRANDS) \
                or n.replace(" ", "").startswith(_GLOBO_BRAND_COMPACT):
            return True
    return False

def expand_open_tv_tiers(nome_limpo: str, grupo: str) -> tuple[list[str], list[str], list[str]]:
    tier1_local: list[str] = []
    tier2_state: list[str] = []
    tier3_national: list[str] = []

    if extract_foreign_country_code(nome_limpo):
        return tier1_local, tier2_state, tier3_national

    network = ""
    if (nome_limpo.startswith("GLOBO") or "GLOBO" in grupo.upper()) and "NEWS" not in nome_limpo and "NOVELAS" not in nome_limpo and "GLOOB" not in nome_limpo:
        network = "GLOBO"
    elif nome_limpo.startswith("SBT") and "NEWS" not in nome_limpo:
        network = "SBT"
    elif nome_limpo.startswith("RECORD") and "NEWS" not in nome_limpo:
        network = "RECORD"
    elif nome_limpo.startswith("BAND") and "NEWS" not in nome_limpo and "SPORT" not in nome_limpo:
        network = "BAND"

    if not network:
        return tier1_local, tier2_state, tier3_national

    without_net = re.sub(rf'^{network}(?:\s+TV)?\s+', '', nome_limpo).strip()
    if without_net and without_net != nome_limpo:
        tier1_local.append(f"{network} {without_net}")
        if network == "GLOBO":
            tier1_local.append(f"{network} TV {without_net}")
            if without_net.startswith(_GLOBO_ONLY_BRANDS):
                tier1_local.append(without_net)

    city_only = re.sub(_AFFILIATE_PATTERNS, ' ', without_net, flags=re.IGNORECASE)
    city_only = ' '.join(city_only.split()).strip()
    if city_only and city_only != without_net:
        tier1_local.append(f"{network} {city_only}")
        city_no_uf = re.sub(r'\s+(SP|RJ|RS|MG|BA|PR|PE|CE|PA|MA|SC|GO|PB|ES|AM|RN|AL|PI|MT|DF|MS|SE|RO|TO|AC|AP|RR)$', '', city_only)
        if city_no_uf and city_no_uf != city_only:
            tier1_local.append(f"{network} {city_no_uf}")

    if "ASA BRANCA" in nome_limpo or "CARUARU" in nome_limpo:
        tier1_local.extend(["TV ASA BRANCA", "GLOBO TV ASA BRANCA", "GLOBO ASA BRANCA", "GLOBO CARUARU"])
        tier2_state.extend(["GLOBO NORDESTE", "GLOBO RECIFE", "GLOBO PERNAMBUCO", "GLOBO PE"])
    elif "GRANDE RIO" in nome_limpo or "PETROLINA" in nome_limpo:
        tier1_local.extend(["TV GRANDE RIO", "GLOBO TV GRANDE RIO", "GLOBO PETROLINA"])
        tier2_state.extend(["GLOBO NORDESTE", "GLOBO RECIFE", "GLOBO PE"])
    elif "PERNAMBUCO" in nome_limpo or "RECIFE" in nome_limpo or "GLOBO NORDESTE" in nome_limpo:
        tier1_local.extend(["GLOBO NORDESTE", "GLOBO RECIFE", "GLOBO PERNAMBUCO", "GLOBO PE"])

    if "GAZETA ALAGOAS" in nome_limpo or "ALAGOAS" in nome_limpo or "MACEIO" in nome_limpo:
        if network == "GLOBO":
            tier1_local.extend(["TV GAZETA DE ALAGOAS", "TV GAZETA ALAGOAS", "GLOBO TV GAZETA", "GLOBO ALAGOAS", "GLOBO MACEIO", "GLOBO AL"])
        elif network == "RECORD":
            tier1_local.extend(["REDE RECORD MACEIO", "RECORD MACEIO", "TV PAJUCARA", "RECORD ALAGOAS"])
        elif network == "SBT":
            tier1_local.extend(["SBT ALAGOAS", "TV PONTA VERDE", "SBT MACEIO"])

    if "GAZETA VITORIA" in nome_limpo or "GAZETA SUL" in nome_limpo or "GAZETA NORTE" in nome_limpo or "ESPIRITO SANTO" in nome_limpo or "VITORIA" in nome_limpo:
        tier1_local.extend(["TV GAZETA ES", "TV GAZETA VITORIA", "GLOBO VITORIA", "GLOBO ESPIRITO SANTO", "GLOBO ES"])
    if "VERDES MARES" in nome_limpo or "FORTALEZA" in nome_limpo or "CEARA" in nome_limpo:
        tier1_local.extend(["TV VERDES MARES", "GLOBO TV VERDES MARES", "GLOBO FORTALEZA", "GLOBO CEARA", "GLOBO CE"])
    if "REDE AMAZONICA" in nome_limpo or "AMAZONICA" in nome_limpo or "MANAUS" in nome_limpo or "JI PARANA" in nome_limpo:
        if "RIO BRANCO" in nome_limpo or "ACRE" in nome_limpo or " AC" in nome_limpo:
            tier1_local.extend(["REDE AMAZONICA RIO BRANCO", "GLOBO RIO BRANCO", "GLOBO ACRE", "GLOBO AC"])
        elif "PORTO VELHO" in nome_limpo or "RONDONIA" in nome_limpo or "JI PARANA" in nome_limpo or "ARIQUEMES" in nome_limpo or " RO" in nome_limpo:
            tier1_local.extend(["REDE AMAZONICA PORTO VELHO", "GLOBO PORTO VELHO", "GLOBO RONDONIA"])
        elif "MACAPA" in nome_limpo or "AMAPA" in nome_limpo or " AP" in nome_limpo:
            tier1_local.extend(["REDE AMAZONICA MACAPA", "GLOBO MACAPA", "GLOBO AMAPA", "GLOBO AP"])
        elif "BOA VISTA" in nome_limpo or "RORAIMA" in nome_limpo or " RR" in nome_limpo:
            tier1_local.extend(["REDE AMAZONICA BOA VISTA", "GLOBO BOA VISTA", "GLOBO RORAIMA", "GLOBO RR"])
        tier2_state.extend(["REDE AMAZONICA MANAUS", "REDE AMAZONICA", "TV AMAZONAS", "GLOBO MANAUS", "GLOBO AMAZONAS", "GLOBO AM"])
    if "TV LIBERAL" in nome_limpo or "BELEM" in nome_limpo:
        tier1_local.extend(["TV LIBERAL", "GLOBO TV LIBERAL", "GLOBO BELEM", "GLOBO PARA", "GLOBO PA"])
    if "TAPAJOS" in nome_limpo or "SANTAREM" in nome_limpo:
        tier1_local.extend(["TV TAPAJOS", "GLOBO SANTAREM"])
        tier2_state.extend(["TV LIBERAL", "GLOBO BELEM", "GLOBO PA"])
    if "TV MIRANTE" in nome_limpo or "SAO LUIS" in nome_limpo or "MARANHAO" in nome_limpo:
        tier1_local.extend(["TV MIRANTE SAO LUIS", "TV MIRANTE", "GLOBO SAO LUIS", "GLOBO MARANHAO", "GLOBO MA"])
    if "ANHANGUERA" in nome_limpo or "GOIANIA" in nome_limpo or "GOIAS" in nome_limpo:
        tier1_local.extend(["TV ANHANGUERA", "GLOBO ANHANGUERA", "GLOBO GOIANIA", "GLOBO GOIAS", "GLOBO GO"])
    if "RBS" in nome_limpo or "PORTO ALEGRE" in nome_limpo:
        tier1_local.extend(["GLOBO RBS TV POA", "RBS TV POA", "RBS TV", "GLOBO RBS", "GLOBO PORTO ALEGRE", "GLOBO RS"])
    if "RPC" in nome_limpo or "CURITIBA" in nome_limpo:
        tier1_local.extend(["RPC CURITIBA", "RPC TV", "RPC", "GLOBO RPC", "GLOBO CURITIBA", "GLOBO PARANA", "GLOBO PR"])
    if "NSC" in nome_limpo or "FLORIANOPOLIS" in nome_limpo:
        tier1_local.extend(["NSC TV FLORIANOPOLIS", "NSC TV", "GLOBO NSC", "GLOBO FLORIANOPOLIS", "GLOBO SANTA CATARINA", "GLOBO SC"])
    if "EPTV" in nome_limpo:
        if "SUL DE MINAS" in nome_limpo or "VARGINHA" in nome_limpo:
            tier1_local.extend(["EPTV SUL DE MINAS", "GLOBO SUL DE MINAS", "GLOBO VARGINHA"])
        elif "CAMPINAS" in nome_limpo:
            tier1_local.extend(["EPTV CAMPINAS", "GLOBO CAMPINAS"])
        elif "RIBEIRAO" in nome_limpo:
            tier1_local.extend(["EPTV RIBEIRAO", "GLOBO RIBEIRAO PRETO"])
        elif "CENTRAL" in nome_limpo or "SAO CARLOS" in nome_limpo:
            tier1_local.extend(["EPTV CENTRAL", "GLOBO SAO CARLOS"])
        tier2_state.extend(["EPTV CAMPINAS", "EPTV SUL DE MINAS", "EPTV"])
    if "TV TEM" in nome_limpo or "SJRP" in nome_limpo or "RIO PRETO" in nome_limpo or "MOGI DAS CRUZES" in nome_limpo or "FRONTEIRA" in nome_limpo:
        tier1_local.extend(["TV TEM", "GLOBO BAURU", "GLOBO SOROCABA", "GLOBO SAO JOSE DO RIO PRETO", "GLOBO ITAPETININGA"])
        tier2_state.extend(["GLOBO SP", "GLOBO SAO PAULO"])
    if "VANGUARDA" in nome_limpo:
        tier1_local.extend(["TV VANGUARDA", "REDE VANGUARDA", "GLOBO SAO JOSE DOS CAMPOS", "GLOBO TAUBATE"])
    if "INTEGRACAO" in nome_limpo or "INTEG " in nome_limpo or "BELOHORIZONTE" in nome_limpo.replace(" ", ""):
        tier1_local.extend(["TV INTEGRACAO", "GLOBO MINAS", "GLOBO BELO HORIZONTE", "GLOBO UBERLANDIA", "GLOBO JUIZ DE FORA", "GLOBO DIVINOPOLIS", "GLOBO ARAXA"])
        tier2_state.extend(["GLOBO MINAS", "GLOBO BELO HORIZONTE", "GLOBO MG"])
    if "INTER TV" in nome_limpo or "INTERTV" in nome_limpo or "INTER VALE" in nome_limpo:
        if "CABUGI" in nome_limpo or "RN" in nome_limpo or "NATAL" in nome_limpo:
            tier1_local.extend(["INTER TV CABUGI", "GLOBO NATAL", "GLOBO RN"])
        elif "MG" in nome_limpo or "MINAS" in nome_limpo or "VALES" in nome_limpo or "MONTES CLAROS" in nome_limpo:
            tier1_local.extend(["INTER TV GRANDE MINAS", "INTER TV DOS VALES"])
            tier2_state.extend(["GLOBO MINAS", "GLOBO MG"])
        else:
            tier1_local.extend(["INTER TV ALTO LITORAL", "INTER TV SERRA MAR", "INTER TV PLANICIE", "INTER TV"])
    if "CABO BRANCO" in nome_limpo or "PARAIBA" in nome_limpo or "JOAO PESSOA" in nome_limpo:
        tier1_local.extend(["TV CABO BRANCO", "TV PARAIBA", "GLOBO JOAO PESSOA", "GLOBO CAMPINA GRANDE", "GLOBO PB"])
    if "SERGIPE" in nome_limpo or "ARACAJU" in nome_limpo:
        tier1_local.extend(["TV SERGIPE", "GLOBO ARACAJU", "GLOBO SERGIPE", "GLOBO SE"])
    if "CLUBE" in nome_limpo or "PIAUI" in nome_limpo or "TERESINA" in nome_limpo:
        tier1_local.extend(["TV CLUBE", "GLOBO TERESINA", "GLOBO PIAUI", "GLOBO PI"])
    if "MORENA" in nome_limpo or "CAMPO GRANDE" in nome_limpo:
        tier1_local.extend(["TV MORENA", "GLOBO CAMPO GRANDE", "GLOBO MATO GROSSO DO SUL", "GLOBO MS"])
    if "CENTRO AMERICA" in nome_limpo or "CUIABA" in nome_limpo:
        tier1_local.extend(["TV CENTRO AMERICA", "GLOBO CUIABA", "GLOBO MATO GROSSO", "GLOBO MT"])
    if "BAHIA" in nome_limpo or "SALVADOR" in nome_limpo or "SANTA CRUZ" in nome_limpo or "SUBAE" in nome_limpo or "SUDOESTE" in nome_limpo:
        if network == "GLOBO":
            tier1_local.extend(["TV BAHIA", "REDE BAHIA", "GLOBO SALVADOR", "GLOBO BAHIA", "GLOBO BA"])
        elif network == "SBT":
            tier1_local.extend(["TV ARATU", "SBT BAHIA", "SBT SALVADOR"])
        elif network == "RECORD":
            tier1_local.extend(["RECORD ITAPOAN", "TV ITAPOAN", "RECORD BAHIA", "RECORD SALVADOR"])
    if "ARATU" in nome_limpo:
        tier1_local.extend(["TV ARATU", "SBT BAHIA", "SBT SALVADOR"])
    if "SERRA DOURADA" in nome_limpo:
        tier1_local.extend(["SBT GOIANIA TV SERRA DOURADA", "TV SERRA DOURADA", "SBT GOIANIA", "SBT GOIAS"])
    if "JANGADEIRO" in nome_limpo:
        tier1_local.extend(["TV JANGADEIRO", "SBT FORTALEZA", "SBT CEARA"])
    if "ALTEROSA" in nome_limpo:
        tier1_local.extend(["TV ALTEROSA", "SBT MINAS", "SBT BELO HORIZONTE", "SBT MG"])
    if "REDE MASSA" in nome_limpo:
        tier1_local.extend(["REDE MASSA", "SBT CURITIBA", "SBT PARANA", "SBT PR"])
    if "TV JORNAL" in nome_limpo or (network == "SBT" and re.search(r"\bJORNAL\b", nome_limpo)):
        tier1_local.extend(["TV JORNAL", "SBT RECIFE", "SBT PERNAMBUCO", "SBT PE"])

    uf_match = re.search(r'\b(SP|RJ|RS|MG|BA|PR|PE|CE|PA|MA|SC|GO|PB|ES|AM|RN|AL|PI|MT|DF|MS|SE|RO|TO|AC|AP|RR)\b', nome_limpo)
    if uf_match:
        uf = uf_match.group(1)
        for state_alias in UF_TO_STATE_AND_CAPITAL.get(uf, []):
            if state_alias == "RO" and network == "GLOBO":
                continue
            if state_alias.startswith(("TV ", "REDE ", "GLOBO ", "INTER ")):
                tier2_state.append(state_alias)
            else:
                tier2_state.append(f"{network} {state_alias}")
            if network == "RECORD":
                tier2_state.append(f"RECORD TV {state_alias}")

    if network == "GLOBO":
        tier3_national.extend(["GLOBO SP", "GLOBO SAO PAULO", "TV GLOBO", "REDE GLOBO", "GLOBO RJ", "GLOBO RIO"])
    elif network == "SBT":
        tier3_national.extend(["SBT SP", "SBT SAO PAULO", "SBT", "SBT NACIONAL"])
    elif network == "RECORD":
        tier3_national.extend(["RECORD SP", "RECORD TV SP", "RECORD", "RECORD TV", "RECORD NACIONAL"])
    elif network == "BAND":
        tier3_national.extend(["BAND SP", "BAND SAO PAULO", "BAND", "TV BAND", "BANDEIRANTES", "REDE BANDEIRANTES"])

    tier1_local = [a for a in tier1_local if _alias_belongs_to_network(a, network)]
    tier2_state = [a for a in tier2_state if _alias_belongs_to_network(a, network)]
    tier3_national = [a for a in tier3_national if _alias_belongs_to_network(a, network)]
    return tier1_local, tier2_state, tier3_national

def aliases_tiers_for(entry: dict[str, object]) -> list[list[str]]:
    base_channel_name = str(entry.get("name", ""))
    foreign_code = extract_foreign_country_code(base_channel_name)
    values: list[str] = [base_channel_name]
    for x in entry.get("aliases", []) or []:
        x_str = str(x)
        if not has_conflicting_channel_number(base_channel_name, x_str) and not has_conflicting_region(base_channel_name, x_str):
            values.append(x_str)
    grupo = str(entry.get("group", entry.get("group-title", entry.get("group_title", ""))))

    tier1, tier2, tier3 = [], [], []
    seen = set()

    def add_to(target_list: list[str], val: str, allow_national_fallback: bool = False):
        if not allow_national_fallback and has_conflicting_region(base_channel_name, val):
            return
        n = original_normalize_name(val)
        if n and n not in seen:
            target_list.append(n)
            seen.add(n)

    cleaned_pairs: list[str] = []
    for value in values:
        nome_limpo = normalizar_canal(value)
        nome_com_grupo = inferir_emissora_pelo_grupo(nome_limpo, grupo)
        cleaned_pairs.append(nome_com_grupo)
        if nome_limpo != nome_com_grupo:
            cleaned_pairs.append(nome_limpo)

        add_to(tier1, nome_com_grupo)
        add_to(tier1, nome_limpo)

        if not foreign_code:
            for syn in CHANNEL_SYNONYMS.get(nome_limpo, []):
                add_to(tier1, syn)

            sem_uf = re.sub(r'\s+(SP|RJ|RS|MG|BA|PR|PE|CE|PA|MA|SC|GO|PB|ES|AM|RN|AL|PI|MT|DF|MS|SE|RO|TO|AC|AP|RR)$', '', nome_limpo)
            if sem_uf != nome_limpo and sem_uf not in {"GLOBO", "SBT", "RECORD", "BAND"}:
                add_to(tier2, sem_uf)
                for syn in CHANNEL_SYNONYMS.get(sem_uf, []):
                    add_to(tier2, syn)

    if not foreign_code:
        for base_cl in cleaned_pairs:
            t1_ext, t2_ext, t3_ext = expand_open_tv_tiers(base_cl, grupo)
            for a in t1_ext:
                add_to(tier1, a)
            for a in t2_ext:
                add_to(tier2, a)
            for a in t3_ext:
                add_to(tier3, a, allow_national_fallback=True)

    return [t for t in (tier1, tier2, tier3) if t]

def choose_exact(source: EpgSource, aliases: Iterable[str], entry_name: str = "", allow_national_fallback: bool = False) -> tuple[str | None, str]:
    for alias in aliases:
        candidates = source.exact_index.get(alias, set())
        valid_cands = [
            cid for cid in candidates
            if (allow_national_fallback or not match_conflicts_network(entry_name, source, cid))
            and not has_conflicting_channel_number(entry_name, cid)
        ] if entry_name else list(candidates)
        if valid_cands:
            return max(valid_cands, key=lambda cid: len(source.rows.get(cid, []))), f"nome exato: {alias}"
    return None, ""

def _sequence_ratio_upper_bound(left: str, right: str, left_counter: Counter[str] | None = None) -> float:
    if not left or not right: return 0.0
    length_bound = (2.0 * min(len(left), len(right))) / (len(left) + len(right))
    if length_bound < (FUZZY_THRESHOLD - FUZZY_MARGIN): return length_bound
    lc = left_counter or Counter(left)
    rc = Counter(right)
    common = sum(min(count, rc.get(ch, 0)) for ch, count in lc.items())
    return min(length_bound, (2.0 * common) / (len(left) + len(right)))

def best_fuzzy(source: EpgSource, aliases: list[str], entry_name: str = "") -> tuple[str | None, float, float, str]:
    if not aliases: return None, 0.0, 0.0, ""
    if entry_name and extract_foreign_country_code(entry_name):
        return None, 0.0, 0.0, ""
    relevant_floor = FUZZY_THRESHOLD - FUZZY_MARGIN
    best_by_channel: dict[str, tuple[float, str, str]] = {}
    for alias in aliases:
        if len(alias) < 4 or _is_foreign_prefix(alias): continue
        alias_counter = Counter(alias)
        bucket = source.fuzzy_index.get(digit_signature(alias), {})
        for normalized_candidate, channel_ids in bucket.items():
            if _fuzzy_incompatible(alias, normalized_candidate): continue
            if _sequence_ratio_upper_bound(alias, normalized_candidate, alias_counter) < relevant_floor: continue
            ratio = SequenceMatcher(None, alias, normalized_candidate).ratio()
            if ratio < relevant_floor: continue
            for channel_id in channel_ids:
                if entry_name and (match_conflicts_network(entry_name, source, channel_id) or has_conflicting_channel_number(entry_name, channel_id)):
                    continue
                old = best_by_channel.get(channel_id)
                if old is None or ratio > old[0]: best_by_channel[channel_id] = (ratio, alias, normalized_candidate)
    if not best_by_channel: return None, 0.0, 0.0, ""
    scored = sorted(((score, cid, a, cand) for cid, (score, a, cand) in best_by_channel.items()), reverse=True)
    best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second_score, f"aproximação {best[0]:.3f}: {best[2]} ~ {best[3]}"

def find_match_in_tier(source: EpgSource, tier_aliases: list[str], source_ids: list[str] | None = None, tier_num: int = 1, entry_name: str = "") -> ChannelMatch | None:
    if tier_num == 1:
        for raw_sid in (source_ids or []):
            if not is_valid_provider_tvid(entry_name, raw_sid):
                continue
            if has_conflicting_channel_number(entry_name, raw_sid) or has_conflicting_region(entry_name, raw_sid):
                continue
            norm_sid = normalize_source_id(raw_sid)
            if norm_sid and norm_sid in source.normalized_id_index:
                cands = source.normalized_id_index[norm_sid]
                best_cid = max(cands, key=lambda cid: len(source.rows.get(cid, [])))
                if not match_conflicts_network(entry_name, source, best_cid):
                    return ChannelMatch(source, best_cid, f"id do dicionário: {raw_sid}", 1.0, tier=1)

    exact_id, reason = choose_exact(source, tier_aliases, entry_name=entry_name, allow_national_fallback=(tier_num >= 3))
    if exact_id:
        return ChannelMatch(source, exact_id, reason if tier_num == 1 else f"{reason} (camada {tier_num})", 1.0, tier=tier_num)
    
    if tier_num <= 2:
        fuzzy_id, best_score, second_score, fuzzy_reason = best_fuzzy(source, tier_aliases, entry_name=entry_name)
        if fuzzy_id and best_score >= FUZZY_THRESHOLD and (best_score - second_score) >= FUZZY_MARGIN \
                and not match_conflicts_network(entry_name, source, fuzzy_id):
            return ChannelMatch(source, fuzzy_id, fuzzy_reason if tier_num == 1 else f"{fuzzy_reason} (camada {tier_num})", best_score, tier=tier_num)
        
    return None

def find_tiered_matches_across_sources(active_sources: list[EpgSource], alias_tiers: list[list[str]], source_ids: list[str], entry_name: str = "") -> list[ChannelMatch]:
    collected_valid: list[ChannelMatch] = []
    fallback_matches: list[ChannelMatch] = []

    for tier_idx, tier_aliases in enumerate(alias_tiers, start=1):
        already_matched_sources = {m.source.spec.key for m in collected_valid}
        tier_matches = [
            m for source in active_sources
            if source.spec.key not in already_matched_sources
            and (m := find_match_in_tier(source, tier_aliases, source_ids if tier_idx == 1 else None, tier_num=tier_idx, entry_name=entry_name)) is not None
        ]
        valid_with_rows = [m for m in tier_matches if m.source.rows.get(m.channel_id)]
        if valid_with_rows:
            collected_valid.extend(valid_with_rows)
            has_strong_match = any(
                m.match_score >= 0.999 or m.source.spec.family == "afiliadas_locais" or m.match_score >= 0.90
                for m in collected_valid
            )
            if has_strong_match or len(collected_valid) >= 2:
                return collected_valid
        if tier_matches and not fallback_matches:
            fallback_matches = tier_matches

    return collected_valid if collected_valid else fallback_matches

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
        if local_tag(child) == "title" and (child.text or "").strip():
            return clean_raw_title((child.text or "").strip())
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
        if row.start <= now < row.stop: current = {"start": row.start, "stop": row.stop, "title": row.title}
        elif row.start > now and next_item is None:
            next_item = {"start": row.start, "stop": row.stop, "title": row.title}
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
    window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=72)
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
        if meta["sub_title"]: pts += 0.20
        if meta["season"] is not None or meta["episode"] is not None: pts += 0.15
        if meta["rating_badge"] or meta["categories"]: pts += 0.20
        rich_points += min(1.0, pts)
    richness_score = round(rich_points / len(sample_rows), 4) if sample_rows else 0.0

    eval_window_end = now + timedelta(hours=36)
    eval_rows = [r for r in rows if r.stop > now and r.start < eval_window_end]
    for row in eval_rows:
        start, stop = row.start, row.stop
        duration = stop - start
        if duration.total_seconds() <= 0 or duration > timedelta(hours=12): invalid += 1
        if previous_stop is not None:
            if start < previous_stop - timedelta(seconds=60): overlaps += 1
            elif start > previous_stop + timedelta(minutes=30): gaps += 1
        if previous_stop is None or stop > previous_stop: previous_stop = stop
        
    future_end = max((row.stop for row in rows if row.stop > now), default=None)
    future_hours = max(0.0, (future_end - now).total_seconds() / 3600) if future_end else 0.0
    score = (
        (0.25 if current else 0.0)
        + (0.10 if next_item else 0.0)
        + (0.35 * near_coverage_ratio)
        + (0.20 * min(future_hours / 20.0, 1.0))
        + (0.10 * min(len(rows) / 12.0, 1.0))
    )
    score -= min(0.25, invalid * 0.08 + overlaps * 0.02 + gaps * 0.02)
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
        fut_h = float(h_info.get("future_hours", 0.0))

        # Saúde efetiva: se tem >= 0.80 de saúde e >= 48h de grade futura, considera saúde plena (1.0)
        eff_health = 1.0 if (health_score >= 0.80 and fut_h >= 48.0) else health_score

        # Pontuação 100% baseada em mérito da grade naquele momento (Precisão/Consenso + Conteúdo/Sinopse + Saúde/Cobertura)
        quality = 0.40 * weighted_ratio + 0.25 * eff_health + 0.20 * richness_score + 0.15 * avg_support
        evidence_factor = 0.55 + 0.45 * min(vote_weight / max(max_active_vote_weight, 0.5), 1.0)
        confidence = round(100 * max(0.0, min(1.0, quality * evidence_factor)))
        evaluations.append(CandidateEvaluation(
            match=match, support_votes=support_votes, vote_sources=vote_sources,
            support_weight=round(support_weight, 3), vote_weight=round(vote_weight, 3),
            support_families=support_families, weighted_ratio=round(weighted_ratio, 4),
            avg_support_agreement=round(avg_support, 4), health_score=round(health_score, 4),
            richness_score=richness_score, health=h_info, confidence=confidence, comparisons=comparisons
        ))

    def merit_rank(ev: CandidateEvaluation) -> tuple:
        """Ranking puro por qualidade real no canal, sem favorecer posição fixa na lista de fontes."""
        has_programmes = int(bool(ev.match.source.rows.get(ev.match.channel_id)))
        fut_h = float(ev.health.get("future_hours", 0.0))
        near_cov = float(ev.health.get("near_coverage_ratio", 0.0))
        eff_h = 1.0 if (ev.health_score >= 0.80 and fut_h >= 48.0) else ev.health_score
        # Só dá bônus para Afiliadas Locais quando é match direto (tier 1) de uma afiliada regional específica
        is_direct_local_affiliate = int(ev.match.tier == 1 and ev.match.source.spec.family == "afiliadas_locais" and ev.health_score >= 0.70)
        # Nota composta de mérito (0.0 a 1.0): Precisão de horário (consenso) + Riqueza de metadados + Cobertura sem buracos
        horizon_factor = min(1.0, fut_h / 48.0)
        composite_merit = (
            0.38 * ev.weighted_ratio
            + 0.22 * ev.avg_support_agreement
            + 0.24 * ev.richness_score
            + 0.10 * eff_h
            + 0.06 * horizon_factor
        )
        return (
            has_programmes,
            -ev.match.tier,
            is_direct_local_affiliate,
            int(near_cov >= 0.65 and ev.health_score >= 0.70),
            round(composite_merit, 4),
            ev.support_votes,
            round(ev.richness_score, 3),
            round(fut_h, 1),
        )

    evaluations.sort(key=merit_rank, reverse=True)
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
    if switched: warnings.append(f"fonte eleita por mérito dinâmico: {chosen.match.source.label} (maior conteúdo/precisão)")

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
        for ev in all_evaluations
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

def get_clean_aliases(entry: dict[str, object]) -> list[str]:
    """Retorna os nomes base, IDs de compatibilidade sincronizados e TODAS as variantes reais da M3U (incluindo FHD [H265])."""
    result = []
    seen = set()
    raw_name = str(entry.get("name", "")).strip()

    def add_alias(val: str):
        v = html.unescape(str(val or "")).strip()
        if not v or len(v) > 100:
            return
        if has_conflicting_channel_number(raw_name, v) or has_conflicting_region(raw_name, v):
            return
        if v.upper() not in seen:
            result.append(v)
            seen.add(v.upper())

    clean_base = strip_qualities_keep_case(raw_name)
    norm_clean = normalizar_canal(raw_name)

    for candidate in (clean_base, norm_clean, raw_name):
        if candidate:
            add_alias(candidate)
            for suf in ("FHD", "HD", "SD", "4K", "FHD [H265]", "FHD H265", "HD [H265]"):
                add_alias(f"{candidate} {suf}")

    for cid in entry.get("compatibility_ids", []) or []:
        if is_valid_provider_tvid(raw_name, str(cid)):
            add_alias(str(cid))

    for t_id in entry.get("tvg_ids_to_export", []) or []:
        if is_valid_provider_tvid(raw_name, str(t_id)):
            add_alias(str(t_id))

    for syn in CHANNEL_SYNONYMS.get(norm_clean, []):
        add_alias(syn)

    for a in entry.get("aliases", []) or []:
        a_str = str(a).strip()
        a_clean = strip_qualities_keep_case(a_str)
        if a_clean:
            add_alias(a_clean)
        if a_str:
            add_alias(a_str)

    for raw_a in entry.get("raw_aliases", []) or []:
        raw_s = str(raw_a).strip()
        if raw_s:
            add_alias(raw_s)

    return result

def get_primary_export_ids(entry: dict[str, object]) -> list[str]:
    """Retorna ESTRITAMENTE 1 único ID por canal, priorizando o tvg-id sincronizado na M3U (ex: hbo.br, A&E)."""
    raw_name = str(entry.get("name", "")).strip()
    target_id = str(entry.get("id", "")).strip()
    clean_base = strip_qualities_keep_case(raw_name) or target_id

    for cid in entry.get("compatibility_ids", []) or []:
        if is_valid_provider_tvid(raw_name, str(cid)) and not has_conflicting_channel_number(raw_name, str(cid)):
            return [str(cid).strip()]
    for t_id in entry.get("tvg_ids_to_export", []) or []:
        if is_valid_provider_tvid(raw_name, str(t_id)) and not has_conflicting_channel_number(raw_name, str(t_id)):
            return [str(t_id).strip()]

    return [clean_base]

def apply_rich_metadata_to_element(
    el: ET.Element, meta_pool: list[dict[str, Any]], channel_name: str, duration_minutes: float
) -> None:
    """Formata <programme> com visual rico enxuto (🟢 LIVRE / 🟠 +14) e SOMENTE [T03E07] sem duplicar S04E07."""
    base_meta = extract_program_metadata(el, channel_name, duration_minutes)
    all_metas = [base_meta, *(meta_pool or [])]

    best_sub = ""
    best_desc = ""
    best_season: int | None = None
    best_episode: int | None = None
    best_date = ""
    best_rating_badge = ""
    best_icon = ""
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
        if not best_rating_badge and m.get("rating_badge"):
            best_rating_badge = m["rating_badge"]
        if not best_icon and m.get("icon"):
            best_icon = m["icon"]
        for c in m.get("categories", []):
            if c and c not in merged_cats:
                merged_cats.append(c)

    is_movie_or_sport = is_likely_movie_or_sport(merged_cats, best_desc, duration_minutes, channel_name)
    if is_movie_or_sport:
        best_season = None
        best_episode = None

    for old_ep in list(el.findall("episode-num")):
        el.remove(old_ep)

    title_node = el.find("title")
    clean_title = base_meta["title"] or (title_node.text.strip() if title_node is not None and title_node.text else "")
    clean_title = clean_raw_title(clean_title)
    if title_node is not None and clean_title:
        title_node.text = clean_title

    best_sub = strip_se_tokens(best_sub)
    best_desc = strip_se_tokens(best_desc)

    if best_sub and (best_sub.upper() == clean_title.upper() or best_sub.upper() in clean_title.upper()):
        best_sub = ""

    if best_sub and best_desc and best_desc.lower().startswith(best_sub.lower()):
        best_desc = best_desc[len(best_sub):].lstrip(" -–—:|.,")

    sub_node = el.find("sub-title")
    if sub_node is not None:
        el.remove(sub_node)

    se_badge = ""
    if not is_movie_or_sport:
        if best_season is not None and best_episode is not None:
            se_badge = f"[T{best_season:02d}E{best_episode:02d}]"
        elif best_episode is not None and best_episode > 1:
            se_badge = f"[Ep.{best_episode:02d}]"

    meta_bar: list[str] = []
    if best_rating_badge:
        meta_bar.append(best_rating_badge)
    if best_date and best_date.isdigit():
        meta_bar.append(f"📅 {best_date}")
    dur_int = int(round(duration_minutes))
    if 1 <= dur_int <= 720:
        meta_bar.append(f"⏱ {dur_int} mins")
    if merged_cats:
        meta_bar.append(f"🎭 {', '.join(merged_cats[:4])}")

    ep_line_parts: list[str] = []
    if se_badge:
        ep_line_parts.append(se_badge)
    if best_sub:
        ep_line_parts.append(best_sub)
    ep_line = " ".join(ep_line_parts).strip()

    header_lines: list[str] = []
    if meta_bar:
        header_lines.append(" | ".join(meta_bar))
    if ep_line:
        header_lines.append(f"📝 {ep_line}" if not se_badge else ep_line)

    if header_lines and best_desc:
        final_desc = "\n".join(header_lines) + "\n\n" + best_desc
    elif header_lines:
        final_desc = "\n".join(header_lines)
    else:
        final_desc = best_desc

    if final_desc:
        desc_node = el.find("desc")
        if desc_node is None:
            desc_node = ET.SubElement(el, "desc", {"lang": "pt"})
        desc_node.text = final_desc

    if best_icon and el.find("icon") is None:
        ET.SubElement(el, "icon", {"src": best_icon})

def _prog_item_richness(meta: dict[str, Any]) -> float:
    pts = 0.0
    if meta.get("desc") and len(meta["desc"]) >= 30:
        pts += 0.45
    if meta.get("sub_title"):
        pts += 0.20
    if meta.get("season") is not None or meta.get("episode") is not None:
        pts += 0.15
    if meta.get("rating_badge") or meta.get("categories"):
        pts += 0.20
    return min(1.0, pts)

def stitch_multi_source_schedule(
    all_evaluations: list[CandidateEvaluation], target_channel_id: str, channel_name: str, cutoff: datetime, max_future: datetime, overrides: dict[str, object]
) -> list[ET.Element]:
    """
    Recorte e Costura Dinâmica em 3 Níveis:
    1. Janelas de 6h: Elege a fonte mais precisa e rica em cada período do dia.
    2. Quebra de Blocos Genéricos: Se a vencedora da janela tiver um bloco esticado (>3h30 sem consenso)
       enquanto outras fontes concordam em programas menores reais, substitui pelos programas reais.
    3. Tapa-Buracos Cirúrgico (Gap Filling >= 10 min): Se faltar qualquer programa dentro da janela de 6h,
       recorta exatamente o programa faltante da 2ª melhor fonte (ordenada por mérito/conteúdo) e encaixa.
    """
    if not all_evaluations:
        return []

    leader_ev = all_evaluations[0]
    shift_by_source_key: dict[str, int] = {leader_ev.match.source.spec.key: 0}
    for comp in leader_ev.comparisons:
        other_key = str(comp.get("other_source_key", ""))
        agreement = comp.get("agreement")
        comparable = int(comp.get("comparable") or 0)
        if isinstance(agreement, float) and agreement >= AGREEMENT_THRESHOLD and comparable >= MIN_COMPARABLE_PROGRAMMES:
            shift_by_source_key[other_key] = int(comp.get("detected_shift") or 0)
        elif comparable < MIN_COMPARABLE_PROGRAMMES:
            cand_ev = next((e for e in all_evaluations if e.match.source.spec.key == other_key), None)
            if cand_ev and cand_ev.match.match_score >= 0.999 and cand_ev.match.tier == 1:
                shift_by_source_key[other_key] = 0

    aligned_sources_data: list[dict[str, Any]] = []
    metadata_by_norm_title: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for ev in all_evaluations:
        src = ev.match.source
        cid = ev.match.channel_id
        if src.spec.key not in shift_by_source_key:
            continue
        fixed_rows = src.rows.get(cid, [])
        if not fixed_rows:
            continue

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
                "richness": _prog_item_richness(meta),
            }
            cleaned_source_progs.append(prog_item)
            if row.normalized_title:
                metadata_by_norm_title[row.normalized_title].append(meta)

        if cleaned_source_progs:
            fut_h = float(ev.health.get("future_hours", 0.0))
            eff_h = 1.0 if (ev.health_score >= 0.80 and fut_h >= 48.0) else ev.health_score
            aligned_sources_data.append({
                "ev": ev,
                "progs": cleaned_source_progs,
                "global_merit": 0.45 * ev.weighted_ratio + 0.35 * ev.richness_score + 0.20 * eff_h,
            })

    if not aligned_sources_data:
        return []

    # Ordena todas as fontes alinhadas da melhor para a pior (garante que o tapa-buracos use sempre a 2ª melhor antes da 3ª)
    aligned_sources_data.sort(key=lambda s: s["global_merit"], reverse=True)

    # 1. SELEÇÃO POR JANELA DE 6 HORAS
    WINDOW_HOURS = 6
    window_delta = timedelta(hours=WINDOW_HOURS)
    merged_timeline: list[dict[str, Any]] = []

    cur_win_start = cutoff
    while cur_win_start < max_future:
        cur_win_end = min(max_future, cur_win_start + window_delta)
        actual_win_sec = max(1.0, (cur_win_end - cur_win_start).total_seconds())

        best_src_idx = -1
        best_win_score = -1.0

        title_counts_in_win: Counter[str] = Counter()
        for s_data in aligned_sources_data:
            seen_t = {
                p["norm_title"]
                for p in s_data["progs"]
                if p["stop"] > cur_win_start and p["start"] < cur_win_end and p["norm_title"]
            }
            for t in seen_t:
                title_counts_in_win[t] += 1

        for idx, s_data in enumerate(aligned_sources_data):
            win_progs = [
                p for p in s_data["progs"]
                if p["stop"] > cur_win_start and p["start"] < cur_win_end
            ]
            if not win_progs:
                continue

            covered_sec = sum(
                max(0.0, (min(cur_win_end, p["stop"]) - max(cur_win_start, p["start"])).total_seconds())
                for p in win_progs
            )
            cov_ratio = min(1.0, covered_sec / actual_win_sec)
            if cov_ratio < 0.25:
                continue

            avg_rich = mean(p["richness"] for p in win_progs)
            if len(aligned_sources_data) > 1:
                agreed_progs = sum(1 for p in win_progs if title_counts_in_win.get(p["norm_title"], 0) >= 2)
                local_agr = agreed_progs / len(win_progs)
            else:
                local_agr = 1.0

            win_score = (
                0.40 * cov_ratio
                + 0.30 * local_agr
                + 0.20 * avg_rich
                + 0.10 * s_data["global_merit"]
            )
            if win_score > best_win_score:
                best_win_score = win_score
                best_src_idx = idx

        if best_src_idx >= 0:
            chosen_win_progs = [
                p for p in aligned_sources_data[best_src_idx]["progs"]
                if p["stop"] > cur_win_start and p["start"] < cur_win_end
            ]
            for cand in chosen_win_progs:
                c_start, c_stop = cand["start"], cand["stop"]
                # Se for um bloco gigante (>3h30) sem sinopse e sem consenso na janela, ignora para deixar a 2ª melhor fonte preencher!
                if (c_stop - c_start).total_seconds() > 3.5 * 3600 and cand["richness"] < 0.25 and title_counts_in_win.get(cand["norm_title"], 0) <= 1 and len(aligned_sources_data) > 1:
                    continue

                if merged_timeline and c_start < merged_timeline[-1]["stop"]:
                    prev = merged_timeline[-1]
                    if prev["norm_title"] == cand["norm_title"] and abs((prev["start"] - c_start).total_seconds()) <= 1800:
                        if c_stop > prev["stop"]:
                            prev["stop"] = c_stop
                        continue
                    if c_start > prev["start"] + timedelta(minutes=5):
                        prev["stop"] = c_start
                    else:
                        c_start = prev["stop"]
                if (c_stop - c_start).total_seconds() >= 300:
                    cp = copy.deepcopy(cand)
                    cp["start"] = c_start
                    cp["stop"] = c_stop
                    merged_timeline.append(cp)

        cur_win_start = cur_win_end

    merged_timeline.sort(key=lambda x: x["start"])

    # 2. TAPA-BURACOS CIRÚRGICO DENTRO DAS JANELAS (Qualquer buraco >= 10 min é preenchido pela melhor fonte seguinte)
    MIN_GAP_SECONDS = 10 * 60
    MAX_EDGE_TRIM_SECONDS = 15 * 60

    for s_data in aligned_sources_data:
        added_any = False
        for cand in s_data["progs"]:
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
                if (gap_dur / c_dur) >= 0.65 or (left_trim <= MAX_EDGE_TRIM_SECONDS and right_trim <= MAX_EDGE_TRIM_SECONDS):
                    new_cand = copy.deepcopy(cand)
                    new_cand["start"] = g_start
                    new_cand["stop"] = g_stop
                    merged_timeline.append(new_cand)
                    added_any = True

        if added_any:
            merged_timeline.sort(key=lambda x: x["start"])

    merged_timeline.sort(key=lambda x: x["start"])

    # 3. SERIALIZAÇÃO E FUSÃO DE METADADOS RICOS
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
        for s_data in aligned_sources_data:
            for other_p in s_data["progs"]:
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
                    "rating_badge": m.get("rating_badge", ""),
                    "icon": m.get("icon", ""),
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
                prev_chosen = xml_id_to_data[out_id][0]
                same_channel = (prev_chosen.match.source.spec.key, prev_chosen.match.channel_id) == (chosen.match.source.spec.key, chosen.match.channel_id)
                if not same_channel and name_key(channel_name) != name_key(out_id):
                    print(f"Aviso: id exportado '{out_id}' disputado por canais diferentes ('{channel_name}'); nomes não mesclados.")
                    continue
                existing_names = xml_id_to_data[out_id][2]
                for n in all_names:
                    if n not in existing_names:
                        existing_names.append(n)

    _deduped = dedupe_display_names({oid: [oid, *v[2]] for oid, v in xml_id_to_data.items()})
    for oid, (chosen_, stitched_, _) in list(xml_id_to_data.items()):
        xml_id_to_data[oid] = (chosen_, stitched_, [n for n in _deduped[oid] if n != oid])

    with gzip_path.open("wb") as raw_gzip:
        with gzip.GzipFile(filename="epg.xml", mode="wb", fileobj=raw_gzip, mtime=0, compresslevel=GZIP_LEVEL) as gz:
            def emit(data: bytes) -> None:
                gz.write(data)
                if plain is not None: plain.write(data)

            generator = "EPG Universal Otimizado (Seleção Dinâmica por Mérito e Período do Dia)"
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

    root_epg_gz = Path("epg.xml.gz")
    try:
        if gzip_path.resolve() != root_epg_gz.resolve():
            shutil.copy2(gzip_path, root_epg_gz)
    except Exception:
        pass
    
    size_mb = gzip_path.stat().st_size / (1024 * 1024)
    print(f"Tamanho final do epg.xml.gz: {size_mb:.2f} MB (Limite do GitHub: 100 MB)")
    return programme_count, len(xml_id_to_data)

def ensure_operator_epgs_generated() -> None:
    """Executa automaticamente fetch_claro_epg.py e fetch_vivo_epg.py se ainda não tiverem gerado os .xml.gz."""
    if not Path("claro_epg.xml.gz").exists() and Path("fetch_claro_epg.py").exists():
        try:
            print("\nGerando claro_epg.xml.gz direto da API da Claro TV+...")
            from fetch_claro_epg import generate_claro_epg
            generate_claro_epg()
        except Exception as e:
            print(f"  -> Aviso ao extrair Claro TV+ API: {e}")

    if not Path("vivo_epg.xml.gz").exists() and Path("fetch_vivo_epg.py").exists():
        try:
            print("\nGerando vivo_epg.xml.gz direto da API da Vivo Play...")
            from fetch_vivo_epg import generate_vivo_epg
            generate_vivo_epg()
        except Exception as e:
            print(f"  -> Aviso ao extrair Vivo Play API: {e}")

def main() -> None:
    print("Iniciando geração de EPG (100% Mérito Dinâmico por Canal + Recorte de Grade por Período do Dia)...")
    run_start = time.perf_counter()
    timings = {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ensure_operator_epgs_generated()

    stage_start = time.perf_counter()
    
    entries = []
    id_observations: list[tuple[str, str]] = []
    grouped_by_norm: dict[str, dict[str, Any]] = {}
    VOD_BLACKLIST = {"VOD", "PLAYBACK", "XXX", "ADULTO", "EVENTO", "LANCAMENTO", "LANÇAMENTO", "FILMES", "SERIES", "SÉRIES", "24H", "24HS"}

    def is_junk_or_24h_channel(text: str) -> bool:
        u = text.upper().strip()
        if not u:
            return True
        if re.search(r"⭐{2,}|^[\s⭐\-—=_.*]+$", u):
            return True
        if re.match(r"^(?:24\s*H|\d{4}[A-Z])", u):
            return True
        if re.search(r"\bS\d{1,3}\s*E\d{1,3}\b|\bT\d{1,3}\s*EP?\d{1,3}\b|\b24HS?\b|^24\s*H\b", u):
            return True
        if any(bad in u for bad in ("LIVE QUARENTENA", "A FAZENDA CAMERA", "ATUALIZACAO DE CONTEUDO", "ATUALIZAÇÃO DE CONTEÚDO")):
            return True
        return False

    def normalize_channel_number_zero(name_str: str) -> str:
        """Normaliza zeros à esquerda em números de canais (ex: Paramount 01 -> Paramount 1, SportyNet + 01 -> SportyNet + 1)."""
        return re.sub(r'\b(0)(\d+)\b', r'\2', name_str)

    def merge_channel_record(ch_id: str, name: str, aliases: Iterable[str] = (), tvg_ids: Iterable[str] = (), comp_ids: Iterable[str] = (), source_ids: Iterable[str] = (), group: str = "") -> None:
        base_name = normalize_channel_number_zero(html.unescape(name or ch_id).strip())
        if not base_name or is_junk_or_24h_channel(base_name):
            return
        canon_name = inferir_emissora_pelo_grupo(normalizar_canal(base_name), group)
        if is_junk_or_24h_channel(canon_name):
            return
        norm_key = original_normalize_name(canon_name) or canon_name.strip().upper()
        if not norm_key:
            return
        if norm_key not in grouped_by_norm:
            grouped_by_norm[norm_key] = {
                "id": ch_id or canon_name,
                "name": strip_qualities_keep_case(base_name) or canon_name,
                "aliases": set(),
                "raw_aliases": set(),
                "tvg_ids_to_export": set(),
                "compatibility_ids": set(),
                "source_ids": set(),
                "group": group or "",
            }
        rec = grouped_by_norm[norm_key]
        rec_base_name = rec["name"]

        if not has_conflicting_channel_number(rec_base_name, base_name) and not has_conflicting_region(rec_base_name, base_name):
            rec["raw_aliases"].add(base_name)
            rec["aliases"].add(strip_qualities_keep_case(base_name) or base_name)

        for a in aliases:
            if a:
                a_raw = normalize_channel_number_zero(html.unescape(str(a)).strip())
                if not a_raw or is_junk_or_24h_channel(a_raw) or has_conflicting_channel_number(rec_base_name, a_raw) or has_conflicting_region(rec_base_name, a_raw):
                    continue
                rec["raw_aliases"].add(a_raw)
                a_clean = strip_qualities_keep_case(a_raw)
                if a_clean:
                    rec["aliases"].add(a_clean)
        for t in tvg_ids:
            t_str = str(t).strip()
            if t_str and is_valid_provider_tvid(rec_base_name, t_str) and not has_conflicting_channel_number(rec_base_name, t_str) and not has_conflicting_region(rec_base_name, t_str):
                rec["tvg_ids_to_export"].add(t_str)
        for c in comp_ids:
            c_str = str(c).strip()
            if c_str and is_valid_provider_tvid(rec_base_name, c_str) and not has_conflicting_channel_number(rec_base_name, c_str) and not has_conflicting_region(rec_base_name, c_str):
                rec["compatibility_ids"].add(c_str)
        for s in source_ids:
            s_str = str(s).strip()
            if s_str and is_valid_provider_tvid(rec_base_name, s_str) and not has_conflicting_channel_number(rec_base_name, s_str) and not has_conflicting_region(rec_base_name, s_str):
                rec["source_ids"].add(s_str)
        if group and not rec["group"]:
            rec["group"] = group

    def tvg_id_id_ok(m) -> bool:
        return bool(m and m.group(1).strip())

    def parse_m3u_file_into_records(m3u_file: Path) -> int:
        count = 0
        raw_lines = m3u_file.read_text("utf-8", errors="ignore").splitlines()
        idx = 0
        while idx < len(raw_lines):
            line = raw_lines[idx].strip()
            if not (line.upper().startswith("#EXTINF") or ('tvg-' in line.lower() and ',' in line)):
                idx += 1
                continue

            current_extinf = line if line.upper().startswith("#EXTINF") else f"#EXTINF:-1 {line}"
            next_line = raw_lines[idx + 1].strip() if (idx + 1) < len(raw_lines) else ""
            if next_line.lower().startswith(("http://", "https://", "rtmp://", "rtsp://")):
                idx += 2
                lower_url = next_line.split("?")[0].lower()
                if lower_url.endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm')):
                    continue
            else:
                idx += 1

            tvg_id_match = re.search(r'tvg-id="(.*?)"', current_extinf, re.IGNORECASE)
            tvg_name_match = re.search(r'tvg-name="(.*?)"', current_extinf, re.IGNORECASE)
            group_match = re.search(r'group-title="(.*?)"', current_extinf, re.IGNORECASE)
            name_match = re.search(r',([^,]*?)$', current_extinf)
            
            tvg_id = tvg_id_match.group(1).strip() if tvg_id_id_ok(tvg_id_match) else ""
            tvg_name = html.unescape(tvg_name_match.group(1).strip()) if tvg_name_match else ""
            group = html.unescape(group_match.group(1).strip()) if group_match else ""
            name = html.unescape(name_match.group(1).strip()) if name_match else ""
            
            if any(blacklisted in group.upper() for blacklisted in VOD_BLACKLIST):
                continue
                
            target_id = tvg_name or name
            if not target_id or is_junk_or_24h_channel(target_id):
                continue

            if tvg_id: id_observations.append((tvg_id, tvg_name or name))
            
            merge_channel_record(
                ch_id=target_id,
                name=name or tvg_name,
                aliases=[x for x in (tvg_name, name) if x],
                tvg_ids=[tvg_id] if tvg_id else [],
                group=group,
            )
            count += 1
        return count

    if CATALOG_PATH.exists():
        try:
            print(f"Lendo Dicionário Persistente ({CATALOG_PATH})...")
            cat_data = json.loads(CATALOG_PATH.read_text("utf-8"))
            cat_list = cat_data.get("channels", []) if isinstance(cat_data, dict) else (cat_data if isinstance(cat_data, list) else [])
            for ch in cat_list:
                if not isinstance(ch, dict): continue
                ch_groups = ch.get("groups", []) or []
                grp_str = " | ".join(str(g) for g in ch_groups) if isinstance(ch_groups, list) else str(ch.get("group", ch.get("group-title", "")))
                ch_name_val = str(ch.get("name", ch.get("id", ""))).strip()
                if is_junk_or_24h_channel(ch_name_val):
                    continue
                all_cat_tvg_ids = [
                    *(ch.get("old_tvg_ids") or []),
                    *(ch.get("tvg_ids_to_export") or []),
                    *(ch.get("compatibility_ids") or []),
                ]
                for tid in all_cat_tvg_ids:
                    if tid and ch_name_val and not has_conflicting_channel_number(ch_name_val, str(tid)):
                        id_observations.append((str(tid).strip(), ch_name_val))
                merge_channel_record(
                    ch_id=str(ch.get("id", ch.get("name", ""))).strip(),
                    name=ch_name_val,
                    aliases=[
                        *(ch.get("aliases", []) or []),
                        *(ch.get("tvg_names", []) or []),
                        *(ch.get("original_names", []) or []),
                        *(ch.get("blank_tvg_id_names", []) or []),
                    ],
                    tvg_ids=all_cat_tvg_ids,
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

    AUTO_POISONED_IDS.clear()
    AUTO_POISONED_IDS.update(detect_poisoned_ids(id_observations))
    print(f"IDs de lista não confiáveis (usados por canais diferentes): {len(AUTO_POISONED_IDS)} -> serão ignorados; o casamento passa a ser só pelo nome.")

    if ESPELHO_PATH.exists():
        try:
            print("Lendo Espelho de Segurança...")
            espelho_data = json.loads(ESPELHO_PATH.read_text("utf-8"))
            for ch in espelho_data.get("channels", []):
                ch_name = str(ch.get("name", "")).strip()
                if is_junk_or_24h_channel(ch_name):
                    continue
                merge_channel_record(
                    ch_id=str(ch.get("id", "")).strip(),
                    name=ch_name,
                    aliases=[*(ch.get("aliases", []) or []), *(ch.get("raw_aliases", []) or [])],
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
                        if not name or is_junk_or_24h_channel(name):
                            continue
                        if epg_id:
                            id_observations.append((epg_id, name))
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

    AUTO_POISONED_IDS.update(detect_poisoned_ids(id_observations))

    for v in grouped_by_norm.values():
        v["aliases"] = sorted(v["aliases"])
        v["raw_aliases"] = sorted(v["raw_aliases"])
        v["tvg_ids_to_export"] = sorted(v["tvg_ids_to_export"])
        v["compatibility_ids"] = sorted(v["compatibility_ids"])
        v["source_ids"] = sorted(v["source_ids"])
        entries.append(v)
        
    print(f"\nCatálogo unificado pronto: {len(entries)} emissoras base únicas ({len(AUTO_POISONED_IDS)} IDs compartilhados bloqueados).")

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
        alias_tiers = aliases_tiers_for(entry)
        source_ids = [str(x) for x in entry.get("source_ids", []) or []]
        matches = find_tiered_matches_across_sources(active_sources, alias_tiers, source_ids, visible_name)

        override_source_key, override_id = manual_override(overrides, target_id)
        forced = False
        if override_source_key and override_id and (src := source_by_key.get(override_source_key)) and override_id in src.channels:
            override_match = ChannelMatch(src, override_id, "override manual", 1.0, tier=1)
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

        evaluations = [chosen, *[ev for ev in evaluations if ev is not chosen]]
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
    timings["serializacao_e_gzip"] = round(time.perf_counter() - stage_start, 3)

    source_counts = {source.label: sum(1 for row in report_rows if row.get("source") == source.label) for source in active_sources}
    counts = {
        "playlist_channels": len(entries),
        "matched": sum(1 for row in report_rows if row["status"] == "matched"),
        "missing": sum(1 for row in report_rows if row["status"] == "missing"),
        "programmes": programme_count,
        "exported_ids": ids_count,
        "timing_seconds": timings, "selected_by_source": source_counts,
    }

    (OUTPUT_DIR / "report.json").write_text(json.dumps({"summary": counts, "source_errors": source_errors, "channels": report_rows}, ensure_ascii=False, indent=2) + "\n", "utf-8")
    (OUTPUT_DIR / "validation.json").write_text(json.dumps({"generated_at": now.isoformat(), "active_sources": [s.label for s in active_sources], "summary": counts, "channels": validations}, ensure_ascii=False, indent=2) + "\n", "utf-8")

    report_md = [
        "# Relatório do EPG Universal — Filmes Limpos & Séries Detalhadas", "",
        f"- Emissoras base consolidadas: **{counts['playlist_channels']}**",
        f"- Emissoras com grade encontrada: **{counts['matched']}**",
        f"- Canais XML gerados: **{counts['exported_ids']}**",
        f"- Total de programas gravados: **{counts['programmes']}**",
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
