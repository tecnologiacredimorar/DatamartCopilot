"""
DataMart Copilot — Backend FastAPI
Roda em paralelo ao Streamlit (app.py) sem conflito de porta.
Streamlit: porta 8501  |  FastAPI: porta 8000
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(
    title="DataMart Copilot API",
    description="Backend do DataMart Copilot — Credimorar",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS: localhost para dev, regex para capturar todos os previews do Vercel.
# allow_origin_regex complementa allow_origins — ambos são verificados.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",   # React / Vite padrão
        "http://localhost:5173",   # Vite alternativo
        "http://localhost:4173",   # Vite preview
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",  # todos os deploys Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── /health ────────────────────────────────────────────────────────────────

@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    """Confirma que a API está no ar. Usado pelo Azure App Service health check."""
    return {"status": "ok"}


# ─── /test-db ────────────────────────────────────────────────────────────────

@app.get("/test-db", tags=["infra"])
def test_db() -> dict[str, Any]:
    """
    Valida que o ODBC Driver 18 está instalado e que as credenciais do Azure SQL
    funcionam dentro do container.

    Lê as variáveis AZURE_SQL_SERVER, AZURE_SQL_DATABASE,
    AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD do ambiente (via .env ou
    variáveis de ambiente do App Service).
    """
    server   = os.getenv("AZURE_SQL_SERVER", "")
    database = os.getenv("AZURE_SQL_DATABASE", "")
    username = os.getenv("AZURE_SQL_USERNAME", "")
    password = os.getenv("AZURE_SQL_PASSWORD", "")

    missing = [k for k, v in {
        "AZURE_SQL_SERVER":   server,
        "AZURE_SQL_DATABASE": database,
        "AZURE_SQL_USERNAME": username,
        "AZURE_SQL_PASSWORD": password,
    }.items() if not v]

    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Variáveis não configuradas: {', '.join(missing)}",
        )

    try:
        from src.connectors.azure_sql import AzureSQLConnector
        connector = AzureSQLConnector(server, database, username, password)
        connector.connect()
        tables = connector.get_tables()
        connector.disconnect()
        return {
            "status": "ok",
            "server": server,
            "database": database,
            "tables_found": len(tables),
        }
    except Exception as exc:
        logger.exception("test-db falhou")
        raise HTTPException(status_code=503, detail=f"Falha na conexão: {exc}") from exc


# ─── /test-claude ────────────────────────────────────────────────────────────

@app.get("/test-claude", tags=["infra"])
def test_claude() -> dict[str, str]:
    """
    Valida que ANTHROPIC_API_KEY funciona fazendo um ping mínimo ao Claude.
    Usa claude-haiku-4-5 (modelo mais barato) para manter o custo baixo.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY não configurada no ambiente.",
        )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=32,
            messages=[{"role": "user", "content": "Responda somente: ok"}],
        )
        resposta = msg.content[0].text if msg.content else ""
        return {
            "status": "ok",
            "model": msg.model,
            "response": resposta,
        }
    except Exception as exc:
        logger.exception("test-claude falhou")
        raise HTTPException(
            status_code=503, detail=f"Falha na chamada Claude: {exc}"
        ) from exc
