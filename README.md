# EPG automático: Genius + Open-EPG

Este projeto usa o **EPG Genius/Curated como fonte principal** e o **Open-EPG Brazil 1 apenas para os canais ausentes**.

## Regras de correspondência

- remove os números sobrescritos `¹²³⁴` usados para identificar cópias do stream;
- remove marcadores de qualidade/codec como `HD`, `FHD`, `4K`, `[H265]` e `HDR`;
- preserva números normais do nome: `HBO` continua diferente de `HBO 2`;
- tenta nome exato no Genius;
- depois tenta nome exato no Open-EPG;
- só depois usa aproximação, com limite alto e proteção contra canais numerados;
- o arquivo final reescreve os IDs do XMLTV para os IDs `auto.*` usados na M3U corrigida.

## Arquivos importantes

- `channels.json`: nomes e IDs da sua lista, sem qualquer URL de stream;
- `merge_epg.py`: baixa, reconhece e mescla os EPGs;
- `overrides.json`: correções manuais para casos ambíguos;
- branch `epg` / `epg.xml.gz`: EPG final para cadastrar no m3u4u/player;
- branch `epg` / `report.md`: canais encontrados, fallback e canais ainda sem guia.

A branch `epg` é recriada a cada atualização e guarda somente a versão mais recente, evitando que o histórico do repositório cresça diariamente.

## Como colocar no GitHub

1. Crie um repositório **público** vazio, por exemplo `meu-epg`.
2. Envie somente o conteúdo desta pasta para o repositório.
3. Abra **Actions**, escolha **Atualizar EPG** e clique em **Run workflow**.
4. Depois da execução, o endereço do guia será:

```text
https://raw.githubusercontent.com/SEU_USUARIO/meu-epg/epg/epg.xml.gz
```

5. No m3u4u, substitua o EPG antigo por esse endereço uma única vez.
6. Importe a `lista-tvg-id-corrigido.m3u` fornecida separadamente. Não coloque essa M3U no repositório público, pois ela contém os endereços privados dos streams.

A atualização automática roda diariamente às 19:30 no horário de Maceió/Brasília e também pode ser executada manualmente.

## Corrigindo um canal ambíguo

Depois da primeira execução, abra `output/report.md`. Para forçar uma associação, edite `overrides.json`:

```json
{
  "by_target_id": {
    "auto.nome-do-canal": {
      "source": "secondary",
      "channel_id": "ID.EXATO.DO.OPEN.EPG"
    }
  }
}
```

`source` aceita `primary` ou `secondary`.

## Atualizando o catálogo no futuro

Quando sua lista mudar, rode localmente:

```bash
python extract_channels.py sua-lista.m3u --catalog channels.json --fixed-playlist lista-corrigida.m3u
```

Depois envie o novo `channels.json` ao GitHub e importe a nova M3U corrigida no serviço/player.
