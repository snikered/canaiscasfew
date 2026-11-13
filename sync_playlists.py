#!/usr/bin/env python3
"""Sincronizador M3U + Mapa JSON para Servidor Xtream Proxy (Motor EPG de Alta Precisão):
- Corrige colisões como 'HBO 2' -> 'HBO' priorizando IDs exatos sobre display-names secundários.
- Limpa sufixos de servidor como (BR2), [P], (Eventos), (PPV), ², H265, FHD/HD/SD/4K.
- Converte sinônimos regionais e de canais (Discovery H&H, Band TV SP, SBT TV RJ, Globo BH, etc.).
- Bloqueia tvg-ids falsos do servidor bruto (ex: agromais.br no Cartoonito e globosp.br em Globos regionais).
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "TiviMate/4.7.0 (Android 12)",
    "IPTVSmartersPlayer",
    "VLC/3.0.20 LibVLC/3.0.20",
]

ADULT_FILTER_RE = re.compile(
    r"(\bADULTO[S]?\b|\bXXX\b|\bSEXO\b|\bPORN[OÔ]?\b|\b18\+\b|\+\s*18\b|\bONLYFANS\b|"
    r"\bSEXY\s*HOT\b|\bPLAYBOY\b|\bSEX\s*PRIV[EÉ]\b|\bVENUS\b|\bBRAZZERS\b)",
    re.IGNORECASE,
)

VOD_URL_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".divx")
_RE_TVG_ID = re.compile(r'tvg-id="[^"]*"', re.IGNORECASE)
_RE_GROUP_TITLE = re.compile(r'group-title="([^"]*)"', re.IGNORECASE)
_RE_STREAM_ID_FROM_URL = re.compile(r"/([^/?#]+?)(?:\.[a-z0-9]+)?(?:\?.*)?$", re.IGNORECASE)
_RE_SERIES_EPISODE = re.compile(r"\b(?:S\d{1,2}\s*E\d{1,3}|T\d{1,2}\s*EP?\d{1,3})\b", re.IGNORECASE)

# Canais informativos/24h que NUNCA devem receber tvg-id para não puxarem guia errado
_RE_NO_EPG_CHANNEL = re.compile(
    r"(^24H|\b24\s*H(?:S|ORAS)?\b|JOGOS\s+DO\s+DIA|ATUALIZA[CÇ][AÃ]O\s+DE\s+CONTE[UÚ]DO|"
    r"GUIA\s+DE\s+CANAIS|^PROGRAMA\s*-\s*|^ESPECIAL\s*-\s*)",
    re.IGNORECASE,
)

# Sinônimos diretos: Nome limpo na lista -> Lista de candidatos de alta precisão no EPG
CHANNEL_SYNONYMS: dict[str, list[str]] = {
    "HBO 2": ["HBO 2", "HBO2", "HBO2.br", "hbo2.br"],
    "HBO": ["HBO", "HBO.br", "hbo.br"],
    "HBO PLUS": ["HBO Plus", "HBOPlus.br", "hboplusbrasil.br"],
    "HBO FAMILY": ["HBOFamily.br", "HBO Family"],
    "HBO SIGNATURE": ["HBOSignature.br", "HBO Signature"],
    "HBO MUNDI": ["HBOMundi.br", "HBO Mundi"],
    "HBO POP": ["HBOPop.br", "HBO Pop"],
    "HBO XTREME": ["HBOXtreme.br", "HBO Xtreme"],
    "DISCOVERY H H": ["Discovery Home & Health", "DiscoveryHomeHealth.br", "discoveryhomehealth.br"],
    "DISCOVERY HOME HEALTH": ["Discovery Home & Health", "DiscoveryHomeHealth.br"],
    "UNIVERSAL STUDIO": ["Studio Universal", "StudioUniversal.br", "studiouniversal.br", "Universal Studio"],
    "STUDIO UNIVERSAL": ["Studio Universal", "StudioUniversal.br", "studiouniversal.br"],
    "UNIVERSAL CHANNEL": ["Universal TV", "Universal Channel", "Universal.br", "universal.br"],
    "SONY CHANNEL": ["Sony Channel", "Sony", "Sony.br", "sony.br"],
    "WARNER": ["Warner Channel", "Warner", "Warner.br", "warnerchannel.br"],
    "WARNER CHANNEL": ["Warner Channel", "Warner", "Warner.br", "warnerchannel.br"],
    " TNT": ["TNT", "TNT.br", "tnt.br"],
    "TNT SERIES": ["TNT Séries", "TNT Series", "TNTSeries.br", "tntseries.br"],
    "TNT NOVELAS": ["TNT Novelas", "TNTNovelas.br"],
    "H2": ["History 2", "H2", "History2.br", "history2.br"],
    "HISTORY 2": ["History 2", "H2", "History2.br", "history2.br"],
    "HISTORY": ["HistoryChannel.br", "History Channel", "History"],
    "CANAL OFF": ["OFF", "Canal OFF", "Off.br"],
    "OFF": ["OFF", "Canal OFF", "Off.br"],
    "SPORTV": ["SporTV 1", "SporTV", "SporTV.br", "sportv.br"],
    "SPORTV 1": ["SporTV 1", "SporTV", "SporTV.br", "sportv.br"],
    "SPORTV 2": ["SPORTV 2", "SporTV 2", "SporTV2.br", "sportv2.br"],
    "SPORTV 3": ["SPORTV 3", "SporTV 3", "SporTV3.br", "sportv3.br"],
    "PREMIERE CLUBES": ["PREMIERE CLUBES", "Premiere Clubes", "PremiereClubes.br", "premiereclubes.br"],
    "JOVEM PAN": ["Jovem Pan News", "JOVEM PAN", "jovempannews.br"],
    "CNN BRASIL": ["cnn.brasil.br", "CNN Brasil", "cnnbrasil.br"],
    "GLOBO SP": ["GB SP", "Globo SP", "globosp.br"],
    "GLOBO RJ": ["GB RJ", "Globo RJ", "globorj.br"],
    "GLOBO MINAS": ["GB MINAS", "Globo Minas", "globominas.br"],
    "GLOBO BELO HORIZONTE": ["GB MINAS", "Globo Minas", "GLOBO BELO HORIZONTE"],
    "GLOBO NORDESTE": ["GB NORDESTE", "GLOBO TV NORDESTE", "Globo Nordeste"],
    "GLOBO PERNAMBUCO": ["GB NORDESTE", "GLOBO TV NORDESTE", "GLOBO PERNAMBUCO"],
    "GLOBO TV NORDESTE": ["GB NORDESTE", "GLOBO TV NORDESTE"],
    "GLOBO BRASILIA": ["GLOBO BRASÍLIA", "Globo Brasilia", "GB BRASILIA"],
    "GLOBO REDE BAHIA": ["GB TV BAHIA", "Globo Rede Bahia", "GLOBO TV BAHIA"],
    "GLOBO TV BAHIA": ["GB TV BAHIA", "Globo Rede Bahia", "GLOBO TV BAHIA"],
    "GLOBO RBS PORTO ALEGRE": ["Globo RBS TV Porto Alegre", "GB RBS PORTO ALEGRE"],
    "GLOBO RBS TV PORTO ALEGRE": ["Globo RBS TV Porto Alegre", "GB RBS PORTO ALEGRE"],
    "RBS PASSO FUNDO GLOBO": ["GLOBO RBS TV PASSO FUNDO", "RBS PASSO FUNDO GLOBO"],
    "RBS CAXIAS": ["GLOBO RBS TV CAXIAS DO SUL", "RBS CAXIAS"],
    "GLOBO RPC CURITIBA": ["Globo RPC TV Curitiba", "GB RPC CURITIBA"],
    "GLOBO RPC TV CURITIBA": ["Globo RPC TV Curitiba", "GB RPC CURITIBA"],
    "GLOBO TV INTEG JUIZ DE FORA": ["Globo TV Integracao Juiz de Fora", "GLOBO TV INTEG JUIZ DE FORA"],
    "GLOBO TV INTEGRACAO JUIZ DE FORA": ["Globo TV Integracao Juiz de Fora", "GLOBO TV INTEG JUIZ DE FORA"],
    "GLOBO TV VANG S J CAMPOS": ["Globo TV Vanguarda S J dos Campos", "GLOBO TV VANG S J CAMPOS"],
    "GLOBO TV VANGUARDA S J DOS CAMPOS": ["Globo TV Vanguarda S J dos Campos", "GLOBO TV VANG S J CAMPOS"],
    "GLOBO VERDES MARES": ["GB TV VERDES MARES", "GLOBO VERDES MARES", "Globo TV Verdes Mares"],
    "GLOBO TV VERDES MARES": ["GB TV VERDES MARES", "GLOBO VERDES MARES", "Globo TV Verdes Mares"],
    "RECORD SP": ["RECORDTV SP", "Record SP", "RecordTV SP"],
    "RECORD TV SP": ["RECORDTV SP", "Record SP", "RecordTV SP"],
    "RECORD RJ": ["RECORDTV RJ", "Record RJ", "RecordTV RJ"],
    "RECORD TV RJ": ["RECORDTV RJ", "Record RJ", "RecordTV RJ"],
    "RECORD BRASILIA": ["RECORD BRASILIA", "RecordTV Brasilia"],
    "RECORD TV BRASILIA": ["RECORD BRASILIA", "RecordTV Brasilia"],
    "RECORD GOIAS": ["RECORDTV GOIAS", "Record Goias"],
    "RECORD TV GOIAS": ["RECORDTV GOIAS", "Record Goias"],
    "RECORD MANAUS": ["RECORDTV MANAUS", "Record Manaus"],
    "RECORD TV NEWS": ["RECORD NEWS", "Record News", "recordnews.br"],
    "BAND TV SP": ["BAND SP", "Band SP", "band.br"],
    "BAND TV RJ": ["BAND RJ", "Band RJ"],
    "BAND TV RS": ["BAND RS", "Band RS"],
    "BAND TV PR": ["BAND PARANA", "Band Parana"],
    "BAND TV PARANA": ["BAND PARANA", "Band Parana"],
    "BAND TV MINAS": ["BAND TV MINAS", "Band Minas"],
    "BAND TV GOIANIA": ["BAND TV GOIANIA", "Band Goiania"],
    "BAND TV CAMPINAS": ["BAND CAMPINAS", "Band Campinas"],
    "BAND TV NEWS": ["BAND NEWS", "Band News", "bandnews.br"],
    "BAND TV SPORTS": ["BAND SPORTS", "BandSports", "bandsports.br"],
    "BANDSPORTS": ["BAND SPORTS", "BandSports", "bandsports.br"],
    "SBT TV SP": ["SBT SP", "SBT", "sbt.br"],
    "SBT TV RJ": ["SBT RJ", "SBT Rio"],
    "SBT TV RS": ["SBT RS", "SBT TV RBS"],
    "SBT TV RBS": ["SBT RS", "SBT TV RBS"],
    "SBT TV BRASILIA": ["SBT BRASILIA"],
    "SBT TV HD PIAUI": ["SBT TV PIAUI"],
    "SBT TV HD RN": ["SBT TV RN"],
}


def norm_lookup_key(title: str) -> str:
    s = html.unescape(title or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


def clean_channel_title_for_epg(raw_name: str) -> str:
    """Limpa todas as tags de resolução, codec, servidor (BR2), [P], (Eventos), ² do nome do canal."""
    s = html.unescape(raw_name or "")
    # Remove (BR1), (BR2), (Eventos), (PPV), [H265], [P], etc.
    s = re.sub(r"\(\s*(?:BR\s*\d+|EVENTOS?|PPV|BACKUP|ALT)\s*\)", " ", s, flags=re.I)
    s = re.sub(r"\[\s*(?:H\.?26[45]|HEVC|P|BR\s*\d+|FHD|HD|SD|4K|UHD)\s*\]", " ", s, flags=re.I)
    # Remove expoentes de links alternativos (², ³, ¹)
    s = re.sub(r"[¹²³]+", " ", s)
    # Remove qualidades soltas
    s = re.sub(r"\b(?:FHD|UHD|4K|HD|SD|H265|H264|HEVC|1080P|720P)\b", " ", s, flags=re.I)
    return norm_lookup_key(s)


def norm_movie_title(title: str) -> str:
    s = html.unescape(title or "")
    s = re.sub(r"^[+\-\*\s]+", "", s)
    s = re.sub(r"\(\d{4}\)|\[\d{4}\]", " ", s)
    s = re.sub(r"\b(FHD|HD|SD|4K|UHD|H265|LEG|DUB|DUBLADO|LEGENDADO|DUAL)\b", " ", s, flags=re.I)
    return norm_lookup_key(s)


def norm_series_base_title(title: str) -> str:
    s = html.unescape(title or "")
    s = re.sub(r"^[+\-\*\s]+", "", s)
    s = re.split(r"\b(?:S\d{1,2}\s*E\d{1,3}|S\d{1,2}\b|T\d{1,2}\s*EP?\d{1,3}|TEMPORADA\s*\d+)", s, maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\(\d{4}\)|\[\d{4}\]", " ", s)
    s = re.sub(r"\b(FHD|HD|SD|4K|UHD|H265|LEG|DUB|DUBLADO|LEGENDADO|DUAL)\b", " ", s, flags=re.I)
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


def fetch_all_category_maps(url: str) -> dict[str, any]:
    maps: dict[str, any] = {
        "live_by_id": {},
        "live_by_name": {},
        "vod_by_id": {},
        "vod_by_name": {},
        "series_by_id": {},
        "series_by_name": {},
        "raw_live_streams": [],
    }

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        return maps

    user = query["username"][0]
    pwd = query["password"][0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"

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
        if isinstance(s_live, list):
            maps["raw_live_streams"] = s_live
        for ch in (s_live if isinstance(s_live, list) else []):
            if not isinstance(ch, dict):
                continue
            grp = live_cats.get(str(ch.get("category_id", "")), "")
            if not grp or "COMPLETO" in grp.upper():
                continue
            if ch.get("stream_id"):
                maps["live_by_id"][str(ch["stream_id"])] = grp
            if ch.get("name"):
                raw_n = str(ch["name"])
                maps["live_by_name"][norm_lookup_key(raw_n)] = grp
                maps["live_by_name"][name_key(raw_n)] = grp
        print(f"  -> ✓ Categorias de Canais via API: {len(live_cats)} grupos ({len(maps['live_by_name'])} canais).")
    except Exception as e:
        print(f"  -> Nota: Mapa Live indisponível ({e}).")

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
                maps["vod_by_name"][norm_movie_title(raw_v)] = grp
        print(f"  -> ✓ Categorias de Filmes via API: {len(vod_cats)} grupos ({len(maps['vod_by_name'])} filmes).")
    except Exception as e:
        print(f"  -> Nota: Mapa VOD indisponível ({e}).")

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
            if not isinstance(s, dict):
                continue
            grp = ser_cats.get(str(s.get("category_id", "")), "")
            if not grp:
                continue
            if s.get("series_id"):
                maps["series_by_id"][str(s["series_id"])] = grp
            if s.get("name"):
                raw_s = str(s["name"])
                maps["series_by_name"][norm_lookup_key(raw_s)] = grp
                maps["series_by_name"][norm_movie_title(raw_s)] = grp
                maps["series_by_name"][norm_series_base_title(raw_s)] = grp
        print(f"  -> ✓ Categorias de Séries via API: {len(ser_cats)} grupos ({len(maps['series_by_name'])} séries).")
    except Exception as e:
        print(f"  -> Nota: Mapa Séries indisponível ({e}).")

    return maps


def infer_fallback_channel_group(channel_name: str, current_group: str) -> str:
    if current_group and "COMPLETO" not in current_group.upper():
        return current_group

    n = norm_lookup_key(channel_name)
    if channel_name.upper().strip().startswith("24H") or " 24H" in n:
        return "Canais ⫸ 24 Horas"
    if any(w in n for w in ("PREMIERE", "SPORTV", "ESPN", "COMBATE", "BANDSPORTS", "BAND SPORTS", "DAZN", "CAZE", "GOAT", "NOSSO FUTEBOL", "PARAMOUNT", "NBA", "NFL", "UFC", "FIGHT", "CONMEBOL", "PAULISTAO", "BRASILEIRAO", "LALIGA", "BUNDESLIGA", "SPORTYNET", "NSPORTS", "XSPORTS")):
        return "Canais ⫸ Esportes⚽"
    if any(w in n for w in ("GLOBO ", "SBT", "RECORD", "BAND ", "REDETV", "REDE TV", "CULTURA", "TV BRASIL", "TV GAZETA", "TV DIARIO", "REDE VIDA", "APARECIDA", "CANCAO NOVA", "NOVO TEMPO", "EVANGELIZAR", "PAI ETERNO", "REDE GOSPEL", "RIT TV", "REDE CNT", "REDE SUPER", "RBS ", "RPC ")):
        if "NEWS" not in n:
            return "Canais ⫸ Abertos"
    if any(w in n for w in ("HBO", "TELECINE", "MEGAPIX", "TNT ", "SPACE", "CINEMAX", "WARNER", "SONY", "AXN", "UNIVERSAL", "STUDIO UNIVERSAL", "STAR ", "AMC", "PARAMOUNT", "TCM", "CINE SKY", "PRIME BOX", "CURTA", "FILME")):
        if "NOVELA" not in n:
            return "Canais ⫸ Filmes e Séries"
    if any(w in n for w in ("CARTOON", "DISCOVERY KIDS", "GLOOB", "DISNEY", "NICK", "TOONCAST", "RA TIM BUM", "ZOOMOO", "DREAMWORKS", "ANIME", "INFANTIL")):
        return "Canais ⫸ Infantil"
    if any(w in n for w in ("NEWS", "CNN", "JOVEM PAN", "BLOOMBERG", "SENADO", "CAMARA", "JUSTICA", "ASSEMBLEIA")):
        return "Canais ⫸ Noticias"
    if any(w in n for w in ("DISCOVERY", "HISTORY", "H2", "ANIMAL PLANET", "NAT GEO", "NATIONAL GEOGRAPHIC", "LOVE NATURE", "SCIENCE", "THEATER", "TURBO", "WORLD", "INVESTIGACAO")):
        return "Canais ⫸ Documentarios"
    return "Canais ⫸ Variedades"


def load_epg_id_index(epg_path: Path) -> dict[str, str]:
    """Carrega o índice EPG em 2 fases para garantir que o ID primário nunca seja sobrescrito por um display-name secundário."""
    primary_index: dict[str, str] = {}
    secondary_index: dict[str, str] = {}

    if not epg_path.exists():
        print(f"Aviso: {epg_path} não encontrado!")
        return primary_index

    with gzip.open(epg_path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "channel":
                cid = (elem.attrib.get("id") or "").strip()
                if cid:
                    # 1. Chaves primárias (do próprio atributo id="...") têm prioridade máxima
                    cid_no_br = re.sub(r"\.br$", "", cid, flags=re.I)
                    for k in (
                        norm_lookup_key(cid),
                        norm_lookup_key(cid_no_br),
                        name_key(cid),
                        name_key(cid_no_br),
                        name_key(normalizar_canal(cid)),
                    ):
                        if k and k not in primary_index:
                            primary_index[k] = cid

                    # 2. Chaves secundárias (display-name) só entram se não colidirem com um ID primário
                    for child in elem:
                        if child.tag.rsplit("}", 1)[-1] == "display-name" and child.text:
                            dn = child.text.strip()
                            dn_no_br = re.sub(r"\.br$", "", dn, flags=re.I)
                            for k_dn in (
                                norm_lookup_key(dn),
                                norm_lookup_key(dn_no_br),
                                name_key(dn),
                                name_key(normalizar_canal(dn)),
                            ):
                                if k_dn and k_dn not in secondary_index:
                                    secondary_index[k_dn] = cid
                elem.clear()

    # Mescla mantendo 100% de prioridade para primary_index
    merged = {**secondary_index, **primary_index}
    print(f"✓ Índice EPG de Alta Precisão carregado ({len(primary_index)} IDs primários, {len(merged)} chaves totais).")
    return merged


def is_orig_tvg_id_poisoned(clean_ch_name: str, orig_id: str) -> bool:
    """Detecta tvg-ids falsos que vêm de fábrica na lista bruta do servidor."""
    oid = orig_id.lower().strip()
    if not oid:
        return True
    # UUIDs de canais informativos
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", oid):
        return True
    # agromais.br usado erroneamente no Cartoonito, Travel Box e SBT Recife
    if oid == "agromais.br" and "AGRO" not in clean_ch_name:
        return True
    # globonews.br usado erroneamente na Jovem Pan
    if oid == "globonews.br" and "GLOBO NEWS" not in clean_ch_name and "GLOBONEWS" not in clean_ch_name:
        return True
    # globosp.br usado em dezenas de afiliadas regionais (Bahia, Minas, Recife, etc.)
    if oid == "globosp.br" and clean_ch_name not in {"GLOBO SP", "GLOBO", "TV GLOBO"}:
        return True
    # recordtvbrasília.br usado em Record SP, RJ, Minas, Goiás, Manaus
    if "recordtvbras" in oid and "BRASILIA" not in clean_ch_name:
        return True
    # sbt.br ou band.br genérico usado em afiliadas regionais que têm guia próprio
    if oid == "sbt.br" and clean_ch_name not in {"SBT", "SBT SP", "SBT TV SP"}:
        return True
    if oid == "band.br" and clean_ch_name not in {"BAND", "BAND SP", "BAND TV SP"}:
        return True
    return False


def resolve_best_tvg_id(channel_name: str, original_tvg_id: str, epg_index: dict[str, str]) -> str:
    # 1. Canais 24h, Jogos do Dia, Programas 24h ficam SEMPRE com tvg-id=""
    if _RE_NO_EPG_CHANNEL.search(channel_name):
        return ""

    clean_title = clean_channel_title_for_epg(channel_name)

    # 2. Verifica Tabela de Sinônimos Calibrados (Ex: HBO 2, Discovery H&H, Band TV SP, Globo BH, etc.)
    if clean_title in CHANNEL_SYNONYMS:
        for syn in CHANNEL_SYNONYMS[clean_title]:
            for k_syn in (norm_lookup_key(syn), name_key(syn)):
                if k_syn and k_syn in epg_index:
                    return epg_index[k_syn]

    # 3. Variações automáticas limpando "TV" intermediário (ex: "SBT TV CAMPINAS" -> "SBT CAMPINAS")
    no_tv_mid = re.sub(r"^(GLOBO|SBT|RECORD|BAND)\s+TV\s+", r"\1 ", clean_title)

    # 4. Busca direta pelo nome limpo do canal no índice do EPG
    for candidate in (
        clean_title,
        no_tv_mid,
        strip_qualities_keep_case(channel_name),
        normalizar_canal(clean_title),
        normalizar_canal(no_tv_mid),
    ):
        for k in (norm_lookup_key(candidate), name_key(candidate)):
            if k and k in epg_index:
                return epg_index[k]

    # 5. Se não achou pelo nome, tenta pelo original_tvg_id APENAS se ele não for um ID envenenado
    if original_tvg_id and not is_orig_tvg_id_poisoned(clean_title, original_tvg_id):
        orig_no_br = re.sub(r"\.(?:br|bt)$", "", original_tvg_id.strip(), flags=re.I)
        for k_id in (norm_lookup_key(original_tvg_id), norm_lookup_key(orig_no_br), name_key(original_tvg_id), name_key(orig_no_br)):
            if k_id and k_id in epg_index:
                return epg_index[k_id]

    # 6. Se o canal realmente não existe no EPG (ex: PPV desligado), deixa vazio ou nome limpo sem lixo
    return ""


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


def classify_stream_type(stream_url: str, curr_grp: str, item_name: str) -> str:
    clean_path = stream_url.split("?", 1)[0].lower()
    grp_norm = norm_lookup_key(curr_grp)
    name_upper = item_name.upper().strip()

    if "/live/" in clean_path or name_upper.startswith("24H-") or grp_norm == "RADIOS":
        return "live"
    if "/series/" in clean_path or "SERIE" in grp_norm or _RE_SERIES_EPISODE.search(item_name):
        return "series"
    if "/movie/" in clean_path or "FILME" in grp_norm or clean_path.endswith(VOD_URL_EXTENSIONS):
        return "movie"
    return "live"


def download_raw_m3u_exact_url(url: str, target_path: Path) -> bool:
    br_proxy = os.environ.get("BR_PROXY", "").strip()
    session = create_http_client()

    for ua in IPTV_USER_AGENTS:
        headers = build_br_headers(ua, url)
        try:
            with session.get(url, headers=headers, stream=True, timeout=240, allow_redirects=True) as resp:
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
        except Exception:
            continue

    cmd = [
        "curl", "-sS", "-L", "--http1.1",
        "--max-time", "300",
        "--connect-timeout", "25",
        "-A", IPTV_USER_AGENTS[0],
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

    return False


def build_calibrated_playlists(
    url: str,
    epg_index: dict[str, str],
    epg_public_url: str,
    full_out_path: Path,
    live_only_out_path: Path,
    xtream_map_out_path: Path,
) -> tuple[int, int, int, int]:
    tmp_raw = tempfile.NamedTemporaryFile(delete=False, suffix=".raw.m3u")
    tmp_raw_path = Path(tmp_raw.name)
    tmp_raw.close()

    live_count = 0
    matched_epg = 0
    movie_count = 0
    series_count = 0

    xtream_tvgid_map: dict[str, str] = {}

    try:
        maps = fetch_all_category_maps(url)
        live_by_id = maps["live_by_id"]
        live_by_name = maps["live_by_name"]
        vod_by_id = maps["vod_by_id"]
        vod_by_name = maps["vod_by_name"]
        series_by_id = maps["series_by_id"]
        series_by_name = maps["series_by_name"]

        for ch in maps.get("raw_live_streams", []):
            if not isinstance(ch, dict):
                continue
            ch_name = html.unescape(str(ch.get("name", "")).strip())
            orig_id = str(ch.get("epg_channel_id", "")).strip()
            sid = str(ch.get("stream_id", "")).strip()
            if ch_name:
                best_id = resolve_best_tvg_id(ch_name, orig_id, epg_index)
                xtream_tvgid_map[norm_lookup_key(ch_name)] = best_id
                if sid:
                    xtream_tvgid_map[f"id:{sid}"] = best_id

        print("  -> Baixando arquivo .m3u original...")
        if not download_raw_m3u_exact_url(url, tmp_raw_path):
            raise RuntimeError("Falha ao baixar o arquivo .m3u bruto.")

        raw_size_mb = tmp_raw_path.stat().st_size / (1024 * 1024)
        print(f"  -> ✓ Arquivo baixado ({raw_size_mb:.2f} MB). Processando Canais, Filmes e Séries...")

        header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"

        with (
            tmp_raw_path.open("r", encoding="utf-8", errors="ignore") as raw_in,
            full_out_path.open("w", encoding="utf-8") as full_out,
            live_only_out_path.open("w", encoding="utf-8") as live_only_out,
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
                    grp_m = _RE_GROUP_TITLE.search(extinf_line)
                    curr_grp = grp_m.group(1).strip() if grp_m else ""
                    tvg_id_m = re.search(r'tvg-id="([^"]*)"', extinf_line, re.I)
                    tvg_name_m = re.search(r'tvg-name="([^"]*)"', extinf_line, re.I)
                    comma_name_m = re.search(r',([^,]*)$', extinf_line)

                    orig_tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""
                    item_name = html.unescape(
                        (comma_name_m.group(1).strip() if comma_name_m else "")
                        or (tvg_name_m.group(1).strip() if tvg_name_m else "")
                    )

                    if REMOVE_ADULT and (ADULT_FILTER_RE.search(curr_grp) or ADULT_FILTER_RE.search(item_name)):
                        pending_lines.clear()
                        continue

                    stream_id_m = _RE_STREAM_ID_FROM_URL.search(line.strip())
                    stream_id = stream_id_m.group(1) if stream_id_m else ""

                    kind = classify_stream_type(line.strip(), curr_grp, item_name)

                    if kind == "series":
                        base_series_key = norm_series_base_title(item_name)
                        real_ser_grp = (
                            series_by_id.get(stream_id, "")
                            or series_by_name.get(base_series_key, "")
                            or series_by_name.get(norm_lookup_key(item_name), "")
                        )
                        if REMOVE_ADULT and real_ser_grp and ADULT_FILTER_RE.search(real_ser_grp):
                            pending_lines.clear()
                            continue

                        if real_ser_grp:
                            pending_lines[extinf_idx] = set_group_title_in_extinf(extinf_line, real_ser_grp)

                        for pl in pending_lines:
                            full_out.write(pl + "\n")
                        full_out.write(line + "\n")
                        series_count += 1

                    elif kind == "movie":
                        real_movie_grp = (
                            vod_by_id.get(stream_id, "")
                            or vod_by_name.get(norm_lookup_key(item_name), "")
                            or vod_by_name.get(norm_movie_title(item_name), "")
                        )
                        if REMOVE_ADULT and real_movie_grp and ADULT_FILTER_RE.search(real_movie_grp):
                            pending_lines.clear()
                            continue

                        if real_movie_grp:
                            pending_lines[extinf_idx] = set_group_title_in_extinf(extinf_line, real_movie_grp)

                        for pl in pending_lines:
                            full_out.write(pl + "\n")
                        full_out.write(line + "\n")
                        movie_count += 1

                    else:
                        real_live_grp = (
                            live_by_id.get(stream_id, "")
                            or live_by_name.get(norm_lookup_key(item_name), "")
                            or live_by_name.get(name_key(item_name), "")
                        )
                        if not real_live_grp or "COMPLETO" in real_live_grp.upper():
                            real_live_grp = infer_fallback_channel_group(item_name, curr_grp)

                        if REMOVE_ADULT and real_live_grp and ADULT_FILTER_RE.search(real_live_grp):
                            pending_lines.clear()
                            continue

                        extinf_line = set_group_title_in_extinf(extinf_line, real_live_grp)

                        best_tvg_id = resolve_best_tvg_id(item_name, orig_tvg_id, epg_index)
                        if best_tvg_id:
                            matched_epg += 1

                        xtream_tvgid_map[norm_lookup_key(item_name)] = best_tvg_id

                        pending_lines[extinf_idx] = replace_only_tvg_id_in_extinf(extinf_line, best_tvg_id)
                        for pl in pending_lines:
                            full_out.write(pl + "\n")
                            live_only_out.write(pl + "\n")
                        full_out.write(line + "\n")
                        live_only_out.write(line + "\n")
                        live_count += 1

                    pending_lines.clear()

        xtream_map_out_path.write_text(json.dumps(xtream_tvgid_map, ensure_ascii=False), encoding="utf-8")
        return live_count, matched_epg, movie_count, series_count

    finally:
        if tmp_raw_path.exists():
            tmp_raw_path.unlink()


def push_files_to_secret_gist_via_git(generated_files: list[Path], token: str, gist_id: str) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="gist_push_"))
    try:
        gist_repo_url = f"https://x-access-token:{token}@gist.github.com/{gist_id}.git"
        subprocess.run(["git", "init", "-b", "main"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "http.postBuffer", "524288000"], cwd=work_dir, check=True)

        for fpath in generated_files:
            target_file = work_dir / fpath.name
            shutil.copy2(fpath, target_file)

            if fpath.name.startswith("lista_"):
                target_gz = work_dir / f"{fpath.name}.gz"
                with fpath.open("rb") as f_in, gzip.open(target_gz, "wb", compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

                if target_file.stat().st_size > 98 * 1024 * 1024:
                    target_file.unlink()

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Listas M3U + Mapa Xtream (Alta Precisao EPG)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
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
        map_file = out_dir / f"xtream_map_{i}.json"
        try:
            live_cnt, matched_cnt, movie_cnt, series_cnt = build_calibrated_playlists(
                url, epg_index, epg_public_url, full_file, live_file, map_file
            )
            if live_cnt == 0 and movie_cnt == 0 and series_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            full_mb = full_file.stat().st_size / (1024 * 1024)
            live_mb = live_file.stat().st_size / (1024 * 1024)
            print(
                f"  -> ✓ [lista_{i}.m3u] {live_cnt} Canais ({matched_cnt} com EPG exato) + {movie_cnt} Filmes + {series_cnt} Episódios de Séries ({full_mb:.2f} MB)\n"
                f"  -> ✓ [canais_{i}.m3u] Só Canais: {live_cnt} Canais ({live_mb:.2f} MB)\n"
                f"  -> ✓ [xtream_map_{i}.json] Mapa de TVG-IDs para o Servidor Xtream atualizado!"
            )
            generated_files.extend([full_file, live_file, map_file])
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
