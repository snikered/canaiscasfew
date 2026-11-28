# Auditoria de Qualidade do EPG

- **Gerado em (BRT):** 28/11/2026 14:53:02 -0300
- **Arquivo analisado:** `output/epg.xml.gz`
- **Canais no XML:** **742** | **Programas:** **58241**
- **Canais SEM programa agora:** **0**
- **Canais com buracos (>2m):** **21** / 742
- **Canais com grade curta (<24h):** **0**
- **Canais vazios (0 programas):** **0** | **IDs inseguros:** **0**
- **Canais do catálogo sem EPG (missing):** **1092** (BR reais: **42** | PPV/Eventos: 304 | Estrangeiros: 679 | Duplicatas/lixo: 67)

## 🟡 Canais com buracos na programação (>2m) — Top 21 de 21

| Canal (ID) | Fonte | Buracos | Maior | Exemplos | Motivo Provável |
|---|---|---:|---:|---|---|
| `sportv.4` | Globo | 2 | 1230m | 28/11 12:29-14:30 (120m), 28/11 15:59-12:30 (1230m) | Falta de conteúdo na fonte única (Globo) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `malhacao.fast` | Globoplay Oficial API | 2 | 30m | 30/11 06:29-07:00 (30m), 30/11 07:29-08:00 (30m) | Lacuna comum nas fontes alinhadas (base: Globoplay Oficial API, 3 fontes, conf. 56%) |
| `redetvpampa.br` | MiTV | 1 | 90m | 29/11 17:00-18:30 (90m) | Falta de conteúdo em todas as fontes (2 fontes com buraco na origem) |
| `Pantanal` | Dearbulut BR | 1 | 75m | 29/11 01:45-03:00 (75m) | Falta de conteúdo em todas as fontes (2 fontes com buraco na origem) |
| `sbt.ma` | Dearbulut BR | 1 | 75m | 29/11 01:45-03:00 (75m) | Divergência/match entre fontes (2/15 concordam, conf. 38%) — tapa-buracos bloqueou fontes divergentes |
| `sbt.sao.luiz.ma` | Dearbulut BR | 1 | 75m | 29/11 01:45-03:00 (75m) | Divergência/match entre fontes (2/15 concordam, conf. 38%) — tapa-buracos bloqueou fontes divergentes |
| `SESCTV` | MiTV | 1 | 60m | 01/12 05:00-06:00 (60m) | Lacuna comum nas fontes alinhadas (base: MiTV, 3 fontes, conf. 56%) |
| `sbt.alagoas` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.alagoas.tv.ponta.verde` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.ba` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na fonte única (MiTV) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `sbt.jornal` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.jornal.pe` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.maringa` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na fonte única (MiTV) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `sbt.mg` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na fonte única (MiTV) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `sbt.pe` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na fonte única (MiTV) — nenhuma outra fonte cobriu o canal para tapar o buraco |
| `sbt.pi` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.sc` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.to` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.jornal.recife` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `sbt.tv.piaui` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |
| `tv.cidade.verde.picos` | MiTV | 1 | 45m | 29/11 17:15-18:00 (45m) | Falta de conteúdo na origem regional (MiTV; 2 fontes) — afiliada/grade local veio com lacuna |

## ⚪ Canais BR sem EPG (missing no catálogo) (42)

| Canal | Target ID | Motivo Provável |
|---|---|---|
| SBT PLUS Novelas | `auto.sbtplusnovelas` | Possível problema de match/alias ou afiliada sem guia nas fontes ativas |
| Love Nature | `auto.lovenature` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV GIDEOES | `auto.tvgideoes` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| BLACK IS KING | `auto.blackisking` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Casqueiro TV | `auto.casqueirotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CHDTV | `auto.chdtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| COM BRASIL | `auto.combrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| GOSPEL MOVIES TELEVISION | `auto.gospelmoviestelevision` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| IGREJA UNIVERSAL | `auto.igrejauniversal` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| LIVEHENRIQUEANDJULIANO1904 | `auto.livehenriqueandjuliano1904` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| MusicTop | `auto.musictop` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| NATURE TIME | `auto.naturetime` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| NEW BRASIL | `auto.newbrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Play TV Horse | `auto.playtvhorse` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Rede Universo TV | `auto.redeuniversotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SBT PLUS Silvio Santos | `auto.sbtplussilviosantos` | Possível problema de match/alias ou afiliada sem guia nas fontes ativas |
| SBT PLUS Tvzyn | `auto.sbtplustvzyn` | Possível problema de match/alias ou afiliada sem guia nas fontes ativas |
| SPORTSNET | `auto.sportsnet` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV Igreja Mundial | `auto.tvigrejamundial` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV Nova Juarez | `auto.tvnovajuarez` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TV WS BRASIL | `auto.tvwsbrasil` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Universo TV | `auto.universotv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| AGROBRASILTV | `auto.agrobrasiltv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| BAH! TV | `auto.bahtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CATVE | `auto.catve` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| CENTRALTOP TV | `auto.centraltoptv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| DJTV | `auto.djtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| FRFRANCE243 | `auto.frfrance243` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Igreja Mundial | `auto.igrejamundial` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| IGREJA MUNDIAL IMPD | `auto.igrejamundialimpd` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| KIDS MAIS | `auto.kidsmais` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| MUsic MC Dance | `auto.musicmcdance` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| REDE GENIISES | `auto.redegeniises` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| Remo TV 1 | `auto.remotv1` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| RETRO MUSIC | `auto.retromusic` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| ROCK BR TV | `auto.rockbrtv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| ROCK TV | `auto.rocktv` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| RS21 | `auto.rs21` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| SANTUÁRIO DE FÁTIMA | `auto.santuariodefatima` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |
| TBC BRASIL CENTRAL GO | `auto.tbcbrasilcentralgo` | Falta de conteúdo nas fontes ativas (ou alias ausente no dicionário) |

*... e mais 2 canais BR em `audit.json`.*
