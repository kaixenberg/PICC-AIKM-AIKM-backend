"""Repository for nnp_km_database + nnp_database_q (NEW)."""

from typing import Dict, List, Optional

from src.config.settings import settings
from src.db.pool import get_cursor
from src.repositories._common import build_set_clause

_DB_UPDATABLE = {"database_name", "database_type", "database_desc", "connection_url",
                 "connection_credential", "area", "training_script", "keywords", "status"}
_SQL_UPDATABLE = {"query_name", "query_desc", "query_context", "query_text", "status",
                  "rank", "milvus_exemplar_id", "quality_score"}


def _vanna_embedding_backend() -> str:
    return "local" if settings.model_type.strip().lower() == "local" else "openai"


def get_by_buckets(bucket_ids: List[str], include_deleted: bool = False) -> List[Dict]:
    """Return databases in the given buckets, each with a nested `queries` list."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM nnp_km_database
            WHERE bucket_id = ANY(%s::uuid[]) AND (%s OR status <> 'DELETED')
            ORDER BY created_at DESC
            """,
            (bucket_ids, include_deleted),
        )
        databases = cur.fetchall()
        db_ids = [str(d["id"]) for d in databases]

        queries_by_db: Dict[str, List[Dict]] = {}
        ddl_by_db: Dict[str, List[Dict]] = {}
        rules_by_db: Dict[str, List[Dict]] = {}
        if db_ids:
            cur.execute(
                """
                SELECT * FROM nnp_database_q
                WHERE database_id = ANY(%s::uuid[]) AND (%s OR status <> 'DELETED')
                ORDER BY database_id, rank
                """,
                (db_ids, include_deleted),
            )
            for q in cur.fetchall():
                queries_by_db.setdefault(str(q["database_id"]), []).append(q)

            cur.execute(
                """
                SELECT * FROM nnp_database_ddl
                WHERE database_id = ANY(%s::uuid[]) AND (%s OR status <> 'DELETED')
                ORDER BY database_id, created_at
                """,
                (db_ids, include_deleted),
            )
            for ddl in cur.fetchall():
                ddl_by_db.setdefault(str(ddl["database_id"]), []).append(ddl)

            cur.execute(
                """
                SELECT * FROM nnp_database_rule
                WHERE database_id = ANY(%s::uuid[]) AND (%s OR status <> 'DELETED')
                ORDER BY database_id, created_at
                """,
                (db_ids, include_deleted),
            )
            for rule in cur.fetchall():
                rules_by_db.setdefault(str(rule["database_id"]), []).append(rule)

        for d in databases:
            d["queries"] = queries_by_db.get(str(d["id"]), [])
            d["ddl"] = ddl_by_db.get(str(d["id"]), [])
            d["rules"] = rules_by_db.get(str(d["id"]), [])
        return databases


# ---- nnp_km_database ----------------------------------------------------

def get_database_by_id(db_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM nnp_km_database WHERE id = %s", (db_id,))
        return cur.fetchone()


def get_sql_by_database(database_id: str) -> List[Dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM nnp_database_q
            WHERE database_id = %s AND status != 'DELETED'
            ORDER BY created_at ASC
            """,
            (database_id,),
        )
        return cur.fetchall()


def create_database(data: Dict, user: str) -> Dict:
    params = {
        "bucket_id": data.get("bucket_id"),
        "database_name": data.get("database_name"),
        "database_type": data.get("database_type"),
        "database_desc": data.get("database_desc"),
        "connection_url": data.get("connection_url"),
        "connection_credential": data.get("connection_credential"),
        "area": data.get("area"),
        "training_script": data.get("training_script"),
        "keywords": data.get("keywords"),
        "vanna_embedding_backend": _vanna_embedding_backend(),
        "status": data.get("status") or "ACTIVE",
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_km_database
                (bucket_id, database_name, database_type, database_desc, connection_url,
                 connection_credential, area, training_script, keywords,
                 vanna_embedding_backend, status, created_by, updated_by)
            VALUES (%(bucket_id)s, %(database_name)s, %(database_type)s, %(database_desc)s,
                    %(connection_url)s, %(connection_credential)s, %(area)s,
                    %(training_script)s, %(keywords)s, %(vanna_embedding_backend)s,
                    %(status)s, %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def delete_database(db_id: str) -> Optional[Dict]:
    """Soft-delete: set status DELETED, return the row (None if not found)."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_km_database SET status = 'DELETED' WHERE id = %s RETURNING *",
            (db_id,),
        )
        return cur.fetchone()


def update_database(db_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _DB_UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = db_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_km_database SET {set_sql} WHERE id = %(id)s RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()


# ---- nnp_database_q -----------------------------------------------------

def get_sql_by_id(sql_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM nnp_database_q WHERE id = %s", (sql_id,))
        return cur.fetchone()


def create_sql(data: Dict, user: str) -> Dict:
    params = {
        "database_id": data.get("database_id"),
        "query_name": data.get("query_name"),
        "query_desc": data.get("query_desc"),
        "query_context": data.get("query_context"),
        "query_text": data.get("query_text"),
        "status": data.get("status") or "DRAFT",
        "rank": data.get("rank"),
        "milvus_exemplar_id": data.get("milvus_exemplar_id"),
        "quality_score": data.get("quality_score"),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_database_q
                (database_id, query_name, query_desc, query_context, query_text, status,
                 rank, milvus_exemplar_id, quality_score, created_by, updated_by)
            VALUES (%(database_id)s, %(query_name)s, %(query_desc)s, %(query_context)s,
                    %(query_text)s, %(status)s, %(rank)s, %(milvus_exemplar_id)s,
                    %(quality_score)s, %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def delete_sql(sql_id: str) -> Optional[Dict]:
    """Soft-delete: set status DELETED, return the row (None if not found)."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_database_q SET status = 'DELETED' WHERE id = %s RETURNING *",
            (sql_id,),
        )
        return cur.fetchone()


def update_sql(sql_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _SQL_UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = sql_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_database_q SET {set_sql} WHERE id = %(id)s RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()
