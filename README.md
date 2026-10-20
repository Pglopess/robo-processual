# Robô de Consulta Processual

## Sobre

Robô em Python que recebe uma lista de números de processo, valida cada um pelo padrão CNJ, consulta os dados (pela API pública do DataJud ou navegando num portal web com Playwright), baixa o inteiro teor em PDF e entrega um relatório em Excel com o que deu certo e o que falhou.

O cenário é o de um escritório de contencioso cível de massa: milhares de processos que alguém precisaria abrir um a um para ver a última movimentação. O foco do projeto não é só "fazer o robô clicar", e sim o que separa um script de um robô que dá para deixar rodando sozinho: validação antes de consultar, retry com backoff, retomada sem repetir trabalho, evidência de erro e relatório que a equipe jurídica consegue usar.

Para não fazer scraping em tribunal real, o projeto inclui um portal de tribunal simulado que reproduz de propósito os problemas típicos desses sites: iframe, lentidão, erro 503 aleatório, sessão que expira e conteúdo renderizado por JavaScript.

## Funcionalidades

- Validação do número CNJ (formato e dígito verificador módulo 97, Resolução CNJ nº 65/2008), aceitando com ou sem pontuação
- Identificação do tribunal pelo número e mapeamento para o índice correspondente no DataJud (TJs, TRTs, TRFs e STJ)
- Duas fontes de dados com a mesma interface:
  - `datajud`: API pública oficial do CNJ, com retry em 429/5xx e parada imediata se a chave for recusada
  - `portal`: robô Playwright que faz login, preenche o formulário dentro de um iframe, espera o resultado, extrai as movimentações e baixa o PDF
- Retry com backoff exponencial em falhas temporárias
- Relogin automático quando a sessão expira, sem contar como falha do portal
- Sessão salva em disco (`storage_state`) para não logar a cada execução
- Conferência de que a página exibida é do processo pedido antes de extrair
- Screenshot automático em cada erro e trace opcional do Playwright
- Estado em SQLite gravado processo a processo: se o robô cair no meio, a próxima execução continua de onde parou
- Duplicados e números inválidos tratados antes de abrir o navegador
- Relatório Excel com abas Resumo, Processos e Falhas
- Log em arquivo e código de saída diferente de zero quando há falha, para alertas em agendador

## Tecnologias

- Python 3.10+
- Playwright (automação de navegador)
- Requests (API DataJud)
- pandas e openpyxl (relatório Excel)
- SQLite (estado da execução)
- pytest (testes unitários e ponta a ponta)

## Estrutura do projeto

```
robo-processual/
    robo/
        cnj.py              validação e parsing do número CNJ
        datajud.py          cliente da API pública do DataJud
        portal.py           robô Playwright para o portal web
        estado.py           estado persistente em SQLite (retomada)
        relatorio.py        geração do Excel
        main.py             linha de comando e orquestração do lote
    portal_simulado/
        servidor.py         tribunal fictício com iframe, 503, sessão e PDF
    tests/
        test_cnj.py
        test_datajud.py     respostas da API simuladas, sem rede
        test_portal.py      robô real contra o portal simulado
    processos.txt           exemplo de entrada com números fictícios
    requirements.txt
```

## Como executar

1. Crie o ambiente e instale as dependências

    ```
    python -m venv .venv
    .venv\Scripts\activate          (Windows)
    source .venv/bin/activate       (Linux/macOS)
    pip install -r requirements.txt
    playwright install chromium
    ```

2. Demonstração com o portal simulado

    Em um terminal, suba o portal:

    ```
    python portal_simulado/servidor.py --taxa-falha 0.2
    ```

    Em outro terminal, rode o robô:

    ```
    python -m robo --fonte portal --entrada processos.txt
    ```

    Para ver o navegador trabalhando, acrescente `--visivel`. Para gravar um trace e inspecionar passo a passo, acrescente `--trace` e depois rode `playwright show-trace saida/trace.zip`. Você também pode abrir http://127.0.0.1:8000 no navegador (usuário `robo`, senha `teste`) para ver o portal.

    Rode o mesmo comando de novo: o robô pula os processos já concluídos. Use `--refazer` para consultar tudo outra vez.

3. Consulta real pelo DataJud

    Coloque números de processos públicos num arquivo e rode:

    ```
    python -m robo --fonte datajud --entrada meus_processos.txt
    ```

    A chave pública do CNJ já vem configurada. Se a API responder 401 ou 403, o CNJ trocou a chave: pegue a atual em https://datajud-wiki.cnj.jus.br/api-publica/acesso e defina a variável `DATAJUD_API_KEY`.

4. Testes

    ```
    pytest
    ```

    Os testes do portal sobem o servidor simulado com 30% de falha e sessão curta, então exercitam retry e relogin de verdade.

Saídas ficam na pasta `saida/`: relatório Excel, `robo.log`, PDFs, screenshots de erro e o banco de estado.

## Conceitos praticados

- Esperas por condição em vez de `sleep` fixo: o robô espera o primeiro desfecho possível (resultado, não encontrado, erro do sistema, sessão expirada) e reage a cada um
- Seletores resilientes (`get_by_label`, `get_by_role`, atributos `data-*`) em vez de XPath absoluto, que quebra na primeira mudança de layout
- `frame_locator` para formulários dentro de iframe, comum no PJe
- Separação entre erro transitório (repetir com backoff) e erro definitivo (credencial errada, tribunal sem índice: parar ou registrar sem repetir)
- Idempotência: estado gravado a cada processo, que num robô de protocolo é o que impede peticionar duas vezes
- Validar a entrada antes de gastar consultas no tribunal
- Observabilidade: log estruturado, screenshot no erro, trace e relatório de falhas
- Preferência por fonte oficial (DataJud) sobre scraping quando ela atende

## Limitações conhecidas

- O cliente DataJud foi validado com processos reais do TJRS. Os campos podem variar entre tribunais, então vale conferir uma amostra antes de rodar o lote de outro tribunal.
- O DataJud envia datas com sufixo "Z" (UTC), mas na conferência com a consulta pública do TJRS a hora já era local: o ajuizamento aparece às 10:03 nas duas fontes. Por isso o robô não converte fuso. Para outro tribunal, vale repetir a conferência.
- O DataJud pode estar desatualizado em relação ao tribunal. No processo conferido, o órgão julgador no DataJud era a vara de origem, enquanto o eproc já mostrava o processo redistribuído para um núcleo de Justiça 4.0. O DataJud serve para triagem e acompanhamento em massa; para controle de prazo, a fonte é o próprio tribunal ou a intimação.
- A API respondeu em cerca de 13 segundos por processo nos testes, com timeouts ocasionais (tratados pelo retry). Para lotes grandes, o caminho é consultar vários números por requisição, e não abrir muitas conexões em paralelo.
- O DataJud traz metadados e movimentações, não o inteiro teor, e não inclui processos em segredo de justiça.
- O robô do portal foi feito para o portal simulado. Para um tribunal real mudam os seletores, o login (muitos exigem certificado digital A1 ou A3) e pode haver captcha, que deve ser tratado como ponto de intervenção humana, não contornado.
- Execução sequencial. Paralelizar com vários contextos do Playwright é possível, mas aumenta a carga no tribunal e o risco de bloqueio.

## Autor

   Pedro Thomas...
   - LinkedIn: www.linkedin.com/in/pedro-gustavo-thomas-5935392b7
   - GitHub: https://github.com/Pglopess
