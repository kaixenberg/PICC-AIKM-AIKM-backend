"""NL→SQL orchestration service — Vanna edition (UPDATED).

Coordinates database selection (local embeddings via sentence-transformers), Vanna SQL generation,
and optional SQL execution.
Never raises — all errors are caught and returned in the response dict.
"""

import asyncio
import json
import math
from urllib.parse import parse_qs, urlparse

import psycopg2

from src.repositories import data_sql_repo
from src.services import vanna_service
from src.services.embeddings import embed_texts_vanna_local
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a)) or 1.0
    mag_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (mag_a * mag_b)


def _error_response(question: str, error: str) -> dict:
    return {
        "question": question,
        "sql": None,
        "sql_data": None,
        "database_name": None,
        "database_type": None,
        "db_id": None,
        "area": None,
        "rag_matches": 0,
        "curated_matches": 0,
        "error": error,
        "answer": error,
    }


def _execute_postgres_query(conn_url: str, username: str, password: str, sql: str) -> tuple[list, list]:
    conn = psycopg2.connect(dsn=conn_url, user=username, password=password)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description]
        fetched = cursor.fetchmany(200)
        return columns, list(fetched)
    finally:
        conn.close()


def _execute_clickhouse_query(conn_url: str, username: str, password: str, sql: str) -> tuple[list, list]:
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError(
            "clickhouse-connect is required for ClickHouse/SigNoz SQL execution."
        ) from exc

    parsed = urlparse(conn_url)
    if not parsed.hostname:
        raise ValueError("ClickHouse connection_url must include a host")

    query_params = parse_qs(parsed.query)
    database = query_params.get("database", [None])[0]
    if not database and parsed.path and parsed.path != "/":
        database = parsed.path.strip("/")

    secure = parsed.scheme == "https"
    port = parsed.port or (8443 if secure else 8123)
    client_kwargs = {
        "host": parsed.hostname,
        "port": port,
        "username": username or "default",
        "password": password or "",
        "secure": secure,
    }
    if database:
        client_kwargs["database"] = database
    client = clickhouse_connect.get_client(**client_kwargs)
    try:
        result = client.query(sql)
        rows = result.result_rows[:200]
        return list(result.column_names), rows
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _execute_target_query(selected_db: dict, sql: str) -> tuple[list, list]:
    cred = json.loads(selected_db.get("connection_credential") or "{}")
    username = cred.get("username", "")
    password = cred.get("password", "")
    conn_url = selected_db["connection_url"]
    db_type = (selected_db.get("database_type") or "postgres").strip().lower()

    if db_type in {"clickhouse", "signoz"}:
        return _execute_clickhouse_query(conn_url, username, password, sql)
    if db_type in {"postgres", "postgresql"}:
        return _execute_postgres_query(conn_url, username, password, sql)

    raise ValueError(f"Unsupported database_type for SQL execution: {db_type}")


async def train_from_script(
    db_id: str,
    training_script: str,
    db_name: str | None = None,
    db_type: str | None = None,
    area: str | None = None,
) -> None:
    """Delegate training to vanna_service. Called by manage_data_sql background task."""
    await vanna_service.train_from_script(db_id, training_script, db_name, db_type, area)


async def query(
    question: str,
    db_id: str | None,
    area: str | None,
    bucket_ids: list[str],
) -> dict:
    """Full NL→SQL orchestration.

    Returns a dict with question, sql, sql_data, database_name, database_type,
    db_id, area, rag_matches, curated_matches, error, answer.
    Never raises.
    """
    try:
        # ------------------------------------------------------------------
        # STEP 1 — Load candidate databases
        # ------------------------------------------------------------------
        all_dbs = await asyncio.to_thread(data_sql_repo.get_by_buckets, bucket_ids)
        if area:
            all_dbs = [d for d in all_dbs if d.get("area") == area]
        if not all_dbs:
            return _error_response(question, "No databases registered for your buckets")

        # ------------------------------------------------------------------
        # STEP 2 — Select database (local embeddings for semantic selection)
        # ------------------------------------------------------------------
        selected_db = None

        if db_id:
            selected_db = next((d for d in all_dbs if str(d["id"]) == db_id), None)
            if selected_db is None:
                logger.warning("query: requested db_id %s not found, using first", db_id)
                selected_db = all_dbs[0]

        elif len(all_dbs) == 1:
            selected_db = all_dbs[0]

        else:
            descriptions = [
                f"{d.get('database_name', '')} {d.get('database_desc', '')} {d.get('area', '')}"
                for d in all_dbs
            ]
            all_embs = await asyncio.to_thread(embed_texts_vanna_local, [question] + descriptions)
            if not all_embs or len(all_embs) < 2:
                logger.warning("query: embedding failed during DB selection, using first DB")
                selected_db = all_dbs[0]
            else:
                q_emb = all_embs[0]
                best_idx = max(
                    range(len(all_dbs)),
                    key=lambda i: _cosine_similarity(q_emb, all_embs[i + 1]),
                )
                selected_db = all_dbs[best_idx]
                logger.info(
                    "query: selected db '%s' from %d candidates for question '%s'",
                    selected_db.get("database_name"),
                    len(all_dbs),
                    question[:80],
                )

        # ------------------------------------------------------------------
        # STEP 3 — Generate SQL via Vanna (RAG + LLM handled internally)
        # ------------------------------------------------------------------
        sql, gen_error = await vanna_service.generate_sql(
            question=question,
            db_id=str(selected_db["id"]),
        )

        # Debug RAG retrieval
        rag_results = None
        try:
            vn = await vanna_service.get_vanna(str(selected_db["id"]))
            rag_results = await asyncio.to_thread(
                vn.get_similar_question_sql,
                question,
            )
            logger.info(
                "[query] RAG retrieved %d results for question: %s",
                len(rag_results), question[:50],
            )
            for i, r in enumerate(rag_results):
                logger.info("[query] RAG[%d]: %s", i, r.get("question", "")[:60])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[query] RAG debug failed: %s", exc)

        if gen_error:
            return {
                "question": question,
                "sql": None,
                "sql_data": None,
                "database_name": selected_db.get("database_name"),
                "database_type": selected_db.get("database_type"),
                "db_id": str(selected_db["id"]),
                "area": selected_db.get("area"),
                "rag_matches": 0,
                "curated_matches": 0,
                "error": gen_error,
                "answer": f"Could not generate SQL: {gen_error}",
            }

        # ------------------------------------------------------------------
        # STEP 4 — Execute SQL against the selected target database
        # ------------------------------------------------------------------
        sql_data = None
        execution_error = None
        rows: list = []

        db_type = (selected_db.get("database_type") or "postgres").strip().lower()
        can_execute = bool(sql and selected_db.get("connection_url")) and (
            db_type in {"clickhouse", "signoz"} or bool(selected_db.get("connection_credential"))
        )
        if can_execute:
            try:
                columns, rows = await asyncio.to_thread(_execute_target_query, selected_db, sql)
                sql_data = {
                    "columns": columns,
                    "rows": [list(r) for r in rows],
                    "row_count": len(rows),
                }
                logger.info(
                    "query: executed SQL for db '%s', %d rows",
                    selected_db.get("database_name"),
                    len(rows),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "query: SQL execution failed for db '%s': %s",
                    selected_db.get("database_name"),
                    exc,
                )
                execution_error = f"SQL execution failed: {exc}"

        # ------------------------------------------------------------------
        # STEP 5 — Self-training via Vanna
        # ------------------------------------------------------------------
        # Disabled intentionally: successful SQL execution does not prove the
        # generated query preserved the user's intended semantics. Auto-training
        # on executable but logically wrong SQL can pollute future retrieval.
        # if sql and sql_data:
        #     try:
        #         vn = await vanna_service.get_vanna(str(selected_db["id"]))
        #         await asyncio.to_thread(vn.train, question=question, sql=sql)
        #         logger.info("[self-train] stored via Vanna db=%s", str(selected_db["id"]))
        #     except Exception as exc:  # noqa: BLE001
        #         logger.warning("[self-train] failed: %s", exc)

        # ------------------------------------------------------------------
        # STEP 6 — Return response
        # ------------------------------------------------------------------
        return {
            "question": question,
            "sql": sql,
            "sql_data": sql_data,
            "database_name": selected_db.get("database_name"),
            "database_type": selected_db.get("database_type"),
            "db_id": str(selected_db["id"]),
            "area": selected_db.get("area"),
            "rag_matches": len(rag_results) if rag_results else 0,
            "curated_matches": 0,
            "error": execution_error,
            "answer": (
                f"Generated SQL for {selected_db.get('database_name', 'database')}. "
                f"{len(rows) if sql_data else 0} rows returned."
            ) if sql else f"Could not generate SQL: {gen_error}",
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("query: unexpected error: %s", exc)
        return _error_response(question, str(exc))
