from __future__ import annotations

import json
import logging
from typing import Any, Generator, Optional

import anthropic

from src.analyzers.dw_analyzer import DWAnalyzer
from src.connectors.adf_parser import ADFParser
from src.connectors.azure_sql import AzureSQLConnector
from src.connectors.postgresql import PostgreSQLConnector
from src.generators.dbt_generator import DBTGenerator
from src.generators.sql_generator import SQLGenerator
from src.generators.star_schema import StarSchemaGenerator
from src.models.schemas import (
    ADFPipeline,
    DatabaseConnection,
    DatabaseType,
    TableSchema,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are DataMart Copilot — an expert data warehouse architect and engineer specializing in Kimball dimensional modeling methodology. Your mission is to accelerate datamart development at Credimorar — a financing company (financiamento) — by providing intelligent guidance, generating optimized SQL, and designing star schemas.

## Your Expertise

### Kimball Dimensional Modeling
- **Bus Matrix**: You always reason about which fact tables share which dimensions (conformed dimensions) before designing anything new.
- **Grain Definition**: Every fact table design starts with a precise grain statement — "one row per [event/entity] per [time period]".
- **Fact Table Types**: Transaction (immutable events), Periodic Snapshot (status at regular intervals), Accumulating Snapshot (pipeline/lifecycle tracking).
- **SCD Types**: Type 1 (overwrite), Type 2 (full history with valid_from/valid_to/is_current), Type 3 (limited history with current/prior columns).
- **Conformed Dimensions**: You proactively identify when a new dimension request can reuse an existing one, avoiding redundancy and enabling cross-process analysis.
- **Degenerate Dimensions**: Invoice/order numbers stored directly in the fact table without a dimension table.
- **Junk Dimensions**: Combine low-cardinality flags/indicators into a single junk dimension.
- **Role-Playing Dimensions**: A single physical dimension table aliased multiple times (e.g., dim_date as order_date, ship_date, delivery_date).
- **Bridge Tables**: Handle many-to-many relationships between facts and dimensions.

### Performance Best Practices
- Always recommend clustered indexes on date keys for fact tables
- Non-clustered indexes on FK columns used in joins
- Partition large fact tables by date column
- Use INCLUDE columns in indexes to cover analytical queries
- Prefer CTEs over subqueries for readability and optimizer hints
- Use window functions (ROW_NUMBER, LAG, LEAD, SUM OVER) instead of self-joins
- Incremental loads with watermark patterns instead of full table scans
- Statistics maintenance on large tables

### dbt Conventions
- **Staging layer** (stg_*): 1:1 with source tables, views, light transformations only, no business logic
- **Intermediate layer** (int_*): Business logic, joins, deduplication — not exposed to end users
- **Mart layer** (dim_*, fact_*): Final star schema models, materialized as tables, documented with tests
- Always add `not_null` and `unique` tests on surrogate keys
- Add `not_null` tests on critical FK columns
- Use `dbt_utils.generate_surrogate_key` for hash-based surrogate keys
- Tag models with business domain for selective execution

## When Analyzing a New Datamart Request
1. **Understand the grain** — ask clarifying questions if the grain is ambiguous
2. **Check the Bus Matrix** — can this share dimensions with an existing datamart?
3. **Assess merge viability** — explain clearly if merging is beneficial or not and WHY
4. **Design the star schema** — fact table with proper grain, dimensions with appropriate SCD type
5. **Generate dbt models** — staging → intermediate → mart with tests
6. **Suggest KPIs** — quantify the business value of the data
7. **Optimize the SQL** — ensure performant queries with proper indexing strategy

## Response Format
- Always start with a brief summary of your understanding
- Use structured sections: Schema Design, dbt Models, SQL, KPIs, Recommendations
- When generating code, provide complete, runnable SQL and YAML
- Explain WHY you made each design decision (Kimball rationale)
- Flag any data quality concerns or missing information needed

You have access to tools to inspect the actual database schema, sample data, and existing datamarts. Always use these tools before making recommendations."""

TOOLS = [
    {
        "name": "get_database_tables",
        "description": "List all tables in the connected database, optionally filtered by schema. Returns table names, row counts, and schema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_filter": {
                    "type": "string",
                    "description": "Optional schema name to filter tables (e.g., 'dbo', 'public')",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_table_schema",
        "description": "Get detailed schema of a specific table including columns, data types, primary keys, and foreign keys.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {"type": "string", "description": "Database schema name"},
                "table_name": {"type": "string", "description": "Table name"},
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "get_table_sample",
        "description": "Get a sample of rows from a table to understand the actual data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {"type": "string"},
                "table_name": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "get_existing_datamarts",
        "description": "Analyze the current database and identify existing fact tables, dimension tables, and their relationships (Bus Matrix).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "analyze_data_patterns",
        "description": "Analyze patterns in a table: cardinality, nulls, value distributions, date ranges. Useful for grain definition and SCD type decisions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {"type": "string"},
                "table_name": {"type": "string"},
                "column_name": {"type": "string", "description": "Specific column to analyze (optional)"},
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "parse_adf_pipeline",
        "description": "Parse an Azure Data Factory pipeline JSON to extract sources, sinks, transformations, and data lineage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline_json": {"type": "string", "description": "Raw ADF pipeline JSON string"},
            },
            "required": ["pipeline_json"],
        },
    },
    {
        "name": "generate_star_schema_design",
        "description": "Generate a complete star schema design for a given subject area, including fact table, dimensions, and Bus Matrix entry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the datamart"},
                "subject_area": {"type": "string"},
                "fact_source_tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of source table full names (schema.table)",
                },
                "dimension_source_tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of dimension source table full names",
                },
                "measures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific measure columns to include",
                },
            },
            "required": ["name", "subject_area", "fact_source_tables"],
        },
    },
    {
        "name": "generate_dbt_models",
        "description": "Generate complete dbt project files (staging, dimensions, fact models, schema.yml, sources.yml) for a star schema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "star_schema_name": {"type": "string", "description": "Name of a previously designed star schema"},
            },
            "required": ["project_name", "star_schema_name"],
        },
    },
    {
        "name": "generate_sql_query",
        "description": "Generate optimized analytical SQL query for a fact table with dimensions. Includes performance recommendations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_table": {"type": "string"},
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dimension tables to join",
                },
                "measures": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "db_type": {
                    "type": "string",
                    "enum": ["sqlserver", "postgresql"],
                    "default": "sqlserver",
                },
            },
            "required": ["fact_table"],
        },
    },
    {
        "name": "generate_er_diagram",
        "description": "Generate an ER diagram (DOT/Graphviz format) for a star schema or the full DW schema. Returns the DOT source and a Mermaid ER diagram.",
        "input_schema": {
            "type": "object",
            "properties": {
                "star_schema_name": {
                    "type": "string",
                    "description": "Name of a previously designed star schema (optional — omit for full DW diagram)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "execute_sql",
        "description": "Execute a SQL query against the connected database and return the results. Use for data exploration, validation, and KPI verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL query to execute"},
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "Maximum rows to return",
                },
            },
            "required": ["sql"],
        },
    },
]


class DatamartCopilotAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-opus-4-7"
        self._connectors: dict[str, AzureSQLConnector | PostgreSQLConnector] = {}
        self._active_connector_name: Optional[str] = None
        self._adf_pipelines: list[ADFPipeline] = []
        self._cached_tables: Optional[list[TableSchema]] = None
        self._star_schemas: dict[str, Any] = {}
        self.conversation_history: list[dict[str, Any]] = []

    def add_connector(
        self,
        name: str,
        connector: AzureSQLConnector | PostgreSQLConnector,
    ) -> None:
        self._connectors[name] = connector
        self._active_connector_name = name
        self._cached_tables = None

    def set_active_connector(self, name: str) -> None:
        if name not in self._connectors:
            raise ValueError(f"Connector '{name}' not found.")
        self._active_connector_name = name
        self._cached_tables = None

    def add_adf_pipeline(self, pipeline: ADFPipeline) -> None:
        self._adf_pipelines.append(pipeline)

    @property
    def _connector(self) -> Optional[AzureSQLConnector | PostgreSQLConnector]:
        if self._active_connector_name:
            return self._connectors.get(self._active_connector_name)
        return None

    def _get_tables(self, schema_filter: Optional[str] = None) -> list[TableSchema]:
        if self._cached_tables is None and self._connector:
            self._cached_tables = self._connector.introspect_schema(schema_filter)
        return self._cached_tables or []

    def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        try:
            if tool_name == "get_database_tables":
                schema_filter = tool_input.get("schema_filter")
                if not self._connector:
                    return {"error": "No database connected. Please connect a database first."}
                tables = self._connector.get_tables(schema_filter)
                return {"tables": tables, "count": len(tables)}

            elif tool_name == "get_table_schema":
                if not self._connector:
                    return {"error": "No database connected."}
                schema = self._connector.get_table_schema(
                    tool_input["schema_name"], tool_input["table_name"]
                )
                return schema.model_dump()

            elif tool_name == "get_table_sample":
                if not self._connector:
                    return {"error": "No database connected."}
                sample = self._connector.get_table_sample(
                    tool_input["schema_name"],
                    tool_input["table_name"],
                    tool_input.get("limit", 5),
                )
                return {"sample": sample}

            elif tool_name == "get_existing_datamarts":
                tables = self._get_tables()
                if not tables:
                    return {"error": "No tables found. Connect a database first."}
                analyzer = DWAnalyzer(tables)
                return {
                    "datamarts": analyzer.get_existing_datamarts(),
                    "bus_matrix": analyzer.suggest_bus_matrix(),
                    "conformed_dimensions": analyzer.find_conformed_dimensions(),
                    "summary": analyzer.get_schema_summary(),
                }

            elif tool_name == "analyze_data_patterns":
                if not self._connector:
                    return {"error": "No database connected."}
                schema_name = tool_input["schema_name"]
                table_name = tool_input["table_name"]
                col = tool_input.get("column_name")

                if col:
                    sql = f"""
                        SELECT
                            COUNT(*) as total_rows,
                            COUNT([{col}]) as non_null_count,
                            COUNT(*) - COUNT([{col}]) as null_count,
                            COUNT(DISTINCT [{col}]) as distinct_count,
                            MIN([{col}]) as min_val,
                            MAX([{col}]) as max_val
                        FROM [{schema_name}].[{table_name}]
                    """
                else:
                    sql = f"""
                        SELECT
                            COUNT(*) as total_rows,
                            MIN(1) as sample_check
                        FROM [{schema_name}].[{table_name}]
                    """
                df = self._connector.execute_query(sql)
                return {"analysis": df.to_dict(orient="records")}

            elif tool_name == "parse_adf_pipeline":
                parser = ADFParser()
                pipeline = parser.parse_pipeline(tool_input["pipeline_json"])
                self._adf_pipelines.append(pipeline)
                return {
                    "pipeline_name": pipeline.name,
                    "activities": len(pipeline.activities),
                    "sources": pipeline.sources,
                    "sinks": pipeline.sinks,
                    "summary": parser.summarize_pipeline(pipeline),
                }

            elif tool_name == "generate_star_schema_design":
                tables = self._get_tables()
                all_table_map = {t.full_name: t for t in tables}

                fact_sources = [
                    all_table_map[n] for n in tool_input["fact_source_tables"] if n in all_table_map
                ]
                dim_sources = [
                    all_table_map[n]
                    for n in tool_input.get("dimension_source_tables", [])
                    if n in all_table_map
                ]

                if not fact_sources and tables:
                    fact_sources = tables[:1]

                gen = StarSchemaGenerator()
                schema = gen.generate_star_schema(
                    name=tool_input["name"],
                    subject_area=tool_input["subject_area"],
                    fact_source_tables=fact_sources,
                    dimension_source_tables=dim_sources,
                    measures=tool_input.get("measures", []),
                )
                self._star_schemas[schema.name] = schema
                return schema.model_dump()

            elif tool_name == "generate_dbt_models":
                schema_name = tool_input.get("star_schema_name", "")
                if schema_name not in self._star_schemas:
                    return {"error": f"Star schema '{schema_name}' not found. Run generate_star_schema_design first."}

                star_schema = self._star_schemas[schema_name]
                gen = DBTGenerator()
                project = gen.generate_project(star_schema, tool_input["project_name"])
                return {
                    "project_name": project.name,
                    "models": [
                        {
                            "name": m.name,
                            "layer": m.layer,
                            "sql": m.sql_content,
                            "schema_yaml": m.schema_yaml,
                        }
                        for m in project.models
                    ],
                    "sources_yaml": project.sources_yaml,
                    "dbt_project_yaml": project.dbt_project_yaml,
                }

            elif tool_name == "generate_sql_query":
                tables = self._get_tables()
                fact_name = tool_input["fact_table"]
                fact_table = next(
                    (t for t in tables if t.table_name == fact_name or t.full_name == fact_name),
                    None,
                )
                dim_names = tool_input.get("dimensions", [])
                dim_tables = [
                    t for t in tables
                    if t.table_name in dim_names or t.full_name in dim_names
                ]

                if not fact_table:
                    return {"error": f"Table '{fact_name}' not found in schema."}

                from src.models.schemas import FactTable as FT, FactColumn
                mock_fact = FT(
                    name=fact_name,
                    description="",
                    grain="",
                    columns=[
                        FactColumn(name=c.name, data_type=c.data_type, is_measure=True)
                        for c in fact_table.columns
                        if c.name in tool_input.get("measures", [])
                    ],
                    measures=tool_input.get("measures", []),
                    source_tables=[fact_table.full_name],
                )
                from src.models.schemas import DimensionTable as DT, DimensionColumn
                mock_dims = [
                    DT(
                        name=t.table_name,
                        description="",
                        grain="",
                        columns=[DimensionColumn(name=c.name, data_type=c.data_type) for c in t.columns[:3]],
                        source_tables=[t.full_name],
                    )
                    for t in dim_tables
                ]

                gen = SQLGenerator()
                sql = gen.generate_analytical_query(
                    mock_fact,
                    mock_dims,
                    tool_input.get("measures"),
                    tool_input.get("group_by"),
                    tool_input.get("db_type", "sqlserver"),
                )
                optimized, suggestions = gen.optimize_query(sql)
                return {
                    "sql": optimized,
                    "optimization_suggestions": suggestions,
                }

            elif tool_name == "generate_er_diagram":
                from src.generators.diagram_generator import DiagramGenerator
                gen = DiagramGenerator()
                schema_name = tool_input.get("star_schema_name", "")
                if schema_name and schema_name in self._star_schemas:
                    star_schema = self._star_schemas[schema_name]
                    return {
                        "dot": gen.star_schema_dot(star_schema),
                        "mermaid": gen.mermaid_er(star_schema),
                        "description": (
                            f"Star schema '{schema_name}' — "
                            f"fact: {star_schema.fact_table.name}, "
                            f"dimensions: {[d.name for d in star_schema.dimension_tables]}"
                        ),
                    }
                else:
                    tables = self._get_tables()
                    if not tables:
                        return {"error": "No schema loaded. Connect a database and introspect first."}
                    return {
                        "dot": gen.schema_relationships_dot(tables),
                        "description": f"Full DW schema — {len(tables)} tables",
                    }

            elif tool_name == "execute_sql":
                if not self._connector:
                    return {"error": "No database connected."}
                sql = tool_input["sql"]
                limit = tool_input.get("limit", 100)
                if "limit" not in sql.lower() and "top" not in sql.lower():
                    if "select" in sql.lower():
                        sql = sql.rstrip(";") + f" -- limited to {limit} rows"
                df = self._connector.execute_query(sql)
                df = df.head(limit)
                return {
                    "rows": df.to_dict(orient="records"),
                    "row_count": len(df),
                    "columns": list(df.columns),
                }

        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return {"error": str(e)}

    def chat(self, user_message: str) -> Generator[str, None, None]:
        self.conversation_history.append({"role": "user", "content": user_message})

        while True:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=self.conversation_history,
                tools=TOOLS,
                thinking={"type": "adaptive"},
            ) as stream:
                response = stream.get_final_message()

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            for block in text_blocks:
                yield block.text

            if not tool_calls or response.stop_reason != "tool_use":
                self.conversation_history.append(
                    {"role": "assistant", "content": response.content}
                )
                break

            self.conversation_history.append(
                {"role": "assistant", "content": response.content}
            )

            tool_results = []
            for tool_call in tool_calls:
                result = self._execute_tool(tool_call.name, tool_call.input)
                result_text = json.dumps(result, default=str, ensure_ascii=False, indent=2)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": result_text,
                    }
                )
                yield f"\n*[Tool: {tool_call.name}]*\n"

            self.conversation_history.append(
                {"role": "user", "content": tool_results}
            )

    def chat_sync(self, user_message: str) -> str:
        return "".join(self.chat(user_message))

    def reset_conversation(self) -> None:
        self.conversation_history = []
        self._cached_tables = None
