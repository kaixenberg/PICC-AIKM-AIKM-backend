"""Repository for nnp_km_buckets + nnp_account_bucket_map (NEW)."""

from typing import Dict, List, Optional

from src.db.pool import get_cursor
from src.repositories._common import build_set_clause
from src.config.settings import settings

_UPDATABLE = {"bucket_category", "bucket_desc", "bucket_spec", "bucket_url", "status"}


def _embedding_backend() -> str:
    return "local" if settings.model_type.strip().lower() == "local" else "openai"


def _visible_embedding_backends() -> List[str]:
    model_type = settings.model_type.strip().lower()
    if model_type == "local":
        return ["local"]
    return ["openai", "public"]


def get_by_account(account_id: str, include_deleted: bool = False) -> List[Dict]:
    sql = """
        SELECT b.*
        FROM   nnp_km_buckets b
        JOIN   nnp_account_bucket_map m ON m.bucket_id = b.id
        WHERE  m.account_id = %s
          AND  (%s OR b.status <> 'DELETED')
          AND  b.embedding_backend = ANY(%s::text[])
        ORDER BY b.created_at DESC
    """
    with get_cursor() as cur:
        cur.execute(sql, (account_id, include_deleted, _visible_embedding_backends()))
        return cur.fetchall()


def get_by_id(bucket_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM nnp_km_buckets WHERE id = %s", (bucket_id,))
        return cur.fetchone()


def get_account_ids_by_bucket(bucket_id: str) -> List[str]:
    """Return non-owner account IDs mapped to a bucket."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT account_id
            FROM nnp_account_bucket_map
            WHERE bucket_id = %s
              AND ownership <> 'owner'
            ORDER BY account_id
            """,
            (bucket_id,),
        )
        return [row["account_id"] for row in cur.fetchall()]


def create(data: Dict, user: str) -> Dict:
    """Insert the bucket (status PROVISIONING) and the account mapping atomically."""
    params = {
        "bucket_name": data.get("bucket_name"),
        "bucket_category": data.get("bucket_category"),
        "bucket_desc": data.get("bucket_desc"),
        "bucket_spec": data.get("bucket_spec"),
        "bucket_url": data.get("bucket_url"),
        "embedding_backend": _embedding_backend(),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_km_buckets
                (bucket_name, bucket_category, bucket_desc, bucket_spec, bucket_url,
                 embedding_backend, status, created_by, updated_by)
            VALUES (%(bucket_name)s, %(bucket_category)s, %(bucket_desc)s, %(bucket_spec)s,
                    %(bucket_url)s, %(embedding_backend)s, 'PROVISIONING', %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        bucket = cur.fetchone()
        cur.execute(
            """
            INSERT INTO nnp_account_bucket_map (account_id, bucket_id, ownership)
            VALUES (%s, %s, 'owner')
            ON CONFLICT (account_id, bucket_id) DO NOTHING
            """,
            (data["account_id"], bucket["id"]),
        )
        return bucket


def update(bucket_id: str, data: Dict, user: str, account_ids: Optional[List[str]]) -> Optional[Dict]:
    """Update bucket attributes and optionally replace the readonly account mappings."""
    clause, params = build_set_clause(data, _UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = bucket_id

    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_km_buckets SET {set_sql} WHERE id = %(id)s RETURNING *",  # nosec B608
            params,
        )
        row = cur.fetchone()
        if row is None:
            return None

        if account_ids is not None:
            cur.execute(
                """
                INSERT INTO nnp_account_bucket_map (account_id, bucket_id, ownership)
                SELECT unnest(%s::text[]), %s, 'readonly'
                ON CONFLICT (account_id, bucket_id) DO NOTHING
                """,
                (account_ids, bucket_id),
            )
            cur.execute(
                """
                DELETE FROM nnp_account_bucket_map
                WHERE bucket_id = %s
                  AND ownership = 'readonly'
                  AND account_id <> ALL(%s::text[])
                """,
                (bucket_id, account_ids),
            )
        return row


def delete(bucket_id: str) -> Optional[Dict]:
    """Soft-delete: set status DELETED, return the row (None if not found)."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_km_buckets SET status = 'DELETED' WHERE id = %s RETURNING *",
            (bucket_id,),
        )
        return cur.fetchone()


def set_provision_result(bucket_id: str, status: str, error_detail: Optional[str]) -> Optional[Dict]:
    """Callback target: write the async Milvus-provision outcome."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_km_buckets SET status = %s, error_detail = %s WHERE id = %s RETURNING *",
            (status, error_detail, bucket_id),
        )
        return cur.fetchone()
