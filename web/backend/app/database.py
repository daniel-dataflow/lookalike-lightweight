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
prod_engine = None
ProdSessionLocal = None

# DW DB 글로벌 SQLAlchemy 엔진 및 세션 팩토리
dw_engine = None
DwSessionLocal = None


# ========================
# PostgreSQL (SQLAlchemy)
# ========================
def init_postgres():
    """PROD_DATABASE_URL 및 DW_DATABASE_URL 기반 SQLAlchemy 엔진 및 커넥션 풀 초기화"""
    global prod_engine, ProdSessionLocal, dw_engine, DwSessionLocal
    settings = get_settings()
    
    # 1. PROD DB 초기화
    try:
        db_url = settings.PROD_DATABASE_URL_ACTIVE or settings.DATABASE_URL
        if not db_url:
            raise ValueError("PROD_DATABASE_URL_ACTIVE가 설정되지 않았습니다.")

        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        connect_args = {}
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        hostname = parsed.hostname or ""
        is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
        if not is_local:
            connect_args["sslmode"] = "require"
            logger.info("🔒 원격 PROD 데이터베이스 연결 - sslmode=require 강제 적용")

        prod_engine = create_engine(
            db_url,
            pool_size=settings.POSTGRES_MIN_CONN,
            max_overflow=max(0, settings.POSTGRES_MAX_CONN - settings.POSTGRES_MIN_CONN),
            connect_args=connect_args,
            pool_pre_ping=True
        )
        ProdSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=prod_engine)
        logger.info("✅ PostgreSQL PROD DB SQLAlchemy 엔진 및 커넥션 풀 초기화 완료")
    except Exception as e:
        logger.error(f"❌ PostgreSQL PROD DB 연결 실패: {e}")
        prod_engine = None
        ProdSessionLocal = None

    # 2. DW DB 초기화
    try:
        dw_url = settings.DW_DATABASE_URL or settings.DATABASE_URL
        if not dw_url:
            raise ValueError("DW_DATABASE_URL이 설정되지 않았습니다.")

        if dw_url.startswith("postgres://"):
            dw_url = dw_url.replace("postgres://", "postgresql://", 1)

        connect_args = {}
        from urllib.parse import urlparse
        parsed = urlparse(dw_url)
        hostname = parsed.hostname or ""
        is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
        if not is_local:
            connect_args["sslmode"] = "require"
            logger.info("🔒 원격 DW 데이터베이스 연결 - sslmode=require 강제 적용")

        dw_engine = create_engine(
            dw_url,
            pool_size=settings.POSTGRES_MIN_CONN,
            max_overflow=max(0, settings.POSTGRES_MAX_CONN - settings.POSTGRES_MIN_CONN),
            connect_args=connect_args,
            pool_pre_ping=True
        )
        DwSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=dw_engine)
        logger.info("✅ PostgreSQL DW DB SQLAlchemy 엔진 및 커넥션 풀 초기화 완료")
    except Exception as e:
        logger.error(f"❌ PostgreSQL DW DB 연결 실패: {e}")
        dw_engine = None
        DwSessionLocal = None


@contextmanager
def get_prod_connection():
    """SQLAlchemy 커넥션 풀에서 raw connection을 획득하여 컨텍스트 매니저로 제공 (PROD DB)"""
    if prod_engine is None:
        raise ConnectionError("PROD DB SQLAlchemy 엔진이 초기화되지 않았습니다")
    conn = prod_engine.raw_connection()
    # 세션 타임존을 서울(KST)로 강제 설정
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul';")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_prod_cursor(dict_cursor=True):
    """PostgreSQL PROD DB 커서를 직접 제공하는 편의 함수"""
    with get_prod_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
        finally:
            cur.close()


@contextmanager
def get_dw_connection():
    """SQLAlchemy 커넥션 풀에서 raw connection을 획득하여 컨텍스트 매니저로 제공 (DW DB)"""
    if dw_engine is None:
        # Fallback to PROD engine if DW DB not separately configured
        if prod_engine is not None:
            conn = prod_engine.raw_connection()
        else:
            raise ConnectionError("DW DB 및 PROD DB SQLAlchemy 엔진이 모두 초기화되지 않았습니다")
    else:
        conn = dw_engine.raw_connection()
    # 세션 타임존을 서울(KST)로 강제 설정
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul';")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_dw_cursor(dict_cursor=True):
    """PostgreSQL DW DB 커서를 직접 제공하는 편의 함수"""
    with get_dw_connection() as conn:
        cursor_factory = RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
        finally:
            cur.close()


# 하위 호환성을 위해 get_pg_connection 및 get_pg_cursor 및 engine을 별칭으로 제공합니다.
get_pg_connection = get_prod_connection
get_pg_cursor = get_prod_cursor

# engine 및 SessionLocal 하위 호환성 (main.py, auto_recovery.py 등 레거시 대응)
def get_engine():
    return prod_engine

# 전역 변수로 바로 참조할 수 있도록 설정 (init_postgres 호출 전에는 None일 수 있으므로 동적 조회 래퍼 활용 권장)
engine = None

SessionLocal = ProdSessionLocal


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
    _ensure_owner_ips_table()
    _migrate_users_table()
    logger.info("🚀 PostgreSQL 데이터베이스 연결 초기화 완료")


def _migrate_users_table():
    """users 테이블에 어드민 세부 권한, 임시 비밀번호 설정, 보안 질문/답변 필드 추가 및 초기 마이그레이션"""
    try:
        import bcrypt
        with get_prod_cursor() as cur:
            # 컬럼 추가 DDL
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_permission VARCHAR(50) DEFAULT 'SUPER_ADMIN';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_temp_password BOOLEAN DEFAULT FALSE;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS security_question VARCHAR(200) DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS security_answer VARCHAR(200) DEFAULT '';")
            
            # 1. admin_30d7 임시 테스트 계정 삭제
            cur.execute("DELETE FROM users WHERE user_id = 'admin_30d7';")
            
            # 2. admin 슈퍼 어드민 계정 초기화 (admin / admin7777! 의 bcrypt 해시)
            hashed = bcrypt.hashpw(b"admin7777!", bcrypt.gensalt()).decode("utf-8")
            cur.execute("""
                INSERT INTO users (user_id, password, user_name, email, role, provider, admin_permission)
                VALUES ('admin', %s, '시스템 관리자', 'admin@lookalike.com', 'ADMIN', 'system', 'SUPER_ADMIN')
                ON CONFLICT (user_id) DO UPDATE
                SET password = EXCLUDED.password, role = 'ADMIN', admin_permission = 'SUPER_ADMIN';
            """, (hashed,))
            
        logger.info("✅ users 테이블 마이그레이션 및 admin 초기 계정 생성 완료")
    except Exception as e:
        logger.error(f"❌ users 테이블 마이그레이션 실패: {e}")



def _ensure_owner_ips_table():
    """owner_ips (관리자 IP 등록) 테이블이 없으면 자동 생성"""
    try:
        with get_prod_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS owner_ips (
                    ip_address VARCHAR(45) PRIMARY KEY,
                    memo VARCHAR(200) DEFAULT '',
                    create_dt TIMESTAMP DEFAULT NOW()
                );
            """)
        logger.info("✅ owner_ips 테이블 확인/생성 완료")
    except Exception as e:
        logger.error(f"❌ owner_ips 테이블 생성 실패: {e}")


def _ensure_infra_metrics_table():
    """infra_metrics 링 버퍼 테이블이 없으면 자동 생성 (초경량 Ring Buffer)"""
    try:
        with get_dw_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS infra_metrics (
                    id        SERIAL PRIMARY KEY,
                    cpu_usage REAL,
                    memory_usage REAL,
                    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
        logger.info("✅ infra_metrics 테이블 확인/생성 완료")
    except Exception as e:
        logger.error(f"❌ infra_metrics 테이블 생성 실패: {e}")


def _ensure_app_logs_table():
    """app_logs 링 버퍼 테이블이 없으면 자동 생성 (초경량 Log Ring Buffer)"""
    try:
        with get_dw_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_logs (
                    id SERIAL PRIMARY KEY,
                    level VARCHAR(20),
                    service VARCHAR(50) DEFAULT 'FastAPI',
                    message TEXT,
                    error_type VARCHAR(100),
                    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
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
    global prod_engine, dw_engine
    
    if prod_engine:
        prod_engine.dispose()
        logger.info("PostgreSQL PROD DB SQLAlchemy 엔진 및 커넥션 풀 종료")
        
    if dw_engine:
        dw_engine.dispose()
        logger.info("PostgreSQL DW DB SQLAlchemy 엔진 및 커넥션 풀 종료")
