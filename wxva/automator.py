"""核心流程：搜索关键词 -> 打开搜一搜结果页 -> 视频 tab -> 账号 -> 滚动采集全部账号。

所有「找按钮」都靠截图 + OCR 定位文字（不写死坐标），每一步都有超时和校验，
失败时给出明确原因，并把截图/OCR 结果保存到 debug 目录方便排查。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .ocr import OcrEngine, OcrLine, norm_text
from .parser import END_MARKERS, Card, Collector, parse_cards

log = logging.getLogger("wxva")

TAB_WORDS = ["全部", "文章", "公众号", "小程序", "视频", "直播", "百科", "新闻", "音乐",
             "表情", "图片", "朋友圈", "问一问", "读书", "听书", "商品", "服务", "小说",
             "游戏", "综合", "账号", "动态", "话题", "用户", "最新", "最热"]
MATCH_ERR_MAX = 0.25  # 滚动前后重叠区域的归一化误差上限（0=完全吻合，1=毫不相干）
ENTRY_WORDS = ["搜一搜", "搜索网络结果", "网络结果", "搜索网络", "网络搜索", "网络查找"]


class StepError(Exception):
    pass


@dataclass
class Config:
    keyword: str
    out_dir: str
    max_pages: int = 400          # 最多滚动多少屏
    max_accounts: int = 0         # 0 = 不限
    scroll_wait: float = 1.2      # 每次滚动后等待（懒加载）
    init_notches: int = 2
    stall_limit: int = 4          # 连续多少次滚不动判定到底
    page_timeout: float = 25.0    # 等待搜索页出现
    debug: bool = False           # 保存每一屏截图
    start_from: str = "main"      # main / tabs / list
    ocr_scale: Optional[float] = None


@dataclass
class Target:
    win: object                   # WinInfo
    tabs_bottom: float = 0.0      # 账号 tab 底部 y（屏幕坐标）


def _kw_seen(lines: Sequence[OcrLine], kw: str) -> bool:
    k = norm_text(kw)
    if not k:
        return False
    for l in lines:
        t = norm_text(l.text)
        if k in t:
            return True
        if len(k) >= 2 and t:
            m = difflib.SequenceMatcher(None, k, t).find_longest_match(0, len(k), 0, len(t))
            if m.size >= max(2, int(0.7 * len(k) + 0.5)):
                return True
    return False


def _row_text(lines: Sequence[OcrLine], ref: OcrLine) -> str:
    tol = max(4.0, 0.6 * ref.h)
    row = sorted([l for l in lines if abs(l.cy - ref.cy) <= tol], key=lambda l: l.x0)
    return "".join(norm_text(l.text) for l in row)


def find_tab(lines: Sequence[OcrLine], word: str, y_range: Tuple[float, float],
             keyword: str = "", forbid_next: str = "", min_score: int = 1
             ) -> Optional[Tuple[float, float, float, float, int]]:
    """在 OCR 结果里找 tab 文字，返回 (cx, cy, h, y1, score)。

    打分：同一行里出现的其它 tab 词越多越像 tab 栏；整行就是这个词再加 1 分。
    """
    kw = norm_text(keyword)
    best = None
    for l in lines:
        if not (y_range[0] <= l.cy <= y_range[1]):
            continue
        t = norm_text(l.text)
        if kw and kw in t and t != word:
            continue                      # 搜索框里的关键词
        for b in l.find_all(word):
            if forbid_next and l.char_after(word, b) and l.char_after(word, b) in forbid_next:
                continue
            row = _row_text(lines, l)
            score = sum(1 for tw in TAB_WORDS if tw != word and tw in row)
            if len(t) <= len(word) + 1:
                score += 1
            cand = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, b[3] - b[1], b[3], score)
            if best is None or (score, -cand[1]) > (best[4], -best[1]):
                best = cand
    if best is not None and best[4] >= min_score:
        return best
    return None


def estimate_shift(prev: Image.Image, cur: Image.Image) -> Tuple[int, float, float]:
    """纯像素估算内容向上移动了多少像素。

    返回 (shift, 归一化误差, raw_diff)；raw_diff（整图平均灰度差）很小说明根本没滚动。
    """
    a = np.asarray(prev.convert("L"), dtype=np.float32)[::2, ::2]
    b = np.asarray(cur.convert("L"), dtype=np.float32)[::2, ::2]
    h = min(a.shape[0], b.shape[0])
    a, b = a[:h], b[:h]
    raw = float(np.abs(a - b).mean())
    bg = float(np.median(a))
    ra = np.abs(a - bg).sum(axis=1)       # 每行的「内容量」
    rb = np.abs(b - bg).sum(axis=1)
    ca = np.concatenate([[0.0], np.cumsum(ra)])
    cb = np.concatenate([[0.0], np.cumsum(rb)])
    min_ov = max(8, int(h * 0.12))
    best_s, best_e = 0, float("inf")
    for s in range(0, h - min_ov):
        num = float(np.abs(a[s:] - b[:h - s]).sum())
        den = (ca[h] - ca[s]) + (cb[h - s] - cb[0])
        e = num / den if den > 1e-6 else (0.0 if num < 1e-6 else 1.0)
        if e < best_e:
            best_s, best_e = s, e
    return best_s * 2, best_e, raw


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def overlap_err(prev: Image.Image, cur: Image.Image, shift: float) -> float:
    """假设内容向上移动了 shift 像素，两图重叠部分的「归一化」误差。

    只统计非背景像素：完全吻合≈0，毫不相干≈1（白底页面上普通平均误差会被大片空白稀释，不可靠）。
    """
    a, b = _gray(prev), _gray(cur)
    h = min(a.shape[0], b.shape[0])
    s = int(round(shift))
    if s < 0 or h - s < max(8, int(h * 0.08)):
        return float("inf")
    pa, pb = a[s:h], b[:h - s]
    bg = float(np.median(a))
    denom = float(np.abs(pa - bg).sum() + np.abs(pb - bg).sum())
    if denom < 1e-6:
        return 0.0 if float(np.abs(pa - pb).sum()) < 1e-6 else 1.0
    return float(np.abs(pa - pb).sum()) / denom


def text_shift_candidates(prev_lines: Sequence[OcrLine], cur_lines: Sequence[OcrLine]) -> List[float]:
    """两屏中相同文字行的 y 差值（候选滚动距离），按支持数从多到少排序。"""
    def uniq(lines):
        m = {}
        for x in lines:
            t = norm_text(x.text)
            if len(t) >= 4:
                m.setdefault(t, []).append(x.y0)
        return {k: v[0] for k, v in m.items() if len(v) == 1}
    pm, cm = uniq(prev_lines), uniq(cur_lines)
    ds = sorted(pm[t] - cm[t] for t in cm if t in pm)
    ds = [d for d in ds if d > 2]
    groups = []
    for d in ds:
        if groups and d - groups[-1][-1] <= 4:
            groups[-1].append(d)
        else:
            groups.append([d])
    groups.sort(key=lambda g: -len(g))
    return [sum(g) / len(g) for g in groups]


def determine_shift(prev: Image.Image, cur: Image.Image, prev_lines, cur_lines
                    ) -> Tuple[Optional[float], float]:
    """综合文字匹配和像素匹配确定滚动距离，返回 (shift 或 None, 误差)。"""
    img_s, _e, _raw = estimate_shift(prev, cur)
    cands = text_shift_candidates(prev_lines, cur_lines)
    if img_s > 0:
        cands.append(float(img_s))
    best, best_err = None, float("inf")
    for c in cands:
        # 文字框精度约 ±2px，在附近微调
        e, c = min((overlap_err(prev, cur, c + dd), c + dd) for dd in (-2, -1, 0, 1, 2))
        if e < best_err:
            best, best_err = c, e
    if best is not None and best_err <= MATCH_ERR_MAX:
        return float(best), best_err
    return None, best_err


class Automator:
    def __init__(self, plat, ocr: OcrEngine, cfg: Config):
        self.p = plat
        self.ocr = ocr
        self.cfg = cfg
        self.dbg_dir = os.path.join(cfg.out_dir, "debug")
        os.makedirs(self.dbg_dir, exist_ok=True)
        self._shot_no = 0
        self.collector = Collector()
        self.on_progress: Optional[Callable[[List[Card]], None]] = None
        self.kick: Callable[[], None] = lambda: None   # 看门狗喂狗

    # ------------------------------------------------------------------ 工具
    def _ocr_scale(self, win) -> float:
        if self.cfg.ocr_scale:
            return self.cfg.ocr_scale
        dpi = 96.0 * self.p.scale(win)
        return max(1.0, min(2.0, 192.0 / dpi))

    def shot(self, win, rect, tag: str, save: bool = True):
        """截图 + OCR，返回 (图片, 屏幕坐标的 OCR 行)。"""
        self.p.check_abort()
        self.kick()
        l, t, r, b = [int(v) for v in rect]
        img = self.p.capture((l, t, r, b))
        lines = self.ocr.recognize(img, scale=self._ocr_scale(win))
        lines = [x.offset(l, t) for x in lines]
        if save:
            self._save_debug(img, lines, tag)
        return img, lines

    def _save_debug(self, img: Image.Image, lines, tag: str):
        self._shot_no += 1
        base = os.path.join(self.dbg_dir, "%03d_%s" % (self._shot_no, tag))
        try:
            img.save(base + ".png")
            with open(base + ".json", "w", encoding="utf-8") as f:
                json.dump([x.to_dict() for x in lines], f, ensure_ascii=False, indent=1)
        except Exception as e:  # 调试文件写失败不影响主流程
            log.debug("save debug failed: %r", e)

    def _front(self, win):
        if not self.p.is_front(win):
            if not self.p.focus(win):
                raise StepError("窗口无法切到前台：%s" % getattr(win, "title", win))
            self.p.sleep(0.3)

    # ------------------------------------------------------------------ 流程
    def run(self) -> List[Card]:
        kw = self.cfg.keyword
        if self.cfg.start_from == "main":
            log.info("【1/5】唤起微信主窗口")
            main = self.p.bring_up_main()
            log.info("【2/5】在主窗口搜索框输入关键词：%s", kw)
            # 只记可见窗口：隐藏窗口被复用显示出来也算「新出现」
            before = {x.hwnd for x in self.p.list_windows() if x.visible}
            self.type_keyword(main)
            log.info("【3/5】打开搜一搜结果页")
            self.trigger_search(main)
            target = self.wait_search_page(main, before)
        else:
            target = self.pick_existing_search_window()
        if self.cfg.start_from in ("main", "tabs"):
            log.info("【4/5】切换到 视频 → 账号")
            tabs_bottom = self.switch_tabs(target)
        else:
            tabs_bottom = self.locate_list_top(target)
        log.info("【5/5】滚动采集账号列表")
        return self.collect(target, tabs_bottom)

    # ---- 2. 输入关键词 -------------------------------------------------
    def _search_area(self, main):
        l, t, r, b = main.rect
        s = self.p.scale(main)
        return (l, t, min(r, l + int(560 * s)), min(b, t + int(110 * s)))

    def type_keyword(self, main):
        kw = self.cfg.keyword
        s = self.p.scale(main)
        old_clip = self.p.get_clipboard()

        def locate_by_ocr():
            _img, lines = self.shot(main, self._search_area(main), "search_box")
            cands = [x for x in lines if norm_text(x.text).startswith("搜索")
                     and len(norm_text(x.text)) <= 6]
            if not cands:
                return None
            c = min(cands, key=lambda x: x.cy)
            return (c.cx, c.cy)

        l, t = main.rect[0], main.rect[1]
        strategies = [
            ("OCR 定位「搜索」框", locate_by_ocr),
            ("默认位置 A", lambda: (l + 160 * s, t + 38 * s)),
            ("默认位置 B", lambda: (l + 170, t + 50)),
            ("快捷键 Ctrl+F", None),
        ]
        try:
            for name, fn in strategies:
                self._front(main)
                if fn is None:
                    self.p.ctrl_f()
                else:
                    pt = fn()
                    if pt is None:
                        log.info("  %s：未找到", name)
                        continue
                    log.info("  %s：点击 (%d, %d)", name, pt[0], pt[1])
                    self.p.click(pt[0], pt[1])
                self.p.sleep(0.5)
                self.p.select_all()
                self.p.sleep(0.1)
                self.p.paste_text(kw)
                self.p.sleep(1.0)
                _img, lines = self.shot(main, self._search_area(main), "typed")
                if _kw_seen(lines, kw):
                    log.info("  搜索框已输入关键词 ✓")
                    return
                log.info("  %s：搜索框中没看到关键词，换下一种方式", name)
        finally:
            if old_clip is not None:
                # 稍后恢复用户原来的剪贴板（等微信读完）
                self.p.sleep(0.2)
                self.p.set_clipboard(old_clip)
        raise StepError("无法在微信搜索框中输入关键词（详见 debug 目录截图）")

    # ---- 3. 打开搜一搜 -------------------------------------------------
    def trigger_search(self, main):
        s = self.p.scale(main)
        end = time.time() + 6
        while time.time() < end:
            m = self.p.refresh(main) or main
            l, t, r, b = m.rect
            # 下拉框可能是独立弹出窗口，直接截屏幕区域即可
            rect = (l, t, min(r, l + int(760 * s)), b)
            _img, lines = self.shot(m, rect, "dropdown")
            hits = []
            for x in lines:
                txt = norm_text(x.text)
                for i, wd in enumerate(ENTRY_WORDS):
                    if wd in txt and x.cy > t + 30 * s:
                        hits.append((i, x.cy, x))
                        break
            if hits:
                hits.sort(key=lambda h: (h[0], h[1]))
                x = hits[0][2]
                log.info("  找到下拉项「%s」，点击", x.text)
                self._front(m)
                self.p.click(x.cx, x.cy)
                return
            self.p.sleep(0.7)
        # 没找到下拉项：焦点已确认在搜索框中（type_keyword 校验过），按回车是安全的
        log.info("  下拉框中没有「搜一搜」项，按回车")
        self.p.enter()

    def _is_search_page(self, win) -> Optional[float]:
        l, t, r, b = win.rect
        _img, lines = self.shot(win, win.rect, "probe_page")
        tab = find_tab(lines, "视频", (t, t + (b - t) * 0.5), self.cfg.keyword,
                       forbid_next="号", min_score=2)
        return tab[1] if tab else None

    def wait_search_page(self, main, before) -> Target:
        end = time.time() + self.cfg.page_timeout
        while time.time() < end:
            self.p.sleep(1.0)
            wins = [x for x in self.p.list_windows() if x.visible and x.width > 300 and x.height > 300]
            cands = []
            for x in wins:
                title_hit = ("搜" in (x.title or ""))
                is_new = x.hwnd not in before
                if x.hwnd == main.hwnd:
                    continue
                if title_hit or is_new:
                    cands.append((0 if title_hit else 1, -x.area, x))
            cands.sort(key=lambda c: (c[0], c[1]))
            for _a, _b, x in cands:
                log.info("  候选搜索窗口: %s", x.short() if hasattr(x, "short") else x)
                self.p.focus(x)
                self.p.sleep(0.4)
                x = self.p.refresh(x) or x
                if self._is_search_page(x) is not None:
                    return self._prepare_target(x)
            # 搜索页可能直接嵌在主窗口里
            m = self.p.refresh(main) or main
            if self._is_search_page(m) is not None:
                return self._prepare_target(m)
        raise StepError("等待 %d 秒仍未出现搜一搜结果页（找不到「视频」tab）" % self.cfg.page_timeout)

    def _prepare_target(self, win) -> Target:
        s = self.p.scale(win)
        self._front(win)
        win = self.p.ensure_on_screen(win, int(700 * s), int(600 * s))
        log.info("  搜索结果页窗口: %s", win.short() if hasattr(win, "short") else win)
        l, t, r, b = win.rect
        _img, lines = self.shot(win, (l, t, r, t + int((b - t) * 0.35)), "page_top")
        if not _kw_seen(lines, self.cfg.keyword):
            log.warning("  注意：结果页顶部没识别到关键词「%s」，可能是旧的搜索页，请留意结果", self.cfg.keyword)
        return Target(win=win)

    def pick_existing_search_window(self) -> Target:
        """--start-from tabs/list：使用已经打开的搜一搜窗口。"""
        wins = [x for x in self.p.list_windows() if x.visible and x.width > 300 and x.height > 300]
        wins.sort(key=lambda x: (0 if "搜" in (x.title or "") else 1,
                                 1 if (x.title or "") in ("微信", "Weixin", "WeChat") else 0,
                                 -x.area))
        for x in wins:
            self.p.focus(x)
            self.p.sleep(0.4)
            x = self.p.refresh(x) or x
            if self.cfg.start_from == "list" or self._is_search_page(x) is not None:
                return self._prepare_target(x)
        raise StepError("没有找到已打开的搜一搜结果页窗口")

    # ---- 4. 切 tab -----------------------------------------------------
    def switch_tabs(self, target: Target) -> float:
        win = target.win
        kw = self.cfg.keyword
        for attempt in range(3):
            win = self.p.refresh(win) or win
            target.win = win
            l, t, r, b = win.rect
            self._front(win)
            _img, lines = self.shot(win, win.rect, "tabs_before")
            vt = find_tab(lines, "视频", (t, t + (b - t) * 0.5), kw, forbid_next="号", min_score=1)
            if vt is None:
                self.p.sleep(1.5)
                continue
            log.info("  点击「视频」tab (%d, %d)", vt[0], vt[1])
            self.p.click(vt[0], vt[1])
            # 等「账号」出现
            end = time.time() + 10
            while time.time() < end:
                self.p.sleep(1.0)
                _img, lines = self.shot(win, win.rect, "tabs_video")
                at = find_tab(lines, "账号", (vt[1] - vt[2] * 0.6, t + (b - t) * 0.6), kw, min_score=1)
                if at is not None:
                    log.info("  点击「账号」tab (%d, %d)", at[0], at[1])
                    self._front(win)
                    self.p.click(at[0], at[1])
                    self.p.sleep(1.5)
                    self._wait_stable(win, (l, int(at[3]), r, b))
                    return max(at[3], vt[3])
            log.info("  没找到「账号」，重试点击「视频」")
        raise StepError("没有找到「视频」/「账号」tab（详见 debug 目录截图）")

    def locate_list_top(self, target: Target) -> float:
        win = target.win
        l, t, r, b = win.rect
        _img, lines = self.shot(win, win.rect, "list_locate")
        at = find_tab(lines, "账号", (t, t + (b - t) * 0.6), self.cfg.keyword, min_score=1)
        if at is not None:
            return at[3]
        return t + (b - t) * 0.2

    def _wait_stable(self, win, rect, timeout: float = 8.0):
        prev = self.p.capture(rect)
        end = time.time() + timeout
        while time.time() < end:
            self.p.sleep(0.8)
            cur = self.p.capture(rect)
            if cur.size == prev.size:
                d = float(np.abs(np.asarray(cur.convert("L"), np.float32)
                                 - np.asarray(prev.convert("L"), np.float32)).mean())
                if d < 0.8:
                    return
            prev = cur

    # ---- 5. 滚动采集 ---------------------------------------------------
    def _list_rect(self, win, tabs_bottom: float):
        l, t, r, b = win.rect
        s = self.p.scale(win)
        top = int(tabs_bottom + 6 * s)
        return (l + int(2 * s), top, r - int(16 * s), b - int(4 * s))

    def _parse(self, img: Image.Image, lines: Sequence[OcrLine], rect, top_hard, bottom_hard):
        local = [x.offset(-rect[0], -rect[1]) for x in lines]
        return parse_cards(local, np.asarray(img), top_hard=top_hard, bottom_hard=bottom_hard)

    def _add(self, cards: Sequence[Card], prev_cards=None, shift: Optional[float] = None,
             tol: float = 10.0):
        """加入本屏卡片；返回 (新增数, [(卡片, 下标)])。

        已知滚动距离 shift 时，本屏 y 坐标 + shift = 上一屏 y 坐标，据此把卡片和上一屏
        同位置的卡片对应起来（即使两次 OCR 结果略有差异也不会重复）。
        """
        placed = []
        new = 0
        for c in cards:
            hint = None
            if prev_cards and shift is not None:
                pred = c.y0 + shift
                best = None
                for pc, idx in prev_cards:
                    d = abs(pc.y0 - pred)
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, idx)
                if best is not None:
                    hint = best[1]
            idx, is_new = self.collector.add_card(c, hint)
            new += int(is_new)
            placed.append((c, idx))
        if new and self.on_progress:
            self.on_progress(self.collector.items)
        return new, placed

    def collect(self, target: Target, tabs_bottom: float) -> List[Card]:
        cfg = self.cfg
        win = self.p.refresh(target.win) or target.win
        rect = self._list_rect(win, tabs_bottom)
        H = rect[3] - rect[1]
        s = self.p.scale(win)
        tol = 10.0 * s
        if H < 120:
            raise StepError("列表区域太小（%d 像素），请把搜一搜窗口拉大一些" % H)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        log.info("  列表区域: %s", rect)

        def ocr_local(img, tag):
            lines = self.ocr.recognize(img, scale=self._ocr_scale(win))
            lines = [x.offset(rect[0], rect[1]) for x in lines]
            if cfg.debug or tag in ("page_000", "page_last"):
                self._save_debug(img, lines, tag)
            return lines

        self._front(win)
        prev = self.p.capture(rect)
        prev_lines = ocr_local(prev, "page_000")
        cards = self._parse(prev, prev_lines, rect, top_hard=True, bottom_hard=False)
        n, prev_placed = self._add(cards)
        log.info("  第 1 屏：识别 %d 个，新增 %d，累计 %d", len(cards), n, len(self.collector.items))
        page = 1
        ended = self._has_end(prev_lines) or not prev_lines

        notches = max(1, cfg.init_notches)
        stall = 0
        no_new = 0
        per_notch_est: Optional[float] = None
        while not ended and page < cfg.max_pages:
            if cfg.max_accounts and len(self.collector.items) >= cfg.max_accounts:
                log.info("  已达到上限 %d 个，停止", cfg.max_accounts)
                break
            self.kick()
            self._front(win)
            self.p.wheel(cx, cy, notches)
            self.p.sleep(cfg.scroll_wait)
            cur = self.p.capture(rect)
            img_shift, err, raw = estimate_shift(prev, cur)
            if raw < 1.0:
                stall += 1
                log.info("  页面没有滚动（%d/%d），等待加载……", stall, cfg.stall_limit)
                if stall >= cfg.stall_limit:
                    break
                self.p.sleep(1.0 + stall * 0.8)
                continue
            cur_lines = ocr_local(cur, "page_%03d" % (page + 1))
            shift, err = determine_shift(prev, cur, prev_lines, cur_lines)
            if shift is None and img_shift == 0 and err > MATCH_ERR_MAX and raw < 3.0:
                shift = 0.0   # 基本没动，只是局部变化（加载动画等）
            if shift is not None and abs(shift) < 2:
                # 只是加载动画之类的变化，内容没动
                stall += 1
                log.info("  页面没有滚动（%d/%d），等待加载……", stall, cfg.stall_limit)
                if stall >= cfg.stall_limit:
                    break
                self.p.sleep(1.0 + stall * 0.8)
                continue
            while shift is None and notches > 1:
                # 两屏之间找不到重叠：一次滚太多可能漏掉账号，往回滚、减小幅度重来
                # 没有重叠说明每格滚动距离 > 0.88H / notches
                floor = 0.88 * H / notches
                per_notch_est = floor if per_notch_est is None else max(per_notch_est, floor)
                new_n = max(1, notches // 2)
                log.info("  两屏没有重叠，回滚 %d 格，滚动幅度改为 %d 格", notches - new_n, new_n)
                self.p.wheel(cx, cy, -(notches - new_n))
                self.p.sleep(cfg.scroll_wait)
                notches = new_n
                cur = self.p.capture(rect)
                cur_lines = ocr_local(cur, "page_%03d_retry" % (page + 1))
                shift, err = determine_shift(prev, cur, prev_lines, cur_lines)
            if shift is None:
                log.warning("  警告：第 %d 屏与上一屏无法对齐，可能有遗漏", page + 1)
            stall = 0
            page += 1
            if shift is not None and shift > 0:
                # 到达已加载内容底部时滚动会被截断，测得的偏小，所以取历史最大值
                per = shift / float(notches)
                per_notch_est = per if per_notch_est is None else max(per_notch_est, per)
                want = max(1, min(15, int(0.55 * H / per_notch_est)))
                notches = min(want, notches + 2)   # 逐步加大，避免一下滚过头
            cards = self._parse(cur, cur_lines, rect, top_hard=False, bottom_hard=False)
            n, placed = self._add(cards, prev_placed, shift, tol)
            log.info("  第 %d 屏：滚动 %s px，识别 %d 个，新增 %d，累计 %d", page,
                     "?" if shift is None else int(shift), len(cards), n, len(self.collector.items))
            prev, prev_lines, prev_placed = cur, cur_lines, placed
            no_new = no_new + 1 if n == 0 else 0
            if no_new >= cfg.stall_limit + 2:
                log.info("  连续 %d 屏没有新账号，判定已到底", no_new)
                break
            if self._has_end(cur_lines):
                log.info("  检测到列表结束标记")
                ended = True
        # 最后一屏：下面已经没有内容，最后一张卡片也算完整
        self._save_debug(prev, prev_lines, "page_last")
        cards = self._parse(prev, prev_lines, rect, top_hard=(page == 1), bottom_hard=True)
        n, _ = self._add(cards, prev_placed, 0.0, tol)
        log.info("  最后一屏补充 %d 个，累计 %d", n, len(self.collector.items))
        return self.collector.items

    @staticmethod
    def _has_end(lines: Sequence[OcrLine]) -> bool:
        for x in lines:
            t = norm_text(x.text)
            if any(m in t for m in END_MARKERS):
                return True
        return False
