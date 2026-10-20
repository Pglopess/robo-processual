"""Robô Playwright para consulta processual num portal de tribunal.

Aponta para o portal simulado (portal_simulado/servidor.py). Para um tribunal real, o que muda
são os seletores e o fluxo de login; a estrutura de retry, evidências e retomada continua igual.
Antes de apontar para um site real, confira os termos de uso e prefira a API oficial (datajud.py).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .cnj import NumeroCNJ

log = logging.getLogger(__name__)


class PortalIndisponivel(RuntimeError):
    """O portal falhou em todas as tentativas."""


class ErroLogin(RuntimeError):
    """Credencial recusada. Repetir não resolve e pode bloquear o usuário no tribunal."""


class SessaoExpirada(RuntimeError):
    pass


class _ErroSistema(RuntimeError):
    pass


def _iso(data_br: str) -> str:
    """'05/03/2024 14:30' -> '2024-03-05 14:30' (ordenável e sem ambiguidade no Excel)."""
    for formato in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_br.strip(), formato).strftime(
                "%Y-%m-%d %H:%M" if " " in data_br.strip() else "%Y-%m-%d")
        except ValueError:
            continue
    return data_br.strip()


class PortalTribunal:
    def __init__(self, url_base: str, usuario: str, senha: str, pasta_saida: Path,
                 headless: bool = True, tentativas: int = 4, espera_base: float = 1.0,
                 timeout_ms: int = 15_000, baixar_pdf: bool = True, trace: bool = False):
        self.url_base = url_base.rstrip("/")
        self.usuario, self.senha = usuario, senha
        self.pasta_pdfs = pasta_saida / "pdfs"
        self.pasta_erros = pasta_saida / "erros"
        self.arquivo_sessao = pasta_saida / "sessao.json"
        self.arquivo_trace = pasta_saida / "trace.zip"
        self.headless, self.tentativas, self.espera_base = headless, tentativas, espera_base
        self.timeout_ms, self.baixar_pdf, self.trace = timeout_ms, baixar_pdf, trace
        self.logado = False

    # -- ciclo de vida -------------------------------------------------------
    def __enter__(self) -> "PortalTribunal":
        self.pasta_pdfs.mkdir(parents=True, exist_ok=True)
        self.pasta_erros.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        # Reaproveita a sessão salva da execução anterior: evita logar a cada rodada.
        estado = str(self.arquivo_sessao) if self.arquivo_sessao.exists() else None
        self._context = self._browser.new_context(accept_downloads=True, storage_state=estado, locale="pt-BR")
        self._context.set_default_timeout(self.timeout_ms)
        if self.trace:
            self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.trace:
                self._context.tracing.stop(path=str(self.arquivo_trace))
                log.info("trace salvo em %s (abra com: playwright show-trace %s)", self.arquivo_trace, self.arquivo_trace)
            self._context.close()
            self._browser.close()
        finally:
            self._pw.stop()

    # -- login ---------------------------------------------------------------
    def _login(self) -> None:
        log.info("fazendo login no portal")
        self.page.goto(f"{self.url_base}/login")
        self.page.get_by_label("Usuário").fill(self.usuario)
        self.page.get_by_label("Senha").fill(self.senha)
        with self.page.expect_navigation():
            self.page.get_by_role("button", name="Entrar").click()
        if "/painel" not in self.page.url:
            # Credencial errada não se resolve com retry: falha logo, com mensagem clara.
            raise ErroLogin("login recusado pelo portal: confira PORTAL_USUARIO e PORTAL_SENHA")
        self._context.storage_state(path=str(self.arquivo_sessao))
        self.logado = True

    def _abrir_painel(self) -> None:
        self.page.goto(f"{self.url_base}/painel")
        if "/login" in self.page.url:
            self._login()

    # -- consulta ------------------------------------------------------------
    def consultar(self, numero: NumeroCNJ) -> dict | None:
        """Dados do processo, ou None se o portal disser que não existe."""
        ultimo_erro = ""
        for tentativa in range(1, self.tentativas + 1):
            try:
                return self._consultar_uma_vez(numero)
            except SessaoExpirada:
                ultimo_erro = "sessão expirada"
                log.info("processo=%s sessão expirou, refazendo login", numero)
                self._login()
                continue  # relogar não é falha do portal: tenta de novo sem esperar
            except (_ErroSistema, PlaywrightTimeout, PlaywrightError) as e:
                ultimo_erro = f"{type(e).__name__}: {str(e).splitlines()[0]}"
                self._evidencia(numero, tentativa)

            if tentativa < self.tentativas:
                espera = self.espera_base * 2 ** (tentativa - 1)
                log.warning("processo=%s tentativa=%d/%d erro=%s nova_tentativa_em=%.1fs",
                            numero, tentativa, self.tentativas, ultimo_erro, espera)
                time.sleep(espera)

        raise PortalIndisponivel(f"falhou após {self.tentativas} tentativas: {ultimo_erro}")

    def _consultar_uma_vez(self, numero: NumeroCNJ) -> dict | None:
        self._abrir_painel()

        # O formulário está dentro de um iframe: sem frame_locator o robô não "enxerga" o campo.
        frame = self.page.frame_locator("#frame-consulta")
        campo = frame.get_by_label("Número do processo")
        expirada = frame.locator("#sessao-expirada")
        campo.or_(expirada).first.wait_for()
        if expirada.count():
            raise SessaoExpirada()
        campo.fill(numero.formatado)
        frame.get_by_role("button", name="Pesquisar").click()

        # Espera o primeiro desfecho possível em vez de um sleep fixo.
        # O portal mostra "Carregando..." antes da tabela; esperar só o load da página pegaria vazio.
        desfecho = frame.locator("#resultado, #nao-encontrado, #erro-sistema, #sessao-expirada").first
        desfecho.wait_for(state="visible")
        tipo = desfecho.get_attribute("id")

        if tipo == "sessao-expirada":
            raise SessaoExpirada()
        if tipo == "erro-sistema":
            raise _ErroSistema(desfecho.inner_text())
        if tipo == "nao-encontrado":
            return None

        # Confere se a página carregada é mesmo do processo pedido.
        # Um robô que extrai o processo errado é pior que um robô que falha.
        exibido = frame.locator('[data-campo="numero"]').inner_text().strip()
        if exibido != numero.formatado:
            raise _ErroSistema(f"portal exibiu {exibido}, esperado {numero.formatado}")

        def campo_txt(nome: str) -> str:
            return frame.locator(f'[data-campo="{nome}"]').inner_text().strip()

        movimentos = []
        for linha in frame.locator("#movimentacoes tbody tr").all():
            data, nome = (c.strip() for c in linha.locator("td").all_inner_texts())
            movimentos.append((_iso(data), nome))
        movimentos.sort()

        documento = None
        if self.baixar_pdf:
            destino = self.pasta_pdfs / f"{numero.digitos}.pdf"
            with self.page.expect_download() as info:
                frame.get_by_role("link", name="Baixar inteiro teor").click()
            info.value.save_as(destino)
            documento = str(destino)

        return {
            "tribunal": "TJ Simulado",
            "grau": "G1",
            "classe": campo_txt("classe"),
            "orgao_julgador": campo_txt("orgao"),
            "data_ajuizamento": _iso(campo_txt("ajuizamento")),
            "assuntos": campo_txt("assunto"),
            "qtd_movimentos": len(movimentos),
            "ultima_movimentacao": movimentos[-1][1] if movimentos else None,
            "data_ultima_movimentacao": movimentos[-1][0] if movimentos else None,
            "documento": documento,
        }

    def _evidencia(self, numero: NumeroCNJ, tentativa: int) -> None:
        """Screenshot no erro: é o que permite investigar uma falha da madrugada."""
        caminho = self.pasta_erros / f"{numero.digitos}_tentativa{tentativa}_{datetime.now():%H%M%S}.png"
        try:
            self.page.screenshot(path=str(caminho), full_page=True)
        except PlaywrightError:
            log.debug("não foi possível salvar screenshot de %s", numero)
