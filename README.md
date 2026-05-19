# RPA Worker — Mandalog

Worker de automação para emissão de CT-e. Roda como serviço HTTP e recebe chamadas do front-end.

## Setup local

```bash
# 1. Criar ambiente virtual
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux/macOS

# 2. Instalar dependências
pip install -r requirements.txt
playwright install chromium

# 3. Configurar variáveis
cp .env.example .env
# Editar .env com os dados reais

# 4. Criar pasta de sessão
mkdir data

# 5. Rodar
uvicorn main:app --reload --port 8000
```

## Endpoints

| Método | Rota              | Descrição                          |
|--------|-------------------|------------------------------------|
| GET    | /health           | Status do serviço                  |
| POST   | /buscar-cliente   | Busca cliente pelo CNPJ            |
| POST   | /importar         | Importa XML NF-e via automação     |
