"""
main.py - Worker RPA Mandalog
"""

import asyncio
import base64
import logging
import os
import sys
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from logging import FileHandler, StreamHandler

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel

from browser import importar_xml
from tms_api import buscar_cliente_por_cnpj, buscar_cliente_por_nome

# Fix para Windows - deve ser antes de qualquer uso relevante do asyncio/playwright
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "rpa.log")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        StreamHandler(sys.stdout),
        FileHandler(LOG_FILE, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger(__name__)

logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

_playwright = None
_browser = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser
    logger.info("Iniciando automacao...")
    logger.info("Logs em arquivo: %s", LOG_FILE)
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
    )
    logger.info("Browser Chromium pronto.")
    yield
    logger.info("Encerrando automacao...")
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


app = FastAPI(
    title="RPA Worker - Mandalog",
    description="Automacao para emissao de CT-e",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BuscarClienteRequest(BaseModel):
    cnpj: str
    nome: str | None = None


class XmlFilePayload(BaseModel):
    name: str
    content_base64: str


class ImportarRequest(BaseModel):
    customer_id: str
    xml: str | None = None                        # XML cru (formato simples)
    corporation_id: str = "MANDALOG - BAURU"      # filial padrão
    # campos legados mantidos para compatibilidade
    customer_name: str | None = None
    xml_files: list[XmlFilePayload] | None = None
    xml_contents: list[str] | None = None


@app.get("/health")
async def health():
    browser_ok = _browser is not None and _browser.is_connected()
    logger.info("GET /health -> browser_ok=%s", browser_ok)
    return {
        "status": "ok" if browser_ok else "degraded",
        "browser": "conectado" if browser_ok else "desconectado",
    }


@app.post("/buscar-cliente")
async def buscar_cliente(req: BuscarClienteRequest):
    logger.info("POST /buscar-cliente cnpj=%s nome=%s", req.cnpj, req.nome)
    try:
        cliente = await buscar_cliente_por_cnpj(req.cnpj)
        if cliente:
            logger.info(
                "Cliente encontrado para cnpj=%s id=%s nome=%s",
                req.cnpj,
                cliente.get("id"),
                cliente.get("nome"),
            )
            return JSONResponse(status_code=200, content={"encontrado": True, "cliente": cliente})

        sugestoes = []
        if req.nome:
            sugestoes = await buscar_cliente_por_nome(req.nome, limite=5)
        logger.info("Cliente nao encontrado para cnpj=%s; sugestoes=%d", req.cnpj, len(sugestoes))
        return JSONResponse(
            status_code=200,
            content={
                "encontrado": False,
                "sugestoes": sugestoes,
                "mensagem": f"CNPJ {req.cnpj} nao encontrado.",
            },
        )
    except RuntimeError as e:
        logger.warning("Erro de negocio em /buscar-cliente: %s", e)
        return JSONResponse(status_code=400, content={"erro": str(e)})
    except Exception as e:
        logger.exception("Erro inesperado em /buscar-cliente")
        return JSONResponse(status_code=500, content={"erro": str(e)})


@app.post("/importar")
async def importar(req: ImportarRequest):
    if _browser is None or not _browser.is_connected():
        logger.warning("Automacao indisponivel no momento da importacao.")
        return JSONResponse(status_code=503, content={"erro": "Automacao nao disponivel."})

    # Resolve xml_files e customer_name a partir do formato recebido
    if req.xml is not None:
        xml_b64 = base64.b64encode(req.xml.encode("utf-8")).decode()
        xml_files = [{"name": _extrair_nome_nfe(req.xml), "content_base64": xml_b64}]
        customer_name = req.customer_name or _extrair_nome_emitente(req.xml) or req.customer_id
    elif req.xml_files:
        xml_files = [item.model_dump() for item in req.xml_files]
        customer_name = req.customer_name or req.customer_id
    elif req.xml_contents:
        xml_files = [
            {"name": f"documento_{i + 1}.xml", "content_base64": c}
            for i, c in enumerate(req.xml_contents)
        ]
        customer_name = req.customer_name or req.customer_id
    else:
        return JSONResponse(status_code=400, content={"erro": "Nenhum XML fornecido."})

    logger.info(
        "POST /importar customer_id=%s customer_name=%s corporation=%s arquivos=%d",
        req.customer_id,
        customer_name,
        req.corporation_id,
        len(xml_files),
    )

    resultado = await importar_xml(
        browser=_browser,
        customer_id=req.customer_id,
        customer_name=customer_name,
        corporation_name=req.corporation_id,
        xml_files=xml_files,
    )
    status = 200 if resultado.get("sucesso") else 422
    logger.info(
        "Resposta /importar status=%s sucesso=%s lote=%s erro=%s passo=%s",
        status,
        resultado.get("sucesso"),
        resultado.get("lote"),
        resultado.get("erro"),
        resultado.get("passo"),
    )
    return JSONResponse(status_code=status, content=resultado)


_NFE_NS = "http://www.portalfiscal.inf.br/nfe"


def _extrair_nome_emitente(xml_str: str) -> str | None:
    try:
        root = ET.fromstring(xml_str)
        el = root.find(f".//{{{_NFE_NS}}}emit/{{{_NFE_NS}}}xNome")
        if el is not None and el.text:
            return el.text.strip()
    except Exception:
        pass
    return None


def _extrair_nome_nfe(xml_str: str) -> str:
    try:
        root = ET.fromstring(xml_str)
        el = root.find(f".//{{{_NFE_NS}}}nNF")
        if el is not None and el.text:
            return f"nfe_{el.text.strip()}.xml"
    except Exception:
        pass
    return "documento.xml"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        loop="asyncio",
        access_log=True,
    )
