"""把「视频 → 账号」列表区域的一张截图 + OCR 结果解析成一张张账号卡片。

与具体布局弱耦合，只依赖以下通用特征：
  * 每张卡片左侧是头像（图片），右侧是一列左对齐的文字：第 1 行是账号名，下面是简介/认证等；
  * 卡片与卡片之间的垂直间距大于卡片内部行距。

算法：
  1. 找出「文字列」：各行左边界 x0 聚类，成员最多的那一簇就是账号名/简介所在列；
  2. 在文字列左侧的竖条里做像素分析找头像（连续的非背景色行段），每个头像 = 一张卡片；
     找不到头像时退化为按行间距分组；
  3. 被截图上下边缘截断的卡片丢弃（滚动时有重叠，下一屏会完整出现）。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .ocr import OcrLine, norm_text

# 按钮/无意义文字，不参与解析
NOISE_TEXTS = {
    "关注", "已关注", "+关注", "＋关注", "十关注", "互相关注", "回关", "进入", "私信",
    "更多", "查看更多", "展开", "收起", "…", "...", "···", "•••", "直播中", "直播",
}
# 不可能是账号名的界面文字（列表头部、筛选条等）
UI_TEXTS = {
    "账号", "视频", "全部", "综合", "综合排序", "最新", "最热", "筛选", "排序", "相关账号",
    "视频号", "搜一搜", "搜索", "取消", "加载中", "正在加载", "加载更多", "上拉加载更多",
}
END_MARKERS = ("没有更多", "已经到底", "到底了", "暂无更多", "已显示全部", "没有更多结果",
               "没有找到", "暂无相关", "无相关结果", "暂无结果")


def is_noise(text: str) -> bool:
    t = norm_text(text)
    if not t:
        return True
    if t in NOISE_TEXTS:
        return True
    # 纯符号
    if re.fullmatch(r"[\W_]+", t):
        return True
    return False


@dataclass
class Card:
    name: str
    details: List[str] = field(default_factory=list)
    y0: float = 0.0
    y1: float = 0.0
    name_score: float = 1.0

    @property
    def detail_text(self) -> str:
        return " | ".join(self.details)

    def to_dict(self):
        return {"name": self.name, "details": self.details}


def _median(vals, default=0.0):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else default


def _cluster_1d(vals: Sequence[float], tol: float) -> List[List[int]]:
    """把一维数值按 tol 贪心聚类，返回下标簇。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    clusters: List[List[int]] = []
    for i in order:
        if clusters and vals[i] - vals[clusters[-1][-1]] <= tol:
            clusters[-1].append(i)
        else:
            clusters.append([i])
    return clusters


def background_color(img: np.ndarray) -> np.ndarray:
    """区域内出现最多的（量化后）颜色 = 页面背景色。"""
    small = img[::3, ::3, :3].reshape(-1, 3).astype(np.int32)
    q = (small // 8)
    keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    k = int(vals[np.argmax(counts)])
    mask = keys == k
    return small[mask].mean(axis=0)


def detect_avatars(img: np.ndarray, col_x: float, med_h: float,
                   bg: Optional[np.ndarray] = None) -> Optional[List[Tuple[int, int]]]:
    """在文字列左侧竖条里找头像，返回 [(top, bottom)]；判断为不可靠时返回 None。"""
    H, W = img.shape[:2]
    x1 = int(col_x - max(3, 0.3 * med_h))
    x0 = int(max(0, col_x - 6.0 * med_h))
    if x1 - x0 < max(8, 1.2 * med_h):
        return None
    if bg is None:
        bg = background_color(img)
    strip = img[:, x0:x1, :3].astype(np.int32)
    diff = np.abs(strip - bg.reshape(1, 1, 3)).max(axis=2)
    fg = diff > 28
    act = fg.mean(axis=1)
    active = act >= 0.10
    runs = []
    y = 0
    while y < H:
        if active[y]:
            s = y
            while y < H and active[y]:
                y += 1
            runs.append([s, y])
        else:
            y += 1
    # 合并小缝
    merged: List[List[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 2:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    min_len = 1.6 * med_h
    avatars = [(a, b) for a, b in merged if b - a >= min_len]
    if not avatars:
        return None
    # 头像高度应该大致一致；出现特别高的块（横幅图等）说明这不是头像列
    full = [b - a for a, b in avatars if a > 1 and b < H - 1]
    if full:
        ref = _median(full)
        if ref > 9 * med_h:
            return None
        if any((b - a) > ref * 1.6 + 4 for a, b in avatars):
            return None
    return avatars


def _group_by_gaps(lines: List[OcrLine], med_h: float) -> Tuple[List[List[OcrLine]], float]:
    lines = sorted(lines, key=lambda l: l.y0)
    if not lines:
        return [], med_h
    gaps = [lines[i + 1].y0 - lines[i].y1 for i in range(len(lines) - 1)]
    thr = 0.9 * med_h
    if len(gaps) >= 2:
        g = sorted(gaps)
        lo, hi = g[0], g[-1]
        if hi - lo > 0.5 * med_h:
            # 1 维 2-means
            c1, c2 = lo, hi
            for _ in range(20):
                a = [x for x in g if abs(x - c1) <= abs(x - c2)]
                b = [x for x in g if abs(x - c1) > abs(x - c2)]
                if not a or not b:
                    break
                c1, c2 = sum(a) / len(a), sum(b) / len(b)
            thr = max(0.35 * med_h, (c1 + c2) / 2)
    groups = [[lines[0]]]
    for i, gap in enumerate(gaps):
        if gap > thr:
            groups.append([lines[i + 1]])
        else:
            groups[-1].append(lines[i + 1])
    return groups, thr


def _clean_name(text: str) -> str:
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    # 去掉 OCR 识别出的尾部小图标字符
    t = re.sub(r"[\s·•|｜>›»]+$", "", t)
    return t


def line_contrast(img: np.ndarray, l: OcrLine, bg: np.ndarray) -> float:
    """文字「墨色」与背景的反差：账号名通常是黑/白色，简介是灰色。"""
    H, W = img.shape[:2]
    x0, y0 = max(0, int(l.x0)), max(0, int(l.y0))
    x1, y1 = min(W, int(l.x1) + 1), min(H, int(l.y1) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    patch = img[y0:y1, x0:x1, :3].astype(np.int32)
    d = np.abs(patch - bg.reshape(1, 1, 3)).max(axis=2).ravel()
    ink = d[d > 40]
    if ink.size < 5:
        return 0.0
    k = max(1, int(ink.size * 0.3))
    return float(np.sort(ink)[-k:].mean())


def _two_means(vals: Sequence[float]) -> Tuple[float, float, float]:
    """1 维 2-means，返回 (低簇中心, 高簇中心, 分界)。"""
    v = sorted(vals)
    c1, c2 = v[0], v[-1]
    for _ in range(30):
        mid = (c1 + c2) / 2
        a = [x for x in v if x <= mid]
        b = [x for x in v if x > mid]
        if not a or not b:
            break
        n1, n2 = sum(a) / len(a), sum(b) / len(b)
        if (n1, n2) == (c1, c2):
            break
        c1, c2 = n1, n2
    return c1, c2, (c1 + c2) / 2


def classify_names(img: np.ndarray, col_lines: List[OcrLine], bg: np.ndarray) -> Optional[List[bool]]:
    """判断每行是不是「账号名」行（更黑或更大的字）。区分不开时返回 None。"""
    if len(col_lines) < 3:
        return None
    con = [line_contrast(img, l, bg) for l in col_lines]
    lo, hi, cut = _two_means(con)
    if hi - lo >= 45 and sum(1 for c in con if c > cut) >= 1:
        return [c > cut for c in con]
    hs = [l.h for l in col_lines]
    lo, hi, cut = _two_means(hs)
    if lo > 0 and hi / lo >= 1.15:
        return [h > cut for h in hs]
    return None


def parse_cards(lines: Sequence[OcrLine], img: np.ndarray,
                top_hard: bool = False, bottom_hard: bool = False,
                min_score: float = 0.5) -> List[Card]:
    """lines 的坐标必须是相对 img（列表区域截图）的坐标。"""
    H, W = img.shape[:2]
    good = [l for l in lines if l.score >= min_score and not is_noise(l.text)]
    good = [l for l in good if not any(m in norm_text(l.text) for m in END_MARKERS)]
    if not good:
        return []
    med_h = _median([l.h for l in good], 16.0)
    tol = max(6.0, 0.8 * med_h)

    # ---- 1. 文字列 ----
    xs = [l.x0 for l in good]
    clusters = _cluster_1d(xs, tol)
    clusters.sort(key=lambda c: (-len(c), _median([xs[i] for i in c])))
    col = clusters[0]
    col_x = _median([xs[i] for i in col])
    col_lines = [l for l in good if abs(l.x0 - col_x) <= tol]
    side_lines = [l for l in good if abs(l.x0 - col_x) > tol and l.x0 > col_x]
    col_lines.sort(key=lambda l: l.y0)
    bg = background_color(img)
    flags = classify_names(img, col_lines, bg)
    is_name = {id(l): f for l, f in zip(col_lines, flags)} if flags else None

    cards: List[Card] = []

    # ---- 2a. 头像模式 ----
    avatars = detect_avatars(img, col_x, med_h, bg)
    if avatars:
        # 相邻头像之间，用文字行之间最大的空隙作为卡片分界
        bounds = []
        for i in range(len(avatars) - 1):
            a_top, a_bot = avatars[i]
            n_top, n_bot = avatars[i + 1]
            a_mid = (a_top + a_bot) / 2.0
            n_h = n_bot - n_top
            # 取本头像中心 ~ 下一头像中心之间的所有行（要包含下一张卡片的名字行）
            between = [l for l in col_lines if a_top - 0.5 * med_h <= l.cy <= n_bot]
            cut = (a_bot + n_top) / 2.0
            best_gap = -1.0
            for j in range(len(between) - 1):
                g0, g1 = between[j].y1, between[j + 1].y0
                mid = (g0 + g1) / 2
                if a_mid < mid < n_top + 0.25 * n_h and g1 - g0 > best_gap:
                    best_gap = g1 - g0
                    cut = mid
            bounds.append(cut)
        first_top = avatars[0][0] - 0.8 * med_h
        edges = [first_top] + bounds + [float(H) + 1]
        for i, (a_top, a_bot) in enumerate(avatars):
            lo, hi = edges[i], edges[i + 1]
            members = [l for l in col_lines if lo <= l.cy < hi]
            partial = False
            if a_top <= 1 or a_bot >= H - 1:
                partial = True
            if members and members[0].y0 <= 1:
                partial = True
            # 第一张卡片：头像上方留白不足时，名字行可能被整行截掉（OCR 根本看不到）
            if i == 0 and not top_hard and a_top < 1.2 * med_h:
                partial = True
            if i == len(avatars) - 1 and not bottom_hard and H - a_bot < 1.2 * med_h:
                partial = True
            if i == len(avatars) - 1 and not bottom_hard:
                last_bottom = max([a_bot] + [l.y1 for l in members])
                if last_bottom + 0.8 * med_h > H:
                    partial = True
            if members and is_name is not None and not is_name[id(members[0])]:
                partial = True       # 第一行不是名字样式 => 名字被截掉了
            if partial or not members:
                continue
            sides = [l for l in side_lines if lo <= l.cy < hi]
            cards.append(_make_card(members, sides))
        return [c for c in cards if c is not None]

    # ---- 2b. 没有头像：按「名字样式」分组，其次按行间距分组 ----
    if is_name is not None:
        groups: List[List[OcrLine]] = []
        lead: List[OcrLine] = []
        for l in col_lines:
            if is_name[id(l)]:
                groups.append([l])
            elif groups:
                groups[-1].append(l)
            else:
                lead.append(l)          # 第一个名字之前的行属于被截断的上一张卡片
        for gi, grp in enumerate(groups):
            y0 = min(l.y0 for l in grp)
            y1 = max(l.y1 for l in grp)
            if gi == 0 and not top_hard and y0 < 0.5 * med_h:
                continue
            if gi == len(groups) - 1 and not bottom_hard and H - y1 < 1.5 * med_h:
                continue
            sides = [l for l in side_lines if y0 - 0.3 * med_h <= l.cy <= y1 + 0.3 * med_h]
            cards.append(_make_card(grp, sides))
        return [c for c in cards if c is not None]

    groups, thr = _group_by_gaps(col_lines, med_h)
    for gi, grp in enumerate(groups):
        y0 = min(l.y0 for l in grp)
        y1 = max(l.y1 for l in grp)
        if gi == 0 and not top_hard and y0 < thr + 2:
            continue
        if gi == len(groups) - 1 and not bottom_hard and H - y1 < thr + 2:
            continue
        sides = [l for l in side_lines if y0 - 0.3 * med_h <= l.cy <= y1 + 0.3 * med_h]
        cards.append(_make_card(grp, sides))
    return [c for c in cards if c is not None]


def _make_card(members: List[OcrLine], sides: List[OcrLine]) -> Optional[Card]:
    members = sorted(members, key=lambda l: l.y0)
    name_line = members[0]
    name = _clean_name(name_line.text)
    if not name or norm_text(name) in UI_TEXTS:
        return None
    details = [m.text.strip() for m in members[1:]]
    # 同一行右侧的文字（如粉丝数）放到最前面
    for s in sorted(sides, key=lambda l: (l.y0, l.x0)):
        details.append(s.text.strip())
    y0 = min(l.y0 for l in members)
    y1 = max(l.y1 for l in members)
    return Card(name=name, details=[d for d in details if d], y0=y0, y1=y1,
                name_score=name_line.score)


# ----------------------------------------------------------------------
class Collector:
    """跨屏去重、保持出现顺序。

    两种去重依据：
      1. 位置：滚动距离已知时，本屏卡片可映射到上一屏同一位置的卡片（不受 OCR 误差影响）；
      2. 文本：名字相同 / 名字+简介高度相似。
    同一账号的多次识别结果做投票，最终取出现次数最多（其次置信度最高）的名字。
    """

    def __init__(self):
        self.items: List[Card] = []
        self._keys: List[str] = []
        self._votes: List[dict] = []

    @staticmethod
    def _key(c: Card) -> str:
        return norm_text(c.name).lower()

    def _find(self, c: Card) -> int:
        k = self._key(c)
        det = norm_text(c.detail_text)
        start = max(0, len(self.items) - 60)
        for i in range(len(self.items) - 1, start - 1, -1):
            ok = self._keys[i]
            odet = norm_text(self.items[i].detail_text)
            if ok == k or any(v == k for v in self._votes[i]):
                if not det or not odet:
                    return i
                if difflib.SequenceMatcher(None, det, odet).ratio() >= 0.5:
                    return i
                continue
            nr = difflib.SequenceMatcher(None, k, ok).ratio()
            if len(k) >= 3 and nr >= 0.6:
                if det and odet and len(det) >= 4:
                    if difflib.SequenceMatcher(None, det, odet).ratio() >= 0.85:
                        return i
                elif nr >= 0.8 and not det and not odet:
                    return i
        for i in range(start - 1, -1, -1):
            if self._keys[i] == k:
                odet = norm_text(self.items[i].detail_text)
                if not det or not odet or difflib.SequenceMatcher(None, det, odet).ratio() >= 0.5:
                    return i
        return -1

    def _merge(self, i: int, c: Card):
        old = self.items[i]
        k = self._key(c)
        v = self._votes[i]
        cnt, best = v.get(k, (0, 0.0, c.name))[:2]
        v[k] = (cnt + 1, max(best, c.name_score), c.name)
        win_key = max(v, key=lambda kk: (v[kk][0], v[kk][1]))
        name = v[win_key][2]
        details = c.details if len(c.details) > len(old.details) else old.details
        self.items[i] = Card(name=name, details=details, y0=c.y0, y1=c.y1,
                             name_score=max(old.name_score, c.name_score))
        self._keys[i] = win_key

    def add_card(self, c: Card, hint: Optional[int] = None) -> Tuple[int, bool]:
        """返回 (下标, 是否新增)。hint = 按位置推算出的已有条目下标。"""
        i = hint if hint is not None and 0 <= hint < len(self.items) else self._find(c)
        if i >= 0:
            self._merge(i, c)
            return i, False
        self.items.append(c)
        self._keys.append(self._key(c))
        self._votes.append({self._key(c): (1, c.name_score, c.name)})
        return len(self.items) - 1, True

    def add(self, cards: Sequence[Card]) -> int:
        return sum(1 for c in cards if self.add_card(c)[1])
