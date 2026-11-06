# Correções do reconhecimento de canais — EPG Universal

## Objetivo

O EPG permanece uma URL XMLTV estática e deve se adaptar às M3Us, sem exigir que o usuário reescreva suas listas. As mudanças abaixo atuam no catálogo, no reconhecimento das fontes e na validação das grades.

## Mudanças

- `channel_dictionary.json`: aliases manuais para Dog TV, Food Network, Fashion TV, Cartoonito, Canal OFF, Multishow, Curta! e SBT.
- `channels.json`: recompilado usando o dicionário atualizado (1.683 identidades canônicas; 94 IDs de compatibilidade exclusivos).
- `learn_channels.py`: ao reaprender listas, registros antigos são reagrupados conforme os aliases canônicos atuais. IDs compartilhados não são mais confiados só porque o prefixo se parece com o nome de um dos canais.
- `compile_dictionary.py`: remove a exceção que promovia IDs ambíguos (notadamente `sbt.br`) a confiáveis quando o prefixo coincidia com um canal.
- `merge_epg.py`: só considera correspondência bem-sucedida quando há pelo menos um programa com horário válido; prioriza candidatos que realmente têm grade; aceita fontes XMLTV com `<programme>` sem um `<channel>` declarado; e grava sugestões/diagnósticos no relatório para nomes sem correspondência segura.
- `.github/workflows/update-epg.yml` e `.github/workflows/learn-m3u.yml`: executam os testes automatizados.
- `tests/test_epg_channel_matching.py`: oito testes de regressão.

## Unificações e separações intencionais

- `DOGTV` + `US DOG TV` → identidade canônica `DOGTV`.
- `FOOD NETWORK` + `US FOOD Network` → identidade canônica `FOODNETWORK`.
- `US FOOD Network (east)` permanece separado, pois pode ter grade/horário diferente.
- `FASHION TV`/`FashionTV` → `FASHIONTV`.
- `CARTOONITO` e `BOOMERANG` → `CARTOONITO`.
- `OFF` + `CANAL OFF` → `OFF` (o catálogo original contém `OFF`; não foi encontrado um canal chamado `OOF`).
- `MULTISHOW` e `CURTA!` permanecem em suas identidades próprias.
- SBT nacional permanece separado de todas as afiliadas/regionais.

## Colisões de IDs encontradas

- `agromais.br` foi aprendido para Cartoonito, Travel Box e SBT TV Jornal Recife.
- `sbt.br` foi aprendido para o SBT nacional e diversas entradas regionais.

Esses IDs não são mais publicados como aliases exclusivos. A escolha evita que o mesmo EPG seja erroneamente atribuído a canais diferentes.

## Limitação que não pode ser resolvida dentro de um XMLTV estático

O XMLTV não recebe a M3U do assinante. Se dois ou mais canais da mesma lista usam o mesmo `tvg-id` errado, não existe uma forma inequívoca de fazer esse único ID representar grades diferentes. O projeto publica IDs únicos e aliases de nome; quando o player prioriza `tvg-id` e ignora `tvg-name`/`display-name`, ele ainda pode deixar de associar os canais que compartilham um ID incorreto. Isso exigiria suporte de correspondência por nome no player ou um serviço dinâmico que recebesse a playlist do usuário — não uma edição manual do feed XMLTV.

## Verificação

Executado localmente:

```bash
python -m py_compile *.py tests/*.py
python -m unittest discover -s tests -v
```

Resultado: 8 testes aprovados. Os workflows também foram validados sintaticamente como YAML.

A execução completa das 16 fontes remotas não pôde ser feita neste ambiente porque a resolução DNS externa está indisponível. Portanto, os testes comprovam o comportamento do código e dos casos sintéticos, mas não comprovam a disponibilidade/programação atual de cada fonte real.
