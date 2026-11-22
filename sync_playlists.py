#!/usr/bin/env python3
"""Sincronizador Ultra-Rápido de Canais Ao Vivo + Mapa JSON para Servidor Xtream Proxy:
- NÃO baixa mais o arquivo .m3u bruto de 70MB+ nem catálogos pesados de Filmes/Séries (que vêm direto pelo Worker).
- Consulta apenas a API leve de Canais ao Vivo (get_live_categories + get_live_streams) em ~2 segundos!
- Gera 'xtream_map_X.json' com '__upstream_host__' e os TVG-IDs de alta precisão para o Cloudflare Worker.
- Gera 'canais_X.m3u' e 'lista_X.m3u' (ambas enxutas, apenas com os canais ao vivo calibrados, < 1 MB).
- Blindado para Smart TVs (Samsung Tizen / LG webOS): bloqueia '&', '!' ou '+' em tvg-id (A&E -> ae.br, E! -> eentertainment.br, BM&C -> bmcnews.br).
- Alinhado 100% com o merge_epg.py: respeita travas de país (PT/US/ARG/CO/MX), UFs estaduais e numeração de canais.
"""
from __future__ import annotations

import gzip
from collections import Counter
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
    extract_foreign_country_code,
    extract_significant_digits,
    has_conflicting_channel_number,
    has_conflicting_region,
    is_smart_tv_safe_id,
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

# Canais informativos/24h/eventos sem guia fixo que NUNCA devem receber tvg-id para não puxarem guia errado
_RE_NO_EPG_CHANNEL = re.compile(
    r"(^24H|\b24\s*H(?:S|ORAS)?\b|\b24/7\b|JOGOS\s+DO\s+DIA|ATUALIZA[CÇ][AÃ]O\s+DE\s+CONTE[UÚ]DO|"
    r"GUIA\s+DE\s+CANAIS|^PROGRAMA\s*-\s*|^ESPECIAL\s*-\s*|^CINE\s+(?!CANAL|BRASIL)|^LIVE\s+)",
    re.IGNORECASE,
)

# Sinônimos diretos alinhados com os IDs reais gerados no epg.xml.gz
CHANNEL_SYNONYMS: dict[str, list[str]] = {
    "E": ["eentertainment.br", "e.br", "E Entertainment", "E! Entertainment", "E!"],
    "E!": ["eentertainment.br", "e.br", "E Entertainment", "E! Entertainment", "E!"],
    "A E": ["ae.br", "AE", "A E", "A&E"],
    "AE": ["ae.br", "AE", "A E", "A&E"],
    "BM C NEWS": ["bmcnews.br", "BMC NEWS", "BM C NEWS", "BM&C NEWS"],
    "BMC NEWS": ["bmcnews.br", "BMC NEWS", "BM C NEWS", "BM&C NEWS"],
    "BM C": ["bmcnews.br", "BMC NEWS", "BM&C NEWS"],
    "FILM ARTS": ["FilmAndArts.br", "FilmAndArts.mx", "FILM AND ARTS", "FILM&ARTS"],
    "FILM AND ARTS": ["FilmAndArts.br", "FilmAndArts.mx", "FILM AND ARTS", "FILM&ARTS"],
    "FAST FUNBOX": ["FastAndFunBox.br", "FAST AND FUNBOX", "FAST & FUNBOX"],
    "TVE": ["tve.br", "tvescola.br", "TVE", "TVE RS"],
    "TVE RS": ["tve.br", "tvescola.br", "TVE RS", "TVE"],
    "TVE BAHIA": ["tvebahia.br", "TVE BAHIA", "TVE Bahia"],
    "CAZE TV": ["caze.tv", "CAZE TV", "CAZETV"],
    "CAZETV": ["caze.tv", "CAZE TV", "CAZETV"],
    "CAZE TV 1": ["caze.tv", "CAZE TV", "CAZETV"],
    "CAZE TV 2": ["caze.tv", "CAZE TV", "CAZETV"],
    "CAZE TV 3": ["caze.tv", "CAZE TV", "CAZETV"],
    "DISNEY 1": ["disney.plus.1", "disney.plus.1.br", "DISNEY PLUS 1", "DISNEY+ 1"],
    "DISNEY PLUS 1": ["disney.plus.1", "disney.plus.1.br", "DISNEY PLUS 1", "DISNEY+ 1"],
    "HBO 2": ["HBO2.br", "hbo2.br", "HBO 2", "HBO2"],
    "HBO": ["hbo.br", "HBO.br", "HBO"],
    "HBO PLUS": ["HBOPlusBrasil.br", "hbo.plus.br", "HBOPlus.br", "HBO Plus"],
    "HBO FAMILY": ["HBOFamily.br", "hbo.family.br", "HBO Family"],
    "HBO SIGNATURE": ["HBOSignature.br", "hbo.signature.br", "HBO Signature"],
    "HBO MUNDI": ["HBOMundi.br", "hbo.mundi.br", "HBO Mundi", "MAX"],
    "MAX": ["HBOMundi.br", "HBO Mundi"],
    "HBO POP": ["HBOPop.br", "hbo.pop.br", "HBO Pop"],
    "HBO XTREME": ["HBOXtreme.br", "hbo.xtreme.br", "HBO Xtreme"],
    "DISCOVERY H H": ["DiscoveryHomeHealth.br", "discovery.home.and.health.br", "Discovery Home and Health", "Discovery Home & Health"],
    "DISCOVERY HOME HEALTH": ["DiscoveryHomeHealth.br", "discovery.home.and.health.br", "Discovery Home and Health", "Discovery Home & Health"],
    "HOME HEALTH": ["DiscoveryHomeHealth.br", "discovery.home.and.health.br", "Discovery Home and Health", "Discovery Home & Health"],
    "UNIVERSAL STUDIO": ["Studiouniversal.br", "studiouniversal.br", "Studio Universal", "Universal Studio"],
    "STUDIO UNIVERSAL": ["Studiouniversal.br", "studiouniversal.br", "Studio Universal"],
    "UNIVERSAL CHANNEL": ["universal.br", "Universal.br", "Universal TV", "Universal Channel"],
    "UNIVERSAL TV": ["universal.br", "Universal.br", "Universal TV"],
    "SONY CHANNEL": ["sony.br", "Sony.br", "Sony Channel", "Sony"],
    "SONY": ["sony.br", "Sony.br", "Sony Channel", "Sony"],
    "WARNER": ["warnerchannel.br", "Warner.br", "Warner Channel", "Warner"],
    "WARNER CHANNEL": ["warnerchannel.br", "Warner.br", "Warner Channel", "Warner"],
    "WARNER TV": ["warnerchannel.br", "Warner.br", "Warner TV", "Warner"],
    "TNT": ["tnt.br", "TNT.br", "TNT"],
    "TNT SERIES": ["tntseries.br", "TNTSeries.br", "TNT Séries", "TNT Series"],
    "TNT NOVELAS": ["tnt.novelas", "TNTNovelas.br", "auto.tntnovelas", "TNT Novelas"],
    "ADULT SWIM": ["adult.swim", "Adult.Swim.HD.br", "auto.adultswim", "Adult Swim"],
    "ADULT SWIN": ["adult.swim", "Adult Swim"],
    "4DULT SWIN": ["adult.swim", "Adult Swim"],
    "GLOBOPLAY NOVELAS": ["viva.br", "Viva.br", "Globoplay Novelas", "VIVA"],
    "VIVA": ["viva.br", "Viva.br", "VIVA", "Globoplay Novelas"],
    "LOVE NATURE": ["LoveNature.br", "lovenature.br", "Love Nature", "Love Nature 4K"],
    "LOVENATURE": ["LoveNature.br", "lovenature.br", "Love Nature", "Love Nature 4K"],
    "H2": ["History2.br", "history.2.br", "History 2", "H2"],
    "HISTORY 2": ["History2.br", "history.2.br", "History 2", "H2"],
    "HISTORY": ["HistoryChannel.br", "history.br", "History Channel", "History"],
    "HISTORY CHANNEL": ["HistoryChannel.br", "history.br", "History Channel", "History"],
    "CANAL OFF": ["off.br", "Off.br", "OFF", "Canal OFF"],
    "OFF": ["off.br", "Off.br", "OFF", "Canal OFF"],
    "FUTURA": ["futura.br", "FUTURA", "CANAL FUTURA"],
    "CANAL FUTURA": ["futura.br", "FUTURA", "CANAL FUTURA"],
    "SPORTV": ["sportv.1", "SporTV.br", "sportv.br", "SporTV 1", "SporTV"],
    "SPORTV 1": ["sportv.1", "SporTV.br", "sportv.br", "SporTV 1", "SporTV"],
    "SPORTV 2": ["Sportv2.br", "sportv2.br", "SPORTV 2", "SporTV 2"],
    "SPORTV 3": ["SporTV3.br", "sportv3.br", "SPORTV 3", "SporTV 3"],
    "PREMIERE CLUBES": ["PremiereClubes.br", "premiere.clubes.br", "PREMIERE CLUBES", "Premiere Clubes", "Premiere 1"],
    "PREMIERE 1": ["PremiereClubes.br", "premiere.clubes.br", "PREMIERE CLUBES", "Premiere Clubes"],
    "PREMIERE": ["PremiereClubes.br", "premiere.clubes.br", "PREMIERE CLUBES"],
    "JOVEM PAN": ["JovemPanNews.br", "jovem.pan.news.br", "Jovem Pan News", "JOVEM PAN"],
    "JOVEM PAN NEWS": ["JovemPanNews.br", "jovem.pan.news.br", "Jovem Pan News"],
    "JOVEN PAN NEWS": ["JovemPanNews.br", "jovem.pan.news.br", "Jovem Pan News"],
    "CNN BRASIL": ["cnn.brasil.br", "cnnbrasil.br", "CNN Brasil"],
    "CNN ESPANHOL": ["cnn.espanhol", "CNN ESPANHOL", "CNN EN ESPANOL"],
    "CNN EN ESPANOL": ["cnn.espanhol", "CNN ESPANHOL", "CNN EN ESPANOL"],
    "CNBC BRASIL": ["timesbrasil.br", "CNBC BRASIL", "TIMES BRASIL", "TIMES BRASIL CNBC"],
    "TIMES BRASIL": ["timesbrasil.br", "CNBC BRASIL", "TIMES BRASIL", "TIMES BRASIL CNBC"],
    "TIMES BRASIL CNBC": ["timesbrasil.br", "CNBC BRASIL", "TIMES BRASIL", "TIMES BRASIL CNBC"],
    "REDE TV": ["redetv.br", "REDETV", "REDE TV", "REDETV!"],
    "REDETV": ["redetv.br", "REDETV", "REDE TV", "REDETV!"],
    "REDE CNT": ["rede.cnt", "cnt.br", "REDE CNT", "CNT"],
    "CNT": ["rede.cnt", "cnt.br", "REDE CNT", "CNT"],
    "TV ASSEMBLEIA": ["tv.assembleia", "TV ASSEMBLEIA", "ASSEMBLEIA LEGISLATIVA"],
    "ASSEMBLEIA LEGISLATIVA": ["tv.assembleia", "TV ASSEMBLEIA", "ASSEMBLEIA LEGISLATIVA"],
    "AGROMAIS": ["agromais.br", "AGROMAIS", "AGRO MAIS", "AGRO+"],
    "AGRO": ["agromais.br", "AGROMAIS", "AGRO MAIS", "AGRO+"],
    "AGRO PLUS": ["agromais.br", "AGROMAIS", "AGRO MAIS", "AGRO+"],
    "USA NETWORK": ["usanetwork.br", "USA Network", "GB USA", "SYFY"],
    "GB USA": ["usanetwork.br", "USA Network", "GB USA"],
    "USA": ["usanetwork.br", "USA Network"],
    "BAND NEWS": ["BandNews.br", "BAND NEWS", "Band News", "bandnews.br"],
    "CARTOON NETWORK": ["CartoonNetwork.br", "CARTOON", "Cartoon Network"],
    "CARTOON": ["CartoonNetwork.br", "CARTOON", "Cartoon Network"],
    "FOOD NETWORK": ["FoodNetwork.br", "Food Network", "Foodnetworkhd.br"],
    "GLOBO SP": ["gb.sp", "globo.sao.paulo", "GB SP", "Globo SP", "Globo São Paulo"],
    "GLOBO SAO PAULO": ["globo.sao.paulo", "gb.sp", "Globo São Paulo", "GB SP"],
    "GLOBO RJ": ["gb.rj", "globo.rio", "GB RJ", "Globo RJ", "Globo Rio"],
    "GLOBO RIO": ["globo.rio", "gb.rj", "Globo Rio", "GB RJ"],
    "GLOBO MINAS": ["gb.minas", "GB MINAS", "Globo Minas"],
    "GLOBO BELO HORIZONTE": ["GLOBOBELOHORIZONTE", "gb.minas", "GLOBO BELO HORIZONTE", "GB MINAS"],
    "GLOBO NORDESTE": ["gb.nordeste", "globo.recife", "GB NORDESTE", "GLOBO TV NORDESTE", "Globo Nordeste"],
    "GLOBO PERNAMBUCO": ["globo.pernambuco", "gb.nordeste", "GLOBO PERNAMBUCO", "GB NORDESTE"],
    "GLOBO TV NORDESTE": ["globo.tv.nordeste", "gb.nordeste", "GLOBO TV NORDESTE", "GB NORDESTE"],
    "GLOBO BRASILIA": ["globo.brasilia", "GLOBO BRASÍLIA", "Globo Brasilia", "GB BRASILIA"],
    "GLOBO REDE BAHIA": ["globo.rede.bahia", "gb.tv.bahia", "globo.bahia", "Globo Rede Bahia", "GB TV BAHIA"],
    "GLOBO TV BAHIA": ["gb.tv.bahia", "globo.bahia", "GB TV BAHIA", "GLOBO TV BAHIA"],
    "GLOBO RBS PORTO ALEGRE": ["gb.rbs.porto.alegre", "globo.rbs.tv.porto.alegre", "GB RBS PORTO ALEGRE", "Globo RBS TV Porto Alegre"],
    "GLOBO RBS TV PORTO ALEGRE": ["globo.rbs.tv.porto.alegre", "gb.rbs.porto.alegre", "Globo RBS TV Porto Alegre", "GB RBS PORTO ALEGRE"],
    "RBS PASSO FUNDO GLOBO": ["rbs.passo.fundo.globo", "rbs.tv.passo.fundo", "globo.rbs.passo.fundo", "RBS PASSO FUNDO GLOBO", "RBS TV PASSO FUNDO"],
    "RBS CAXIAS": ["rbs.caxias", "rbs.caxias.do.sul", "rbs.tv.caxias.do.sul", "RBS CAXIAS", "RBS TV CAXIAS DO SUL"],
    "GLOBO RPC CURITIBA": ["gb.rpc.curitiba", "globo.rpc.tv.curitiba", "GB RPC CURITIBA", "Globo RPC TV Curitiba"],
    "GLOBO RPC TV CURITIBA": ["globo.rpc.tv.curitiba", "gb.rpc.curitiba", "Globo RPC TV Curitiba", "GB RPC CURITIBA"],
    "GLOBO TV DIARIO": ["tv.diario.mogi.das.cruzes.br", "globo.diario.mogi.das.cruzes", "GLOBOTVDIARIO"],
    "GLOBO TV DIARIO MOGI DAS CRUZES": ["tv.diario.mogi.das.cruzes.br", "globo.diario.mogi.das.cruzes", "TV DIARIO MOGI DAS CRUZES"],
    "GLOBO TV INTEG JUIZ DE FORA": ["tv.integracao.juiz.de.fora.br", "GLOBOTVINTEGJUIZDEFORA", "globo.integ.juiz.de.fora", "TV INTEGRAÇÃO JUIZ DE FORA"],
    "GLOBO TV INTEGRACAO JUIZ DE FORA": ["tv.integracao.juiz.de.fora.br", "globo.integ.juiz.de.fora", "GLOBOTVINTEGJUIZDEFORA", "TV INTEGRAÇÃO JUIZ DE FORA"],
    "TV INTEGRACAO JUIZ DE FORA": ["tv.integracao.juiz.de.fora.br", "globo.integ.juiz.de.fora", "TV INTEGRAÇÃO JUIZ DE FORA"],
    "GLOBO TV VANG S J CAMPOS": ["GLOBOTVVANGSJCAMPOS", "globo.vang.s.j.campos", "tv.vanguarda.s.j.campos", "Globo TV Vanguarda S J dos Campos"],
    "GLOBO TV VANGUARDA S J DOS CAMPOS": ["globo.tv.vanguarda.s.j.dos.campos", "tv.vanguarda.s.j.campos", "Globo TV Vanguarda S J dos Campos"],
    "GLOBO VERDES MARES": ["globo.verdes.mares", "gb.tv.verdes.mares", "GLOBO VERDES MARES", "GB TV VERDES MARES"],
    "GLOBO TV VERDES MARES": ["gb.tv.verdes.mares", "globo.verdes.mares", "GB TV VERDES MARES", "GLOBO VERDES MARES"],
    "RECORD SP": ["recordtv.sp", "RECORDTV SP", "Record SP", "RecordTV SP"],
    "RECORD TV SP": ["recordtv.sp", "RECORDTV SP", "Record SP", "RecordTV SP"],
    "RECORD RJ": ["recordtv.rj", "record.rio", "RECORDTV RJ", "Record RJ"],
    "RECORD TV RJ": ["recordtv.rj", "record.rio", "RECORDTV RJ", "Record RJ"],
    "RECORD BRASILIA": ["record.brasilia", "RECORD BRASILIA", "RecordTV Brasilia"],
    "RECORD TV BRASILIA": ["record.brasilia", "RECORD BRASILIA", "RecordTV Brasilia"],
    "RECORD GOIAS": ["recordtv.goias", "RECORDTV GOIAS", "Record Goias"],
    "RECORD TV GOIAS": ["recordtv.goias", "RECORDTV GOIAS", "Record Goias"],
    "RECORD MANAUS": ["recordtv.manaus", "RECORDTV MANAUS", "Record Manaus"],
    "RECORD TV NEWS": ["Recordnews.br", "RECORD NEWS", "Record News", "recordnews.br"],
    "BAND TV SP": ["band.sp", "BAND SP", "Band SP"],
    "BAND TV RJ": ["band.rj", "BAND RJ", "Band RJ"],
    "BAND TV RS": ["band.rs", "BAND RS", "Band RS"],
    "BAND TV PR": ["band.parana", "band.pr", "BAND PARANA", "Band Parana"],
    "BAND TV PARANA": ["band.parana", "band.pr", "BAND PARANA", "Band Parana"],
    "BAND TV MINAS": ["band.tv.minas", "band.mg", "BAND TV MINAS", "Band Minas"],
    "BAND TV GOIANIA": ["band.tv.goiania", "BAND TV GOIANIA", "Band Goiania"],
    "BAND TV CAMPINAS": ["band.campinas", "BAND CAMPINAS", "Band Campinas"],
    "BAND TV NEWS": ["BandNews.br", "BAND NEWS", "Band News", "bandnews.br"],
    "BAND TV SPORTS": ["BandSports.br", "BAND SPORTS", "BandSports", "bandsports.br"],
    "BANDSPORTS": ["BandSports.br", "BAND SPORTS", "BandSports", "bandsports.br"],
    "SBT TV SP": ["sbt.sp", "SBT SP", "SBT"],
    "SBT TV RJ": ["sbt.rj", "SBT RJ", "SBT Rio"],
    "SBT TV RS": ["sbt.rs", "SBT RS", "SBT TV RBS"],
    "SBT TV RBS": ["sbt.rs", "SBT RS", "SBT TV RBS"],
    "SBT TV BRASILIA": ["sbt.brasilia", "SBT BRASILIA"],
    "SBT TV HD PIAUI": ["SBT TV PIAUI"],
    "SBT TV HD RN": ["SBT TV RN"],
}


def norm_lookup_key(title: str) -> str:
    s = html.unescape(title or "").replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    s = re.sub(r"\b0+(\d+)\b", r"\1", s)
    return " ".join(s.split())


def clean_channel_title_for_epg(raw_name: str) -> str:
    """Limpa todas as tags de resolução, codec, servidor (BR2), [P], (Eventos), ² do nome do canal, preservando numeração real."""
    s = html.unescape(raw_name or "").replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"\[\s*\]", " ", s)
    s = re.sub(r"\.(?:br|us|uk|pt|ar|mx|cl|co|pe|ca|latam|int|bt)$", " ", s.strip(), flags=re.I)
    s = re.sub(r"(?<=[A-Za-z0-9])(4K|8K|UHD|FHDR|FHD|(?<![Ff])HD|SD)\b", r" \1", s, flags=re.I)
    s = re.sub(r"(?<=[A-Za-z0-9])(?:BR\d+|SRV\d+|OP\d+)\b", " ", s, flags=re.I)
    s = re.sub(
        r"\(\s*(?:BR\s*\d*|SRV\s*\d*|OP[CÇ][AÃ]O\s*\d*|OP\d*|EVENTOS?|PPV|BACKUP|BKP|ALT|ALTERNATIVO|FHDR|FHD|HD|SD|4K|8K|UHD|H\.?26[45]|HEVC|LEG|DUB|DUAL)\s*\)",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"\[\s*(?:H\.?26[45]|HEVC|P|BR\s*\d*|SRV\s*\d*|OP\d*|FHDR|FHD|HD|SD|4K|8K|UHD|ALT|BACKUP|BKP|LEG|DUB|DUAL)\s*\]",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"\[\s*(\d+)\s*\]", r" \1 ", s)
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰ᴴᴰ~*]+", " ", s)
    s = re.sub(
        r"\b(?:FHDR|FHD|UHD|4K|8K|HD|SD|H\.?265|H\.?264|HEVC|AVC|1080P|1080I|720P|60FPS|LEG|LEGENDADO|DUB|DUBLADO|DUAL|ALT|ALTERNATIVO|BACKUP|BKP|BR\d+|SRV\d+|OP\d+|1X)\b",
        " ",
        s,
        flags=re.I,
    )
    s = re.sub(r"\s+F$", "", s.strip(), flags=re.I)
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
    if any(w in n for w in ("NEWS", "CNN", "JOVEM PAN", "CNBC", "TIMES BRASIL", "BLOOMBERG", "SENADO", "CAMARA", "JUSTICA", "ASSEMBLEIA")):
        return "Canais ⫸ Noticias"
    if any(w in n for w in ("DISCOVERY", "HISTORY", "H2", "ANIMAL PLANET", "NAT GEO", "NATIONAL GEOGRAPHIC", "LOVE NATURE", "SCIENCE", "THEATER", "TURBO", "WORLD", "INVESTIGACAO")):
        return "Canais ⫸ Documentarios"
    return "Canais ⫸ Variedades"


def find_epg_gz_path() -> Path:
    for p in EPG_GZ_CANDIDATES:
        if p.exists() and p.stat().st_size > 100:
            return p
    return EPG_GZ_CANDIDATES[-1]


def load_epg_id_index(epg_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """
    Carrega o índice EPG em 2 fases e também retorna um mapa {channel_id: canonical_display_name}
    para validar estado (UF) e numeração sem falsos bloqueios.
    """
    primary_index: dict[str, str] = {}
    secondary_index: dict[str, str] = {}
    id_to_canonical_name: dict[str, str] = {}

    if not epg_path.exists():
        print(f"Aviso: {epg_path} não encontrado!")
        return primary_index, id_to_canonical_name

    with gzip.open(epg_path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "channel":
                cid = (elem.attrib.get("id") or "").strip()
                if cid:
                    display_names = [
                        child.text.strip()
                        for child in elem
                        if child.tag.rsplit("}", 1)[-1] == "display-name" and child.text and child.text.strip()
                    ]
                    canon_label = next((d for d in display_names if d != cid), cid)
                    id_to_canonical_name[cid] = canon_label

                    cid_no_br = re.sub(r"\.(?:br|bt)$", "", cid, flags=re.I)
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

                    for dn in display_names:
                        if dn.upper().endswith(" F") or len(dn) < 2:
                            continue
                        dn_clean = clean_channel_title_for_epg(dn)
                        if has_conflicting_channel_number(canon_label, dn_clean):
                            continue
                        dn_no_br = re.sub(r"\.(?:br|bt)$", "", dn, flags=re.I)
                        for k_dn in (
                            norm_lookup_key(dn),
                            norm_lookup_key(dn_no_br),
                            dn_clean,
                            name_key(dn),
                            name_key(dn_clean),
                            name_key(normalizar_canal(dn)),
                            name_key(strip_qualities_keep_case(dn)),
                        ):
                            if k_dn and k_dn not in secondary_index:
                                secondary_index[k_dn] = cid
                elem.clear()

    merged = {**secondary_index, **primary_index}
    print(f"✓ Índice EPG de Alta Precisão carregado de {epg_path} ({len(primary_index)} IDs primários, {len(merged)} chaves totais).")
    return merged, id_to_canonical_name


def is_orig_tvg_id_poisoned(channel_name: str, clean_ch_name: str, orig_id: str) -> bool:
    """Detecta tvg-ids falsos, caracteres incompatíveis com Smart TV (&), conflito de UF/país ou colisão de número."""
    oid = orig_id.lower().strip()
    if not oid or not is_smart_tv_safe_id(orig_id):
        return True
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", oid):
        return True
    if has_conflicting_region(channel_name, orig_id):
        return True
    oid_no_br = re.sub(r"\.(?:br|bt)$", "", oid, flags=re.I)
    oid_clean = clean_channel_title_for_epg(oid_no_br)
    if has_conflicting_channel_number(channel_name, oid_clean):
        return True
    if oid == "agromais.br" and "AGRO" not in clean_ch_name:
        return True
    if oid == "globonews.br" and "GLOBO NEWS" not in clean_ch_name and "GLOBONEWS" not in clean_ch_name and "GB NEWS" not in clean_ch_name:
        return True
    if oid in {"globosp.br", "gb.sp"} and clean_ch_name not in {"GLOBO SP", "GB SP", "GLOBO SAO PAULO", "GLOBO", "TV GLOBO"}:
        return True
    if "recordtvbras" in oid and "BRASILIA" not in clean_ch_name and clean_ch_name != "RECORD":
        return True
    if oid == "sbt.br" and clean_ch_name not in {"SBT", "SBT SP", "SBT TV SP"}:
        return True
    if oid == "band.br" and clean_ch_name not in {"BAND", "BAND SP", "BAND TV SP"}:
        return True
    if oid == "tv.diario.br" and ("GLOBO" in clean_ch_name or "MOGI" in clean_ch_name):
        return True
    return False


def resolve_best_tvg_id(
    channel_name: str,
    original_tvg_id: str,
    epg_index: dict[str, str],
    id_to_canonical_name: dict[str, str],
) -> str:
    if _RE_NO_EPG_CHANNEL.search(channel_name):
        return ""

    # Se o canal for estrangeiro (ex: PT ..., US ..., ARG ..., CO ..., MX ...), não vincula em EPG brasileiro
    if extract_foreign_country_code(channel_name):
        return ""

    clean_title = clean_channel_title_for_epg(channel_name)
    stripped_title = clean_channel_title_for_epg(strip_qualities_keep_case(channel_name))
    norm_canon_title = clean_channel_title_for_epg(normalizar_canal(channel_name))

    def _valid_candidate_id(cand_id: str) -> bool:
        if not cand_id or not is_smart_tv_safe_id(cand_id):
            return False
        canon_target_name = id_to_canonical_name.get(cand_id, cand_id)
        if has_conflicting_region(channel_name, canon_target_name):
            return False
        return not has_conflicting_channel_number(channel_name, canon_target_name)

    for lookup_title in dict.fromkeys([clean_title, stripped_title, norm_canon_title]):
        if lookup_title in CHANNEL_SYNONYMS:
            for syn in CHANNEL_SYNONYMS[lookup_title]:
                for k_syn in (norm_lookup_key(syn), name_key(syn), clean_channel_title_for_epg(syn)):
                    if k_syn and k_syn in epg_index:
                        found_id = epg_index[k_syn]
                        if _valid_candidate_id(found_id):
                            return found_id

    no_tv_mid = re.sub(r"^(GLOBO|SBT|RECORD|BAND)\s+TV\s+", r"\1 ", stripped_title)

    for candidate in dict.fromkeys([
        clean_title,
        stripped_title,
        no_tv_mid,
        strip_qualities_keep_case(channel_name),
        normalizar_canal(channel_name),
        normalizar_canal(stripped_title),
        normalizar_canal(no_tv_mid),
    ]):
        for k in (norm_lookup_key(candidate), name_key(candidate)):
            if k and k in epg_index:
                found_id = epg_index[k]
                if _valid_candidate_id(found_id):
                    return found_id

    if original_tvg_id and not is_orig_tvg_id_poisoned(channel_name, stripped_title, original_tvg_id):
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


SMOKE_TEST = os.environ.get("SMOKE_TEST", "1").strip().lower() in {"1", "true", "yes"}


def smoke_test_stream(host: str, q_user: str, q_pwd: str, sid: str) -> str:
    """Lê 1 KB do stream. O formato gerado (/live/user/pass/<id numérico>.ts) difere do M3U original do provedor
    (UUID + .m3u). Falha aqui PODE ser só bloqueio do IP do GitHub; se tocar no player, ignore."""
    url = f"{host}/live/{q_user}/{q_pwd}/{sid}.ts"
    try:
        with requests.get(url, stream=True, timeout=(8, 12), allow_redirects=True,
                          headers={"User-Agent": IPTV_USER_AGENTS[0]}) as r:
            first = next(r.iter_content(chunk_size=1024), b"")
            return f"stream {sid}: HTTP {r.status_code} | {r.headers.get('Content-Type', '?')} | {len(first)} bytes"
    except Exception as exc:  # noqa: BLE001
        return f"stream {sid}: falhou ({type(exc).__name__})"


def build_live_playlists_and_xtream_map(
    url: str,
    epg_index: dict[str, str],
    id_to_canonical_name: dict[str, str],
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
    q_user, q_pwd = urllib.parse.quote(user, safe=""), urllib.parse.quote(pwd, safe="")
    source_host = f"{parsed.scheme}://{parsed.netloc}"

    xtream_tvgid_map: dict[str, str] = {
        "__upstream_host__": source_host,
    }

    print(f"  -> Consultando API de Canais Ao Vivo em {source_host}...")
    c_resp = request_with_bypass(
        f"{source_host}/player_api.php?username={q_user}&password={q_pwd}&action=get_live_categories", timeout=30
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
        f"{source_host}/player_api.php?username={q_user}&password={q_pwd}&action=get_live_streams", timeout=45
    ).json()

    if not isinstance(s_live, list):
        raise RuntimeError("Resposta inválida de get_live_streams.")

    print(f"  -> ✓ {len(live_cats)} categorias e {len(s_live)} canais recebidos via API em segundos!")

    header = f'#EXTM3U url-tvg="{epg_public_url}" x-tvg-url="{epg_public_url}"\n' if epg_public_url else "#EXTM3U\n"
    live_count = 0
    matched_epg = 0
    motivos: Counter = Counter()
    sem_correspondencia: list[str] = []
    amostra_com_epg: list[str] = []

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

            best_tvg_id = resolve_best_tvg_id(ch_name, orig_id, epg_index, id_to_canonical_name)
            if best_tvg_id:
                matched_epg += 1
                motivos["com tvg-id (EPG)"] += 1
                if len(amostra_com_epg) < 3 and sid:
                    amostra_com_epg.append(sid)
            elif _RE_NO_EPG_CHANNEL.search(ch_name):
                motivos["sem guia por natureza (24H/loop/Cine/Live)"] += 1
            elif extract_foreign_country_code(ch_name):
                motivos["estrangeiro (EPG é brasileiro)"] += 1
            else:
                motivos["sem correspondência no EPG"] += 1
                if len(sem_correspondencia) < 25:
                    sem_correspondencia.append(ch_name)

            xtream_tvgid_map[norm_lookup_key(ch_name)] = best_tvg_id
            if sid:
                xtream_tvgid_map[f"id:{sid}"] = best_tvg_id

            safe_id = best_tvg_id.replace('"', "'")
            safe_name = ch_name.replace('"', "'")
            safe_logo = logo.replace('"', "'")
            safe_grp = grp.replace('"', "'")

            stream_url = f"{source_host}/live/{q_user}/{q_pwd}/{sid}.ts"
            live_out.write(
                f'#EXTINF:-1 tvg-id="{safe_id}" tvg-name="{safe_name}" tvg-logo="{safe_logo}" group-title="{safe_grp}",{ch_name}\n'
                f"{stream_url}\n"
            )
            live_count += 1

    shutil.copy2(live_only_out_path, full_out_path)
    xtream_map_out_path.write_text(json.dumps(xtream_tvgid_map, ensure_ascii=False), encoding="utf-8")

    print("  -> Cobertura de tvg-id por motivo:")
    for motivo, n in motivos.most_common():
        print(f"       {n:5d}  {motivo} ({100 * n / max(1, live_count):.0f}%)")
    if sem_correspondencia:
        print(f"     sem correspondência (candidatos a alias/dicionário): {', '.join(sem_correspondencia[:15])}")
    if SMOKE_TEST and amostra_com_epg:
        print("  -> Teste de fumaça da URL de stream (id numérico + .ts):")
        for sid in amostra_com_epg:
            print("       " + smoke_test_stream(source_host, q_user, q_pwd, sid))
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
    epg_index, id_to_canonical_name = load_epg_id_index(epg_path)

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
                url, epg_index, id_to_canonical_name, epg_public_url, full_file, live_file, map_file
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
