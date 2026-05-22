from __future__ import annotations

import json
import logging
from typing import Any

from .brain import KnowledgeBrain
from .llm import call_ai_json

logger = logging.getLogger(__name__)

SYSTEM = """Você é um engenheiro de dados sênior especialista em Azure Data Factory.
Analisa pipelines ADF e explica em linguagem de negócio clara.
Empresa: Credimorar (financiamento).
Responda sempre em português, SOMENTE com JSON válido."""


class ADFAIAnalyzer:
    def __init__(self, brain: KnowledgeBrain):
        self.brain = brain

    def analyze_pipeline(self, pipeline_name: str, pipeline_raw: dict[str, Any]) -> dict[str, Any]:
        """Analyze a single ADF pipeline using AI. Returns structured analysis."""
        brain_ctx = self.brain.build_ai_context()

        # Compact pipeline for token budget
        props = pipeline_raw.get("properties", {})
        activities = props.get("activities", [])
        compact = {
            "name": pipeline_raw.get("name", pipeline_name),
            "description": props.get("description", ""),
            "activities": [
                {
                    "name": a.get("name", ""),
                    "type": a.get("type", ""),
                    "depends_on": [d.get("activity", "") for d in a.get("dependsOn", [])],
                    "inputs": [i.get("referenceName", "") for i in a.get("inputs", [])],
                    "outputs": [o.get("referenceName", "") for o in a.get("outputs", [])],
                }
                for a in activities[:20]
            ],
        }

        # Keep context short to stay under 4000 tokens
        ctx_snippet = brain_ctx[:800] if brain_ctx else ""

        prompt = f"""Contexto do DW:
{ctx_snippet}

Pipeline ADF para analisar:
{json.dumps(compact, ensure_ascii=False, indent=2)[:2000]}

Analise e retorne JSON com exatamente estas chaves:
{{
  "business_description": "O que este pipeline faz em linguagem de negócio (2-4 frases)",
  "load_strategy": "full_load|incremental|unknown",
  "watermark_column": "nome da coluna usada como marca d'água ou null",
  "source_tables": ["lista de tabelas/datasets lidos"],
  "target_tables": ["lista de tabelas/datasets escritos"],
  "problems_found": ["problema 1", "problema 2"],
  "suggestions": ["sugestão 1", "sugestão 2"]
}}"""

        try:
            analysis = call_ai_json(prompt, SYSTEM, max_tokens=1500)
            # Ensure lists are lists
            for field in ("source_tables", "target_tables", "problems_found", "suggestions"):
                if not isinstance(analysis.get(field), list):
                    analysis[field] = []
            return analysis
        except Exception as e:
            logger.error("ADF analysis failed for %s: %s", pipeline_name, e)
            return {
                "business_description": f"Análise falhou: {e}",
                "load_strategy": "unknown",
                "watermark_column": None,
                "source_tables": [],
                "target_tables": [],
                "problems_found": [str(e)],
                "suggestions": [],
            }

    def analyze_and_save(self, pipeline_name: str, pipeline_raw: dict[str, Any]) -> dict[str, Any]:
        """Analyze pipeline and persist to brain."""
        self.brain.save_adf_pipeline(pipeline_name, pipeline_raw)
        analysis = self.analyze_pipeline(pipeline_name, pipeline_raw)
        self.brain.save_adf_analysis(pipeline_name, analysis)
        return analysis

    def generate_table_descriptions(
        self, tables: list[dict[str, Any]], batch_size: int = 8
    ) -> list[dict[str, Any]]:
        """Generate AI descriptions for multiple tables in batches."""
        brain_ctx = self.brain.build_ai_context()
        ctx_snippet = brain_ctx[:600]
        results = []

        for i in range(0, len(tables), batch_size):
            batch = tables[i : i + batch_size]
            table_list = "\n".join(
                f"- {t['full_name']} ({t.get('classification','?')}): "
                f"colunas: {', '.join(c['column_name'] for c in t.get('columns', [])[:8])}"
                for t in batch
            )
            prompt = f"""Contexto: {ctx_snippet}

Tabelas do DW para descrever:
{table_list}

Para cada tabela, gere uma descrição em 1-2 frases em português.
Retorne JSON:
{{
  "descriptions": [
    {{"full_name": "schema.table", "description": "..."}},
    ...
  ]
}}"""
            try:
                result = call_ai_json(prompt, SYSTEM, max_tokens=1200)
                results.extend(result.get("descriptions", []))
            except Exception as e:
                logger.error("Batch description failed: %s", e)
        return results
