from __future__ import annotations

import re
from typing import Any

from src.models.schemas import (
    ColumnSchema,
    DimensionColumn,
    DimensionTable,
    FactColumn,
    FactTable,
    FactTableType,
    KPISuggestion,
    SCDType,
    StarSchema,
    TableSchema,
)

NUMERIC_TYPES = {"int", "bigint", "decimal", "numeric", "float", "double", "money", "real", "smallint"}
DATE_HINT = re.compile(r"(date|dt|period|month|year|week|day)", re.IGNORECASE)
STATUS_HINT = re.compile(r"(status|state|flag|type|category|code)", re.IGNORECASE)


class StarSchemaGenerator:
    def generate_star_schema(
        self,
        name: str,
        subject_area: str,
        fact_source_tables: list[TableSchema],
        dimension_source_tables: list[TableSchema],
        description: str = "",
        measures: list[str] | None = None,
    ) -> StarSchema:
        fact = self.design_fact_table(
            name=f"fact_{name.lower().replace(' ', '_')}",
            source_tables=fact_source_tables,
            explicit_measures=measures or [],
        )
        dims = [
            self.design_dimension(table)
            for table in dimension_source_tables
        ]
        for dim in dims:
            fk_col = FactColumn(
                name=f"{dim.name}_key",
                data_type="INT",
                is_foreign_key=True,
                referenced_dimension=dim.name,
                description=f"FK to {dim.name}",
            )
            fact.columns.insert(1, fk_col)
            if dim.name not in fact.dimensions:
                fact.dimensions.append(dim.name)

        bus_matrix = {dim.name: True for dim in dims}
        kpis = self._suggest_kpis(fact, dims)

        return StarSchema(
            name=name,
            description=description or f"Star schema for {subject_area}",
            subject_area=subject_area,
            fact_table=fact,
            dimension_tables=dims,
            bus_matrix_row=bus_matrix,
            kpi_opportunities=[k.name for k in kpis],
        )

    def design_fact_table(
        self,
        name: str,
        source_tables: list[TableSchema],
        explicit_measures: list[str] | None = None,
    ) -> FactTable:
        all_columns: list[ColumnSchema] = []
        for t in source_tables:
            all_columns.extend(t.columns)

        sk_col = FactColumn(
            name=f"{name}_key",
            data_type="BIGINT",
            description="Surrogate key (auto-increment)",
        )

        measure_cols = []
        for col in all_columns:
            is_measure = (
                any(col.data_type.lower().startswith(t) for t in NUMERIC_TYPES)
                and not col.is_primary_key
                and not col.is_foreign_key
                and not DATE_HINT.search(col.name)
            )
            if explicit_measures:
                is_measure = col.name in explicit_measures
            if is_measure:
                measure_cols.append(
                    FactColumn(
                        name=col.name,
                        data_type=col.data_type,
                        is_measure=True,
                        aggregation=self._infer_aggregation(col.name),
                        description=f"Measure: {col.name}",
                    )
                )

        date_cols = [
            FactColumn(
                name=col.name,
                data_type="INT",
                is_foreign_key=True,
                referenced_dimension="dim_date",
                description="FK to dim_date",
            )
            for col in all_columns
            if DATE_HINT.search(col.name) and col.data_type.lower() in ("date", "datetime", "timestamp")
        ]

        fact_type = FactTableType.TRANSACTION
        if any("snapshot" in t.table_name.lower() for t in source_tables):
            fact_type = FactTableType.PERIODIC_SNAPSHOT

        return FactTable(
            name=name,
            description=f"Fact table built from: {', '.join(t.full_name for t in source_tables)}",
            grain=self._define_grain(source_tables, date_cols, measure_cols),
            fact_type=fact_type,
            columns=[sk_col] + date_cols + measure_cols,
            measures=[c.name for c in measure_cols],
            source_tables=[t.full_name for t in source_tables],
            partitioning_column=date_cols[0].name if date_cols else None,
        )

    def design_dimension(self, source: TableSchema) -> DimensionTable:
        raw_name = source.table_name.lower()
        dim_name = raw_name if raw_name.startswith("dim_") else f"dim_{raw_name.lstrip('d_dim_')}"

        scd_type = SCDType.TYPE1
        has_status = any(STATUS_HINT.search(c.name) for c in source.columns)
        has_date_range = any(
            "valid" in c.name.lower() or "effective" in c.name.lower() or "expir" in c.name.lower()
            for c in source.columns
        )
        if has_date_range or len(source.columns) > 8:
            scd_type = SCDType.TYPE2

        sk = DimensionColumn(
            name=f"{dim_name}_key",
            data_type="INT",
            is_surrogate_key=True,
            description="Surrogate key",
        )
        nk_cols = [
            DimensionColumn(
                name=col.name,
                data_type=col.data_type,
                is_natural_key=col.is_primary_key,
                description=f"Natural key from source" if col.is_primary_key else None,
            )
            for col in source.columns
            if not col.is_primary_key or col == source.columns[0]
        ]

        scd_tracking_cols = []
        if scd_type == SCDType.TYPE2:
            scd_tracking_cols = [
                DimensionColumn(name="valid_from", data_type="DATE", is_scd_tracking=True, scd_type=SCDType.TYPE2),
                DimensionColumn(name="valid_to", data_type="DATE", is_scd_tracking=True, scd_type=SCDType.TYPE2),
                DimensionColumn(name="is_current", data_type="BIT", is_scd_tracking=True, scd_type=SCDType.TYPE2),
            ]

        return DimensionTable(
            name=dim_name,
            description=f"Dimension built from {source.full_name}",
            grain=f"One row per unique {source.table_name}",
            scd_type=scd_type,
            columns=[sk] + nk_cols + scd_tracking_cols,
            source_tables=[source.full_name],
            conformed=False,
        )

    def _define_grain(
        self,
        sources: list[TableSchema],
        date_cols: list[FactColumn],
        measures: list[FactColumn],
    ) -> str:
        parts = []
        if date_cols:
            parts.append(date_cols[0].name.replace("_key", "").replace("_id", ""))
        for s in sources[:2]:
            parts.append(s.table_name)
        if measures:
            parts.append(f"measuring {', '.join(m.name for m in measures[:2])}")
        return "One row per " + " + ".join(parts) if parts else "One row per event"

    def _infer_aggregation(self, col_name: str) -> str:
        name = col_name.lower()
        if any(w in name for w in ("qty", "quantity", "count", "cnt", "num")):
            return "SUM"
        if any(w in name for w in ("avg", "average", "rate", "ratio", "pct", "percent")):
            return "AVG"
        if any(w in name for w in ("max", "peak", "highest")):
            return "MAX"
        if any(w in name for w in ("min", "lowest")):
            return "MIN"
        return "SUM"

    def _suggest_kpis(
        self, fact: FactTable, dims: list[DimensionTable]
    ) -> list[KPISuggestion]:
        kpis = []
        measures = fact.measures[:4]
        dim_names = [d.name.replace("dim_", "") for d in dims[:3]]

        for measure in measures:
            agg = self._infer_aggregation(measure)
            kpis.append(
                KPISuggestion(
                    name=f"Total {measure.replace('_', ' ').title()}",
                    description=f"Aggregate {measure} across all {fact.name}",
                    formula=f"{agg}({measure})",
                    dimensions=dim_names,
                    business_value=f"Track overall performance of {measure}",
                    sql_example=(
                        f"SELECT {', '.join(dim_names[:2]) + ', ' if dim_names else ''}"
                        f"{agg}({measure}) as total_{measure}\n"
                        f"FROM {fact.name}\n"
                        + (f"JOIN {dims[0].name} USING ({dims[0].name}_key)\n" if dims else "")
                        + f"GROUP BY {', '.join(dim_names[:2])}"
                        if dim_names else f"SELECT {agg}({measure}) FROM {fact.name}"
                    ),
                )
            )
        return kpis
