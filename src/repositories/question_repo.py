"""Repository for nnp_km_qa (NEW)."""

from typing import Dict, List, Optional

from src.db.pool import get_cursor
from src.repositories._common import build_set_clause

_UPDATABLE = {"question", "answer", "rank", "status", "match_threshold"}


def get_by_buckets(bucket_ids: List[str], include_deleted: bool = False) -> List[Dict]:
    sql = """
        SELECT *
        FROM   nnp_km_qa
        WHERE  bucket_id = ANY(%s::uuid[])
          AND  (%s OR status <> 'DELETED')
        ORDER BY bucket_id, rank
    """
    with get_cursor() as cur:
        cur.execute(sql, (bucket_ids, include_deleted))
        return cur.fetchall()


def get_by_id(qa_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM nnp_km_qa WHERE id = %s", (qa_id,))
        return cur.fetchone()


def create(data: Dict, user: str) -> Dict:
    params = {
        "bucket_id": data.get("bucket_id"),
        "question": data.get("question"),
        "answer": data.get("answer"),
        "rank": data.get("rank"),
        "status": data.get("status") or "DRAFT",
        "match_threshold": data.get("match_threshold"),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_km_qa
                (bucket_id, question, answer, rank, status, match_threshold,
                 created_by, updated_by)
            VALUES (%(bucket_id)s, %(question)s, %(answer)s, %(rank)s, %(status)s,
                    %(match_threshold)s, %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def delete(qa_id: str) -> Optional[Dict]:
    """Soft-delete: set status DELETED, return the row (None if not found)."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_km_qa SET status = 'DELETED' WHERE id = %s RETURNING *",
            (qa_id,),
        )
        return cur.fetchone()


def update(qa_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = qa_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_km_qa SET {set_sql} WHERE id = %(id)s RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()
