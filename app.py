from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="DataMart Copilot",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Persistence ──────────────────────────────────────────────────────────────

@st.cache_resource
def get_store():
    from src.persistence.store import CopilotStore
    return CopilotStore()

store = get_store()

# ─── Session state — load from SQLite on first run ────────────────────────────

def _init_state() -> None:
    if "initialized" in st.session_state:
        return

    st.session_state.initialized = True
    # Remove any stale api_key from SQLite (legacy — keys must not be persisted)
    store.set_setting("api_key", "")
    st.session_state.connections = {}          # name -> connector instance
    st.session_state.connection_configs = {}   # name -> config dict (for display)
    st.session_state.active_connection = None
    st.session_state.adf_pipelines = []
    st.session_state.messages = store.load_messages()
    st.session_state.schema_cache = {}
    st.session_state.generated_models = []
    # API key: env var only — never persist to SQLite
    st.session_state.api_key = os.getenv("ANTHROPIC_API_KEY", "")
    st.session_state.agent = None

    # Restore saved connection configs (don't auto-connect — just restore metadata)
    for cfg in store.load_connections():
        st.session_state.connection_configs[cfg["name"]] = cfg

    # Restore active connection name
    last_active = store.get_setting("active_connection", "")
    if last_active and last_active in st.session_state.connection_configs:
        st.session_state.active_connection = last_active

_init_state()

# ─── Lazy agent factory ───────────────────────────────────────────────────────

def get_agent():
    from src.agents.copilot_agent import DatamartCopilotAgent
    if st.session_state.agent is None:
        if not st.session_state.api_key:
            st.error("Informe sua ANTHROPIC_API_KEY na barra lateral ou no arquivo .env.")
            st.stop()
        st.session_state.agent = DatamartCopilotAgent(st.session_state.api_key)

        # Re-attach any already-connected connectors
        for name, connector in st.session_state.connections.items():
            st.session_state.agent.add_connector(name, connector)
        if st.session_state.active_connection and st.session_state.active_connection in st.session_state.connections:
            st.session_state.agent.set_active_connector(st.session_state.active_connection)

        # Restore star schemas from SQLite
        for sname, sdict in store.load_star_schemas().items():
            try:
                from src.models.schemas import StarSchema
                st.session_state.agent._star_schemas[sname] = StarSchema(**sdict)
            except Exception:
                pass

    return st.session_state.agent


def _reconnect(name: str) -> bool:
    """Try to re-establish a live connector from stored config."""
    cfg = st.session_state.connection_configs.get(name)
    if not cfg:
        return False
    try:
        if cfg["db_type"] == "azure_sql":
            from src.connectors.azure_sql import AzureSQLConnector
            c = AzureSQLConnector(cfg["host"], cfg["database"], cfg["username"], cfg["password"], cfg.get("driver", "ODBC Driver 18 for SQL Server"))
        else:
            from src.connectors.postgresql import PostgreSQLConnector
            c = PostgreSQLConnector(cfg["host"], int(cfg.get("port", 5432)), cfg["database"], cfg["username"], cfg["password"])
        c.connect()
        st.session_state.connections[name] = c
        if st.session_state.agent:
            st.session_state.agent.add_connector(name, c)
        return True
    except Exception:
        return False


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
        st.session_state.agent = None  # force recreation with new key

    # Visual feedback on key status
    if st.session_state.api_key and st.session_state.api_key.startswith("sk-ant-"):
        st.caption("🟢 Chave configurada")
    elif st.session_state.api_key:
        st.caption("🔴 Chave parece inválida (deve começar com `sk-ant-`)")
    else:
        st.caption("⚠️ Sem chave — defina ANTHROPIC_API_KEY no .env ou acima")

    st.divider()
    page = st.radio(
        "Navegação",
        [
            "💬 Copilot Chat",
            "🔌 Conexões",
            "📋 ADF Analyzer",
            "🗄️ DW Explorer",
            "📦 Modelos Gerados",
        ],
        label_visibility="collapsed",
    )

    # Connection status
    st.divider()
    if st.session_state.connection_configs:
        st.markdown("**Bancos configurados:**")
        for cname, cfg in st.session_state.connection_configs.items():
            is_live = cname in st.session_state.connections
            is_active = cname == st.session_state.active_connection
            icon = "🟢" if (is_live and is_active) else ("🟡" if is_live else "🔴")
            label = f"{icon} **{cname}** ({'ativo' if is_active else cfg['db_type']})"
            col1, col2 = st.columns([3, 1])
            col1.markdown(label)
            if not is_active and col2.button("✓", key=f"set_active_{cname}", help="Ativar"):
                if cname not in st.session_state.connections:
                    with st.spinner(f"Reconectando {cname}..."):
                        _reconnect(cname)
                st.session_state.active_connection = cname
                store.set_setting("active_connection", cname)
                if st.session_state.agent and cname in st.session_state.connections:
                    st.session_state.agent.set_active_connector(cname)
                st.rerun()
    else:
        st.info("Nenhum banco conectado")

    if st.button("🗑️ Limpar conversa", use_container_width=True):
        st.session_state.messages = []
        store.clear_messages()
        if st.session_state.agent:
            st.session_state.agent.reset_conversation()
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Chat
# ─────────────────────────────────────────────────────────────────────────────

if page == "💬 Copilot Chat":
    st.header("💬 DataMart Copilot")
    st.caption(
        "Pergunte sobre seu DW, peça modelagens, diagramas, queries otimizadas e modelos dbt."
    )

    # Active connection banner
    if st.session_state.active_connection:
        cfg = st.session_state.connection_configs.get(st.session_state.active_connection, {})
        is_live = st.session_state.active_connection in st.session_state.connections
        status = "🟢 conectado" if is_live else "🔴 desconectado"
        st.info(
            f"Banco ativo: **{st.session_state.active_connection}** "
            f"(`{cfg.get('db_type','?')}` — {cfg.get('host','?')}/{cfg.get('database','?')}) {status}"
        )
    else:
        st.warning("Nenhum banco ativo. Configure uma conexão na aba **Conexões**.")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ex: Crie um datamart de vendas com dimensão produto, cliente e tempo..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        store.save_message("user", prompt)
        with st.chat_message("user"):
            st.markdown(prompt)

        agent = get_agent()

        # Ensure active connector is set
        active = st.session_state.active_connection
        if active and active in st.session_state.connections:
            agent.set_active_connector(active)
        elif active and active not in st.session_state.connections:
            with st.spinner(f"Reconectando {active}..."):
                _reconnect(active)
            if active in st.session_state.connections:
                agent.set_active_connector(active)

        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            full_response = ""
            with st.spinner("Pensando..."):
                try:
                    for chunk in agent.chat(prompt):
                        full_response += chunk
                        response_placeholder.markdown(full_response + "▌")
                    response_placeholder.markdown(full_response)
                except Exception as e:
                    err_str = str(e)
                    if "401" in err_str or "authentication_error" in err_str or "invalid x-api-key" in err_str:
                        full_response = (
                            "**Erro de autenticação (401):** A chave da Anthropic API é inválida.\n\n"
                            "**Como corrigir:**\n"
                            "1. Copie sua chave em [console.anthropic.com](https://console.anthropic.com)\n"
                            "2. Cole no campo **Anthropic API Key** na barra lateral\n"
                            "3. A chave deve começar com `sk-ant-`\n\n"
                            "Ou defina `ANTHROPIC_API_KEY=sk-ant-...` no arquivo `.env` e reinicie o app."
                        )
                        # Reset agent so it's recreated with the corrected key
                        st.session_state.agent = None
                    else:
                        full_response = f"**Erro:** {err_str}"
                    response_placeholder.error(full_response)

        st.session_state.messages.append({"role": "assistant", "content": full_response})
        store.save_message("assistant", full_response)

        # Persist any new star schemas the agent generated
        if agent._star_schemas:
            for sname, sschema in agent._star_schemas.items():
                store.save_star_schema(sname, sschema.model_dump())

        st.rerun()

    with st.expander("💡 Sugestões de perguntas", expanded=False):
        suggestions = [
            "Analise meu DW e mostre o Bus Matrix atual",
            "Quais dimensões conformadas existem?",
            "Crie um datamart de vendas por produto, cliente e período",
            "Gere o diagrama ER do modelo de vendas",
            "Esta solicitação pode ser unida a outro datamart existente?",
            "Gere os modelos dbt completos com staging e mart",
            "Quais KPIs posso extrair das tabelas de pedidos?",
            "Mostre as oportunidades de otimização desta query",
        ]
        cols = st.columns(2)
        for i, sug in enumerate(suggestions):
            if cols[i % 2].button(sug, key=f"sug_{i}", use_container_width=True):
                st.session_state.messages.append({"role": "user", "content": sug})
                store.save_message("user", sug)
                st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Conexões
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🔌 Conexões":
    st.header("🔌 Conexões de Banco de Dados")

    tab1, tab2, tab3 = st.tabs(["➕ Azure SQL", "➕ PostgreSQL", "📋 Gerenciar"])

    with tab1:
        st.subheader("Azure SQL Server")
        with st.form("azure_sql_form"):
            col1, col2 = st.columns(2)
            name   = col1.text_input("Nome da conexão", value="azure_prod")
            server = col2.text_input("Servidor", placeholder="server.database.windows.net")
            db     = col1.text_input("Database")
            user   = col2.text_input("Usuário")
            pwd    = st.text_input("Senha", type="password")
            driver = st.selectbox("ODBC Driver", ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"])
            if st.form_submit_button("Conectar e salvar", use_container_width=True):
                try:
                    from src.connectors.azure_sql import AzureSQLConnector
                    connector = AzureSQLConnector(server, db, user, pwd, driver)
                    with st.spinner("Conectando..."):
                        connector.connect()
                    st.session_state.connections[name] = connector
                    cfg = {"db_type": "azure_sql", "host": server, "database": db, "username": user, "password": pwd, "driver": driver}
                    st.session_state.connection_configs[name] = cfg
                    st.session_state.active_connection = name
                    store.save_connection(name, "azure_sql", cfg)
                    store.set_setting("active_connection", name)
                    agent = get_agent()
                    agent.add_connector(name, connector)
                    st.success(f"Conectado a **{name}**!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    with tab2:
        st.subheader("PostgreSQL")
        with st.form("postgres_form"):
            col1, col2 = st.columns(2)
            pg_name = col1.text_input("Nome da conexão", value="postgres_dev")
            pg_host = col2.text_input("Host", placeholder="localhost")
            pg_port = col1.number_input("Porta", value=5432, step=1)
            pg_db   = col2.text_input("Database")
            pg_user = col1.text_input("Usuário")
            pg_pwd  = col2.text_input("Senha", type="password")
            if st.form_submit_button("Conectar e salvar", use_container_width=True):
                try:
                    from src.connectors.postgresql import PostgreSQLConnector
                    connector = PostgreSQLConnector(pg_host, int(pg_port), pg_db, pg_user, pg_pwd)
                    with st.spinner("Conectando..."):
                        connector.connect()
                    st.session_state.connections[pg_name] = connector
                    cfg = {"db_type": "postgresql", "host": pg_host, "port": int(pg_port), "database": pg_db, "username": pg_user, "password": pg_pwd}
                    st.session_state.connection_configs[pg_name] = cfg
                    st.session_state.active_connection = pg_name
                    store.save_connection(pg_name, "postgresql", cfg)
                    store.set_setting("active_connection", pg_name)
                    agent = get_agent()
                    agent.add_connector(pg_name, connector)
                    st.success(f"Conectado a **{pg_name}**!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    with tab3:
        st.subheader("Conexões salvas")
        if not st.session_state.connection_configs:
            st.info("Nenhuma conexão salva ainda.")
        for cname, cfg in list(st.session_state.connection_configs.items()):
            is_live   = cname in st.session_state.connections
            is_active = cname == st.session_state.active_connection
            with st.expander(
                f"{'🟢' if is_live else '🔴'} **{cname}** — {cfg['db_type']} | {cfg.get('host','?')}/{cfg.get('database','?')}",
                expanded=is_active,
            ):
                st.json({k: ("***" if k == "password" else v) for k, v in cfg.items()})
                col1, col2, col3 = st.columns(3)
                if not is_live and col1.button("Reconectar", key=f"reconn_{cname}"):
                    with st.spinner("Reconectando..."):
                        ok = _reconnect(cname)
                    st.success("Reconectado!" if ok else "Falhou.")
                    st.rerun()
                if not is_active and col2.button("Ativar", key=f"activ_{cname}"):
                    if not is_live:
                        _reconnect(cname)
                    st.session_state.active_connection = cname
                    store.set_setting("active_connection", cname)
                    if st.session_state.agent and cname in st.session_state.connections:
                        st.session_state.agent.set_active_connector(cname)
                    st.rerun()
                if col3.button("🗑️ Remover", key=f"del_{cname}"):
                    store.delete_connection(cname)
                    st.session_state.connection_configs.pop(cname, None)
                    st.session_state.connections.pop(cname, None)
                    if st.session_state.active_connection == cname:
                        st.session_state.active_connection = None
                    st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: ADF Analyzer
# ─────────────────────────────────────────────────────────────────────────────

elif page == "📋 ADF Analyzer":
    st.header("📋 Azure Data Factory — Analisador de Pipelines")

    uploaded = st.file_uploader(
        "Faça upload dos JSON de pipelines ADF",
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
                store.save_adf_pipeline(pipeline.name, pipeline.model_dump())
                if st.session_state.agent:
                    st.session_state.agent.add_adf_pipeline(pipeline)

                with st.expander(f"Pipeline: **{pipeline.name}**", expanded=True):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Atividades", len(pipeline.activities))
                    c2.metric("Fontes", len(pipeline.sources))
                    c3.metric("Destinos", len(pipeline.sinks))
                    if pipeline.description:
                        st.caption(pipeline.description)
                    for act in pipeline.activities:
                        dep = f" ← {', '.join(act.depends_on)}" if act.depends_on else ""
                        st.markdown(f"- **{act.name}** `[{act.activity_type}]`{dep}")
                    cs, ck = st.columns(2)
                    if pipeline.sources:
                        cs.write("**Fontes**"); [cs.code(s) for s in pipeline.sources]
                    if pipeline.sinks:
                        ck.write("**Destinos**"); [ck.code(s) for s in pipeline.sinks]
            except Exception as e:
                st.error(f"Erro em {file.name}: {e}")

    # Saved pipelines
    saved = store.load_adf_pipelines()
    if saved:
        st.divider()
        st.info(f"{len(saved)} pipeline(s) armazenados. Pergunte ao Copilot sobre eles!")

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: DW Explorer
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🗄️ DW Explorer":
    st.header("🗄️ Data Warehouse Explorer")

    if not st.session_state.connection_configs:
        st.warning("Nenhuma conexão configurada. Vá para a aba **Conexões**.")
        st.stop()

    # ── Database selector ──────────────────────────────────────────────────
    all_conn_names = list(st.session_state.connection_configs.keys())
    default_idx = (
        all_conn_names.index(st.session_state.active_connection)
        if st.session_state.active_connection in all_conn_names
        else 0
    )
    selected_conn = st.selectbox(
        "Banco de dados",
        all_conn_names,
        index=default_idx,
        help="Selecione qual banco explorar",
    )

    cfg = st.session_state.connection_configs[selected_conn]
    is_live = selected_conn in st.session_state.connections

    st.caption(
        f"**{cfg['db_type']}** — `{cfg.get('host','?')}/{cfg.get('database','?')}` "
        + ("🟢 conectado" if is_live else "🔴 desconectado")
    )

    if not is_live:
        if st.button("Reconectar", use_container_width=True):
            with st.spinner("Reconectando..."):
                ok = _reconnect(selected_conn)
            if ok:
                st.success("Reconectado!")
                st.rerun()
            else:
                st.error("Falha na reconexão. Verifique as credenciais.")
            st.stop()
        else:
            st.stop()

    connector = st.session_state.connections[selected_conn]

    col_btn1, col_btn2 = st.columns(2)
    if col_btn1.button("🔍 Introspectar Schema", use_container_width=True):
        with st.spinner("Escaneando schema..."):
            try:
                tables = connector.introspect_schema()
                st.session_state.schema_cache[selected_conn] = tables
                store.save_schema_cache(selected_conn, [t.model_dump() for t in tables])
                st.success(f"{len(tables)} tabelas encontradas.")
            except Exception as e:
                st.error(f"Erro: {e}")

    # Load from SQLite cache if not in memory
    if selected_conn not in st.session_state.schema_cache:
        cached = store.load_schema_cache(selected_conn)
        if cached:
            from src.models.schemas import TableSchema
            try:
                st.session_state.schema_cache[selected_conn] = [TableSchema(**t) for t in cached]
            except Exception:
                pass

    tables = st.session_state.schema_cache.get(selected_conn, [])

    if tables:
        from src.analyzers.dw_analyzer import DWAnalyzer
        analyzer = DWAnalyzer(tables)
        analyzer.classify_tables()
        summary = analyzer.get_schema_summary()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total tabelas", summary["total_tables"])
        c2.metric("Fact tables", len(summary["fact_tables"]))
        c3.metric("Dimensions", len(summary["dimension_tables"]))
        c4.metric("Dims conformadas", len(summary["conformed_dimensions"]))

        tab_facts, tab_dims, tab_bus, tab_all, tab_diagram, tab_sql = st.tabs(
            ["Fact Tables", "Dimensões", "Bus Matrix", "Todas", "Diagrama ER", "SQL Playground"]
        )

        with tab_facts:
            for tname in summary["fact_tables"]:
                t = next((x for x in tables if x.full_name == tname), None)
                if t:
                    with st.expander(f"📊 {tname} ({t.row_count or '?'} linhas)"):
                        df = pd.DataFrame([{"Coluna": c.name, "Tipo": c.data_type, "PK": c.is_primary_key, "FK": c.is_foreign_key, "Nullable": c.is_nullable} for c in t.columns])
                        st.dataframe(df, use_container_width=True, hide_index=True)
                        if t.foreign_keys:
                            st.markdown("**Relacionamentos:**")
                            for fk in t.foreign_keys:
                                st.markdown(f"- `{fk.column}` → `{fk.referenced_table}.{fk.referenced_column}`")

        with tab_dims:
            for tname in summary["dimension_tables"]:
                t = next((x for x in tables if x.full_name == tname), None)
                if t:
                    is_conformed = tname in summary["conformed_dimensions"]
                    label = f"📁 {tname}" + (" ⭐ conformada" if is_conformed else "")
                    with st.expander(label):
                        df = pd.DataFrame([{"Coluna": c.name, "Tipo": c.data_type, "PK": c.is_primary_key} for c in t.columns])
                        st.dataframe(df, use_container_width=True, hide_index=True)

        with tab_bus:
            bus = analyzer.suggest_bus_matrix()
            if bus:
                df_bus = pd.DataFrame(bus).T.fillna(False).replace({True: "✓", False: ""})
                st.dataframe(df_bus, use_container_width=True)
                if summary["conformed_dimensions"]:
                    st.markdown("**Dimensões conformadas** (usadas em múltiplos facts):")
                    for d in summary["conformed_dimensions"]:
                        st.markdown(f"- `{d}`")
            else:
                st.info("Nenhum relacionamento fact/dim detectado ainda.")

        with tab_all:
            all_data = [
                {
                    "Schema": t.schema_name, "Tabela": t.table_name,
                    "Colunas": len(t.columns), "Linhas": t.row_count or "?",
                    "Tipo": "Fact" if t.is_fact_table else ("Dimension" if t.is_dimension_table else "Outro"),
                }
                for t in tables
            ]
            schema_filter = st.text_input("Filtrar por nome", "")
            df_all = pd.DataFrame(all_data)
            if schema_filter:
                df_all = df_all[df_all["Tabela"].str.contains(schema_filter, case=False, na=False)]
            st.dataframe(df_all, use_container_width=True, hide_index=True)

        with tab_diagram:
            st.caption("Diagrama de relacionamentos do schema (tabelas e FKs)")
            from src.generators.diagram_generator import DiagramGenerator
            gen = DiagramGenerator()
            # Filter to only connected tables for readability
            relevant = [t for t in tables if t.is_fact_table or t.is_dimension_table or t.foreign_keys]
            if not relevant:
                relevant = tables[:20]
            try:
                dot = gen.schema_relationships_dot(relevant[:30])
                st.graphviz_chart(dot, use_container_width=True)
            except Exception as e:
                st.error(f"Erro ao gerar diagrama: {e}")

        with tab_sql:
            st.caption("Execute queries diretamente no banco selecionado")
            sql_input = st.text_area(
                "SQL",
                height=150,
                placeholder="SELECT TOP 10 * FROM dbo.MinhaTabela",
                key=f"sql_playground_{selected_conn}",
            )
            col_run, col_exp = st.columns([1, 3])
            if col_run.button("▶ Executar", use_container_width=True):
                if sql_input.strip():
                    with st.spinner("Executando..."):
                        try:
                            df = connector.execute_query(sql_input)
                            st.success(f"{len(df)} linhas retornadas.")
                            st.dataframe(df, use_container_width=True, hide_index=True)
                            with st.expander("Exportar CSV"):
                                st.download_button(
                                    "Download CSV",
                                    df.to_csv(index=False).encode("utf-8"),
                                    "resultado.csv",
                                    "text/csv",
                                )
                        except Exception as e:
                            st.error(f"Erro: {e}")
                else:
                    st.warning("Digite uma query.")

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Modelos Gerados
# ─────────────────────────────────────────────────────────────────────────────

elif page == "📦 Modelos Gerados":
    st.header("📦 Modelos Gerados")

    agent = st.session_state.agent
    star_schemas = {}

    # Combine in-memory + SQLite
    if agent:
        star_schemas.update(agent._star_schemas)
    for sname, sdict in store.load_star_schemas().items():
        if sname not in star_schemas:
            try:
                from src.models.schemas import StarSchema
                star_schemas[sname] = StarSchema(**sdict)
            except Exception:
                pass

    if not star_schemas:
        st.info("Nenhum modelo gerado ainda. Peça ao Copilot para criar um datamart.")
        st.stop()

    for schema_name, star_schema in star_schemas.items():
        with st.expander(f"⭐ {schema_name} — {star_schema.subject_area}", expanded=True):

            # Overview
            c1, c2, c3 = st.columns(3)
            c1.metric("Fact table", star_schema.fact_table.name)
            c2.metric("Dimensões", len(star_schema.dimension_tables))
            c3.metric("KPI oportunidades", len(star_schema.kpi_opportunities))

            tab_overview, tab_diagram, tab_dbt, tab_ddl, tab_kpi = st.tabs(
                ["Visão Geral", "Diagrama ER", "dbt Models", "DDL", "KPIs"]
            )

            with tab_overview:
                st.markdown(f"**Grain:** {star_schema.fact_table.grain}")
                st.markdown(f"**Tipo:** {star_schema.fact_table.fact_type.value}")
                st.markdown("**Medidas:**")
                for m in star_schema.fact_table.measures:
                    st.markdown(f"- `{m}`")
                st.markdown("**Dimensões:**")
                for dim in star_schema.dimension_tables:
                    conformed_mark = " ⭐ conformada" if dim.conformed else ""
                    st.markdown(f"- **{dim.name}** — SCD Type {dim.scd_type.value[-1]}{conformed_mark}")
                    st.caption(f"  Grain: {dim.grain}")

            with tab_diagram:
                from src.generators.diagram_generator import DiagramGenerator
                diag_gen = DiagramGenerator()
                try:
                    dot = diag_gen.star_schema_dot(star_schema)
                    st.graphviz_chart(dot, use_container_width=True)
                except Exception as e:
                    st.error(f"Erro no diagrama: {e}")
                with st.expander("Mermaid ER (copiar para docs)"):
                    mermaid = diag_gen.mermaid_er(star_schema)
                    st.code(mermaid, language="text")

            with tab_dbt:
                from src.generators.dbt_generator import DBTGenerator
                dbt_gen = DBTGenerator()
                project_name = schema_name.lower().replace(" ", "_")

                if st.button(f"Gerar modelos dbt", key=f"gen_dbt_{schema_name}"):
                    with st.spinner("Gerando..."):
                        project = dbt_gen.generate_project(star_schema, project_name)

                    st.success(f"{len(project.models)} modelos gerados!")

                    for model in project.models:
                        with st.expander(f"📄 {model.layer}/{model.name}.sql"):
                            st.code(model.sql_content, language="sql")
                            if model.schema_yaml:
                                st.subheader("schema.yml")
                                st.code(model.schema_yaml, language="yaml")

                    with st.expander("📄 sources.yml"):
                        st.code(project.sources_yaml or "", language="yaml")
                    with st.expander("📄 dbt_project.yml"):
                        st.code(project.dbt_project_yaml or "", language="yaml")

            with tab_ddl:
                from src.generators.sql_generator import SQLGenerator
                sql_gen = SQLGenerator()
                db_type = st.radio("Dialect", ["sqlserver", "postgresql"], key=f"ddl_type_{schema_name}", horizontal=True)

                st.subheader(f"CREATE TABLE {star_schema.fact_table.name}")
                st.code(sql_gen.generate_fact_ddl(star_schema.fact_table, db_type), language="sql")

                for dim in star_schema.dimension_tables:
                    st.subheader(f"CREATE TABLE {dim.name}")
                    st.code(sql_gen.generate_dimension_ddl(dim, db_type), language="sql")
                    if dim.scd_type.value == "type2":
                        with st.expander(f"SCD2 MERGE — {dim.name}"):
                            src = dim.source_tables[0].split(".")[-1] if dim.source_tables else "source"
                            st.code(sql_gen.generate_scd2_merge(dim, src), language="sql")

            with tab_kpi:
                if star_schema.kpi_opportunities:
                    for kpi_name in star_schema.kpi_opportunities:
                        st.markdown(f"- **{kpi_name}**")
                else:
                    st.info("Peça ao Copilot para sugerir KPIs para este datamart.")

            if st.button(f"🗑️ Remover {schema_name}", key=f"del_{schema_name}"):
                store.delete_star_schema(schema_name)
                if agent:
                    agent._star_schemas.pop(schema_name, None)
                st.rerun()
