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

@st.cache_resource
def get_brain():
    from src.knowledge.brain import KnowledgeBrain
    return KnowledgeBrain()

store = get_store()
brain = get_brain()
# Always run schema migrations on startup — safe even if columns already exist
brain._init_tables()

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
        st.session_state.agent = DatamartCopilotAgent(st.session_state.api_key, brain=brain)

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
            "🧠 Cérebro",
            "🚀 Acelerador",
            "📖 Dicionário",
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
        "Faça upload do template ARM (deploymentTemplate.json) ou JSONs individuais de pipeline",
        type="json",
        accept_multiple_files=True,
    )
    run_ai = st.checkbox("Analisar pipelines com IA automaticamente ao fazer upload", value=True)

    if uploaded:
        from src.connectors.adf_parser import ADFParser
        from src.knowledge.adf_ai_analyzer import ADFAIAnalyzer
        from src.models.schemas import ADFFactory
        parser = ADFParser()
        ai_analyzer = ADFAIAnalyzer(brain)

        for file in uploaded:
            try:
                content = json.loads(file.read())

                # ── ARM deployment template ───────────────────────────────────
                if parser.is_arm_template(content):
                    factory = parser.parse_arm_template(content)

                    st.success(f"Template ARM detectado: **{factory.factory_name}**")

                    # Summary metrics
                    mc = st.columns(5)
                    mc[0].metric("Linked Services", len(factory.linked_services))
                    mc[1].metric("Datasets", len(factory.datasets))
                    mc[2].metric("Pipelines", len(factory.pipelines))
                    mc[3].metric("Dataflows", len(factory.dataflows))
                    mc[4].metric("Triggers", len(factory.triggers))

                    # Linked services
                    with st.expander("🔗 Linked Services", expanded=True):
                        for ls in factory.linked_services:
                            host_info = f" → `{ls.host}/{ls.database}`" if ls.host else ""
                            st.markdown(f"- **{ls.name}** `[{ls.service_type}]`{host_info}")
                        # Persist linked service info to brain context
                        ls_summary = "\n".join(
                            f"- {ls.name} ({ls.service_type}): {ls.host or '?'}/{ls.database or '?'}"
                            for ls in factory.linked_services
                        )
                        brain.save_context(
                            "adf_linked_services",
                            f"factory_{factory.factory_name}",
                            f"Factory {factory.factory_name} — Linked Services:\n{ls_summary}",
                            source="adf_import",
                        )

                    # Datasets
                    with st.expander(f"📂 Datasets ({len(factory.datasets)})", expanded=False):
                        for ds in factory.datasets:
                            tbl = f"{ds.schema_name}.{ds.table_name}" if ds.schema_name else ds.table_name or "?"
                            st.markdown(f"- **{ds.name}** `[{ds.dataset_type}]` → `{tbl}` via `{ds.linked_service}`")

                    # Triggers
                    with st.expander("⏰ Triggers", expanded=True):
                        for trig in factory.triggers:
                            pipes = ", ".join(trig.pipelines) if trig.pipelines else "?"
                            rec = ""
                            if trig.recurrence:
                                freq = trig.recurrence.get("frequency", "")
                                interval = trig.recurrence.get("interval", "")
                                rec = f" ({interval}x {freq})"
                            st.markdown(f"- **{trig.name}** `[{trig.trigger_type}]`{rec} → {pipes}")

                    # Pipelines
                    st.subheader("Pipelines")
                    for pipeline in factory.pipelines:
                        st.session_state.adf_pipelines.append(pipeline)
                        store.save_adf_pipeline(pipeline.name, pipeline.model_dump())
                        brain.save_adf_pipeline(pipeline.name, pipeline.raw_json or {})
                        if st.session_state.agent:
                            st.session_state.agent.add_adf_pipeline(pipeline)

                        with st.expander(f"Pipeline: **{pipeline.name}**", expanded=False):
                            pc1, pc2, pc3, pc4 = st.columns(4)
                            pc1.metric("Atividades", len(pipeline.activities))
                            pc2.metric("Fontes", len(pipeline.sources))
                            pc3.metric("Destinos", len(pipeline.sinks))
                            pc4.metric("Queries SQL", len(pipeline.embedded_queries))
                            if pipeline.description:
                                st.caption(pipeline.description)
                            for act in pipeline.activities:
                                dep = f" ← {', '.join(act.depends_on)}" if act.depends_on else ""
                                st.markdown(f"- **{act.name}** `[{act.activity_type}]`{dep}")
                            if pipeline.embedded_queries:
                                with st.expander("Queries SQL embutidas"):
                                    for qname, sql in pipeline.embedded_queries.items():
                                        st.markdown(f"**{qname}:**")
                                        st.code(sql, language="sql")
                            if run_ai and st.session_state.api_key:
                                with st.spinner(f"IA analisando {pipeline.name}..."):
                                    try:
                                        analysis = ai_analyzer.analyze_and_save(
                                            pipeline.name, pipeline.raw_json or {}
                                        )
                                        st.success("Análise salva no Cérebro!")
                                        st.markdown(f"**O que faz:** {analysis.get('business_description','?')}")
                                        st.markdown(f"**Estratégia:** `{analysis.get('load_strategy','?')}`")
                                        if analysis.get("problems_found"):
                                            st.warning("**Problemas:** " + " | ".join(analysis["problems_found"]))
                                        if analysis.get("suggestions"):
                                            st.info("**Sugestões:** " + " | ".join(analysis["suggestions"]))
                                    except Exception as e:
                                        st.warning(f"Análise IA falhou: {e}")

                    # Dataflows
                    st.subheader("Dataflows")
                    for df in factory.dataflows:
                        with st.expander(f"Dataflow: **{df.name}**", expanded=False):
                            dc1, dc2, dc3 = st.columns(3)
                            dc1.metric("Fontes", len(df.sources))
                            dc2.metric("Destinos", len(df.sinks))
                            dc3.metric("Transformações", len(df.transformations))
                            if df.sources:
                                st.markdown("**Fontes:** " + ", ".join(f"`{s}`" for s in df.sources))
                            if df.sinks:
                                st.markdown("**Destinos:** " + ", ".join(f"`{s}`" for s in df.sinks))
                            if df.transformations:
                                st.markdown("**Transformações:** " + ", ".join(f"`{t}`" for t in df.transformations))
                            if df.embedded_queries:
                                with st.expander("Queries SQL embutidas"):
                                    for qname, sql in df.embedded_queries.items():
                                        st.markdown(f"**{qname}:**")
                                        st.code(sql, language="sql")
                            if df.script_lines:
                                with st.expander("Script ADF completo"):
                                    st.code("\n".join(df.script_lines), language="text")
                            # Persist dataflow to brain as pipeline entry
                            df_raw = {
                                "name": df.name,
                                "properties": {
                                    "description": df.description,
                                    "sources": df.sources,
                                    "sinks": df.sinks,
                                    "transformations": df.transformations,
                                    "embedded_queries": df.embedded_queries,
                                    "script_lines": df.script_lines[:30],
                                },
                            }
                            brain.save_adf_pipeline(f"df_{df.name}", df_raw)

                # ── Individual pipeline JSON ──────────────────────────────────
                else:
                    pipeline = parser.parse_pipeline(content)
                    st.session_state.adf_pipelines.append(pipeline)
                    store.save_adf_pipeline(pipeline.name, pipeline.model_dump())
                    brain.save_adf_pipeline(pipeline.name, content)
                    if st.session_state.agent:
                        st.session_state.agent.add_adf_pipeline(pipeline)

                    with st.expander(f"Pipeline: **{pipeline.name}**", expanded=True):
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Atividades", len(pipeline.activities))
                        c2.metric("Fontes", len(pipeline.sources))
                        c3.metric("Destinos", len(pipeline.sinks))
                        c4.metric("Queries SQL", len(pipeline.embedded_queries))
                        if pipeline.description:
                            st.caption(pipeline.description)
                        for act in pipeline.activities:
                            dep = f" ← {', '.join(act.depends_on)}" if act.depends_on else ""
                            st.markdown(f"- **{act.name}** `[{act.activity_type}]`{dep}")
                        if pipeline.embedded_queries:
                            with st.expander("Queries SQL embutidas"):
                                for qname, sql in pipeline.embedded_queries.items():
                                    st.markdown(f"**{qname}:**")
                                    st.code(sql, language="sql")
                        if run_ai and st.session_state.api_key:
                            with st.spinner(f"IA analisando {pipeline.name}..."):
                                try:
                                    analysis = ai_analyzer.analyze_and_save(pipeline.name, content)
                                    st.success("Análise salva no Cérebro!")
                                    st.markdown(f"**O que faz:** {analysis.get('business_description','?')}")
                                    st.markdown(f"**Estratégia:** `{analysis.get('load_strategy','?')}`")
                                    if analysis.get("problems_found"):
                                        st.warning("**Problemas:** " + " | ".join(analysis["problems_found"]))
                                    if analysis.get("suggestions"):
                                        st.info("**Sugestões:** " + " | ".join(analysis["suggestions"]))
                                except Exception as e:
                                    st.warning(f"Análise IA falhou: {e}")
            except Exception as e:
                st.error(f"Erro em {file.name}: {e}")

    # Show all analyzed pipelines from brain
    st.divider()
    st.subheader("Pipelines no Cérebro")
    brain_pipelines = brain.get_adf_pipelines()
    if not brain_pipelines:
        st.info("Nenhum pipeline analisado ainda. Faça upload dos JSONs acima.")
    else:
        for p in brain_pipelines:
            analyzed = bool(p.get("business_description"))
            icon = "✅" if p.get("confirmed_by_user") else ("🔍" if analyzed else "⏳")
            with st.expander(f"{icon} **{p['pipeline_name']}** — {p.get('load_strategy','?')}"):
                if analyzed:
                    st.markdown(f"**Descrição:** {p['business_description']}")
                    c1, c2 = st.columns(2)
                    if p.get("source_tables"):
                        c1.markdown("**Fontes:**\n" + "\n".join(f"- `{s}`" for s in p["source_tables"]))
                    if p.get("target_tables"):
                        c2.markdown("**Destinos:**\n" + "\n".join(f"- `{s}`" for s in p["target_tables"]))
                    if p.get("problems_found"):
                        st.warning("**Problemas:** " + " | ".join(p["problems_found"]))
                    if p.get("suggestions"):
                        st.info("**Sugestões:** " + " | ".join(p["suggestions"]))
                    corrections = st.text_input("Correções (opcional)", key=f"corr_{p['pipeline_name']}")
                    if st.button("Confirmar análise", key=f"conf_{p['pipeline_name']}"):
                        brain.confirm_adf_analysis(p["pipeline_name"], corrections or None)
                        st.success("Confirmado e salvo no Cérebro!")
                        st.rerun()
                else:
                    st.warning("Pipeline ainda não analisado pela IA.")
                    if st.session_state.api_key and st.button("Analisar agora", key=f"anal_{p['pipeline_name']}"):
                        from src.knowledge.adf_ai_analyzer import ADFAIAnalyzer
                        try:
                            raw = json.loads(p.get("raw_json") or "{}")
                            analysis = ADFAIAnalyzer(brain).analyze_and_save(p["pipeline_name"], raw)
                            st.success("Análise salva!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")

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
        with st.spinner("Escaneando schema e alimentando Cérebro..."):
            try:
                from src.analyzers.dw_analyzer import DWAnalyzer
                tables = connector.introspect_schema()
                st.session_state.schema_cache[selected_conn] = tables
                store.save_schema_cache(selected_conn, [t.model_dump() for t in tables])
                # Classify + feed brain
                analyzer = DWAnalyzer(tables)
                analyzer.classify_tables()
                tables_for_brain = [
                    {**t.model_dump(), "classification": (
                        "fact" if t.is_fact_table else ("dimension" if t.is_dimension_table else "unknown")
                    )}
                    for t in tables
                ]
                brain.sync_dw_tables(selected_conn, tables_for_brain)
                brain.detect_naming_patterns(tables_for_brain)
                st.success(f"{len(tables)} tabelas encontradas e salvas no Cérebro.")
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

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Cérebro
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🧠 Cérebro":
    st.header("🧠 Cérebro — Base de Conhecimento")
    st.caption("Tudo que o Copilot sabe sobre seu ambiente. Edite, corrija e enriqueça a qualquer momento.")

    tab_ctx, tab_dw, tab_patterns, tab_glossary, tab_preview = st.tabs(
        ["📝 Contexto Livre", "🗄️ Conhecimento DW", "🔤 Padrões", "📚 Glossário", "👁️ Preview IA"]
    )

    with tab_ctx:
        st.subheader("Contexto livre — o que a IA precisa saber")
        st.caption("Escreva fatos sobre seu ambiente. Ex: 'Schema pub = datamarts Power BI'")

        CATEGORIES = ["schema_info", "business_rule", "naming_convention", "constraint", "general"]
        with st.form("add_context_form"):
            c1, c2 = st.columns([1, 2])
            cat = c1.selectbox("Categoria", CATEGORIES)
            key = c1.text_input("Chave (identificador único)", placeholder="pub_schema_purpose")
            value = c2.text_area("Valor", height=80, placeholder="Schema Pub contém os datamarts finais consumidos pelo Power BI")
            if st.form_submit_button("Salvar no Cérebro", use_container_width=True):
                if key and value:
                    brain.save_context(cat, key, value, source="user")
                    st.success("Salvo!")
                    st.rerun()
                else:
                    st.warning("Preencha chave e valor.")

        st.divider()
        all_ctx = brain.get_all_context()
        if not all_ctx:
            st.info("Nenhum contexto salvo ainda. Adicione acima para alimentar o Cérebro.")
        else:
            by_cat: dict[str, list] = {}
            for row in all_ctx:
                by_cat.setdefault(row["category"], []).append(row)
            for cat_name, rows in by_cat.items():
                st.markdown(f"**{cat_name.replace('_', ' ').title()}**")
                for row in rows:
                    col1, col2, col3 = st.columns([2, 4, 1])
                    col1.code(row["key"])
                    new_val = col2.text_input("", value=row["value"], key=f"ctx_edit_{row['id']}", label_visibility="collapsed")
                    if new_val != row["value"]:
                        brain.save_context(row["category"], row["key"], new_val, source="user")
                    if col3.button("🗑️", key=f"ctx_del_{row['id']}", help="Remover"):
                        brain.delete_context(row["id"])
                        st.rerun()
                st.divider()

    with tab_dw:
        st.subheader("Tabelas do DW no Cérebro")
        dw_tables = brain.get_dw_tables()
        if not dw_tables:
            st.info("Nenhuma tabela no Cérebro. Faça introspection no DW Explorer primeiro.")
        else:
            filter_text = st.text_input("Filtrar tabelas", "")
            classifications = ["todos", "fact", "dimension", "staging", "source", "unknown"]
            filter_class = st.selectbox("Filtrar por tipo", classifications)

            shown = dw_tables
            if filter_text:
                shown = [t for t in shown if filter_text.lower() in t["full_name"].lower()]
            if filter_class != "todos":
                shown = [t for t in shown if t["classification"] == filter_class]

            st.caption(f"{len(shown)} de {len(dw_tables)} tabelas")
            for t in shown[:50]:
                desc = t.get("description_user") or t.get("description_ai") or ""
                with st.expander(f"**{t['full_name']}** `{t['classification']}` {('— ' + desc[:60]) if desc else ''}"):
                    c1, c2 = st.columns([1, 2])
                    new_class = c1.selectbox(
                        "Classificação",
                        ["fact", "dimension", "staging", "source", "lookup", "unknown"],
                        index=["fact", "dimension", "staging", "source", "lookup", "unknown"].index(t["classification"])
                        if t["classification"] in ["fact", "dimension", "staging", "source", "lookup", "unknown"] else 5,
                        key=f"class_{t['id']}",
                    )
                    if new_class != t["classification"]:
                        brain.update_table_classification(t["id"], new_class)
                        st.rerun()
                    new_desc = c2.text_area(
                        "Descrição (você edita, tem precedência sobre IA)",
                        value=t.get("description_user") or "",
                        key=f"desc_{t['id']}",
                        height=60,
                    )
                    if new_desc != (t.get("description_user") or ""):
                        brain.update_table_description(t["id"], new_desc, "user")

                    # Show columns
                    cols = brain.get_dw_columns(t["id"])
                    if cols:
                        col_data = [
                            {"Coluna": c["column_name"], "Tipo": c["data_type"],
                             "PK": "✓" if c["is_pk"] else "", "FK": "✓" if c["is_fk"] else "",
                             "Semântica": c.get("semantic_type") or "",
                             "Descrição": c.get("description_user") or c.get("description_ai") or ""}
                            for c in cols
                        ]
                        st.dataframe(pd.DataFrame(col_data), use_container_width=True, hide_index=True)

    with tab_patterns:
        st.subheader("Padrões de nomenclatura detectados")
        patterns = brain.get_naming_patterns()
        if not patterns:
            st.info("Nenhum padrão detectado ainda. Faça introspection no DW Explorer.")
        else:
            for p in patterns:
                confirmed = p.get("confirmed", False)
                icon = "✅" if confirmed else "⏳"
                with st.expander(f"{icon} `{p['pattern']}` ({p['scope']}) — {p.get('meaning','')}"):
                    examples = p.get("examples", [])
                    if examples:
                        st.caption("Exemplos: " + ", ".join(f"`{e}`" for e in examples[:5]))
                    new_meaning = st.text_input("Significado", value=p.get("meaning", ""), key=f"pat_{p['id']}")
                    if new_meaning != p.get("meaning", ""):
                        brain.save_naming_pattern(p["scope"], p["pattern"], new_meaning, examples)
                    if not confirmed and st.button("✅ Confirmar", key=f"conf_pat_{p['id']}"):
                        brain.confirm_naming_pattern(p["id"])
                        st.rerun()

    with tab_glossary:
        st.subheader("Glossário de negócio")
        with st.form("add_term_form"):
            c1, c2 = st.columns(2)
            term = c1.text_input("Termo", placeholder="Contrato de Financiamento")
            definition = c2.text_input("Definição", placeholder="Acordo formal de crédito entre cliente e Credimorar")
            context = st.text_area("Contexto de negócio (opcional)", height=60)
            if st.form_submit_button("Adicionar ao glossário"):
                if term and definition:
                    brain.save_glossary_term(term, definition, context)
                    st.success("Adicionado!")
                    st.rerun()

        st.divider()
        glossary = brain.get_glossary()
        if not glossary:
            st.info("Nenhum termo no glossário ainda.")
        else:
            for g in glossary:
                with st.expander(f"**{g['term']}** — {g['definition'][:60]}"):
                    new_def = st.text_input("Definição", value=g["definition"], key=f"glos_def_{g['id']}")
                    new_ctx = st.text_area("Contexto", value=g.get("business_context", ""), key=f"glos_ctx_{g['id']}", height=60)
                    c1, c2 = st.columns(2)
                    if c1.button("💾 Salvar", key=f"glos_save_{g['id']}"):
                        brain.save_glossary_term(g["term"], new_def, new_ctx)
                        st.success("Salvo!")
                    if c2.button("🗑️ Remover", key=f"glos_del_{g['id']}"):
                        brain.delete_glossary_term(g["id"])
                        st.rerun()

    with tab_preview:
        st.subheader("Como a IA vê o Cérebro")
        st.caption("Este é exatamente o contexto injetado no system prompt do Copilot.")
        ctx_preview = brain.build_ai_context()
        st.code(ctx_preview, language="markdown")
        st.caption(f"Tamanho: ~{len(ctx_preview.split())} palavras / ~{len(ctx_preview)//4} tokens estimados")

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Acelerador
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🚀 Acelerador":
    st.header("🚀 Acelerador de Novos Datamarts")
    st.caption("Descreva a demanda em linguagem natural. O sistema consulta o Cérebro, faz perguntas e gera o pacote completo.")

    if not st.session_state.api_key:
        st.warning("Configure a ANTHROPIC_API_KEY para usar o Acelerador.")
        st.stop()

    from src.knowledge.datamart_accelerator import DatamartAccelerator
    accelerator = DatamartAccelerator(brain)

    tab_new, tab_history = st.tabs(["➕ Nova demanda", "📋 Histórico"])

    with tab_new:
        requests = brain.get_all_requests()
        active = [r for r in requests if r["status"] not in ("ready", "delivered")]
        in_progress = active[0] if active else None

        if in_progress:
            st.info(f"Demanda em andamento: **{in_progress['title']}** (status: `{in_progress['status']}`)")
            req_id = in_progress["id"]
        else:
            req_id = None

        if not req_id:
            st.subheader("1. Descreva a demanda")
            with st.form("new_request_form"):
                title = st.text_input("Título da demanda", placeholder="Datamart de Contratos por Produto e Prazo")
                description = st.text_area(
                    "Descrição detalhada",
                    height=120,
                    placeholder="Precisamos analisar o volume de contratos por produto, prazo e regional, com indicadores de ticket médio, prazo médio e concentração por convênio..."
                )
                source_query = st.text_area(
                    "Query de referência (opcional — cole a query que já está em produção)",
                    height=80,
                )
                if st.form_submit_button("▶ Iniciar análise", use_container_width=True):
                    if title and description:
                        req_id = brain.create_request(title, description, source_query or None)
                        brain.update_request(req_id, status="analyzing")
                        st.rerun()
                    else:
                        st.warning("Preencha título e descrição.")

        if req_id:
            req = brain.get_request(req_id)
            if not req:
                st.error("Solicitação não encontrada.")
                st.stop()

            st.markdown(f"### {req['title']}")
            status = req["status"]

            # ── Step: analyzing ─────────────────────────────────────────
            if status == "analyzing":
                with st.spinner("Consultando Cérebro e analisando demanda..."):
                    analysis = accelerator.analyze_demand(req_id)
                if "error" in analysis:
                    st.error(analysis["error"])
                else:
                    st.session_state[f"analysis_{req_id}"] = analysis
                    brain.update_request(req_id, status="clarifying" if analysis.get("perguntas") else "designing")
                    st.rerun()

            analysis = st.session_state.get(f"analysis_{req_id}")
            if not analysis:
                # Restore from brain (persisted in analyze_demand) after page reload
                persisted = req.get("analysis_json", {})
                if persisted and isinstance(persisted, dict):
                    analysis = persisted
                    st.session_state[f"analysis_{req_id}"] = analysis
                elif status in ("clarifying", "designing", "ready"):
                    analysis = {}

            if analysis:
                st.subheader("📋 Análise do Cérebro")
                if analysis.get("entendimento"):
                    st.markdown(f"**Entendimento:** {analysis['entendimento']}")

                c1, c2 = st.columns(2)
                if analysis.get("tabelas_relevantes"):
                    c1.markdown("**Tabelas relevantes:**\n" + "\n".join(f"- {t}" for t in analysis["tabelas_relevantes"]))
                if analysis.get("gaps"):
                    c2.markdown("**Gaps identificados:**\n" + "\n".join(f"- {g}" for g in analysis["gaps"]))

                if analysis.get("pode_unir_com"):
                    st.warning(f"**Pode unir com:** `{analysis['pode_unir_com']}` — {analysis.get('motivo_uniao','')}")
                if analysis.get("grain_preliminar"):
                    st.info(f"**Grain preliminar:** {analysis['grain_preliminar']}")

            # ── Step: clarifying ────────────────────────────────────────
            if status == "clarifying" and analysis and analysis.get("perguntas"):
                st.subheader("❓ Perguntas antes de continuar")
                st.caption("O sistema precisa das suas respostas para não assumir decisões de negócio.")

                answers = {}
                with st.form("clarification_form"):
                    for i, q in enumerate(analysis["perguntas"]):
                        answers[q] = st.text_area(f"**{q}**", key=f"q_{i}", height=60)
                    if st.form_submit_button("✅ Responder e continuar", use_container_width=True):
                        qa_list = [{"question": q, "answer": a} for q, a in answers.items() if a.strip()]
                        accelerator.save_answers(req_id, qa_list)
                        st.rerun()

            # ── Step: designing ─────────────────────────────────────────
            elif status == "designing":
                design_key = f"design_{req_id}"

                # Restore from SQLite when session state is empty (page reload)
                if design_key not in st.session_state:
                    persisted_design = req.get("design_json", {})
                    if persisted_design and persisted_design.get("fact_table"):
                        st.session_state[design_key] = persisted_design

                # Only call the AI when there is genuinely no design yet,
                # or the previous attempt returned an explicit error.
                # Do NOT invalidate just because fact_table is empty — that
                # creates an infinite loop where every rerun pops and retriggers.
                cached = st.session_state.get(design_key)
                needs_generation = cached is None or "error" in cached or "_raw" in cached

                if needs_generation:
                    with st.spinner("Desenhando star schema Kimball... (pode levar ~30s)"):
                        try:
                            design = accelerator.design_star_schema(req_id, analysis or {})
                        except Exception as exc:
                            design = {"error": str(exc)}
                        st.session_state[design_key] = design

                design = st.session_state.get(design_key, {})

                # Debug expander — always visible so user can see raw AI response
                with st.expander("🔍 Debug: resposta bruta da IA", expanded=not design.get("fact_table")):
                    st.json(design)

                if design.get("error"):
                    st.error(f"Erro ao gerar design: {design['error']}")
                    if st.button("🔄 Tentar novamente"):
                        st.session_state.pop(design_key, None)
                        st.rerun()

                elif design.get("fact_table"):
                    st.subheader("⭐ Design Star Schema")
                    fact = design["fact_table"]
                    fname = fact.get("name") or "?"
                    fgrain = fact.get("grain") or "?"
                    st.markdown(f"**Fact:** `{fname}` — Grain: _{fgrain}_")
                    st.markdown(f"**Tipo:** {fact.get('type','?')}")

                    if fact.get("measures"):
                        st.markdown("**Medidas:**")
                        for m in fact["measures"]:
                            st.markdown(f"- `{m.get('name')}` ({m.get('type')}) — {m.get('description','')}")

                    if design.get("dimensions"):
                        st.markdown("**Dimensões:**")
                        for dim in design["dimensions"]:
                            conformed = " ⭐ conformada" if dim.get("conformed") else ""
                            st.markdown(f"- **{dim.get('name')}** — SCD Type {dim.get('scd_type',1)}{conformed}")
                            st.caption(f"  {dim.get('rationale','')}")

                    if design.get("kimball_notes"):
                        st.info(f"**Notas Kimball:** {design['kimball_notes']}")

                    c1, c2 = st.columns(2)
                    if c1.button("✅ Aprovar design e gerar pacote", use_container_width=True):
                        brain.update_request(req_id, status="ready")
                        with st.spinner("Gerando pacote completo (dbt + ADF + docs)..."):
                            artifacts = accelerator.generate_full_package(req_id, design)
                            st.session_state[f"artifacts_{req_id}"] = artifacts
                        st.success("Pacote gerado!")
                        st.rerun()
                    if c2.button("↩️ Refazer com ajustes", use_container_width=True):
                        st.session_state.pop(design_key, None)
                        brain.update_request(req_id, status="analyzing")
                        st.rerun()

                else:
                    st.warning("Design retornou sem fact_table. Veja o debug acima para entender a resposta da IA.")
                    if st.button("🔄 Reprocessar"):
                        st.session_state.pop(design_key, None)
                        st.rerun()

            # ── Step: ready ─────────────────────────────────────────────
            elif status == "ready":
                req_full = brain.get_request(req_id)
                artifacts = (
                    st.session_state.get(f"artifacts_{req_id}")
                    or (req_full.get("artifacts") if isinstance(req_full.get("artifacts"), dict) else {})
                )
                if artifacts:
                    st.success("✅ Pacote completo gerado!")
                    tab_dbt, tab_adf, tab_doc = st.tabs(["dbt Models", "Pipeline ADF", "Documentação"])
                    with tab_dbt:
                        st.code(artifacts.get("dbt_models", ""), language="sql")
                    with tab_adf:
                        st.code(artifacts.get("adf_json", ""), language="json")
                    with tab_doc:
                        st.markdown(artifacts.get("documentation", ""))

                    col1, col2 = st.columns(2)
                    if col1.button("⬇️ Baixar documentação .md", use_container_width=True):
                        st.download_button(
                            "Download .md",
                            artifacts.get("documentation", "").encode("utf-8"),
                            f"{req['title'].replace(' ','_')}.md",
                            "text/markdown",
                        )
                    if col2.button("🗑️ Fechar e iniciar nova demanda", use_container_width=True):
                        brain.update_request(req_id, status="delivered")
                        st.session_state.pop(f"analysis_{req_id}", None)
                        st.session_state.pop(f"design_{req_id}", None)
                        st.session_state.pop(f"artifacts_{req_id}", None)
                        st.rerun()

    with tab_history:
        st.subheader("Histórico de demandas")
        all_requests = brain.get_all_requests()
        if not all_requests:
            st.info("Nenhuma demanda registrada ainda.")
        else:
            status_icons = {"intake": "⏳", "analyzing": "🔍", "clarifying": "❓", "designing": "✏️", "ready": "✅", "delivered": "📦"}
            for r in all_requests:
                icon = status_icons.get(r["status"], "?")
                st.markdown(f"{icon} **{r['title']}** — `{r['status']}` — {r['created_at'][:10]}")

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: Dicionário
# ─────────────────────────────────────────────────────────────────────────────

elif page == "📖 Dicionário":
    st.header("📖 Dicionário de Dados")
    st.caption("Descrições de tabelas e colunas. Edite qualquer campo — suas edições têm precedência sobre a IA.")

    tab_tables, tab_columns, tab_export = st.tabs(["Tabelas", "Colunas", "Exportar"])

    with tab_tables:
        dw_tables = brain.get_dw_tables()
        if not dw_tables:
            st.info("Nenhuma tabela no Cérebro ainda. Faça introspection no DW Explorer.")
        else:
            # Bulk AI generation
            if st.session_state.api_key:
                tables_without_desc = [t for t in dw_tables if not t.get("description_user") and not t.get("description_ai")]
                if tables_without_desc:
                    if st.button(f"🤖 Gerar descrições IA para {len(tables_without_desc)} tabelas sem descrição", use_container_width=True):
                        from src.knowledge.adf_ai_analyzer import ADFAIAnalyzer
                        ai_analyzer = ADFAIAnalyzer(brain)
                        with st.spinner("Gerando descrições..."):
                            cols_by_table = {
                                t["id"]: brain.get_dw_columns(t["id"]) for t in tables_without_desc[:20]
                            }
                            tables_for_ai = [
                                {**t, "columns": cols_by_table.get(t["id"], [])}
                                for t in tables_without_desc[:20]
                            ]
                            descriptions = ai_analyzer.generate_table_descriptions(tables_for_ai)
                            name_to_id = {t["full_name"]: t["id"] for t in dw_tables}
                            for d in descriptions:
                                tid = name_to_id.get(d.get("full_name", ""))
                                if tid and d.get("description"):
                                    brain.update_table_description(tid, d["description"], source="ai")
                        st.success(f"{len(descriptions)} descrições geradas!")
                        st.rerun()

            st.divider()
            filter_text = st.text_input("Buscar tabela", "", key="dict_filter")
            shown = [t for t in dw_tables if filter_text.lower() in t["full_name"].lower()] if filter_text else dw_tables

            for t in shown[:60]:
                user_desc = t.get("description_user") or ""
                ai_desc = t.get("description_ai") or ""
                displayed = user_desc or ai_desc
                source_tag = " *(editado)*" if user_desc else (" *(IA)*" if ai_desc else "")
                with st.expander(f"**{t['full_name']}** `{t['classification']}`{source_tag}"):
                    new_desc = st.text_area(
                        "Descrição",
                        value=displayed,
                        key=f"dict_desc_{t['id']}",
                        height=70,
                        help="Você edita — tem precedência sobre a IA",
                    )
                    if new_desc != displayed and new_desc.strip():
                        brain.update_table_description(t["id"], new_desc, "user")

    with tab_columns:
        dw_tables = brain.get_dw_tables()
        if not dw_tables:
            st.info("Nenhuma tabela no Cérebro.")
        else:
            selected_table = st.selectbox(
                "Selecione a tabela",
                [t["full_name"] for t in dw_tables],
                key="dict_table_select",
            )
            sel = next((t for t in dw_tables if t["full_name"] == selected_table), None)
            if sel:
                cols = brain.get_dw_columns(sel["id"])
                if not cols:
                    st.info("Nenhuma coluna registrada para esta tabela.")
                else:
                    for col in cols:
                        user_desc = col.get("description_user") or ""
                        ai_desc = col.get("description_ai") or ""
                        displayed = user_desc or ai_desc
                        with st.expander(f"`{col['column_name']}` — {col.get('data_type','')} {'🔑' if col['is_pk'] else '🔗' if col['is_fk'] else ''}"):
                            c1, c2 = st.columns(2)
                            new_desc = c1.text_input(
                                "Descrição",
                                value=displayed,
                                key=f"col_desc_{col['id']}",
                            )
                            sem_options = ["", "measure", "date_key", "status_flag", "natural_key", "surrogate_key", "degenerate_dim", "attribute"]
                            cur_sem = col.get("semantic_type") or ""
                            new_sem = c2.selectbox(
                                "Tipo semântico",
                                sem_options,
                                index=sem_options.index(cur_sem) if cur_sem in sem_options else 0,
                                key=f"col_sem_{col['id']}",
                            )
                            if new_desc != displayed and new_desc.strip():
                                brain.update_column_description(col["id"], new_desc, "user")

    with tab_export:
        st.subheader("Exportar Dicionário")
        dw_tables = brain.get_dw_tables()
        if not dw_tables:
            st.info("Nenhuma tabela para exportar.")
        else:
            if st.button("Gerar exportação", use_container_width=True):
                # Build markdown
                lines = ["# Dicionário de Dados — Credimorar\n"]
                for t in dw_tables:
                    desc = t.get("description_user") or t.get("description_ai") or "Sem descrição."
                    lines.append(f"## {t['full_name']} `{t['classification']}`")
                    lines.append(f"{desc}\n")
                    cols = brain.get_dw_columns(t["id"])
                    if cols:
                        lines.append("| Coluna | Tipo | PK | FK | Descrição |")
                        lines.append("|--------|------|----|----|-----------|")
                        for c in cols:
                            cdesc = c.get("description_user") or c.get("description_ai") or ""
                            lines.append(f"| `{c['column_name']}` | {c['data_type']} | {'✓' if c['is_pk'] else ''} | {'✓' if c['is_fk'] else ''} | {cdesc} |")
                    lines.append("")

                md_content = "\n".join(lines)

                # Build JSON
                export_json = []
                for t in dw_tables:
                    entry = {
                        "full_name": t["full_name"],
                        "classification": t["classification"],
                        "description": t.get("description_user") or t.get("description_ai") or "",
                        "columns": [
                            {
                                "name": c["column_name"],
                                "type": c["data_type"],
                                "is_pk": c["is_pk"],
                                "is_fk": c["is_fk"],
                                "semantic_type": c.get("semantic_type") or "",
                                "description": c.get("description_user") or c.get("description_ai") or "",
                            }
                            for c in brain.get_dw_columns(t["id"])
                        ],
                    }
                    export_json.append(entry)

                c1, c2 = st.columns(2)
                c1.download_button("⬇️ Download .md", md_content.encode("utf-8"), "dicionario_dados.md", "text/markdown", use_container_width=True)
                c2.download_button("⬇️ Download .json", json.dumps(export_json, ensure_ascii=False, indent=2).encode("utf-8"), "dicionario_dados.json", "application/json", use_container_width=True)
