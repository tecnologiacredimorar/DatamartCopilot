from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="DataMart Copilot",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session state initialization ────────────────────────────────────────────

def _init_state() -> None:
    defaults: dict[str, Any] = {
        "agent": None,
        "connections": {},
        "active_connection": None,
        "adf_pipelines": [],
        "messages": [],
        "schema_cache": {},
        "generated_models": [],
        "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

_init_state()

# ─── Lazy agent factory ───────────────────────────────────────────────────────

def get_agent():
    from src.agents.copilot_agent import DatamartCopilotAgent

    if st.session_state.agent is None:
        if not st.session_state.api_key:
            st.error("Set your ANTHROPIC_API_KEY in the sidebar or .env file.")
            st.stop()
        st.session_state.agent = DatamartCopilotAgent(st.session_state.api_key)
    return st.session_state.agent


# ─── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🏗️ DataMart Copilot")
    st.caption("Powered by Claude + Kimball Methodology")
    st.divider()

    api_key_input = st.text_input(
        "Anthropic API Key",
        value=st.session_state.api_key,
        type="password",
        help="sk-ant-...",
    )
    if api_key_input != st.session_state.api_key:
        st.session_state.api_key = api_key_input
        st.session_state.agent = None

    st.divider()
    page = st.radio(
        "Navigation",
        [
            "💬 Copilot Chat",
            "🔌 Connections",
            "📋 ADF Analyzer",
            "🗄️ DW Explorer",
            "📦 Generated Models",
        ],
        label_visibility="collapsed",
    )

    if st.session_state.active_connection:
        st.success(f"Connected: {st.session_state.active_connection}")
    else:
        st.info("No database connected")

    if st.button("Reset Conversation", use_container_width=True):
        st.session_state.messages = []
        if st.session_state.agent:
            st.session_state.agent.reset_conversation()
        st.rerun()

# ─── Pages ────────────────────────────────────────────────────────────────────

if page == "💬 Copilot Chat":
    st.header("💬 DataMart Copilot Chat")
    st.caption(
        "Ask anything about your data warehouse. I'll analyze your schema, "
        "design star schemas following Kimball methodology, and generate dbt models."
    )

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ex: Preciso de um datamart de vendas por produto e região..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        agent = get_agent()

        if st.session_state.active_connection and agent._active_connector_name is None:
            conn_name = st.session_state.active_connection
            if conn_name in st.session_state.connections:
                agent.add_connector(conn_name, st.session_state.connections[conn_name])

        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            full_response = ""
            with st.spinner("Thinking..."):
                try:
                    for chunk in agent.chat(prompt):
                        full_response += chunk
                        response_placeholder.markdown(full_response + "▌")
                    response_placeholder.markdown(full_response)
                except Exception as e:
                    full_response = f"Error: {e}"
                    response_placeholder.error(full_response)

        st.session_state.messages.append({"role": "assistant", "content": full_response})

    with st.expander("💡 Suggested prompts", expanded=False):
        suggestions = [
            "Analise meu data warehouse e mostre o Bus Matrix atual",
            "Crie um datamart de vendas com dimensões de produto, cliente e tempo",
            "Esta solicitação pode ser unida a algum datamart existente? Por quê?",
            "Gere os modelos dbt completos para o datamart de pedidos",
            "Quais KPIs posso extrair das tabelas de faturamento?",
            "Mostre as oportunidades de dimensões conformadas no DW",
        ]
        cols = st.columns(2)
        for i, sug in enumerate(suggestions):
            if cols[i % 2].button(sug, key=f"sug_{i}", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": sug})
                st.rerun()

elif page == "🔌 Connections":
    st.header("🔌 Database Connections")

    tab1, tab2 = st.tabs(["Azure SQL", "PostgreSQL"])

    with tab1:
        st.subheader("Azure SQL Server")
        with st.form("azure_sql_form"):
            col1, col2 = st.columns(2)
            name = col1.text_input("Connection Name", value="azure_prod")
            server = col2.text_input("Server", placeholder="server.database.windows.net")
            db = col1.text_input("Database")
            username = col2.text_input("Username")
            password = st.text_input("Password", type="password")
            driver = st.selectbox(
                "ODBC Driver",
                ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"],
            )
            submitted = st.form_submit_button("Connect", use_container_width=True)

        if submitted:
            try:
                from src.connectors.azure_sql import AzureSQLConnector
                connector = AzureSQLConnector(server, db, username, password, driver)
                with st.spinner("Connecting..."):
                    connector.connect()
                st.session_state.connections[name] = connector
                st.session_state.active_connection = name
                agent = get_agent()
                agent.add_connector(name, connector)
                st.success(f"Connected to {name}!")
            except Exception as e:
                st.error(f"Connection failed: {e}")

    with tab2:
        st.subheader("PostgreSQL")
        with st.form("postgres_form"):
            col1, col2 = st.columns(2)
            pg_name = col1.text_input("Connection Name", value="postgres_dev")
            pg_host = col2.text_input("Host", placeholder="localhost")
            pg_port = col1.number_input("Port", value=5432, step=1)
            pg_db = col2.text_input("Database")
            pg_user = col1.text_input("Username")
            pg_pass = col2.text_input("Password", type="password")
            pg_submitted = st.form_submit_button("Connect", use_container_width=True)

        if pg_submitted:
            try:
                from src.connectors.postgresql import PostgreSQLConnector
                connector = PostgreSQLConnector(pg_host, int(pg_port), pg_db, pg_user, pg_pass)
                with st.spinner("Connecting..."):
                    connector.connect()
                st.session_state.connections[pg_name] = connector
                st.session_state.active_connection = pg_name
                agent = get_agent()
                agent.add_connector(pg_name, connector)
                st.success(f"Connected to {pg_name}!")
            except Exception as e:
                st.error(f"Connection failed: {e}")

    if st.session_state.connections:
        st.divider()
        st.subheader("Active Connections")
        for cname in st.session_state.connections:
            col1, col2 = st.columns([3, 1])
            col1.write(f"**{cname}**")
            if col2.button("Activate", key=f"act_{cname}"):
                st.session_state.active_connection = cname
                agent = get_agent()
                agent.set_active_connector(cname)
                st.rerun()

elif page == "📋 ADF Analyzer":
    st.header("📋 Azure Data Factory Analyzer")
    st.caption("Upload ADF pipeline JSON files to analyze data lineage and flows.")

    uploaded = st.file_uploader(
        "Upload ADF Pipeline JSON",
        type="json",
        accept_multiple_files=True,
    )

    if uploaded:
        from src.connectors.adf_parser import ADFParser
        parser = ADFParser()

        for file in uploaded:
            try:
                content = json.loads(file.read())
                pipeline = parser.parse_pipeline(content)
                st.session_state.adf_pipelines.append(pipeline)

                if st.session_state.agent:
                    st.session_state.agent.add_adf_pipeline(pipeline)

                with st.expander(f"Pipeline: **{pipeline.name}**", expanded=True):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Activities", len(pipeline.activities))
                    col2.metric("Sources", len(pipeline.sources))
                    col3.metric("Sinks", len(pipeline.sinks))

                    if pipeline.description:
                        st.caption(pipeline.description)

                    if pipeline.activities:
                        st.subheader("Activities")
                        for act in pipeline.activities:
                            dep_str = f" → depends on: {', '.join(act.depends_on)}" if act.depends_on else ""
                            st.markdown(f"- **{act.name}** `[{act.activity_type}]`{dep_str}")

                    col_s, col_k = st.columns(2)
                    if pipeline.sources:
                        col_s.subheader("Sources")
                        for s in pipeline.sources:
                            col_s.markdown(f"- `{s}`")
                    if pipeline.sinks:
                        col_k.subheader("Sinks")
                        for s in pipeline.sinks:
                            col_k.markdown(f"- `{s}`")

            except Exception as e:
                st.error(f"Failed to parse {file.name}: {e}")

    if st.session_state.adf_pipelines:
        st.divider()
        st.info(f"{len(st.session_state.adf_pipelines)} pipeline(s) loaded. Ask the Copilot about them!")

elif page == "🗄️ DW Explorer":
    st.header("🗄️ Data Warehouse Explorer")

    if not st.session_state.active_connection:
        st.warning("Connect a database first (Connections page).")
    else:
        connector = st.session_state.connections.get(st.session_state.active_connection)

        if st.button("Introspect Schema", use_container_width=True):
            with st.spinner("Scanning schema..."):
                try:
                    tables = connector.introspect_schema()
                    st.session_state.schema_cache[st.session_state.active_connection] = tables
                    st.success(f"Found {len(tables)} tables.")
                except Exception as e:
                    st.error(f"Error: {e}")

        tables = st.session_state.schema_cache.get(st.session_state.active_connection, [])

        if tables:
            from src.analyzers.dw_analyzer import DWAnalyzer
            analyzer = DWAnalyzer(tables)
            analyzer.classify_tables()
            summary = analyzer.get_schema_summary()

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Tables", summary["total_tables"])
            col2.metric("Fact Tables", len(summary["fact_tables"]))
            col3.metric("Dimension Tables", len(summary["dimension_tables"]))
            col4.metric("Conformed Dims", len(summary["conformed_dimensions"]))

            tab1, tab2, tab3, tab4 = st.tabs(["Fact Tables", "Dimension Tables", "Bus Matrix", "All Tables"])

            with tab1:
                for tname in summary["fact_tables"]:
                    table = next((t for t in tables if t.full_name == tname), None)
                    if table:
                        with st.expander(f"📊 {tname} ({table.row_count or '?'} rows)"):
                            cols_df = [{"Column": c.name, "Type": c.data_type, "PK": c.is_primary_key, "FK": c.is_foreign_key} for c in table.columns]
                            import pandas as pd
                            st.dataframe(pd.DataFrame(cols_df), use_container_width=True)

            with tab2:
                for tname in summary["dimension_tables"]:
                    table = next((t for t in tables if t.full_name == tname), None)
                    if table:
                        with st.expander(f"📁 {tname}"):
                            cols_df = [{"Column": c.name, "Type": c.data_type, "PK": c.is_primary_key} for c in table.columns]
                            import pandas as pd
                            st.dataframe(pd.DataFrame(cols_df), use_container_width=True)

            with tab3:
                bus_matrix = analyzer.suggest_bus_matrix()
                if bus_matrix:
                    import pandas as pd
                    df = pd.DataFrame(bus_matrix).T.fillna(False)
                    df = df.replace({True: "✓", False: ""})
                    st.dataframe(df, use_container_width=True)
                else:
                    st.info("No fact/dimension relationships detected yet.")

            with tab4:
                import pandas as pd
                all_data = [
                    {
                        "Schema": t.schema_name,
                        "Table": t.table_name,
                        "Columns": len(t.columns),
                        "Rows": t.row_count or "?",
                        "Type": "Fact" if t.is_fact_table else ("Dimension" if t.is_dimension_table else "Other"),
                    }
                    for t in tables
                ]
                st.dataframe(pd.DataFrame(all_data), use_container_width=True)

elif page == "📦 Generated Models":
    st.header("📦 Generated Models")

    agent = st.session_state.agent
    if not agent or not agent._star_schemas:
        st.info("No models generated yet. Use the Copilot Chat to design a datamart.")
    else:
        for schema_name, star_schema in agent._star_schemas.items():
            with st.expander(f"⭐ {schema_name}", expanded=True):
                st.subheader("Fact Table")
                st.code(f"Table: {star_schema.fact_table.name}\nGrain: {star_schema.fact_table.grain}", language="yaml")

                st.subheader("Dimensions")
                for dim in star_schema.dimension_tables:
                    st.markdown(f"- **{dim.name}** (SCD {dim.scd_type.value}) — {dim.description}")

                if star_schema.kpi_opportunities:
                    st.subheader("KPI Opportunities")
                    for kpi in star_schema.kpi_opportunities:
                        st.markdown(f"- {kpi}")

                from src.generators.dbt_generator import DBTGenerator
                from src.generators.sql_generator import SQLGenerator

                col1, col2 = st.columns(2)
                if col1.button(f"Generate dbt Models", key=f"dbt_{schema_name}"):
                    gen = DBTGenerator()
                    project = gen.generate_project(star_schema, schema_name.lower().replace(" ", "_"))
                    for model in project.models:
                        st.subheader(f"📄 {model.name}.sql ({model.layer})")
                        st.code(model.sql_content, language="sql")
                        if model.schema_yaml:
                            st.subheader(f"📄 schema.yml")
                            st.code(model.schema_yaml, language="yaml")

                if col2.button(f"Generate DDL", key=f"ddl_{schema_name}"):
                    gen = SQLGenerator()
                    st.subheader("Fact Table DDL")
                    st.code(gen.generate_fact_ddl(star_schema.fact_table), language="sql")
                    for dim in star_schema.dimension_tables:
                        st.subheader(f"Dimension DDL: {dim.name}")
                        st.code(gen.generate_dimension_ddl(dim), language="sql")
