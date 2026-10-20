"""Cliente da API Pública do DataJud (CNJ).

Preferir a API oficial ao scraping sempre que ela atender: é estável, não depende de layout
de tela, não tem captcha e está dentro das regras de uso do Judiciário. Limitação: traz
metadados e movimentações, mas não o inteiro teor dos documentos nem processos em segredo.

Documentação: https://datajud-wiki.cnj.jus.br/api-publica/
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable

import requests

from .cnj import NumeroCNJ

log = logging.getLogger(__name__)

URL_BASE = "https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search"

# Chave pública divulgada pelo CNJ na wiki do DataJud. O CNJ pode trocá-la a qualquer momento.
# Se a API responder 401/403, pegue a atual em https://datajud-wiki.cnj.jus.br/api-publica/acesso
# e defina a variável de ambiente DATAJUD_API_KEY.
CHAVE_PUBLICA_PADRAO = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="

STATUS_TRANSITORIOS = {429, 500, 502, 503, 504}


class ErroAutenticacao(RuntimeError):
    """Chave recusada. Não adianta repetir: o lote inteiro vai falhar igual."""


class ErroTransitorio(RuntimeError):
    """Falha temporária que persistiu depois de todas as tentativas."""


class TribunalNaoSuportado(ValueError):
    """O número é válido, mas o tribunal não tem índice mapeado no DataJud."""


class DataJudClient:
    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        tentativas: int = 4,
        espera_base: float = 2.0,
        dormir: Callable[[float], None] = time.sleep,
    ):
        chave = api_key or os.environ.get("DATAJUD_API_KEY") or CHAVE_PUBLICA_PADRAO
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"APIKey {chave}", "Content-Type": "application/json"})
        self.timeout = timeout
        self.tentativas = tentativas
        self.espera_base = espera_base
        self.dormir = dormir  # injetável para os testes não esperarem de verdade

    # Interface comum com o PortalTribunal: o main.py usa as duas fontes do mesmo jeito.
    def __enter__(self) -> "DataJudClient":
        return self

    def __exit__(self, *exc) -> None:
        self.session.close()

    def consultar(self, numero: NumeroCNJ) -> dict | None:
        fontes = self.buscar(numero)
        return resumir(fontes) if fontes else None

    def buscar(self, numero: NumeroCNJ) -> list[dict]:
        """Retorna os registros (_source) do processo. Lista vazia se não encontrado.

        Um mesmo processo pode vir em mais de um registro (um por grau ou órgão julgador).
        """
        alias = numero.alias_datajud
        if alias is None:
            raise TribunalNaoSuportado(f"segmento {numero.segmento}, tribunal {numero.tribunal} sem índice no DataJud")

        url = URL_BASE.format(alias=alias)
        corpo = {"query": {"match": {"numeroProcesso": numero.digitos}}, "size": 10}
        ultimo_erro = ""

        for tentativa in range(1, self.tentativas + 1):
            try:
                resp = self.session.post(url, json=corpo, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                ultimo_erro = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code in (401, 403):
                    raise ErroAutenticacao(f"HTTP {resp.status_code}: chave do DataJud recusada")
                if resp.status_code in STATUS_TRANSITORIOS:
                    ultimo_erro = f"HTTP {resp.status_code}"
                else:
                    resp.raise_for_status()  # 4xx restantes são erro do nosso lado: não repetir
                    hits = resp.json().get("hits", {}).get("hits", [])
                    return [h.get("_source", {}) for h in hits]

            if tentativa < self.tentativas:
                espera = self.espera_base * 2 ** (tentativa - 1)  # backoff exponencial: 2s, 4s, 8s...
                log.warning("processo=%s tentativa=%d/%d erro=%s nova_tentativa_em=%.0fs",
                            numero.formatado, tentativa, self.tentativas, ultimo_erro, espera)
                self.dormir(espera)

        raise ErroTransitorio(f"DataJud indisponível após {self.tentativas} tentativas: {ultimo_erro}")


def _data(valor: str | None) -> str | None:
    """Normaliza datas do DataJud, que aparecem como ISO 8601 ou como AAAAMMDDhhmmss."""
    if not valor:
        return None
    texto = str(valor)
    try:
        if texto.isdigit() and len(texto) == 14:
            dt = datetime.strptime(texto, "%Y%m%d%H%M%S")
        else:
            dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return texto


def _nomes_assuntos(assuntos: list) -> list[str]:
    # Em alguns tribunais "assuntos" vem com listas aninhadas.
    nomes = []
    for item in assuntos or []:
        if isinstance(item, list):
            nomes.extend(_nomes_assuntos(item))
        elif isinstance(item, dict) and item.get("nome"):
            nomes.append(item["nome"])
    return nomes


def resumir(fontes: list[dict]) -> dict:
    """Reduz os registros do DataJud às colunas do relatório."""
    principal = max(fontes, key=lambda f: f.get("dataHoraUltimaAtualizacao") or "")
    movimentos = [m for f in fontes for m in (f.get("movimentos") or [])]
    movimentos.sort(key=lambda m: _data(m.get("dataHora")) or "")
    ultimo = movimentos[-1] if movimentos else {}

    return {
        "tribunal": principal.get("tribunal"),
        "grau": ", ".join(sorted({f["grau"] for f in fontes if f.get("grau")})),
        "classe": (principal.get("classe") or {}).get("nome"),
        "orgao_julgador": (principal.get("orgaoJulgador") or {}).get("nome"),
        "data_ajuizamento": (_data(principal.get("dataAjuizamento")) or "")[:10] or None,  # só a data
        "assuntos": "; ".join(_nomes_assuntos(principal.get("assuntos"))),
        "qtd_movimentos": len(movimentos),
        "ultima_movimentacao": ultimo.get("nome"),
        "data_ultima_movimentacao": _data(ultimo.get("dataHora")),
        "documento": None,
    }
