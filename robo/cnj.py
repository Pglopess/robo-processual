"""Validação e parsing de números de processo no padrão CNJ (Resolução CNJ nº 65/2008).

Formato: NNNNNNN-DD.AAAA.J.TR.OOOO
    NNNNNNN  número sequencial do processo na origem
    DD       dígito verificador (módulo 97, ISO 7064)
    AAAA     ano de ajuizamento
    J        segmento do Judiciário (8 = Justiça Estadual, 5 = Trabalho, 4 = Federal...)
    TR       tribunal dentro do segmento (na Justiça Estadual, 21 = TJRS, 26 = TJSP...)
    OOOO     unidade de origem (vara, foro)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PADRAO = re.compile(r"^(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})$")

# Justiça Estadual (J=8): código TR -> UF do Tribunal de Justiça
_UF_POR_TR = {
    "01": "ac", "02": "al", "03": "ap", "04": "am", "05": "ba", "06": "ce",
    "07": "df", "08": "es", "09": "go", "10": "ma", "11": "mt", "12": "ms",
    "13": "mg", "14": "pa", "15": "pb", "16": "pr", "17": "pe", "18": "pi",
    "19": "rj", "20": "rn", "21": "rs", "22": "ro", "23": "rr", "24": "sc",
    "25": "se", "26": "sp", "27": "to",
}


class NumeroInvalido(ValueError):
    """O texto não é um número CNJ válido (formato ou dígito verificador)."""


def calcular_dv(sequencial: str, ano: str, segmento: str, tribunal: str, origem: str) -> str:
    """Calcula o dígito verificador pelo módulo 97, como define o anexo da Res. 65/2008."""
    resto = int(f"{sequencial}{ano}{segmento}{tribunal}{origem}00") % 97
    return f"{98 - resto:02d}"


@dataclass(frozen=True)
class NumeroCNJ:
    sequencial: str
    dv: str
    ano: str
    segmento: str
    tribunal: str
    origem: str

    @property
    def digitos(self) -> str:
        """Os 20 dígitos sem pontuação (é assim que o DataJud indexa o processo)."""
        return f"{self.sequencial}{self.dv}{self.ano}{self.segmento}{self.tribunal}{self.origem}"

    @property
    def formatado(self) -> str:
        return f"{self.sequencial}-{self.dv}.{self.ano}.{self.segmento}.{self.tribunal}.{self.origem}"

    @property
    def alias_datajud(self) -> str | None:
        """Nome do índice do tribunal na API do DataJud (ex.: tjrs, trt4). None se não mapeado."""
        if self.segmento == "8":
            uf = _UF_POR_TR.get(self.tribunal)
            if uf is None:
                return None
            return "tjdft" if uf == "df" else f"tj{uf}"
        if self.segmento == "5" and 1 <= int(self.tribunal) <= 24:
            return f"trt{int(self.tribunal)}"
        if self.segmento == "4" and 1 <= int(self.tribunal) <= 6:
            return f"trf{int(self.tribunal)}"
        if self.segmento == "3":
            return "stj"
        return None

    def __str__(self) -> str:
        return self.formatado


def parse(texto: str) -> NumeroCNJ:
    """Converte texto (com ou sem pontuação) em NumeroCNJ, validando formato e dígito verificador."""
    limpo = re.sub(r"\s+", "", texto or "")
    m = _PADRAO.match(limpo)
    if not m:
        raise NumeroInvalido(f"formato inválido: {texto!r}")
    seq, dv, ano, seg, tr, origem = m.groups()
    esperado = calcular_dv(seq, ano, seg, tr, origem)
    if dv != esperado:
        raise NumeroInvalido(f"dígito verificador {dv} não confere (esperado {esperado})")
    return NumeroCNJ(seq, dv, ano, seg, tr, origem)


def gerar(sequencial: int, ano: int, segmento: str = "8", tribunal: str = "21", origem: str = "0001") -> NumeroCNJ:
    """Monta um número válido. Útil para testes e dados sintéticos."""
    seq, a = f"{sequencial:07d}", f"{ano:04d}"
    return NumeroCNJ(seq, calcular_dv(seq, a, segmento, tribunal, origem), a, segmento, tribunal, origem)
