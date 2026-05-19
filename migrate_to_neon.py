#!/usr/bin/env python
"""
migrate_to_neon.py - 로컬 PostgreSQL에서 Neon DB로 데이터를 대량 마이그레이션하는 스크립트
"""
import os
import sys
import argparse
import logging
from pathlib import Path
from urllib.parse import urlparse
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from tqdm import tqdm

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

# 마이그레이션 대상 테이블 (순서 보장: 부모 테이블이 자식 테이블보다 먼저 위치해야 함)
TABLES = [
    ("users", "user_id"),
    ("products", "product_id"),
    ("product_features", "product_id"),
    ("naver_prices", "nprice_id"),
    ("product_embeddings", "product_id"),
    ("recent_views", "id"),
    ("likes", "id"),
    ("inquiry_board", "inquiry_board_id"),
    ("comments", "comment_id"),
    ("user_sessions", "token"),
]

# Serial / Bigserial 시퀀스 매핑 (마이그레이션 완료 후 시퀀스 최신값 갱신용)
SEQUENCES = {
    "naver_prices": ("nprice_id", "naver_prices_nprice_id_seq"),
    "recent_views": ("id", "recent_views_id_seq"),
    "likes": ("id", "likes_id_seq"),
    "inquiry_board": ("inquiry_board_id", "inquiry_board_inquiry_board_id_seq"),
    "comments": ("comment_id", "comments_comment_id_seq"),
}


def get_db_connection(db_url: str):
    """DATABASE_URL을 기반으로 psycopg2 커넥션 생성 (SSL 대응)"""
    connect_args = {}
    parsed = urlparse(db_url)
    hostname = parsed.hostname or ""
    is_local = any(host in hostname for host in ["localhost", "127.0.0.1", "db", "postgres"])
    
    if not is_local:
        connect_args["sslmode"] = "require"
        logger.info(f"🔒 원격 DB 연결 ({hostname}) - sslmode=require 적용")
    else:
        logger.info(f"🔌 로컬 DB 연결 ({hostname})")
        
    return psycopg2.connect(db_url, **connect_args)


def clear_destination_tables(cur, tables):
    """자식 테이블부터 부모 테이블 순서로 대상 테이블들의 기존 데이터 비우기 (TRUNCATE)"""
    logger.info("🧹 대상 테이블 데이터 비우기 시작 (TRUNCATE)...")
    # FK 제약조건 순서를 고려하여 역순으로 비우기 수행
    for table_name, _ in reversed(tables):
        try:
            logger.info(f"  Truncating: {table_name}")
            cur.execute(f"TRUNCATE TABLE {table_name} CASCADE;")
        except Exception as e:
            logger.warning(f"  ⚠️ {table_name} 비우기 중 에러 (테이블이 없을 수 있음): {e}")


def main():
    parser = argparse.ArgumentParser(description="로컬 PostgreSQL 데이터를 Neon DB로 마이그레이션")
    parser.add_argument("--batch-size", type=int, default=500, help="한 번에 Neon DB에 삽입할 배치 크기 (기본값: 500)")
    parser.add_argument("--clear", action="store_true", help="마이그레이션 전에 대상 테이블 데이터를 비움 (TRUNCATE)")
    parser.add_argument("--dry-run", action="store_true", help="실제 마이그레이션을 진행하지 않고 건수만 확인")
    args = parser.parse_args()

    # 1. 환경 변수 확인
    local_user = os.getenv("POSTGRES_USER", "datauser")
    local_pass = os.getenv("POSTGRES_PASSWORD", "DataPass2026!")
    local_db = os.getenv("POSTGRES_DB", "datadb")
    local_host = os.getenv("POSTGRES_HOST", "postgresql")
    
    local_db_url = f"postgresql://{local_user}:{local_pass}@{local_host}:5432/{local_db}"
    neon_db_url = os.getenv("DATABASE_URL")
    
    if not neon_db_url:
        logger.error("❌ DATABASE_URL 환경 변수가 설정되지 않았습니다.")
        sys.exit(1)
        
    logger.info("=" * 60)
    logger.info("📦 PostgreSQL -> Neon DB 데이터 마이그레이션 시작")
    logger.info(f"  소스 DB(로컬): {local_db_url.replace(local_pass, '****')}")
    logger.info(f"  대상 DB(Neon): {neon_db_url.split('@')[-1] if '@' in neon_db_url else neon_db_url}")
    logger.info(f"  배치 크기: {args.batch_size}")
    logger.info(f"  대상 초기화(clear): {args.clear}")
    logger.info(f"  시뮬레이션(dry-run): {args.dry_run}")
    logger.info("=" * 60)

    try:
        # 2. 커넥션 연결
        conn_src = get_db_connection(local_db_url)
        conn_dst = get_db_connection(neon_db_url)
        
        conn_src.autocommit = False
        conn_dst.autocommit = False
        
        cur_src = conn_src.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur_dst = conn_dst.cursor()
        
        # 3. 대상 테이블 초기화 (Truncate)
        if args.clear and not args.dry_run:
            clear_destination_tables(cur_dst, TABLES)
            conn_dst.commit()
            logger.info("🧹 대상 테이블 초기화 완료")
            
        # 4. 각 테이블 데이터 순차 마이그레이션
        for table_name, pk in TABLES:
            logger.info(f"--- 🏷️ 테이블 작업 시작: {table_name} ---")
            
            # 소스 테이블 컬럼 정보 및 데이터 읽기
            try:
                cur_src.execute(f"SELECT * FROM {table_name}")
                columns = [desc[0] for desc in cur_src.description]
                rows = cur_src.fetchall()
            except Exception as se:
                logger.error(f"  ❌ 소스 DB {table_name} 조회 실패: {se}")
                conn_src.rollback()
                continue
                
            total_rows = len(rows)
            logger.info(f"  📊 마이그레이션할 레코드 수: {total_rows:,}개")
            
            if total_rows == 0:
                logger.info(f"  ⏭️ 데이터가 없어 {table_name} 테이블 스킵")
                continue
                
            if args.dry_run:
                logger.info(f"  [DRY-RUN] {table_name} 테이블에서 {total_rows:,}개 레코드 삽입 예정")
                continue
                
            # 대상 DB에 삽입 쿼리 빌드
            col_str = ", ".join(columns)
            # execute_values를 사용하므로 VALUES 뒤에 플레이스홀더 %s 하나만 기입
            insert_query = f"INSERT INTO {table_name} ({col_str}) VALUES %s ON CONFLICT ({pk}) DO NOTHING"
            
            # composite unique key가 지정된 테이블 대응
            if table_name in ["recent_views", "likes"]:
                insert_query = f"INSERT INTO {table_name} ({col_str}) VALUES %s ON CONFLICT (user_id, product_id) DO NOTHING"
                
            # 배치 단위 대량 삽입
            inserted_count = 0
            with tqdm(total=total_rows, desc=f"  🚀 {table_name} 적재", unit="row", colour="green") as pbar:
                for i in range(0, total_rows, args.batch_size):
                    batch = rows[i : i + args.batch_size]
                    
                    # DictRow를 일반 tuple 또는 list로 변환하여 execute_values가 파싱 가능하게 함
                    batch_tuples = [tuple(row) for row in batch]
                    
                    try:
                        psycopg2.extras.execute_values(cur_dst, insert_query, batch_tuples)
                        conn_dst.commit()
                        inserted_count += len(batch)
                        pbar.update(len(batch))
                    except Exception as ie:
                        conn_dst.rollback()
                        logger.error(f"\n  ❌ {table_name} 배치 삽입 중 오류 발생 (인덱스 {i}~{i+len(batch)}): {ie}")
                        # 에러 발생 시 전체 중단
                        sys.exit(1)
                        
            logger.info(f"  ✅ {table_name} 적재 완료 (성공: {inserted_count:,}/{total_rows:,})")
            
            # 5. 시퀀스 갱신 (Serial/Bigserial)
            if table_name in SEQUENCES:
                pk_col, seq_name = SEQUENCES[table_name]
                try:
                    cur_dst.execute(f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({pk_col}) FROM {table_name}), 1));")
                    conn_dst.commit()
                    logger.info(f"  🔄 시퀀스 최신화 완료: {seq_name}")
                except Exception as seq_err:
                    conn_dst.rollback()
                    logger.warning(f"  ⚠️ 시퀀스 갱신 경고 ({seq_name}): {seq_err}")
                    
        logger.info("🎉 모든 데이터 마이그레이션이 성공적으로 완료되었습니다!")
        
    except Exception as e:
        logger.error(f"❌ 마이그레이션 전체 작업 에러: {e}")
        sys.exit(1)
    finally:
        if 'conn_src' in locals() and conn_src:
            conn_src.close()
        if 'conn_dst' in locals() and conn_dst:
            conn_dst.close()


if __name__ == "__main__":
    main()
