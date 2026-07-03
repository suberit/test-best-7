#!/usr/bin/env python3
"""GitHub Actions Playwright 完整数据采集测试脚本

基于 V11，添加以下功能：
- 1h 翻页（获取完整 1h 历史）
- 周线采集（初始 + 翻页）
- 可配置翻页等待策略（E/F/G 三种模式）
- 翻页次数增加到 10 次

环境变量：
  WAIT_STRATEGY: e/f/g（翻页等待策略）
  CHART_SCROLL_TIMES: 日线翻页次数（默认 10）
  SCROLL_1H_TIMES: 1h 翻页次数（默认 3）
  SCROLL_WEEKLY_TIMES: 周线翻页次数（默认 3）
"""

import argparse
import datetime
import json
import os
import time
from playwright.sync_api import sync_playwright

DETAIL_URL = "https://csqaq.com/goods/{goods_id}"
RESULT_FILE = "result.json"

WAIT_STRATEGY = os.environ.get("WAIT_STRATEGY", "e")
CHART_SCROLL_TIMES = int(os.environ.get("CHART_SCROLL_TIMES", "10"))
SCROLL_1H_TIMES = int(os.environ.get("SCROLL_1H_TIMES", "3"))
SCROLL_WEEKLY_TIMES = int(os.environ.get("SCROLL_WEEKLY_TIMES", "3"))
SINGLE_ITEM_TIMEOUT = int(os.environ.get("SINGLE_ITEM_TIMEOUT", "120"))

# 方案 H：混合策略（日线用 E，1h 用 G）
STRATEGY_MAP_H = {"日线": "e", "1h": "g", "周线": "g"}


def get_strategy(period_name):
    """获取指定 K 线类型的翻页策略"""
    if WAIT_STRATEGY == "h":
        return STRATEGY_MAP_H.get(period_name, "e")
    return WAIT_STRATEGY

API_CHART_ALL = "info/simple/chartAll"
API_CHIP_DATA = "info/chipData"


def wait_network_idle(page, timeout=15000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        page.wait_for_timeout(2000)


def get_api_count(all_api_data, url_pattern):
    return sum(len(all_api_data[u]) for u in all_api_data if url_pattern in u)


def wait_for_new_response(page, all_api_data, url_pattern, before_count, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        current_count = get_api_count(all_api_data, url_pattern)
        if current_count > before_count:
            return True
        page.wait_for_timeout(300)
    return False


def scroll_and_wait(page, all_api_data, canvas_info, url_pattern, before_count, strategy=WAIT_STRATEGY):
    """根据策略执行翻页 + 等待"""
    center_y = canvas_info["y"] + canvas_info["height"] / 2
    page.mouse.move(canvas_info["x"] + canvas_info["width"] / 2, center_y)

    if strategy == "e":
        # E: V11 networkidle + API计数 + 2000ms固定等待
        for _ in range(5):
            page.mouse.wheel(-1500, 0)
            page.wait_for_timeout(300)
        wait_network_idle(page, timeout=8000)
        wait_for_new_response(page, all_api_data, url_pattern, before_count, timeout=8)
        page.wait_for_timeout(2000)

    elif strategy == "f":
        # F: B方向固定等待 5500ms
        for _ in range(5):
            page.mouse.wheel(-1500, 0)
            page.wait_for_timeout(800)
        page.wait_for_timeout(1500)

    elif strategy == "g":
        # G: 混合 500ms×5 + networkidle + API计数 + 2000ms
        for _ in range(5):
            page.mouse.wheel(-1500, 0)
            page.wait_for_timeout(500)
        wait_network_idle(page, timeout=8000)
        wait_for_new_response(page, all_api_data, url_pattern, before_count, timeout=8)
        page.wait_for_timeout(2000)
    else:
        for _ in range(5):
            page.mouse.wheel(-1500, 0)
            page.wait_for_timeout(300)
        wait_network_idle(page, timeout=8000)
        page.wait_for_timeout(2000)


def parse_chart_responses(all_api_data, chart_url, start_idx):
    """解析 chartAll API 响应，返回新数据和新的索引"""
    new_data = []
    current_count = len(all_api_data.get(chart_url, []))
    for idx in range(start_idx, current_count):
        try:
            parsed = json.loads(all_api_data[chart_url][idx]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    new_data.extend(arr)
        except Exception:
            pass
    return new_data, current_count


def dedup_and_sort(data_list, key="t"):
    """去重并排序"""
    seen = set()
    unique = []
    for item in data_list:
        k = item.get(key)
        if k and k not in seen:
            seen.add(k)
            unique.append(item)
    unique.sort(key=lambda x: int(x.get(key, 0)))
    return unique


def do_scroll_loop(page, all_api_data, canvas_info, chart_url, strategy, scroll_times, period_name="日线"):
    """通用翻页循环：日线/1h/周线共用"""
    all_data = []
    no_new_count = 0

    # 获取初始数据
    new_data, _ = parse_chart_responses(all_api_data, chart_url, 0)
    all_data.extend(new_data)
    print(f"      初始{period_name}: {len(new_data)} 条", flush=True)

    for i in range(scroll_times):
        before_total = len(all_data)
        before_resp_count = len(all_api_data.get(chart_url, []))

        scroll_and_wait(page, all_api_data, canvas_info, API_CHART_ALL, before_resp_count, strategy)

        new_data, _ = parse_chart_responses(all_api_data, chart_url, before_resp_count)
        all_data.extend(new_data)

        if len(all_data) > before_total:
            print(f"      {period_name}翻页 {i+1}: +{len(new_data)} 条, 总计 {len(all_data)} 条", flush=True)
            no_new_count = 0
        else:
            no_new_count += 1
            print(f"      {period_name}翻页 {i+1}: 无新数据 ({no_new_count}/3)", flush=True)
            if no_new_count >= 3:
                print(f"      连续 3 次无新数据，停止{period_name}翻页", flush=True)
                break

    return dedup_and_sort(all_data)


def click_period(page, period_name):
    """点击 K 线周期按钮"""
    targets_map = {
        "日线": ["日线"],
        "1小时": ["1小时", "1H", "1h"],
        "周线": ["周线", "W", "w", "1W", "1w"],
    }
    targets = targets_map.get(period_name, [period_name])
    result = page.evaluate("""(targets) => {
        const els = document.querySelectorAll('span, div, a, button, li');
        for (const target of targets) {
            for (const el of els) {
                if (el.textContent.trim() === target && el.offsetParent !== null) { el.click(); return target; }
            }
        }
        return false;
    }""", targets)
    return result


def scrape_one(page, goods_id, item_name=None):
    print(f"\n{'='*60}", flush=True)
    strategy_info = f"日线={get_strategy('日线')} 1h={get_strategy('1h')}" if WAIT_STRATEGY == "h" else f"策略={WAIT_STRATEGY}"
    print(f"  抓取饰品: goods_id={goods_id} name={item_name} {strategy_info}", flush=True)
    print(f"{'='*60}", flush=True)

    detail_url = DETAIL_URL.format(goods_id=goods_id)
    item_result = {
        "name": item_name or "",
        "goods_id": str(goods_id),
        "detail": None,
        "chart_daily": [],
        "chart_1h": [],
        "chart_weekly": [],
        "chip_data": None,
        "scrape_ok": False,
        "scrape_fail": "",
        "wait_strategy": WAIT_STRATEGY,
    }

    all_api_data = {}

    def handle_response(response):
        url = response.url
        if "csqaq.com/proxies/api" not in url:
            return
        try:
            body = response.text()
            if not body or len(body) > 2000000:
                return
            if url not in all_api_data:
                all_api_data[url] = []
            all_api_data[url].append({"status": response.status, "body": body})
        except Exception:
            pass

    page.on("response", handle_response)

    chart_url = "https://csqaq.com/proxies/api/v1/info/simple/chartAll"

    try:
        # 1. 访问详情页
        print(f"  [1] 访问详情页...", flush=True)
        try:
            page.goto(detail_url, wait_until="networkidle", timeout=30000)
        except Exception:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            wait_network_idle(page)

        # 2. 提取基本信息
        print(f"  [2] 提取基本信息...", flush=True)
        for url, responses in all_api_data.items():
            if "info/good" in url:
                last_resp = responses[-1]
                try:
                    parsed = json.loads(last_resp["body"])
                    if parsed.get("code") == 200 and parsed.get("data"):
                        item_result["detail"] = parsed["data"]
                        info_data = parsed["data"].get("goods_info", parsed["data"])
                        if not item_name and info_data.get("name"):
                            item_result["name"] = info_data["name"]
                        print(f"      ✓ {info_data.get('name', 'N/A')}", flush=True)
                        break
                except Exception as e:
                    print(f"      解析失败: {e}", flush=True)

        # 3. 点击 K 线图
        print(f"  [3] 点击 K 线图...", flush=True)
        page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.textContent.trim() === 'K线图') { btn.click(); return true; }
            }
            return false;
        }""")
        wait_network_idle(page)

        # 4. 切换平台到悠悠有品
        print(f"  [4] 切换平台到悠悠有品...", flush=True)
        select_info = page.evaluate("""() => {
            const selects = document.querySelectorAll('select');
            for (const sel of selects) {
                const options = Array.from(sel.options).map(o => ({text: o.text, value: o.value}));
                if (options.some(o => o.text === '悠悠有品')) {
                    return {value: options.find(o => o.text === '悠悠有品').value};
                }
            }
            return null;
        }""")
        if select_info:
            page.evaluate("""(targetValue) => {
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {
                    if (Array.from(sel.options).some(o => o.text === '悠悠有品')) {
                        sel.value = targetValue;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        sel.dispatchEvent(new Event('input', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }""", select_info["value"])
            wait_network_idle(page)
            print(f"      ✓ 切换完成", flush=True)

        # 5. 切换日线 + 翻页（10次）
        print(f"  [5] 切换日线 + 翻页({CHART_SCROLL_TIMES}次)...", flush=True)
        click_period(page, "日线")
        wait_network_idle(page)

        canvas_info = page.evaluate("""() => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return null;
            const rect = canvas.getBoundingClientRect();
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
        }""")

        if canvas_info:
            daily_strategy = get_strategy("日线")
            all_chart_daily = do_scroll_loop(page, all_api_data, canvas_info, chart_url, daily_strategy, CHART_SCROLL_TIMES, "日线")
            item_result["chart_daily"] = all_chart_daily
            print(f"      ✓ 日线总计: {len(all_chart_daily)} 条 (策略={daily_strategy})", flush=True)
        else:
            print(f"      [警告] 未找到 canvas，跳过日线翻页", flush=True)
            new_data, _ = parse_chart_responses(all_api_data, chart_url, 0)
            item_result["chart_daily"] = dedup_and_sort(new_data)

        # 6. 切换 1 小时 + 翻页（3次）
        print(f"  [6] 切换 1 小时 + 翻页({SCROLL_1H_TIMES}次)...", flush=True)

        # V10 方式：等待 3s + 重新激活日线 + 切换 1h
        page.wait_for_timeout(3000)
        click_period(page, "日线")
        page.wait_for_timeout(1000)

        before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
        click_result = click_period(page, "1小时")

        # 轮询等待 chartAll API 新响应
        v10_start = time.time()
        v10_success = False
        while time.time() - v10_start < 5:
            current_count = get_api_count(all_api_data, API_CHART_ALL)
            if current_count > before_chart_count:
                v10_success = True
                break
            page.wait_for_timeout(500)

        if v10_success:
            page.wait_for_timeout(2000)
            print(f"      ✓ V10 切换 1h 成功 ({time.time()-v10_start:.1f}s)", flush=True)
        else:
            # V9 补救：重新加载页面
            print(f"      V10 失败，启用 V9 补救...", flush=True)
            try:
                page.goto(detail_url, wait_until="networkidle", timeout=30000)
            except Exception:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                wait_network_idle(page)

            page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.trim() === 'K线图') { btn.click(); return true; }
                }
                return false;
            }""")
            wait_network_idle(page)

            if select_info:
                page.evaluate("""(targetValue) => {
                    const selects = document.querySelectorAll('select');
                    for (const sel of selects) {
                        if (Array.from(sel.options).some(o => o.text === '悠悠有品')) {
                            sel.value = targetValue;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            sel.dispatchEvent(new Event('input', {bubbles: true}));
                            return true;
                        }
                    }
                    return false;
                }""", select_info["value"])
                wait_network_idle(page)

            click_period(page, "日线")
            wait_network_idle(page)

            before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
            click_period(page, "1小时")

            if not wait_for_new_response(page, all_api_data, API_CHART_ALL, before_chart_count, timeout=20):
                print(f"      V9 补救也失败", flush=True)
            else:
                page.wait_for_timeout(2000)
                print(f"      ✓ V9 补救成功", flush=True)

        # 1h 翻页
        if canvas_info:
            # 重新获取 canvas（V9 可能重新加载了页面）
            canvas_info = page.evaluate("""() => {
                const canvas = document.querySelector('canvas');
                if (!canvas) return null;
                const rect = canvas.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }""")

        if canvas_info:
            # 清空之前 1h 的 API 响应计数，只获取 1h 切换后的数据
            before_1h_count = len(all_api_data.get(chart_url, []))
            h1_strategy = get_strategy("1h")
            all_chart_1h = do_scroll_loop(page, all_api_data, canvas_info, chart_url, h1_strategy, SCROLL_1H_TIMES, "1h")
            item_result["chart_1h"] = all_chart_1h
            print(f"      ✓ 1h 总计: {len(all_chart_1h)} 条 (策略={h1_strategy})", flush=True)
        else:
            print(f"      [警告] 未找到 canvas，跳过 1h 翻页", flush=True)

        # 7. 周线采集（CSQAQ 不支持周线，跳过）
        print(f"  [7] 周线采集: 跳过（CSQAQ 不支持周线数据）", flush=True)

        # 8. 筹码分布（V5）
        print(f"  [8] 点击筹码分布图...", flush=True)
        try:
            page.wait_for_timeout(1000)

            click_result = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    if (text === '筹码分布图' || text === '筹码分布') {
                        btn.click(); return 'button:' + text;
                    }
                }
                const chipEl = document.querySelector('.chip_tag___2aXfK');
                if (chipEl && chipEl.parentElement) {
                    chipEl.parentElement.click(); return 'parent';
                }
                const els = document.querySelectorAll('span, div, a, button, li, p');
                for (const el of els) {
                    const text = el.textContent.trim();
                    if ((text === '筹码分布图' || text === '筹码分布' || text === '筹码') && el.offsetParent !== null) {
                        el.click(); return 'text:' + text;
                    }
                }
                return false;
            }""")
            print(f"      点击返回: {click_result}", flush=True)

            chip_found = False
            chip_start = time.time()
            while time.time() - chip_start < 40:
                for url in all_api_data:
                    if API_CHIP_DATA in url and all_api_data[url]:
                        last_resp = all_api_data[url][-1]
                        try:
                            parsed = json.loads(last_resp["body"])
                            if parsed.get("code") == 200 and parsed.get("data"):
                                chip_full_data = parsed["data"]
                                item_result["chip_data"] = chip_full_data
                                print(f"      ✓ 筹码分布: {len(chip_full_data.get('date', []))} 天", flush=True)
                                chip_found = True
                                break
                        except Exception:
                            pass
                if chip_found:
                    break
                page.wait_for_timeout(500)

            if not chip_found:
                print(f"      chipData API 无响应（{time.time()-chip_start:.1f}s），跳过", flush=True)

        except Exception as e:
            print(f"      [筹码分布异常] {type(e).__name__}: {e}，跳过", flush=True)
            item_result["scrape_fail"] = f"筹码分布异常: {type(e).__name__}"

        if item_result["detail"]:
            item_result["scrape_ok"] = True
        else:
            item_result["scrape_fail"] = "无基本信息"

    except Exception as e:
        item_result["scrape_fail"] = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {type(e).__name__}: {e}", flush=True)

    page.remove_listener("response", handle_response)
    return item_result


def scrape_with_retry(page, goods_id, item_name=None, max_retries=None):
    if max_retries is None:
        max_retries = int(os.environ.get("MAX_RETRIES", "1"))
    for attempt in range(max_retries + 1):
        try:
            result = scrape_one(page, goods_id, item_name)
            if result["scrape_ok"]:
                return result
            if attempt < max_retries:
                print(f"  [重试] 第 {attempt+1} 次失败，重试中...", flush=True)
            else:
                print(f"  [失败] 重试次数已用完", flush=True)
        except Exception as e:
            if attempt < max_retries:
                print(f"  [异常重试] {type(e).__name__}，重试中...", flush=True)
            else:
                print(f"  [失败] 重试次数已用完: {e}", flush=True)

    return {
        "name": item_name or "",
        "goods_id": str(goods_id),
        "detail": None,
        "chart_daily": [],
        "chart_1h": [],
        "chart_weekly": [],
        "chip_data": None,
        "scrape_ok": False,
        "scrape_fail": "重试失败",
        "wait_strategy": WAIT_STRATEGY,
    }


def main():
    parser = argparse.ArgumentParser(description="CSQAQ 完整数据采集测试")
    parser.add_argument("--items-json", default="", help="批量 JSON 数组")
    parser.add_argument("--text", default="", help="单 item 饰品名称")
    parser.add_argument("--goods-id", default="", help="单 item 饰品ID")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print(f"  CSQAQ 完整数据采集测试 (策略={WAIT_STRATEGY})", flush=True)
    if WAIT_STRATEGY == "h":
        print(f"  方案H: 日线=E(300ms) 1h=G(500ms) 周线=跳过", flush=True)
    print(f"  日线翻页={CHART_SCROLL_TIMES} 1h翻页={SCROLL_1H_TIMES} 周线翻页=跳过", flush=True)
    print("=" * 60, flush=True)

    items = []
    if args.items_json:
        try:
            items = json.loads(args.items_json)
        except json.JSONDecodeError as e:
            print(f"[ERROR] items_json 解析失败: {e}", flush=True)
            return
    elif args.text and args.goods_id:
        items = [{"name": args.text, "goods_id": args.goods_id}]
    else:
        print("[ERROR] 未提供 items_json 或 text+goods_id", flush=True)
        return

    print(f"  饰品数量: {len(items)}", flush=True)

    results = []
    start_time = datetime.datetime.now()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            page.set_default_timeout(30000)

            for idx, item in enumerate(items):
                print(f"\n{'#'*60}", flush=True)
                print(f"  进度: {idx+1}/{len(items)} - {item.get('name', 'N/A')}", flush=True)
                print(f"{'#'*60}", flush=True)

                try:
                    result = scrape_with_retry(page, item["goods_id"], item.get("name"))

                    try:
                        page.wait_for_function("() => true", timeout=5000)
                    except Exception:
                        print(f"  [页面无响应] 重新创建 page...", flush=True)
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = context.new_page()
                        page.set_default_timeout(30000)

                except Exception as e:
                    print(f"  [FATAL] {type(e).__name__}: {e}", flush=True)
                    print(f"  [恢复] 重新创建 page...", flush=True)
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = context.new_page()
                    page.set_default_timeout(30000)
                    result = {
                        "name": item.get("name", ""),
                        "goods_id": str(item["goods_id"]),
                        "detail": None,
                        "chart_daily": [],
                        "chart_1h": [],
                        "chart_weekly": [],
                        "chip_data": None,
                        "scrape_ok": False,
                        "scrape_fail": f"事件循环损坏: {type(e).__name__}",
                        "wait_strategy": WAIT_STRATEGY,
                    }

                results.append(result)

                name = result["name"] or "N/A"
                daily_n = len(result["chart_daily"])
                h1_n = len(result["chart_1h"])
                weekly_n = len(result["chart_weekly"])
                chip_n = len(result["chip_data"].get("date", [])) if result["chip_data"] else 0
                ok = "✓" if result["scrape_ok"] else "✗"
                print(f"  → {ok} {name}: 日线{daily_n} 1h{h1_n} 周线{weekly_n} 筹码{chip_n}", flush=True)

            browser.close()

    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}", flush=True)

    end_time = datetime.datetime.now()
    duration = (end_time - start_time).total_seconds()

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    success_count = sum(1 for r in results if r["scrape_ok"])
    print(f"\n{'='*60}", flush=True)
    print(f"  汇总: {success_count}/{len(items)} 成功, 耗时 {duration:.0f}s", flush=True)
    for r in results:
        ok = "✓" if r["scrape_ok"] else "✗"
        print(f"    [{r['goods_id']}] {ok} {r['name']}: 日线{len(r['chart_daily'])} 1h{len(r['chart_1h'])} 周线{len(r['chart_weekly'])} 筹码{len(r['chip_data'].get('date', [])) if r['chip_data'] else 0}", flush=True)


if __name__ == "__main__":
    main()
