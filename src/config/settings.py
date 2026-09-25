"""Application settings loaded from the config server."""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.config.config_server import fetch_config, load_bootstrap, load_local_overrides

logger = logging.getLogger(__name__)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


class BaseAppSettings(BaseModel):
    """Shared API settings."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")  # nosec B104
    api_port: int = Field(default=8000, alias="API_PORT")
    api_public_url: Optional[str] = Field(default=None, alias="API_PUBLIC_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


class ApiSettings(BaseAppSettings):
    """CoreComp CRUD API settings loaded from the config server."""

    # CoreComp PostgreSQL - the database that holds the configured KM schema.
    pg_host: str = Field(default="", alias="PG_HOST")
    pg_port: int = Field(default=5432, alias="PG_PORT")
    pg_db: str = Field(default="", alias="PG_DB")
    pg_user: str = Field(default="", alias="PG_USER")
    pg_password: str = Field(default="", alias="PG_PASSWORD")
    pg_schema: str = Field(default="", alias="PG_SCHEMA")
    pg_pool_min: int = Field(default=1, alias="PG_POOL_MIN")
    pg_pool_max: int = Field(default=10, alias="PG_POOL_MAX")

    # Identity - created_by / updated_by come from this request header.
    user_header: str = Field(default="User", alias="USER_HEADER")
    default_user: str = Field(default="system", alias="DEFAULT_USER")

    # Milvus - direct connection for collection management.
    milvus_host: str = Field(default="", alias="MILVUS_HOST")
    milvus_port: int = Field(default=19530, alias="MILVUS_PORT")
    milvus_db_name: str = Field(default="", alias="MILVUS_DB_NAME")

    # Milvus provisioning service (async + callback).
    provision_enabled: bool = Field(default=False, alias="PROVISION_ENABLED")
    provision_service_url: Optional[str] = Field(default=None, alias="PROVISION_SERVICE_URL")
    provision_delete_url: Optional[str] = Field(default=None, alias="PROVISION_DELETE_URL")

    # Ingestion service (async + callback).
    ingestion_enabled: bool = Field(default=False, alias="INGESTION_ENABLED")
    ingestion_service_url: Optional[str] = Field(default=None, alias="INGESTION_SERVICE_URL")
    ingestion_delete_url: Optional[str] = Field(default=None, alias="INGESTION_DELETE_URL")

    # Externally-reachable callback target for async services.
    callback_base_url: str = Field(default="", alias="CALLBACK_BASE_URL")

    # Outbound HTTP timeout in seconds.
    http_timeout: float = Field(default=30.0, alias="HTTP_TIMEOUT")

    # Embeddings and document summaries.
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    local_embedding_model: str = Field(default="", alias="LOCAL_EMBEDDING_MODEL")
    vanna_local_embedding_model: str = Field(default="", alias="VANNA_LOCAL_EMBEDDING_MODEL")
    vanna_public_embedding_model: str = Field(default="", alias="VANNA_PUBLIC_EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=3072, alias="EMBEDDING_DIMENSION")
    local_embedding_dimension: int = Field(default=384, alias="LOCAL_EMBEDDING_DIMENSION")
    embedding_batch_size: int = Field(default=100, alias="EMBEDDING_BATCH_SIZE")
    chunk_size_tokens: int = Field(default=800, alias="CHUNK_SIZE_TOKENS")
    chunk_overlap_tokens: int = Field(default=120, alias="CHUNK_OVERLAP_TOKENS")
    normalize_embeddings: bool = Field(default=True, alias="NORMALIZE_EMBEDDINGS")
    model_type: str = Field(default="", validation_alias="MODEL_TYPE")
    get_summary_timeout_public: int = Field(default=120, alias="GET_SUMMARY_TIMEOUT_PUBLIC")
    get_summary_timeout_local: int = Field(default=180, alias="GET_SUMMARY_TIMEOUT_LOCAL")
    pdf_vlm_local_model: str = Field(default="", alias="PDF_VLM_LOCAL_MODEL")
    pdf_vlm_public_model: str = Field(default="", alias="PDF_VLM_PUBLIC_MODEL")

    # MinIO PDF visual artifacts.
    minio_host: str = Field(default="", alias="MINIO_HOST")
    minio_root_user: str = Field(default="", alias="MINIO_ROOT_USER")
    minio_root_password: str = Field(default="", alias="MINIO_ROOT_PASSWORD")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_bucket: str = Field(default="", alias="MINIO_BUCKET")

    # Anthropic Claude - retained for fallback/public-mode paths.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Ollama - local LLM server for SQL generation.
    ollama_base_url: str = Field(default="", alias="OLLAMA_BASE_URL")
    ollama_keep_alive: str = Field(default="", alias="OLLAMA_KEEP_ALIVE")
    ollama_num_ctx: int = Field(default=16384, alias="OLLAMA_NUM_CTX")

    # Vanna NL->SQL (PGVector vector store).
    pg_connection_string_vanna: str = Field(default="", alias="PG_CONNECTION_STRING_VANNA")
    vanna_n_results: int = Field(default=6, alias="VANNA_N_RESULTS")

    # RAG query service proxy.
    rag_query_service_url: str = Field(default="", alias="RAG_QUERY_SERVICE_URL")

    # NL->SQL generation.
    llm_model: str = Field(default="", alias="LLM_MODEL")
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=1000, alias="LLM_MAX_TOKENS")
    sql_milvus_collection: str = Field(default="", alias="SQL_MILVUS_COLLECTION")


REQUIRED_CONFIG_KEYS = {
    "API_HOST",
    "API_PORT",
    "LOG_LEVEL",
    "PG_HOST",
    "PG_PORT",
    "PG_DB",
    "PG_USER",
    "PG_PASSWORD",
    "PG_SCHEMA",
    "PG_POOL_MIN",
    "PG_POOL_MAX",
    "USER_HEADER",
    "DEFAULT_USER",
    "MILVUS_HOST",
    "MILVUS_PORT",
    "MILVUS_DB_NAME",
    "CALLBACK_BASE_URL",
    "HTTP_TIMEOUT",
    "MODEL_TYPE",
    "GET_SUMMARY_TIMEOUT_PUBLIC",
    "GET_SUMMARY_TIMEOUT_LOCAL",
    "PDF_VLM_LOCAL_MODEL",
    "PDF_VLM_PUBLIC_MODEL",
    "EMBEDDING_MODEL",
    "LOCAL_EMBEDDING_MODEL",
    "VANNA_LOCAL_EMBEDDING_MODEL",
    "VANNA_PUBLIC_EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
    "LOCAL_EMBEDDING_DIMENSION",
    "EMBEDDING_BATCH_SIZE",
    "CHUNK_SIZE_TOKENS",
    "CHUNK_OVERLAP_TOKENS",
    "NORMALIZE_EMBEDDINGS",
    "MINIO_HOST",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "MINIO_BUCKET",
    "OLLAMA_BASE_URL",
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_NUM_CTX",
    "PG_CONNECTION_STRING_VANNA",
    "VANNA_N_RESULTS",
    "RAG_QUERY_SERVICE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
}

CONDITIONAL_REQUIRED_CONFIG_KEYS = {
    "PROVISION_ENABLED": {"PROVISION_SERVICE_URL", "PROVISION_DELETE_URL"},
    "INGESTION_ENABLED": {"INGESTION_SERVICE_URL", "INGESTION_DELETE_URL"},
}

SECRET_CONFIG_KEYS = {
    "ANTHROPIC_API_KEY",
    "MINIO_ROOT_PASSWORD",
    "OPENAI_API_KEY",
    "PG_CONNECTION_STRING_VANNA",
    "PG_PASSWORD",
}


def _settings_aliases() -> set[str]:
    aliases: set[str] = set()
    for field_name, field_info in ApiSettings.model_fields.items():
        aliases.add(str(field_info.alias or field_name))
        validation_alias = field_info.validation_alias
        if validation_alias:
            aliases.add(str(validation_alias))
    return aliases


def _required_keys(config: dict[str, object]) -> set[str]:
    required = set(REQUIRED_CONFIG_KEYS)
    if str(config.get("MODEL_TYPE", "")).strip().lower() != "local":
        required.add("OPENAI_API_KEY")
    for flag_key, dependent_keys in CONDITIONAL_REQUIRED_CONFIG_KEYS.items():
        if _truthy(config.get(flag_key)):
            required.update(dependent_keys)
    return required


def _validate_required_config(config: dict[str, object]) -> None:
    required = _required_keys(config)
    missing = sorted(key for key in required if _is_missing(config.get(key)))
    fetched = len(required) - len(missing)

    if missing:
        logger.error(
            "Effective config validation failed: resolved %s/%s required keys; missing=%s",
            fetched,
            len(required),
            ", ".join(missing),
        )
        raise RuntimeError(
            "Missing required config values: " + ", ".join(missing)
        )

    logger.info(
        "Effective config validation succeeded: resolved all %s required keys",
        len(required),
    )


BOOTSTRAP_CONFIG_KEYS = {
    "CONFIG_SERVER_URL",
    "CONFIG_APP_NAME",
    "CONFIG_PROFILE",
    "CONFIG_TAG",
    "CONFIG_SERVER_TIMEOUT",
    "CONFIG_SERVER_REQUIRED",
    "ALLOW_LOCAL_CONFIG_OVERRIDES",
}


def _load_config_server_values() -> tuple[dict[str, object], bool]:
    bootstrap = load_bootstrap()
    try:
        values, _ = fetch_config()
        return values, True
    except Exception:
        if bootstrap.required:
            raise
        logger.exception(
            "Config server fetch failed, continuing with local env overrides "
            "because CONFIG_SERVER_REQUIRED=false"
        )
        return {}, False


_bootstrap = load_bootstrap()
_config_server_values, _config_server_loaded = _load_config_server_values()
_allowed_override_keys = (
    _settings_aliases()
    | REQUIRED_CONFIG_KEYS
    | set(CONDITIONAL_REQUIRED_CONFIG_KEYS)
    | set().union(*CONDITIONAL_REQUIRED_CONFIG_KEYS.values())
    | SECRET_CONFIG_KEYS
)
_local_overrides = (
    load_local_overrides(
        excluded_keys=BOOTSTRAP_CONFIG_KEYS,
        allowed_keys=_allowed_override_keys,
    )
    if _bootstrap.allow_local_config_overrides or not _config_server_loaded
    else {}
)
if not _bootstrap.allow_local_config_overrides and _config_server_loaded:
    logger.info(
        "Local runtime config overrides disabled; using config-server values only"
    )
_effective_config = {**_config_server_values, **_local_overrides}
if _local_overrides:
    logger.info(
        "Applied %s non-empty local config override(s): %s",
        len(_local_overrides),
        ", ".join(sorted(_local_overrides)),
    )
_validate_required_config(_effective_config)
settings = ApiSettings(**_effective_config)
