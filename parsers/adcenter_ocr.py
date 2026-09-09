"""캡처 이미지에서 소재명·소진 뽑기 (마지막 수단).

OCR 은 숫자를 자주 틀리므로 **결과를 그대로 저장하지 않는다.**
app.py 에서 표로 띄워 사용자가 눈으로 고친 뒤 저장하는 것을 전제로 한다.

엔진은 있는 것을 쓴다: easyocr → pytesseract 순.
둘 다 없으면 설치 안내를 담은 OCRUnavailable 을 던진다.
"""
from __future__ import annotations

import io
import re

import pandas as pd

INSTALL_HINT = (
    "OCR 엔진이 없습니다. 아래 중 하나를 설치해 주세요.\n"
    "  · 간편(권장): pip install easyocr    ← 첫 실행 때 모델을 내려받아 1~2분 걸립니다\n"
    "  · 가벼움    : pip install pytesseract 후 Tesseract 본체 설치\n"
    "               https://github.com/UB-Mannheim/tesseract/wiki 에서 설치하고\n"
    "               '한국어(Korean)' 언어 데이터를 함께 선택하세요."
)

# '채무조정3_ad2' 같은 소재명
RX_CREATIVE_NAME = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9]*\d*_ad\d+", re.I)
# '4,200원' / '17,100'
RX_MONEY = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")


class OCRUnavailable(RuntimeError):
    pass


def _lines_easyocr(image_bytes: bytes) -> list[str]:
    import easyocr  # noqa: PLC0415
    reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    # detail=0 → 텍스트만, paragraph=True → 같은 줄끼리 묶어줌
    return [str(t) for t in reader.readtext(image_bytes, detail=0, paragraph=True)]


def _lines_pytesseract(image_bytes: bytes) -> list[str]:
    import pytesseract  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    img = Image.open(io.BytesIO(image_bytes))
    txt = pytesseract.image_to_string(img, lang="kor+eng")
    return [ln for ln in txt.splitlines() if ln.strip()]


def ocr_lines(image_bytes: bytes) -> tuple[list[str], str]:
    """이미지 → 텍스트 줄 목록, 사용한 엔진 이름."""
    errors = []
    for name, fn in (("easyocr", _lines_easyocr), ("pytesseract", _lines_pytesseract)):
        try:
            return fn(image_bytes), name
        except ImportError:
            errors.append(f"{name}: 설치 안 됨")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise OCRUnavailable(INSTALL_HINT + "\n\n시도 기록: " + " / ".join(errors))


def extract_rows(image_bytes: bytes) -> tuple[pd.DataFrame, str, list[str]]:
    """캡처 → (소재/소진 초안 DataFrame, 엔진명, 원본 줄).

    한 줄에서 소재명과 숫자를 찾아 짝지운다. 숫자가 여럿이면 가장 큰 값을 소진으로 본다
    (표에서 소진이 노출/클릭보다 큰 경우가 대부분) - **반드시 사람이 확인해야 한다.**
    """
    lines, engine = ocr_lines(image_bytes)
    rows = []
    for ln in lines:
        name = RX_CREATIVE_NAME.search(ln)
        if not name:
            continue
        after = ln[name.end():]
        nums = [int(m.group().replace(",", "")) for m in RX_MONEY.finditer(after)]
        rows.append({"소재": name.group(), "소진비용": float(max(nums)) if nums else 0.0,
                     "원본줄": ln.strip()})
    df = pd.DataFrame(rows, columns=["소재", "소진비용", "원본줄"])
    if not df.empty:
        df = df.drop_duplicates(subset=["소재"], keep="first").reset_index(drop=True)
    return df, engine, lines
