# EPG Universal — agregador multi-fonte

O EPG é publicado como XMLTV (`output/epg.xml.gz`) e não exige reescrever a M3U do usuário. A identificação da grade é feita pelo nome normalizado do canal; `tvg-id` aprendido é apenas uma compatibilidade adicional quando não existe colisão.

## Como o reconhecimento funciona

1. O catálogo `channels.json` é compilado a partir do dicionário manual `channel_dictionary.json` e do aprendizado em `learned_ids.json`.
2. O agregador compara o nome canônico e os aliases com os nomes e IDs publicados pelas fontes XMLTV.
3. A correspondência exata tem prioridade. A aproximação só é aceita com limiar alto e margem de segurança para evitar atribuir a grade de outro canal.
4. Só contam como correspondência as fontes com pelo menos um programa e horário válidos. Canais XMLTV que tenham `<programme>` mas não declarem `<channel>` também são indexados pelo próprio ID.
5. IDs antigos compartilhados por canais diferentes são descartados como compatibilidade. Eles não podem identificar com segurança mais de uma grade.

## Adicionar um alias ou canal novo

Edite `channel_dictionary.json`, dentro de `channels`. Use a chave canônica em maiúsculas, um `name` e uma lista `aliases`:

```json
{
  "channels": {
    "DOGTV": {
      "name": "Dog TV",
      "aliases": ["DOGTV", "DOG TV", "US DOG TV"]
    }
  }
}
```

- Para um canal que já existe, acrescente os nomes alternativos na lista `aliases` da chave canônica.
- Para um canal novo, crie uma chave nova com `name` e `aliases`.
- Depois execute `python compile_dictionary.py --catalog channels.json` e `python merge_epg.py`.
- No GitHub Actions, o fluxo de atualização compila o catálogo automaticamente antes de gerar o EPG.

Não acrescente aliases genéricos como `SBT` a canais regionais. Mantenha feeds diferentes (por exemplo, `Food Network` e `Food Network (east)`) separados quando possam ter grades/horários diferentes.

## Limite técnico importante sobre `tvg-id`

Um XMLTV estático não recebe a M3U de cada usuário. Portanto, não consegue descobrir em tempo real qual canal o usuário colocou em uma entrada. O projeto publica IDs canônicos, IDs antigos que são comprovadamente exclusivos e nomes alternativos em `display-name`/IDs de compatibilidade.

Se uma playlist usa o mesmo `tvg-id` incorreto para dois ou mais canais (por exemplo, `agromais.br` atribuído a Cartoonito, Travel Box e SBT TV Jornal Recife), esse ID é ambíguo: um único `programme channel="agromais.br"` não pode apontar para três grades diferentes. O agregador deliberadamente não publica esse ID compartilhado. Nesses casos, o player precisa aceitar correspondência pelo nome (`tvg-name`/`display-name`) ou a playlist teria de corrigir o ID. Isso é uma limitação do formato XMLTV estático, não algo que um dicionário consiga resolver sozinho.

## Diagnóstico

Após a execução, consulte:

- `output/report.json`: correspondência por canal, método usado, contagem de programas válidos, fontes candidatas e sugestões para canais sem correspondência segura.
- `output/validation.json`: saúde da grade, agora/próximo, confiança e comparação entre fontes.
- `output/report.md` e `output/validation.md`: resumos legíveis.

Para canais sem grade, `report.json` diferencia nome não encontrado de nome encontrado em fonte sem horários válidos e lista até oito sugestões por fonte.

## Testes locais

```bash
python -m unittest discover -s tests -v
```
