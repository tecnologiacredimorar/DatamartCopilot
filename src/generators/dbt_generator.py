from __future__ import annotations

import textwrap
from typing import Any

import yaml

from src.models.schemas import (
    DBTModel,
    DBTProject,
    DimensionTable,
    FactTable,
    SCDType,
    StarSchema,
    TableSchema,
)


class DBTGenerator:
    def generate_project(self, star_schema: StarSchema, project_name: str) -> DBTProject:
        models: list[DBTModel] = []

        for src_table in star_schema.fact_table.source_tables:
            models.append(self._staging_model(src_table, project_name))

        for dim in star_schema.dimension_tables:
            for src_table in dim.source_tables:
                models.append(self._staging_model(src_table, project_name))
            models.append(self._dimension_model(dim, project_name))

        models.append(self._fact_model(star_schema.fact_table, star_schema.dimension_tables, project_name))

        sources_yaml = self._sources_yaml(star_schema, project_name)
        dbt_project_yaml = self._dbt_project_yaml(project_name)

        return DBTProject(
            name=project_name,
            models=models,
            sources_yaml=sources_yaml,
            dbt_project_yaml=dbt_project_yaml,
        )

    def _staging_model(self, source_full_name: str, project_name: str) -> DBTModel:
        parts = source_full_name.split(".")
        schema = parts[0] if len(parts) > 1 else "dbo"
        table = parts[-1]
        model_name = f"stg_{table.lower()}"

        sql = textwrap.dedent(f"""\
            with source as (
                select * from {{{{ source('{project_name}', '{table}') }}}}
            ),
            renamed as (
                select
                    *
                    -- TODO: rename and cast columns as needed
                from source
            )
            select * from renamed
        """)
        schema_yaml = self._model_schema_yaml(model_name, f"Staging model for {source_full_name}", [])
        return DBTModel(
            name=model_name,
            layer="staging",
            schema_name="staging",
            description=f"Staging model for {source_full_name}",
            sql_content=sql,
            schema_yaml=schema_yaml,
            materialization="view",
            tags=["staging"],
            depends_on=[source_full_name],
        )

    def _dimension_model(self, dim: DimensionTable, project_name: str) -> DBTModel:
        stg_deps = [f"stg_{t.split('.')[-1].lower()}" for t in dim.source_tables]
        src_ref = stg_deps[0] if stg_deps else "stg_source"

        sk_col = f"{dim.name}_key"
        natural_keys = [c.name for c in dim.columns if c.is_natural_key]
        attribute_cols = [
            c.name for c in dim.columns
            if not c.is_surrogate_key and not c.is_scd_tracking
        ]

        if dim.scd_type == SCDType.TYPE2:
            sql = textwrap.dedent(f"""\
                with source as (
                    select * from {{{{ ref('{src_ref}') }}}}
                ),
                surrogate as (
                    select
                        {{{{ dbt_utils.generate_surrogate_key([{', '.join(repr(k) for k in natural_keys)}]) }}}} as {sk_col},
                        *,
                        cast('1900-01-01' as date) as valid_from,
                        cast('9999-12-31' as date) as valid_to,
                        true as is_current
                    from source
                )
                select * from surrogate
            """)
        else:
            col_list = ",\n        ".join(attribute_cols) if attribute_cols else "*"
            sql = textwrap.dedent(f"""\
                with source as (
                    select * from {{{{ ref('{src_ref}') }}}}
                ),
                surrogate as (
                    select
                        {{{{ dbt_utils.generate_surrogate_key([{', '.join(repr(k) for k in natural_keys)}]) }}}} as {sk_col},
                        {col_list}
                    from source
                )
                select * from surrogate
            """)

        config = f"{{% set materialization = 'table' %}}\n" if dim.scd_type == SCDType.TYPE2 else ""
        full_sql = config + sql

        schema_yaml = self._model_schema_yaml(
            dim.name,
            dim.description,
            [c.name for c in dim.columns],
        )
        return DBTModel(
            name=dim.name,
            layer="mart",
            schema_name="marts",
            description=dim.description,
            sql_content=full_sql,
            schema_yaml=schema_yaml,
            materialization="table",
            tags=["dimension", f"scd_{dim.scd_type.value}"],
            depends_on=stg_deps,
        )

    def _fact_model(
        self, fact: FactTable, dims: list[DimensionTable], project_name: str
    ) -> DBTModel:
        stg_deps = [f"stg_{t.split('.')[-1].lower()}" for t in fact.source_tables]
        dim_refs = [f"{{{{ ref('{d.name}') }}}}" for d in dims]
        dim_joins = "\n".join(
            f"    left join {{{{ ref('{d.name}') }}}} {d.name}\n"
            f"        on source.{d.name}_key = {d.name}.{d.name}_key"
            for d in dims
        )

        measures = [c for c in fact.columns if c.is_measure]
        measure_cols = ",\n    ".join(
            f"source.{m.name}" for m in measures
        ) or "    source.*"

        fk_cols = ",\n    ".join(
            f"{d.name}.{d.name}_key"
            for d in dims
        )

        src_ref = stg_deps[0] if stg_deps else "stg_source"
        sql = textwrap.dedent(f"""\
            with source as (
                select * from {{{{ ref('{src_ref}') }}}}
            )
            select
                {{{{ dbt_utils.generate_surrogate_key([{', '.join(repr(c.name) for c in fact.columns if c.is_foreign_key)[:3]}]) }}}} as {fact.name}_key,
                {fk_cols},
                {measure_cols}
            from source
            {dim_joins}
        """)

        schema_yaml = self._model_schema_yaml(
            fact.name,
            fact.description,
            [c.name for c in fact.columns],
        )
        return DBTModel(
            name=fact.name,
            layer="mart",
            schema_name="marts",
            description=fact.description,
            sql_content=sql,
            schema_yaml=schema_yaml,
            materialization="incremental" if fact.partitioning_column else "table",
            tags=["fact"],
            depends_on=stg_deps + [d.name for d in dims],
        )

    def _model_schema_yaml(
        self, model_name: str, description: str, columns: list[str]
    ) -> str:
        data: dict[str, Any] = {
            "version": 2,
            "models": [
                {
                    "name": model_name,
                    "description": description,
                    "columns": [{"name": c, "description": ""} for c in columns[:10]],
                }
            ],
        }
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    def _sources_yaml(self, star_schema: StarSchema, project_name: str) -> str:
        all_sources: set[str] = set()
        for src in star_schema.fact_table.source_tables:
            all_sources.add(src)
        for dim in star_schema.dimension_tables:
            for src in dim.source_tables:
                all_sources.add(src)

        table_entries = []
        for src in sorted(all_sources):
            parts = src.split(".")
            table_entries.append({"name": parts[-1], "description": f"Source: {src}"})

        data: dict[str, Any] = {
            "version": 2,
            "sources": [
                {
                    "name": project_name,
                    "schema": "dbo",
                    "description": f"Source data for {project_name}",
                    "tables": table_entries,
                }
            ],
        }
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    def _dbt_project_yaml(self, project_name: str) -> str:
        data: dict[str, Any] = {
            "name": project_name,
            "version": "1.0.0",
            "config-version": 2,
            "profile": "default",
            "model-paths": ["models"],
            "analysis-paths": ["analyses"],
            "test-paths": ["tests"],
            "seed-paths": ["seeds"],
            "macro-paths": ["macros"],
            "snapshot-paths": ["snapshots"],
            "target-path": "target",
            "clean-targets": ["target", "dbt_packages"],
            "models": {
                project_name: {
                    "staging": {
                        "+materialized": "view",
                        "+schema": "staging",
                    },
                    "marts": {
                        "+materialized": "table",
                        "+schema": "marts",
                    },
                }
            },
        }
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    def render_model_to_file_content(self, model: DBTModel) -> dict[str, str]:
        return {
            f"models/{model.layer}/{model.name}.sql": model.sql_content,
            f"models/{model.layer}/schema.yml": model.schema_yaml or "",
        }
