# EPG Universal v11 — dicionário persistente + motor otimizado

Esta versão não precisa mais de uma M3U em cada atualização diária.

## Otimizações de desempenho da v11

A lógica de identificação e consenso é a mesma da v10. A v11 muda principalmente **como** o trabalho é executado:

- baixa/processa as 5 fontes em paralelo;
- datas e títulos dos programas são parseados uma única vez;
- comparação de grades usa janela deslizante em vez de varrer a lista inteira repetidamente;
- resultados de saúde/concordância são cacheados;
- fuzzy continua usando o mesmo `SequenceMatcher`, threshold e margem, mas elimina candidatos matematicamente incapazes de atingir a nota mínima;
- `tvg-id` antigo usa índice direto em vez de procurar em todos os canais da fonte;
- o XML final é escrito diretamente em `epg.xml.gz`, sem montar uma árvore gigante e sem `deepcopy()` para cada programa;
- gzip padrão passou de nível 9 para nível 6, reduzindo bastante CPU com pequena diferença de tamanho;
- programas encerrados há mais de 12 horas não são republicados no XML final; isso não altera a votação/validação e reduz o arquivo;
- `report.md` e `report.json` agora mostram o tempo gasto por etapa.

Por padrão a rotina diária gera apenas `epg.xml.gz` (que é o arquivo publicado). Se precisar também do XML sem compressão, defina `WRITE_PLAIN_XML=1`.

## Arquitetura

- `channel_dictionary.json` — regras semânticas permanentes e aliases conhecidos.
- `learned_ids.json` — memória aprendida de M3Us: nomes, aliases e tvg-id vistos.
- `channels.json` — catálogo compilado dos dois arquivos acima. É gerado automaticamente.
- `learn_channels.py` — aprende uma ou várias M3Us sem guardar URLs de stream.
- `compile_dictionary.py` — compila o dicionário para o formato consumido pelo gerador.
- `merge_epg.py` — encontra os canais nas 5 fontes, compara a programação e escolhe a grade por consenso.
- `preparar_importacao.html` — transforma uma M3U local em arquivo seguro para aprendizado, sem URLs dos streams.

## Fontes EPG

1. Genius / Curated — principal na ordem de desempate.
2. Open-EPG Brazil4.
3. EPGShare BR1.
4. IPTV-EPG Brasil.
5. EPGShare BR2.

A prioridade só desempata. Se outras fontes concordarem melhor entre si, a fonte usada pode mudar canal a canal.
BR1 e BR2 pertencem à mesma família EPGShare e compartilham peso no cálculo decisivo.

## Como o dicionário aprende

O nome do canal é a identidade principal. O `tvg-id` é compatibilidade, não verdade.

Exemplo: se várias listas apresentarem:

- `AMC FHD` + `AMC.br`
- `AMC HDR+` + `5e9860...`
- `AMC HD` + `São.Paulo/SP..AMC.br (src05)`

as três relações são armazenadas sob a identidade `AMC`. O EPG final pode publicar a mesma grade em todos esses IDs.

Um `tvg-id` só é promovido se estiver associado a uma única identidade de canal. Se o mesmo ID aparecer em canais diferentes, ele entra em `ambiguous_ids` e não é publicado automaticamente.

## Normalização de nomes

O identificador pelo nome ignora marcadores de qualidade/cópia como:

- `HD`, `FHD`, `SD`, `4K`, `8K`, `UHD`
- `HDR`, `HDR+`, `HDR10+`
- `[H265]`, `H265`, `H.265`, `HEVC`, AVC
- marcadores sobrescritos `¹²³⁴...`

Números normais são preservados: `HBO` != `HBO 2`; `SporTV 1` != `SporTV 2`.
O `+` semântico também é preservado: `HBO +` = `HBO Plus`, mas é diferente de `HBO`.

Há aliases conhecidos para casos como A&E/A and E, H2/History 2, Canal Sony/Sony, SporTV/SporTV 1, Discovery H&H/Home & Health, etc.

## Atualização diária do EPG

O workflow `.github/workflows/update-epg.yml` faz apenas:

1. compila `channel_dictionary.json` + `learned_ids.json`;
2. baixa as 5 fontes EPG;
3. encontra cada canal pelo nome;
4. compara a grade entre as fontes;
5. escolhe a fonte com maior consenso;
6. publica `epg.xml.gz`, `report.md` e `validation.md` na branch `epg`.

Nenhuma M3U é necessária nessa rotina.

## Ensinar uma lista nova — método recomendado (URL como Secret)

Use este método quando sua lista possui uma URL. A URL e as credenciais não ficam no repositório.

1. Abra o repositório no GitHub.
2. Vá em `Settings`.
3. Entre em `Secrets and variables` > `Actions`.
4. Clique em `New repository secret`.
5. Nome: `LEARN_M3U_URLS`.
6. No valor, coloque uma URL por linha. Também pode usar `Nome|URL`:

```text
Lista A|https://servidor/lista.m3u
Lista B|https://outro-servidor/lista.m3u
```

7. Salve o Secret.
8. Vá em `Actions`.
9. Abra `Aprender canais de M3U`.
10. Clique em `Run workflow` e confirme `main`.
11. O Action baixa as listas temporariamente, aprende nomes/IDs, atualiza `learned_ids.json` e `channels.json`, e grava somente esses metadados no repositório.
12. Depois rode `Atualizar EPG Universal` para gerar o novo EPG imediatamente. Caso contrário, ele entrará na próxima atualização agendada.

O Secret pode ficar salvo para reaprender a lista futuramente ou pode ser removido após a importação.

## Ensinar uma lista local sem colocar streams no GitHub

Não envie a M3U original para um repositório público: ela costuma conter usuário, senha/token e URLs dos streams.

1. Baixe/abra `preparar_importacao.html` no PC ou Android.
2. Clique em escolher arquivo e selecione a M3U original.
3. Clique em `Gerar arquivo seguro`.
4. Será criado algo como `minha-lista-canais.safe.m3u`.
5. Esse arquivo contém apenas `tvg-id`, `tvg-name`, grupo e nome do canal. URLs de streams, credenciais e logos não são exportados.
6. No GitHub abra a pasta `learning`.
7. `Add file` > `Upload files`.
8. Envie o arquivo `*.safe.m3u` e faça `Commit changes` na `main`.
9. Vá em `Actions` > `Aprender canais de M3U` > `Run workflow`.
10. O Action incorpora o aprendizado em `learned_ids.json` e recompila `channels.json`.
11. Rode `Atualizar EPG Universal`.

O arquivo `.safe.m3u` pode ser apagado depois; o aprendizado permanece no `learned_ids.json`.

## Se o repositório for privado

O workflow também consegue ler arquivos `learning/*.safe.m3u`. Mesmo em repositório privado, ainda é preferível não versionar uma M3U original com credenciais. Use Secret ou o arquivo sanitizado.

## Como adicionar uma regra manual

Edite apenas `channel_dictionary.json` quando houver uma ambiguidade semântica real. Exemplo:

```json
"SONY": {
  "name": "Sony",
  "aliases": ["Sony", "Canal Sony", "Sony Channel"]
}
```

Não é necessário cadastrar qualidades como FHD/HDR+/H265; a normalização já remove esses marcadores.

## Relatórios

Na branch `epg`:

- `report.md` — fonte escolhida, canais sem EPG, trocas feitas por consenso e aproximações.
- `validation.md` — Agora/Próximo, saúde da grade e concordância entre fontes.

Use esses relatórios para descobrir aliases que realmente precisam entrar no dicionário manual.
