# NX Media Hub: listas

Lista de canais de TV ao vivo do [NX Media Hub](https://github.com/Otavio-Novais/nx-media-hub), já sem os canais
fora do ar. Atualizada a cada 6 horas pelo GitHub Actions.

```
https://raw.githubusercontent.com/Otavio-Novais/nx-media-hub-lists/main/lista_limpa.m3u
```

## Como funciona

[`check.py`](check.py) (só a biblioteca padrão do Python):

1. Baixa as listas públicas do [iptv-org](https://github.com/iptv-org/iptv): canais em português e inglês, esportes e notícias.
2. Junta tudo e une os canais repetidos (mesmo link), somando as categorias.
3. Testa cada canal como o player do app faria: mesmo User-Agent e Referer da lista, sem verificar certificado, e
   segue o HLS até baixar um segmento de vídeo inteiro (playlist → variante de maior qualidade, a que o mpv
   escolhe → playlist de áudio, se separada → segmento).
   - Tudo isso precisa chegar em **até 10 s**, o tempo de espera aceitável na tela de carregamento.
   - O segmento precisa baixar **mais rápido do que dura** (ex.: 6 s de vídeo em menos de 6 s); senão o vídeo trava.
4. Repete os que falharam, 30 s depois e com só 8 testes em paralelo, para descartar falhas passageiras e a
   disputa de banda entre os próprios testes.
5. Esconde o canal só depois de **2 execuções seguidas** com falha. Ele volta no primeiro teste que passar.

Conta como falha: DNS, conexão recusada, timeout e qualquer erro HTTP, inclusive 401/403/451. O teste roda nos
servidores do GitHub (EUA), não na sua rede, mas numa medição de 20/10/2026, dos ~310 canais recusados nos EUA,
280 também eram recusados no Brasil (ex.: SporTV 2 e 3); os que tocavam no Brasil eram quase todos estrangeiros.
O 429 (limite de requisições) e o 416 (servidor que não aceita leitura parcial) não contam como falha. Se mais de
60% da lista falhar, a execução é descartada (problema de rede do runner) e os arquivos não mudam.

[`status.json`](status.json) lista os canais com falha, o motivo e quantas execuções seguidas falharam.
O commit só acontece quando a lista ou esse estado mudam.

## Rodar localmente

```bash
python3 check.py   # ~15 min, grava lista_limpa.m3u e status.json
```

As fontes, o tempo limite e o número de falhas para esconder ficam no topo do `check.py`.

Em repositórios públicos, o GitHub desativa workflows agendados depois de 60 dias sem atividade. Se a lista parar
de atualizar, reative em **Actions → Atualizar lista → Enable workflow**.
