from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("datamart_copilot.db")


class CopilotStore:
    """SQLite-backed persistence for connections, conversations, star schemas, and schema cache."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS connections (
                    name        TEXT PRIMARY KEY,
                    db_type     TEXT NOT NULL,
                    config      TEXT NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS star_schemas (
                    name        TEXT PRIMARY KEY,
                    schema_json TEXT NOT NULL,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS schema_cache (
                    connection_name TEXT PRIMARY KEY,
                    tables_json     TEXT NOT NULL,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS adf_pipelines (
                    name            TEXT PRIMARY KEY,
                    pipeline_json   TEXT NOT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL
                );
            """)

    # ── Connections ────────────────────────────────────────────────────────

    def save_connection(self, name: str, db_type: str, config: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO connections (name, db_type, config) VALUES (?,?,?)",
                (name, db_type, json.dumps(config)),
            )

    def load_connections(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT name, db_type, config FROM connections").fetchall()
        return [{"name": r["name"], "db_type": r["db_type"], **json.loads(r["config"])} for r in rows]

    def delete_connection(self, name: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM connections WHERE name=?", (name,))

    # ── Chat messages ──────────────────────────────────────────────────────

    def save_message(self, role: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (role, content) VALUES (?,?)",
                (role, content),
            )

    def load_messages(self) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages ORDER BY id"
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def clear_messages(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM messages")

    # ── Star schemas ───────────────────────────────────────────────────────

    def save_star_schema(self, name: str, schema_dict: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO star_schemas (name, schema_json, updated_at) VALUES (?,?, CURRENT_TIMESTAMP)",
                (name, json.dumps(schema_dict, default=str)),
            )

    def load_star_schemas(self) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT name, schema_json FROM star_schemas").fetchall()
        return {r["name"]: json.loads(r["schema_json"]) for r in rows}

    def delete_star_schema(self, name: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM star_schemas WHERE name=?", (name,))

    # ── Schema cache ───────────────────────────────────────────────────────

    def save_schema_cache(self, connection_name: str, tables: list[dict[str, Any]]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_cache (connection_name, tables_json, updated_at) VALUES (?,?, CURRENT_TIMESTAMP)",
                (connection_name, json.dumps(tables, default=str)),
            )

    def load_schema_cache(self, connection_name: str) -> Optional[list[dict[str, Any]]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT tables_json FROM schema_cache WHERE connection_name=?",
                (connection_name,),
            ).fetchone()
        return json.loads(row["tables_json"]) if row else None

    # ── ADF pipelines ──────────────────────────────────────────────────────

    def save_adf_pipeline(self, name: str, pipeline_dict: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO adf_pipelines (name, pipeline_json) VALUES (?,?)",
                (name, json.dumps(pipeline_dict, default=str)),
            )

    def load_adf_pipelines(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT pipeline_json FROM adf_pipelines").fetchall()
        return [json.loads(r["pipeline_json"]) for r in rows]

    # ── Settings ───────────────────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default
