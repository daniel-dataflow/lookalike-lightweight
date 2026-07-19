#!/usr/bin/env bash
# Cloudinary 마이그레이션 실행 래퍼
# 사용법: 아래 값들을 채운 뒤 ./migrate_cloudinary.sh 실행

####
# 사전 준비 (최초 1회만)
# bashcd ~/dev/lookalike-lightweight/scripts/move_data
# pip install cloudinary
# chmod +x migrate_cloudinary.sh
# 1. 계정 정보 입력
# migrate_cloudinary.sh 파일 열어서 SOURCE_*(원본), TARGET_*(대상) 값 채우기.
# ⚠️ API Secret은 화면 공유/스크린샷 시 절대 노출되지 않게 주의.

# 2. dry-run으로 먼저 확인
# bash./migrate_cloudinary.sh --dry-run  -> 실제 업로드 없이 미리보기

# 3. 실제 마이그레이션 실행 (백그라운드)
# bash nohup ./migrate_cloudinary.sh > log.txt 2>&1 &

# 4. 진행 상황 확인
# bashtail -f log.txt          # 실시간 로그 (Ctrl+C로 빠져나와도 실행은 안 멈춤)
# ps -p <PID>               # 프로세스 살아있는지 확인 (PID는 nohup 실행 직후 [1] 뒤에 표시된 숫자)
# cat progress.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"   # 처리된 개수
# jobs                      # 백그라운드 작업이 Running인지 Done인지 확인

# 5. 중단됐다가 다시 실행할 때
# progress.json이 남아있으면 이미 처리한 항목은 자동으로 건너뛰고 이어서 진행됨. 그냥 3번 명령 다시 실행.
# bashnohup ./migrate_cloudinary.sh > log.txt 2>&1 &
# 만약 폴더별로 잘 들어간다면 폴더 재배치를 건너뛰고 싶으면 --skip-folder-update 옵션을 붙여서 실행. 2배 빠르다
# nohup ./migrate_cloudinary.sh --skip-folder-update > log.txt 2>&1 &
# tail -f log.txt

# 6. 실패 항목 확인
# bashcat failed.csv

# 7. 완료 확인
# bashtail -20 log.txt
# === 완료 === 와 전체 / 성공 / 건너뜀 / 실패 요약이 보이면 끝난 것.

####



set -euo pipefail

# ↓↓↓ 여기에 실제 계정 정보를 입력하세요 ↓↓↓
# 계정 정보는 여기 안 넣고, .env 파일에서 자동으로 읽어옵니다.
# export SOURCE_CLOUD_NAME="source_cloud_name"
# export SOURCE_API_KEY="source_api_key"
# export SOURCE_API_SECRET="source_api_secret"

# export TARGET_CLOUD_NAME="target_cloud_name"
# export TARGET_API_KEY="target_api_key"
# export TARGET_API_SECRET="target_api_secret"
# ↑↑↑ 여기까지 ↑↑↑

# 필요하면 python3 대신 가상환경 python 경로로 바꾸세요
# -u 옵션: 출력 버퍼링 비활성화 (파일로 리다이렉트해도 로그가 실시간으로 찍히게 함)
# 원래는 별도 파일(migrate_cloudinary.py)을 호출했지만, neon_db_replicate.sh처럼
# 파일 하나로 관리하기 위해 파이썬 코드를 아래에 그대로 내장(heredoc)해서 실행합니다.
# "$@" 는 그대로 파이썬 스크립트의 커맨드라인 인자(argv)로 전달됩니다. (예: --dry-run, --skip-folder-update)
python3 -u - "$@" <<'PYEOF'
#!/usr/bin/env python3
"""
Cloudinary 계정 간 자산(이미지/비디오/raw) 마이그레이션 스크립트

동작 방식:
  1. 소스 계정에서 Admin API로 리소스 목록(secure_url, public_id, tags, context 등)을 페이지 단위로 가져온다.
  2. 각 리소스를 타겟 계정에 secure_url을 그대로 업로드 소스로 넘겨서 업로드한다.
     (Cloudinary Upload API는 원격 URL을 그대로 받아 업로드할 수 있음 -> 로컬 다운로드 불필요)
  3. public_id, folder, tags, context를 최대한 그대로 유지한다.
  4. 실패한 항목은 failed.csv에 기록해서 재시도할 수 있게 한다.
  5. progress.json에 이미 처리한 public_id를 기록해서, 중간에 끊겨도 이어서 실행 가능(resume).

필요 조건:
  - 소스 계정의 자산이 "공개(public delivery)"여야 함 (private/authenticated 자산은 signed URL 별도 처리 필요)
  - pip install cloudinary python-dotenv
  - 같은 폴더(또는 상위 폴더)의 .env 파일에서 아래 6개 값을 자동으로 읽어옴:
      SOURCE_CLOUD_NAME, SOURCE_API_KEY, SOURCE_API_SECRET,
      TARGET_CLOUD_NAME, TARGET_API_KEY, TARGET_API_SECRET
"""

import os
import sys
import csv
import json
import time
import argparse
import cloudinary
import cloudinary.uploader
from cloudinary import api as cloudinary_api

try:
    from dotenv import load_dotenv
    # 현재 폴더의 .env, 없으면 상위 폴더들을 자동으로 탐색해서 로드
    load_dotenv()
except ImportError:
    print("[경고] python-dotenv가 설치되어 있지 않아 .env 파일을 읽지 못합니다.")
    print("       pip install python-dotenv 로 설치하거나, 환경변수를 직접 export 하세요.")

PROGRESS_FILE = "progress.json"
FAILED_FILE = "failed.csv"


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_progress(done_set):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done_set), f)


def append_failed(row):
    is_new = not os.path.exists(FAILED_FILE)
    with open(FAILED_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["public_id", "resource_type", "type", "secure_url", "error"])
        writer.writerow(row)


def configure_source():
    cloudinary.config(
        cloud_name=os.environ["SOURCE_CLOUD_NAME"],
        api_key=os.environ["SOURCE_API_KEY"],
        api_secret=os.environ["SOURCE_API_SECRET"],
        secure=True,
    )


def configure_target():
    cloudinary.config(
        cloud_name=os.environ["TARGET_CLOUD_NAME"],
        api_key=os.environ["TARGET_API_KEY"],
        api_secret=os.environ["TARGET_API_SECRET"],
        secure=True,
    )


def list_source_resources(resource_type, resource_delivery_type="upload"):
    """소스 계정의 모든 리소스를 next_cursor로 페이지네이션하며 순차적으로 yield.

    주의: cloudinary.config()는 전역 상태라서, 이 제너레이터가 yield로
    바깥 루프에 제어권을 넘긴 사이 바깥 루프가 configure_target()을 호출하면
    전역 설정이 타겟 계정으로 바뀐다. 그래서 다음 페이지를 가져오기 직전에
    반드시 다시 configure_source()를 호출해 소스 계정으로 되돌린다.
    """
    next_cursor = None
    while True:
        configure_source()  # API 호출 직전마다 소스 계정으로 재설정 (필수)
        params = dict(
            type=resource_delivery_type,
            resource_type=resource_type,
            max_results=500,
            context=True,
            tags=True,
        )
        if next_cursor:
            params["next_cursor"] = next_cursor

        result = cloudinary_api.resources(**params)
        for res in result.get("resources", []):
            yield res

        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break


def migrate(resource_types, dry_run=False, sleep_sec=0.2, skip_folder_update=False):
    done = load_progress()
    total, ok, skipped, failed, folder_warn = 0, 0, 0, 0, 0
    migrate.rate_limited = False  # Admin API 시간당 한도 초과 시 True로 전환되어 이후 update 단계 자동 생략

    for rtype in resource_types:
        print(f"\n=== {rtype} 리소스 목록 조회 중 (소스 계정) ===")
        for res in list_source_resources(rtype):
            total += 1
            public_id = res["public_id"]
            key = f"{rtype}:{public_id}"

            if key in done:
                skipped += 1
                continue

            secure_url = res["secure_url"]
            # 소스 계정이 Dynamic Folder Mode면 응답에 'asset_folder' 필드가 실제 폴더 위치를 담고 있음 (최우선)
            # 소스가 Fixed Folder Mode면 'folder' 필드나 public_id의 경로 부분을 사용
            folder_path = (
                res.get("asset_folder")
                or res.get("folder")
                or ("/".join(public_id.split("/")[:-1]) if "/" in public_id else "")
            )
            tags = res.get("tags", [])
            context = res.get("context", {}).get("custom", {}) if isinstance(res.get("context"), dict) else {}

            if dry_run:
                print(f"[DRY-RUN] {key} (folder={folder_path or '(root)'}) -> {secure_url}")
                done.add(key)
                continue

            try:
                configure_target()
                cloudinary.uploader.upload(
                    secure_url,
                    public_id=public_id,
                    resource_type=rtype,
                    folder=folder_path if folder_path else None,
                    asset_folder=folder_path if folder_path else None,
                    tags=tags if tags else None,
                    context=context if context else None,
                    overwrite=True,
                    unique_filename=False,
                    use_filename=False,
                )
            except Exception as e:
                # 업로드 자체가 실패한 경우만 진짜 실패로 처리 (재시도 대상)
                failed += 1
                print(f"[FAIL] {key}: 업로드 실패 - {e}")
                append_failed([public_id, rtype, "upload", secure_url, str(e)])
                save_progress(done)
                time.sleep(sleep_sec)
                continue

            # 업로드는 성공. 여기서부터는 "이미 타겟에 있던 자산의 폴더 위치 재확인"용 보조 단계라
            # 실패해도 업로드 자체는 성공한 것이므로 별도 경고로만 표시하고 실패 집계에 넣지 않는다.
            # (upload() 호출에 asset_folder를 이미 넘겼으므로, 신규 생성 자산은 이 단계 없이도 이미 폴더에 들어가 있음.
            #  이 단계는 예전에 잘못된 위치에 남아있던 기존 자산을 강제로 옮기기 위한 보험 성격.)
            folder_confirmed = True
            if folder_path and not skip_folder_update and not migrate.rate_limited:
                last_err = None
                for attempt in range(4):  # 최초 시도 + 최대 3회 재시도
                    try:
                        if attempt > 0:
                            time.sleep(0.5 * attempt)  # 0.5s, 1.0s, 1.5s 순으로 대기
                        cloudinary_api.update(
                            public_id,
                            resource_type=rtype,
                            type="upload",
                            asset_folder=folder_path,
                        )
                        last_err = None
                        break
                    except Exception as ue:
                        last_err = ue
                        if "420" in str(ue) or "Rate Limit" in str(ue):
                            # 시간당 한도 자체가 다 찬 것 -> 재시도해봐야 소용없고 호출만 더 낭비함.
                            # 남은 실행 동안은 update 단계를 자동으로 건너뛰고 업로드만 계속 진행.
                            migrate.rate_limited = True
                            break
                if last_err is not None:
                    folder_confirmed = False
                    folder_warn += 1
                    if migrate.rate_limited:
                        print(f"[OK-경고] {key}: 업로드는 성공, Admin API 한도 초과로 폴더 재확인 생략 (남은 항목도 자동 생략됨) - {last_err}")
                    else:
                        print(f"[OK-경고] {key}: 업로드는 성공, 폴더 재확인만 실패(인덱싱 지연 가능성) - {last_err}")

            ok += 1
            done.add(key)
            if folder_confirmed:
                print(f"[OK] {key} -> {folder_path or '(root)'}")

            # 진행 상황은 매 건마다 저장 (중간에 끊겨도 이어서 가능)
            save_progress(done)
            time.sleep(sleep_sec)

    print("\n=== 완료 ===")
    print(f"전체: {total} / 성공: {ok} (그중 폴더 재확인 경고: {folder_warn}) / 이미완료(건너뜀): {skipped} / 실패: {failed}")
    if failed:
        print(f"실패(업로드 자체 실패) 목록은 {FAILED_FILE} 에서 확인하세요. 재실행하면 자동으로 다시 시도됩니다.")
    if folder_warn:
        print(f"폴더 재확인 경고 {folder_warn}건은 업로드는 성공했으니 데이터 유실은 없습니다. 콘솔에서 폴더 위치만 한번 확인해보세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cloudinary 계정 간 자산 마이그레이션")
    parser.add_argument(
        "--types",
        default="image,video,raw",
        help="마이그레이션할 resource_type, 콤마로 구분 (기본: image,video,raw)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 업로드 없이 대상 목록만 출력",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="각 업로드 사이 대기 시간(초), API rate limit 방지용 (기본 0.2)",
    )
    parser.add_argument(
        "--skip-folder-update",
        action="store_true",
        help=(
            "업로드 후 폴더 재확인(update) 호출을 건너뜀. "
            "타겟 계정이 비어있는 상태에서 새로 올리는 경우, upload()에 이미 asset_folder를 "
            "넘기므로 이 단계 없이도 폴더 배치가 정상 동작함. Admin API 호출을 절반으로 줄여 "
            "rate limit(420)을 피할 수 있음. 예전에 잘못된 위치에 남은 자산을 옮기는 '복구 실행'일 때만 끄지 말 것."
        ),
    )
    args = parser.parse_args()

    required_env = [
        "SOURCE_CLOUD_NAME", "SOURCE_API_KEY", "SOURCE_API_SECRET",
        "TARGET_CLOUD_NAME", "TARGET_API_KEY", "TARGET_API_SECRET",
    ]
    missing = [v for v in required_env if v not in os.environ]
    if missing:
        print(f"다음 환경변수가 설정되지 않았습니다: {', '.join(missing)}")
        sys.exit(1)

    migrate(
        args.types.split(","),
        dry_run=args.dry_run,
        sleep_sec=args.sleep,
        skip_folder_update=args.skip_folder_update,
    )
PYEOF
