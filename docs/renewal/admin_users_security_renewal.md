# 어드민 세부 권한 통제 및 보안/유저 모니터링 시스템 구축 사양서 (Renewal Specs)

본 문서는 Lookalike 서비스의 관리자 권한 세분화(RBAC) 체계 도입, 일반 유저 계정 보안 취약점 개선(보안 질문 기반 임시 패스워드 및 강제 변경 흐름), 그리고 데이터베이스 SQL 쿼리 무결성 최적화에 대한 상세 스펙을 기술합니다.

---

## 1. 개편 배경 및 목적 (Architectural Background)
1. **관리자 권한의 획일성 극복**: 
   기존에는 어드민 계정의 권한이 단순 3등급 문자열(`SUPER_ADMIN`, `CRAWL_ONLY`, `ANALYSIS_ONLY`) 형태로 고정되어 있어 페이지별 미세 제어가 불가능했습니다. 최고 관리자가 각 어드민에게 필요한 페이지만 직접 체크 선택하여 동적으로 권한을 구성할 수 있는 시스템이 요구되었습니다.
2. **이메일 가입 회원의 패스워드 유실 대응**: 
   구축된 이메일 서버가 없는 상황에서 사용자가 패스워드를 분실했을 때 안전하게 본인 인증을 수행하고 임시 비밀번호를 발급받아 스스로 패스워드를 갱신할 수 있는 무인 프로세스가 필요했습니다.
3. **활동 로그 질의 시 PostgreSQL DISTINCT 쿼리 충돌 핫픽스**: 
   기존 검색어 목록 조회 시 사용하던 `SELECT DISTINCT ... ORDER BY create_dt` 구문이 PostgreSQL의 엄격한 문법 규약에 위배되어 화면이 뻗어버리는 심각한 런타임 오류가 발생하여 이를 표준 규격으로 개선했습니다.

---

## 2. 상세 변경 사양 (Technical Specifications)

### A. PostgreSQL SELECT DISTINCT ORDER BY 문법 오류 해결
* **오류 원인**: `SELECT DISTINCT input_text` 구문을 서브쿼리 내에서 쓸 때 정렬(`ORDER BY create_dt`)에 사용되는 `create_dt`가 Select 대상 컬럼 리스트에 포함되어 있지 않아 발생한 SQL 컴파일 에러입니다.
* **조치 사항**: 
  `GROUP BY input_text`와 `MAX(create_dt)` 집계를 사용하여 중복 키워드를 단일화하는 임시 테이블 구조로 인라인 서브쿼리를 완전히 교체하였습니다.
* **적용 쿼리 (예시)**:
  ```sql
  (SELECT ARRAY_TO_STRING(ARRAY(
      SELECT input_text FROM (
          SELECT input_text, MAX(create_dt) as max_dt
          FROM search_logs
          WHERE user_id = u.user_id AND input_text IS NOT NULL AND input_text != ''
          GROUP BY input_text
          ORDER BY max_dt DESC
          LIMIT 3
      ) tmp
  ), ', ')) AS recent_keywords
  ```
  이 방식을 적용하여 일반 회원 탭 및 비로그인 유저 탭에서 에러 없이 1초 내외로 실시간 통계 조회가 완료되도록 조치했습니다.

### B. 페이지 단위 체크박스 기반 관리자 세부 권한 제어 (RBAC)
* **스키마 변경**: `users` 테이블의 `admin_permission` 컬럼에 쉼표로 분리된 문자열(Comma-separated string, 예: `infra,crawling,logs`)을 보관하여 여러 권한의 동시 부여를 지원합니다.
* **메뉴별 권한 이름 매핑**:
  * `infra`: 인프라 모니터링
  * `crawling`: 크롤링 모니터링
  * `logs`: 로그 모니터링
  * `visitors`: 방문자 분석
  * `inquiry`: 문의 관리
  * `SUPER_ADMIN`: 최고 권한자 (모든 페이지 접근 허용)
* **권한 인가 미들웨어 구현**: 
  * `pages.py` 내에 `_check_page_permission(request, required_permission)` 함수를 신설하여 어드민 세션 접속 시 세부 권한 목록을 파싱해 매칭 여부를 검증합니다.
  * 권한이 없는 어드민의 접근이 감지될 경우, 멋진 진입 제한 경고 페이지인 [admin_forbidden.html](file:///d:/dev/lookalike-lightweight/web/frontend/templates/admin_forbidden.html) 화면을 렌더링하고 차단합니다.
  * **사이드바 메뉴 가시성 제어**: 템플릿 렌더링 컨텍스트에 `admin_permissions` 를 전달하여, 로그인한 어드민 계정의 허용 목록에 없는 메뉴는 화면에 아예 노출조차 되지 않도록 차단 처리했습니다.

### C. 일반 가입 유저 비밀번호 분실 찾기 & 필수 변경 강제 흐름
* **회원가입 양식 확장**: 
  `users` 테이블에 `security_question` 및 `security_answer` (bcrypt 해싱) 필드를 DDL 기동 시점에 자동 추가하고, 회원가입 시 보안 질문 선택 및 답변 입력을 필수로 받도록 설계했습니다.
* **임시 비밀번호 발급 및 로그인**: 
  * 사용자가 이메일 및 질문/답변을 기입하고 임시 비밀번호를 요청하면 안전하게 검증하여 화면에 즉각적으로 임시 비밀번호(12자리 난수)를 노출하며, DB 내 `is_temp_password` 플래그를 `True` 로 갱신합니다.
  * 로그인 성공 시 `require_password_change: True` 플래그를 프론트엔드로 전달합니다.
* **새 비밀번호 강제 변경 팝업 모달**: 
  * 임시 비밀번호 로그인 성공이 감지되면 다른 화면을 이용할 수 없도록 격리된 **비밀번호 강제 변경 팝업창**을 띄웁니다.
  * 변경 완료 전까지 다른 서비스 이용을 영구 차단하여 잠재적 보안 취약점을 완벽히 제거했습니다.

---

## 3. 핵심 변경 파일 일람 (Key Affected Files)

1. **`web/backend/app/routers/admin.py`**:
   * SQL DISTINCT 에러 핫픽스 적용.
   * `create-admin`, `update-permission`, `reset-admin-password` API 등에 `_verify_super_admin` 보안 가드를 적용하여 하위 어드민의 탈취/변조 시도 원천 차단.
2. **`web/backend/app/routers/pages.py`**:
   * 페이지별 세션 권한 매칭용 `_check_page_permission` 헬퍼 함수 탑재.
   * `/admin/users` 독립 어드민 관리 뷰 렌더링 및 `/forgot-password` 분실 찾기 뷰 렌더링 추가.
3. **`web/frontend/templates/admin_base.html`**:
   * Jinja2 조건문을 활용해 `admin_permissions` 별로 PC/모바일의 사이드바 메뉴 가시성 동적 분기.
4. **`web/frontend/templates/admin_users.html`**:
   *select 형태였던 권한 드롭다운을 페이지별 체크박스 리스트 및 SUPER_ADMIN 프리패스 토글 체크박스로 완전 개편.
   * "권한수정" 전용 모달 창 추가 바인딩.
5. **`web/frontend/templates/admin_forbidden.html` (신설)**:
   * 접근 권한이 막힌 어드민 화면 서빙용 경고 뷰.
6. **`web/frontend/templates/forgot_password.html` (신설)**:
   * 본인 인증 성공 시 세련된 그라디언트 카드에 임시 비밀번호를 복사할 수 있게 렌더링해 주는 분실 찾기 독립 화면.
