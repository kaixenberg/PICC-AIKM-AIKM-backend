"""Repository for nnp_bucket_details (NEW)."""

from typing import Dict, List, Optional

from src.db.pool import get_cursor
from src.repositories._common import build_set_clause

_UPDATABLE = {"doc_category", "doc_name", "description", "format", "doc_size", "status"}


def get_by_bucket(
    bucket_id: str,
    include_deleted: bool = False,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict]:
    sql = """
        SELECT *
        FROM   nnp_bucket_details
        WHERE  bucket_id = %(bucket_id)s
          AND  (%(include_deleted)s OR status <> 'DELETED')
          AND  (%(category)s IS NULL OR doc_category = %(category)s)
          AND  (%(status)s IS NULL OR status = %(status)s)
        ORDER BY created_at DESC
    """
    with get_cursor() as cur:
        cur.execute(sql, {
            "bucket_id": bucket_id,
            "include_deleted": include_deleted,
            "category": category,
            "status": status,
        })
        return cur.fetchall()


def get_by_id(detail_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM nnp_bucket_details WHERE id = %s", (detail_id,))
        return cur.fetchone()


def create(data: Dict, user: str) -> Dict:
    """Insert a document row as PENDING; ingestion runs async and calls back."""
    params = {
        "bucket_id": data.get("bucket_id"),
        "doc_category": data.get("doc_category"),
        "doc_name": data.get("doc_name"),
        "description": data.get("description"),
        "format": data.get("format"),
        "doc_size": data.get("doc_size"),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_bucket_details
                (bucket_id, doc_category, doc_name, description, format, doc_size,
                 status, created_by, updated_by)
            VALUES (%(bucket_id)s, %(doc_category)s, %(doc_name)s, %(description)s,
                    %(format)s, %(doc_size)s, 'PENDING', %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def update(detail_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = detail_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_bucket_details SET {set_sql} WHERE id = %(id)s RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()


def delete(detail_id: str) -> Optional[Dict]:
    """Soft-delete: set status DELETED, return the row (None if not found)."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_bucket_details SET status = 'DELETED' WHERE id = %s RETURNING *",
            (detail_id,),
        )
        return cur.fetchone()


def apply_ingestion_result(data: Dict) -> Optional[Dict]:
    """Callback target: write the async ingestion outcome + milvus_* columns."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE nnp_bucket_details
            SET status = %(status)s,
                milvus_source_id = COALESCE(%(milvus_source_id)s, milvus_source_id),
                milvus_chunks_stored = COALESCE(%(milvus_chunks_stored)s, milvus_chunks_stored),
                milvus_chunks_duplicated = COALESCE(%(milvus_chunks_duplicated)s, milvus_chunks_duplicated),
                ingest_request_id = COALESCE(%(ingest_request_id)s, ingest_request_id),
                ingested_at = COALESCE(%(ingested_at)s, ingested_at),
                error_detail = %(error_detail)s
            WHERE id = %(id)s
            RETURNING *
            """,
            data,
        )
        return cur.fetchone()
