"""PostgreSQL connection pool + cursor helper (NEW).

No sibling owns a CRUD-over-its-own-Postgres store, so this layer is net-new.
It follows the siblings' driver choice (psycopg2) and is consumed from async
endpoints via fastapi.concurrency.run_in_threadpool.

Every checkout sets search_path to the configured schema ("nnp-rag") so the
repositories can use unqualified table names. The schema name contains a hyphen
and is therefore double-quoted.
"""

from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

_pool: Optional[ThreadedConnectionPool] = None


def init_pool() -> None:
    """Create the global connection pool without creating or changing schema."""
    global _pool
    if _pool is not None:
        return
    _pool = ThreadedConnectionPool(
        minconn=settings.pg_pool_min,
        maxconn=settings.pg_pool_max,
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=settings.pg_password,
    )
    logger.info(
        "DB pool initialised (%s:%s/%s, schema=%s, min=%s max=%s)",
        settings.pg_host, settings.pg_port, settings.pg_db,
        settings.pg_schema, settings.pg_pool_min, settings.pg_pool_max,
    )


def close_pool() -> None:
    """Dispose of the global connection pool."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("DB pool closed")


@contextmanager
def get_cursor() -> Iterator[RealDictCursor]:
    """Yield a RealDictCursor bound to a pooled connection.

    Sets search_path on checkout, commits on clean exit, rolls back on error,
    and always returns the connection to the pool. Run multiple statements in
    one `with` block to keep them in a single transaction (e.g. createBucket).
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised; call init_pool() first")
    conn = _pool.getconn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Schema name is hyphenated -> must be double-quoted. Value comes from
        # trusted config, and we quote it, so this is safe.
        cur.execute(f'SET search_path TO "{settings.pg_schema}", public')
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        _pool.putconn(conn)


def ping() -> None:
    """Lightweight connectivity check used by /health/deep."""
    with get_cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
