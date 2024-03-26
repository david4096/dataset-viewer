# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.

import logging
from http import HTTPStatus
from typing import Optional

import anyio
import pyarrow as pa
from datasets import Features
from duckdb import DuckDBPyConnection
from libapi.authentication import auth_check
from libapi.duckdb import (
    get_cache_entry_from_duckdb_index_job,
    get_index_file_location_and_download_if_missing,
)
from libapi.exceptions import (
    ApiError,
    SearchFeatureNotAvailableError,
    UnexpectedApiError,
)
from libapi.request import (
    get_request_parameter,
    get_request_parameter_length,
    get_request_parameter_offset,
)
from libapi.response import ROW_IDX_COLUMN
from libapi.utils import (
    Endpoint,
    get_json_api_error_response,
    get_json_error_response,
    get_json_ok_response,
    to_rows_list,
)
from libcommon.constants import MAX_NUM_ROWS_PER_PAGE
from libcommon.dtos import PaginatedResponse
from libcommon.duckdb_utils import duckdb_index_is_partial
from libcommon.prometheus import StepProfiler
from libcommon.storage import StrPath
from libcommon.storage_client import StorageClient
from libcommon.viewer_utils.features import (
    get_supported_unsupported_columns,
    to_features_list,
)
from starlette.requests import Request
from starlette.responses import Response

from search.duckdb_connection import duckdb_connect

FTS_COUNT_COMMAND = "SELECT COUNT(*) FROM (SELECT __hf_index_id, fts_main_data.match_bm25(__hf_index_id, ?) AS __hf_fts_score FROM data) A WHERE __hf_fts_score IS NOT NULL;"


FTS_BY_TABLE_COMMAND = (
    "SELECT * EXCLUDE (__hf_fts_score) FROM (SELECT *, fts_main_data.match_bm25(__hf_index_id, ?) AS __hf_fts_score"
    " FROM data) A WHERE __hf_fts_score IS NOT NULL ORDER BY __hf_fts_score DESC OFFSET {offset} LIMIT {length};"
)

FTS_BY_BATCHES_COMMAND = "SELECT * FROM (SELECT *, fts_main_data.match_bm25(__hf_index_id, ?) AS __hf_fts_score FROM data WHERE __hf_index_id BETWEEN {offset} and {length}) A WHERE __hf_fts_score IS NOT NULL;"

FTS_BATCH_SIZE = 1000

logger = logging.getLogger(__name__)

DATASETS_WITH_FTS_BY_BATCHES = ["wikimedia/wikipedia", "asoria/search_test"]


def _search_by_table(con: DuckDBPyConnection, query: str, offset: int, length: int) -> pa.Table:
    query_result = con.execute(
        query=FTS_BY_TABLE_COMMAND.format(offset=offset, length=length),
        parameters=[query],
    )
    return query_result.arrow()


def _search_by_batches(
    num_original_rows: int, con: DuckDBPyConnection, query: str, offset: int, length: int
) -> pa.Table:
    num_matched_rows = 0
    index = 0
    pa_tables = []

    for index in range(index, num_original_rows, FTS_BATCH_SIZE):
        fts_query = FTS_BY_BATCHES_COMMAND.format(offset=index, length=index + FTS_BATCH_SIZE)
        logging.info(fts_query)
        pa_table = con.execute(
            query=fts_query,
            parameters=[query],
        ).arrow()
        pa_tables.append(pa_table)
        num_matched_rows += pa_table.num_rows
        logging.info(f"{pa_table.num_rows} rows for batch {index} - {num_matched_rows} for table")
        if num_matched_rows > (offset + length):
            break
    pa_result = pa.concat_tables(pa_tables)
    pa_result = pa_result.sort_by([("__hf_fts_score", "descending")]).drop("__hf_fts_score")
    return pa_result.slice(offset, length)


def full_text_search(
    num_original_rows: int,
    fts_batches: bool,
    index_file_location: str,
    query: str,
    offset: int,
    length: int,
    extensions_directory: Optional[str] = None,
) -> tuple[int, pa.Table]:
    with duckdb_connect(extensions_directory=extensions_directory, database=index_file_location) as con:
        count_result = con.execute(query=FTS_COUNT_COMMAND, parameters=[query]).fetchall()
        num_rows_total = count_result[0][0]  # it will always return a non-empty list with one element in a tuple
        logging.debug(f"got {num_rows_total=} results for {query=}")
        pa_table = (
            _search_by_batches(num_original_rows, con, query, offset, length)
            if fts_batches
            else _search_by_table(con, query, offset, length)
        )
        return num_rows_total, pa_table


async def create_response(
    pa_table: pa.Table,
    dataset: str,
    revision: str,
    config: str,
    split: str,
    storage_client: StorageClient,
    offset: int,
    features: Features,
    num_rows_total: int,
    partial: bool,
) -> PaginatedResponse:
    features_without_key = features.copy()
    features_without_key.pop(ROW_IDX_COLUMN, None)

    _, unsupported_columns = get_supported_unsupported_columns(
        features,
    )
    pa_table = pa_table.drop(unsupported_columns)
    logging.debug(f"create response for {dataset=} {config=} {split=}")

    return PaginatedResponse(
        features=to_features_list(features_without_key),
        rows=await to_rows_list(
            pa_table=pa_table,
            dataset=dataset,
            revision=revision,
            config=config,
            split=split,
            storage_client=storage_client,
            offset=offset,
            features=features,
            unsupported_columns=unsupported_columns,
            row_idx_column=ROW_IDX_COLUMN,
        ),
        num_rows_total=num_rows_total,
        num_rows_per_page=MAX_NUM_ROWS_PER_PAGE,
        partial=partial,
    )


def create_search_endpoint(
    duckdb_index_file_directory: StrPath,
    cached_assets_storage_client: StorageClient,
    target_revision: str,
    hf_endpoint: str,
    blocked_datasets: list[str],
    external_auth_url: Optional[str] = None,
    hf_token: Optional[str] = None,
    hf_jwt_public_keys: Optional[list[str]] = None,
    hf_jwt_algorithm: Optional[str] = None,
    hf_timeout_seconds: Optional[float] = None,
    max_age_long: int = 0,
    max_age_short: int = 0,
    storage_clients: Optional[list[StorageClient]] = None,
    extensions_directory: Optional[str] = None,
) -> Endpoint:
    async def search_endpoint(request: Request) -> Response:
        revision: Optional[str] = None
        with StepProfiler(method="search_endpoint", step="all"):
            try:
                with StepProfiler(method="search_endpoint", step="validate parameters"):
                    dataset = get_request_parameter(request, "dataset", required=True)
                    config = get_request_parameter(request, "config", required=True)
                    split = get_request_parameter(request, "split", required=True)
                    query = get_request_parameter(request, "query", required=True)
                    offset = get_request_parameter_offset(request)
                    length = get_request_parameter_length(request)

                with StepProfiler(method="search_endpoint", step="check authentication"):
                    # if auth_check fails, it will raise an exception that will be caught below
                    await auth_check(
                        dataset=dataset,
                        external_auth_url=external_auth_url,
                        request=request,
                        hf_jwt_public_keys=hf_jwt_public_keys,
                        hf_jwt_algorithm=hf_jwt_algorithm,
                        hf_timeout_seconds=hf_timeout_seconds,
                    )

                logging.info(f"/search {dataset=} {config=} {split=} {query=} {offset=} {length=}")

                with StepProfiler(method="search_endpoint", step="validate indexing was done"):
                    # no cache data is needed to download the index file
                    # but will help to validate if indexing was done
                    duckdb_index_cache_entry = get_cache_entry_from_duckdb_index_job(
                        dataset=dataset,
                        config=config,
                        split=split,
                        hf_endpoint=hf_endpoint,
                        hf_token=hf_token,
                        hf_timeout_seconds=hf_timeout_seconds,
                        blocked_datasets=blocked_datasets,
                        storage_clients=storage_clients,
                    )
                    revision = duckdb_index_cache_entry["dataset_git_revision"]
                    if duckdb_index_cache_entry["http_status"] != HTTPStatus.OK:
                        return get_json_error_response(
                            content=duckdb_index_cache_entry["content"],
                            status_code=duckdb_index_cache_entry["http_status"],
                            max_age=max_age_short,
                            error_code=duckdb_index_cache_entry["error_code"],
                            revision=revision,
                        )
                    if duckdb_index_cache_entry["content"]["has_fts"] is not True:
                        raise SearchFeatureNotAvailableError("The split does not have search feature enabled.")

                    # check if the index is on the full dataset or if it's partial
                    url = duckdb_index_cache_entry["content"]["url"]
                    filename = duckdb_index_cache_entry["content"]["filename"]
                    num_original_rows = duckdb_index_cache_entry["content"]["num_rows"]
                    partial = duckdb_index_is_partial(url)

                with StepProfiler(method="search_endpoint", step="download index file if missing"):
                    index_file_location = await get_index_file_location_and_download_if_missing(
                        duckdb_index_file_directory=duckdb_index_file_directory,
                        dataset=dataset,
                        config=config,
                        split=split,
                        revision=revision,
                        filename=filename,
                        url=url,
                        target_revision=target_revision,
                        hf_token=hf_token,
                    )

                with StepProfiler(method="search_endpoint", step="perform FTS command"):
                    logging.debug(f"connect to index file {index_file_location}")
                    fts_batches = dataset in DATASETS_WITH_FTS_BY_BATCHES
                    num_rows_total, pa_table = await anyio.to_thread.run_sync(
                        full_text_search,
                        num_original_rows,
                        fts_batches,
                        index_file_location,
                        query,
                        offset,
                        length,
                        extensions_directory,
                    )

                with StepProfiler(method="search_endpoint", step="create response"):
                    # Features can be missing in old cache entries,
                    # but in this case it's ok to get them from the Arrow schema.
                    # Indded at one point we refreshed all the datasets that need the features
                    # to be cached (i.e. image and audio datasets).
                    if "features" in duckdb_index_cache_entry["content"] and isinstance(
                        duckdb_index_cache_entry["content"]["features"], dict
                    ):
                        features = Features.from_dict(duckdb_index_cache_entry["content"]["features"])
                    else:
                        features = Features.from_arrow_schema(pa_table.schema)
                    response = await create_response(
                        pa_table=pa_table,
                        dataset=dataset,
                        revision=revision,
                        config=config,
                        split=split,
                        storage_client=cached_assets_storage_client,
                        offset=offset,
                        features=features,
                        num_rows_total=num_rows_total,
                        partial=partial,
                    )
                with StepProfiler(method="search_endpoint", step="generate the OK response"):
                    return get_json_ok_response(response, max_age=max_age_long, revision=revision)
            except Exception as e:
                error = e if isinstance(e, ApiError) else UnexpectedApiError("Unexpected error.", e)
                with StepProfiler(method="search_endpoint", step="generate API error response"):
                    return get_json_api_error_response(error=error, max_age=max_age_short, revision=revision)

    return search_endpoint
