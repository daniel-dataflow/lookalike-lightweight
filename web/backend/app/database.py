"""
데이터베이스 연결 관리 (SQLAlchemy & PostgreSQL 표준 연동)
- Redis/MongoDB 제거 → DB 기반 세션 + PostgreSQL 통합
"""
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from typing import Optional
import logging
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# 글로벌 SQLAlchemy 엔진 및 세션 팩토리
# ──────────────────────────────────────
engine = None
SessionLocal = None


# ========================
# PostgreSQL (SQLAlchemy)
# ========================
def init_postgres():
    """DATABASE_URL 기반 SQLAlchemy 엔진 및 커넥션 풀 초기화"""
    global engine, SessionLocal
    settings = get_settings()
    try:
        db_url = settings.DATABASE_URL
        if not db_url:
            raise ValueError("DATABASE_URL이 설정되지 않았습니다.")

        # SQLAlchemy 호환성을 위해 postgres:// 스키마 수정
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        connect_args = {}
        # URL에서 호스트 이름을 추출하여 로컬/도커 개발용 호스트인지 검증합니다.
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        hostname = parsed.hostname or ""
        is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
        if not is_local:
            connect_args["sslmode"] = "require"
            logger.info("🔒 원격 데이터베이스 연결 - sslmode=require 강제 적용")

        engine = create_engine(
            db_url,
            pool_size=settings.POSTGRES_MIN_CONN,
            max_overflow=max(0, settings.POSTGRES_MAX_CONN - settings.POSTGRES_MIN_CONN),
            connect_args=connect_args,
            pool_pre_ping=True
        )
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("✅ PostgreSQL SQLAlchemy 엔진 및 커넥션 풀 초기화 완료")
    except Exception as e:
        logger.error(f"❌ PostgreSQL 연결 실패: {e}")
        engine = None
        SessionLocal = None


@contextmanager
def get_pg_connection():
    """SQLAlchemy 커넥션 풀에서 raw connection을 획득하여 컨텍스트 매니저로 제공"""
    if engine is None:
        raise ConnectionError("SQLAlchemy 엔진이 초기화되지 않았습니다")
    conn = engine.raw_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_pg_cursor(dict_cursor=True):
    """PostgreSQL 커서를 직접 제공하는 편의 함수"""
    with get_pg_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
        finally:
            cur.close()


# ========================
# DB 기반 세션 관리 (Redis 대체)
# ========================
def create_session(user_data: dict, is_admin: bool = False) -> str:
    """DB 기반 세션 생성 (Redis 대체)

    Args:
        user_data: 세션에 저장할 사용자 데이터
        is_admin: 어드민 세션 여부

    Returns:
        세션 토큰 문자열
    """
    settings = get_settings()
    token = uuid.uuid4().hex
    session_json = json.dumps(user_data, default=str, ensure_ascii=False)
    expires_at = datetime.utcnow() + timedelta(hours=settings.SESSION_EXPIRE_HOURS)

    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_sessions (token, user_id, session_data, is_admin, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE
                SET session_data = EXCLUDED.session_data, expires_at = EXCLUDED.expires_at
                """,
                (token, user_data.get("user_id"), session_json, is_admin, expires_at),
            )
        return token
    except Exception as e:
        logger.error(f"세션 생성 실패: {e}")
        raise


def get_session(token: str, is_admin: bool = False) -> Optional[dict]:
    """DB에서 세션 조회 (Redis 대체)

    Args:
        token: 세션 토큰
        is_admin: 어드민 세션만 검색할지 여부

    Returns:
        세션 데이터 dict 또는 None
    """
    if not token:
        return None

    try:
        with get_pg_cursor() as cur:
            cur.execute(
                """
                SELECT session_data FROM user_sessions
                WHERE token = %s AND is_admin = %s AND expires_at > NOW()
                """,
                (token, is_admin),
            )
            row = cur.fetchone()
            if row:
                data = row["session_data"]
                return data if isinstance(data, dict) else json.loads(data)
    except Exception as e:
        logger.warning(f"세션 조회 실패: {e}")

    return None


def delete_session(token: str):
    """DB에서 세션 삭제"""
    if not token:
        return
    try:
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM user_sessions WHERE token = %s", (token,))
    except Exception as e:
        logger.warning(f"세션 삭제 실패: {e}")


def cleanup_expired_sessions():
    """만료된 세션 정리 (주기적 호출용)"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("DELETE FROM user_sessions WHERE expires_at < NOW()")
            deleted = cur.rowcount
            if deleted > 0:
                logger.info(f"만료 세션 {deleted}건 정리 완료")
    except Exception as e:
        logger.warning(f"세션 정리 실패: {e}")


# ========================
# 전체 초기화 / 종료
# ========================
def init_all_databases():
    """데이터베이스 연결 초기화 (앱 시작 시 호출)"""
    init_postgres()
    _ensure_infra_metrics_table()
    _ensure_app_logs_table()
    logger.info("🚀 PostgreSQL 데이터베이스 연결 초기화 완료")


def _ensure_infra_metrics_table():
    """infra_metrics 링 버퍼 테이블이 없으면 자동 생성 (초경량 Ring Buffer)"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS infra_metrics (
                    id        SERIAL PRIMARY KEY,
                    cpu_usage REAL,
                    memory_usage REAL,
                    timestamp TIMESTAMP DEFAULT NOW()
                );
            """)
        logger.info("✅ infra_metrics 테이블 확인/생성 완료")
    except Exception as e:
        logger.error(f"❌ infra_metrics 테이블 생성 실패: {e}")


def _ensure_app_logs_table():
    """app_logs 링 버퍼 테이블이 없으면 자동 생성 (초경량 Log Ring Buffer)"""
    try:
        with get_pg_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_logs (
                    id SERIAL PRIMARY KEY,
                    level VARCHAR(20),
                    service VARCHAR(50) DEFAULT 'FastAPI',
                    message TEXT,
                    error_type VARCHAR(100),
                    timestamp TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # 기존 컬럼이 있는 경우 TIMESTAMPTZ로 마이그레이션 시도
            cur.execute("""
                ALTER TABLE app_logs ALTER COLUMN timestamp TYPE TIMESTAMPTZ;
            """)
        logger.info("✅ app_logs 테이블 확인/생성 및 TIMESTAMPTZ 설정 완료")
    except Exception as e:
        logger.error(f"❌ app_logs 테이블 생성/수정 실패: {e}")




def close_all_databases():
    """데이터베이스 연결 종료 (앱 종료 시 호출)"""
    global engine

    if engine:
        engine.dispose()
        logger.info("PostgreSQL SQLAlchemy 엔진 및 커넥션 풀 종료")
