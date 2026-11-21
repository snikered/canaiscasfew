# Auditoria de Qualidade do EPG

- **Gerado em (BRT):** 21/11/2026 14:40:58 -0300
- **Arquivo analisado:** `output/epg.xml.gz`
- **Canais no XML:** **748** | **Programas:** **58883**
- **Canais SEM programa agora:** **1**
- **Canais com buracos (>2m):** **52** / 748
- **Canais com grade curta (<24h):** **1**
- **Canais vazios (0 programas):** **0** | **IDs inseguros:** **0**
- **Canais do catálogo sem EPG (missing):** **1097** (BR reais: **194** | PPV/Eventos: 233 | Estrangeiros: 638 | Duplicatas/lixo: 32)

## 🔴 Canais SEM programa no horário atual (1)

| Canal (ID) | Fonte | Futuras | Próximo Programa | Motivo Provável |
|---|---|---:|---|---|
| `sportv.4` | Globo | 12.3h | 22/11 00:00 (Aquecimento sportv: Fórmula ) | Falta de conteúdo na fonte única (Globo) — nenhuma outra fonte cobriu o canal para tapar o buraco |

## 🟡 Canais com buracos na programação (>2m) — Top 40 de 52

| Canal (ID) | Fonte | Buracos | Maior | Exemplos | Motivo Provável |
|---|---|---:|---:|---|---|
| `sbt.cuiaba` | MiTV | 4 | 165m | 22/11 15:15-18:00 (165m), 23/11 12:30-12:40 (10m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.cuiaba.mt` | Open-EPG BR | 4 | 165m | 22/11 15:15-18:00 (165m), 23/11 12:30-12:40 (10m) | Falta de conteúdo na origem regional (Open-EPG BR; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.mt` | MiTV | 4 | 165m | 22/11 15:15-18:00 (165m), 23/11 12:30-12:40 (10m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.alagoas` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.alagoas.tv.ponta.verde` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.jornal` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `sbt.jornal.pe` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `sbt.pa` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.pa.guajara` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.para` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.pi` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.sc` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.to` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.jornal.recife` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.piaui` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.serra.dourada` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `tv.cidade.verde.picos` | MiTV | 2 | 165m | 22/11 15:15-18:00 (165m), 24/11 05:30-06:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `FilmAndArts.br` | Open-EPG BR 4 | 2 | 140m | 23/11 00:10-02:30 (140m), 23/11 11:00-13:15 (135m) | Lacuna comum nas fontes alinhadas (base: Open-EPG BR 4, 12 fontes, conf. 76%) |
| `malhacao.fast` | Globoplay Oficial API | 2 | 60m | 24/11 04:29-05:30 (60m), 24/11 08:29-09:00 (30m) | Lacuna comum nas fontes alinhadas (base: Globoplay Oficial API, 3 fontes, conf. 59%) |
| `tv.assembleia` | MiTV | 2 | 60m | 21/11 12:00-12:15 (15m), 22/11 06:00-07:00 (60m) | Falta de conteúdo na fonte única (MiTV) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `record.itapoan` | MiTV | 2 | 35m | 23/11 06:00-06:10 (10m), 23/11 14:55-15:30 (35m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `record.tv.bahia` | MiTV | 2 | 35m | 23/11 06:00-06:10 (10m), 23/11 14:55-15:30 (35m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `record.tv.bahia.itabuna` | MiTV | 2 | 35m | 23/11 06:00-06:10 (10m), 23/11 14:55-15:30 (35m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `recordtv.ba` | MiTV | 2 | 35m | 23/11 06:00-06:10 (10m), 23/11 14:55-15:30 (35m) | Lacuna comum nas fontes alinhadas (base: MiTV, 2 fontes, conf. 53%) |
| `tvebahia.br` | MiTV | 2 | 30m | 23/11 11:30-12:00 (30m), 24/11 11:30-12:00 (30m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `Pantanal` | Open-EPG BR | 1 | 1320m | 22/11 01:30-23:30 (1320m) | Falta de conteúdo na fonte única (Open-EPG BR) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `band.fm.bahia` | Open-EPG BR | 1 | 1320m | 22/11 01:30-23:30 (1320m) | Divergência/match entre fontes (1/10 concordam, conf. 17%) — tapa-buracos bloqueou fontes divergentes |
| `sbt.ma` | Open-EPG BR | 1 | 1320m | 22/11 01:30-23:30 (1320m) | Divergência/match entre fontes (1/13 concordam, conf. 18%) — tapa-buracos bloqueou fontes divergentes |
| `sbt.sao.luiz.ma` | Open-EPG BR | 1 | 1320m | 22/11 01:30-23:30 (1320m) | Divergência/match entre fontes (1/13 concordam, conf. 18%) — tapa-buracos bloqueou fontes divergentes |
| `redetvpampa.br` | MiTV | 1 | 210m | 22/11 15:00-18:30 (210m) | Falta de conteúdo em todas as fontes (2 fontes com buraco na origem) |
| `RMINAS` | Open-EPG BR | 1 | 180m | 23/11 03:00-06:00 (180m) | Lacuna comum nas fontes alinhadas (base: Open-EPG BR, 2 fontes, conf. 53%) |
| `sbt.jangadeiro` | MiTV | 1 | 165m | 22/11 15:15-18:00 (165m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `sbt.jangadeiro.ce` | MiTV | 1 | 165m | 22/11 15:15-18:00 (165m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.aratu.bahia` | MiTV | 1 | 165m | 22/11 15:15-18:00 (165m) | Falta de conteúdo na origem regional (MiTV; 3 fontes) — afiliada/grade local veio com lacuna |
| `rede.21` | Claro TV+ API | 1 | 120m | 21/11 09:00-11:00 (120m) | Lacuna comum nas fontes alinhadas (base: Claro TV+ API, 3 fontes, conf. 49%) |
| `band.natal` | MiTV | 1 | 90m | 23/11 06:30-08:00 (90m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `rede.amazonica.cruzeiro.do.sul` | Globo Oficial API | 1 | 90m | 24/11 04:00-05:30 (90m) | Falta de conteúdo na origem regional (Globo Oficial API; 2 fontes) — afiliada/grade local veio com lacuna |
| `rede.familia` | MiTV | 1 | 60m | 24/11 04:00-05:00 (60m) | Lacuna comum nas fontes alinhadas (base: MiTV, 2 fontes, conf. 54%) |
| `tv.diario.br` | MiTV | 1 | 60m | 24/11 04:00-05:00 (60m) | Lacuna comum nas fontes alinhadas (base: MiTV, 2 fontes, conf. 52%) |
| `SBTRIO` | Vivo Play API | 1 | 30m | 24/11 05:30-06:00 (30m) | Lacuna comum nas fontes alinhadas (base: Vivo Play API, 3 fontes, conf. 55%) |

## 🟠 Canais com grade curta (<24h futuras) (1)

| Canal (ID) | Fonte | Futuras | Progs | Motivo Provável |
|---|---|---:|---:|---|
| `sportv.4` | Globo | 12.3h | 2 | Falta de conteúdo na fonte única (Globo) — nenhuma outra fonte cobriu o canal para tapar o buraco |

## ⚪ Canais BR sem EPG (missing no catálogo) (194)

| Canal | Target ID | Motivo Provável |
|---|---|---|
| SPORTYNET 4 | `auto.sportynet4` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| HBO MAIS | `auto.hbomais` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Love Nature | `auto.lovenature` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV GIDEOES | `auto.tvgideoes` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| BLACK IS KING | `auto.blackisking` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Casqueiro TV | `auto.casqueirotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CBC NEWS NETWORK | `auto.cbcnewsnetwork` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CBC NEWS OTTAWA | `auto.cbcnewsottawa` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CBC NEWS TORONTO | `auto.cbcnewstoronto` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CHDTV | `auto.chdtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CNN PORTUGAL | `auto.cnnportugal` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| COM BRASIL | `auto.combrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| GOSPEL MOVIES TELEVISION | `auto.gospelmoviestelevision` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| IGREJA UNIVERSAL | `auto.igrejauniversal` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| KRONE HIT | `auto.kronehit` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| LIVEHENRIQUEANDJULIANO1904 | `auto.livehenriqueandjuliano1904` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| MusicTop | `auto.musictop` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| NATURE TIME | `auto.naturetime` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| NEW BRASIL | `auto.newbrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| NSPORTS 1 | `auto.nsports1` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Play TV Horse | `auto.playtvhorse` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Rede Universo TV | `auto.redeuniversotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SPORTYNETPLUS13 | `auto.sportynetplus13` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SPORTYNETPLUS21 | `auto.sportynetplus21` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SP USA Bally Sports Kansas CITY | `auto.spusaballysportskansascity` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SP USA Bally Sports NEW Orleans PLUS | `auto.spusaballysportsneworleansplus` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SP USA Nbcsn Washington | `auto.spusanbcsnwashington` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV Igreja Mundial | `auto.tvigrejamundial` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV Nova Juarez | `auto.tvnovajuarez` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV WS BRASIL | `auto.tvwsbrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Universo TV | `auto.universotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| AGROBRASILTV | `auto.agrobrasiltv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| AGRO CANAL | `auto.agrocanal` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| BAH! TV | `auto.bahtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CANALGOAT012 | `auto.canalgoat012` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CANALGOAT022 | `auto.canalgoat022` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CANALGOAT032 | `auto.canalgoat032` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CATVE | `auto.catve` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CENTRALTOP TV | `auto.centraltoptv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| COMBATE 1 | `auto.combate1` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |

*... e mais 154 canais BR em `audit.json`.*
