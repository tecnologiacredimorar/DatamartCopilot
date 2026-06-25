from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("datamart_copilot.db")


class KnowledgeBrain:
    """Central knowledge base — the persistent memory of the Copilot."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_tables(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kb_dw_tables (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_name TEXT NOT NULL,
                    schema_name     TEXT NOT NULL,
                    table_name      TEXT NOT NULL,
                    full_name       TEXT NOT NULL,
                    classification  TEXT DEFAULT 'unknown',
                    description_ai  TEXT,
                    description_user TEXT,
                    row_count       INTEGER,
                    last_synced_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(connection_name, full_name)
                );

                CREATE TABLE IF NOT EXISTS kb_dw_columns (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id        INTEGER NOT NULL REFERENCES kb_dw_tables(id) ON DELETE CASCADE,
                    column_name     TEXT NOT NULL,
                    data_type       TEXT,
                    is_pk           BOOLEAN DEFAULT FALSE,
                    is_fk           BOOLEAN DEFAULT FALSE,
                    fk_references   TEXT,
                    description_ai  TEXT,
                    description_user TEXT,
                    semantic_type   TEXT,
                    sample_values   TEXT,
                    UNIQUE(table_id, column_name)
                );

                CREATE TABLE IF NOT EXISTS kb_adf_pipelines (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    pipeline_name       TEXT UNIQUE NOT NULL,
                    raw_json            TEXT,
                    business_description TEXT,
                    load_strategy       TEXT DEFAULT 'unknown',
                    watermark_column    TEXT,
                    source_tables       TEXT DEFAULT '[]',
                    target_tables       TEXT DEFAULT '[]',
                    problems_found      TEXT DEFAULT '[]',
                    suggestions         TEXT DEFAULT '[]',
                    confirmed_by_user   BOOLEAN DEFAULT FALSE,
                    user_corrections    TEXT,
                    analyzed_at         TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS kb_context (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    category    TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    source      TEXT DEFAULT 'user',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(category, key)
                );

                CREATE TABLE IF NOT EXISTS kb_naming_patterns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope       TEXT,
                    pattern     TEXT NOT NULL,
                    meaning     TEXT,
                    examples    TEXT DEFAULT '[]',
                    confirmed   BOOLEAN DEFAULT FALSE,
                    UNIQUE(scope, pattern)
                );

                CREATE TABLE IF NOT EXISTS kb_glossary (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    term             TEXT UNIQUE NOT NULL,
                    definition       TEXT NOT NULL,
                    business_context TEXT,
                    related_tables   TEXT DEFAULT '[]',
                    related_columns  TEXT DEFAULT '[]',
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS kb_datamart_requests (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    title               TEXT NOT NULL,
                    description_raw     TEXT NOT NULL,
                    source_query        TEXT,
                    status              TEXT DEFAULT 'intake',
                    clarification_qa    TEXT DEFAULT '[]',
                    brain_context_used  TEXT,
                    star_schema_id      TEXT,
                    artifacts           TEXT DEFAULT '{}',
                    kimball_notes       TEXT,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        # executescript() issues its own COMMIT, so run migrations in a fresh connection
        with self._conn() as conn:
            for col_sql in (
                "ALTER TABLE kb_datamart_requests ADD COLUMN analysis_json TEXT",
                "ALTER TABLE kb_datamart_requests ADD COLUMN design_json TEXT",
            ):
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass  # column already exists

    # ── DW Tables ──────────────────────────────────────────────────────────

    def sync_dw_tables(self, connection_name: str, tables: list[dict[str, Any]]) -> None:
        with self._conn() as conn:
            for t in tables:
                conn.execute("""
                    INSERT INTO kb_dw_tables
                        (connection_name, schema_name, table_name, full_name, classification, row_count, last_synced_at)
                    VALUES (?,?,?,?,?,?, CURRENT_TIMESTAMP)
                    ON CONFLICT(connection_name, full_name) DO UPDATE SET
                        row_count=excluded.row_count,
                        last_synced_at=CURRENT_TIMESTAMP
                """, (
                    connection_name,
                    t.get("schema_name", ""),
                    t.get("table_name", ""),
                    t.get("full_name", ""),
                    t.get("classification", "unknown"),
                    t.get("row_count"),
                ))

                row = conn.execute(
                    "SELECT id FROM kb_dw_tables WHERE connection_name=? AND full_name=?",
                    (connection_name, t.get("full_name", "")),
                ).fetchone()
                if not row:
                    continue
                table_id = row["id"]

                for col in t.get("columns", []):
                    fk_ref = None
                    for fk in t.get("foreign_keys", []):
                        if fk.get("column") == col.get("name"):
                            fk_ref = f"{fk.get('referenced_table')}.{fk.get('referenced_column')}"
                    conn.execute("""
                        INSERT INTO kb_dw_columns
                            (table_id, column_name, data_type, is_pk, is_fk, fk_references)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(table_id, column_name) DO UPDATE SET
                            data_type=excluded.data_type,
                            is_pk=excluded.is_pk,
                            is_fk=excluded.is_fk,
                            fk_references=excluded.fk_references
                    """, (
                        table_id,
                        col.get("name", ""),
                        col.get("data_type", ""),
                        col.get("is_primary_key", False),
                        col.get("is_foreign_key", False),
                        fk_ref,
                    ))

    def get_dw_tables(self, connection_name: Optional[str] = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if connection_name:
                rows = conn.execute(
                    "SELECT * FROM kb_dw_tables WHERE connection_name=? ORDER BY schema_name, table_name",
                    (connection_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM kb_dw_tables ORDER BY connection_name, schema_name, table_name"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_dw_columns(self, table_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_dw_columns WHERE table_id=? ORDER BY id",
                (table_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_table_description(self, table_id: int, description: str, source: str = "user") -> None:
        field = "description_user" if source == "user" else "description_ai"
        with self._conn() as conn:
            conn.execute(f"UPDATE kb_dw_tables SET {field}=? WHERE id=?", (description, table_id))

    def update_column_description(self, column_id: int, description: str, source: str = "user") -> None:
        field = "description_user" if source == "user" else "description_ai"
        with self._conn() as conn:
            conn.execute(f"UPDATE kb_dw_columns SET {field}=? WHERE id=?", (description, column_id))

    def update_table_classification(self, table_id: int, classification: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE kb_dw_tables SET classification=? WHERE id=?", (classification, table_id))

    # ── ADF Pipelines ──────────────────────────────────────────────────────

    def save_adf_pipeline(self, name: str, raw_json: dict) -> int:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO kb_adf_pipelines (pipeline_name, raw_json)
                VALUES (?,?)
                ON CONFLICT(pipeline_name) DO UPDATE SET raw_json=excluded.raw_json
            """, (name, json.dumps(raw_json, default=str)))
            row = conn.execute("SELECT id FROM kb_adf_pipelines WHERE pipeline_name=?", (name,)).fetchone()
        return row["id"]

    def save_adf_analysis(self, pipeline_name: str, analysis: dict) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE kb_adf_pipelines SET
                    business_description=?,
                    load_strategy=?,
                    watermark_column=?,
                    source_tables=?,
                    target_tables=?,
                    problems_found=?,
                    suggestions=?,
                    analyzed_at=CURRENT_TIMESTAMP
                WHERE pipeline_name=?
            """, (
                analysis.get("business_description"),
                analysis.get("load_strategy", "unknown"),
                analysis.get("watermark_column"),
                json.dumps(analysis.get("source_tables", []), ensure_ascii=False),
                json.dumps(analysis.get("target_tables", []), ensure_ascii=False),
                json.dumps(analysis.get("problems_found", []), ensure_ascii=False),
                json.dumps(analysis.get("suggestions", []), ensure_ascii=False),
                pipeline_name,
            ))

    def confirm_adf_analysis(self, pipeline_name: str, corrections: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE kb_adf_pipelines SET confirmed_by_user=TRUE, user_corrections=?
                WHERE pipeline_name=?
            """, (corrections, pipeline_name))

    def get_adf_pipelines(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM kb_adf_pipelines ORDER BY pipeline_name").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for field in ("source_tables", "target_tables", "problems_found", "suggestions"):
                try:
                    d[field] = json.loads(d[field] or "[]")
                except Exception:
                    d[field] = []
            result.append(d)
        return result

    # ── Context (free-form knowledge) ──────────────────────────────────────

    def save_context(self, category: str, key: str, value: str, source: str = "user") -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO kb_context (category, key, value, source, updated_at)
                VALUES (?,?,?,?, CURRENT_TIMESTAMP)
                ON CONFLICT(category, key) DO UPDATE SET
                    value=excluded.value,
                    source=excluded.source,
                    updated_at=CURRENT_TIMESTAMP
            """, (category, key, value, source))

    def delete_context(self, context_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM kb_context WHERE id=?", (context_id,))

    def get_all_context(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_context ORDER BY category, key"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Naming patterns ────────────────────────────────────────────────────

    def save_naming_pattern(self, scope: str, pattern: str, meaning: str, examples: list[str]) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO kb_naming_patterns (scope, pattern, meaning, examples)
                VALUES (?,?,?,?)
                ON CONFLICT(scope, pattern) DO UPDATE SET
                    meaning=excluded.meaning,
                    examples=excluded.examples
            """, (scope, pattern, meaning, json.dumps(examples)))

    def confirm_naming_pattern(self, pattern_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE kb_naming_patterns SET confirmed=TRUE WHERE id=?", (pattern_id,))

    def get_naming_patterns(self, confirmed_only: bool = False) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if confirmed_only:
                rows = conn.execute("SELECT * FROM kb_naming_patterns WHERE confirmed=TRUE ORDER BY scope, pattern").fetchall()
            else:
                rows = conn.execute("SELECT * FROM kb_naming_patterns ORDER BY scope, pattern").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["examples"] = json.loads(d.get("examples") or "[]")
            except Exception:
                d["examples"] = []
            result.append(d)
        return result

    # ── Glossary ───────────────────────────────────────────────────────────

    def save_glossary_term(self, term: str, definition: str, business_context: str = "", related_tables: list[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO kb_glossary (term, definition, business_context, related_tables)
                VALUES (?,?,?,?)
                ON CONFLICT(term) DO UPDATE SET
                    definition=excluded.definition,
                    business_context=excluded.business_context,
                    related_tables=excluded.related_tables
            """, (term, definition, business_context, json.dumps(related_tables or [])))

    def delete_glossary_term(self, term_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM kb_glossary WHERE id=?", (term_id,))

    def get_glossary(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM kb_glossary ORDER BY term").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["related_tables"] = json.loads(d.get("related_tables") or "[]")
            except Exception:
                d["related_tables"] = []
            result.append(d)
        return result

    # ── Datamart requests ──────────────────────────────────────────────────

    def create_request(self, title: str, description: str, source_query: Optional[str] = None) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO kb_datamart_requests (title, description_raw, source_query)
                VALUES (?,?,?)
            """, (title, description, source_query))
        return cur.lastrowid

    def update_request(self, request_id: int, **fields) -> None:
        # Drop None values — passing None would attempt to SET a column to NULL
        # which crashes if the column doesn't exist yet (e.g. before migration).
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [request_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE kb_datamart_requests SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                values,
            )

    def get_request(self, request_id: int) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM kb_datamart_requests WHERE id=?", (request_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        for field, default in (
            ("clarification_qa", "[]"),
            ("artifacts", "{}"),
            ("analysis_json", "{}"),
            ("design_json", "{}"),
        ):
            try:
                d[field] = json.loads(d[field] or default)
            except Exception:
                d[field] = {} if field != "clarification_qa" else []
        return d

    def get_all_requests(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, status, created_at, updated_at FROM kb_datamart_requests ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Context builder for AI ─────────────────────────────────────────────

    def build_ai_context(self) -> str:
        """Build a concise text block to inject into AI system prompts."""
        parts = []

        # Free context (most important — user-written)
        ctx_rows = self.get_all_context()
        if ctx_rows:
            by_cat: dict[str, list[str]] = {}
            for row in ctx_rows:
                by_cat.setdefault(row["category"], []).append(f"- {row['key']}: {row['value']}")
            parts.append("## Conhecimento do ambiente (Credimorar)")
            for cat, items in by_cat.items():
                parts.append(f"\n### {cat.replace('_', ' ').title()}")
                parts.extend(items)

        # Naming patterns (confirmed only)
        patterns = self.get_naming_patterns(confirmed_only=True)
        if patterns:
            parts.append("\n## Padrões de nomenclatura confirmados")
            for p in patterns:
                ex = ", ".join(p["examples"][:3]) if p["examples"] else ""
                parts.append(f"- `{p['pattern']}` ({p['scope']}): {p['meaning']}" + (f" — ex: {ex}" if ex else ""))

        # Glossary
        glossary = self.get_glossary()
        if glossary:
            parts.append("\n## Glossário de negócio")
            for g in glossary[:20]:
                parts.append(f"- **{g['term']}**: {g['definition']}")

        # DW structure summary
        tables = self.get_dw_tables()
        if tables:
            facts = [t for t in tables if t["classification"] == "fact"]
            dims = [t for t in tables if t["classification"] == "dimension"]
            staging = [t for t in tables if t["classification"] == "staging"]
            parts.append(f"\n## Estrutura do DW ({len(tables)} tabelas)")
            if facts:
                parts.append(f"Facts: {', '.join(t['full_name'] for t in facts[:10])}")
            if dims:
                parts.append(f"Dimensões: {', '.join(t['full_name'] for t in dims[:15])}")
            if staging:
                parts.append(f"Staging: {', '.join(t['full_name'] for t in staging[:10])}")

        # ADF pipelines summary
        pipelines = self.get_adf_pipelines()
        analyzed = [p for p in pipelines if p.get("business_description")]
        if analyzed:
            parts.append(f"\n## Pipelines ADF ({len(analyzed)} analisados)")
            for p in analyzed[:10]:
                strategy = p.get("load_strategy", "?")
                parts.append(f"- **{p['pipeline_name']}** ({strategy}): {p.get('business_description', '')[:120]}")

        return "\n".join(parts) if parts else "(Cérebro ainda vazio — adicione contexto na aba Cérebro)"

    def detect_naming_patterns(self, tables: list[dict[str, Any]]) -> None:
        """Auto-detect naming patterns from table list."""
        from collections import Counter
        prefixes: Counter = Counter()
        schema_names: Counter = Counter()

        for t in tables:
            schema_names[t.get("schema_name", "")] += 1
            name = t.get("table_name", "")
            for prefix in ("fact_", "fct_", "dim_", "d_", "stg_", "stage_", "pub_", "int_", "src_"):
                if name.lower().startswith(prefix):
                    prefixes[prefix] += 1

        prefix_meanings = {
            "fact_": "Tabela fato Kimball",
            "fct_": "Tabela fato Kimball",
            "dim_": "Tabela dimensão Kimball",
            "d_": "Tabela dimensão (abreviado)",
            "stg_": "Camada staging — dados brutos",
            "stage_": "Camada staging — dados brutos",
            "pub_": "Camada publicação — consumo Power BI",
            "int_": "Camada intermediária — lógica de negócio",
            "src_": "Dados fonte — origem externa",
        }
        for prefix, count in prefixes.items():
            if count >= 1:
                examples = [t["full_name"] for t in tables if t.get("table_name", "").lower().startswith(prefix)][:3]
                self.save_naming_pattern("table_prefix", prefix, prefix_meanings.get(prefix, ""), examples)

        for schema, count in schema_names.items():
            if schema and count >= 2:
                self.save_naming_pattern("schema", schema, f"Schema com {count} tabelas", [])
