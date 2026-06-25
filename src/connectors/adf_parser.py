from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from src.models.schemas import (
    ADFActivity,
    ADFDataflow,
    ADFDataset,
    ADFFactory,
    ADFLinkedService,
    ADFPipeline,
    ADFTrigger,
)

logger = logging.getLogger(__name__)

_ARM_SCHEMA_MARKER = "deploymentTemplate"
_FACTORY_PREFIX_RE = re.compile(r"\[concat\(parameters\('[^']+'\),\s*'/([^']+)'\)\]")


def _extract_resource_name(raw_name: str) -> str:
    """Strip ARM concat expression → plain resource name."""
    m = _FACTORY_PREFIX_RE.search(raw_name)
    if m:
        return m.group(1)
    if "/" in raw_name:
        return raw_name.rsplit("/", 1)[-1]
    return raw_name


def _parse_connection_string(conn_str: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (host, database) from an ADF-style JDBC/ADO.NET connection string."""
    host = None
    database = None
    # Server=tcp:hostname,port or Server=hostname
    m = re.search(r"[Ss]erver\s*=\s*(?:tcp:)?([^;,]+)", conn_str)
    if m:
        host = m.group(1).split(",")[0].strip()
    # Initial Catalog=X  or  Database=X
    m = re.search(r"(?:Initial Catalog|Database)\s*=\s*([^;]+)", conn_str)
    if m:
        database = m.group(1).strip()
    return host, database


class ADFParser:
    # ──────────────────────────────────────────────────────────────────────────
    # Format detection
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_arm_template(raw: dict[str, Any]) -> bool:
        schema = raw.get("$schema", "")
        if _ARM_SCHEMA_MARKER in schema:
            return True
        resources = raw.get("resources", [])
        if resources and isinstance(resources, list):
            first_type = resources[0].get("type", "") if resources else ""
            return first_type.startswith("Microsoft.DataFactory")
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # ARM template parser (full factory)
    # ──────────────────────────────────────────────────────────────────────────

    def parse_arm_template(self, source: str | dict | Path) -> ADFFactory:
        if isinstance(source, Path):
            raw = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, str):
            raw = json.loads(source)
        else:
            raw = source

        # Factory name from parameters default
        factory_name = (
            raw.get("parameters", {})
            .get("factoryName", {})
            .get("defaultValue", "unknown_factory")
        )

        linked_services: list[ADFLinkedService] = []
        datasets: list[ADFDataset] = []
        pipelines: list[ADFPipeline] = []
        dataflows: list[ADFDataflow] = []
        triggers: list[ADFTrigger] = []

        for resource in raw.get("resources", []):
            rtype = resource.get("type", "")
            rname = _extract_resource_name(resource.get("name", ""))
            props = resource.get("properties", {})

            if rtype.endswith("/linkedServices"):
                linked_services.append(self._parse_linked_service(rname, props))

            elif rtype.endswith("/datasets"):
                datasets.append(self._parse_dataset(rname, props))

            elif rtype.endswith("/pipelines"):
                pipelines.append(self._parse_pipeline_props(rname, props))

            elif rtype.endswith("/dataflows"):
                dataflows.append(self._parse_dataflow(rname, props))

            elif rtype.endswith("/triggers"):
                triggers.append(self._parse_trigger(rname, props))

        return ADFFactory(
            factory_name=factory_name,
            linked_services=linked_services,
            datasets=datasets,
            pipelines=pipelines,
            dataflows=dataflows,
            triggers=triggers,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Linked service
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_linked_service(self, name: str, props: dict[str, Any]) -> ADFLinkedService:
        stype = props.get("type", "Unknown")
        type_props = props.get("typeProperties", {})

        host = None
        database = None

        conn_str = type_props.get("connectionString", "")
        if isinstance(conn_str, dict):
            # Encrypted value — skip
            conn_str = ""
        if conn_str:
            host, database = _parse_connection_string(conn_str)

        # PostgreSQL / other explicit fields
        if not host:
            host = type_props.get("server") or type_props.get("host")
        if not database:
            database = type_props.get("database") or type_props.get("databaseName")

        return ADFLinkedService(
            name=name,
            service_type=stype,
            host=str(host) if host else None,
            database=str(database) if database else None,
            description=props.get("description"),
            properties=type_props,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Dataset
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_dataset(self, name: str, props: dict[str, Any]) -> ADFDataset:
        dtype = props.get("type", "Unknown")
        type_props = props.get("typeProperties", {})
        ls_name = props.get("linkedServiceName", {}).get("referenceName", "")

        schema_name = None
        table_name = None

        # Azure SQL table ref
        table_ref = type_props.get("tableName") or type_props.get("table")
        if isinstance(table_ref, dict):
            schema_name = table_ref.get("schema")
            table_name = table_ref.get("table")
        elif isinstance(table_ref, str):
            if "." in table_ref:
                parts = table_ref.split(".", 1)
                schema_name, table_name = parts[0], parts[1]
            else:
                table_name = table_ref

        # Schema object list
        schema_list = type_props.get("schema", [])
        return ADFDataset(
            name=name,
            dataset_type=dtype,
            linked_service=ls_name,
            schema_name=schema_name,
            table_name=table_name,
            properties=type_props,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Pipeline (from ARM resource props)
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_pipeline_props(self, name: str, props: dict[str, Any]) -> ADFPipeline:
        activities_raw = props.get("activities", [])
        activities = [self._parse_activity(a) for a in activities_raw]
        sources, sinks = self._extract_sources_sinks(activities_raw)
        embedded = self._extract_embedded_queries(activities_raw)

        return ADFPipeline(
            name=name,
            description=props.get("description"),
            activities=activities,
            parameters=props.get("parameters", {}),
            variables=props.get("variables", {}),
            sources=sources,
            sinks=sinks,
            embedded_queries=embedded,
            raw_json={"name": name, "properties": props},
        )

    def _extract_embedded_queries(self, activities: list[dict[str, Any]]) -> dict[str, str]:
        """Pull SQL queries embedded in Copy activity sources and pre-copy scripts."""
        queries: dict[str, str] = {}
        for act in activities:
            name = act.get("name", "")
            props = act.get("typeProperties", {})
            act_type = act.get("type", "")

            if act_type == "Copy":
                src_query = props.get("source", {}).get("sqlReaderQuery", "")
                if isinstance(src_query, str) and src_query.strip():
                    queries[f"{name}_source_query"] = src_query.strip()
                pre_copy = props.get("sink", {}).get("preCopyScript", "")
                if isinstance(pre_copy, str) and pre_copy.strip():
                    queries[f"{name}_precopy"] = pre_copy.strip()

            # SqlServerStoredProcedure
            if act_type == "SqlServerStoredProcedure":
                sp = props.get("storedProcedureName", "")
                if sp:
                    queries[f"{name}_stored_proc"] = sp

        return queries

    # ──────────────────────────────────────────────────────────────────────────
    # Dataflow
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_dataflow(self, name: str, props: dict[str, Any]) -> ADFDataflow:
        type_props = props.get("typeProperties", {})
        script_lines: list[str] = type_props.get("scriptLines", [])

        sources: list[str] = []
        sinks: list[str] = []
        transformations: list[str] = []
        embedded_queries: dict[str, str] = {}

        # Extract from typeProperties.sources / sinks
        for s in type_props.get("sources", []):
            sname = s.get("name", "")
            if sname:
                sources.append(sname)
            # Inline query in source dataset schema query
            ds_props = s.get("dataset", {})
            query = s.get("typeProperties", {}).get("query", "")
            if isinstance(query, str) and query.strip():
                embedded_queries[sname] = query.strip()

        for s in type_props.get("sinks", []):
            sname = s.get("name", "")
            if sname:
                sinks.append(sname)

        for t in type_props.get("transformations", []):
            tname = t.get("name", "")
            if tname:
                transformations.append(tname)

        # Parse scriptLines to extract source queries (source(<query>))
        embedded_queries.update(self._parse_script_line_queries(script_lines, sources))

        return ADFDataflow(
            name=name,
            description=props.get("description"),
            sources=sources,
            sinks=sinks,
            transformations=transformations,
            script_lines=script_lines,
            embedded_queries=embedded_queries,
        )

    @staticmethod
    def _parse_script_line_queries(
        script_lines: list[str], sources: list[str]
    ) -> dict[str, str]:
        """Extract SQL from ADF dataflow script source() calls."""
        queries: dict[str, str] = {}
        full_script = "\n".join(script_lines)

        # Pattern: source(... query: 'SELECT ...' ...) ~> SourceName
        for match in re.finditer(
            r"query:\s*'([^']+)'.*?~>\s*(\w+)", full_script, re.DOTALL
        ):
            sql = match.group(1).replace("\\n", "\n").strip()
            src_name = match.group(2).strip()
            if sql:
                queries[src_name] = sql

        return queries

    # ──────────────────────────────────────────────────────────────────────────
    # Trigger
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_trigger(self, name: str, props: dict[str, Any]) -> ADFTrigger:
        ttype = props.get("type", "Unknown")
        type_props = props.get("typeProperties", {})
        recurrence = type_props.get("recurrence")
        pipeline_refs = [
            p.get("pipelineReference", {}).get("referenceName", "")
            for p in type_props.get("pipelines", [])
            if p.get("pipelineReference", {}).get("referenceName")
        ]
        return ADFTrigger(
            name=name,
            trigger_type=ttype,
            pipelines=pipeline_refs,
            recurrence=recurrence,
            description=props.get("description"),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Individual pipeline JSON (legacy format)
    # ──────────────────────────────────────────────────────────────────────────

    def parse_pipeline(self, source: str | dict | Path) -> ADFPipeline:
        if isinstance(source, Path):
            raw = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, str):
            raw = json.loads(source)
        else:
            raw = source

        props = raw.get("properties", {})
        activities_raw = props.get("activities", [])
        activities = [self._parse_activity(a) for a in activities_raw]
        sources, sinks = self._extract_sources_sinks(activities_raw)
        embedded = self._extract_embedded_queries(activities_raw)

        return ADFPipeline(
            name=raw.get("name", "unnamed"),
            description=props.get("description"),
            activities=activities,
            parameters=props.get("parameters", {}),
            variables=props.get("variables", {}),
            sources=sources,
            sinks=sinks,
            embedded_queries=embedded,
            raw_json=raw,
        )

    def parse_any(self, source: str | dict | Path) -> ADFFactory | ADFPipeline:
        """Auto-detect format and return ADFFactory or ADFPipeline."""
        if isinstance(source, Path):
            raw = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, str):
            raw = json.loads(source)
        else:
            raw = source

        if self.is_arm_template(raw):
            return self.parse_arm_template(raw)
        return self.parse_pipeline(raw)

    # ──────────────────────────────────────────────────────────────────────────
    # Shared helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_activity(self, raw: dict[str, Any]) -> ADFActivity:
        activity_type = raw.get("type", "Unknown")
        props = raw.get("typeProperties", {})
        depends_on = [d["activity"] for d in raw.get("dependsOn", [])]

        inputs = [i.get("referenceName", "") for i in raw.get("inputs", [])]
        outputs = [o.get("referenceName", "") for o in raw.get("outputs", [])]

        if activity_type == "ExecuteDataFlow":
            df_ref = props.get("dataFlow", {}).get("referenceName", "")
            if df_ref:
                inputs.append(f"dataflow:{df_ref}")

        return ADFActivity(
            name=raw.get("name", ""),
            activity_type=activity_type,
            description=raw.get("description"),
            inputs=inputs,
            outputs=outputs,
            properties=props,
            depends_on=depends_on,
        )

    def _extract_sources_sinks(
        self, activities: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        sinks: list[str] = []

        for act in activities:
            props = act.get("typeProperties", {})
            act_type = act.get("type", "")

            if act_type == "Copy":
                src_ds = (
                    act.get("inputs", [{}])[0].get("referenceName", "")
                    if act.get("inputs")
                    else ""
                )
                snk_ds = (
                    act.get("outputs", [{}])[0].get("referenceName", "")
                    if act.get("outputs")
                    else ""
                )
                if src_ds:
                    sources.append(src_ds)
                if snk_ds:
                    sinks.append(snk_ds)

            elif act_type == "ExecuteDataFlow":
                df_ref = props.get("dataFlow", {}).get("referenceName", "")
                if df_ref:
                    sources.append(f"dataflow:{df_ref}")

        return list(dict.fromkeys(sources)), list(dict.fromkeys(sinks))

    def parse_directory(self, directory: str | Path) -> list[ADFPipeline]:
        directory = Path(directory)
        pipelines = []
        for json_file in directory.rglob("*.json"):
            try:
                pipeline = self.parse_pipeline(json_file)
                pipelines.append(pipeline)
                logger.info("Parsed pipeline: %s", pipeline.name)
            except Exception as e:
                logger.warning("Failed to parse %s: %s", json_file, e)
        return pipelines

    def get_lineage_graph(self, pipelines: list[ADFPipeline]) -> dict[str, list[str]]:
        graph: dict[str, list[str]] = {}
        for pipeline in pipelines:
            for activity in pipeline.activities:
                node = f"{pipeline.name}/{activity.name}"
                graph[node] = [
                    f"{pipeline.name}/{dep}" for dep in activity.depends_on
                ]
        return graph

    def summarize_pipeline(self, pipeline: ADFPipeline) -> str:
        lines = [
            f"Pipeline: {pipeline.name}",
            f"Description: {pipeline.description or 'N/A'}",
            f"Activities ({len(pipeline.activities)}):",
        ]
        for act in pipeline.activities:
            dep_str = f" (depends on: {', '.join(act.depends_on)})" if act.depends_on else ""
            lines.append(f"  - {act.name} [{act.activity_type}]{dep_str}")
        if pipeline.sources:
            lines.append(f"Sources: {', '.join(pipeline.sources)}")
        if pipeline.sinks:
            lines.append(f"Sinks: {', '.join(pipeline.sinks)}")
        if pipeline.embedded_queries:
            lines.append(f"SQL queries: {len(pipeline.embedded_queries)} embedded")
        return "\n".join(lines)

    def summarize_factory(self, factory: ADFFactory) -> str:
        lines = [
            f"Factory: {factory.factory_name}",
            f"Linked Services ({len(factory.linked_services)}): "
            + ", ".join(ls.name for ls in factory.linked_services),
            f"Datasets ({len(factory.datasets)}): "
            + ", ".join(ds.name for ds in factory.datasets[:10]),
            f"Pipelines ({len(factory.pipelines)}): "
            + ", ".join(p.name for p in factory.pipelines),
            f"Dataflows ({len(factory.dataflows)}): "
            + ", ".join(df.name for df in factory.dataflows),
            f"Triggers ({len(factory.triggers)}): "
            + ", ".join(t.name for t in factory.triggers),
        ]
        return "\n".join(lines)
