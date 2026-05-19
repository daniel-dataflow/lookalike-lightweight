#!/usr/bin/env python
"""
init_db.py - Neon DB(또는 설정된 DATABASE_URL)에 테이블 스키마 및 인덱스를 생성하는 스크립트
"""
import os
import sys
import logging
from pathlib import Path
from urllib.parse import urlparse
import psycopg2
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 설정 및 환경 로드
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# 마이그레이션 파일 경로
MIGRATIONS_DIR = BASE_DIR / "supabase" / "migrations"
MIGRATION_FILES = [
    "001_create_tables.sql",
    "002_admin_tables.sql",
]


def get_db_connection(db_url: str):
    """DATABASE_URL을 기반으로 psycopg2 커넥션 생성 (SSL 대응)"""
    connect_args = {}
    parsed = urlparse(db_url)
    hostname = parsed.hostname or ""
    is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
    
    if not is_local:
        connect_args["sslmode"] = "require"
        logger.info("🔒 원격 데이터베이스 연결 - sslmode=require 적용")
        
    return psycopg2.connect(db_url, **connect_args)


def run_ddl_file(cur, filepath: Path):
    """DDL SQL 파일을 읽어 데이터베이스에 실행"""
    logger.info(f"📄 SQL 파일 실행 중: {filepath.name}")
    with open(filepath, "r", encoding="utf-8") as f:
        sql_content = f.read()
        
    # SQL 실행
    cur.execute(sql_content)
    logger.info(f"✅ SQL 파일 실행 완료: {filepath.name}")


def main():
    # .env 파일 또는 시스템 환경 변수에서 DATABASE_URL 읽기
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("❌ DATABASE_URL 환경 변수가 .env 또는 시스템에 설정되지 않았습니다.")
        sys.exit(1)
        
    logger.info(f"🚀 DB 초기화 시작 (대상 호스트: {urlparse(db_url).hostname})")
    
    conn = None
    try:
        conn = get_db_connection(db_url)
        conn.autocommit = True
        
        with conn.cursor() as cur:
            # 1. pgvector 확장 활성화 확인/시도
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                logger.info("✅ pgvector 익스텐션 활성화 완료")
            except Exception as ve:
                logger.warning(f"⚠️ pgvector 익스텐션 활성화 중 경고 (이미 설치되었거나 권한 부족일 수 있음): {ve}")
            
            # 2. 마이그레이션 파일 순차 실행
            for filename in MIGRATION_FILES:
                filepath = MIGRATIONS_DIR / filename
                if not filepath.exists():
                    logger.error(f"❌ 마이그레이션 SQL 파일을 찾을 수 없습니다: {filepath}")
                    sys.exit(1)
                run_ddl_file(cur, filepath)
                
        logger.info("🎉 모든 테이블 스키마 및 인덱스 초기화가 성공적으로 완료되었습니다!")
        
    except Exception as e:
        logger.error(f"❌ DB DDL 초기화 오류 발생: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
