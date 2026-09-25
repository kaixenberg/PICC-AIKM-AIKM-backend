"""Repository for nnp_database_ddl + nnp_database_rule (NEW)."""

from typing import Dict, List, Optional

from src.db.pool import get_cursor
from src.repositories._common import build_set_clause

_DDL_UPDATABLE = {"table_name", "ddl_text", "vanna_vector_id"}
_RULE_UPDATABLE = {"rule_name", "rule_text", "vanna_vector_id"}


# ---- nnp_database_ddl -------------------------------------------------------

def create_ddl(data: Dict, user: str) -> Dict:
    params = {
        "database_id": data.get("database_id"),
        "table_name":  data.get("table_name"),
        "ddl_text":    data.get("ddl_text"),
        "vanna_vector_id": data.get("vanna_vector_id"),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_database_ddl
                (database_id, table_name, ddl_text, vanna_vector_id, created_by, updated_by)
            VALUES (%(database_id)s, %(table_name)s, %(ddl_text)s, %(vanna_vector_id)s,
                    %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def get_ddl_by_database(database_id: str) -> List[Dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM nnp_database_ddl
            WHERE database_id = %s AND status != 'DELETED'
            ORDER BY created_at ASC
            """,
            (database_id,),
        )
        return cur.fetchall()


def update_ddl(ddl_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _DDL_UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_at = NOW(), updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = ddl_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_database_ddl SET {set_sql} WHERE id = %(id)s AND status != 'DELETED' RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()


def delete_ddl(ddl_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_database_ddl SET status = 'DELETED', updated_at = NOW() WHERE id = %s AND status != 'DELETED' RETURNING *",
            (ddl_id,),
        )
        return cur.fetchone()


# ---- nnp_database_rule ------------------------------------------------------

def create_rule(data: Dict, user: str) -> Dict:
    params = {
        "database_id": data.get("database_id"),
        "rule_name":   data.get("rule_name"),
        "rule_text":   data.get("rule_text"),
        "vanna_vector_id": data.get("vanna_vector_id"),
        "user": user,
    }
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO nnp_database_rule
                (database_id, rule_name, rule_text, vanna_vector_id, created_by, updated_by)
            VALUES (%(database_id)s, %(rule_name)s, %(rule_text)s, %(vanna_vector_id)s,
                    %(user)s, %(user)s)
            RETURNING *
            """,
            params,
        )
        return cur.fetchone()


def get_rules_by_database(database_id: str) -> List[Dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM nnp_database_rule
            WHERE database_id = %s AND status != 'DELETED'
            ORDER BY created_at ASC
            """,
            (database_id,),
        )
        return cur.fetchall()


def update_rule(rule_id: str, data: Dict, user: str) -> Optional[Dict]:
    clause, params = build_set_clause(data, _RULE_UPDATABLE)
    set_sql = (clause + ", " if clause else "") + "updated_at = NOW(), updated_by = %(updated_by)s"
    params["updated_by"] = user
    params["id"] = rule_id
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE nnp_database_rule SET {set_sql} WHERE id = %(id)s AND status != 'DELETED' RETURNING *",  # nosec B608
            params,
        )
        return cur.fetchone()


def delete_rule(rule_id: str) -> Optional[Dict]:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE nnp_database_rule SET status = 'DELETED', updated_at = NOW() WHERE id = %s AND status != 'DELETED' RETURNING *",
            (rule_id,),
        )
        return cur.fetchone()
