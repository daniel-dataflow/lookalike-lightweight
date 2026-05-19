#!/bin/bash

echo "=========================================="
echo "Main Project Lookalike - 로컬 서비스 중지"
echo "=========================================="

echo ""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

echo "1. 모든 로컬 컨테이너 및 리소스 정리 (docker compose down)..."
docker compose down

echo ""
echo "=========================================="
echo "모든 로컬 서비스 중지 완료!"
echo "=========================================="
