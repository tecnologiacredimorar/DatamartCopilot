from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .brain import KnowledgeBrain
from .llm import call_ai_json, call_ai

logger = logging.getLogger(__name__)

SYSTEM = """Você é um arquiteto de dados sênior especialista em Kimball dimensional modeling.
Trabalha na Credimorar (empresa de financiamento).
DW no Azure SQL alimentado por pipelines ADF lendo do PostgreSQL. Consumo final no Power BI.
Responda em português. NUNCA assuma decisões de negócio — pergunte antes de continuar.
Quando retornar JSON, retorne SOMENTE JSON válido."""


class DatamartAccelerator:
    """Structured multi-step flow for new datamart requests."""

    def __init__(self, brain: KnowledgeBrain):
        self.brain = brain

    # ── Step 1: Initial analysis ───────────────────────────────────────────

    def analyze_demand(self, request_id: int) -> dict[str, Any]:
        """
        Analyzes the demand against the brain.
        Returns: gaps, existing_relevant, questions, preliminary design hints.
        """
        req = self.brain.get_request(request_id)
        if not req:
            return {"error": "Solicitação não encontrada."}

        brain_ctx = self.brain.build_ai_context()

        prompt = f"""## Nova Solicitação de Datamart

Título: {req['title']}
Descrição: {req['description_raw']}
{f"Query de referência:{chr(10)}{req['source_query']}" if req.get('source_query') else ''}

## Conhecimento disponível do DW
{brain_ctx[:2500]}

## Sua tarefa
Analise a solicitação e retorne JSON:
{{
  "entendimento": "Resumo do que foi pedido em 2-3 frases",
  "tabelas_relevantes": ["tabela: por que é relevante"],
  "pipelines_relevantes": ["pipeline: o que já carrega"],
  "gaps": ["o que falta no DW para atender essa demanda"],
  "pode_unir_com": "nome do datamart existente ou null",
  "motivo_uniao": "por que pode/não pode unir (Kimball rationale)",
  "grain_preliminar": "uma linha por ...",
  "perguntas": ["pergunta 1 essencial", "pergunta 2 se necessário"],
  "complexidade": "baixa|media|alta"
}}

Máximo 3 perguntas. Só pergunte o que é realmente ambíguo."""

        try:
            result = call_ai_json(prompt, SYSTEM, max_tokens=2000)
            # Persist analysis results to request
            self.brain.update_request(
                request_id,
                status="clarifying" if result.get("perguntas") else "designing",
                brain_context_used=brain_ctx[:1000],
                kimball_notes=result.get("entendimento", ""),
                analysis_json=json.dumps(result, ensure_ascii=False),
            )
            return result
        except Exception as e:
            logger.error("Demand analysis failed: %s", e)
            return {"error": str(e)}

    # ── Step 2: Save clarification answers ────────────────────────────────

    def save_answers(self, request_id: int, qa_list: list[dict[str, str]]) -> None:
        """Persist Q&A pairs and advance status to designing."""
        self.brain.update_request(
            request_id,
            clarification_qa=json.dumps(qa_list, ensure_ascii=False),
            status="designing",
        )

    # ── Step 3: Design star schema ─────────────────────────────────────────

    def design_star_schema(self, request_id: int, analysis: dict[str, Any]) -> dict[str, Any]:
        """Generate the full Kimball star schema design."""
        req = self.brain.get_request(request_id)
        if not req:
            return {"error": "Solicitação não encontrada."}

        # Restore analysis from brain if caller passed an empty dict (e.g. after page reload)
        if not analysis and req.get("analysis_json"):
            persisted = req["analysis_json"]
            analysis = persisted if isinstance(persisted, dict) else {}

        qa_text = ""
        raw_qa = req.get("clarification_qa", [])
        qa_pairs = raw_qa if isinstance(raw_qa, list) else []
        if qa_pairs:
            qa_text = "\n".join(f"P: {q['question']}\nR: {q['answer']}" for q in qa_pairs)

        prompt = f"""## Solicitação
{req['description_raw']}

## Análise prévia
{json.dumps({k: v for k, v in analysis.items() if k != 'perguntas'}, ensure_ascii=False, indent=2)[:1200]}

## Respostas às perguntas
{qa_text or "Sem perguntas pendentes."}

## Tarefa: Design Kimball Star Schema

ATENÇÃO: Retorne SOMENTE o objeto JSON completo abaixo.
NÃO retorne arrays soltos, sub-listas, ou partes do JSON.
O JSON DEVE começar com {{ e terminar com }}.
NÃO use markdown fences.

{{
  "fact_table": {{
    "name": "fact_nome",
    "grain": "uma linha por ...",
    "type": "transaction|periodic_snapshot|accumulating_snapshot",
    "measures": [
      {{"name": "col", "type": "DECIMAL(18,2)", "aggregation": "SUM", "description": "..."}}
    ],
    "degenerate_dimensions": ["col_que_fica_no_fact"],
    "source_tables": ["schema.tabela"]
  }},
  "dimensions": [
    {{
      "name": "dim_nome",
      "grain": "uma linha por ...",
      "scd_type": 1,
      "columns": [
        {{"name": "col", "type": "VARCHAR(100)", "role": "natural_key|attribute|scd_tracking"}}
      ],
      "source_tables": ["schema.tabela"],
      "conformed": true,
      "rationale": "por que SCD tipo X"
    }}
  ],
  "indexes": ["CREATE INDEX ... (recomendação de performance)"],
  "kimball_notes": "decisões e rationale Kimball importantes",
  "merge_with_existing": null,
  "merge_rationale": "por que pode/não pode mergear"
}}"""

        try:
            result = call_ai_json(prompt, SYSTEM, max_tokens=4000)
            # If the AI returned a list wrapped by _ensure_dict, try to unwrap
            if "items" in result and isinstance(result.get("items"), list) and result["items"]:
                first = result["items"][0]
                if isinstance(first, dict) and "fact_table" in first:
                    result = first
            # Persist to SQLite so design survives page refreshes / reruns
            if result.get("fact_table"):
                self.brain.update_request(
                    request_id,
                    design_json=json.dumps(result, ensure_ascii=False),
                    status="designing",
                )
            return result
        except Exception as e:
            logger.error("Star schema design failed: %s", e)
            return {"error": str(e)}

    # ── Step 4: Generate full package ──────────────────────────────────────

    def generate_dbt_models(self, request_id: int, design: dict[str, Any]) -> str:
        """Generate dbt staging + mart models SQL."""
        req = self.brain.get_request(request_id)
        fact = design.get("fact_table", {})
        dims = design.get("dimensions", [])

        prompt = f"""Gere os modelos dbt completos para este datamart.

Fact table: {json.dumps(fact, ensure_ascii=False)[:800]}
Dimensions: {json.dumps(dims, ensure_ascii=False)[:1200]}

Retorne os arquivos dbt em formato texto, separados por:
--- models/staging/stg_NomeFonte.sql ---
[conteúdo SQL]

--- models/marts/dim_nome.sql ---
[conteúdo SQL]

--- models/marts/fact_nome.sql ---
[conteúdo SQL]

--- models/marts/schema.yml ---
[conteúdo YAML com testes]

Use:
- {{{{ source('credimorar', 'tabela') }}}} para fontes
- {{{{ ref('model') }}}} para referências entre modelos
- dbt_utils.generate_surrogate_key para surrogate keys
- Testes: not_null, unique em surrogate keys; not_null em FKs críticas
- Materialização: staging=view, marts=table, fact com incremental se tiver campo de data"""

        try:
            return call_ai(prompt, SYSTEM, max_tokens=3500)
        except Exception as e:
            return f"Erro ao gerar modelos dbt: {e}"

    def generate_adf_pipeline_json(self, request_id: int, design: dict[str, Any]) -> str:
        """Generate ADF pipeline JSON skeleton."""
        fact = design.get("fact_table", {})
        sources = fact.get("source_tables", [])

        prompt = f"""Gere um JSON de pipeline Azure Data Factory para carregar o datamart.

Fact table: {fact.get('name', 'fact_table')}
Source tables: {sources[:5]}
Load type: incremental se existir campo de data, senão full_load

Retorne um JSON válido de pipeline ADF com:
- Uma atividade Copy por tabela fonte
- Dataset references corretos
- Dependências entre atividades
- Parâmetros para watermark se incremental

Use a estrutura padrão ADF: {{"name": "...", "properties": {{"activities": [...]}}}}"""

        try:
            return call_ai(prompt, SYSTEM, max_tokens=2000)
        except Exception as e:
            return f"Erro ao gerar pipeline ADF: {e}"

    def generate_documentation(self, request_id: int, design: dict[str, Any]) -> str:
        """Generate markdown documentation."""
        req = self.brain.get_request(request_id)
        prompt = f"""Gere documentação markdown completa para este datamart.

Solicitação: {req['description_raw'] if req else ''}
Design: {json.dumps(design, ensure_ascii=False)[:1500]}

Inclua:
# Datamart: [Nome]
## Objetivo de negócio
## Grain
## Modelo estrela (tabelas e relacionamentos)
## Descrição das métricas
## Descrição das dimensões e SCD
## Queries de exemplo (3 consultas analíticas úteis)
## Checklist de entrega
- [ ] Modelos dbt criados e testados
- [ ] Pipeline ADF configurado
- [ ] Documentação revisada
- [ ] Testes de qualidade executados
- [ ] Validação com usuário final"""

        try:
            return call_ai(prompt, SYSTEM, max_tokens=2500)
        except Exception as e:
            return f"Erro ao gerar documentação: {e}"

    def generate_full_package(self, request_id: int, design: dict[str, Any]) -> dict[str, str]:
        """Generate all artifacts: dbt, ADF JSON, docs, checklist."""
        artifacts = {
            "dbt_models": self.generate_dbt_models(request_id, design),
            "adf_json": self.generate_adf_pipeline_json(request_id, design),
            "documentation": self.generate_documentation(request_id, design),
        }
        self.brain.update_request(
            request_id,
            status="ready",
            artifacts=json.dumps(artifacts, ensure_ascii=False),
        )
        return artifacts
