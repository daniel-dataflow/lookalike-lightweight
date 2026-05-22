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

        # ── YOLO 탐지 ─────────────────────────────────────
        results = yolo_model(pil_img)
        boxes = []
        detected_category = None

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

                        boxes.append(
                            {
                                "x1": x1,
                                "y1": y1,
                                "x2": x2,
                                "y2": y2,
                                "confidence": conf,
                                "label": label,
                            }
                        )

                        if not detected_category and label != "unknown":
                            detected_category = label

        # ── CLIP 임베딩 (512d) ─────────────────────────────
        # 수정: get_image_features() 대신 vision_model → visual_projection 순서로
        # 명시적으로 호출해야 최신 transformers에서 텐서를 올바르게 얻을 수 있음
        inputs = clip_processor(images=pil_img, return_tensors="pt")

        with torch.no_grad():
            # vision_model: BaseModelOutputWithPooling 반환
            vision_outputs = clip_model.vision_model(**inputs)
            # pooler_output: [batch, hidden_dim=768] → visual_projection → [batch, 512]
            features = clip_model.visual_projection(vision_outputs.pooler_output)

        # L2 정규화 (코사인 유사도 최적화)
        embedding = torch.nn.functional.normalize(features, p=2, dim=1)
        embedding_list = embedding[0].cpu().tolist()

        logger.info(f"임베딩 생성 완료: dim={len(embedding_list)}, boxes={len(boxes)}")

        return {
            "status": "success",
            "embedding": embedding_list,   # 512d 벡터
            "boxes": boxes,
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
