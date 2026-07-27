"""
ROTAS — Agrupador de Cargas e Roteirizador (Frinense)
=====================================================
Projeto novo, servico proprio na Railway, seguindo o MESMO padrao do IAF:
  - Conexao Drive identica ao IAF (token OAuth picklado em GOOGLE_TOKEN_PICKLE),
    so trocando a variavel do arquivo: DRIVE_FILE_ID_ROTAS (convencao do IA3).
  - FastAPI servindo HTML (painel.html).
  - SQLite persistente em /data (rotas.db) para os romaneios (historico).

Fluxo: CSV de pedidos nao faturados (Drive)  -  pedidos ja em rota (SQLite)
       = POOL disponivel  ->  filtros  ->  OR-Tools monta cargas  ->  salva romaneio.
"""

import os
import io
import json
import base64
import pickle
import math
import logging
import unicodedata
import threading
import sqlite3
from datetime import datetime

os.environ.setdefault("TZ", "America/Sao_Paulo")
try:
    import time; time.tzset()
except AttributeError:
    pass

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

logging.basicConfig(level=logging.WARNING)  # Railway so mostra WARNING+
log = logging.getLogger("rotas")

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])

# Arquivo de pedidos no Drive (mesma conta do IAF, variavel propria).
FILE_ID_ROTAS = os.environ.get("DRIVE_FILE_ID_ROTAS", "")
# Fallback local (so pra teste fora da Railway):
LOCAL_CSV = os.environ.get("ROTAS_LOCAL_CSV", "")

DEPOT_IBGE = os.environ.get("DEPOT_IBGE", "3302205")  # Itaperuna/RJ
EXCLUIR_UF = ["SP"]                                    # long-haul fora (por ora)
ROTAS_DB = os.environ.get("ROTAS_DB", "/data/rotas.db")
CACHE_TTL = 1800  # 30 min

# Cadastro de frota por TIPO+capacidade (kg) — nunca por placa.
FROTA_CADASTRO = {"Truck": 14000, "Toco": 7000, "VUC": 3000}

# ─────────────────────────────────────────────
#  DRIVE  (identico ao IAF)
# ─────────────────────────────────────────────
def get_drive_service():
    token_bytes = os.environ.get("GOOGLE_TOKEN_PICKLE")
    if not token_bytes:
        raise HTTPException(status_code=500, detail="Token do Google Drive nao configurado.")
    creds = pickle.loads(base64.b64decode(token_bytes))
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)

_cache_lock = threading.Lock()
_cache = {"df": None, "ts": None}

def _download_pedidos() -> pd.DataFrame:
    if LOCAL_CSV and os.path.exists(LOCAL_CSV):        # teste local
        return pd.read_csv(LOCAL_CSV, sep=";", encoding="utf-8-sig", dtype=str)
    if not FILE_ID_ROTAS:
        raise HTTPException(status_code=500, detail="DRIVE_FILE_ID_ROTAS nao configurado.")
    service = get_drive_service()
    req = service.files().get_media(fileId=FILE_ID_ROTAS)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return pd.read_csv(buf, sep=";", encoding="utf-8-sig", dtype=str)

def carregar_bruto(force=False) -> pd.DataFrame:
    with _cache_lock:
        agora = time.time()
        if (not force and _cache["df"] is not None
                and _cache["ts"] and agora - _cache["ts"] < CACHE_TTL):
            return _cache["df"]
        df = _download_pedidos()
        _cache["df"] = df
        _cache["ts"] = agora
        log.warning("Pedidos recarregados do Drive: %d linhas", len(df))
        return df

# ─────────────────────────────────────────────
#  MUNICIPIOS  (IBGE -> lat/long)
# ─────────────────────────────────────────────
_MUN = None
def municipios() -> pd.DataFrame:
    global _MUN
    if _MUN is None:
        for p in ["municipios.csv", "/app/municipios.csv"]:
            if os.path.exists(p):
                _MUN = pd.read_csv(p, dtype={"codigo_ibge": str}).set_index("codigo_ibge")
                break
    return _MUN

_MESO = None
def mesorregioes() -> pd.DataFrame:
    global _MESO
    if _MESO is None:
        for p in ["mesorregioes.csv", "/app/mesorregioes.csv"]:
            if os.path.exists(p):
                _MESO = pd.read_csv(p, dtype={"ibge": str}).set_index("ibge")
                break
    return _MESO

def depot_coord():
    m = municipios()
    r = m.loc[DEPOT_IBGE]
    return float(r["latitude"]), float(r["longitude"])

# ─────────────────────────────────────────────
#  PREPARAR  (item -> pedido, peso somado, coordenada por IBGE)
# ─────────────────────────────────────────────
def preparar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ibge"] = df["COD_IBGE"].str.strip()
    df["kg"] = pd.to_numeric(df["QTDE_KG"], errors="coerce").fillna(0.0)  # ponto=decimal
    if EXCLUIR_UF:
        df = df[~df["UF"].isin(EXCLUIR_UF)]

    def tipos(s):
        return ", ".join(sorted(set(v for v in s.dropna() if str(v).strip())))

    ped = (df.groupby("NUM_DOCTO")
             .agg(kg=("kg", "sum"), cidade=("CIDADE", "first"), ibge=("ibge", "first"),
                  uf=("UF", "first"), cliente=("NOME_CLIENTE", "first"),
                  vendedor=("NOME_VENDEDOR", "first"), emissao=("EMISSAO", "first"),
                  tipo_produto=("TIPO_PRODUTO", tipos), tipo_carne=("TIPO_CARNE", tipos),
                  tipo_corte=("TIPO_CORTE", tipos), itens=("ITEM", "count"))
             .reset_index().rename(columns={"NUM_DOCTO": "pedido"}))

    ped["kg"] = ped["kg"].round().astype(int)
    ped["emissao"] = pd.to_datetime(ped["emissao"], errors="coerce")
    hoje = pd.Timestamp.now().normalize()
    ped["idade_dias"] = (hoje - ped["emissao"]).dt.days.clip(lower=0).fillna(0).astype(int)

    m = municipios()
    ped = ped.join(m[["latitude", "longitude"]], on="ibge")
    ped["cidade"] = ped["ibge"].map(m["nome"])   # nome canonico (texto CIDADE e sujo)
    # mesorregiao oficial IBGE (regiao do estado). Sem meso -> rotulo pela UF.
    meso = mesorregioes()
    ped["mesorregiao"] = ped["ibge"].map(meso["mesorregiao"])
    ped["mesorregiao"] = ped["mesorregiao"].fillna("(s/ região) " + ped["uf"].astype(str))
    ped["emissao"] = ped["emissao"].dt.strftime("%Y-%m-%d")
    return ped

def _norm(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper().strip()

def aplicar_filtros(ped, f: dict) -> pd.DataFrame:
    p = ped.copy()
    if f.get("pedidos_ids"):
        return p[p["pedido"].isin(f["pedidos_ids"])]
    for campo, col in [("uf", "uf"), ("cidade", "cidade"), ("mesorregiao", "mesorregiao")]:
        if f.get(campo):
            alvos = [_norm(x) for x in f[campo]]
            p = p[p[col].apply(_norm).isin(alvos)]
    for campo, col in [("vendedor", "vendedor"), ("tipo_carne", "tipo_carne"),
                       ("tipo_corte", "tipo_corte"), ("tipo_produto", "tipo_produto")]:
        if f.get(campo):
            pat = "|".join(_norm(x) for x in f[campo])
            p = p[p[col].apply(_norm).str.contains(pat)]
    return p

# ─────────────────────────────────────────────
#  POOL  (CSV nao faturado  -  pedidos ja em rota salva)
# ─────────────────────────────────────────────
def pool_disponivel(force=False) -> pd.DataFrame:
    ped = preparar(carregar_bruto(force=force))
    with _db_connect() as con:
        ja = {r[0] for r in con.execute("SELECT pedido FROM rota_pedidos").fetchall()}
    return ped[~ped["pedido"].isin(ja)].reset_index(drop=True)

# ─────────────────────────────────────────────
#  SOLVER  (OR-Tools CVRP com paradas opcionais)
# ─────────────────────────────────────────────
def _haversine_m(a, b, c, d):
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dphi, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return int(R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x)))

def montar_cargas(pool: pd.DataFrame, frota: Dict[str, int], segundos=8):
    res = {"cargas": [], "bandeja": [], "dedicados": []}
    veic = []
    for tipo, qtd in frota.items():
        cap = FROTA_CADASTRO.get(tipo)
        for _ in range(int(qtd)):
            if cap:
                veic.append({"tipo": tipo, "cap": cap})
    if not veic or pool.empty:
        res["bandeja"] = [_lin(r) for _, r in pool.iterrows()]
        return res
    cap_max = max(v["cap"] for v in veic)

    dedic = pool[pool["kg"] > cap_max]
    for _, r in dedic.iterrows():
        res["dedicados"].append(_lin(r))
    pool = pool[pool["kg"] <= cap_max].reset_index(drop=True)
    if pool.empty:
        return res

    lats = [depot_coord()[0]] + pool["latitude"].tolist()
    lons = [depot_coord()[1]] + pool["longitude"].tolist()
    dem = [0] + pool["kg"].tolist()
    n = len(lats)
    dist = [[0]*n for _ in range(n)]
    for i in range(n):
        for j in range(i+1, n):
            dd = _haversine_m(lats[i], lons[i], lats[j], lons[j])
            dist[i][j] = dist[j][i] = dd

    mgr = pywrapcp.RoutingIndexManager(n, len(veic), 0)
    routing = pywrapcp.RoutingModel(mgr)
    cb = routing.RegisterTransitCallback(lambda i, j: dist[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
    routing.SetArcCostEvaluatorOfAllVehicles(cb)
    dcb = routing.RegisterUnaryTransitCallback(lambda i: dem[mgr.IndexToNode(i)])
    routing.AddDimensionWithVehicleCapacity(dcb, 0, [v["cap"] for v in veic], True, "Peso")

    for node in range(1, n):
        idade = int(pool.iloc[node-1]["idade_dias"])
        routing.AddDisjunction([mgr.NodeToIndex(node)], 10_000_000 + idade * 50_000)

    prm = pywrapcp.DefaultRoutingSearchParameters()
    prm.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    prm.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    prm.time_limit.FromSeconds(segundos)
    sol = routing.SolveWithParameters(prm)
    if not sol:
        res["bandeja"] = [_lin(r) for _, r in pool.iterrows()]
        return res

    atend = set()
    cid = 0
    for v in range(len(veic)):
        idx = routing.Start(v); nodes = []
        while not routing.IsEnd(idx):
            nd = mgr.IndexToNode(idx)
            if nd != 0:
                nodes.append(nd); atend.add(nd)
            idx = sol.Value(routing.NextVar(idx))
        if not nodes:
            continue
        cid += 1
        peds = [pool.iloc[nd-1] for nd in nodes]
        # agrupa em PARADAS por IBGE, preservando a ordem da sequencia
        stops, ordem = [], {}
        for r in peds:
            key = r["ibge"]
            if key not in ordem:
                ordem[key] = len(stops)
                stops.append({"cidade": r["cidade"], "ibge": key,
                              "lat": float(r["latitude"]), "lng": float(r["longitude"]),
                              "kg": 0, "pedidos": []})
            s = stops[ordem[key]]
            s["kg"] += int(r["kg"]); s["pedidos"].append(_lin(r))
        for i, s in enumerate(stops, 1):
            s["ordem"] = i
        peso = sum(int(r["kg"]) for r in peds)
        res["cargas"].append({
            "id_tmp": cid, "veiculo": veic[v]["tipo"], "cap_kg": veic[v]["cap"],
            "peso_kg": peso, "ocupacao": round(100*peso/veic[v]["cap"], 1),
            "n_pedidos": len(peds), "n_cidades": len(stops), "stops": stops,
            "pedidos_ids": [r["pedido"] for r in peds],
        })
    for node in range(1, n):
        if node not in atend:
            res["bandeja"].append(_lin(pool.iloc[node-1]))
    return res

def _lin(r):
    return {"pedido": r["pedido"], "cidade": r["cidade"], "uf": r["uf"], "kg": int(r["kg"]),
            "cliente": r["cliente"], "vendedor": r["vendedor"],
            "idade_dias": int(r["idade_dias"]), "emissao": r["emissao"],
            "tipo_carne": r["tipo_carne"], "ibge": r["ibge"],
            "mesorregiao": r.get("mesorregiao", "")}

# ─────────────────────────────────────────────
#  SQLITE  (romaneios = historico)
# ─────────────────────────────────────────────
def _db_connect():
    os.makedirs(os.path.dirname(ROTAS_DB), exist_ok=True)
    return sqlite3.connect(ROTAS_DB)

def _db_init():
    with _db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS rotas(
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, data TEXT, modo TEXT,
            veiculo TEXT, cap_kg INTEGER, peso_kg INTEGER, n_pedidos INTEGER, cidades TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS rota_pedidos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, rota_id INTEGER, pedido TEXT,
            cidade TEXT, ibge TEXT, uf TEXT, kg INTEGER, cliente TEXT, vendedor TEXT,
            emissao TEXT, tipo_carne TEXT)""")
_db_init()

# ─────────────────────────────────────────────
#  MODELS
# ─────────────────────────────────────────────
class MontarReq(BaseModel):
    frota: Dict[str, int]                 # {"Truck": 3, "Toco": 2}
    modo: str = "automatico"              # automatico | selecao
    filtros: dict = {}                    # uf, vendedor, cidade, tipo_carne...
    segundos: int = 8

class SalvarReq(BaseModel):
    nome: str
    veiculo: str
    cap_kg: int
    modo: str
    pedidos_ids: List[str]
    cidades: List[str] = []

# ─────────────────────────────────────────────
#  ENDPOINTS
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root():
    for p in ["painel.html", "/app/painel.html"]:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Painel de Rotas — painel.html nao encontrado</h1>")

@app.get("/pool")
def get_pool(force: bool = False):
    p = pool_disponivel(force=force)
    meso_por_uf = {}
    for uf, grp in p.groupby("uf"):
        meso_por_uf[uf] = sorted(grp["mesorregiao"].dropna().unique().tolist())
    return {
        "total": len(p), "kg": int(p["kg"].sum()), "cidades": int(p["cidade"].nunique()),
        "ufs": sorted(p["uf"].dropna().unique().tolist()),
        "mesorregioes_por_uf": meso_por_uf,
        "vendedores": sorted(p["vendedor"].dropna().unique().tolist()),
        "tipos_carne": sorted({t for v in p["tipo_carne"] for t in str(v).split(", ") if t}),
        "frota_cadastro": FROTA_CADASTRO,
    }

def resumo_produto(pedidos_ids):
    """Agrega os ITENS (corte + produto) dos pedidos de uma carga, por kg."""
    df = carregar_bruto()  # raw cacheado (nivel item)
    sub = df[df["NUM_DOCTO"].isin(pedidos_ids)].copy()
    if sub.empty:
        return []
    sub["kg"] = pd.to_numeric(sub["QTDE_KG"], errors="coerce").fillna(0.0)
    g = (sub.groupby([sub["TIPO_CORTE"].fillna("-"), sub["desc_produto2"].fillna("-")])
            .agg(kg=("kg", "sum"), itens=("ITEM", "count")).reset_index())
    g.columns = ["corte", "produto", "kg", "itens"]
    g = g.sort_values("kg", ascending=False)
    return [{"corte": r["corte"], "produto": r["produto"],
             "kg": int(round(r["kg"])), "itens": int(r["itens"])} for _, r in g.iterrows()]


@app.post("/montar")
def montar(req: MontarReq):
    p = pool_disponivel()
    p = aplicar_filtros(p, req.filtros)
    res = montar_cargas(p, req.frota, segundos=req.segundos)
    for c in res["cargas"]:
        c["resumo_produto"] = resumo_produto(c["pedidos_ids"])
    aloc = sum(c["n_pedidos"] for c in res["cargas"])
    cap_total = sum(FROTA_CADASTRO.get(t, 0)*q for t, q in req.frota.items())
    res["resumo"] = {
        "pedidos": int(len(p)), "kg": int(p["kg"].sum()),
        "cidades": int(p["cidade"].nunique()) if len(p) else 0,
        "cap_total": cap_total, "alocados": aloc,
        "sobra": len(res["bandeja"]),
        "kg_sobra": sum(x["kg"] for x in res["bandeja"]),
        "dedicados": len(res["dedicados"]),
        "depot": {"cidade": "Itaperuna", "lat": depot_coord()[0], "lng": depot_coord()[1]},
    }
    return JSONResponse(res)

@app.post("/salvar-rota")
def salvar_rota(req: SalvarReq):
    ped = pool_disponivel()
    sel = ped[ped["pedido"].isin(req.pedidos_ids)]
    if sel.empty:
        raise HTTPException(400, "Nenhum pedido valido para salvar (ja roteirizado?).")
    peso = int(sel["kg"].sum())
    with _db_connect() as con:
        cur = con.execute(
            "INSERT INTO rotas(nome,data,modo,veiculo,cap_kg,peso_kg,n_pedidos,cidades) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (req.nome, datetime.now().strftime("%Y-%m-%d %H:%M"), req.modo, req.veiculo,
             req.cap_kg, peso, len(sel), json.dumps(req.cidades, ensure_ascii=False)))
        rid = cur.lastrowid
        for _, r in sel.iterrows():
            con.execute(
                "INSERT INTO rota_pedidos(rota_id,pedido,cidade,ibge,uf,kg,cliente,vendedor,emissao,tipo_carne)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, r["pedido"], r["cidade"], r["ibge"], r["uf"], int(r["kg"]),
                 r["cliente"], r["vendedor"], r["emissao"], r["tipo_carne"]))
    log.warning("Rota salva id=%s nome=%s pedidos=%d", rid, req.nome, len(sel))
    return {"ok": True, "rota_id": rid, "n_pedidos": len(sel), "peso_kg": peso}

@app.get("/rotas")
def listar_rotas():
    with _db_connect() as con:
        rows = con.execute(
            "SELECT id,nome,data,modo,veiculo,cap_kg,peso_kg,n_pedidos,cidades "
            "FROM rotas ORDER BY id DESC").fetchall()
    cols = ["id", "nome", "data", "modo", "veiculo", "cap_kg", "peso_kg", "n_pedidos", "cidades"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["cidades"] = json.loads(d["cidades"] or "[]")
        d["ocupacao"] = round(100*d["peso_kg"]/d["cap_kg"], 1) if d["cap_kg"] else 0
        out.append(d)
    return {"rotas": out}

@app.get("/rota/{rid}")
def detalhe_rota(rid: int):
    with _db_connect() as con:
        r = con.execute("SELECT id,nome,data,modo,veiculo,cap_kg,peso_kg,n_pedidos,cidades "
                        "FROM rotas WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "Rota nao encontrada.")
        peds = con.execute("SELECT pedido,cidade,ibge,uf,kg,cliente,vendedor,emissao,tipo_carne "
                           "FROM rota_pedidos WHERE rota_id=?", (rid,)).fetchall()
    m = municipios()
    stops, ordem = [], {}
    for pd_ in peds:
        ibge = pd_[2]
        if ibge not in ordem:
            ordem[ibge] = len(stops)
            coord = m.loc[ibge] if ibge in m.index else None
            stops.append({"cidade": pd_[1], "ibge": ibge,
                          "lat": float(coord["latitude"]) if coord is not None else None,
                          "lng": float(coord["longitude"]) if coord is not None else None,
                          "kg": 0, "pedidos": []})
        s = stops[ordem[ibge]]
        s["kg"] += int(pd_[4])
        s["pedidos"].append({"pedido": pd_[0], "cliente": pd_[5], "kg": int(pd_[4]),
                             "vendedor": pd_[6], "tipo_carne": pd_[8]})
    return {"id": r[0], "nome": r[1], "data": r[2], "veiculo": r[4], "cap_kg": r[5],
            "peso_kg": r[6], "stops": stops,
            "depot": {"cidade": "Itaperuna", "lat": depot_coord()[0], "lng": depot_coord()[1]}}

@app.delete("/rota/{rid}")
def excluir_rota(rid: int):
    with _db_connect() as con:
        con.execute("DELETE FROM rota_pedidos WHERE rota_id=?", (rid,))
        con.execute("DELETE FROM rotas WHERE id=?", (rid,))
    log.warning("Rota excluida id=%s (pedidos liberados)", rid)
    return {"ok": True}

@app.delete("/rota/{rid}/pedido/{pedido}")
def excluir_pedido_rota(rid: int, pedido: str):
    with _db_connect() as con:
        con.execute("DELETE FROM rota_pedidos WHERE rota_id=? AND pedido=?", (rid, pedido))
        # recalcula peso/nped da rota
        row = con.execute("SELECT COUNT(*),COALESCE(SUM(kg),0) FROM rota_pedidos WHERE rota_id=?",
                          (rid,)).fetchone()
        con.execute("UPDATE rotas SET n_pedidos=?, peso_kg=? WHERE id=?", (row[0], row[1], rid))
        if row[0] == 0:
            con.execute("DELETE FROM rotas WHERE id=?", (rid,))
    return {"ok": True, "restantes": row[0]}

@app.get("/health")
def health():
    return {"ok": True, "drive": bool(FILE_ID_ROTAS) or bool(LOCAL_CSV)}
