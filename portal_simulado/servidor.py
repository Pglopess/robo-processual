"""Portal de tribunal simulado, para treinar o robô sem tocar em site real.

Reproduz, de propósito, os problemas que sites de tribunal costumam ter:
    - login com sessão que expira depois de algumas consultas
    - formulário de consulta dentro de um iframe (como no PJe)
    - resposta lenta e com tempo variável
    - erro 503 "sistema indisponível" aleatório
    - resultado renderizado por JavaScript depois que a página carrega
    - inteiro teor em PDF entregue como download

Uso:
    python portal_simulado/servidor.py --porta 8000 --taxa-falha 0.2

Credenciais do portal simulado: usuário "robo", senha "teste".
Os processos são fictícios, gerados de forma determinística a partir do número.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import secrets
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USUARIO, SENHA = "robo", "teste"

CLASSES = [
    "Procedimento Comum Cível",
    "Execução de Título Extrajudicial",
    "Monitória",
    "Cumprimento de Sentença",
    "Procedimento do Juizado Especial Cível",
]
ASSUNTOS = [
    "Indenização por Dano Moral",
    "Contratos Bancários",
    "Inclusão Indevida em Cadastro de Inadimplentes",
    "Cobrança de Aluguéis",
    "Obrigação de Fazer / Não Fazer",
]
ORGAOS = ["1ª Vara Cível", "3ª Vara Cível", "5ª Vara Cível", "Juizado Especial Cível", "16ª Vara Cível"]
MOVIMENTOS = [
    "Conclusos para despacho",
    "Expedida citação",
    "Juntada de petição",
    "Audiência de conciliação designada",
    "Expedida intimação",
    "Decorrido prazo",
    "Conclusos para sentença",
    "Sentença proferida",
]


def processo_existe(digitos: str) -> bool:
    """Regra fixa para os testes saberem o resultado esperado: cerca de 1 em 7 não existe."""
    return int(hashlib.sha256(digitos.encode()).hexdigest(), 16) % 7 != 0


def dados_processo(digitos: str) -> dict:
    rng = random.Random(digitos)
    ano = int(digitos[9:13])
    inicio = datetime(ano, 1, 1) + timedelta(days=rng.randint(0, 300), hours=rng.randint(8, 17))
    movs, quando = [("Distribuído por sorteio", inicio)], inicio
    for _ in range(rng.randint(3, 9)):
        quando = (quando + timedelta(days=rng.randint(5, 60))).replace(hour=rng.randint(8, 18), minute=rng.randint(0, 59))
        movs.append((rng.choice(MOVIMENTOS), quando))
    return {
        "classe": rng.choice(CLASSES),
        "assunto": rng.choice(ASSUNTOS),
        "orgao": rng.choice(ORGAOS),
        "ajuizamento": inicio.strftime("%d/%m/%Y"),
        # o portal mostra a mais recente primeiro, como a maioria dos tribunais
        "movimentos": [(n, d.strftime("%d/%m/%Y %H:%M")) for n, d in reversed(movs)],
    }


def formatar(digitos: str) -> str:
    d = digitos
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def pdf_simples(linhas: list[str]) -> bytes:
    """PDF mínimo de uma página com texto, montado à mão para não depender de biblioteca."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    texto = "BT /F1 11 Tf 50 790 Td 16 TL " + " ".join(f"({esc(l)}) '" for l in linhas) + " ET"
    fluxo = texto.encode("cp1252", errors="replace")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream",
    ]
    saida, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objetos, start=1):
        offsets.append(len(saida))
        saida += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    inicio_xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        saida += f"{off:010d} 00000 n \n".encode()
    saida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{inicio_xref}\n%%EOF\n".encode()
    return bytes(saida)


ESTILO = """
<style>
 body{font-family:Arial,sans-serif;margin:0;background:#f2f2f2;color:#222}
 header{background:#1f3a5f;color:#fff;padding:12px 20px}
 main{padding:20px}
 form{background:#fff;padding:16px;border:1px solid #ccc;max-width:520px}
 label{display:block;margin:8px 0 4px}
 table{border-collapse:collapse;background:#fff;margin-top:12px}
 td,th{border:1px solid #ccc;padding:6px 10px;text-align:left}
 .aviso{background:#fff3cd;border:1px solid #e0c46c;padding:12px;margin-top:12px}
 .erro{background:#f8d7da;border:1px solid #d9939b;padding:12px}
 iframe{width:100%;height:640px;border:1px solid #999;background:#fff}
</style>
"""


def pagina(titulo: str, corpo: str) -> str:
    return f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><title>{titulo}</title>{ESTILO}</head><body>{corpo}</body></html>"


FORM_CONSULTA = """
<form action="/consulta/resultado" method="get">
  <label for="numero">Número do processo</label>
  <input id="numero" name="numero" size="30" placeholder="NNNNNNN-DD.AAAA.J.TR.OOOO">
  <button type="submit">Pesquisar</button>
</form>
"""


class Portal:
    def __init__(self, taxa_falha: float = 0.15, atraso_max: float = 1.5,
                 consultas_por_sessao: int = 8, semente: int | None = None):
        self.taxa_falha = taxa_falha
        self.atraso_max = atraso_max
        self.consultas_por_sessao = consultas_por_sessao
        self.rng = random.Random(semente)
        self.sessoes: dict[str, int] = {}
        self.trava = threading.Lock()

    def sorteio(self) -> tuple[float, bool, int]:
        with self.trava:
            return (self.rng.uniform(0.05, self.atraso_max),
                    self.rng.random() < self.taxa_falha,
                    self.rng.randint(200, 1200))


def criar_handler(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silencioso; o log que importa é o do robô
            pass

        # -- utilitários -------------------------------------------------------
        def _sessao(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie["sessao"].value if "sessao" in cookie else None
            return token if token in portal.sessoes else None

        def _html(self, conteudo: str, status: int = 200, extra: dict | None = None):
            dados = conteudo.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(dados)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(dados)

        def _redirecionar(self, destino: str, extra: dict | None = None):
            self.send_response(303)
            self.send_header("Location", destino)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _sessao_expirada(self):
            self._html(pagina("Sessão expirada",
                              "<main><p id='sessao-expirada' class='erro'>Sua sessão expirou. Faça login novamente.</p></main>"))

        # -- rotas -------------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            rota = url.path
            if rota == "/":
                return self._redirecionar("/login")
            if rota == "/login":
                return self._html(pagina("Login", self._form_login()))
            if rota == "/painel":
                if not self._sessao():
                    return self._redirecionar("/login")
                return self._html(pagina("Painel", """
                    <header><strong>Tribunal de Justiça Simulado</strong> &middot; Consulta processual</header>
                    <main><iframe id="frame-consulta" title="Consulta processual" src="/consulta"></iframe></main>"""))
            if rota == "/consulta":
                if not self._sessao():
                    return self._sessao_expirada()
                return self._html(pagina("Consulta", f"<main>{FORM_CONSULTA}</main>"))
            if rota == "/consulta/resultado":
                return self._resultado(parse_qs(url.query).get("numero", [""])[0])
            if rota.startswith("/documentos/") and rota.endswith(".pdf"):
                return self._documento(rota.removeprefix("/documentos/").removesuffix(".pdf"))
            self._html(pagina("Não encontrado", "<p>Página não encontrada</p>"), 404)

        def do_POST(self):
            if urlparse(self.path).path != "/login":
                return self._html("", 405)
            tamanho = int(self.headers.get("Content-Length", 0))
            campos = parse_qs(self.rfile.read(tamanho).decode())
            if campos.get("usuario", [""])[0] == USUARIO and campos.get("senha", [""])[0] == SENHA:
                token = secrets.token_hex(16)
                with portal.trava:
                    portal.sessoes[token] = 0
                return self._redirecionar("/painel", {"Set-Cookie": f"sessao={token}; Path=/; HttpOnly"})
            self._html(pagina("Login", self._form_login("<p class='erro'>Usuário ou senha inválidos.</p>")), 401)

        def _form_login(self, aviso: str = "") -> str:
            return f"""
            <header><strong>Tribunal de Justiça Simulado</strong></header>
            <main>{aviso}
              <form action="/login" method="post">
                <label for="usuario">Usuário</label><input id="usuario" name="usuario">
                <label for="senha">Senha</label><input id="senha" name="senha" type="password">
                <button type="submit">Entrar</button>
              </form>
            </main>"""

        def _resultado(self, numero: str):
            token = self._sessao()
            if not token:
                return self._sessao_expirada()
            with portal.trava:
                portal.sessoes[token] += 1
                expirou = portal.sessoes[token] > portal.consultas_por_sessao
                if expirou:
                    del portal.sessoes[token]
            if expirou:
                return self._sessao_expirada()

            atraso, falhar, atraso_js = portal.sorteio()
            time.sleep(atraso)
            if falhar:
                return self._html(pagina("Indisponível",
                    "<main><p id='erro-sistema' class='erro'>Sistema temporariamente indisponível. Tente mais tarde.</p></main>"), 503)

            digitos = "".join(c for c in numero if c.isdigit())
            if len(digitos) != 20 or not processo_existe(digitos):
                return self._html(pagina("Resultado", f"""<main>{FORM_CONSULTA}
                    <p id='nao-encontrado' class='aviso'>Nenhum processo encontrado para {html.escape(numero)}.</p></main>"""))

            # Os dados chegam num JSON e só viram tabela depois de um atraso no navegador,
            # como em portais feitos com JavaScript. Um robô que lê a página cedo demais pega vazio.
            dados = dados_processo(digitos)
            dados["numero"] = formatar(digitos)
            dados["pdf"] = f"/documentos/{digitos}.pdf"
            self._html(pagina("Resultado", f"""<main>{FORM_CONSULTA}
                <p id="carregando">Carregando dados do processo...</p>
                <div id="area"></div></main>
                <script>
                const d = {json.dumps(dados, ensure_ascii=False)};
                setTimeout(() => {{
                  const linhas = d.movimentos.map(m => `<tr><td>${{m[1]}}</td><td>${{m[0]}}</td></tr>`).join("");
                  document.getElementById("area").innerHTML = `
                    <section id="resultado" aria-label="Dados do processo">
                      <h2>Processo <span data-campo="numero">${{d.numero}}</span></h2>
                      <table>
                        <tr><th>Classe</th><td data-campo="classe">${{d.classe}}</td></tr>
                        <tr><th>Assunto</th><td data-campo="assunto">${{d.assunto}}</td></tr>
                        <tr><th>Órgão julgador</th><td data-campo="orgao">${{d.orgao}}</td></tr>
                        <tr><th>Data de ajuizamento</th><td data-campo="ajuizamento">${{d.ajuizamento}}</td></tr>
                      </table>
                      <h3>Movimentações</h3>
                      <table id="movimentacoes"><thead><tr><th>Data</th><th>Movimento</th></tr></thead>
                        <tbody>${{linhas}}</tbody></table>
                      <p><a href="${{d.pdf}}">Baixar inteiro teor (PDF)</a></p>
                    </section>`;
                  document.getElementById("carregando").remove();
                }}, {atraso_js});
                </script>"""))

        def _documento(self, digitos: str):
            if not self._sessao():
                return self._sessao_expirada()
            if not (digitos.isdigit() and len(digitos) == 20 and processo_existe(digitos)):
                return self._html(pagina("Não encontrado", "<p>Documento não encontrado</p>"), 404)
            d = dados_processo(digitos)
            linhas = [
                "TRIBUNAL DE JUSTIÇA SIMULADO (documento fictício para testes)",
                f"Processo {formatar(digitos)}",
                f"Classe: {d['classe']}",
                f"Assunto: {d['assunto']}",
                f"Órgão julgador: {d['orgao']}",
                "",
                "Movimentações:",
            ] + [f"  {data}  {nome}" for nome, data in d["movimentos"]]
            corpo = pdf_simples(linhas)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{digitos}.pdf"')
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    return Handler


def iniciar(porta: int = 8000, **opcoes) -> ThreadingHTTPServer:
    """Sobe o portal numa thread em segundo plano (usado pelos testes). Porta 0 = porta livre."""
    servidor = ThreadingHTTPServer(("127.0.0.1", porta), criar_handler(Portal(**opcoes)))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def main():
    p = argparse.ArgumentParser(description="Portal de tribunal simulado")
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--taxa-falha", type=float, default=0.15, help="chance de erro 503 por consulta (0 a 1)")
    p.add_argument("--atraso-max", type=float, default=1.5, help="atraso máximo do servidor, em segundos")
    p.add_argument("--consultas-por-sessao", type=int, default=8)
    a = p.parse_args()
    servidor = ThreadingHTTPServer(("127.0.0.1", a.porta), criar_handler(
        Portal(a.taxa_falha, a.atraso_max, a.consultas_por_sessao)))
    print(f"Portal simulado em http://127.0.0.1:{a.porta}  (usuário: {USUARIO} / senha: {SENHA})  Ctrl+C para sair")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
