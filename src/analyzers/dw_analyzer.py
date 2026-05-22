from __future__ import annotations

import re
import logging
from typing import Any, Optional

from src.models.schemas import TableSchema, ColumnSchema

logger = logging.getLogger(__name__)

FACT_PATTERNS = re.compile(
    r"^(fact_|fct_|ft_|f_)|_(fact|fct|transactions?|sales?|orders?|events?)$",
    re.IGNORECASE,
)
DIM_PATTERNS = re.compile(
    r"^(dim_|d_|dimension_)|_(dim|dimension|lookup|ref|master)$",
    re.IGNORECASE,
)
DATE_COLUMNS = {"date_key", "data_key", "dt_key", "date_id", "data_id", "period_key"}
MEASURE_TYPES = {"int", "bigint", "decimal", "numeric", "float", "double", "money", "real"}


class DWAnalyzer:
    def __init__(self, tables: list[TableSchema]):
        self.tables = tables
        self._by_name: dict[str, TableSchema] = {t.full_name: t for t in tables}

    def classify_tables(self) -> None:
        for table in self.tables:
            name_lower = table.table_name.lower()
            table.is_fact_table = bool(FACT_PATTERNS.search(name_lower))
            table.is_dimension_table = bool(DIM_PATTERNS.search(name_lower))
            if not table.is_fact_table and not table.is_dimension_table:
                table.is_fact_table = self._heuristic_is_fact(table)
                table.is_dimension_table = not table.is_fact_table and self._heuristic_is_dim(table)

    def _heuristic_is_fact(self, table: TableSchema) -> bool:
        fk_count = sum(1 for c in table.columns if c.is_foreign_key)
        measure_count = sum(
            1 for c in table.columns
            if any(c.data_type.lower().startswith(t) for t in MEASURE_TYPES)
            and not c.is_primary_key and not c.is_foreign_key
        )
        return fk_count >= 2 and measure_count >= 1

    def _heuristic_is_dim(self, table: TableSchema) -> bool:
        pk_count = len(table.primary_keys)
        col_count = len(table.columns)
        text_count = sum(
            1 for c in table.columns
            if c.data_type.lower() in ("varchar", "nvarchar", "char", "text", "character varying")
        )
        return pk_count == 1 and col_count >= 3 and text_count >= 1

    def get_existing_datamarts(self) -> list[dict[str, Any]]:
        self.classify_tables()
        facts = [t for t in self.tables if t.is_fact_table]
        datamarts = []
        for fact in facts:
            related_dims = self._find_related_dimensions(fact)
            datamarts.append(
                {
                    "fact_table": fact.full_name,
                    "dimension_tables": [d.full_name for d in related_dims],
                    "measures": self._get_measures(fact),
                    "grain": self._infer_grain(fact),
                    "row_count": fact.row_count,
                }
            )
        return datamarts

    def _find_related_dimensions(self, fact: TableSchema) -> list[TableSchema]:
        related = []
        fk_targets = {fk.referenced_table for fk in fact.foreign_keys}
        for table in self.tables:
            if table.table_name in fk_targets or table.full_name in fk_targets:
                if table.is_dimension_table or DIM_PATTERNS.search(table.table_name.lower()):
                    related.append(table)
        return related

    def _get_measures(self, fact: TableSchema) -> list[str]:
        return [
            c.name for c in fact.columns
            if any(c.data_type.lower().startswith(t) for t in MEASURE_TYPES)
            and not c.is_primary_key and not c.is_foreign_key
        ]

    def _infer_grain(self, fact: TableSchema) -> str:
        pk_cols = fact.primary_keys
        fk_cols = [fk.referenced_table for fk in fact.foreign_keys]
        date_cols = [
            c.name for c in fact.columns
            if c.name.lower() in DATE_COLUMNS or "date" in c.name.lower()
        ]
        grain_parts = pk_cols or fk_cols[:3]
        if date_cols:
            grain_parts = date_cols[:1] + [g for g in grain_parts if g not in date_cols]
        if grain_parts:
            return "One row per " + ", ".join(grain_parts[:3])
        return "Unknown grain"

    def map_relationships(self) -> dict[str, list[str]]:
        graph: dict[str, list[str]] = {}
        for table in self.tables:
            deps = []
            for fk in table.foreign_keys:
                for other in self.tables:
                    if other.table_name == fk.referenced_table:
                        deps.append(other.full_name)
            graph[table.full_name] = deps
        return graph

    def find_conformed_dimensions(self) -> list[str]:
        self.classify_tables()
        dims = [t for t in self.tables if t.is_dimension_table]
        facts = [t for t in self.tables if t.is_fact_table]

        conformed = []
        for dim in dims:
            referencing_facts = 0
            for fact in facts:
                fk_targets = {fk.referenced_table for fk in fact.foreign_keys}
                if dim.table_name in fk_targets or dim.full_name in fk_targets:
                    referencing_facts += 1
            if referencing_facts > 1:
                conformed.append(dim.full_name)
        return conformed

    def suggest_bus_matrix(self) -> dict[str, dict[str, bool]]:
        self.classify_tables()
        facts = [t for t in self.tables if t.is_fact_table]
        dims = [t for t in self.tables if t.is_dimension_table]

        matrix: dict[str, dict[str, bool]] = {}
        for fact in facts:
            fk_targets = {fk.referenced_table for fk in fact.foreign_keys}
            matrix[fact.full_name] = {
                dim.full_name: (
                    dim.table_name in fk_targets or dim.full_name in fk_targets
                )
                for dim in dims
            }
        return matrix

    def get_schema_summary(self) -> dict[str, Any]:
        self.classify_tables()
        return {
            "total_tables": len(self.tables),
            "fact_tables": [t.full_name for t in self.tables if t.is_fact_table],
            "dimension_tables": [t.full_name for t in self.tables if t.is_dimension_table],
            "unclassified_tables": [
                t.full_name
                for t in self.tables
                if not t.is_fact_table and not t.is_dimension_table
            ],
            "conformed_dimensions": self.find_conformed_dimensions(),
            "total_columns": sum(len(t.columns) for t in self.tables),
        }
