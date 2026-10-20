"""Ponto de entrada do robô.

Exemplos:
    python -m robo --fonte portal --entrada processos.txt
    python -m robo --fonte datajud --entrada meus_processos.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import cnj, relatorio
from .datajud import DataJudClient, ErroAutenticacao, TribunalNaoSuportado
from .estado import Estado
from .portal import ErroLogin, PortalTribunal

log = logging.getLogger("robo")


def configurar_log(pasta: Path, verboso: bool) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    raiz = logging.getLogger()
    for antigo in raiz.handlers[:]:
        antigo.close()
        raiz.removeHandler(antigo)
    raiz.setLevel(logging.DEBUG if verboso else logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(pasta / "robo.log", encoding="utf-8")):
        handler.setFormatter(formato)
        raiz.addHandler(handler)


def ler_entrada(caminho: Path) -> list[str]:
    """Um número por linha. Linhas vazias e linhas começando com # são ignoradas."""
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in linhas if l.strip() and not l.strip().startswith("#")]


def criar_fonte(args):
    if args.fonte == "datajud":
        return DataJudClient()
    return PortalTribunal(
        url_base=args.portal_url,
        usuario=os.environ.get("PORTAL_USUARIO", "robo"),
        senha=os.environ.get("PORTAL_SENHA", "teste"),
        pasta_saida=args.saida,
        headless=not args.visivel,
        baixar_pdf=not args.sem_pdf,
        trace=args.trace,
    )


def executar(args) -> int:
    configurar_log(args.saida, args.verboso)
    estado = Estado(args.saida / "estado.sqlite")
    fonte = args.fonte

    # 1) Valida tudo antes de abrir o navegador: número inválido não gasta tentativa no portal.
    chaves, pendentes, vistos = [], [], set()
    for bruto in ler_entrada(args.entrada):
        try:
            numero = cnj.parse(bruto)
        except cnj.NumeroInvalido as e:
            estado.registrar(bruto, fonte, bruto, "numero_invalido", erro=str(e))
            chaves.append(bruto)
            log.warning("entrada=%r status=numero_invalido motivo=%s", bruto, e)
            continue
        if numero.digitos in vistos:
            log.info("processo=%s duplicado na entrada, ignorado", numero)
            continue
        vistos.add(numero.digitos)
        chaves.append(numero.digitos)
        if not args.refazer and estado.ja_concluido(numero.digitos, fonte):
            log.info("processo=%s já concluído em execução anterior, pulando", numero)
            continue
        pendentes.append(numero)

    if args.limite:
        pendentes = pendentes[: args.limite]
    log.info("fonte=%s entrada=%d validos=%d pendentes=%d", fonte, len(chaves), len(vistos), len(pendentes))

    # 2) Processa um por vez. A falha de um processo nunca derruba o lote.
    abortado = False
    if pendentes:
        with criar_fonte(args) as consultor:
            for i, numero in enumerate(pendentes, start=1):
                try:
                    dados = consultor.consultar(numero)
                    status = "ok" if dados else "nao_encontrado"
                    estado.registrar(numero.digitos, fonte, numero.formatado, status, dados)
                    log.info("[%d/%d] processo=%s status=%s", i, len(pendentes), numero, status)
                except TribunalNaoSuportado as e:
                    estado.registrar(numero.digitos, fonte, numero.formatado, "tribunal_nao_suportado", erro=str(e))
                    log.warning("[%d/%d] processo=%s status=tribunal_nao_suportado", i, len(pendentes), numero)
                except (ErroAutenticacao, ErroLogin) as e:
                    # Erro de credencial vale para o lote todo: parar e avisar é melhor que
                    # gerar centenas de falhas iguais (ou bloquear o usuário no tribunal).
                    estado.registrar(numero.digitos, fonte, numero.formatado, "falha", erro=str(e))
                    log.critical("abortando o lote: %s", e)
                    abortado = True
                    break
                except Exception as e:  # noqa: BLE001 - qualquer outro erro vira falha registrada
                    estado.registrar(numero.digitos, fonte, numero.formatado, "falha", erro=f"{type(e).__name__}: {e}")
                    log.error("[%d/%d] processo=%s status=falha erro=%s", i, len(pendentes), numero, e)

    # 3) Relatório com todos os processos da entrada, inclusive os concluídos em rodadas anteriores.
    linhas = estado.linhas(chaves, fonte)
    caminho = relatorio.gerar(linhas, args.saida / f"relatorio_{fonte}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    estado.fechar()

    contagem: dict[str, int] = {}
    for linha in linhas:
        contagem[linha["status"]] = contagem.get(linha["status"], 0) + 1
    log.info("resumo %s", " ".join(f"{k}={v}" for k, v in sorted(contagem.items())))
    log.info("relatório: %s", caminho)

    # Código de saída diferente de zero quando algo falhou: o agendador (cron, Task Scheduler)
    # consegue disparar alerta sem ninguém precisar ler o log.
    return 2 if abortado else (1 if contagem.get("falha") else 0)


def criar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="robo", description="Robô de consulta processual (CNJ, DataJud e portal web)")
    p.add_argument("--fonte", choices=["portal", "datajud"], default="portal")
    p.add_argument("--entrada", type=Path, default=Path("processos.txt"), help="arquivo com um número CNJ por linha")
    p.add_argument("--saida", type=Path, default=Path("saida"), help="pasta de relatório, log, PDFs e estado")
    p.add_argument("--portal-url", default=os.environ.get("PORTAL_URL", "http://127.0.0.1:8000"))
    p.add_argument("--refazer", action="store_true", help="consulta de novo mesmo os já concluídos")
    p.add_argument("--limite", type=int, help="processa no máximo N pendentes (útil para testar)")
    p.add_argument("--visivel", action="store_true", help="abre o navegador na tela em vez de headless")
    p.add_argument("--sem-pdf", action="store_true", help="não baixa o inteiro teor")
    p.add_argument("--trace", action="store_true", help="grava trace do Playwright em saida/trace.zip")
    p.add_argument("--verboso", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    return executar(criar_parser().parse_args(argv))
