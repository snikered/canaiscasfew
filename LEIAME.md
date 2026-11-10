# v2 — Cartoonito errado no TiviMate

## Causa
1. A lista "plus" marca TV Brasil, Cartoonito, Tooncast, Music Box e Record News com o MESMO tvg-id: `AandE.br`.
2. O EPG publicava um canal `aande.br` (grade do A&E). O TiviMate casa o ID ignorando maiúsculas/minúsculas:
   `AandE.br` = `aande.br` -> Cartoonito recebe a grade do A&E. O Televizo ignora esse ID e casa pelo nome -> certo.
3. O bloqueio manual já existia (`POISONED_OPEN_TV_IDS` com `aande`), mas o arquivo define esse conjunto DUAS vezes e a
   2ª definição apagava `aande`, `agromais`, `terraviva` etc.: o bloqueio nunca valeu.
4. O `import name_dedupe` falhava em silêncio quando o arquivo não estava no repo, e o EPG saía com 1.466 nomes repetidos
   (o nome "Cartoonito HD [1]" estava no canal TV BRASIL também).

## Mudanças em merge_epg.py
- Corrige IndentationError na linha 1188 (`  def get_clean_aliases`): o arquivo enviado não executava.
- Une os dois conjuntos de IDs envenenados.
- Detecção AUTOMÁTICA: todo tvg-id usado por canais diferentes nas suas listas deixa de ser exportado (vale para qualquer lista).
- Deduplicação de nomes embutida no arquivo (sem dependência extra) + tratamento de sobrescritos (`²`).
- Guardas de emissora/UF da v1 mantidas.
