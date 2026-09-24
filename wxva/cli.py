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


def read_keywords(path: str) -> List[str]:
    """关键词文件：每行一个，空行和 # 开头的行忽略，自动去重。支持 UTF-8 / GBK。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法识别关键词文件编码，请另存为 UTF-8")
    out = []
    for line in text.splitlines():
        t = line.strip()
        if t and not t.startswith("#") and t not in out:
            out.append(t)
    return out


def write_summary(root: str, results: List[tuple]) -> dict:
    """批量模式汇总：所有关键词的账号合并到一个 CSV，以及跨关键词去重后的名单。"""
    csv_p = os.path.join(root, "汇总_全部关键词.csv")
    txt_p = os.path.join(root, "汇总_去重账号名单.txt")
    with open(csv_p, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["关键词", "序号", "账号名称", "详细信息"])
        for kw, cards, _status in results:
            for i, c in enumerate(cards, 1):
                wr.writerow([kw, i, c.name, c.detail_text])
    seen, names = set(), []
    for _kw, cards, _status in results:
        for c in cards:
            k = "".join(c.name.split()).lower()
            if k not in seen:
                seen.add(k)
                names.append(c.name)
    with open(txt_p, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + ("\n" if names else ""))
    return {"csv": csv_p, "txt": txt_p, "unique": len(names)}


def run_keywords(plat, ocr, keywords: List[str], root: str, args, kick=lambda: None,
                 batch: bool = False) -> int:
    """依次处理每个关键词。单个关键词失败不影响后面的关键词。"""
    from .automator import Automator, Config, StepError
    from .winapi import UserAbort, WinError

    results = []
    worst = 0
    for idx, kw in enumerate(keywords, 1):
        out_dir = os.path.join(root, "%02d_%s" % (idx, _safe_name(kw))) if batch else root
        os.makedirs(out_dir, exist_ok=True)
        if batch:
            log.info("#" * 50)
            log.info("关键词 %d/%d：%s", idx, len(keywords), kw)
        cfg = Config(keyword=kw, out_dir=out_dir, max_pages=args.max_pages,
                     max_accounts=args.max_accounts, scroll_wait=args.scroll_wait,
                     debug=args.debug, start_from=args.start_from if idx == 1 else "main",
                     ocr_scale=args.ocr_scale)
        bot = Automator(plat, ocr, cfg)
        bot.kick = kick
        # 边采集边落盘：中途中止也不丢数据
        bot.on_progress = lambda items, _d=out_dir, _k=kw: write_outputs(_d, _k, items)
        try:
            cards = bot.run()
            status = "ok"
        except UserAbort as e:
            log.error("✗ %s", e)
            cards, status = bot.collector.items, "aborted"
        except (StepError, WinError) as e:
            log.error("✗ %s", e)
            log.info("排查资料（截图 / OCR / 日志）在: %s", os.path.abspath(out_dir))
            cards, status = bot.collector.items, "failed"
        paths = write_outputs(out_dir, kw, cards)
        results.append((kw, list(cards), status))
        if status == "ok":
            log.info("✓ 「%s」完成，共 %d 个账号 -> %s", kw, len(cards), os.path.abspath(paths["txt"]))
        else:
            worst = 2
            if cards:
                log.info("已保存中途采集到的 %d 个账号: %s", len(cards), os.path.abspath(paths["txt"]))
        if status == "aborted":
            break
    if batch:
        summ = write_summary(root, results)
        log.info("=" * 50)
        for kw, cards, status in results:
            log.info("  %-16s %4d 个  %s", kw, len(cards),
                     {"ok": "✓", "failed": "✗ 失败", "aborted": "中止"}[status])
        log.info("汇总表: %s", os.path.abspath(summ["csv"]))
        log.info("去重名单（%d 个）: %s", summ["unique"], os.path.abspath(summ["txt"]))
    elif results and results[0][2] == "ok":
        log.info("=" * 50)
        log.info("✓ 完成！共 %d 个账号", len(results[0][1]))
        log.info("  账号名单: %s", os.path.abspath(os.path.join(root, "accounts.txt")))
        log.info("  含详情  : %s", os.path.abspath(os.path.join(root, "accounts.csv")))
    return worst


def main(argv=None) -> int:
    _fix_console_encoding()
    ap = argparse.ArgumentParser(prog="wxva", description="微信搜一搜：视频 → 账号 列表导出")
    ap.add_argument("keyword", nargs="?", help="搜索关键词，例如：羽绒服库存")
    ap.add_argument("-f", "--keywords-file", help="批量模式：关键词文件，每行一个")
    ap.add_argument("-o", "--output", default="output", help="输出根目录（默认 ./output）")
    ap.add_argument("--max-accounts", type=int, default=0, help="每个关键词最多采集多少个账号（0=不限）")
    ap.add_argument("--max-pages", type=int, default=400, help="最多滚动多少屏（默认 400）")
    ap.add_argument("--scroll-wait", type=float, default=1.2, help="每次滚动后等待秒数（网络慢就调大）")
    ap.add_argument("--start-from", choices=["main", "tabs", "list"], default="main",
                    help="main=从头开始(默认)；tabs=搜一搜页已打开；list=已在账号列表")
    ap.add_argument("--ocr-scale", type=float, default=None, help="OCR 放大倍数（默认按 DPI 自动）")
    ap.add_argument("--debug", action="store_true", help="保存每一屏截图和 OCR 结果")
    ap.add_argument("--probe", action="store_true", help="只测试能否唤起微信主窗口")
    ap.add_argument("--selftest", action="store_true",
                    help="不依赖微信，用记事本自检窗口激活/粘贴/截图/OCR 是否正常")
    ap.add_argument("--step-timeout", type=int, default=180,
                    help="看门狗：任何一步卡住超过这么多秒就打印堆栈并退出（默认 180）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    keywords: List[str] = []
    batch = False
    if args.probe:
        root = os.path.join(args.output, "probe_" + stamp)
    elif args.selftest:
        root = os.path.join(args.output, "selftest_" + stamp)
    elif args.keywords_file:
        keywords = read_keywords(args.keywords_file)
        if not keywords:
            ap.error("关键词文件里没有关键词")
        batch = True
        root = os.path.join(args.output, "batch_" + stamp)
    else:
        if not args.keyword:
            try:
                args.keyword = input("请输入搜索关键词（例如 羽绒服库存）: ").strip()
            except EOFError:
                args.keyword = ""
        if not args.keyword:
            ap.error("请提供搜索关键词，例如：python -m wxva 羽绒服库存")
        keywords = [args.keyword]
        root = os.path.join(args.output, "%s_%s" % (_safe_name(args.keyword), stamp))
    setup_logging(root, args.verbose)

    # 看门狗：真卡死时也能看到卡在哪一行，而不是「没反应」
    wd_file = open(os.path.join(root, "watchdog_traceback.txt"), "w", encoding="utf-8")

    def kick():
        faulthandler.dump_traceback_later(args.step_timeout, exit=True, file=wd_file)
    kick()

    from .winapi import UserAbort, WinError
    try:
        if args.probe:
            return probe(root)
        if args.selftest:
            from .selftest import selftest
            return selftest(root)

        from .ocr import OcrEngine
        from .platform_win import WinPlatform

        log.info("输出目录: %s", os.path.abspath(root))
        plat = WinPlatform()          # 最先创建：设置 DPI 感知必须在任何窗口/截图调用之前
        log.info("DPI 感知模式: %s    （运行中按住 F12 可随时中止）", plat.dpi_mode)
        log.info("加载 OCR 引擎……")
        ocr = OcrEngine()
        log.info("OCR 引擎: %s", ocr.kind)
        if batch:
            log.info("批量模式：共 %d 个关键词", len(keywords))
        return run_keywords(plat, ocr, keywords, root, args, kick, batch)
    except KeyboardInterrupt:
        log.error("用户中断")
        return 130
    except (WinError, UserAbort) as e:
        log.error("✗ %s", e)
        return 2
    except Exception as e:
        log.exception("✗ 未预期的错误: %r", e)
        log.info("请把 %s 整个文件夹发给开发者排查", os.path.abspath(root))
        return 3
    finally:
        faulthandler.cancel_dump_traceback_later()
        try:
            wd_file.close()
        except Exception:
            pass
