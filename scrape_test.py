#!/usr/bin/env python3
"""GitHub Actions Playwright 生产级抓取脚本

在 GA runner 内运行，用 Playwright 抓取 CSQAQ 网页数据。
输出 result.json（list 格式，兼容旧方法）。

用法：
  python scrape.py --items-json '[{"name":"AK-47","goods_id":"135"}]'
  python scrape.py --text "AK-47" --goods-id "135"
"""

import argparse
import json
import os
import datetime
import signal
from playwright.sync_api import sync_playwright

DETAIL_URL = "https://csqaq.com/goods/{goods_id}"
RESULT_FILE = "result.json"

CHART_SCROLL_TIMES = 3  # 方向 C：减少翻页次数 5→3
SINGLE_ITEM_TIMEOUT = int(os.environ.get("SINGLE_ITEM_TIMEOUT", "120"))


class TimeoutException(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutException("单饰品抓取超时")


def scrape_one(page, goods_id, item_name=None):
    """抓取单个饰品数据"""
    print(f"\n{'='*60}", flush=True)
    print(f"  抓取饰品: goods_id={goods_id} name={item_name}", flush=True)
    print(f"{'='*60}", flush=True)

    detail_url = DETAIL_URL.format(goods_id=goods_id)
    item_result = {
        "name": item_name or "",
        "goods_id": str(goods_id),
        "detail": None,
        "chart_daily": [],
        "chart_1h": [],
        "chip_data": None,
        "scrape_ok": False,
        "scrape_fail": "",
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

    all_chart_daily = []
    all_chart_1h = []
    chip_full_data = None

    try:
        # 1. 访问详情页
        print(f"  [1] 访问详情页...", flush=True)
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

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
        page.wait_for_timeout(2000)

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
            page.wait_for_timeout(2000)
            print(f"      ✓ 切换完成", flush=True)

        # 5. 切换日线 + 翻页
        print(f"  [5] 切换日线并翻页...", flush=True)
        chart_url = "https://csqaq.com/proxies/api/v1/info/simple/chartAll"

        page.evaluate("""() => {
            const els = document.querySelectorAll('span, div, a, button');
            for (const el of els) {
                if (el.textContent.trim() === '日线' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        page.wait_for_timeout(3000)

        if chart_url in all_api_data and all_api_data[chart_url]:
            parsed = json.loads(all_api_data[chart_url][-1]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    all_chart_daily.extend(arr)
                    print(f"      初始日线: {len(arr)} 条", flush=True)

        canvas_info = page.evaluate("""() => {
            const canvas = document.querySelector('canvas');
            if (!canvas) return null;
            const rect = canvas.getBoundingClientRect();
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
        }""")

        if canvas_info:
            center_y = canvas_info["y"] + canvas_info["height"] / 2
            no_new_count = 0

            for i in range(CHART_SCROLL_TIMES):
                before_total = len(all_chart_daily)
                before_resp_count = len(all_api_data.get(chart_url, []))

                page.mouse.move(canvas_info["x"] + canvas_info["width"] / 2, center_y)
                for _ in range(3):
                    page.mouse.wheel(-3000, 0)
                    page.wait_for_timeout(400)
                page.wait_for_timeout(800)

                current_resp_count = len(all_api_data.get(chart_url, []))
                if current_resp_count > before_resp_count:
                    for idx in range(before_resp_count, current_resp_count):
                        parsed = json.loads(all_api_data[chart_url][idx]["body"])
                        if parsed.get("code") == 200:
                            arr = parsed.get("data", [])
                            if isinstance(arr, list) and len(arr) > 0:
                                all_chart_daily.extend(arr)
                                print(f"      翻页 {i+1}: +{len(arr)} 条, 总计 {len(all_chart_daily)} 条", flush=True)

                if len(all_chart_daily) == before_total:
                    no_new_count += 1
                    if no_new_count >= 3:
                        print(f"      连续 3 次无新数据，停止翻页", flush=True)
                        break
                else:
                    no_new_count = 0

        # 去重日线
        seen_t = set()
        unique_daily = []
        for item in all_chart_daily:
            t = item.get("t")
            if t and t not in seen_t:
                seen_t.add(t)
                unique_daily.append(item)
        all_chart_daily = unique_daily
        all_chart_daily.sort(key=lambda x: int(x.get("t", 0)))
        item_result["chart_daily"] = all_chart_daily
        print(f"      ✓ 日线总计: {len(all_chart_daily)} 条", flush=True)

        # 6. 切换 1 小时
        print(f"  [6] 切换 1 小时...", flush=True)
        page.evaluate("""() => {
            const targets = ['1小时', '1H', '1h'];
            const els = document.querySelectorAll('span, div, a, button, li');
            for (const target of targets) {
                for (const el of els) {
                    if (el.textContent.trim() === target && el.offsetParent !== null) { el.click(); return true; }
                }
            }
            return false;
        }""")
        page.wait_for_timeout(3000)

        if chart_url in all_api_data and all_api_data[chart_url]:
            latest_idx = len(all_api_data[chart_url]) - 1
            parsed = json.loads(all_api_data[chart_url][latest_idx]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    all_chart_1h.extend(arr)
                    print(f"      ✓ 1 小时: {len(arr)} 条", flush=True)

        item_result["chart_1h"] = all_chart_1h

        # 7. 筹码分布
        print(f"  [7] 点击筹码分布图...", flush=True)
        chip_url = "https://csqaq.com/proxies/api/v1/info/chipData"

        page.evaluate("""() => {
            const chipEl = document.querySelector('.chip_tag___2aXfK');
            if (chipEl) { chipEl.click(); return 'class'; }
            const els = document.querySelectorAll('span, div, a, button, li, p');
            for (const el of els) {
                const text = el.textContent.trim();
                if ((text === '筹码分布图' || text === '筹码分布' || text === '筹码') && el.offsetParent !== null) {
                    el.click(); return 'text:' + text;
                }
            }
            return false;
        }""")
        page.wait_for_timeout(6000)

        if chip_url not in all_api_data:
            page.evaluate("""() => {
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    const text = el.textContent.trim();
                    if (text.includes('筹码') && text.length < 20 && el.offsetParent !== null && el.children.length === 0) {
                        el.click(); return text;
                    }
                }
                return false;
            }""")
            page.wait_for_timeout(6000)

        if chip_url in all_api_data and all_api_data[chip_url]:
            last_resp = all_api_data[chip_url][-1]
            parsed = json.loads(last_resp["body"])
            if parsed.get("code") == 200 and parsed.get("data"):
                chip_full_data = parsed["data"]
                item_result["chip_data"] = chip_full_data
                print(f"      ✓ 筹码分布: {len(chip_full_data.get('date', []))} 天", flush=True)

        # 标记成功
        if item_result["detail"]:
            item_result["scrape_ok"] = True
        else:
            item_result["scrape_fail"] = "无基本信息"

    except TimeoutException as e:
        item_result["scrape_fail"] = f"超时: {e}"
        print(f"  [TIMEOUT] {e}", flush=True)
    except Exception as e:
        item_result["scrape_fail"] = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {type(e).__name__}: {e}", flush=True)

    page.remove_listener("response", handle_response)
    return item_result


def scrape_with_retry(page, goods_id, item_name=None, max_retries=None):
    """带重试的抓取"""
    if max_retries is None:
        max_retries = int(os.environ.get("MAX_RETRIES", "1"))
    for attempt in range(max_retries + 1):
        try:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(SINGLE_ITEM_TIMEOUT)

            result = scrape_one(page, goods_id, item_name)

            signal.alarm(0)

            if result["scrape_ok"]:
                return result

            if attempt < max_retries:
                print(f"  [重试] 第 {attempt+1} 次失败，重试中...", flush=True)
            else:
                print(f"  [失败] 重试次数已用完", flush=True)

        except TimeoutException:
            signal.alarm(0)
            if attempt < max_retries:
                print(f"  [超时重试] 第 {attempt+1} 次超时，重试中...", flush=True)
            else:
                print(f"  [失败] 超时重试次数已用完", flush=True)
        except Exception as e:
            signal.alarm(0)
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
        "chip_data": None,
        "scrape_ok": False,
        "scrape_fail": "重试失败",
    }


def main():
    parser = argparse.ArgumentParser(description="CSQAQ Playwright 抓取")
    parser.add_argument("--items-json", default="", help="批量 JSON 数组")
    parser.add_argument("--text", default="", help="单 item 饰品名称")
    parser.add_argument("--goods-id", default="", help="单 item 饰品ID")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("  CSQAQ Playwright 生产级抓取", flush=True)
    print("=" * 60, flush=True)

    # 解析 items
    items = []
    if args.items_json:
        try:
            items = json.loads(args.items_json)
        except json.JSONDecodeError as e:
            print(f"[ERROR] items_json 解析失败: {e}", flush=True)
            items = []
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

            for idx, item in enumerate(items):
                print(f"\n{'#'*60}", flush=True)
                print(f"  进度: {idx+1}/{len(items)} - {item.get('name', 'N/A')}", flush=True)
                print(f"{'#'*60}", flush=True)

                result = scrape_with_retry(page, item["goods_id"], item.get("name"))
                results.append(result)

                name = result["name"] or "N/A"
                daily_n = len(result["chart_daily"])
                h1_n = len(result["chart_1h"])
                chip_n = len(result["chip_data"].get("date", [])) if result["chip_data"] else 0
                ok = "✓" if result["scrape_ok"] else "✗"
                print(f"  → {ok} {name}: 日线{daily_n} 1h{h1_n} 筹码{chip_n}", flush=True)

            browser.close()

    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}", flush=True)

    end_time = datetime.datetime.now()
    duration = (end_time - start_time).total_seconds()

    # 保存结果
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    # 汇总
    success_count = sum(1 for r in results if r["scrape_ok"])
    print(f"\n{'='*60}", flush=True)
    print(f"  汇总: {success_count}/{len(items)} 成功, 耗时 {duration:.0f}s", flush=True)
    for r in results:
        ok = "✓" if r["scrape_ok"] else "✗"
        print(f"    [{r['goods_id']}] {ok} {r['name']}", flush=True)


if __name__ == "__main__":
    main()
