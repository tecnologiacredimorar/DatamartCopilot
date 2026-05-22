from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.models.schemas import ADFActivity, ADFPipeline

logger = logging.getLogger(__name__)


class ADFParser:
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

        return ADFPipeline(
            name=raw.get("name", "unnamed"),
            description=props.get("description"),
            activities=activities,
            parameters=props.get("parameters", {}),
            variables=props.get("variables", {}),
            sources=sources,
            sinks=sinks,
            raw_json=raw,
        )

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
                src = props.get("source", {})
                snk = props.get("sink", {})
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
                df_props = props.get("dataflow", {})
                for s in df_props.get("sources", []):
                    name = s.get("dataset", {}).get("referenceName", "")
                    if name:
                        sources.append(name)
                for s in df_props.get("sinks", []):
                    name = s.get("dataset", {}).get("referenceName", "")
                    if name:
                        sinks.append(name)

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
        return "\n".join(lines)
