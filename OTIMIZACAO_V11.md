# Otimização v11

A v11 mantém as regras de identificação/consenso da v10 e otimiza o motor:

- 5 fontes carregadas em paralelo;
- datas/títulos parseados uma vez;
- índices exatos, fuzzy e de tvg-id pré-calculados;
- fuzzy usa limites matemáticos seguros antes de SequenceMatcher;
- comparação de grade com janela deslizante;
- cache de concordância e saúde;
- XMLTV escrito em streaming diretamente no gzip;
- gzip nível 6 por padrão;
- descarte apenas na saída de programas encerrados há mais de 12h;
- métricas de tempo em report.md/report.json.

Testes locais compararam v10 e v11 em cenários de consenso e fuzzy e produziram as mesmas escolhas de canal/fonte.
