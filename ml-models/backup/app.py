import gradio as gr
from ultralytics import YOLO
import numpy as np
from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel
import logging
import traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("YOLO 로딩...")
yolo_model = YOLO("best.pt")
logger.info("Fashion-CLIP 로딩...")
clip_model = CLIPModel.from_pretrained("patrickjohncyh/fashion-clip")
clip_processor = CLIPProcessor.from_pretrained("patrickjohncyh/fashion-clip")
clip_model.eval()
logger.info("모델 로딩 완료!")

# ─── 탐지 품질 파라미터 ───────────────────────────────────────────────────────
# 신뢰도 임계값: 이보다 낮은 탐지 결과는 노이즈로 간주하여 제거
CONF_THRESHOLD = 0.35

# 최소 면적 비율: 이미지 전체 면적 대비 탐지 박스 면적이 이 비율 미만이면 제거
# (예: 0.04 = 전체 이미지의 4% 미만인 박스는 너무 작아 신뢰할 수 없음)
MIN_AREA_RATIO = 0.04

# IOU 임계값: YOLO 내부 NMS에서 중복 박스 제거 기준 (낮을수록 엄격)
IOU_THRESHOLD = 0.45


def _containment_ratio(inner: dict, outer: dict) -> float:
    """
    inner 박스가 outer 박스 내부에 포함된 비율(0.0~1.0)을 반환.
    1.0이면 inner가 outer에 완전히 포함됨.
    inner 면적 대비 교집합 면적의 비율로 계산.
    """
    ix1 = max(inner["x1"], outer["x1"])
    iy1 = max(inner["y1"], outer["y1"])
    ix2 = min(inner["x2"], outer["x2"])
    iy2 = min(inner["y2"], outer["y2"])

    inter_w = max(0.0, ix2 - ix1)
    inter_h = max(0.0, iy2 - iy1)
    inter_area = inter_w * inter_h

    inner_area = max(1.0, (inner["x2"] - inner["x1"]) * (inner["y2"] - inner["y1"]))
    return inter_area / inner_area


def _select_best_boxes(raw_boxes: list[dict], img_w: int, img_h: int) -> list[dict]:
    """
    YOLO가 반환한 모든 박스에서 품질 낮은 박스를 제거하고
    카테고리(레이블)별로 신뢰도가 가장 높은 박스 1개씩만 선택.

    추가 후처리:
    - 포함 관계 필터: 한 박스가 다른 박스 안에 크게 포함되면 제거
      예) Outer 박스 안에 Top 박스가 80% 이상 들어 있으면 Top 제거
    - Bottom 박스 확장: 하의 박스가 이미지 하단에 닿지 않으면 아래로 늘려 바지 전체 포함

    Args:
        raw_boxes: YOLO에서 반환된 원본 박스 목록
        img_w: 원본 이미지 가로 픽셀
        img_h: 원본 이미지 세로 픽셀

    Returns:
        정제된 박스 목록
    """
    img_area = img_w * img_h
    if img_area <= 0:
        return raw_boxes

    # 1단계: 신뢰도 + 최소 면적 기준으로 노이즈 박스 제거
    # Bottom 전용 임계값: 가려진 다리·오른쪽 바지 등이 낮은 신뢰도로 탐지될 수 있으므로
    # 다른 카테고리(0.35)보다 낮은 0.20을 적용해 2개 박스가 Union까지 살아남도록 함.
    BOTTOM_CONF_THRESHOLD = 0.20
    filtered = []
    for box in raw_boxes:
        label = box.get("label", "").lower()
        conf = box.get("confidence", 0.0)
        # 하의는 낮은 신뢰도 임계값 적용, 나머지는 기본 임계값
        threshold = BOTTOM_CONF_THRESHOLD if label in ("bottom", "하의") else CONF_THRESHOLD
        if conf < threshold:
            logger.debug(f"신뢰도 미달 박스 제거: label={label}, conf={conf:.3f} (기준={threshold:.2f})")
            continue

        x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
        box_area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
        area_ratio = box_area / img_area

        if area_ratio < MIN_AREA_RATIO:
            logger.debug(
                f"면적 미달 박스 제거: label={label}, "
                f"area_ratio={area_ratio:.3f} (<{MIN_AREA_RATIO})"
            )
            continue

        filtered.append(box)

    # 2단계: 카테고리(레이블)별 박스를 Union으로 병합
    # 동일 카테고리(예: Bottom)가 여러 개 탐지된 경우,
    # 가장 신뢰도 높은 것만 고르지 않고 모든 박스를 합집합(Union)으로 합침.
    # → 바지가 두 박스로 나뉘어 탐지될 때 두 박스를 합쳐 전체 바지 영역 커버
    union_by_label: dict[str, dict] = {}
    for box in filtered:
        label = box.get("label", "unknown")
        if label not in union_by_label:
            # 첫 번째 박스는 그대로 복사 (원본 변경 방지)
            union_by_label[label] = dict(box)
        else:
            prev = union_by_label[label]
            # 기존 박스와 현재 박스의 합집합(Union) 계산
            prev["x1"] = min(prev["x1"], box["x1"])
            prev["y1"] = min(prev["y1"], box["y1"])
            prev["x2"] = max(prev["x2"], box["x2"])
            prev["y2"] = max(prev["y2"], box["y2"])
            # 신뢰도는 최대값 유지
            prev["confidence"] = max(prev.get("confidence", 0.0), box.get("confidence", 0.0))
            logger.info(
                f"박스 Union 병합: '{label}' 박스 2개 합산 "
                f"→ ({prev['x1']:.0f},{prev['y1']:.0f})-({prev['x2']:.0f},{prev['y2']:.0f})"
            )

    # 3단계: 포함 관계 필터
    # 박스 A가 박스 B 안에 CONTAINMENT_THRESHOLD 이상 포함되면 A를 제거
    # 예) Outer(큰 박스) 안에 Top(작은 박스)이 80%+ 포함 → Top 제거
    # 단, Bottom은 Outer 아래쪽에 별도 존재하므로 다른 기준 적용
    CONTAINMENT_THRESHOLD = 0.75  # inner 박스 면적의 이 비율 이상이 outer 안에 있으면 제거
    candidates = list(union_by_label.values())
    to_remove = set()

    for i, box_a in enumerate(candidates):
        for j, box_b in enumerate(candidates):
            if i == j:
                continue
            label_a = box_a.get("label", "").lower()
            label_b = box_b.get("label", "").lower()

            # bottom은 outer와 수직으로 분리되므로 포함 판단에서 제외
            if label_a in ("bottom", "하의") or label_b in ("bottom", "하의"):
                continue

            ratio = _containment_ratio(box_a, box_b)
            if ratio >= CONTAINMENT_THRESHOLD:
                # box_a가 box_b 안에 크게 포함됨 → box_a 면적이 box_b보다 작으면 제거
                area_a = (box_a["x2"] - box_a["x1"]) * (box_a["y2"] - box_a["y1"])
                area_b = (box_b["x2"] - box_b["x1"]) * (box_b["y2"] - box_b["y1"])
                if area_a < area_b:
                    to_remove.add(label_a)
                    logger.info(
                        f"포함 관계 필터: '{label_a}' 박스가 '{label_b}' 박스에 "
                        f"{ratio:.0%} 포함 → '{label_a}' 제거"
                    )

    result = [b for b in candidates if b.get("label", "").lower() not in to_remove]

    logger.info(
        f"박스 필터링: 원본 {len(raw_boxes)}개 → "
        f"신뢰도/면적 필터 후 {len(filtered)}개 → "
        f"Union 병합 후 {len(union_by_label)}개 → "
        f"포함 관계 필터 후 {len(result)}개"
    )
    return result



def _get_best_crop(pil_img: Image.Image, boxes: list[dict]) -> Image.Image:
    """
    필터링된 박스 중 신뢰도가 가장 높은 박스 영역을 크롭하여 반환.
    박스가 없으면 원본 이미지를 그대로 반환.

    크롭 이미지로 CLIP 임베딩을 생성하면
    전체 이미지 임베딩보다 패션 아이템에 집중된 더 정확한 벡터를 얻을 수 있음.
    """
    if not boxes:
        return pil_img

    # 신뢰도가 가장 높은 박스 선택
    best = max(boxes, key=lambda b: b.get("confidence", 0.0))
    x1 = int(best["x1"])
    y1 = int(best["y1"])
    x2 = int(best["x2"])
    y2 = int(best["y2"])

    # 원본 이미지 범위 클램핑
    w, h = pil_img.size
    x1 = max(0, min(x1, w - 1))
    x2 = max(x1 + 1, min(x2, w))
    y1 = max(0, min(y1, h - 1))
    y2 = max(y1 + 1, min(y2, h))

    try:
        cropped = pil_img.crop((x1, y1, x2, y2))
        logger.info(
            f"CLIP 임베딩용 크롭 이미지: "
            f"label={best.get('label')}, conf={best.get('confidence', 0):.3f}, "
            f"crop=({x1},{y1},{x2},{y2})"
        )
        return cropped
    except Exception as e:
        logger.warning(f"크롭 실패, 원본 사용: {e}")
        return pil_img


def predict(image):
    try:
        if image is None:
            return {
                "status": "error",
                "error_message": "No image provided",
                "embedding": None,
                "boxes": [],
                "label": "unknown",
                "category": None,
            }

        # ── 이미지 전처리 ─────────────────────────────────
        if isinstance(image, str):
            pil_img = Image.open(image).convert("RGB")
        elif isinstance(image, np.ndarray):
            pil_img = Image.fromarray(image).convert("RGB")
        elif isinstance(image, Image.Image):
            pil_img = image.convert("RGB")
        else:
            pil_img = Image.open(str(image)).convert("RGB")

        img_w, img_h = pil_img.size

        # ── YOLO 탐지 ─────────────────────────────────────
        # conf=0.10: 낮게 설정하여 가려진/약하게 탐지된 박스도 일단 수집
        #            실제 신뢰도 필터는 _select_best_boxes에서 카테고리별로 처리
        # iou=0.80: NMS를 느슨하게 → 겹치는 Bottom 박스 2개가 모두 살아남음
        #           (예: 왼쪽 다리 박스 + 오른쪽 다리 박스가 0.80 미만으로 겹침)
        #           두 박스가 모두 도달해야 Union으로 전체 바지 영역 합산 가능
        results = yolo_model.predict(
            source=pil_img,
            conf=0.10,       # 낮게: 약한 탐지도 수집 (이후 _select_best_boxes에서 필터)
            iou=0.80,        # 느슨한 NMS: 같은 카테고리 박스 2개가 모두 살아남도록
            save=False,
            verbose=False,
        )

        raw_boxes = []
        if results and len(results) > 0:
            for result in results:
                if result.boxes:
                    for box in result.boxes:
                        x1, y1, x2, y2 = box.xyxy[0].tolist()
                        conf = float(box.conf[0]) if box.conf is not None else 0
                        cls = int(box.cls[0]) if box.cls is not None else 0
                        label = (
                            result.names.get(cls, "unknown")
                            if hasattr(result, "names")
                            else "unknown"
                        )
                        raw_boxes.append(
                            {
                                "x1": x1,
                                "y1": y1,
                                "x2": x2,
                                "y2": y2,
                                "confidence": conf,
                                "label": label,
                            }
                        )

        # ── 박스 후처리: 노이즈 제거 + 카테고리별 최고 신뢰도 1개 선택 ────────
        filtered_boxes = _select_best_boxes(raw_boxes, img_w, img_h)

        # 대표 카테고리: 신뢰도 가장 높은 박스의 레이블
        detected_category = None
        if filtered_boxes:
            best_box = max(filtered_boxes, key=lambda b: b.get("confidence", 0.0))
            label = best_box.get("label", "")
            if label and label != "unknown":
                detected_category = label

        # ── CLIP 임베딩 (512d) ─────────────────────────────
        # 크롭 이미지로 임베딩 생성 (박스가 있으면 상품 영역만 크롭)
        embed_img = _get_best_crop(pil_img, filtered_boxes)
        inputs = clip_processor(images=embed_img, return_tensors="pt")

        with torch.no_grad():
            # vision_model → visual_projection 순서로 명시 호출
            vision_outputs = clip_model.vision_model(**inputs)
            features = clip_model.visual_projection(vision_outputs.pooler_output)

        # L2 정규화 (코사인 유사도 최적화)
        embedding = torch.nn.functional.normalize(features, p=2, dim=1)
        embedding_list = embedding[0].cpu().tolist()

        logger.info(
            f"임베딩 생성 완료: dim={len(embedding_list)}, "
            f"filtered_boxes={len(filtered_boxes)}, "
            f"category={detected_category}"
        )

        return {
            "status": "success",
            "embedding": embedding_list,   # 512d 벡터
            "boxes": filtered_boxes,
            "label": detected_category if detected_category else "full_image",
            "category": detected_category,
        }

    except Exception as e:
        err_msg = traceback.format_exc()
        logger.error(f"추론 중 예외 발생: {err_msg}")
        return {
            "status": "error",
            "error_message": str(e),
            "traceback": err_msg,
            "embedding": None,
            "boxes": [],
            "label": "unknown",
            "category": None,
        }


demo = gr.Interface(
    fn=predict,
    inputs=gr.Image(type="numpy"),
    outputs=gr.JSON(),
)
demo.launch(show_error=True)
