"""
로컬 ML 추론 서비스 (YOLOv8 + Fashion-CLIP)
- YOLOv8 기반 패션 아이템 객체 탐지
- Fashion-CLIP 기반 이미지 특징 벡터 추출 (512차원)
- 싱글톤 패턴으로 모델 상주 및 CPU/GPU 디바이스 최적화
- 한국어 주석 필수 준수
"""
import os
import io
import logging
import asyncio
from typing import Optional, List, Dict
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)

# 전역 싱글톤 인스턴스를 저장할 변수
_local_ml_service_instance = None


class LocalMLService:
    """
    로컬에서 YOLO 및 Fashion-CLIP 추론을 담당하는 서비스 클래스 (싱글톤)
    """
    def __init__(self):
        self.yolo_model = None
        self.clip_model = None
        self.clip_processor = None
        self.device = None
        self.is_ready = False
        
        # 탐지 품질 파라미터 (HF Space app.py와 완벽 매핑)
        self.CONF_THRESHOLD = 0.35
        self.MIN_AREA_RATIO = 0.04
        self.IOU_THRESHOLD = 0.45
        self.BOTTOM_CONF_THRESHOLD = 0.20
        self.CONTAINMENT_THRESHOLD = 0.75

    @classmethod
    def get_instance(cls) -> "LocalMLService":
        """싱글톤 인스턴스 반환"""
        global _local_ml_service_instance
        if _local_ml_service_instance is None:
            _local_ml_service_instance = cls()
        return _local_ml_service_instance

    def check_dependencies(self) -> bool:
        """필수 패키지 설치 여부 검사"""
        try:
            import torch
            import transformers
            import ultralytics
            return True
        except ImportError:
            return False

    def load_models(self, yolo_path: str) -> bool:
        """
        모델을 디스크에서 메모리로 로드 (싱글톤)
        """
        if self.is_ready:
            return True

        if not self.check_dependencies():
            logger.warning("⚠️ 로컬 ML 필수 라이브러리(torch, transformers, ultralytics)가 설치되어 있지 않습니다.")
            return False

        try:
            import torch
            from ultralytics import YOLO
            from transformers import CLIPModel, CLIPProcessor

            logger.info("🔄 로컬 ML 모델 로드 중...")

            # CUDA 디바이스 사용 가능 여부 체크
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            logger.info(f"로컬 ML 디바이스 설정: {self.device}")

            # YOLO 모델 로딩
            if not os.path.exists(yolo_path):
                logger.error(f"❌ YOLO 가중치 파일을 찾을 수 없습니다: {yolo_path}")
                return False

            logger.info(f"YOLOv8 가중치 파일 로드: {yolo_path}")
            self.yolo_model = YOLO(yolo_path)

            # Fashion-CLIP 모델 로딩
            logger.info("Fashion-CLIP 모델 및 프로세서 로드 중...")
            self.clip_model = CLIPModel.from_pretrained("patrickjohncyh/fashion-clip")
            self.clip_processor = CLIPProcessor.from_pretrained("patrickjohncyh/fashion-clip")
            self.clip_model.eval()
            self.clip_model.to(self.device)

            self.is_ready = True
            logger.info("✅ 로컬 ML 모델 (YOLOv8 + Fashion-CLIP) 메모리 상주 완료!")
            return True

        except Exception as e:
            logger.error(f"❌ 로컬 ML 모델 로딩 실패: {e}", exc_info=True)
            self.is_ready = False
            return False

    def _containment_ratio(self, inner: dict, outer: dict) -> float:
        """inner 박스가 outer 박스 내부에 포함된 비율 계산"""
        ix1 = max(inner["x1"], outer["x1"])
        iy1 = max(inner["y1"], outer["y1"])
        ix2 = min(inner["x2"], outer["x2"])
        iy2 = min(inner["y2"], outer["y2"])

        inter_w = max(0.0, ix2 - ix1)
        inter_h = max(0.0, iy2 - iy1)
        inter_area = inter_w * inter_h

        inner_area = max(1.0, (inner["x2"] - inner["x1"]) * (inner["y2"] - inner["y1"]))
        return inter_area / inner_area

    def _select_best_boxes(self, raw_boxes: list[dict], img_w: int, img_h: int) -> list[dict]:
        """
        YOLO가 탐지한 전체 박스 중 품질이 낮거나 중복되는 영역을 필터링 및 병합
        """
        img_area = img_w * img_h
        if img_area <= 0:
            return raw_boxes

        # 1단계: 신뢰도 및 최소 면적 기준으로 필터링
        filtered = []
        for box in raw_boxes:
            label = box.get("label", "").lower()
            conf = box.get("confidence", 0.0)
            threshold = self.BOTTOM_CONF_THRESHOLD if label in ("bottom", "하의") else self.CONF_THRESHOLD
            
            if conf < threshold:
                continue

            x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
            box_area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
            area_ratio = box_area / img_area

            if area_ratio < self.MIN_AREA_RATIO:
                continue

            filtered.append(box)

        # 2단계: 동일 카테고리(레이블) 박스를 합집합(Union)으로 병합
        union_by_label = {}
        for box in filtered:
            label = box.get("label", "unknown")
            if label not in union_by_label:
                union_by_label[label] = dict(box)
            else:
                prev = union_by_label[label]
                prev["x1"] = min(prev["x1"], box["x1"])
                prev["y1"] = min(prev["y1"], box["y1"])
                prev["x2"] = max(prev["x2"], box["x2"])
                prev["y2"] = max(prev["y2"], box["y2"])
                prev["confidence"] = max(prev.get("confidence", 0.0), box.get("confidence", 0.0))
                logger.info(f"로컬 박스 Union 병합: '{label}'")

        # 3단계: 포함 관계 필터링 (Outer 안에 Top이 과도하게 포함되는 경우 등 처리)
        candidates = list(union_by_label.values())
        to_remove = set()

        for i, box_a in enumerate(candidates):
            for j, box_b in enumerate(candidates):
                if i == j:
                    continue
                label_a = box_a.get("label", "").lower()
                label_b = box_b.get("label", "").lower()

                # bottom은 outer와 수직 분할이므로 포함 여부 판단에서 제외
                if label_a in ("bottom", "하의") or label_b in ("bottom", "하의"):
                    continue

                ratio = self._containment_ratio(box_a, box_b)
                if ratio >= self.CONTAINMENT_THRESHOLD:
                    area_a = (box_a["x2"] - box_a["x1"]) * (box_a["y2"] - box_a["y1"])
                    area_b = (box_b["x2"] - box_b["x1"]) * (box_b["y2"] - box_b["y1"])
                    if area_a < area_b:
                        to_remove.add(label_a)
                        logger.info(f"로컬 포함관계 필터 제거: '{label_a}'가 '{label_b}'에 포함됨")

        result = [b for b in candidates if b.get("label", "").lower() not in to_remove]
        logger.info(f"로컬 박스 필터링 완료: {len(raw_boxes)}개 -> {len(result)}개")
        return result

    def _get_best_crop(self, pil_img: Image.Image, boxes: list[dict]) -> Image.Image:
        """
        탐지된 박스 중 신뢰도가 가장 높은 패션 객체 영역을 크롭
        """
        if not boxes:
            return pil_img

        best = max(boxes, key=lambda b: b.get("confidence", 0.0))
        x1, y1, x2, y2 = int(best["x1"]), int(best["y1"]), int(best["x2"]), int(best["y2"])

        w, h = pil_img.size
        x1 = max(0, min(x1, w - 1))
        x2 = max(x1 + 1, min(x2, w))
        y1 = max(0, min(y1, h - 1))
        y2 = max(y1 + 1, min(y2, h))

        try:
            cropped = pil_img.crop((x1, y1, x2, y2))
            logger.info(f"로컬 크롭 성공: label={best.get('label')}, conf={best.get('confidence', 0):.3f}")
            return cropped
        except Exception as e:
            logger.warning(f"로컬 크롭 실패 (원본 사용): {e}")
            return pil_img

    async def predict_local(self, image_bytes: bytes) -> dict:
        """
        로컬 모델 기반 YOLO 객체 탐지 및 Fashion-CLIP 이미지 임베딩 동시 추론
        """
        if not self.is_ready:
            raise RuntimeError("로컬 ML 모델이 아직 준비되지 않았거나 로딩에 실패했습니다.")

        import torch

        # 이미지 전처리
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_w, img_h = pil_img.size

        # 1. YOLO 객체 탐지 (별도 스레드 실행으로 비동기 안전성 확보)
        def _yolo_run():
            return self.yolo_model.predict(
                source=pil_img,
                conf=0.10,
                iou=0.80,
                save=False,
                verbose=False
            )

        # CPU/GPU 추론 연산은 대기열이 생기지 않도록 차단 방지 적용
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _yolo_run)

        raw_boxes = []
        if results and len(results) > 0:
            for result in results:
                if result.boxes:
                    for box in result.boxes:
                        x1, y1, x2, y2 = box.xyxy[0].tolist()
                        conf = float(box.conf[0]) if box.conf is not None else 0.0
                        cls = int(box.cls[0]) if box.cls is not None else 0
                        label = result.names.get(cls, "unknown") if hasattr(result, "names") else "unknown"
                        raw_boxes.append({
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                            "confidence": conf,
                            "label": label
                        })

        # 박스 후처리 및 최적의 검색 카테고리 판정
        filtered_boxes = self._select_best_boxes(raw_boxes, img_w, img_h)
        detected_category = None
        if filtered_boxes:
            best_box = max(filtered_boxes, key=lambda b: b.get("confidence", 0.0))
            label = best_box.get("label", "")
            if label and label != "unknown":
                detected_category = label

        # 2. Fashion-CLIP 임베딩 생성
        embed_img = self._get_best_crop(pil_img, filtered_boxes)

        def _clip_run():
            inputs = self.clip_processor(images=embed_img, return_tensors="pt").to(self.device)
            with torch.no_grad():
                vision_outputs = self.clip_model.vision_model(**inputs)
                features = self.clip_model.visual_projection(vision_outputs.pooler_output)
                # L2 정규화 (코사인 유사도 매칭 최적화)
                embedding = torch.nn.functional.normalize(features, p=2, dim=1)
                return embedding[0].cpu().tolist()

        embedding_list = await loop.run_in_executor(None, _clip_run)

        return {
            "status": "success",
            "embedding": embedding_list,
            "boxes": filtered_boxes,
            "label": detected_category if detected_category else "full_image",
            "category": detected_category
        }


# 모듈 임포트 시 싱글톤 접근이 용이하도록 기본 인스턴스 노출
local_ml_service = LocalMLService.get_instance()
