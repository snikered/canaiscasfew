# Abastecimento inicial do dicionário v10

Foram analisadas as listas fornecidas nesta conversa. Uma das listas atuais era duplicata exata de outra e foi ignorada para não inflar o aprendizado.

## Resultado do banco

- Identidades aprendidas antes da consolidação semântica: **3.310**
- Identidades compiladas no `channels.json`: **3.310**
- `tvg-id` de compatibilidade considerados seguros: **811**
- `tvg-id` ambíguos bloqueados: **123**
- Fontes M3U únicas representadas no banco: **8** (6 listas únicas do lote atual + 2 listas já analisadas anteriormente)

O filtro de aprendizado descarta a maior parte de catálogos de filmes/séries/24H. Isso foi necessário porque algumas M3Us possuem centenas de milhares de entradas VOD misturadas com poucas centenas de canais lineares.

## Exemplos já aprendidos

### AMC
IDs seguros observados incluem:

- `AMC.br`
- `amc.br`
- `Amc.br`
- `5e98607e8d76e68daa5b3acb9f0c1604`
- `AMC HD`
- `São.Paulo/SP..AMC.br (src05)`

### Animal Planet
IDs seguros observados incluem:

- `animalplanet.br`
- `AnimalPlanet.br`
- `ANIPB.br (m3u4u)`
- `Animal Planet HD`
- `476c98e227890f9477494c87171291cb`
- `e6782d4e82ac2a0a70cd8332cae1997c`

Os dois hashes diferentes de Animal Planet são mantidos como aliases da mesma programação.

### Sony
Já foram associados `SonyChannel.br`, `sony.br`, `SETB.br (m3u4u)` e um hash de fornecedor à identidade `Sony`.

### SporTV 1
`Sportv.br`, `br#sportv-hd`, hashes e variantes `SporTV HD/SD` convergem para `SporTV 1`.

### History / H2
History e History 2 são identidades separadas. `H2` converge para `History 2`; `History Channel` converge para `History`.
