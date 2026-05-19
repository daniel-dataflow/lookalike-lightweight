#!/bin/bash
# ============================================================
# Main Project Lookalike - 로컬 실행 스크립트 (Hybrid Architecture)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=========================================="
echo "🚀 Lookalike 하이브리드 아키텍처 로컬 서버 시작"
echo "=========================================="
echo "DB(PostgreSQL+pgvector) 및 FastAPI만 부팅합니다."

cd "${PROJECT_ROOT}"

# 기존 컨테이너가 꼬이지 않도록 먼저 stop
bash scripts/stop_all.sh

echo "[1/2] 도커 컴포즈 빌드 및 실행 (docker compose up --build -d)..."
docker compose up --build -d

if [ $? -ne 0 ]; then
    echo "❌ Docker Compose 실행 중 오류가 발생했습니다."
    exit 1
fi

echo "[2/2] 컨테이너 부팅 대기 (FastAPI 헬스체크 대기)..."
# 간단한 대기 스크립트
max_retries=30
count=0
while [ $count -lt $max_retries ]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' fastapi-main 2>/dev/null)
    if [ "$status" == "healthy" ]; then
        echo "✅ FastAPI 서버가 정상적으로 시작되었습니다!"
        break
    fi
    echo -n "."
    sleep 2
    count=$((count + 1))
done

if [ $count -ge $max_retries ]; then
    echo -e "\n⚠️ FastAPI 시작 확인 시간 초과. (컨테이너 상태를 확인하세요: docker logs fastapi-main)"
else
    echo -e "\n🎉 전체 시작 완료"
    echo "  - FastAPI 주소: http://localhost:8900"
    echo "  - API 문서:     http://localhost:8900/docs"
    echo "  - DB 접속정보:  localhost:5432 (datauser/DataPass2026!)"
    echo "=========================================="
    echo "로그를 실시간으로 보려면 다음 명령어를 사용하세요:"
    echo "  docker logs -f fastapi-main"
fi