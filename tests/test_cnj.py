import pytest

from robo import cnj


def test_numero_valido_com_e_sem_pontuacao():
    a = cnj.parse("0001234-18.2024.8.21.0001")
    b = cnj.parse("00012341820248210001")
    assert a == b
    assert a.digitos == "00012341820248210001"
    assert a.formatado == "0001234-18.2024.8.21.0001"


def test_ignora_espacos():
    assert cnj.parse("  0001234-18.2024.8.21.0001 ").digitos == "00012341820248210001"


def test_digito_verificador_errado():
    with pytest.raises(cnj.NumeroInvalido, match="dígito verificador"):
        cnj.parse("0001234-99.2024.8.21.0001")


@pytest.mark.parametrize("texto", ["", "abc", "123456-78.2024.8.21", "0001234-18.2024.8.21.00011"])
def test_formato_invalido(texto):
    with pytest.raises(cnj.NumeroInvalido, match="formato"):
        cnj.parse(texto)


def test_gerar_sempre_produz_numero_valido():
    for seq in range(1, 500, 7):
        n = cnj.gerar(seq, 2024)
        assert cnj.parse(n.formatado) == n


def test_dv_de_um_numero_conhecido():
    # Exemplo publicado em fórum por quem usa a API do DataJud (TJMG).
    assert cnj.parse("1366828-41.2021.8.13.0024").alias_datajud == "tjmg"


@pytest.mark.parametrize("seg,tr,esperado", [
    ("8", "21", "tjrs"), ("8", "26", "tjsp"), ("8", "07", "tjdft"),
    ("5", "04", "trt4"), ("4", "04", "trf4"), ("3", "00", "stj"),
    ("8", "99", None), ("6", "21", None),
])
def test_alias_datajud(seg, tr, esperado):
    assert cnj.gerar(1, 2024, seg, tr).alias_datajud == esperado
