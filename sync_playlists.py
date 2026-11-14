#!/usr/bin/env python3
"""Sincronizador Ultra-Rápido de Canais Ao Vivo + Mapa JSON para Servidor Xtream Proxy:
- NÃO baixa mais o arquivo .m3u bruto de 70MB+ nem catálogos pesados de Filmes/Séries (que vêm direto pelo Worker).
- Consulta apenas a API leve de Canais ao Vivo (get_live_categories + get_live_streams) em ~2 segundos!
- Gera 'xtream_map_X.json' com '__upstream_host__' e os TVG-IDs de alta precisão para o Cloudflare Worker.
- Gera 'canais_X.m3u' e 'lista_X.m3u' (ambas enxutas, apenas com os canais ao vivo calibrados, < 1 MB).
- Blindado contra colisão de numeração (HBO vs HBO 2), suporte total a FHD [H265] e canais de 1 letra (E!).
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

from merge_epg import (
    extract_significant_digits,
    has_conflicting_channel_number,
    name_key,
    normalizar_canal,
    strip_qualities_keep_case,
)

EPG_GZ_CANDIDATES = [Path("output/epg.xml.gz"), Path("epg.xml.gz")]
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

# Canais informativos/24h que NUNCA devem receber tvg-id para não puxarem guia errado
_RE_NO_EPG_CHANNEL = re.compile(
    r"(^24H|\b24\s*H(?:S|ORAS)?\b|JOGOS\s+DO\s+DIA|ATUALIZA[CÇ][AÃ]O\s+DE\s+CONTE[UÚ]DO|"
    r"GUIA\s+DE\s+CANAIS|^PROGRAMA\s*-\s*|^ESPECIAL\s*-\s*)",
    re.IGNORECASE,
)

# Sinônimos diretos: Nome limpo na lista -> Lista de candidatos de alta precisão no EPG
CHANNEL_SYNONYMS: dict[str, list[str]] = {
    "E": ["E!", "E! Entertainment", "eentertainment.br", "E!.br", "e.br", "E"],
    "E!": ["E!", "E! Entertainment", "eentertainment.br", "E!.br", "e.br", "E"],
    "A E": ["A&E", "A&E.br", "ae.br"],
    "TVE": ["TVE Bahia", "TVE BAHIA", "tve.bahia.br", "TV BRASIL", "tvbrasil.br"],
    "HBO 2": ["HBO 2", "HBO2", "HBO2.br", "hbo2.br"],
    "HBO": ["HBO", "HBO.br", "hbo.br"],
    "HBO PLUS": ["HBO Plus", "HBOPlusBrasil.br", "HBOPlus.br", "hboplusbrasil.br"],
    "HBO FAMILY": ["HBOFamily.br", "HBO Family"],
    "HBO SIGNATURE": ["HBOSignature.br", "HBO Signature"],
    "HBO MUNDI": ["HBOMundi.br", "HBO Mundi"],
    "HBO POP": ["HBOPop.br", "HBO Pop"],
    "HBO XTREME": ["HBOXtreme.br", "HBO Xtreme"],
    "DISCOVERY H H": ["Discovery Home & Health", "DiscoveryHomeHealth.br", "discoveryhomehealth.br"],
    "DISCOVERY HOME HEALTH": ["Discovery Home & Health", "DiscoveryHomeHealth.br"],
    "UNIVERSAL STUDIO": ["Studio Universal", "Studiouniversal.br", "studiouniversal.br", "Universal Studio"],
    "STUDIO UNIVERSAL": ["Studio Universal", "Studiouniversal.br", "studiouniversal.br"],
    "UNIVERSAL CHANNEL": ["Universal TV", "Universal Channel", "Universal.br", "universal.br"],
    "SONY CHANNEL": ["Sony Channel", "Sony", "Sony.br", "sony.br"],
    "WARNER": ["Warner Channel", "Warner", "Warner.br", "warnerchannel.br"],
    "WARNER CHANNEL": ["Warner Channel", "Warner", "Warner.br", "warnerchannel.br"],
    "TNT": ["TNT", "TNT.br", "tnt.br"],
    "TNT SERIES": ["TNT Séries", "TNT Series", "TNTSeries.br", "tntseries.br"],
    "TNT NOVELAS": ["TNT Novelas", "TNTNovelas.br", "auto.tntnovelas"],
    "ADULT SWIM": ["Adult Swim", "Adult.Swim.HD.br", "auto.adultswim"],
    "GLOBOPLAY NOVELAS": ["Globoplay Novelas", "VIVA", "Viva.br"],
    "LOVE NATURE": ["Love Nature", "LoveNature.br", "lovenature.br"],
    "H2": ["History 2", "H2", "History2.br", "history2.br"],
    "HISTORY 2": ["History 2", "H2", "History2.br", "history2.br"],
    "HISTORY": ["HistoryChannel.br", "History Channel", "History"],
    "CANAL OFF": ["OFF", "Canal OFF", "Off.br"],
    "OFF": ["OFF", "Canal OFF", "Off.br"],
    "SPORTV": ["SporTV 1", "SporTV", "SporTV.br", "sportv.br"],
    "SPORTV 1": ["SporTV 1", "SporTV", "SporTV.br", "sportv.br"],
    "SPORTV 2": ["SPORTV 2", "SporTV 2", "Sportv2.br", "sportv2.br"],
    "SPORTV 3": ["SPORTV 3", "SporTV 3", "SporTV3.br", "sportv3.br"],
    "PREMIERE CLUBES": ["PREMIERE CLUBES", "Premiere Clubes", "PremiereClubes.br", "premiereclubes.br", "Premiere 1"],
    "PREMIERE 1": ["PREMIERE CLUBES", "Premiere Clubes", "PremiereClubes.br", "premiereclubes.br"],
    "JOVEM PAN": ["jovem.pan.news.br", "Jovem Pan News", "JOVEM PAN", "jovempannews.br"],
    "CNN BRASIL": ["cnn.brasil.br", "CNN Brasil", "cnnbrasil.br"],
    "GLOBO SP": ["GB SP", "Globo SP", "globosp.br"],
    "GLOBO RJ": ["GB RJ", "Globo RJ", "globorj.br"],
    "GLOBO MINAS": ["GB MINAS", "Globo Minas", "globominas.br"],
    "GLOBO BELO HORIZONTE": ["GB MINAS", "Globo Minas", "GLOBO BELO HORIZONTE", "auto.globobelohorizontebr2"],
    "GLOBO NORDESTE": ["GB NORDESTE", "GLOBO TV NORDESTE", "Globo Nordeste"],
    "GLOBO PERNAMBUCO": ["GB NORDESTE", "GLOBO TV NORDESTE", "GLOBO PERNAMBUCO"],
    "GLOBO TV NORDESTE": ["GB NORDESTE", "GLOBO TV NORDESTE"],
    "GLOBO BRASILIA": ["GLOBO BRASÍLIA", "Globo Brasilia", "GB BRASILIA"],
    "GLOBO REDE BAHIA": ["GB TV BAHIA", "Globo Rede Bahia", "GLOBO TV BAHIA"],
    "GLOBO TV BAHIA": ["GB TV BAHIA", "Globo Rede Bahia", "GLOBO TV BAHIA"],
    "GLOBO RBS PORTO ALEGRE": ["Globo RBS TV Porto Alegre", "GB RBS PORTO ALEGRE"],
    "GLOBO RBS TV PORTO ALEGRE": ["Globo RBS TV Porto Alegre", "GB RBS PORTO ALEGRE"],
    "RBS PASSO FUNDO GLOBO": ["RBS TV PASSO FUNDO", "GLOBO RBS TV PASSO FUNDO", "RBS PASSO FUNDO GLOBO"],
    "RBS CAXIAS": ["RBS TV CAXIAS DO SUL", "GLOBO RBS TV CAXIAS DO SUL", "RBS CAXIAS"],
    "GLOBO RPC CURITIBA": ["Globo RPC TV Curitiba", "GB RPC CURITIBA"],
    "GLOBO RPC TV CURITIBA": ["Globo RPC TV Curitiba", "GB RPC CURITIBA"],
    "GLOBO TV DIARIO": ["tv.diario.mogi.das.cruzes.br", "auto.globotvdiariobr2", "GB SP", "Globo SP"],
    "GLOBO TV DIARIO MOGI DAS CRUZES": ["tv.diario.mogi.das.cruzes.br", "auto.globotvdiariobr2", "GB SP", "Globo SP"],
    "GLOBO TV INTEG JUIZ DE FORA": ["Globo TV Integracao Juiz de Fora", "GLOBO TV INTEG JUIZ DE FORA", "GB MINAS"],
    "GLOBO TV INTEGRACAO JUIZ DE FORA": ["Globo TV Integracao Juiz de Fora", "GLOBO TV INTEG JUIZ DE FORA", "GB MINAS"],
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
    "RECORD TV NEWS": ["Recordnews.br", "RECORD NEWS", "Record News", "recordnews.br"],
    "BAND TV SP": ["BAND SP", "Band SP", "band.br"],
    "BAND TV RJ": ["BAND RJ", "Band RJ"],
    "BAND TV RS": ["BAND RS", "Band RS"],
    "BAND TV PR": ["BAND PARANA", "Band Parana"],
    "BAND TV PARANA": ["BAND PARANA", "Band Parana"],
    "BAND TV MINAS": ["BAND TV MINAS", "Band Minas"],
    "BAND TV GOIANIA": ["BAND TV GOIANIA", "Band Goiania"],
    "BAND TV CAMPINAS": ["BAND CAMPINAS", "Band Campinas"],
    "BAND TV NEWS": ["band.news.br", "BAND NEWS", "Band News", "bandnews.br"],
    "BAND TV SPORTS": ["BandSports.br", "BAND SPORTS", "BandSports", "bandsports.br"],
    "BANDSPORTS": ["BandSports.br", "BAND SPORTS", "BandSports", "bandsports.br"],
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
    """Limpa todas as tags de resolução, codec, servidor (BR2), [P], (Eventos), ² do nome do canal, preservando [2] como número."""
    s = html.unescape(raw_name or "")
    s = re.sub(r"\(\s*(?:BR\s*\d+|EVENTOS?|PPV|BACKUP|ALT|FHD|HD|SD|4K|UHD|H\.?26[45]|HEVC)\s*\)", " ", s, flags=re.I)
    s = re.sub(r"\[\s*(?:H\.?26[45]|HEVC|P|BR\s*\d+|FHD|HD|SD|4K|UHD|ALT|BACKUP)\s*\]", " ", s, flags=re.I)
    # Se houver "[2]" (ex: HBO HD [2]), transforma em " 2 " antes de limpar
    s = re.sub(r"\[\s*(\d+)\s*\]", r" \1 ", s)
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", " ", s)
    s = re.sub(r"\b(?:FHD|UHD|4K|8K|HD|SD|H\.?265|H\.?264|HEVC|AVC|1080P|1080I|720P|60FPS)\b", " ", s, flags=re.I)
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


def request_with_bypass(url: str, stream: bool = False, timeout: int = 45) -> requests.Response:
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


def find_epg_gz_path() -> Path:
    for p in EPG_GZ_CANDIDATES:
        if p.exists() and p.stat().st_size > 100:
            return p
    return EPG_GZ_CANDIDATES[-1]


def load_epg_id_index(epg_path: Path) -> dict[str, str]:
    """Carrega o índice EPG em 2 fases, bloqueando aliases com numeração divergente (ex: HBO vs HBO 2)."""
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
                    cid_no_br = re.sub(r"\.br$", "", cid, flags=re.I)
                    cid_clean = clean_channel_title_for_epg(cid_no_br)
                    for k in (
                        norm_lookup_key(cid),
                        norm_lookup_key(cid_no_br),
                        cid_clean,
                        name_key(cid),
                        name_key(cid_no_br),
                        name_key(cid_clean),
                        name_key(normalizar_canal(cid_clean)),
                    ):
                        if k and k not in primary_index:
                            primary_index[k] = cid

                    for child in elem:
                        if child.tag.rsplit("}", 1)[-1] == "display-name" and child.text:
                            dn = child.text.strip()
                            dn_clean = clean_channel_title_for_epg(dn)
                            if has_conflicting_channel_number(cid_clean, dn_clean):
                                continue
                            dn_no_br = re.sub(r"\.br$", "", dn, flags=re.I)
                            for k_dn in (
                                norm_lookup_key(dn),
                                norm_lookup_key(dn_no_br),
                                dn_clean,
                                name_key(dn),
                                name_key(dn_clean),
                                name_key(normalizar_canal(dn_clean)),
                            ):
                                if k_dn and k_dn not in secondary_index:
                                    secondary_index[k_dn] = cid
                elem.clear()

    merged = {**secondary_index, **primary_index}
    print(f"✓ Índice EPG de Alta Precisão carregado de {epg_path} ({len(primary_index)} IDs primários, {len(merged)} chaves totais).")
    return merged


def is_orig_tvg_id_poisoned(clean_ch_name: str, orig_id: str) -> bool:
    """Detecta tvg-ids falsos ou com colisão de número (ex: hbo.br em HBO 2)."""
    oid = orig_id.lower().strip()
    if not oid:
        return True
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", oid):
        return True
    oid_no_br = re.sub(r"\.(?:br|bt)$", "", oid, flags=re.I)
    oid_clean = clean_channel_title_for_epg(oid_no_br)
    if has_conflicting_channel_number(clean_ch_name, oid_clean):
        return True
    if oid == "agromais.br" and "AGRO" not in clean_ch_name:
        return True
    if oid == "globonews.br" and "GLOBO NEWS" not in clean_ch_name and "GLOBONEWS" not in clean_ch_name:
        return True
    if oid == "globosp.br" and clean_ch_name not in {"GLOBO SP", "GLOBO", "TV GLOBO"}:
        return True
    if "recordtvbras" in oid and "BRASILIA" not in clean_ch_name:
        return True
    if oid == "sbt.br" and clean_ch_name not in {"SBT", "SBT SP", "SBT TV SP"}:
        return True
    if oid == "band.br" and clean_ch_name not in {"BAND", "BAND SP", "BAND TV SP"}:
        return True
    if "tvescola" in oid and "ESCOLA" not in clean_ch_name:
        return True
    if "uberlandia" in oid and "UBERLANDIA" not in clean_ch_name:
        return True
    return False


def resolve_best_tvg_id(channel_name: str, original_tvg_id: str, epg_index: dict[str, str]) -> str:
    if _RE_NO_EPG_CHANNEL.search(channel_name):
        return ""

    clean_title = clean_channel_title_for_epg(channel_name)

    def _valid_candidate_id(cand_id: str) -> bool:
        if not cand_id:
            return False
        cid_low = cand_id.lower()
        if "tvescola" in cid_low and "ESCOLA" not in clean_title:
            return False
        if "uberlandia" in cid_low and "UBERLANDIA" not in clean_title:
            return False
        cand_no_br = re.sub(r"\.(?:br|bt)$", "", cand_id, flags=re.I)
        cand_clean = clean_channel_title_for_epg(cand_no_br)
        return not has_conflicting_channel_number(clean_title, cand_clean)

    if clean_title in CHANNEL_SYNONYMS:
        for syn in CHANNEL_SYNONYMS[clean_title]:
            for k_syn in (norm_lookup_key(syn), name_key(syn), clean_channel_title_for_epg(syn)):
                if k_syn and k_syn in epg_index:
                    found_id = epg_index[k_syn]
                    if _valid_candidate_id(found_id):
                        return found_id

    no_tv_mid = re.sub(r"^(GLOBO|SBT|RECORD|BAND)\s+TV\s+", r"\1 ", clean_title)

    for candidate in (
        clean_title,
        no_tv_mid,
        strip_qualities_keep_case(channel_name),
        normalizar_canal(clean_title),
        normalizar_canal(no_tv_mid),
    ):
        for k in (norm_lookup_key(candidate), name_key(candidate)):
            if k and k in epg_index:
                found_id = epg_index[k]
                if _valid_candidate_id(found_id):
                    return found_id

    if original_tvg_id and not is_orig_tvg_id_poisoned(clean_title, original_tvg_id):
        orig_no_br = re.sub(r"\.(?:br|bt)$", "", original_tvg_id.strip(), flags=re.I)
        for k_id in (
            norm_lookup_key(original_tvg_id),
            norm_lookup_key(orig_no_br),
            clean_channel_title_for_epg(orig_no_br),
            name_key(original_tvg_id),
            name_key(orig_no_br),
        ):
            if k_id and k_id in epg_index:
                found_id = epg_index[k_id]
                if _valid_candidate_id(found_id):
                    return found_id

    return ""


def build_live_playlists_and_xtream_map(
    url: str,
    epg_index: dict[str, str],
    epg_public_url: str,
    full_out_path: Path,
    live_only_out_path: Path,
    xtream_map_out_path: Path,
) -> tuple[int, int]:
    """Consulta apenas a API de Canais Ao Vivo (sem baixar VODs nem o .m3u bruto de 70MB)."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "username" not in query or "password" not in query:
        raise ValueError("A URL configurada no Secret precisa conter username=... e password=...")

    user = query["username"][0]
    pwd = query["password"][0]
    source_host = f"{parsed.scheme}://{parsed.netloc}"

    xtream_tvgid_map: dict[str, str] = {
        "__upstream_host__": source_host,
    }

    print(f"  -> Consultando API de Canais Ao Vivo em {source_host}...")
    c_resp = request_with_bypass(
        f"{source_host}/player_api.php?username={user}&password={pwd}&action=get_live_categories", timeout=30
    )
    if c_resp.url:
        real_parsed = urllib.parse.urlparse(c_resp.url)
        if real_parsed.netloc:
            source_host = f"{real_parsed.scheme}://{real_parsed.netloc}"
            xtream_tvgid_map["__upstream_host__"] = source_host

    c_live = c_resp.json()
    live_cats = {
        str(c.get("category_id", "")): html.unescape(str(c.get("category_name", "")).strip())
        for c in (c_live if isinstance(c_live, list) else [])
        if isinstance(c, dict) and c.get("category_name")
    }

    s_live = request_with_bypass(
        f"{source_host}/player_api.php?username={user}&password={pwd}&action=get_live_streams", timeout=45
    ).json()

    if not isinstance(s_live, list):
        raise RuntimeError("Resposta inválida de get_live_streams.")

    print(f"  -> ✓ {len(live_cats)} categorias e {len(s_live)} canais recebidos via API em segundos!")

    header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"
    live_count = 0
    matched_epg = 0

    with live_only_out_path.open("w", encoding="utf-8") as live_out:
        live_out.write(header)

        for ch in s_live:
            if not isinstance(ch, dict):
                continue

            ch_name = html.unescape(str(ch.get("name", "")).strip())
            if not ch_name:
                continue

            cat_id = str(ch.get("category_id", ""))
            grp = live_cats.get(cat_id, "")
            if not grp or "COMPLETO" in grp.upper():
                grp = infer_fallback_channel_group(ch_name, grp)

            if REMOVE_ADULT and (ADULT_FILTER_RE.search(grp) or ADULT_FILTER_RE.search(ch_name)):
                continue

            orig_id = str(ch.get("epg_channel_id") or "").strip()
            sid = str(ch.get("stream_id") or "").strip()
            logo = str(ch.get("stream_icon") or "").strip()

            best_tvg_id = resolve_best_tvg_id(ch_name, orig_id, epg_index)
            if best_tvg_id:
                matched_epg += 1

            xtream_tvgid_map[norm_lookup_key(ch_name)] = best_tvg_id
            if sid:
                xtream_tvgid_map[f"id:{sid}"] = best_tvg_id

            safe_id = best_tvg_id.replace('"', "'")
            safe_name = ch_name.replace('"', "'")
            safe_logo = logo.replace('"', "'")
            safe_grp = grp.replace('"', "'")

            stream_url = f"{source_host}/live/{user}/{pwd}/{sid}.ts"
            live_out.write(
                f'#EXTINF:-1 tvg-id="{safe_id}" tvg-name="{safe_name}" tvg-logo="{safe_logo}" group-title="{safe_grp}",{ch_name}\n'
                f"{stream_url}\n"
            )
            live_count += 1

    shutil.copy2(live_only_out_path, full_out_path)
    xtream_map_out_path.write_text(json.dumps(xtream_tvgid_map, ensure_ascii=False), encoding="utf-8")
    return live_count, matched_epg


def push_files_to_secret_gist_via_git(generated_files: list[Path], token: str, gist_id: str) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="gist_push_"))
    try:
        gist_repo_url = f"https://x-access-token:{token}@gist.github.com/{gist_id}.git"
        subprocess.run(["git", "init", "-b", "main"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=work_dir, check=True)

        for fpath in generated_files:
            shutil.copy2(fpath, work_dir / fpath.name)

        subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Atualiza Canais + Mapa Xtream (Ultra-Rapido sem VOD)"], cwd=work_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "push", "--force", gist_repo_url, "HEAD:main"], cwd=work_dir, check=True)
        print(f"\n✓ Gist Secreto ({gist_id}) atualizado via Git Push com sucesso!")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    epg_public_url = os.environ.get("EPG_PUBLIC_URL", "").strip()
    epg_path = find_epg_gz_path()
    epg_index = load_epg_id_index(epg_path)

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
            live_cnt, matched_cnt = build_live_playlists_and_xtream_map(
                url, epg_index, epg_public_url, full_file, live_file, map_file
            )
            if live_cnt == 0:
                print(f"  -> ⚠ M3U_URL_{i} retornou vazia. Pulando.")
                continue

            live_mb = live_file.stat().st_size / (1024 * 1024)
            map_kb = map_file.stat().st_size / 1024
            print(
                f"  -> ✓ [canais_{i}.m3u / lista_{i}.m3u] {live_cnt} Canais ({matched_cnt} com EPG exato) ({live_mb:.2f} MB)\n"
                f"  -> ✓ [xtream_map_{i}.json] Mapa Xtream + Auto-Host gerado ({map_kb:.1f} KB)!"
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
