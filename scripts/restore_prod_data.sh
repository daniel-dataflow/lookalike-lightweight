#!/bin/bash

# 에러 발생 시 즉시 중단
set -e

ENV_FILE="./.env"
if [ ! -f "$ENV_FILE" ]; then
  # 만약 scripts/ 경로에서 실행되었다면 상위 디렉토리로 탐색
  ENV_FILE="../.env"
fi

if [ -f "$ENV_FILE" ]; then
  echo "🔑 .env 파일을 감지하여 환경변수를 파싱합니다..."
  while IFS= read -r line || [ -n "$line" ]; do
    # 주석 및 빈 줄 무시
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    
    # 키와 값 분리 (첫 번째 = 기준)
    if [[ "$line" == *"="* ]]; then
      key=$(echo "${line%%=*}" | xargs)
      val=$(echo "${line#*=}" | xargs | tr -d '"'\''')
      export "$key=$val"
    fi
  done < "$ENV_FILE"
  
  # eval로 변수 대입 처리
  LOCAL_DB_URL=$(eval echo "$DATABASE_URL")
  PROD_DB_URL=$(eval echo "$PROD_DATABASE_URL")
else
  echo "❌ .env 파일을 찾을 수 없습니다."
  exit 1
fi

# Neon Pooler 호스트의 경우 search_path 관련 제약으로 인해 unpooled 호스트로 우회 전환
# 예: -pooler.c-2 -> .c-2
if [[ "$PROD_DB_URL" == *"-pooler."* ]]; then
  echo "🔌 Neon Pooler 감지: unpooled 커넥션으로 전환합니다..."
  PROD_DB_URL="${PROD_DB_URL//-pooler./.}"
fi

# 출력 위치 정의 (supabase/migrations)
SQL_OUTPUT_DIR="./supabase/migrations"
if [ ! -d "$SQL_OUTPUT_DIR" ]; then
  SQL_OUTPUT_DIR="../supabase/migrations"
fi

SQL_FILE="${SQL_OUTPUT_DIR}/backup_restore.sql"

echo "📂 [1/3] 로컬 데이터웨어하우스(DW) DB로부터 원본 백업본 추출 중..."
# 대상 테이블: products, product_embeddings, naver_prices
# COPY 방식을 사용하여 업로드 속도를 극대화하기 위해 --column-inserts 옵션 배제
pg_dump --data-only \
  -d "$LOCAL_DB_URL" \
  -t products \
  -t product_embeddings \
  -t naver_prices \
  -f "$SQL_FILE"

echo "💾 [2/3] 백업 파일 생성 완료 ($SQL_FILE)"

echo "🔄 [3/3] 프로덕션 Neon DB로 원본 데이터 덮어쓰기(Restore) 진행 중..."
# 1. pgvector 확장 및 스키마 테이블 자동 구성 (기존 테이블 스키마 갱신을 위해 드롭 후 재생성)
echo "🛠️ 프로덕션 Neon DB의 스키마와 pgvector 확장을 설정합니다..."
psql -d "$PROD_DB_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -d "$PROD_DB_URL" -c "DROP TABLE IF EXISTS product_embeddings CASCADE; DROP TABLE IF EXISTS naver_prices CASCADE; DROP TABLE IF EXISTS products CASCADE;"

# migrations 경로 지정
MIGRATION_DIR="./supabase/migrations"
if [ ! -d "$MIGRATION_DIR" ]; then
  MIGRATION_DIR="../supabase/migrations"
fi

psql -d "$PROD_DB_URL" -f "${MIGRATION_DIR}/001_create_tables.sql"
psql -d "$PROD_DB_URL" -f "${MIGRATION_DIR}/002_admin_tables.sql"

# 2. 기존 대상 테이블들 비우기 후 이관 실행
psql -d "$PROD_DB_URL" -c "TRUNCATE TABLE product_embeddings, naver_prices CASCADE; TRUNCATE TABLE products CASCADE;"
psql -d "$PROD_DB_URL" -f "$SQL_FILE"

echo "✨ [완료] products, product_embeddings, naver_prices 데이터가 프로덕션에 무결하게 원상 복구되었습니다!"
