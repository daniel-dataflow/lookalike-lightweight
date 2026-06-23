import os
import re
import psycopg2

def parse_env_dw_url():
    """APP_ENV에 따라 DEV_DW_DATABASE_URL 또는 PROD_DW_DATABASE_URL을 .env에서 읽어 반환"""
    _env_candidates = [
        r"D:\dev\lookalike-lightweight\.env",
        os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")),
    ]
    dw_url = None
    env_mode = "local"
    for _env_path in _env_candidates:
        if os.path.isfile(_env_path):
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    _m_mode = re.match(r'^APP_ENV\s*=\s*(.+)$', _line)
                    if _m_mode:
                        env_mode = _m_mode.group(1).strip().strip('"').strip("'").lower()
                    # DEV 환경
                    _m_dev_dw = re.match(r'^DEV_DW_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_dev_dw and env_mode in ["local", "dev"]:
                        dw_url = _m_dev_dw.group(1).strip().strip('"').strip("'")
                    # PROD 환경
                    _m_prod_dw = re.match(r'^PROD_DW_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_prod_dw and env_mode in ["prod", "production"]:
                        dw_url = _m_prod_dw.group(1).strip().strip('"').strip("'")
            break
    print(f"[APP_ENV={env_mode}] -> {'DEV' if env_mode in ['local', 'dev'] else 'PROD'}_DW_DB 연결")
    return dw_url

def main():
    dw_url = parse_env_dw_url()
    if not dw_url:
        print("❌ DW_DATABASE_URL을 .env에서 찾을 수 없습니다.")
        return

    print("📡 DW DB 연결 및 필수 테이블 생성 시작 (KST 표준 TIMESTAMPTZ 적용)...")
    try:
        conn = psycopg2.connect(dw_url)
        conn.autocommit = False
        cur = conn.cursor()

        # 세션 타임존을 서울로 강제 설정
        cur.execute("SET TIME ZONE 'Asia/Seoul';")

        # 1. pipeline_runs 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id BIGSERIAL PRIMARY KEY,
                pipeline_name VARCHAR(100),
                brand VARCHAR(50),
                status VARCHAR(20),
                total_items INTEGER DEFAULT 0,
                new_items INTEGER DEFAULT 0,
                updated_items INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                github_run_id VARCHAR(100),
                started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ,
                duration_sec INTEGER,
                metadata JSONB DEFAULT '{}'::jsonb
            );
        """)
        
        # 2. pipeline_errors 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_errors (
                error_id BIGSERIAL PRIMARY KEY,
                run_id BIGINT,
                error_type VARCHAR(100),
                error_message TEXT,
                stack_trace TEXT,
                product_id VARCHAR(50),
                source_url VARCHAR(512),
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. staging_products 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staging_products (
                product_id VARCHAR(20) PRIMARY KEY,
                model_code VARCHAR(50),
                brand_name VARCHAR(50),
                prod_name VARCHAR(512),
                base_price INTEGER,
                gender VARCHAR(10),
                category_code VARCHAR(50),
                img_url VARCHAR(512),
                origin_url VARCHAR(512),
                create_dt TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                update_dt TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 4. staging_naver_prices 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staging_naver_prices (
                nprice_id BIGSERIAL PRIMARY KEY,
                product_id VARCHAR(20),
                brand VARCHAR(100),
                model_code VARCHAR(100),
                original_name VARCHAR(255),
                original_price INTEGER,
                rank SMALLINT,
                naver_title VARCHAR(255),
                naver_price INTEGER,
                mall_name VARCHAR(100),
                mall_url VARCHAR(512),
                image_url VARCHAR(512),
                similarity_score NUMERIC(5, 2),
                create_dt TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                update_dt TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 5. brand_sequences 테이블
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brand_sequences (
                brand_name VARCHAR(50) PRIMARY KEY,
                last_seq INTEGER DEFAULT 0
            );
        """)

        # 인덱스 생성
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_staging_naver_prices_product_id 
                ON staging_naver_prices(product_id);
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at 
                ON pipeline_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pipeline_errors_created_at 
                ON pipeline_errors(created_at DESC);
        """)

        conn.commit()
        print("✅ DW DB 필수 테이블 (pipeline_runs, pipeline_errors, staging_products, staging_naver_prices, brand_sequences) 생성 완료!")

        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ DW DB 테이블 생성 중 에러 발생: {e}")

if __name__ == "__main__":
    main()
