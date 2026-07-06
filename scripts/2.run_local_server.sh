#!/bin/bash
# ==============================================================================
# Lookalike - Local Server Execution Script (ml-env)
# ==============================================================================
# 이 스크립트는 로컬에 설치된 miniconda3/envs/ml-env 환경의 Python을 사용하여
# 종속 라이브러리를 설치하고 FastAPI 백엔드 서버를 구동합니다.
# ==============================================================================

set -e

WORKSPACE_DIR="/Users/daniel/GitHub/personal/lookalike-lightweight"
PYTHON_BIN="$WORKSPACE_DIR/miniconda3/envs/ml-env/bin/python"

# 1. 가상환경 존재 여부 검사
if [ ! -f "$PYTHON_BIN" ]; then
    echo "❌  가상환경 Python을 찾을 수 없습니다: $PYTHON_BIN"
    echo "먼저 'bash scripts/install_miniconda.sh' 스크립트를 실행해 주세요."
    exit 1
fi

# 2. 패키지 설치
echo "📦  의존성 패키지 설치 중..."
"$PYTHON_BIN" -m pip install -r "$WORKSPACE_DIR/web/backend/requirements.txt"

# 3. uvicorn 서버 실행
echo "🚀  FastAPI 백엔드 서버 구동 중..."
cd "$WORKSPACE_DIR/web/backend"
"$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port 8900 --reload
