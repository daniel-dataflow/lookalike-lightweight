#!/usr/bin/env bash
#
# neon_db_replicate.sh
# ---------------------------------------------------------
# 하나의 Neon Postgres DB를 다른 Neon Postgres DB로 복제하는 스크립트
#
# 사용법:
#   bash neon_db_replicate.sh
#   - 세션 설정 에러 안나오게 하고 싶으면
#   PG_BIN_DIR=/usr/lib/postgresql/16/bin bash neon_db_replicate.sh 
#   (또는 환경변수를 미리 export 해두고 실행해도 됩니다)
#
# 필요한 것:
#   - pg_dump, pg_restore (postgresql-client 패키지)
#     설치 예: sudo apt-get install postgresql-client
#   - 소스/타겟 Neon DB의 연결 문자열 (Connection string)
#     Neon 콘솔 > Project > Connection Details 에서 확인 가능
#     형식: postgres://USER:PASSWORD@HOST/DBNAME?sslmode=require
#
# 동작 방식:
#   1. 소스 DB를 custom format(-Fc)으로 덤프
#   2. 타겟 DB를 pg_restore로 복원 (--clean --if-exists 로 기존 객체 정리 후 복원)
# ---------------------------------------------------------

set -euo pipefail

# ===================== 설정 =====================
# .env 파일에서 DB 연결 정보를 불러옵니다.
# .env 파일 위치는 ENV_FILE 환경변수로 바꿀 수 있습니다. (기본값: 스크립트와 같은 경로의 .env)
#
# .env 파일 예시:
#   SOURCE_DB_URL=postgres://user:pass@ep-xxx.neon.tech/dbname?sslmode=require
#   TARGET_DB_URL=postgres://user:pass@ep-yyy.neon.tech/dbname?sslmode=require

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ENV_FILE이 지정되지 않았다면, 스크립트 위치에서 상위 폴더로 올라가며 .env를 자동으로 탐색합니다.
# (예: scripts/move_data/neon_db_replicate.sh 에서 실행해도 프로젝트 루트의 .env를 찾습니다)
if [[ -z "${ENV_FILE:-}" ]]; then
  SEARCH_DIR="$SCRIPT_DIR"
  ENV_FILE=""
  for _ in 1 2 3 4 5; do
    if [[ -f "$SEARCH_DIR/.env" ]]; then
      ENV_FILE="$SEARCH_DIR/.env"
      break
    fi
    PARENT_DIR="$(dirname "$SEARCH_DIR")"
    [[ "$PARENT_DIR" == "$SEARCH_DIR" ]] && break
    SEARCH_DIR="$PARENT_DIR"
  done
fi

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  echo "환경변수 파일 로드: $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "경고: '$ENV_FILE' 파일을 찾을 수 없습니다. 환경변수 또는 직접 입력으로 진행합니다."
fi

SOURCE_DB_URL="${SOURCE_DB_URL:-}"
TARGET_DB_URL="${TARGET_DB_URL:-}"

# 덤프를 저장할 임시 디렉토리 (병렬 작업(--jobs)은 directory 포맷에서만 지원됩니다)
DUMP_DIR="${DUMP_DIR:-./neon_dump_$(date +%Y%m%d_%H%M%S)}"

# 병렬 작업 수 (덤프/복원 속도 향상, 데이터 양이 많을 때 유용)
JOBS="${JOBS:-4}"

# 기존 타겟 DB의 데이터/스키마를 삭제하고 새로 덮어쓸지 여부 (true/false)
CLEAN_TARGET="${CLEAN_TARGET:-true}"

# pg_dump/pg_restore 바이너리가 있는 디렉토리 (버전이 여러 개 설치된 경우 명시적으로 지정)
# 예: PG_BIN_DIR=/usr/lib/postgresql/16/bin bash neon_db_replicate.sh
if [[ -n "${PG_BIN_DIR:-}" ]]; then
  PG_DUMP_BIN="$PG_BIN_DIR/pg_dump"
  PG_RESTORE_BIN="$PG_BIN_DIR/pg_restore"
  PSQL_BIN="$PG_BIN_DIR/psql"
else
  PG_DUMP_BIN="pg_dump"
  PG_RESTORE_BIN="pg_restore"
  PSQL_BIN="psql"
fi

# ===================== 입력값 확인 =====================
if [[ -z "$SOURCE_DB_URL" ]]; then
  read -rp "소스(원본) Neon DB Connection String을 입력하세요: " SOURCE_DB_URL
fi

if [[ -z "$TARGET_DB_URL" ]]; then
  read -rp "타겟(대상) Neon DB Connection String을 입력하세요: " TARGET_DB_URL
fi

if [[ -z "$SOURCE_DB_URL" || -z "$TARGET_DB_URL" ]]; then
  echo "오류: 소스/타겟 DB URL이 모두 필요합니다." >&2
  exit 1
fi

# ===================== 사전 점검 =====================
for cmd in "$PG_DUMP_BIN" "$PG_RESTORE_BIN"; do
  if ! command -v "$cmd" &> /dev/null; then
    echo "오류: '$cmd' 명령을 찾을 수 없습니다. postgresql-client를 설치하거나 PG_BIN_DIR을 올바르게 지정해주세요." >&2
    exit 1
  fi
done

# 클라이언트(pg_dump) 버전과 소스 DB 서버 버전이 다르면 경고
# (예: 클라이언트가 17+인데 서버가 16이면 transaction_timeout 등 신규 파라미터 관련 에러 발생 가능)
CLIENT_MAJOR_VERSION="$("$PG_DUMP_BIN" --version | grep -oE '[0-9]+' | head -1)"
SERVER_VERSION_STRING="$("$PSQL_BIN" "$SOURCE_DB_URL" -tAc "SHOW server_version;" 2>/dev/null || echo "")"
SERVER_MAJOR_VERSION="$(echo "$SERVER_VERSION_STRING" | grep -oE '^[0-9]+')"

if [[ -n "$SERVER_MAJOR_VERSION" && "$CLIENT_MAJOR_VERSION" != "$SERVER_MAJOR_VERSION" ]]; then
  echo "----------------------------------------------------"
  echo "경고: pg_dump 클라이언트 버전($CLIENT_MAJOR_VERSION)과 소스 DB 서버 버전($SERVER_MAJOR_VERSION)이 다릅니다."
  echo "복원 중 'unrecognized configuration parameter' 같은 에러가 날 수 있습니다."
  echo "PG_BIN_DIR 환경변수로 서버와 동일한 major 버전의 바이너리 경로를 지정하세요."
  echo "  예: PG_BIN_DIR=/usr/lib/postgresql/$SERVER_MAJOR_VERSION/bin bash $0"
  echo "  (설치가 안 되어 있다면: sudo apt-get install postgresql-client-$SERVER_MAJOR_VERSION)"
  echo "----------------------------------------------------"
fi

echo "======================================================"
echo " Neon DB 복제 시작"
echo "======================================================"
echo "덤프 경로     : $DUMP_DIR"
echo "병렬 작업 수  : $JOBS"
echo "타겟 초기화   : $CLEAN_TARGET"
echo "======================================================"

# ===================== 1. 소스 DB 덤프 =====================
echo ""
echo "[1/2] 소스 DB 덤프 중..."
"$PG_DUMP_BIN" \
  --dbname="$SOURCE_DB_URL" \
  --format=directory \
  --no-owner \
  --no-privileges \
  --jobs="$JOBS" \
  --file="$DUMP_DIR"

echo "덤프 완료: $(du -sh "$DUMP_DIR" | cut -f1)"

# ===================== 2. 타겟 DB로 복원 =====================
echo ""
echo "[2/2] 타겟 DB로 복원 중..."

RESTORE_OPTS=(
  --dbname="$TARGET_DB_URL"
  --no-owner
  --no-privileges
  --jobs="$JOBS"
)

if [[ "$CLEAN_TARGET" == "true" ]]; then
  RESTORE_OPTS+=(--clean --if-exists)
fi

"$PG_RESTORE_BIN" "${RESTORE_OPTS[@]}" "$DUMP_DIR"

echo ""
echo "======================================================"
echo " 복제 완료!"
echo "======================================================"
echo "덤프 폴더는 '$DUMP_DIR' 경로에 남아있습니다."
echo "필요 없으면 삭제하세요: rm -rf \"$DUMP_DIR\""