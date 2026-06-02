#!/usr/bin/env python
"""
init_dev_db.py - DEV_DATABASE_URL에 필수 테이블(products, naver_prices, product_embeddings 등)을 생성하는 초기화 스크립트
"""
import os
import sys
import psycopg2
from urllib.parse import urlparse
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = BASE_DIR / "supabase" / "migrations"
MIGRATION_FILES = [
    "001_create_tables.sql",
    "002_admin_tables.sql",
]

def get_db_connection(db_url: str):
    connect_args = {}
    parsed = urlparse(db_url)
    hostname = parsed.hostname or ""
    is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
    
    if not is_local:
        connect_args["sslmode"] = "require"
        print("[INFO] Remote DB connection - sslmode=require applied")
        
    return psycopg2.connect(db_url, **connect_args)

def run_ddl_file(cur, filepath: Path):
    print(f"[INFO] Running SQL file: {filepath.name}")
    with open(filepath, "r", encoding="utf-8") as f:
        sql_content = f.read()
    cur.execute(sql_content)
    print(f"[SUCCESS] SQL file executed: {filepath.name}")

def main():
    # .env 파일 로딩을 위해 직접 파싱 수행
    db_url = None
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEV_DATABASE_URL"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        db_url = parts[1].strip().strip('"').strip("'")
                        break

    if not db_url:
        db_url = os.getenv("DEV_DATABASE_URL")
        
    if not db_url:
        print("[ERROR] DEV_DATABASE_URL 환경 변수가 .env 또는 시스템에 설정되지 않았습니다.")
        sys.exit(1)
        
    print(f"[INFO] DEV DB init start (Target Host: {urlparse(db_url).hostname})")
    
    conn = None
    try:
        conn = get_db_connection(db_url)
        conn.autocommit = True
        
        with conn.cursor() as cur:
            # pgvector 확장 활성화 확인/시도
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                print("[SUCCESS] pgvector extension activated")
            except Exception as ve:
                print(f"[WARN] pgvector activation warning: {ve}")
            
            # 마이그레이션 파일 순차 실행
            for filename in MIGRATION_FILES:
                filepath = MIGRATIONS_DIR / filename
                if not filepath.exists():
                    print(f"[ERROR] Migration SQL file not found: {filepath}")
                    sys.exit(1)
                run_ddl_file(cur, filepath)
                
        print("[SUCCESS] DEV_DATABASE_URL DB tables and indexes initialized successfully!")
        
    except Exception as e:
        print(f"❌ DB DDL 초기화 오류 발생: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()
