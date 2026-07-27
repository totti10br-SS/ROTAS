# ROTAS · Frinense — Agrupador de Cargas + Roteirizador

Projeto novo, serviço próprio na Railway, **mesmo padrão do IAF**: FastAPI serve
HTML, deploy por push, SQLite em `/data`. Conexão Drive **idêntica** à do IAF —
só muda a variável do arquivo.

## Arquivos
- `main.py` — backend (Drive + OR-Tools + SQLite dos romaneios)
- `painel.html` — interface de sala (Leaflet, dark, estilo IAF)
- `municipios.csv` — IBGE → lat/long (5.570 municípios, embarcado)
- `requirements.txt` / `nixpacks.toml` — deploy

## Deploy na Railway
1. Repo novo → conectar novo serviço na Railway (auto-deploy por push).
2. Adicionar um **Volume** montado em `/data` (igual ao IAF).
3. Variáveis de ambiente:

| Variável | O que é |
|---|---|
| `GOOGLE_TOKEN_PICKLE` | **o mesmo do IAF** (token OAuth em base64) |
| `DRIVE_FILE_ID_ROTAS` | ID do CSV de pedidos: `1bbHmh31AKOS6KGO-JG9YBHh8fn5cwCQT` |
| `ROTAS_DB` | opcional (default `/data/rotas.db`) |
| `DEPOT_IBGE` | opcional (default `3302205` = Itaperuna) |

Não precisa configurar mais nada — o `GOOGLE_TOKEN_PICKLE` é literalmente o
mesmo valor que já roda no IAF.

## Como funciona
- **Pool** = pedidos do CSV (não faturados) **menos** os que já estão em rotas
  salvas (anti-join por `NUM_DOCTO`). Pedido faturado some do CSV → some do pool.
- **Montar** = filtra o pool → OR-Tools agrupa em cargas respeitando o kg do
  caminhão, priorizando os pedidos mais antigos. O que não cabe vai pra bandeja.
- **Salvar romaneio** = grava a carga no SQLite com **snapshot** dos pedidos
  (sobrevive ao pedido sumir do CSV). Aqueles pedidos saem do pool.
- **Excluir rota / pedido** = devolve ao pool.

## Rotas (endpoints)
- `GET /` painel · `GET /pool` · `POST /montar` · `POST /salvar-rota`
- `GET /rotas` · `GET /rota/{id}` · `DELETE /rota/{id}` · `DELETE /rota/{id}/pedido/{pedido}`
- `GET /health`

## O que ainda é próximo passo (não bloqueia o uso)
- **Distância real de estrada (OSRM)** — hoje o percurso é linha reta entre
  cidades. Funciona pra agrupar e visualizar; a ordem fina das paradas melhora
  com OSRM.
- **Cadastro de frota editável** — hoje os tipos (Truck/Toco/VUC + capacidade)
  estão fixos em `FROTA_CADASTRO` no `main.py`. Vira tela depois.
- **Botão ocupação × rota limpa** — knob pra aceitar caminhão a 85% em troca de
  rota mais coerente geograficamente.
