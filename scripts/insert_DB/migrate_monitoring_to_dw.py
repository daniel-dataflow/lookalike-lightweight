import os
import re
import psycopg2

def parse_env_urls():
    env_path = r"D:\dev\lookalike-lightweight\.env"
    prod_url = None
    dw_url = None
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m_prod = re.match(r'^PROD_DATABASE_URL\s*=\s*(.+)$', line)
                if m_prod:
                    prod_url = m_prod.group(1).strip().strip('"').strip("'")
                m_dw = re.match(r'^DW_DATABASE_URL\s*=\s*(.+)$', line)
                if m_dw:
                    dw_url = m_dw.group(1).strip().strip('"').strip("'")
    return prod_url, dw_url

def main():
    prod_url, dw_url = parse_env_urls()
    if not prod_url or not dw_url:
        print("[ERROR] DB URL not found")
        return

    # 1. PROD DB에서 테이블 제거
    print("[PROD DB] Cleaning up app_logs and infra_metrics...")
    try:
        prod_conn = psycopg2.connect(prod_url)
        prod_conn.autocommit = True
        prod_cur = prod_conn.cursor()
        
        prod_cur.execute("DROP TABLE IF EXISTS app_logs CASCADE;")
        print("[PROD DB] Drop app_logs success")
        
        prod_cur.execute("DROP TABLE IF EXISTS infra_metrics CASCADE;")
        print("[PROD DB] Drop infra_metrics success")
        
        prod_cur.close()
        prod_conn.close()
        print("[PROD DB] Monitoring tables cleanup complete.")
    except Exception as e:
        print(f"[ERROR] PROD DB cleanup failed: {e}")

    # 2. DW DB에 테이블 생성
    print("[DW DB] Creating app_logs and infra_metrics...")
    try:
        dw_conn = psycopg2.connect(dw_url)
        dw_conn.autocommit = True
        dw_cur = dw_conn.cursor()
        
        dw_cur.execute("SET TIME ZONE 'Asia/Seoul';")
        
        dw_cur.execute("""
            CREATE TABLE IF NOT EXISTS app_logs (
                id SERIAL PRIMARY KEY,
                level VARCHAR(20) NOT NULL,
                service VARCHAR(50) NOT NULL,
                message TEXT NOT NULL,
                error_type VARCHAR(100) DEFAULT 'unknown_error',
                timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("[DW DB] Create app_logs table success")
        
        dw_cur.execute("""
            CREATE TABLE IF NOT EXISTS infra_metrics (
                id SERIAL PRIMARY KEY,
                cpu_usage REAL NOT NULL,
                memory_usage REAL NOT NULL,
                timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("[DW DB] Create infra_metrics table success")
        
        dw_cur.close()
        dw_conn.close()
        print("[DW DB] Monitoring tables initialization complete.")
    except Exception as e:
        print(f"[ERROR] DW DB initialization failed: {e}")

if __name__ == "__main__":
    main()
