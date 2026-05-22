from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.models.schemas import TableSchema, ColumnSchema, ForeignKeyRelation

logger = logging.getLogger(__name__)


class PostgreSQLConnector:
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self._engine: Optional[Engine] = None

    def connect(self) -> None:
        conn_str = (
            f"postgresql+psycopg2://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )
        self._engine = create_engine(conn_str, pool_pre_ping=True, pool_size=5)
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Connected to PostgreSQL: %s:%s/%s", self.host, self.port, self.database)

    def disconnect(self) -> None:
        if self._engine:
            self._engine.dispose()
            self._engine = None

    @contextmanager
    def _get_connection(self):
        if not self._engine:
            raise RuntimeError("Not connected. Call connect() first.")
        with self._engine.connect() as conn:
            yield conn

    def get_tables(self, schema_filter: Optional[str] = None) -> list[dict[str, Any]]:
        query = """
            SELECT
                t.table_schema,
                t.table_name,
                t.table_type,
                pg_class.reltuples::BIGINT as row_count
            FROM information_schema.tables t
            LEFT JOIN pg_class ON pg_class.relname = t.table_name
            WHERE t.table_type = 'BASE TABLE'
              AND t.table_schema NOT IN ('pg_catalog', 'information_schema')
        """
        params: dict[str, Any] = {}
        if schema_filter:
            query += " AND t.table_schema = :schema"
            params["schema"] = schema_filter
        query += " ORDER BY t.table_schema, t.table_name"

        with self._get_connection() as conn:
            result = conn.execute(text(query), params)
            return [dict(row._mapping) for row in result]

    def get_table_schema(self, schema_name: str, table_name: str) -> TableSchema:
        col_query = """
            SELECT
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.character_maximum_length,
                c.numeric_precision,
                c.numeric_scale,
                c.column_default,
                CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END as is_pk
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                    AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = :schema
                  AND tc.table_name = :table
            ) pk ON pk.column_name = c.column_name
            WHERE c.table_schema = :schema AND c.table_name = :table
            ORDER BY c.ordinal_position
        """
        fk_query = """
            SELECT
                kcu.column_name,
                ccu.table_name as referenced_table,
                ccu.column_name as referenced_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON rc.unique_constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = :schema
              AND tc.table_name = :table
        """
        params = {"schema": schema_name, "table": table_name}

        with self._get_connection() as conn:
            col_rows = conn.execute(text(col_query), params).fetchall()
            fk_rows = conn.execute(text(fk_query), params).fetchall()

        columns = [
            ColumnSchema(
                name=row.column_name,
                data_type=row.data_type,
                is_nullable=row.is_nullable == "YES",
                is_primary_key=bool(row.is_pk),
                max_length=row.character_maximum_length,
                precision=row.numeric_precision,
                scale=row.numeric_scale,
                default_value=row.column_default,
            )
            for row in col_rows
        ]
        foreign_keys = [
            ForeignKeyRelation(
                column=row.column_name,
                referenced_table=row.referenced_table,
                referenced_column=row.referenced_column,
            )
            for row in fk_rows
        ]
        pk_cols = [c.name for c in columns if c.is_primary_key]
        for c in columns:
            if any(fk.column == c.name for fk in foreign_keys):
                c.is_foreign_key = True

        return TableSchema(
            schema_name=schema_name,
            table_name=table_name,
            full_name=f"{schema_name}.{table_name}",
            columns=columns,
            primary_keys=pk_cols,
            foreign_keys=foreign_keys,
        )

    def get_table_sample(
        self, schema_name: str, table_name: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        query = f'SELECT * FROM "{schema_name}"."{table_name}" LIMIT {limit}'
        with self._get_connection() as conn:
            df = pd.read_sql(query, conn)
        return df.to_dict(orient="records")

    def execute_query(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        with self._get_connection() as conn:
            return pd.read_sql(text(sql), conn, params=params)

    def introspect_schema(self, schema_filter: Optional[str] = None) -> list[TableSchema]:
        tables = self.get_tables(schema_filter)
        result = []
        for t in tables:
            try:
                ts = self.get_table_schema(t["table_schema"], t["table_name"])
                ts.row_count = t.get("row_count")
                result.append(ts)
            except SQLAlchemyError as e:
                logger.warning("Skipping %s.%s: %s", t["table_schema"], t["table_name"], e)
        return result
