#!/bin/bash
# ==============================================================================
# Miniconda Local Installation & ml-env Setup Script
# ==============================================================================
# 이 스크립트는 lookalike-lightweight 폴더 내에 Miniconda를 설치하고
# python 3.11 기반의 ml-env 가상환경을 구성합니다.
# ==============================================================================

set -e

# 1. 경로 설정
WORKSPACE_DIR="/Users/daniel/GitHub/personal/lookalike-lightweight"
INSTALL_DIR="$WORKSPACE_DIR/miniconda3"
INSTALLER_PATH="$WORKSPACE_DIR/miniconda.sh"

# 기존에 miniconda3가 있으면 건너뜁니다
if [ -d "$INSTALL_DIR" ]; then
    echo "⚠️  $INSTALL_DIR 디렉토리가 이미 존재합니다. 콘다 환경 구성을 진행합니다."
else
    # 2. 아키텍처 감지 및 URL 결정
    ARCH=$(uname -m)
    echo "⚙️  감지된 아키텍처: $ARCH"
    if [ "$ARCH" = "arm64" ]; then
        CONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh"
    else
        CONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh"
    fi

    # 3. installer 다운로드
    echo "⬇️  Miniconda 다운로드 중... ($CONDA_URL)"
    curl -L -o "$INSTALLER_PATH" "$CONDA_URL"

    # 4. Miniconda 설치 (배치 모드)
    echo "💾  Miniconda 설치 중 ($INSTALL_DIR)..."
    bash "$INSTALLER_PATH" -b -p "$INSTALL_DIR" -u

    # 5. installer 제거
    rm -f "$INSTALLER_PATH"
    echo "✅  Miniconda 설치 완료."
fi

# 6. 약관 동의 (Terms of Service) 자동 승인
echo "📝  Anaconda 서비스 약관(ToS) 동의 중..."
"$INSTALL_DIR/bin/conda" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main || true
"$INSTALL_DIR/bin/conda" tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r || true

# 7. ml-env 콘다 환경 생성
echo "🌀  ml-env 가상환경 생성 중 (python=3.11)..."
"$INSTALL_DIR/bin/conda" create -y -n ml-env python=3.11

echo "=============================================================================="
echo "🎉  설정이 완료되었습니다!"
echo "- Miniconda 경로: $INSTALL_DIR"
echo "- 가상환경 실행 경로: $INSTALL_DIR/envs/ml-env/bin/python"
echo "=============================================================================="
