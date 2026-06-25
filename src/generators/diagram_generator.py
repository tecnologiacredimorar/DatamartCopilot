from __future__ import annotations

from src.models.schemas import DimensionTable, FactTable, SCDType, StarSchema, TableSchema


class DiagramGenerator:
    def design_dict_dot(self, design: dict) -> str:
        """Generate a Graphviz DOT diagram from the accelerator's raw design dict."""
        fact = design.get("fact_table", {})
        dims = design.get("dimensions", [])

        fact_name = fact.get("name", "FactTable")
        fact_type = fact.get("type", "transaction")
        measures = fact.get("measures", [])

        measure_names = [
            (m.get("name", "?") if isinstance(m, dict) else str(m))
            for m in measures[:8]
        ]

        lines = [
            "digraph star_schema {",
            '    graph [rankdir=LR fontname="Arial" bgcolor="#f8f9fa"];',
            '    node  [fontname="Arial" fontsize=11];',
            '    edge  [fontsize=9 color="#555555"];',
            "",
        ]

        fact_rows = "<BR/>".join(f"&#x25CF; {m}" for m in measure_names) or "(sem medidas)"
        fact_label = (
            f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" BGCOLOR="#4A90D9">'
            f'<TR><TD ALIGN="CENTER"><FONT COLOR="white"><B>{fact_name}</B></FONT><BR/>'
            f'<FONT COLOR="#d0e8ff" POINT-SIZE="9">FACT — {fact_type}</FONT></TD></TR>'
            f'<TR><TD ALIGN="LEFT"><FONT COLOR="white" POINT-SIZE="9">{fact_rows}</FONT></TD></TR>'
            f'</TABLE>>'
        )
        lines.append(f'    "{fact_name}" [shape=plain label={fact_label}];')
        lines.append("")

        scd_colors = {"1": "#F5A623", "2": "#7ED321", "3": "#9B59B6",
                      1: "#F5A623", 2: "#7ED321", 3: "#9B59B6"}
        for dim in dims:
            dim_name = dim.get("name", "Dim")
            scd = dim.get("scd_type", 1)
            color = scd_colors.get(scd, "#F5A623")
            cols = dim.get("columns", [])
            col_names = [
                (c.get("name", "?") if isinstance(c, dict) else str(c))
                for c in cols[:6]
            ]
            col_rows = "<BR/>".join(f"&#x25B8; {c}" for c in col_names) or "&nbsp;"
            dim_label = (
                f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" BGCOLOR="{color}">'
                f'<TR><TD ALIGN="CENTER"><FONT COLOR="white"><B>{dim_name}</B></FONT><BR/>'
                f'<FONT COLOR="white" POINT-SIZE="9">DIM — SCD Type {scd}</FONT></TD></TR>'
                f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9">{col_rows}</FONT></TD></TR>'
                f'</TABLE>>'
            )
            lines.append(f'    "{dim_name}" [shape=plain label={dim_label}];')
            lines.append(
                f'    "{dim_name}" -> "{fact_name}" [arrowhead=crow arrowtail=none dir=back];'
            )
            lines.append("")

        lines.append("}")
        return "\n".join(lines)

    def star_schema_dot(self, star_schema: StarSchema) -> str:
        fact = star_schema.fact_table
        lines = [
            "digraph star_schema {",
            '    graph [rankdir=LR fontname="Arial" bgcolor="#f8f9fa"];',
            '    node  [fontname="Arial" fontsize=11];',
            '    edge  [fontsize=9 color="#555555"];',
            "",
        ]

        # Fact table node
        fact_measures = "\\n".join(f"  {m}" for m in fact.measures[:8])
        fact_label = (
            f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" BGCOLOR="#4A90D9">'
            f'<TR><TD ALIGN="CENTER"><FONT COLOR="white"><B>{fact.name}</B></FONT><BR/>'
            f'<FONT COLOR="#d0e8ff" POINT-SIZE="9">FACT — {fact.fact_type.value}</FONT></TD></TR>'
            f'<TR><TD ALIGN="LEFT"><FONT COLOR="white" POINT-SIZE="9">'
            + "<BR/>".join(f"&#x25CF; {m}" for m in fact.measures[:8])
            + "</FONT></TD></TR></TABLE>>"
        )
        lines.append(f'    "{fact.name}" [shape=plain label={fact_label}];')
        lines.append("")

        # Dimension nodes
        for dim in star_schema.dimension_tables:
            scd_color = {"type1": "#F5A623", "type2": "#7ED321", "type3": "#9B59B6"}.get(
                dim.scd_type.value, "#F5A623"
            )
            attr_cols = [
                c.name for c in dim.columns
                if not c.is_surrogate_key and not c.is_scd_tracking
            ][:6]
            dim_label = (
                f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="2" BGCOLOR="{scd_color}">'
                f'<TR><TD ALIGN="CENTER"><FONT COLOR="white"><B>{dim.name}</B></FONT><BR/>'
                f'<FONT COLOR="white" POINT-SIZE="9">DIM — SCD Type {dim.scd_type.value[-1]}</FONT></TD></TR>'
                f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9">'
                + "<BR/>".join(f"&#x25B8; {c}" for c in attr_cols)
                + "</FONT></TD></TR></TABLE>>"
            )
            lines.append(f'    "{dim.name}" [shape=plain label={dim_label}];')
            sk = f"{dim.name}_key"
            lines.append(
                f'    "{dim.name}" -> "{fact.name}" '
                f'[label="{sk}" arrowhead=crow arrowtail=none dir=back];'
            )
            lines.append("")

        lines.append("}")
        return "\n".join(lines)

    def schema_relationships_dot(self, tables: list[TableSchema]) -> str:
        lines = [
            "digraph dw_schema {",
            '    graph [rankdir=TB fontname="Arial" bgcolor="#f8f9fa" splines=ortho];',
            '    node  [shape=box fontname="Arial" fontsize=10 style=filled fillcolor=white];',
            '    edge  [fontsize=8 color="#888888"];',
            "",
        ]
        for table in tables:
            color = "#cce5ff" if table.is_fact_table else ("#fff3cd" if table.is_dimension_table else "#f8f9fa")
            border = "2" if table.is_fact_table else "1"
            col_rows = "".join(
                f'<TR><TD ALIGN="LEFT" BGCOLOR="{"#e8f4f8" if c.is_primary_key else "white"}">'
                f'{"🔑 " if c.is_primary_key else ("🔗 " if c.is_foreign_key else "   ")}'
                f"{c.name} <FONT POINT-SIZE='8'>({c.data_type})</FONT></TD></TR>"
                for c in table.columns[:10]
            )
            label = (
                f'<<TABLE BORDER="{border}" CELLBORDER="0" CELLSPACING="1" BGCOLOR="{color}">'
                f'<TR><TD ALIGN="CENTER"><B>{table.full_name}</B></TD></TR>'
                f"{col_rows}</TABLE>>"
            )
            lines.append(f'    "{table.full_name}" [shape=plain label={label}];')

        for table in tables:
            for fk in table.foreign_keys:
                target = next(
                    (t.full_name for t in tables if t.table_name == fk.referenced_table),
                    fk.referenced_table,
                )
                lines.append(
                    f'    "{table.full_name}" -> "{target}" '
                    f'[label="{fk.column}" arrowhead=crow arrowtail=none dir=back];'
                )

        lines.append("}")
        return "\n".join(lines)

    def mermaid_er(self, star_schema: StarSchema) -> str:
        lines = ["erDiagram"]
        fact = star_schema.fact_table

        for dim in star_schema.dimension_tables:
            lines.append(f'    {dim.name} ||--o{{ {fact.name} : "{dim.name}_key"')

        lines.append("")
        fact_cols = "\n".join(
            f"        {c.data_type} {c.name}" for c in fact.columns[:10]
        )
        lines.append(f"    {fact.name} {{")
        lines.append(fact_cols)
        lines.append("    }")

        for dim in star_schema.dimension_tables:
            dim_cols = "\n".join(
                f"        {c.data_type} {c.name}" for c in dim.columns[:8]
            )
            lines.append(f"    {dim.name} {{")
            lines.append(dim_cols)
            lines.append("    }")

        return "\n".join(lines)
