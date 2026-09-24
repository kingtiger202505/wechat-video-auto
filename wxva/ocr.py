"""OCR 封装（RapidOCR，纯本地离线，中文效果好）。

统一输出 OcrLine 列表，坐标为「传入图片」内的像素坐标，并尽量给出逐字框，
这样即使 OCR 把一排 tab（"全部文章视频公众号"）识别成一整行，
也能精确算出「视频」两个字的位置去点击。

支持两套包（任选其一安装即可）：
  * rapidocr >= 3.x          （推荐，支持逐字框，支持 Python 3.8 ~ 3.13）
  * rapidocr_onnxruntime 1.x （旧版，只有行框，逐字位置按比例估算）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

Box = Tuple[float, float, float, float]  # x0, y0, x1, y1

log = logging.getLogger("wxva.ocr")


def _quad_to_box(quad) -> Box:
    arr = np.asarray(quad, dtype=float).reshape(-1, 2)
    return (float(arr[:, 0].min()), float(arr[:, 1].min()),
            float(arr[:, 0].max()), float(arr[:, 1].max()))


def norm_text(s: str) -> str:
    """去掉所有空白，用于比较。"""
    return "".join((s or "").split())


@dataclass
class OcrLine:
    text: str
    box: Box
    score: float
    # 逐字（或逐词）框：[(字符串, box)]，拼起来 == text（已去空白）
    chars: List[Tuple[str, Box]] = field(default_factory=list)

    @property
    def x0(self): return self.box[0]

    @property
    def y0(self): return self.box[1]

    @property
    def x1(self): return self.box[2]

    @property
    def y1(self): return self.box[3]

    @property
    def cx(self): return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self): return (self.box[1] + self.box[3]) / 2

    @property
    def h(self): return self.box[3] - self.box[1]

    @property
    def w(self): return self.box[2] - self.box[0]

    def offset(self, dx: float, dy: float) -> "OcrLine":
        def mv(b):
            return (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
        return OcrLine(self.text, mv(self.box), self.score,
                       [(c, mv(b)) for c, b in self.chars])

    def find_all(self, target: str) -> List[Box]:
        """返回 target 在本行中每次出现的框（基于逐字框，没有则按比例估算）。"""
        t = norm_text(target)
        if not t:
            return []
        units = self.chars or []
        joined = "".join(u for u, _ in units)
        out: List[Box] = []
        if units and joined == norm_text(self.text):
            # 建立 字符下标 -> 单元下标 映射
            idx_map = []
            for ui, (u, _) in enumerate(units):
                idx_map.extend([ui] * len(u))
            start = joined.find(t)
            while start >= 0:
                u0, u1 = idx_map[start], idx_map[start + len(t) - 1]
                bs = [units[k][1] for k in range(u0, u1 + 1)]
                out.append((min(b[0] for b in bs), min(b[1] for b in bs),
                            max(b[2] for b in bs), max(b[3] for b in bs)))
                start = joined.find(t, start + 1)
            return out
        # 按比例估算
        txt = norm_text(self.text)
        n = max(len(txt), 1)
        start = txt.find(t)
        while start >= 0:
            x0 = self.x0 + self.w * start / n
            x1 = self.x0 + self.w * (start + len(t)) / n
            out.append((x0, self.y0, x1, self.y1))
            start = txt.find(t, start + 1)
        return out

    def char_after(self, target: str, occurrence_box: Box) -> str:
        """target 在本行中某次出现后面紧跟的那个字符（用于排除「视频号」）。"""
        txt = norm_text(self.text)
        t = norm_text(target)
        boxes = self.find_all(t)
        starts = []
        s = txt.find(t)
        while s >= 0:
            starts.append(s)
            s = txt.find(t, s + 1)
        for st, b in zip(starts, boxes):
            if b == occurrence_box:
                k = st + len(t)
                return txt[k] if k < len(txt) else ""
        return ""

    def to_dict(self):
        return {"text": self.text, "box": [round(v, 1) for v in self.box],
                "score": round(self.score, 3)}


class OcrEngine:
    def __init__(self, upscale: Optional[float] = None):
        """upscale: OCR 前把图片放大的倍数；None 表示自动（小字放大 2 倍，提高准确率）。"""
        self.upscale = upscale
        self.kind = None
        self._eng = None
        err_msgs = []
        try:
            from rapidocr import RapidOCR  # type: ignore
            try:
                self._eng = RapidOCR(params={"Global.log_level": "error",
                                             "Global.use_cls": False})
            except Exception:
                self._eng = RapidOCR()
            self.kind = "rapidocr"
        except Exception as e:  # pragma: no cover - 依赖环境
            err_msgs.append("rapidocr: %r" % (e,))
        if self._eng is None:
            try:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore
                self._eng = RapidOCR()
                self.kind = "rapidocr_onnxruntime"
            except Exception as e:  # pragma: no cover
                err_msgs.append("rapidocr_onnxruntime: %r" % (e,))
        if self._eng is None:
            raise RuntimeError(
                "没有可用的 OCR 引擎，请先执行: pip install -r requirements.txt\n"
                + "\n".join(err_msgs))
        for name in ("RapidOCR", "rapidocr"):
            logging.getLogger(name).setLevel(logging.ERROR)

    # ------------------------------------------------------------------
    def recognize(self, img: Image.Image, scale: Optional[float] = None) -> List[OcrLine]:
        if img.mode != "RGB":
            img = img.convert("RGB")
        s = scale if scale is not None else (self.upscale or 1.0)
        if s and abs(s - 1.0) > 1e-3:
            big = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                             Image.LANCZOS)
        else:
            s = 1.0
            big = img
        arr = np.asarray(big)[:, :, ::-1].copy()  # RGB -> BGR
        lines = self._run(arr)
        if s != 1.0:
            lines = [_scale_line(l, 1.0 / s) for l in lines]
        lines.sort(key=lambda l: (round(l.cy), l.x0))
        return lines

    def _run(self, arr: np.ndarray) -> List[OcrLine]:
        if self.kind == "rapidocr":
            try:
                r = self._eng(arr, return_word_box=True, return_single_char_box=True)
            except TypeError:
                r = self._eng(arr)
            boxes = getattr(r, "boxes", None)
            txts = getattr(r, "txts", None)
            scores = getattr(r, "scores", None)
            words = getattr(r, "word_results", None)
            if boxes is None or txts is None:
                return []
            out = []
            for i, (quad, txt, sc) in enumerate(zip(boxes, txts, scores)):
                chars = []
                if words is not None and i < len(words) and words[i]:
                    for unit in words[i]:
                        try:
                            u_txt, u_quad = unit[0], unit[2]
                        except Exception:
                            continue
                        u_txt = norm_text(u_txt)
                        if u_txt and u_quad is not None:
                            chars.append((u_txt, _quad_to_box(u_quad)))
                    if "".join(c for c, _ in chars) != norm_text(txt):
                        chars = []
                out.append(OcrLine(str(txt), _quad_to_box(quad), float(sc), chars))
            return out
        # rapidocr_onnxruntime 1.x
        res = self._eng(arr)
        result = res[0] if isinstance(res, tuple) else res
        out = []
        for item in result or []:
            quad, txt, sc = item[0], item[1], item[2]
            out.append(OcrLine(str(txt), _quad_to_box(quad), float(sc), []))
        return out


def _scale_line(l: OcrLine, k: float) -> OcrLine:
    def sc(b):
        return (b[0] * k, b[1] * k, b[2] * k, b[3] * k)
    return OcrLine(l.text, sc(l.box), l.score, [(c, sc(b)) for c, b in l.chars])


def lines_text(lines: Sequence[OcrLine]) -> str:
    return "\n".join(l.text for l in lines)
