# DataMart Copilot

Intelligent datamart development assistant powered by Claude (Kimball methodology).

## Stack
- **AI**: Claude claude-opus-4-7 with adaptive thinking + tool use + prompt caching
- **UI**: Streamlit multi-page app (`app.py`)
- **DB**: Azure SQL (`pyodbc`/SQLAlchemy) + PostgreSQL (`psycopg2`)
- **Modeling**: dbt-core, dbt-sqlserver, dbt-postgres
- **Schemas**: Pydantic v2

## Run
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY + DB credentials
streamlit run app.py
```

## Project Layout
```
src/
  models/schemas.py       # Pydantic models (StarSchema, FactTable, DimensionTable, etc.)
  connectors/
    azure_sql.py          # Azure SQL Server connector (SQLAlchemy + pyodbc)
    postgresql.py         # PostgreSQL connector (SQLAlchemy + psycopg2)
    adf_parser.py         # Azure Data Factory JSON parser
  analyzers/
    dw_analyzer.py        # Classify tables, Bus Matrix, conformed dimension detection
  generators/
    star_schema.py        # Kimball star schema designer
    dbt_generator.py      # dbt model + YAML generator (staging/mart layers)
    sql_generator.py      # DDL, ETL SQL, SCD2 MERGE, analytical query generator
  agents/
    copilot_agent.py      # Claude agent with 9 tools, streaming, conversation history
app.py                    # Streamlit UI (Chat, Connections, ADF, Explorer, Models)
```

## Agent Tools
| Tool | Purpose |
|------|---------|
| `get_database_tables` | List tables with row counts |
| `get_table_schema` | Columns, types, PKs, FKs |
| `get_table_sample` | Sample rows |
| `get_existing_datamarts` | Bus Matrix + conformed dimensions |
| `analyze_data_patterns` | Cardinality, nulls, ranges |
| `parse_adf_pipeline` | ADF JSON lineage |
| `generate_star_schema_design` | Kimball star schema |
| `generate_dbt_models` | dbt staging + mart models |
| `generate_sql_query` | Optimized analytical SQL |

## Key Design Decisions
- Prompt caching on the large Kimball system prompt (saves ~40% tokens on repeated calls)
- Adaptive thinking enabled for complex schema analysis
- Conversation history persisted in session state across UI pages
- DW Analyzer uses heuristics (name patterns + FK counts) to classify tables when no explicit metadata exists
