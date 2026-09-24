"""在模拟平台上跑完整流程（真实 OCR）。运行：python -m pytest tests -s"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.sim import SimPlatform, find_font  # noqa: E402
from wxva.automator import Automator, Config  # noqa: E402
from wxva.ocr import OcrEngine  # noqa: E402

pytestmark = pytest.mark.skipif(find_font() is None, reason="没有中文字体")

_OCR = None


def ocr():
    global _OCR
    if _OCR is None:
        _OCR = OcrEngine()
    return _OCR


def run_sim(**kw):
    plat = SimPlatform(**kw)
    out = tempfile.mkdtemp(prefix="wxva_test_")
    cfg = Config(keyword="羽绒服库存", out_dir=out, scroll_wait=0.0, debug=True)
    bot = Automator(plat, ocr(), cfg)
    cards = bot.run()
    truth = [a[0] for a in plat.accounts]
    got = [c.name for c in cards]
    missing = [n for n in truth if n not in got]
    extra = [n for n in got if n not in truth]
    print("\n[%s] truth=%d got=%d missing=%s extra=%s out=%s"
          % (kw, len(truth), len(got), missing, extra, out))
    return plat, truth, got, cards


def _check(truth, got, max_miss=0, max_extra=0):
    missing = [n for n in truth if n not in got]
    extra = [n for n in got if n not in truth]
    assert len(missing) <= max_miss, missing
    assert len(extra) <= max_extra, extra
    # 顺序与页面一致
    idx = [truth.index(n) for n in got if n in truth]
    assert idx == sorted(idx)


def test_default_100pct():
    plat, truth, got, cards = run_sim(n_accounts=45)
    _check(truth, got)
    assert plat.enter_pressed == 0          # 通过点击下拉「搜一搜」进入，没按回车


def test_150pct_dpi():
    plat, truth, got, _ = run_sim(n_accounts=35, scale=1.5)
    _check(truth, got)


def test_dark_mode_and_entry_variant():
    plat, truth, got, _ = run_sim(n_accounts=30, dark=True, entry_text="搜索网络结果")
    _check(truth, got)


def test_search_page_inside_main_and_big_scroll_step():
    plat, truth, got, _ = run_sim(n_accounts=40, search_in_main=True, notch_px=260)
    _check(truth, got)


def test_details_captured():
    plat, truth, got, cards = run_sim(n_accounts=25)
    truth_map = dict(plat.accounts)
    for c in cards:
        exp = [d for d in truth_map[c.name] if d]
        for e in exp:
            assert any(e.replace(" ", "") in d.replace(" ", "") for d in c.details), (c, exp)


def test_no_avatar_gap_mode():
    plat, truth, got, _ = run_sim(n_accounts=30, avatars=False)
    _check(truth, got)


def test_fallbacks_no_placeholder_no_entry():
    plat, truth, got, _ = run_sim(n_accounts=20, placeholder=False, entry_text=None)
    _check(truth, got)
    assert plat.enter_pressed == 1          # 找不到下拉项时按回车（此前已确认焦点在搜索框）


def test_long_list_lazy_load():
    plat, truth, got, _ = run_sim(n_accounts=90, lazy_batch=15)
    _check(truth, got)
