# v3 — Brasília / Campinas / emissora trocada

Substitua `merge_epg.py` e `tests/test_open_tv_matching.py` (a v2 já trouxe o dedupe embutido; não precisa de name_dedupe.py).

## O que houve
- **Brasília** (Globo/Record/SBT/Band): 4 fontes aproximavam `TVBRASILIA ~ TVBRASIL` (TV Brasil/EBC). Como concordavam entre si,
  o "consenso" vencia o nome exato (MiTV "Record TV Brasília" etc.). Agora: fuzzy BRASILIA<->BRASIL bloqueado e, no ranking,
  nome exato vence consenso de aproximações.
- **Campinas** (Band/Record/SBT): `TVCAMPINAS ~ EPTVCAMPINAS` (EPTV é da Globo). Fuzzy não pode mais "adicionar" marca de afiliada da Globo.
- **Outros**: SBT/Record/Band não aceitam mais canal da fonte que é de TV Brasil, Rede TV, outra emissora ou afiliada exclusiva da Globo
  (EPTV, NSC, RPC, TV Sergipe, TV Paraíba...); mesma emissora com cidade diferente (SBT Campinas ~ SBT Minas, Record Manaus ~ Record Minas) também.
