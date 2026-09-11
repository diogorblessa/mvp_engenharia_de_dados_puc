"""Baixa e prepara os arquivos mensais de reclamações finalizadas do consumidor.gov.br.

Fonte: consumidor.gov.br → Indicadores → Dados Abertos (Senacon/MJ, licença CC BY).
1. Baixa os zips para dados/brutos/, com o nome original publicado.
2. Converte cada zip em dados/preparados/finalizadas_AAAA-MM.csv.gz, formato que o
   Spark lê direto, e grava dados/preparados/manifesto.csv com a origem de cada arquivo.

Dois meses fogem do padrão da fonte e usam ferramentas externas: 2026-05 traz um .7z
dentro do zip (tar.exe do Windows) e 2026-06 usa compressão Deflate64, que o zipfile
do Python não lê (unzip.exe do Git). Rodar da raiz do repositório:

    .\\.venv\\Scripts\\python.exe scripts\\baixar_reclamacoes.py
"""

import csv
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

INICIO, FIM = "2021-01", "2026-08"  # período do MVP (aaaa-mm, inclusivo)

BASE = "https://consumidor.gov.br"
PAGINA = f"{BASE}/pages/dadosabertos/externo"
LISTAGEM = f"{BASE}/pages/publicacao/externo/publicacoes.json?indicadorTipoPublicacao=2"
DOWNLOAD = f"{BASE}/pages/publicacao/externo/{{codigo}}/download"
RAIZ = Path(__file__).resolve().parent.parent
BRUTOS, PREPARADOS = RAIZ / "dados" / "brutos", RAIZ / "dados" / "preparados"

TAR_WINDOWS = r"C:\Windows\System32\tar.exe"
UNZIP_GIT = r"C:\Program Files\Git\usr\bin\unzip.exe"
DEFLATE64 = 9  # código do método de compressão no formato ZIP
CABECALHO = (
    "Região;UF;Cidade;Sexo;Faixa Etária;Data Finalização;Tempo Resposta;Nome Fantasia;"
    "Segmento de Mercado;Área;Assunto;Grupo Problema;Problema;Como Comprou Contratou;"
    "Procurou Empresa;Respondida;Situação;Avaliação Reclamação;Nota do Consumidor"
)

# A listagem só responde com o cookie de sessão criado ao abrir a página.
navegador = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def meses_esperados():
    ano, mes = map(int, INICIO.split("-"))
    meses = []
    while f"{ano:04d}-{mes:02d}" <= FIM:
        meses.append(f"{ano:04d}-{mes:02d}")
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


def listar_meses():
    """Retorna as publicações do período, uma por mês, na ordem cronológica."""
    navegador.open(PAGINA).read()
    publicacoes = json.loads(navegador.open(LISTAGEM).read().decode("utf-8"))
    por_mes = {}
    for p in publicacoes:
        achou = re.search(r"(\d{4}-\d{2})", p["nomeArquivo"])
        if p["titulo"].startswith("Dados") and achou and INICIO <= achou.group(1) <= FIM:
            if achou.group(1) in por_mes:
                raise SystemExit(f"Mês {achou.group(1)} publicado mais de uma vez na listagem.")
            por_mes[achou.group(1)] = p
    esperados = meses_esperados()
    faltando = sorted(set(esperados) - set(por_mes))
    if faltando:
        raise SystemExit(f"Meses ausentes na listagem: {faltando}")
    return [(mes, por_mes[mes]) for mes in esperados]


def preparar(caminho_zip, destino):
    """Extrai o CSV do zip e grava como .csv.gz; só cria o destino se as 19 colunas padrão conferirem."""
    membros = zipfile.ZipFile(caminho_zip).infolist()
    if len(membros) != 1:
        raise SystemExit(f"{caminho_zip.name}: esperado 1 arquivo dentro do zip, há {len(membros)}.")
    membro = membros[0]
    parcial = destino.with_name(destino.name + ".parcial")
    with tempfile.TemporaryDirectory() as tmp:
        if membro.filename.endswith(".7z"):
            subprocess.run([TAR_WINDOWS, "-xf", caminho_zip, "-C", tmp], check=True)
            subprocess.run([TAR_WINDOWS, "-xf", Path(tmp) / membro.filename, "-C", tmp], check=True)
            origem = open(next(Path(tmp).glob("*.csv")), "rb")
        elif membro.compress_type == DEFLATE64:
            subprocess.run([UNZIP_GIT, "-q", caminho_zip, "-d", tmp], check=True)
            origem = open(Path(tmp) / membro.filename, "rb")
        else:
            origem = zipfile.ZipFile(caminho_zip).open(membro)
        with origem, gzip.open(parcial, "wb") as saida:
            shutil.copyfileobj(origem, saida)
    with gzip.open(parcial, "rt", encoding="utf-8-sig") as f:
        colunas = f.readline().rstrip("\r\n").split(";")
    # Colunas extras no fim são aceitas (2025-09 veio com 21); as 19 padrão têm de estar lá, na ordem.
    if colunas[:19] != CABECALHO.split(";"):
        raise SystemExit(f"{caminho_zip.name}: as 19 primeiras colunas diferem do cabeçalho padrão.")
    parcial.replace(destino)


def main():
    BRUTOS.mkdir(parents=True, exist_ok=True)
    PREPARADOS.mkdir(parents=True, exist_ok=True)
    manifesto = []
    for mes, p in listar_meses():
        caminho_zip = BRUTOS / p["nomeArquivo"]
        if caminho_zip.exists() and caminho_zip.stat().st_size == p["tamanhoArquivo"]:
            situacao = "já existia"
        else:
            caminho_zip.write_bytes(navegador.open(DOWNLOAD.format(codigo=p["codigo"])).read())
            if caminho_zip.stat().st_size != p["tamanhoArquivo"]:
                raise SystemExit(f"{caminho_zip.name}: tamanho diferente do publicado na listagem.")
            situacao = "baixado"
        destino = PREPARADOS / f"finalizadas_{mes}.csv.gz"
        if not destino.exists():
            preparar(caminho_zip, destino)
        with gzip.open(destino, "rt", encoding="utf-8-sig") as f:
            extras = f.readline().rstrip("\r\n").split(";")[19:]
        aviso = f"  (colunas extras: {extras})" if extras else ""
        print(f"{mes}  {caminho_zip.name:<28} {p['tamanhoArquivo'] / 1e6:5.1f} MB  {situacao:<10}  -> {destino.name}{aviso}")
        manifesto.append({
            "codigo": p["codigo"],
            "mes_referencia": mes,
            "nome_arquivo": caminho_zip.name,
            "data_publicacao": p["dataPublicacao"],
            "tamanho_bytes": p["tamanhoArquivo"],
            "sha256": hashlib.sha256(caminho_zip.read_bytes()).hexdigest(),
            "arquivo_preparado": destino.name,
        })
    with open(PREPARADOS / "manifesto.csv", "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=list(manifesto[0]), delimiter=";")
        escritor.writeheader()
        escritor.writerows(manifesto)
    total_gz = sum(m.stat().st_size for m in PREPARADOS.glob("*.csv.gz")) / 1e6
    print(f"\n{len(manifesto)} meses preparados, {total_gz:.0f} MB em .csv.gz. Pasta para o volume: {PREPARADOS}")


if __name__ == "__main__":
    main()
