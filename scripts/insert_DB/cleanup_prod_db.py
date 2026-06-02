import os
import re
import psycopg2

def parse_env_prod_url():
    _env_candidates = [
        r"D:\dev\lookalike-lightweight\.env",
        os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env")),
    ]
    prod_url = None
    for _env_path in _env_candidates:
        if os.path.isfile(_env_path):
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    _m_prod = re.match(r'^PROD_DATABASE_URL\s*=\s*(.+)$', _line)
                    if _m_prod:
                        prod_url = _m_prod.group(1).strip().strip('"').strip("'")
            break
    return prod_url

def main():
    prod_url = parse_env_prod_url()
    if not prod_url:
        print("❌ PROD_DATABASE_URL을 찾을 수 없습니다.")
        return

    print("🧹 PROD DB에서 불필요한 수집/로그 테이블 제거 시작...")
    try:
        conn = psycopg2.connect(prod_url)
        conn.autocommit = True
        cur = conn.cursor()

        tables_to_drop = [
            "staging_products",
            "staging_naver_prices",
            "pipeline_runs",
            "pipeline_errors"
        ]

        for table in tables_to_drop:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
            print(f"🗑️ PROD DB 테이블 {table} 삭제 완료")

        # brand_sequences는 PROD DB에서도 지워도 되는지 검토
        # crawlers_pipeline_cli 등에서 사용하지 않으므로 PROD에서는 Drop합니다.
        cur.execute("DROP TABLE IF EXISTS brand_sequences CASCADE;")
        print("🗑️ PROD DB 테이블 brand_sequences 삭제 완료")

        cur.close()
        conn.close()
        print("✅ PROD DB 클린업 완료!")
    except Exception as e:
        print(f"❌ PROD DB 클린업 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
