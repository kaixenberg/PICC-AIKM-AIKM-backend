"""manageSqlQuery router (NEW). NL→SQL query and promote-to-curated endpoints."""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from src.api.deps import current_user
from src.db.pool import get_cursor
from src.repositories import data_sql_repo, query_history_repo
from src.services import vanna_service, sql_query_service
from src.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/manageSqlQuery", tags=["manageSqlQuery"])


class SqlQueryRequest(BaseModel):
    question: str
    bucket_ids: list[str]
    db_id: str | None = None
    area: str | None = None


class PromoteQueryRequest(BaseModel):
    database_id: str
    question: str
    sql: str
    query_name: str | None = None
    query_desc: str | None = None


class SaveHistoryRequest(BaseModel):
    question: str
    sql_text: str | None = None
    db_id: str | None = None
    db_name: str | None = None
    area: str | None = None
    row_count: int | None = None
    has_error: bool = False


class FeedbackRequest(BaseModel):
    question: str
    sql_text: str | None = None
    db_id: str | None = None
    feedback: int
    comment: str | None = None


@router.post("/query")
async def sql_query(
    payload: SqlQueryRequest,
    request: Request,
):
    """Main NL→SQL endpoint.

    Orchestrates: DB selection → Vanna generate → execution → Vanna self-train.
    """
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="question cannot be empty")
    if not payload.bucket_ids:
        raise HTTPException(status_code=422, detail="bucket_ids cannot be empty")

    result = await sql_query_service.query(
        question=payload.question,
        db_id=payload.db_id,
        area=payload.area,
        bucket_ids=payload.bucket_ids,
    )
    return result


@router.post("/promoteQuery", status_code=201)
async def promote_query(
    payload: PromoteQueryRequest,
    request: Request,
):
    """Save a successful NL→SQL pair as a curated query (quality_score=1.0, status=PUBLISHED).

    Also trains the pair into the Vanna vector store for this database.
    """
    user = current_user(request)

    row = await run_in_threadpool(
        data_sql_repo.create_sql,
        {
            "database_id": payload.database_id,
            "query_name": payload.query_name or payload.question[:60],
            "query_desc": payload.query_desc,
            "query_context": payload.question,
            "query_text": payload.sql,
            "status": "PUBLISHED",
            "quality_score": 1.0,
            "rank": 1,
        },
        user,
    )

    try:
        db = await run_in_threadpool(data_sql_repo.get_database_by_id, payload.database_id)
        if db:
            vn = await vanna_service.get_vanna(str(db["id"]))
            await asyncio.to_thread(vn.train, question=payload.question, sql=payload.sql)
            logger.info("[promoteQuery] trained via Vanna for db=%s", str(db["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.error("[promoteQuery] Vanna train failed: %s", exc)

    return row


@router.post("/saveHistory", status_code=201)
async def save_history(
    payload: SaveHistoryRequest,
    request: Request,
):
    """Persist a query result to the user's history."""
    user = current_user(request)
    try:
        row = await run_in_threadpool(
            query_history_repo.save,
            user,
            payload.model_dump(),
        )
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("[saveHistory] failed for user=%s: %s", user, exc)
        return {"saved": False}


@router.post("/submitFeedback", status_code=201)
async def submit_feedback(
    payload: FeedbackRequest,
    request: Request,
):
    """Record thumbs-up (1) or thumbs-down (-1) feedback for a query result."""
    if payload.feedback not in (1, -1):
        raise HTTPException(status_code=422, detail="feedback must be 1 or -1")
    user = current_user(request)
    try:
        def _insert() -> dict:
            with get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO nnp_km_query_feedback
                        (user_id, question, sql_text, db_id, feedback, comment)
                    VALUES (%(user_id)s, %(question)s, %(sql_text)s, %(db_id)s,
                            %(feedback)s, %(comment)s)
                    RETURNING *
                    """,
                    {
                        "user_id": user,
                        "question": payload.question,
                        "sql_text": payload.sql_text,
                        "db_id": payload.db_id,
                        "feedback": payload.feedback,
                        "comment": payload.comment,
                    },
                )
                return cur.fetchone()
        row = await run_in_threadpool(_insert)
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("[submitFeedback] failed for user=%s: %s", user, exc)
        return {"saved": False}


@router.get("/getHistory")
async def get_history(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
):
    """Return the most recent query history rows for the current user."""
    user = current_user(request)
    try:
        rows = await run_in_threadpool(query_history_repo.get_by_user, user, limit)
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("[getHistory] failed for user=%s: %s", user, exc)
        return []
