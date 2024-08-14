import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Sequence, Set, Tuple, Union

import dagster._check as check
import polars as pl
from dagster import AssetCheckSpec, AssetKey, AssetObservation, AssetSpec, TableColumn
from dagster._core.definitions.metadata import (
    CodeReferencesMetadataSet,
    CodeReferencesMetadataValue,
    LocalFileCodeReference,
    TableColumnConstraints,
    TableMetadataSet,
    TableSchema,
)
from dagster._record import IHaveNew, record_custom

from .asset_utils import get_info_schema_dir, get_output_dir
from .constants import (
    DEFAULT_SDF_WORKSPACE_ENVIRONMENT,
    SDF_INFORMATION_SCHEMA_TABLES_STAGE_COMPILE,
    SDF_INFORMATION_SCHEMA_TABLES_STAGE_PARSE,
)
from .dagster_sdf_translator import DagsterSdfTranslator
from .sdf_event_iterator import SdfDagsterEventType


@record_custom(checked=False)
class SdfInformationSchema(IHaveNew):
    """A class to represent the SDF information schema.

    The information schema is a set of tables that are generated by the sdf cli on compilation.
    It can be queried directly via the sdf cli, or by reading the parquet files that live in the
    `sdftarget` directory.

    This class specifically interfaces with the tables and columns tables, which contain metadata
    on their upstream and downstream dependencies, as well as their schemas, descriptions, classifiers,
    and other metadata.

    Read more about the information schema here: https://docs.sdf.com/reference/sdf-information-schema#sdf-information-schema

    Args:
        workspace_dir (Union[Path, str]): The path to the workspace directory.
        target_dir (Union[Path, str]): The path to the target directory.
        environment (str, optional): The environment to use. Defaults to "dbg".
    """

    workspace_dir: Path
    target_dir: Path
    environment: str
    information_schema_dir: Path
    information_schema: Dict[str, pl.DataFrame]

    def __new__(
        cls,
        workspace_dir: Union[Path, str],
        target_dir: Union[Path, str],
        environment: str = DEFAULT_SDF_WORKSPACE_ENVIRONMENT,
    ):
        check.inst_param(workspace_dir, "workspace_dir", (str, Path))
        check.inst_param(target_dir, "target_dir", (str, Path))
        check.str_param(environment, "environment")

        workspace_dir = Path(workspace_dir)
        target_dir = Path(target_dir)

        information_schema_dir = get_info_schema_dir(target_dir, environment)
        check.invariant(
            information_schema_dir.exists(),
            f"Information schema directory {information_schema_dir} does not exist.",
        )

        return super().__new__(
            cls,
            workspace_dir=workspace_dir,
            target_dir=target_dir,
            environment=environment,
            information_schema_dir=information_schema_dir,
            information_schema={},
        )

    def read_table(
        self,
        table_name: Literal["tables", "columns", "table_lineage", "column_lineage", "table_deps"],
    ) -> pl.DataFrame:
        check.invariant(
            table_name
            in SDF_INFORMATION_SCHEMA_TABLES_STAGE_COMPILE
            + SDF_INFORMATION_SCHEMA_TABLES_STAGE_PARSE,
            f"Table `{table_name}` is not valid information schema table."
            f" Select from one of {SDF_INFORMATION_SCHEMA_TABLES_STAGE_COMPILE + SDF_INFORMATION_SCHEMA_TABLES_STAGE_PARSE}.",
        )

        return self.information_schema.setdefault(
            table_name, pl.read_parquet(self.information_schema_dir.joinpath(table_name))
        )

    def build_sdf_multi_asset_args(
        self, dagster_sdf_translator: DagsterSdfTranslator
    ) -> Tuple[
        Sequence[AssetSpec],
        Sequence[AssetCheckSpec],
    ]:
        table_id_to_dep: Dict[str, AssetKey] = {}
        table_id_to_upstream: Dict[str, Set[AssetKey]] = {}
        asset_specs: Sequence[AssetSpec] = []
        asset_checks: Sequence[AssetCheckSpec] = []
        origin_remote_tables: Set[str] = set()

        # Step 0: Filter out system and external-system tables
        table_deps = self.read_table("table_deps").filter(
            ~pl.col("purpose").is_in(["system", "external-system"])
        )

        # Step 1: Build Map of Table Deps to Rows
        table_rows_deps = {row["table_id"]: row for row in table_deps.rows(named=True)}
        # Step 2: Build Dagster Asset Deps
        for table_row in table_deps.rows(named=True):
            # Iterate over the meta column to find the dagster-asset-key
            if len(table_row["meta"]) > 0:
                for meta_map in table_row["meta"]:
                    # If the meta_map has a key of dagster-asset-key, add it to the deps
                    if meta_map["keys"] == "dagster-asset-key":
                        dep_asset_key = meta_map["values"]
                        table_id_to_dep[table_row["table_id"]] = AssetKey(dep_asset_key)
                    elif meta_map["keys"] == "dagster-depends-on-asset-key":
                        dep_asset_key = meta_map["values"]
                        # Currently, we only support one upstream asset
                        table_id_to_upstream.setdefault(table_row["table_id"], set()).add(
                            AssetKey(dep_asset_key)
                        )
                    elif table_row["origin"] == "remote":
                        origin_remote_tables.add(table_row["table_id"])
            elif table_row["origin"] == "remote":
                origin_remote_tables.add(table_row["table_id"])

        # Step 3: Build Dagster Asset Outs and Internal Asset Deps
        for table_row in table_deps.rows(named=True):
            asset_key = dagster_sdf_translator.get_asset_key(
                table_row["catalog_name"], table_row["schema_name"], table_row["table_name"]
            )
            code_references = None
            if dagster_sdf_translator.settings.enable_code_references:
                code_references = self._extract_code_ref(table_row)
            metadata = {**(code_references if code_references else {})}
            # If the table is a annotated as a dependency, we don't need to create an output for it
            if (
                table_row["table_id"] not in table_id_to_dep
                and table_row["table_id"] not in origin_remote_tables
            ):
                dependencies = {
                    table_id_to_dep[dep]
                    if dep
                    in table_id_to_dep  # If the dep is a dagster dependency, use the meta asset key
                    else dagster_sdf_translator.get_asset_key(
                        table_rows_deps[dep]["catalog_name"],
                        table_rows_deps[dep]["schema_name"],
                        table_rows_deps[dep]["table_name"],
                    )  # Otherwise, use the translator to get the asset key
                    for dep in table_row["depends_on"]
                    if dep not in origin_remote_tables and dep in table_deps["table_id"]
                }.union(table_id_to_upstream.get(table_row["table_id"], set()))
                asset_specs.append(
                    AssetSpec(
                        key=asset_key,
                        deps=dependencies,
                        description=dagster_sdf_translator.get_description(
                            table_row,
                            self.workspace_dir,
                            get_output_dir(self.target_dir, self.environment),
                        ),
                        metadata=metadata,
                        skippable=True,
                    )
                )
                # This registers an asset check on all inner tables, since SDF will execute all tests as a single query (greedy approach)
                # If no table or column tests are registered, they will simply be skipped
                if dagster_sdf_translator.settings.enable_asset_checks:
                    test_name_prefix = "TEST_" if table_row["dialect"] == "snowflake" else "test_"
                    test_name = f"{table_row['catalog_name']}.{table_row['schema_name']}.{test_name_prefix}{table_row['table_name']}"
                    asset_checks.append(
                        AssetCheckSpec(
                            name=test_name,
                            asset=asset_key,
                        )
                    )
        return asset_specs, asset_checks

    def get_columns(self) -> Dict[str, List[TableColumn]]:
        columns = self.read_table("columns")[
            ["table_id", "column_id", "classifiers", "column_name", "datatype", "description"]
        ]
        table_columns: Dict[str, List[TableColumn]] = {}
        for row in columns.rows(named=True):
            if row["table_id"] not in table_columns:
                table_columns[row["table_id"]] = []
            table_columns[row["table_id"]].append(
                TableColumn(
                    name=row["column_name"],
                    type=row["datatype"],
                    description=row["description"],
                    constraints=TableColumnConstraints(other=row["classifiers"]),
                )
            )
        return table_columns

    def _extract_code_ref(
        self, table_row: Dict[str, Any]
    ) -> Union[CodeReferencesMetadataSet, None]:
        code_references = None
        # Check if any of the source locations are .sql files, return the first one
        loc = (
            next(
                (
                    source_location
                    for source_location in table_row["source_locations"]
                    if source_location.endswith(".sql")
                ),
                None,
            )
            or next(
                (
                    source_location
                    for source_location in table_row["source_locations"]
                    if source_location.endswith(".sdf.yml")
                ),
                None,
            )
            or "workspace.sdf.yml"
        )
        code_references = CodeReferencesMetadataSet(
            code_references=CodeReferencesMetadataValue(
                code_references=[
                    LocalFileCodeReference(file_path=os.fspath(self.workspace_dir.joinpath(loc)))
                ]
            )
        )
        return code_references

    def stream_asset_observations(
        self, dagster_sdf_translator: DagsterSdfTranslator
    ) -> Iterator[SdfDagsterEventType]:
        table_columns = self.get_columns()
        tables = self.read_table("tables").filter(
            ~pl.col("purpose").is_in(["system", "external-system"])
        )

        for table_row in tables.rows(named=True):
            asset_key = dagster_sdf_translator.get_asset_key(
                table_row["catalog_name"], table_row["schema_name"], table_row["table_name"]
            )
            code_references = None
            if dagster_sdf_translator.settings.enable_code_references:
                code_references = self._extract_code_ref(table_row)
            metadata = {
                **TableMetadataSet(
                    column_schema=TableSchema(
                        columns=table_columns.get(table_row["table_id"], []),
                    ),
                    relation_identifier=table_row["table_id"],
                ),
                **(code_references if code_references else {}),
            }
            yield AssetObservation(
                asset_key=asset_key,
                description=dagster_sdf_translator.get_description(
                    table_row, self.workspace_dir, get_output_dir(self.target_dir, self.environment)
                ),
                metadata=metadata,
            )

    def is_compiled(self) -> bool:
        for table in SDF_INFORMATION_SCHEMA_TABLES_STAGE_COMPILE:
            if not any(self.information_schema_dir.joinpath(table).iterdir()):
                return False
        return True

    def is_parsed(self) -> bool:
        for table in SDF_INFORMATION_SCHEMA_TABLES_STAGE_PARSE:
            if not any(self.information_schema_dir.joinpath(table).iterdir()):
                return False
        return True
