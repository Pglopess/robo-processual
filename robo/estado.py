"""Estado persistente da execução (SQLite).

Garante idempotência: cada processo é gravado assim que termina, então se o robô cair
no meio do lote, rodar de novo continua de onde parou em vez de repetir o que já foi feito.
Num robô de protocolo isso é o que impede de peticionar duas vezes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

CONCLUIDOS = ("ok", "nao_encontrado")


class Estado:
    def __init__(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(caminho)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processos (
                chave         TEXT NOT NULL,
                fonte         TEXT NOT NULL,
                numero        TEXT NOT NULL,
                status        TEXT NOT NULL,
                dados         TEXT,
                erro          TEXT,
                tentativas    INTEGER NOT NULL DEFAULT 0,
                atualizado_em TEXT NOT NULL,
                PRIMARY KEY (chave, fonte)
            )
            """
        )
        self.conn.commit()

    def ja_concluido(self, chave: str, fonte: str) -> bool:
        linha = self.conn.execute(
            "SELECT status FROM processos WHERE chave = ? AND fonte = ?", (chave, fonte)
        ).fetchone()
        return linha is not None and linha["status"] in CONCLUIDOS

    def registrar(self, chave: str, fonte: str, numero: str, status: str,
                  dados: dict | None = None, erro: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO processos (chave, fonte, numero, status, dados, erro, tentativas, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (chave, fonte) DO UPDATE SET
                numero = excluded.numero, status = excluded.status, dados = excluded.dados,
                erro = excluded.erro, tentativas = processos.tentativas + 1,
                atualizado_em = excluded.atualizado_em
            """,
            (chave, fonte, numero, status, json.dumps(dados, ensure_ascii=False) if dados else None,
             erro, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()  # commit por processo: é o que torna a retomada segura

    def linhas(self, chaves: list[str], fonte: str) -> list[dict]:
        """Linhas das chaves pedidas, na ordem da planilha de entrada."""
        resultado = []
        for chave in chaves:
            linha = self.conn.execute(
                "SELECT * FROM processos WHERE chave = ? AND fonte = ?", (chave, fonte)
            ).fetchone()
            if linha is not None:
                item = dict(linha)
                item["dados"] = json.loads(item["dados"]) if item["dados"] else {}
                resultado.append(item)
        return resultado

    def fechar(self) -> None:
        self.conn.close()
