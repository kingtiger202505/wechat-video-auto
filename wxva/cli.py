"""命令行入口。

示例：
    python -m wxva "羽绒服库存"
    python -m wxva "羽绒服库存" --max-accounts 200 --debug
    python -m wxva --probe                     # 只测试第一步：能否找到并唤起微信主窗口
    python -m wxva "羽绒服库存" --start-from tabs   # 搜一搜页已手动打开，从切 tab 开始
    python -m wxva "羽绒服库存" --start-from list   # 已手动切到「账号」，只做滚动采集
"""
from __future__ import annotations

import argparse
import csv
import faulthandler
import json
import logging
import os
import re
import sys
import time
from typing import List

from .parser import Card

log = logging.getLogger("wxva")


def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")
    return s[:40] or "keyword"


def write_outputs(out_dir: str, keyword: str, cards: List[Card]) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    txt = os.path.join(out_dir, "accounts.txt")
    csv_p = os.path.join(out_dir, "accounts.csv")
    js = os.path.join(out_dir, "accounts.json")
    tmp = txt + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for c in cards:
            f.write(c.name + "\n")
    os.replace(tmp, txt)
    tmp = csv_p + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: Excel 直接打开不乱码
        wr = csv.writer(f)
        wr.writerow(["序号", "账号名称", "详细信息"])
        for i, c in enumerate(cards, 1):
            wr.writerow([i, c.name, c.detail_text])
    os.replace(tmp, csv_p)
    tmp = js + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keyword": keyword, "count": len(cards),
                   "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "accounts": [c.to_dict() for c in cards]}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, js)
    return {"txt": txt, "csv": csv_p, "json": js}


def setup_logging(out_dir: str, verbose: bool):
    os.makedirs(out_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    fh = logging.FileHandler(os.path.join(out_dir, "run.log"), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(ch)
    root.addHandler(fh)
    for noisy in ("PIL", "RapidOCR", "rapidocr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fix_console_encoding():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def probe(out_dir: str) -> int:
    """只做第 1 步（找到并唤起微信主窗口），用来单独验证最容易出问题的环节。"""
    from . import winapi as w
    from .platform_win import WinPlatform
    plat = WinPlatform()
    log.info("DPI 感知模式: %s", plat.dpi_mode)
    log.info("锁屏: %s", w.screen_locked())
    proc = w.wechat_process()
    log.info("微信进程: %s", proc)
    log.info("微信相关顶层窗口：")
    for x in plat.list_windows():
        log.info("  %s", x.short())
    main = w.find_main_window()
    log.info("判定的主窗口: %s", main.short() if main else None)
    main = plat.bring_up_main()
    img = plat.capture(main.rect)
    p = os.path.join(out_dir, "probe_main.png")
    img.save(p)
    log.info("✓ 第一步通过：主窗口已唤起并在前台，截图已保存 %s", p)
    return 0


def main(argv=None) -> int:
    _fix_console_encoding()
    ap = argparse.ArgumentParser(prog="wxva", description="微信搜一搜：视频 → 账号 列表导出")
    ap.add_argument("keyword", nargs="?", help="搜索关键词，例如：羽绒服库存")
    ap.add_argument("-o", "--output", default="output", help="输出根目录（默认 ./output）")
    ap.add_argument("--max-accounts", type=int, default=0, help="最多采集多少个账号（0=不限）")
    ap.add_argument("--max-pages", type=int, default=400, help="最多滚动多少屏（默认 400）")
    ap.add_argument("--scroll-wait", type=float, default=1.2, help="每次滚动后等待秒数（网络慢就调大）")
    ap.add_argument("--start-from", choices=["main", "tabs", "list"], default="main",
                    help="main=从头开始(默认)；tabs=搜一搜页已打开；list=已在账号列表")
    ap.add_argument("--ocr-scale", type=float, default=None, help="OCR 放大倍数（默认按 DPI 自动）")
    ap.add_argument("--debug", action="store_true", help="保存每一屏截图和 OCR 结果")
    ap.add_argument("--probe", action="store_true", help="只测试能否唤起微信主窗口")
    ap.add_argument("--step-timeout", type=int, default=180,
                    help="看门狗：任何一步卡住超过这么多秒就打印堆栈并退出（默认 180）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    if args.probe:
        out_dir = os.path.join(args.output, "probe_" + stamp)
    else:
        if not args.keyword:
            try:
                args.keyword = input("请输入搜索关键词（例如 羽绒服库存）: ").strip()
            except EOFError:
                args.keyword = ""
        if not args.keyword:
            ap.error("请提供搜索关键词，例如：python -m wxva 羽绒服库存")
        out_dir = os.path.join(args.output, "%s_%s" % (_safe_name(args.keyword), stamp))
    setup_logging(out_dir, args.verbose)

    # 看门狗：真卡死时也能看到卡在哪一行，而不是「没反应」
    wd_file = open(os.path.join(out_dir, "watchdog_traceback.txt"), "w", encoding="utf-8")
    def kick():
        faulthandler.dump_traceback_later(args.step_timeout, exit=True, file=wd_file)
    kick()

    from .winapi import UserAbort, WinError
    try:
        if args.probe:
            return probe(out_dir)

        from .automator import Automator, Config, StepError
        from .ocr import OcrEngine
        from .platform_win import WinPlatform

        log.info("输出目录: %s", os.path.abspath(out_dir))
        plat = WinPlatform()          # 最先创建：设置 DPI 感知必须在任何窗口/截图调用之前
        log.info("DPI 感知模式: %s    （运行中按住 F12 可随时中止）", plat.dpi_mode)
        log.info("加载 OCR 引擎……")
        ocr = OcrEngine()
        log.info("OCR 引擎: %s", ocr.kind)
        cfg = Config(keyword=args.keyword, out_dir=out_dir, max_pages=args.max_pages,
                     max_accounts=args.max_accounts, scroll_wait=args.scroll_wait,
                     debug=args.debug, start_from=args.start_from, ocr_scale=args.ocr_scale)
        bot = Automator(plat, ocr, cfg)
        bot.kick = kick
        # 边采集边落盘：中途中止也不丢数据
        bot.on_progress = lambda items: write_outputs(out_dir, args.keyword, items)
        try:
            cards = bot.run()
        except (StepError, WinError, UserAbort) as e:
            cards = bot.collector.items
            log.error("✗ %s", e)
            if cards:
                paths = write_outputs(out_dir, args.keyword, cards)
                log.info("已保存中途采集到的 %d 个账号: %s", len(cards), paths["txt"])
            log.info("排查资料（截图 / OCR / 日志）在: %s", os.path.abspath(out_dir))
            return 2
        paths = write_outputs(out_dir, args.keyword, cards)
        log.info("=" * 50)
        log.info("✓ 完成！共 %d 个账号", len(cards))
        log.info("  账号名单: %s", os.path.abspath(paths["txt"]))
        log.info("  含详情  : %s", os.path.abspath(paths["csv"]))
        return 0
    except KeyboardInterrupt:
        log.error("用户中断")
        return 130
    except (WinError, UserAbort) as e:     # --probe 模式
        log.error("✗ %s", e)
        return 2
    except Exception as e:
        log.exception("✗ 未预期的错误: %r", e)
        log.info("请把 %s 整个文件夹发给开发者排查", os.path.abspath(out_dir))
        return 3
    finally:
        faulthandler.cancel_dump_traceback_later()
        try:
            wd_file.close()
        except Exception:
            pass
