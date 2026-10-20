import requests
from robo.datajud import URL_BASE, CHAVE_PUBLICA_PADRAO

r = requests.post(
    URL_BASE.format(alias="tjrs"),
    headers={"Authorization": f"APIKey {CHAVE_PUBLICA_PADRAO}"},
    json={"size": 5, "query": {"match": {"classe.nome": "Procedimento Comum Cível"}}},
    timeout=30,
)
print("HTTP", r.status_code)
r.raise_for_status()
numeros = [h["_source"]["numeroProcesso"] for h in r.json()["hits"]["hits"]]
with open("meus_processos.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(numeros))
print(numeros)