#!/usr/bin/env python3
"""Auditor completo de EPG (XML / XML.GZ) com diagnóstico automático (match vs. falta de conteúdo).
- Gera relatório em Markdown (audit.md) e JSON (audit.json) para publicação na branch `epg`.
- Cruza epg.xml.gz com report.json e validation.json para apontar o motivo provável de cada falha.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BRT_TZ = timezone(timedelta(hours=-3))

PLACEHOLDER_TITLES = {
    "", "SEM PROGRAMACAO", "SEM PROGRAMAÇÃO", "PROGRAMACAO NAO DISPONIVEL",
    "PROGRAMAÇÃO NÃO DISPONÍVEL", "NAO DISPONIVEL", "NÃO DISPONÍVEL",
    "A DEFINIR", "TBA", "TO BE ANNOUNCED", "NO INFORMATION", "OFF AIR",
    "NO DATA", "NO EPG", "SEM DADOS", "SEM INFORMACAO", "FORA DO AR",
}

_FOREIGN_RE = re.compile(
    r"^(?:PT|US|USA|UK|CO|ARG|AR|MX|EC|PY|UY|CL|PE|BOL|VEN|DE|ES|FR|IT|CA|JP|CR|LA|DR|PR|LT|DW|RUS|SP\s+USA)\b|"
    r"^(?:FRFRANCE|LATIN)\b|"
    r"\b(?:PY|US|USA|UK|PORTUGAL)\s*$|"
    r"\b(?:ABC|CBS|NBC|FOX|CW|PBS|CTV|TSN|CBC\s+NEWS|GLOBAL NEWS|NEWS 12|TELEMUNDO|UNIVISION|BBC AMERICA|AL JAZEERA|EURONEWS|ARIRANG|WEATHER NETWORK|KRONE\s*HIT|RT\s+DOCUMENTARY)\b",
    re.I,
)

_PPV_OR_EVENT_RE = re.compile(
    r"(?:PPV|EVENTO|EVENTOS|JOGO\s*\d*|CAZ[EÉ]\s*TV|CANAL\s*GOAT|GOAT\d*|DAZN\d*|DISNEY\s*\+|DISNEY\s*PLUS\d*|"
    r"HBO\s*MAX|MAX\s*\d+|PRIME\s*VIDEO|APPLE\s*TV|NETFLIX|NBA\s*LEAGUE|NBA\s*TV|NFL|MLS|"
    r"UFC|BJJ|FLOGRAPPLING|KINGS\s*LEAGUE|CAMPEONATO|BRASILEIR[AÃ]O|LIGA\s*FUTSAL\d*|"
    r"NORDESTE\s*FC|BUNDESLIGA|LALIGA|CONMEBOL|FORMULA\s*1|DESIMPEDIDOS|FURAC[AÃ]O\s*TV\d*|FURACAOTV\d*|"
    r"FLAMENGO\s*TV|OLYMPIA|REDBULL|CINE\s*SKY|PLUTO\s*TV|SPORTYNET\s*PLUS\d*|SPORTYNET\s*[4-9]|24\s*H|24HORAS|CAMERA|CÂMERA|LIVE|XXX)\b",
    re.I,
)

_REGIONAL_OPEN_RE = re.compile(
    r"\b(?:GLOBO|GB\s|SBT|RECORD|BAND|RBS|RPC|NSC|EPTV|INTER\s*TV|ANHANGUERA|LIBERAL|"
    r"MIRANTE|VERDES\s*MARES|MORENA|CENTRO\s*AMERICA|AMAZ[OÔ]NICA|GAZETA|ARATU|JANGADEIRO|"
    r"ALTEROSA|SERRA\s*DOURADA|ITAPOAN|PAJU[CÇ]ARA|CIDADE\s*VERDE|ASSEMBLEIA|TVE)\b",
    re.I,
)

_INTERMITTENT_RE = re.compile(
    r"\b(?:SPORTV\s*4|PREMIERE|CONMEBOL|PARAMOUNT\+|SPORTYNET|FAST)\b",
    re.I,
)


def norm_key(s: str) -> str:
    t = unicodedata.normalize("NFKD", (s or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    return re.sub(r"[^A-Z0-9]+", "", t)


def parse_dt(s: str) -> datetime | None:
    if not s or len(s) < 14:
        return None
    m = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", s.strip())
    if not m:
        return None
    digits, offset = m.groups()
    try:
        if offset:
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
        else:
            tz = BRT_TZ
        return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=tz).astimezone(BRT_TZ)
    except ValueError:
        return None


def has_accent_or_unsafe(cid: str) -> bool:
    if not cid or " " in cid:
        return True
    if any(ch in cid for ch in ("&", "!", "+", '"', "'", "<", ">", ";")):
        return True
    nfkd = unicodedata.normalize("NFKD", cid)
    return any(unicodedata.combining(c) for c in nfkd)


def is_placeholder(title: str) -> bool:
    t = unicodedata.normalize("NFKD", (title or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    t = " ".join(t.split())
    return not t or t in PLACEHOLDER_TITLES


def find_default_file(candidates: tuple[str, ...]) -> Path | None:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return None


def load_json_safe(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def diagnose_gap_or_no_now(cid: str, info: dict[str, Any], val: dict[str, Any] | None) -> str:
    if _INTERMITTENT_RE.search(cid) and info["progs"] <= 10:
        return "Grade intermitente/eventual (canal só tem programação em horários de evento)"

    if not val:
        return "Falta de conteúdo na origem (sem metadados de validação vinculados)"

    source = val.get("source") or "desconhecida"
    candidates = val.get("candidates") or []
    n_cands = len(candidates)
    consensus = val.get("consensus") or {}
    sup = int(consensus.get("supporting_votes") or 1)
    tot = int(consensus.get("vote_sources") or 1)
    conf = int(val.get("confidence") or 0)

    if n_cands <= 1:
        return f"Falta de conteúdo na fonte única ({source}) — nenhuma outra fonte cobriu o canal para tapar o buraco"

    if tot >= 3 and (sup / max(tot, 1)) < 0.55:
        return f"Divergência/match entre fontes ({sup}/{tot} concordam, conf. {conf}%) — tapa-buracos bloqueou fontes divergentes"

    if n_cands <= 3 and _REGIONAL_OPEN_RE.search(f"{cid} {val.get('name', '')}"):
        return f"Falta de conteúdo na origem regional ({source}; {n_cands} fontes) — afiliada/grade local veio com lacuna"

    low_health_all = all(float(c.get("health_score", 1.0)) < 0.95 for c in candidates)
    if low_health_all:
        return f"Falta de conteúdo em todas as fontes ({n_cands} fontes com buraco na origem)"

    return f"Lacuna comum nas fontes alinhadas (base: {source}, {n_cands} fontes, conf. {conf}%)"


def classify_missing(name: str, source_errors: dict[str, str]) -> tuple[str, str]:
    n = (name or "").strip()
    if _FOREIGN_RE.search(n):
        return "estrangeiro", "Canal estrangeiro (fora do escopo BR)"
    if _PPV_OR_EVENT_RE.search(n):
        return "ppv_evento", "Canal de evento/PPV/24h (normal sem EPG fixo)"
    if re.search(
        r"(?:^|\s)(?:SPORTV|ESPN|PREMIERE|HBO|HISTORY)\s*\d{2}\b|"
        r"(?:TELECINE\s+\w+|HBO\s+(?:FAMILY|MUNDI|PLUS|POP|SIGNATURE|XTREME)|HISTORY|E!|FILM&ARTS|FISH\s+TV|COMBATE|NSPORTS|LIKE|MODO\s+VIAGEM|MUSIC\s+BOX\s+BRASIL|RIT\s+TV|SABOR\s*&\s*ART|TIMES\s+(?:BRASIL|EXCLUSIVO)\s+CNBC|TRAVEL\s+BOX\s+BRAZIL|TV\s+RA\s+TIM\s+BUM|XSPORTS|ZOOMOOO)\s+[1-3]\b|"
        r"\b(?:FHD60FPS|SPORTV\s+[56])\b",
        n,
        re.I,
    ):
        return "duplicata_lista", "Duplicata/opção numerada da lista M3U (canal principal já tem EPG)"
    if source_errors:
        return "br_real", f"Falta de conteúdo ou fonte fora do ar ({', '.join(list(source_errors)[:3])})"
    if _REGIONAL_OPEN_RE.search(n):
        return "br_real", "Possível problema de match/alias ou afiliada sem guia nas fontes ativas"
    return "br_real", "Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário)"


def build_markdown(data: dict[str, Any], top_n: int) -> str:
    s = data["summary"]
    lines = [
        "# Auditoria de Qualidade do EPG",
        "",
        f"- **Gerado em (BRT):** {data['generated_at_brt']}",
        f"- **Arquivo analisado:** `{data['epg_file']}`",
        f"- **Canais no XML:** **{s['xml_channels']}** | **Programas:** **{s['programmes']}**",
        f"- **Canais SEM programa agora:** **{s['no_now_count']}**",
        f"- **Canais com buracos (>{s['min_gap_minutes']}m):** **{s['gaps_count']}** / {s['xml_channels']}",
        f"- **Canais com grade curta (<{s['min_future_hours']}h):** **{s['short_horizon_count']}**",
        f"- **Canais vazios (0 programas):** **{s['empty_channels_count']}** | **IDs inseguros:** **{s['unsafe_ids_count']}**",
        f"- **Canais do catálogo sem EPG (missing):** **{s['missing_total']}** "
        f"(BR reais: **{s['missing_br_real']}** | PPV/Eventos: {s['missing_ppv_evento']} | Estrangeiros: {s['missing_estrangeiro']} | Duplicatas/lixo: {s['missing_duplicata']})",
        "",
    ]

    if data["source_errors"]:
        lines.extend(["## ⚠️ Fontes com erro nesta execução", ""])
        for src, err in data["source_errors"].items():
            lines.append(f"- **{src}:** `{err}`")
        lines.append("")

    if data["no_now"]:
        lines.extend([
            f"## 🔴 Canais SEM programa no horário atual ({len(data['no_now'])})",
            "",
            "| Canal (ID) | Fonte | Futuras | Próximo Programa | Motivo Provável |",
            "|---|---|---:|---|---|",
        ])
        for item in data["no_now"][:top_n]:
            lines.append(
                f"| `{item['channel_id']}` | {item['source']} | {item['future_hours']}h | {item['next']} | {item['reason']} |"
            )
        lines.append("")

    if data["gaps"]:
        lines.extend([
            f"## 🟡 Canais com buracos na programação (>{s['min_gap_minutes']}m) — Top {min(top_n, len(data['gaps']))} de {len(data['gaps'])}",
            "",
            "| Canal (ID) | Fonte | Buracos | Maior | Exemplos | Motivo Provável |",
            "|---|---|---:|---:|---|---|",
        ])
        for item in data["gaps"][:top_n]:
            ex = ", ".join(item["samples"][:2])
            lines.append(
                f"| `{item['channel_id']}` | {item['source']} | {item['gap_count']} | {item['max_gap_min']}m | {ex} | {item['reason']} |"
            )
        lines.append("")

    if data["short_horizon"]:
        lines.extend([
            f"## 🟠 Canais com grade curta (<{s['min_future_hours']}h futuras) ({len(data['short_horizon'])})",
            "",
            "| Canal (ID) | Fonte | Futuras | Progs | Motivo Provável |",
            "|---|---|---:|---:|---|",
        ])
        for item in data["short_horizon"][:top_n]:
            lines.append(
                f"| `{item['channel_id']}` | {item['source']} | {item['future_hours']}h | {item['progs']} | {item['reason']} |"
            )
        lines.append("")

    if data["missing_br_real"]:
        lines.extend([
            f"## ⚪ Canais BR sem EPG (missing no catálogo) ({len(data['missing_br_real'])})",
            "",
            "| Canal | Target ID | Motivo Provável |",
            "|---|---|---|",
        ])
        for item in data["missing_br_real"][:top_n]:
            lines.append(f"| {item['name']} | `{item['target_id']}` | {item['reason']} |")
        if len(data["missing_br_real"]) > top_n:
            lines.append(f"\n*... e mais {len(data['missing_br_real']) - top_n} canais BR em `audit.json`.*")
        lines.append("")

    if not (data["no_now"] or data["gaps"] or data["short_horizon"] or data["source_errors"]):
        lines.append("✅ **Nenhuma falha crítica ou buraco encontrado no EPG.**\n")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audita buracos, canais sem programação e causas prováveis.")
    parser.add_argument("arquivo", nargs="?", default=None, help="Caminho do epg.xml ou epg.xml.gz")
    parser.add_argument("--report", type=str, default=None, help="Caminho do report.json")
    parser.add_argument("--validation", type=str, default=None, help="Caminho do validation.json")
    parser.add_argument("--out-md", type=str, default="", help="Salva relatório Markdown (ex: output/audit.md)")
    parser.add_argument("--out-json", type=str, default="", help="Salva relatório JSON (ex: output/audit.json)")
    parser.add_argument("--min-gap", type=int, default=2, help="Lacuna mínima em minutos (padrão: 2)")
    parser.add_argument("--min-hours", type=int, default=24, help="Cobertura futura mínima em horas (padrão: 24)")
    parser.add_argument("--top", type=int, default=40, help="Limite de linhas por tabela (padrão: 40)")
    parser.add_argument("--busca", type=str, default="", help="Inspeciona canais específicos no terminal")
    args = parser.parse_args()

    path = Path(args.arquivo) if args.arquivo else find_default_file(
        ("output/epg.xml.gz", "epg.xml.gz", "output/epg.xml", "epg.xml")
    )
    if not path or not path.exists():
        print(f"Arquivo EPG não encontrado: {path}")
        return

    report_path = Path(args.report) if args.report else find_default_file(("output/report.json", "report.json"))
    val_path = Path(args.validation) if args.validation else find_default_file(("output/validation.json", "validation.json"))

    report_data = load_json_safe(report_path)
    val_data = load_json_safe(val_path)
    source_errors: dict[str, str] = report_data.get("source_errors") or {}

    val_by_tid: dict[str, dict[str, Any]] = {}
    val_by_name: dict[str, dict[str, Any]] = {}
    for v in val_data.get("channels") or []:
        tid = str(v.get("target_id") or "").strip().lower()
        nm = norm_key(str(v.get("name") or ""))
        if tid:
            val_by_tid[tid] = v
        if nm and nm not in val_by_name:
            val_by_name[nm] = v

    with path.open("rb") as raw:
        is_gz = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if is_gz else open

    now = datetime.now(BRT_TZ)
    channel_ids: list[str] = []
    display_names_by_ch: dict[str, list[str]] = defaultdict(list)
    by_ch: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    total_progs = 0

    with opener(path, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "channel":
                cid = (el.attrib.get("id") or "").strip()
                if cid:
                    channel_ids.append(cid)
                    for child in el:
                        if child.tag.rsplit("}", 1)[-1] == "display-name" and (child.text or "").strip():
                            display_names_by_ch[cid].append(child.text.strip())
                el.clear()
            elif tag == "programme":
                cid = (el.attrib.get("channel") or "").strip()
                dt_s = parse_dt(el.attrib.get("start", ""))
                dt_e = parse_dt(el.attrib.get("stop", ""))
                title_el = el.find("title")
                title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
                if cid and dt_s and dt_e:
                    by_ch[cid].append((dt_s, dt_e, title))
                    total_progs += 1
                el.clear()

    def lookup_validation(cid: str) -> dict[str, Any] | None:
        for dn in display_names_by_ch.get(cid, []):
            low = dn.lower()
            if low in val_by_tid:
                return val_by_tid[low]
        for cand in [cid, *display_names_by_ch.get(cid, [])]:
            nk = norm_key(cand)
            if nk in val_by_name:
                return val_by_name[nk]
        return None

    min_sec = args.min_gap * 60
    issues_by_ch: dict[str, dict[str, Any]] = {}
    no_now_out: list[dict[str, Any]] = []
    gaps_out: list[dict[str, Any]] = []
    short_out: list[dict[str, Any]] = []

    for cid in sorted(set(channel_ids) | set(by_ch)):
        rows = sorted(by_ch.get(cid, []), key=lambda x: x[0])
        ch_gaps = []
        overlaps = 0
        placeholders = 0
        now_prog = None
        next_prog = None

        for i, (s, e, title) in enumerate(rows):
            if is_placeholder(title):
                placeholders += 1
            if s <= now < e and now_prog is None:
                now_prog = (s, e, title)
            elif s > now and next_prog is None:
                next_prog = (s, e, title)
            if i + 1 < len(rows):
                ns = rows[i + 1][0]
                diff = (ns - e).total_seconds()
                if diff > min_sec:
                    ch_gaps.append((e, ns, int(diff // 60)))
                elif diff < -120:
                    overlaps += 1

        future_end = max((e for _, e, _ in rows if e > now), default=None)
        future_hours = round((future_end - now).total_seconds() / 3600.0, 1) if future_end else 0.0
        val = lookup_validation(cid)
        src_label = (val or {}).get("source") or "N/A"

        info = {
            "progs": len(rows),
            "now": now_prog,
            "next": next_prog,
            "future_hours": future_hours,
            "gaps": ch_gaps,
            "overlaps": overlaps,
            "placeholders": placeholders,
            "source": src_label,
        }
        issues_by_ch[cid] = info

        if rows:
            if now_prog is None:
                nxt_txt = f"{next_prog[0]:%d/%m %H:%M} ({next_prog[2][:28]})" if next_prog else "sem próximo"
                no_now_out.append({
                    "channel_id": cid,
                    "source": src_label,
                    "future_hours": future_hours,
                    "next": nxt_txt,
                    "reason": diagnose_gap_or_no_now(cid, info, val),
                })
            if future_hours < args.min_hours:
                short_out.append({
                    "channel_id": cid,
                    "source": src_label,
                    "future_hours": future_hours,
                    "progs": len(rows),
                    "reason": diagnose_gap_or_no_now(cid, info, val),
                })
            if ch_gaps:
                gaps_out.append({
                    "channel_id": cid,
                    "source": src_label,
                    "gap_count": len(ch_gaps),
                    "max_gap_min": max(g[2] for g in ch_gaps),
                    "samples": [f"{a:%d/%m %H:%M}-{b:%H:%M} ({m}m)" for a, b, m in ch_gaps[:5]],
                    "reason": diagnose_gap_or_no_now(cid, info, val),
                })

    gaps_out.sort(key=lambda x: (x["gap_count"], x["max_gap_min"]), reverse=True)
    short_out.sort(key=lambda x: x["future_hours"])

    empty_channels = [cid for cid in channel_ids if not by_ch.get(cid)]
    unsafe_ids = [cid for cid in channel_ids if has_accent_or_unsafe(cid)]

    missing_counts = defaultdict(int)
    missing_br_real: list[dict[str, str]] = []
    for row in report_data.get("channels") or []:
        if row.get("status") != "missing":
            continue
        name = str(row.get("name") or "")
        tid = str(row.get("target_id") or "")
        cat, reason = classify_missing(name, source_errors)
        missing_counts[cat] += 1
        if cat == "br_real":
            missing_br_real.append({"name": name, "target_id": tid, "reason": reason})

    audit_payload = {
        "generated_at_brt": now.strftime("%d/%m/%Y %H:%M:%S -0300"),
        "epg_file": str(path),
        "summary": {
            "xml_channels": len(channel_ids),
            "programmes": total_progs,
            "min_gap_minutes": args.min_gap,
            "min_future_hours": args.min_hours,
            "no_now_count": len(no_now_out),
            "gaps_count": len(gaps_out),
            "short_horizon_count": len(short_out),
            "empty_channels_count": len(empty_channels),
            "unsafe_ids_count": len(unsafe_ids),
            "missing_total": sum(missing_counts.values()),
            "missing_br_real": missing_counts["br_real"],
            "missing_ppv_evento": missing_counts["ppv_evento"],
            "missing_estrangeiro": missing_counts["estrangeiro"],
            "missing_duplicata": missing_counts["duplicata_lista"],
        },
        "source_errors": source_errors,
        "no_now": no_now_out,
        "gaps": gaps_out,
        "short_horizon": short_out,
        "empty_channels": empty_channels,
        "unsafe_ids": unsafe_ids,
        "missing_br_real": missing_br_real,
    }

    md_text = build_markdown(audit_payload, args.top)
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md_text, "utf-8")
        print(f"✓ Relatório Markdown salvo em {out_md}")
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"✓ Relatório JSON salvo em {out_json}")

    print(
        f"Auditoria: {len(channel_ids)} canais | {total_progs} progs | "
        f"sem agora: {len(no_now_out)} | com buracos (>{args.min_gap}m): {len(gaps_out)} | "
        f"missing BR reais: {missing_counts['br_real']}"
    )

    if args.busca:
        term = args.busca.lower()
        print(f"\n=== INSPEÇÃO: '{args.busca}' ===")
        for cid in sorted(k for k in issues_by_ch if term in k.lower()):
            d = issues_by_ch[cid]
            now_s = f"{d['now'][0]:%H:%M}-{d['now'][1]:%H:%M} {d['now'][2]}" if d["now"] else "❌ SEM PROGRAMA AGORA"
            print(f"[{cid}] ({d['source']}) {d['progs']} progs | {d['future_hours']}h fut | {len(d['gaps'])} buracos")
            print(f"  Agora: {now_s}")
            for a, b, m in d["gaps"][:6]:
                print(f"  ⚠ Buraco: {a:%d/%m %H:%M} -> {b:%d/%m %H:%M} ({m}m)")


if __name__ == "__main__":
    main()
