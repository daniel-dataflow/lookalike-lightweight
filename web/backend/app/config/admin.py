"""
어드민 대시보드 및 시스템 모니터링 설정 (Admin Configs)
"""

# 캐시 TTL (초)
SYSTEM_CACHE_TTL = 5
# DB 캐시 TTL을 늘려서 Neon API/DB 호출 빈도를 낮춥니다 (초 단위)
# 원래 10초였으나 운영에서 과도한 쿼리가 발생하여 기본값을 600초(10분)으로 변경합니다.
DB_CACHE_TTL = 600
INFRA_CACHE_TTL = 60
