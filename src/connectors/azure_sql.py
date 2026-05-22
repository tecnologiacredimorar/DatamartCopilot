from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Optional

import pyodbc
import pandas as pd
from sqlalchemy import create_engine, text, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.models.schemas import TableSchema, ColumnSchema, ForeignKeyRelation

logger = logging.getLogger(__name__)


class AzureSQLConnector:
    def __init__(
        self,
        server: str,
        database: str,
        username: str,
        password: str,
        driver: str = "ODBC Driver 18 for SQL Server",
    ):
        self.server = server
        self.database = database
        self.username = username
        self.password = password
        self.driver = driver
        self._engine: Optional[Engine] = None

    def connect(self) -> None:
        conn_str = (
            f"mssql+pyodbc://{self.username}:{self.password}"
            f"@{self.server}/{self.database}"
            f"?driver={self.driver.replace(' ', '+')}"
            f"&Encrypt=yes&TrustServerCertificate=no&Connection+Timeout=30"
        )
        self._engine = create_engine(conn_str, pool_pre_ping=True, pool_size=5)
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Connected to Azure SQL: %s/%s", self.server, self.database)

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

    def get_tables(self, schema_filter: Optional[str] = None) -> list[dict[str, str]]:
        query = """
            SELECT
                t.TABLE_SCHEMA,
                t.TABLE_NAME,
                t.TABLE_TYPE,
                CAST(p.rows AS BIGINT) as ROW_COUNT
            FROM INFORMATION_SCHEMA.TABLES t
            LEFT JOIN sys.tables st ON st.name = t.TABLE_NAME
            LEFT JOIN sys.partitions p ON p.object_id = st.object_id AND p.index_id IN (0,1)
            WHERE t.TABLE_TYPE = 'BASE TABLE'
        """
        params: dict[str, Any] = {}
        if schema_filter:
            query += " AND t.TABLE_SCHEMA = :schema"
            params["schema"] = schema_filter
        query += " ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME"

        with self._get_connection() as conn:
            result = conn.execute(text(query), params)
            return [dict(row._mapping) for row in result]

    def get_table_schema(self, schema_name: str, table_name: str) -> TableSchema:
        col_query = """
            SELECT
                c.COLUMN_NAME,
                c.DATA_TYPE,
                c.IS_NULLABLE,
                c.CHARACTER_MAXIMUM_LENGTH,
                c.NUMERIC_PRECISION,
                c.NUMERIC_SCALE,
                c.COLUMN_DEFAULT,
                CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END as IS_PK
            FROM INFORMATION_SCHEMA.COLUMNS c
            LEFT JOIN (
                SELECT ku.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                    ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                    AND tc.TABLE_SCHEMA = ku.TABLE_SCHEMA
                    AND tc.TABLE_NAME = ku.TABLE_NAME
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                    AND tc.TABLE_SCHEMA = :schema
                    AND tc.TABLE_NAME = :table
            ) pk ON pk.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.TABLE_SCHEMA = :schema AND c.TABLE_NAME = :table
            ORDER BY c.ORDINAL_POSITION
        """
        fk_query = """
            SELECT
                kcu.COLUMN_NAME,
                ccu.TABLE_NAME as REFERENCED_TABLE,
                ccu.COLUMN_NAME as REFERENCED_COLUMN
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu
                ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME
            WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
                AND tc.TABLE_SCHEMA = :schema
                AND tc.TABLE_NAME = :table
        """
        params = {"schema": schema_name, "table": table_name}

        with self._get_connection() as conn:
            col_rows = conn.execute(text(col_query), params).fetchall()
            fk_rows = conn.execute(text(fk_query), params).fetchall()

        columns = [
            ColumnSchema(
                name=row.COLUMN_NAME,
                data_type=row.DATA_TYPE,
                is_nullable=row.IS_NULLABLE == "YES",
                is_primary_key=bool(row.IS_PK),
                max_length=row.CHARACTER_MAXIMUM_LENGTH,
                precision=row.NUMERIC_PRECISION,
                scale=row.NUMERIC_SCALE,
                default_value=row.COLUMN_DEFAULT,
            )
            for row in col_rows
        ]
        foreign_keys = [
            ForeignKeyRelation(
                column=row.COLUMN_NAME,
                referenced_table=row.REFERENCED_TABLE,
                referenced_column=row.REFERENCED_COLUMN,
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
        query = f"SELECT TOP {limit} * FROM [{schema_name}].[{table_name}]"
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
                ts = self.get_table_schema(t["TABLE_SCHEMA"], t["TABLE_NAME"])
                ts.row_count = t.get("ROW_COUNT")
                result.append(ts)
            except SQLAlchemyError as e:
                logger.warning("Skipping %s.%s: %s", t["TABLE_SCHEMA"], t["TABLE_NAME"], e)
        return result
