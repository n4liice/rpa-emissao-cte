# RPA — Emissão de CT-e (Mandalog)

Worker de automação que recebe XMLs de NF-e e conduz todo o fluxo de emissão de CT-e no **ESL Cloud TMS**, exposto como API HTTP via FastAPI.

---

## Sumário

1. [Visão Geral](#visão-geral)
2. [Arquitetura](#arquitetura)
3. [Variáveis de Ambiente](#variáveis-de-ambiente)
4. [API — Endpoints](#api--endpoints)
5. [Fluxo RPA — Passos Detalhados](#fluxo-rpa--passos-detalhados)
6. [Tratamento de Erros e Recuperação](#tratamento-de-erros-e-recuperação)
7. [Deploy (Docker / Easypanel)](#deploy-docker--easypanel)
8. [Desenvolvimento Local](#desenvolvimento-local)

---

## Visão Geral

O sistema automatiza via Playwright (Chromium headless) o seguinte fluxo no ESL Cloud:

```
NF-e XML(s) → Upload no TMS → Processar documentos → Aguardar fretes → Gerar CT-es → Status final
```

A sessão do browser é reutilizada entre requisições para evitar reautenticações desnecessárias.

---

## Arquitetura

```
main.py          — FastAPI app, lifespan do browser, endpoints HTTP
browser.py       — Lógica RPA (24+ passos de automação com Playwright)
session.py       — Gerenciamento de sessão/browser persistente entre requests
tms_api.py       — Cliente GraphQL para busca de clientes no ESL
data/
  session.json           — Estado de sessão salvo (cookies Playwright)
  xml_recebidos/{ts}/    — Arquivo dos XMLs recebidos com checksum SHA-256
logs/
  rpa.log        — Log estruturado de todos os passos
```

**Dependências principais:**

| Pacote | Uso |
|---|---|
| `fastapi` + `uvicorn` | Servidor HTTP / API REST |
| `playwright` | Automação do Chromium |
| `httpx` | Chamadas GraphQL ao ESL |
| `python-dotenv` | Carregamento do `.env` |

---

## Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto (nunca commitar):

```dotenv
# Credenciais de login no ESL Cloud (fallback manual se sessão expirar)
TMS_EMAIL=seu@email.com
TMS_PASSWORD=sua_senha

# Token Bearer para a API GraphQL do ESL
TMS_API_TOKEN=Bearer <token>

# Domínio do TMS
TMS_HOST=mandalog.eslcloud.com.br

# Onde salvar o estado de sessão do browser
SESSION_FILE=./data/session.json

# Porta do servidor HTTP
PORT=8000
```

> **No Easypanel:** configure as variáveis em *Environment → Variables*, nunca no repositório.

---

## API — Endpoints

### `GET /health`

Verifica se o serviço e o browser estão operacionais.

**Resposta `200`:**
```json
{
  "status": "ok",
  "browser": "conectado"
}
```

`status` pode ser `"degraded"` e `browser` pode ser `"desconectado"` se o Chromium não estiver acessível.

---

### `POST /buscar-cliente`

Busca um cliente no ESL Cloud por CNPJ (ou nome como fallback).

**Request body:**
```json
{
  "cnpj": "12345678000199",
  "nome": "EMPRESA EXEMPLO"
}
```

**Resposta — encontrado:**
```json
{
  "encontrado": true,
  "cliente": {
    "id": "abc123",
    "nome": "EMPRESA EXEMPLO LTDA",
    "nomeFantasia": "EXEMPLO",
    "cnpj": "12345678000199",
    "cnpjBase": "12345678",
    "cidade": "Bauru",
    "uf": "SP",
    "logradouro": "Rua X",
    "numero": "100",
    "bairro": "Centro",
    "cep": "17000-000",
    "email": "...",
    "telefone": "...",
    "ie": "...",
    "tipo": "...",
    "centrosCusto": [{ "id": "1", "name": "Principal" }]
  }
}
```

**Resposta — não encontrado:**
```json
{
  "encontrado": false,
  "mensagem": "CNPJ 12345678000199 não encontrado.",
  "sugestoes": [ { "id": "...", "nome": "..." } ]
}
```

**Erros:**

| Código | Motivo |
|---|---|
| `400` | Token inválido, rate limit ou erro de negócio |
| `500` | Erro inesperado no servidor |

---

### `POST /importar`

Executa o fluxo completo de importação de NF-e e emissão de CT-e.

**Request body:**
```json
{
  "customer_id": "abc123",
  "corporation_id": "MANDALOG - BAURU",
  "customer_name": "EMPRESA EXEMPLO LTDA",
  "xml_files": [
    {
      "name": "nfe_001.xml",
      "content_base64": "<base64 do XML>"
    }
  ]
}
```

> Formas alternativas de enviar o XML: campo `xml` (texto cru) ou `xml_contents` (array de base64).

**Mapeamento de `corporation_id`:**

| Nome | ID interno ESL |
|---|---|
| `MANDALOG - BAURU` | `153564` |
| `MANDALOG OPERACOES LOGISTICAS` | `29697` |

**Resposta — sucesso `200`:**
```json
{
  "sucesso": true,
  "lote": "78901",
  "documentos": 1,
  "status_cte": "OK",
  "screenshot": "<base64 opcional>"
}
```

**Resposta — falha `422`:**
```json
{
  "sucesso": false,
  "lote": "78901",
  "documentos": 1,
  "erro": "Nenhum frete encontrado após 10 tentativas (605s).",
  "passo": "Passo 19 — Clicar na aba Fretes",
  "screenshot": "<base64 do estado de erro>"
}
```

**Valores de `status_cte`:**

| Valor | Significado |
|---|---|
| `"OK"` | Todos os CT-es autorizados |
| `"INCONSISTENTE (N frete(s))"` | N fretes com inconsistência |
| `"REJEITADO (N frete(s))"` | N fretes rejeitados pela SEFAZ |
| `"PENDENTE (N frete(s))"` | N fretes aguardando processamento |

**Erros HTTP:**

| Código | Motivo |
|---|---|
| `400` | Nenhum XML enviado |
| `503` | Browser/automação indisponível |
| `422` | Erro de negócio durante o fluxo |
| `500` | Erro inesperado |

---

## Fluxo RPA — Passos Detalhados

| # | Nome | O que faz | Timeout |
|---|---|---|---|
| 1 | Navegar para Lotes | Acessa a página de lotes de importação EDI | — |
| 2 | Abrir dropdown "Nova Importação" | Clica no botão flutuante principal | — |
| 3 | Selecionar "XML - NF-e" | Clica no link com href `/new?file_format=xml` | — |
| 4 | Aguardar modal | Espera o formulário `#edi_import_batch_upload` renderizar | — |
| 5 | Selecionar cliente (Select2) | Seleciona a empresa por ID ou, como fallback, por nome | até 10 s |
| 6 | Selecionar filial (Select2) | Seleciona a filial (`corporation_id`) | até 10 s |
| 7 | Upload dos XMLs | Injeta os arquivos no input (bootstrap-fileinput) | 20 s |
| 8 | Clicar Salvar | Submete o formulário (`#submit`) | — |
| 9 | Confirmar modal de atenção | Aceita SweetAlert2/Bootstrap modal se aparecer (não fatal) | — |
| 10 | Fechar popup intermediário | Fecha overlay/backdrop com retry de 4× | — |
| 11 | Definir opção automática | Garante `Importar documentos` no Select2 de processamento | — |
| 12 | Aguardar página do lote | Espera network idle e carregamento completo | 30 s |
| 13 | Extrair número do lote | Lê o ID do lote da URL ou do HTML | — |
| 14 | Aguardar processamento dos arquivos | Espera ✓ verdes ou ✗ vermelhos nos arquivos | 10 min |
| 15 | Aba "Documentos Importados" | Navega para a aba de documentos | — |
| 16 | Verificar duplicados | Detecta badge "Duplicados - N"; falha se > 0 | — |
| 17 | Selecionar todos os documentos | Marca o checkbox mestre; aguarda barra de ações | — |
| 18 | Clicar "Processar" | Clica no botão Processar (ícone file-import) | — |
| 19 | Confirmar modal de fretes | Aceita "Confirma geração dos fretes?" (SweetAlert2) | — |
| 20 | Aguardar processamento | Espera "Pendentes" = 0 e "Processados" > 0 (não fatal) | 30 s |
| 21 | Aba "Fretes" | Alterna entre abas para forçar reload AJAX; espera progressiva 4–15 s | 10 min |
| 22 | Selecionar todos os fretes | Marca o checkbox mestre na aba Fretes | — |
| 23 | Clicar "Gerar CT-es" | Clica no botão de geração na barra de ações | — |
| 24 | Confirmar emissão de CT-es | Aceita "Confirma a emissão dos CT-es?" | — |
| 25 | Aguardar CT-es | Espera spinner sumir nas linhas de frete | 2 min |
| 26 | Verificar status final | Lê contadores Autorizados/Inconsistentes/Rejeitados/Pendentes + screenshot | — |

---

## Tratamento de Erros e Recuperação

**Recuperação de sessão expirada:**
Se qualquer passo detectar redirecionamento para login (`/sign_in`, `401`, etc.), o worker descarta a sessão salva, reautentica e repete o fluxo uma vez.

**Screenshots de erro:**
Toda falha captura screenshot full-page em base64 e devolve no campo `screenshot` da resposta.

**Passos não fatais:**
Passo 9 (modal de atenção) e Passo 20 (aguardar Processados via AJAX) não interrompem o fluxo se não ocorrerem.

**Espera progressiva no Passo 21:**
O ESL pode demorar vários minutos para gerar fretes. O loop tenta por até **10 minutos**, aumentando o intervalo de espera de 4 s até 15 s a cada tentativa, e registra o tempo decorrido no log.

---

## Deploy (Docker / Easypanel)

**Dockerfile:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .
RUN mkdir -p data/xml_recebidos logs
EXPOSE 8000
CMD ["python", "main.py"]
```

**Passos no Easypanel:**
1. Aponte o serviço para o repositório `n4liice/rpa-emissao-cte`, branch `main`.
2. Configure as variáveis de ambiente (seção acima).
3. Exponha a porta `8000`.
4. Para redeploy após mudanças: `git push origin main` e acione o redeploy no painel.

> Configure um volume persistente em `/app/data` para preservar a sessão do browser entre redeploys.

---

## Desenvolvimento Local

```bash
# 1. Criar e ativar ambiente virtual
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux/macOS

# 2. Instalar dependências
pip install -r requirements.txt
playwright install chromium --with-deps

# 3. Configurar variáveis de ambiente
cp .env.example .env   # edite com suas credenciais

# 4. Iniciar o servidor
python main.py
# Disponível em: http://localhost:8000

# Logs em tempo real
tail -f logs/rpa.log
```
