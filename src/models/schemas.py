from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class DatabaseType(str, Enum):
    AZURE_SQL = "azure_sql"
    POSTGRESQL = "postgresql"


class SCDType(str, Enum):
    TYPE1 = "type1"
    TYPE2 = "type2"
    TYPE3 = "type3"


class FactTableType(str, Enum):
    TRANSACTION = "transaction"
    PERIODIC_SNAPSHOT = "periodic_snapshot"
    ACCUMULATING_SNAPSHOT = "accumulating_snapshot"


class DatabaseConnection(BaseModel):
    name: str
    db_type: DatabaseType
    host: str
    port: int
    database: str
    username: str
    password: str
    driver: Optional[str] = None
    is_connected: bool = False


class ColumnSchema(BaseModel):
    name: str
    data_type: str
    is_nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    default_value: Optional[str] = None
    description: Optional[str] = None


class ForeignKeyRelation(BaseModel):
    column: str
    referenced_table: str
    referenced_column: str


class TableSchema(BaseModel):
    schema_name: str
    table_name: str
    full_name: str
    columns: list[ColumnSchema] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyRelation] = Field(default_factory=list)
    row_count: Optional[int] = None
    description: Optional[str] = None
    is_fact_table: bool = False
    is_dimension_table: bool = False
    sample_data: Optional[list[dict[str, Any]]] = None


class ADFActivity(BaseModel):
    name: str
    activity_type: str
    description: Optional[str] = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class ADFDataset(BaseModel):
    name: str
    dataset_type: str
    linked_service: str
    schema_name: Optional[str] = None
    table_name: Optional[str] = None
    properties: dict[str, Any] = Field(default_factory=dict)


class ADFPipeline(BaseModel):
    name: str
    description: Optional[str] = None
    activities: list[ADFActivity] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    sinks: list[str] = Field(default_factory=list)
    raw_json: Optional[dict[str, Any]] = None


class DimensionColumn(BaseModel):
    name: str
    data_type: str
    is_surrogate_key: bool = False
    is_natural_key: bool = False
    is_scd_tracking: bool = False
    scd_type: Optional[SCDType] = None
    description: Optional[str] = None


class DimensionTable(BaseModel):
    name: str
    description: str
    grain: str
    scd_type: SCDType = SCDType.TYPE1
    columns: list[DimensionColumn] = Field(default_factory=list)
    source_tables: list[str] = Field(default_factory=list)
    conformed: bool = False
    role_playing: bool = False
    role_playing_base: Optional[str] = None
    junk: bool = False


class FactColumn(BaseModel):
    name: str
    data_type: str
    is_measure: bool = False
    is_foreign_key: bool = False
    referenced_dimension: Optional[str] = None
    aggregation: Optional[str] = None
    description: Optional[str] = None


class FactTable(BaseModel):
    name: str
    description: str
    grain: str
    fact_type: FactTableType = FactTableType.TRANSACTION
    columns: list[FactColumn] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    source_tables: list[str] = Field(default_factory=list)
    partitioning_column: Optional[str] = None
    clustering_columns: list[str] = Field(default_factory=list)


class StarSchema(BaseModel):
    name: str
    description: str
    subject_area: str
    fact_table: FactTable
    dimension_tables: list[DimensionTable] = Field(default_factory=list)
    bus_matrix_row: dict[str, bool] = Field(default_factory=dict)
    kpi_opportunities: list[str] = Field(default_factory=list)
    merge_candidates: list[str] = Field(default_factory=list)


class DBTModel(BaseModel):
    name: str
    layer: str  # staging | intermediate | mart
    schema_name: str
    description: str
    sql_content: str
    schema_yaml: Optional[str] = None
    materialization: str = "table"
    tags: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class DBTProject(BaseModel):
    name: str
    version: str = "1.0.0"
    profile: str = "default"
    models: list[DBTModel] = Field(default_factory=list)
    sources_yaml: Optional[str] = None
    dbt_project_yaml: Optional[str] = None


class DatamartRequest(BaseModel):
    title: str
    description: str
    subject_area: str
    required_metrics: list[str] = Field(default_factory=list)
    required_dimensions: list[str] = Field(default_factory=list)
    source_tables: list[str] = Field(default_factory=list)
    business_questions: list[str] = Field(default_factory=list)
    target_grain: Optional[str] = None


class KPISuggestion(BaseModel):
    name: str
    description: str
    formula: Optional[str] = None
    dimensions: list[str] = Field(default_factory=list)
    business_value: str
    sql_example: Optional[str] = None


class DatamartAnalysis(BaseModel):
    request: DatamartRequest
    can_merge_with_existing: bool = False
    merge_candidates: list[str] = Field(default_factory=list)
    merge_rationale: Optional[str] = None
    recommended_star_schema: Optional[StarSchema] = None
    dbt_project: Optional[DBTProject] = None
    kpi_suggestions: list[KPISuggestion] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
    estimated_complexity: str = "medium"
    kimball_notes: Optional[str] = None
