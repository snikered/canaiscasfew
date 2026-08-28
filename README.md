# EPG Universal v9 — multi-M3U, nome primeiro

Esta versão foi feita para listas com `tvg-id` vazio, estranho, hash, errado ou diferente entre qualidades.

## Regra central

O **nome do canal é a identidade**. `tvg-id` antigo não decide qual canal é; ele é reaproveitado apenas como alias de compatibilidade depois que o canal é reconhecido pelo nome.

O EPG final escolhe a grade por consenso entre 5 fontes:

1. Genius / Curated
2. Open-EPG Brazil 4
3. EPGShare BR1
4. IPTV-EPG BR
5. EPGShare BR2

BR1 e BR2 dividem peso por pertencerem à mesma família EPGShare.

## Melhorias da v9

- aceita **uma ou várias M3Us ao mesmo tempo**;
- `channels.json` é reconstruído em cada Action a partir das listas atuais; não existe fallback silencioso para catálogo antigo;
- reúne todos os `tvg-id` encontrados para o mesmo canal em listas diferentes;
- `tvg-id` errado não é usado para identificar o canal;
- entradas com `tvg-id=""` recebem aliases XMLTV baseados no nome exato e no `tvg-name`;
- vários `<display-name>` são publicados para melhorar o casamento por nome;
- `HBO +` / `HBO PLUS` ficam separados de `HBO`;
- `AGRO+` / `AgroPlus.br` são equivalentes;
- `DISCOVERY H&H` = `Discovery Home & Health` = `Discovery Home and Health`;
- IDs EPGShare como `São.Paulo/SP..AMC.br` são interpretados como `AMC`;
- continua ignorando `¹²³`, HD, FHD, SD, UHD, 4K, HDR, HDR+, `[H265]`, `FHD [H265]`, HEVC etc.;
- preserva números reais: `HBO` != `HBO 2`, `SporTV 1` != `SporTV 2`.

## Configurar uma lista

No GitHub:

`Settings > Secrets and variables > Actions > New repository secret`

Crie:

- nome: `M3U_URL`
- valor: URL privada/pública da sua M3U

A URL não é escrita no repositório.

## Configurar várias listas

Crie o Secret `M3U_URLS` e coloque **uma URL por linha**:

```text
https://servidor/lista1.m3u
https://servidor/lista2.m3u
https://servidor/lista3.m3u
```

Também pode nomear cada uma usando `NOME|URL`:

```text
Casa|https://servidor/lista1.m3u
TVBox|https://servidor/lista2.m3u
Celular|https://servidor/lista3.m3u
```

`M3U_URL` e `M3U_URLS` podem coexistir. URLs repetidas são descartadas.

## Saída

Na branch `epg`:

- `epg.xml.gz` — EPG universal;
- `report.md` — correspondências e fonte escolhida;
- `validation.md` — Agora/Próximo, coerência e confiança;
- JSONs equivalentes.

A Action **não publica as M3Us**, pois elas podem conter URLs/credenciais.

## Como os IDs são publicados

Para um Animal Planet que aparece nas listas com:

```text
e6782d4e82ac2a0a70cd8332cae1997c
476c98e227890f9477494c87171291cb
```

os dois IDs recebem a mesma programação encontrada pelo nome `ANIMAL PLANET`.

Quando `tvg-id` está vazio, a v9 também cria aliases pelo nome, por exemplo:

```text
DISCOVERY SCIENCE FHD
DISCOVERY SCIENCE
```

Isso melhora a compatibilidade com players que fazem fallback pelo nome. Não existe garantia universal para `tvg-id=""`, porque o comportamento final depende do player; a M3U corrigida automática continua sendo gerada temporariamente pela Action, mas não é publicada por segurança.

## Atualizar

Substitua no repositório:

- `merge_epg.py`
- `epg_utils.py`
- `extract_channels.py`
- `.github/workflows/update-epg.yml`
- `README.md`

Depois rode `Actions > Atualizar EPG Universal > Run workflow`.
