"""Testes do cliente DataJud com respostas simuladas (sem rede)."""

import pytest
import requests

from robo import cnj
from robo.datajud import (DataJudClient, ErroAutenticacao, ErroTransitorio,
                          TribunalNaoSuportado, resumir)

RESPOSTA = {
    "hits": {"hits": [
        {"_source": {
            "tribunal": "TJRS", "grau": "G1", "numeroProcesso": "00012341820248210001",
            "classe": {"codigo": 7, "nome": "Procedimento Comum Cível"},
            "orgaoJulgador": {"nome": "3ª Vara Cível do Foro Central"},
            "dataAjuizamento": "20240305000000",
            "dataHoraUltimaAtualizacao": "2024-09-01T10:00:00.000Z",
            "assuntos": [[{"nome": "Indenização por Dano Moral"}], {"nome": "Contratos Bancários"}],
            "movimentos": [
                {"nome": "Distribuição", "dataHora": "2024-03-05T09:00:00.000Z"},
                {"nome": "Conclusão", "dataHora": "2024-06-10T14:00:00.000Z"},
            ],
        }},
        {"_source": {
            "tribunal": "TJRS", "grau": "G2",
            "dataHoraUltimaAtualizacao": "2024-08-01T10:00:00.000Z",
            "movimentos": [{"nome": "Recebido o recurso", "dataHora": "2024-07-20T11:30:00.000Z"}],
        }},
    ]}
}


class RespostaFalsa:
    def __init__(self, status, corpo=None):
        self.status_code, self._corpo = status, corpo or {}

    def json(self):
        return self._corpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class SessaoFalsa(requests.Session):
    """Devolve as respostas da fila, na ordem, e guarda as chamadas feitas."""

    def __init__(self, respostas):
        super().__init__()
        self.respostas, self.chamadas = list(respostas), []

    def post(self, url, **kw):
        self.chamadas.append((url, kw))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def cliente(respostas):
    esperas = []
    c = DataJudClient(api_key="CHAVE", session=SessaoFalsa(respostas), dormir=esperas.append)
    return c, esperas


NUMERO = cnj.parse("0001234-18.2024.8.21.0001")


def test_consulta_monta_requisicao_certa():
    c, _ = cliente([RespostaFalsa(200, RESPOSTA)])
    c.consultar(NUMERO)
    url, kw = c.session.chamadas[0]
    assert url.endswith("/api_publica_tjrs/_search")
    assert kw["json"]["query"]["match"]["numeroProcesso"] == "00012341820248210001"
    assert c.session.headers["Authorization"] == "APIKey CHAVE"


def test_resumo_junta_graus_e_pega_ultima_movimentacao():
    r = resumir([h["_source"] for h in RESPOSTA["hits"]["hits"]])
    assert r["grau"] == "G1, G2"
    assert r["classe"] == "Procedimento Comum Cível"
    assert r["data_ajuizamento"] == "2024-03-05"
    assert r["assuntos"] == "Indenização por Dano Moral; Contratos Bancários"
    assert r["qtd_movimentos"] == 3
    assert r["ultima_movimentacao"] == "Recebido o recurso"


def test_nao_encontrado_retorna_none():
    c, _ = cliente([RespostaFalsa(200, {"hits": {"hits": []}})])
    assert c.consultar(NUMERO) is None


def test_retry_com_backoff_em_erro_transitorio():
    c, esperas = cliente([RespostaFalsa(503), requests.ConnectionError("caiu"), RespostaFalsa(200, RESPOSTA)])
    assert c.consultar(NUMERO)["classe"] == "Procedimento Comum Cível"
    assert esperas == [2.0, 4.0]


def test_desiste_depois_das_tentativas():
    c, esperas = cliente([RespostaFalsa(503)] * 4)
    with pytest.raises(ErroTransitorio):
        c.consultar(NUMERO)
    assert len(esperas) == 3


def test_chave_recusada_nao_repete():
    c, esperas = cliente([RespostaFalsa(401)])
    with pytest.raises(ErroAutenticacao):
        c.consultar(NUMERO)
    assert esperas == []


def test_tribunal_sem_indice():
    c, _ = cliente([])
    with pytest.raises(TribunalNaoSuportado):
        c.consultar(cnj.gerar(1, 2024, "6", "21"))
