"""模拟「屏幕 + 微信」的测试平台。

用真实字体把微信主窗口 / 搜一搜页面画成像素图，点击、滚轮、粘贴会改变页面状态，
然后让 Automator 在上面跑完整流程（真实 RapidOCR 识别），验证：
  tab 定位、下拉项定位、滚动幅度自适应、跨屏去重、截断卡片处理、到底检测。
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    os.environ.get("WXVA_TEST_FONT", ""),
    "/tmp/fonts/wq/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def find_font() -> Optional[str]:
    for p in FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


_font_cache = {}


def font(size: int):
    key = size
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(find_font(), size)
    return _font_cache[key]


PREFIX = ["羽绒服", "冬装", "服装", "女装", "男装", "童装", "外套", "棉服", "尾货", "品牌"]
MID = ["库存", "清仓", "批发", "工厂店", "源头", "直播间", "特卖", "折扣", "尾单", "好货"]
SUFFIX = ["小王", "阿杰", "老李", "姐妹", "优选", "严选", "官方", "旗舰", "仓库", "专营",
          "丽丽", "大鹏", "小美", "阿强", "老周"]
DESC = ["专注羽绒服库存处理十年", "工厂一手货源 支持一件代发", "每天晚上八点直播",
        "品牌尾货 质量保证", "广州十三行实体档口", "诚信经营 欢迎合作", "", "",
        "河北白沟箱包服装批发", "浙江平湖羽绒服产业带"]


def make_accounts(n: int, seed: int = 1) -> List[Tuple[str, List[str]]]:
    rnd = random.Random(seed)
    seen = set()
    out = []
    while len(out) < n:
        name = rnd.choice(PREFIX) + rnd.choice(MID) + rnd.choice(SUFFIX)
        if rnd.random() < 0.3:
            name += str(rnd.randint(1, 99))
        if name in seen:
            continue
        seen.add(name)
        d = rnd.choice(DESC)
        details = [d] if d else []
        if rnd.random() < 0.5:
            details.append("%d个作品 · %s粉丝" % (rnd.randint(3, 900),
                                              rnd.choice(["1.2万", "3456", "8.8万", "521", "10万+"])))
        out.append((name, details))
    return out


@dataclass
class SimWin:
    hwnd: int
    title: str
    rect: Tuple[int, int, int, int]
    pid: int = 1000
    visible: bool = True
    iconic: bool = False
    cls: str = "Qt51514QWindowIcon"

    @property
    def width(self): return self.rect[2] - self.rect[0]

    @property
    def height(self): return self.rect[3] - self.rect[1]

    @property
    def area(self): return self.width * self.height

    def short(self): return "SimWin(%s %r %s)" % (self.hwnd, self.title, self.rect)


class SimPlatform:
    def __init__(self, n_accounts=57, scale=1.0, dark=False, entry_text="搜一搜",
                 search_in_main=False, lazy_batch=20, notch_px=100, screen=None,
                 avatars=True, placeholder=True, stale_keyword=None, refresh_delay=3):
        self.avatars = avatars
        self.placeholder = placeholder
        self.s = scale
        self.dark = dark
        self.entry_text = entry_text
        self.search_in_main = search_in_main
        self.notch_px = int(notch_px * scale)
        self.accounts = make_accounts(n_accounts)
        self.loaded = min(lazy_batch, n_accounts)
        self.lazy_batch = lazy_batch
        self.pending_load = 0
        self.screen = screen or (int(1920 * scale), int(1080 * scale))
        S = lambda v: int(v * scale)
        self.main = SimWin(1, "微信", (S(60), S(40), S(60 + 900), S(40 + 640)))
        self.search = SimWin(2, "搜一搜", (S(420), S(60), S(420 + 820), S(60 + 900)), pid=1001,
                             cls="Chrome_WidgetWin_0")
        self.n_accounts = n_accounts
        self.refresh_delay = refresh_delay
        self.page_text = stale_keyword or ""
        self.pending_page = None          # (剩余 sleep 次数, 新关键词)
        self.search_open = bool(stale_keyword)
        self.search_focus = False
        self.search_text = ""
        self.dropdown = False
        self.tab = "全部"
        self.subtab = None
        self.scroll = 0
        self.front = None
        self.clicks = []
        self.enter_pressed = 0
        self._list_img = None
        if stale_keyword:
            self._load_page(stale_keyword)   # 上次留下的搜一搜窗口

    # ---------------------------------------------------------------- API
    def sleep(self, sec):
        if self.pending_page is not None:
            n, kw = self.pending_page
            if n <= 1:
                self._load_page(kw)
                self.pending_page = None
            else:
                self.pending_page = (n - 1, kw)
        # 懒加载：等待一段时间后加载下一批
        if self.pending_load:
            self.pending_load -= 1
            if self.pending_load == 0:
                self.loaded = min(len(self.accounts), self.loaded + self.lazy_batch)
                self._list_img = None

    def check_abort(self):
        pass

    def list_windows(self):
        w = [self.main]
        if self.search_open and not self.search_in_main:
            w.append(self.search)
        return w

    def refresh(self, win):
        return win

    def scale(self, win):
        return self.s

    def is_front(self, win):
        return self.front is win

    def focus(self, win, click_fallback=True):
        self.front = win
        return True

    def bring_up_main(self):
        self.front = self.main
        return self.main

    def ensure_on_screen(self, win, min_w=0, min_h=0):
        return win

    def get_clipboard(self):
        return "old clipboard"

    def set_clipboard(self, text):
        pass

    def select_all(self):
        pass

    def ctrl_f(self):
        if self.front is self.main:
            self.search_focus = True

    def paste_text(self, text):
        if self.search_focus:
            self.search_text = text
            self.dropdown = True

    def enter(self):
        self.enter_pressed += 1
        if self.search_focus and self.search_text:
            self._open_search()

    def move_to(self, x, y):
        pass

    def _open_search(self):
        was_open = self.search_open
        self.search_open = True
        self.dropdown = False
        self.search_focus = False
        self.front = self.main if self.search_in_main else self.search
        if was_open and self.page_text:
            # 已打开的搜一搜窗口：过一会儿才刷新成新关键词的结果
            self.pending_page = (self.refresh_delay, self.search_text)
        else:
            self._load_page(self.search_text)

    def _load_page(self, kw):
        import zlib
        self.page_text = kw
        self.tab = "全部"
        self.subtab = None
        self.scroll = 0
        if kw != "羽绒服库存":   # 其它关键词换一批账号
            self.accounts = make_accounts(self.n_accounts, seed=zlib.crc32(kw.encode()))
        else:
            self.accounts = make_accounts(self.n_accounts)
        self.loaded = min(self.lazy_batch, len(self.accounts))
        self._list_img = None

    # ---------------------------------------------------------------- 布局
    def _S(self, v):
        return int(v * self.s)

    def _main_layout(self):
        l, t, r, b = self.main.rect
        S = self._S
        return {
            "search_box": (l + S(70), t + S(22), l + S(290), t + S(54)),
            "entry_row": (l + S(60), t + S(60) + S(56) * 3, l + S(320), t + S(60) + S(56) * 4),
        }

    def _page_win(self):
        return self.main if self.search_in_main else self.search

    def _page_layout(self):
        l, t, r, b = self._page_win().rect
        S = self._S
        tabs = ["全部", "文章", "公众号", "视频", "小程序", "直播", "百科", "新闻"]
        x = l + S(40)
        tab_boxes = {}
        f = font(S(15))
        for name in tabs:
            w = int(f.getlength(name))
            tab_boxes[name] = (x, t + S(90), x + w, t + S(90) + S(20))
            x += w + S(26)
        sub_boxes = {}
        if self.tab == "视频":
            x = l + S(40)
            for name in ["综合", "视频", "账号", "直播"]:
                w = int(font(S(14)).getlength(name))
                sub_boxes[name] = (x, t + S(132), x + w, t + S(132) + S(18))
                x += w + S(30)
        list_top = t + S(170)
        return tabs, tab_boxes, sub_boxes, list_top

    def click(self, x, y):
        self.clicks.append((x, y))
        inside = lambda bx: bx[0] - 4 <= x <= bx[2] + 4 and bx[1] - 4 <= y <= bx[3] + 4
        ml, mt, mr, mb = self.main.rect
        main_on_top = (self.front is self.main and not self.search_in_main
                       and ml <= x <= mr and mt <= y <= mb)
        if self.search_open and not main_on_top:
            win = self._page_win()
            l, t, r, b = win.rect
            if l <= x <= r and t <= y <= b:
                self.front = win
                _tabs, tab_boxes, sub_boxes, _lt = self._page_layout()
                for name, bx in tab_boxes.items():
                    if inside(bx):
                        self.tab = name
                        self.subtab = None
                        self.scroll = 0
                        return
                for name, bx in sub_boxes.items():
                    if inside(bx):
                        self.subtab = name
                        self.scroll = 0
                        return
                return
        l, t, r, b = self.main.rect
        if l <= x <= r and t <= y <= b:
            self.front = self.main
            lay = self._main_layout()
            if inside(lay["search_box"]):
                self.search_focus = True
                return
            if self.dropdown and self.entry_text and inside(lay["entry_row"]):
                self._open_search()
                return
            self.search_focus = False

    def _list_image(self):
        if self._list_img is not None:
            return self._list_img
        S = self._S
        W = self._page_win().width
        card_h = S(76)
        n = self.loaded
        H = S(12) + card_h * n + S(60)
        bg = (25, 25, 25) if self.dark else (255, 255, 255)
        fg = (230, 230, 230) if self.dark else (20, 20, 20)
        gray = (140, 140, 140)
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        rnd = random.Random(7)
        for i in range(n):
            name, details = self.accounts[i]
            y = S(12) + card_h * i
            col = tuple(rnd.randint(40, 220) for _ in range(3))
            if self.avatars:
                d.ellipse((S(24), y + S(12), S(24) + S(50), y + S(62)), fill=col)
            tx = S(24) + S(50) + S(14)
            lines = [(name, S(16), fg)] + [(t, S(13), gray) for t in details[:2]]
            total = sum(sz + S(6) for _, sz, _c in lines) - S(6)
            ty = y + S(37) - total // 2
            for text, sz, c in lines:
                d.text((tx, ty), text, font=font(sz), fill=c)
                ty += sz + S(6)
            # 右侧「关注」按钮
            d.rounded_rectangle((W - S(90), y + S(24), W - S(40), y + S(50)), radius=S(6),
                                outline=(7, 193, 96))
            d.text((W - S(80), y + S(28)), "关注", font=font(S(13)), fill=(7, 193, 96))
            d.line((tx, y + card_h - 1, W - S(20), y + card_h - 1),
                   fill=(50, 50, 50) if self.dark else (238, 238, 238))
        foot = "没有更多了" if self.loaded >= len(self.accounts) else "加载中..."
        d.text((W // 2 - S(35), H - S(40)), foot, font=font(S(13)), fill=gray)
        self._list_img = img
        return img

    def wheel(self, x, y, notches):
        if not (self.search_open and self.tab == "视频" and self.subtab == "账号"):
            return
        _t, _tb, _sb, list_top = self._page_layout()
        view_h = self._page_win().rect[3] - list_top
        full = self._list_image().height
        self.scroll = max(0, min(full - view_h, self.scroll + notches * self.notch_px))
        if self.scroll >= full - view_h - 5 and self.loaded < len(self.accounts):
            self.pending_load = 2

    # ---------------------------------------------------------------- 渲染
    def _render_main(self, img):
        S = self._S
        l, t, r, b = self.main.rect
        d = ImageDraw.Draw(img)
        d.rectangle((l, t, r, b), fill=(245, 245, 245))
        d.rectangle((l, t, l + S(56), b), fill=(46, 46, 46))
        lay = self._main_layout()
        sb = lay["search_box"]
        d.rounded_rectangle(sb, radius=S(4), fill=(226, 226, 226))
        if self.search_text:
            d.text((sb[0] + S(26), sb[1] + S(7)), self.search_text, font=font(S(14)), fill=(20, 20, 20))
        elif self.placeholder:
            d.text((sb[0] + S(26), sb[1] + S(7)), "搜索", font=font(S(14)), fill=(160, 160, 160))
        if self.dropdown:
            y = t + S(60)
            rows = ["联系人", "羽绒服群 (3)", "聊天记录"]
            if self.entry_text:
                rows.append(self.entry_text + "  " + self.search_text)
            for row in rows:
                d.text((l + S(80), y + S(18)), row, font=font(S(14)), fill=(30, 30, 30))
                y += S(56)
        else:
            y = t + S(70)
            for name in ["文件传输助手", "张三", "工作群", "订阅号消息"]:
                d.text((l + S(120), y), name, font=font(S(14)), fill=(30, 30, 30))
                y += S(64)

    def _render_page(self, img):
        S = self._S
        win = self._page_win()
        l, t, r, b = win.rect
        bg = (25, 25, 25) if self.dark else (255, 255, 255)
        fg = (230, 230, 230) if self.dark else (20, 20, 20)
        d = ImageDraw.Draw(img)
        d.rectangle((l, t, r, b), fill=bg)
        d.text((l + S(16), t + S(10)), "搜一搜", font=font(S(13)), fill=(120, 120, 120))
        d.rounded_rectangle((l + S(40), t + S(40), r - S(40), t + S(74)), radius=S(6),
                            fill=(60, 60, 60) if self.dark else (242, 242, 242))
        d.text((l + S(60), t + S(47)), self.page_text, font=font(S(15)), fill=fg)
        tabs, tab_boxes, sub_boxes, list_top = self._page_layout()
        for name, bx in tab_boxes.items():
            c = (7, 193, 96) if name == self.tab else fg
            d.text((bx[0], bx[1]), name, font=font(S(15)), fill=c)
        for name, bx in sub_boxes.items():
            c = (7, 193, 96) if name == self.subtab else (110, 110, 110)
            d.text((bx[0], bx[1]), name, font=font(S(14)), fill=c)
        if self.tab == "视频" and self.subtab == "账号":
            li = self._list_image()
            view_h = b - list_top
            crop = li.crop((0, self.scroll, win.width, self.scroll + view_h))
            img.paste(crop, (l, list_top))
        elif self.tab == "视频":
            d.text((l + S(40), list_top + S(20)), "羽绒服库存怎么处理？三分钟看懂", font=font(S(15)), fill=fg)
            d.rectangle((l + S(40), list_top + S(50), l + S(300), list_top + S(200)), fill=(90, 120, 160))
        else:
            d.text((l + S(40), list_top + S(20)), self.page_text + " 相关文章", font=font(S(15)), fill=fg)

    def capture(self, rect):
        img = Image.new("RGB", self.screen, (0, 90, 140))
        if self.search_in_main and self.search_open:
            self._render_page(img)
        else:
            if self.search_open and self.front is self.main:
                self._render_page(img)     # 主窗口在前台时盖住搜一搜窗口
                self._render_main(img)
            else:
                self._render_main(img)
                if self.search_open:
                    self._render_page(img)
        l, t, r, b = [int(v) for v in rect]
        return img.crop((l, t, r, b))
