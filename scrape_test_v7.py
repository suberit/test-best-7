#!/usr/bin/env python3
"""GitHub Actions Playwright 抓取脚本 V7（响应计数等待）

改进：通过响应计数判断是否有新数据，最精确的等待方式。
每次操作前记录 before_count，操作后等待计数增加。
确保是"新触发的 API 响应"，而不是旧缓存。
"""
import argparse
import datetime
import json
import os
import signal
import time
from playwright.sync_api import sync_playwright

DETAIL_URL = "https://csqaq.com/goods/{goods_id}"
RESULT_FILE = "result.json"

CHART_SCROLL_TIMES = int(os.environ.get("CHART_SCROLL_TIMES", "5"))
SINGLE_ITEM_TIMEOUT = int(os.environ.get("SINGLE_ITEM_TIMEOUT", "120"))

# API URL 模式
API_INFO_GOOD = "info/good"
API_CHART_ALL = "info/simple/chartAll"
API_CHIP_DATA = "info/chipData"


class TimeoutException(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutException("单饰品抓取超时")


def get_api_count(all_api_data, url_pattern):
    """获取特定 API 的响应总数"""
    return sum(len(all_api_data[u]) for u in all_api_data if url_pattern in u)


def wait_for_new_response(all_api_data, url_pattern, before_count, timeout=15):
    """等待特定 API 出现新响应（通过响应计数）

    通过响应计数判断是否有新数据：
    - 操作前记录 before_count
    - 操作后轮询检查计数是否增加
    - 计数增加表示有新 API 响应，立即返回
    - 超时返回 False
    """
    start = time.time()
    while time.time() - start < timeout:
        current_count = get_api_count(all_api_data, url_pattern)
        if current_count > before_count:
            return True
        time.sleep(0.3)
    return False


def scrape_one(page, goods_id, item_name=None):
    """抓取单个饰品数据（响应计数等待版）"""
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
        # 1. 访问详情页 + 等待 info/good API 新响应
        print(f"  [1] 访问详情页（等待 info/good API 新响应）...", flush=True)
        before_info_count = get_api_count(all_api_data, API_INFO_GOOD)
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        if not wait_for_new_response(all_api_data, API_INFO_GOOD, before_info_count, timeout=15):
            print(f"      info/good API 超时，回退等待", flush=True)
            page.wait_for_timeout(3000)

        # 2. 提取基本信息
        print(f"  [2] 提取基本信息...", flush=True)
        for url, responses in all_api_data.items():
            if API_INFO_GOOD in url:
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

        # 3. 点击 K 线图 + 等待 chartAll API 新响应
        print(f"  [3] 点击 K 线图（等待 chartAll API 新响应）...", flush=True)
        before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
        page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.textContent.trim() === 'K线图') { btn.click(); return true; }
            }
            return false;
        }""")
        if not wait_for_new_response(all_api_data, API_CHART_ALL, before_chart_count, timeout=15):
            print(f"      chartAll API 超时，回退等待", flush=True)
            page.wait_for_timeout(2000)

        # 4. 切换平台到悠悠有品 + 等待 chartAll API 新响应
        print(f"  [4] 切换平台到悠悠有品（等待 chartAll API 新响应）...", flush=True)
        before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
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
            if not wait_for_new_response(all_api_data, API_CHART_ALL, before_chart_count, timeout=15):
                page.wait_for_timeout(2000)
            print(f"      ✓ 切换完成", flush=True)

        # 5. 切换日线 + 等待 chartAll API 新响应
        print(f"  [5] 切换日线（等待 chartAll API 新响应）...", flush=True)
        before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
        page.evaluate("""() => {
            const els = document.querySelectorAll('span, div, a, button');
            for (const el of els) {
                if (el.textContent.trim() === '日线' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        if not wait_for_new_response(all_api_data, API_CHART_ALL, before_chart_count, timeout=15):
            page.wait_for_timeout(2000)

        chart_url = None
        for url in all_api_data:
            if API_CHART_ALL in url:
                chart_url = url
                break

        if chart_url and all_api_data[chart_url]:
            parsed = json.loads(all_api_data[chart_url][-1]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    all_chart_daily.extend(arr)
                    print(f"      初始日线: {len(arr)} 条", flush=True)

        # 6. 翻页（每次等待 chartAll API 新响应）
        print(f"  [6] 翻页（等待 chartAll API 新响应）...", flush=True)
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
                before_resp_count = len(all_api_data.get(chart_url, [])) if chart_url else 0

                page.mouse.move(canvas_info["x"] + canvas_info["width"] / 2, center_y)
                for _ in range(5):
                    page.mouse.wheel(-1500, 0)
                    page.wait_for_timeout(300)

                # 等待 chartAll API 新响应（通过响应计数）
                if not wait_for_new_response(all_api_data, API_CHART_ALL, before_resp_count, timeout=8):
                    page.wait_for_timeout(1000)

                current_resp_count = len(all_api_data.get(chart_url, [])) if chart_url else 0
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

        # 7. 切换 1 小时 + 等待 chartAll API 新响应
        print(f"  [7] 切换 1 小时（等待 chartAll API 新响应）...", flush=True)
        before_chart_count = get_api_count(all_api_data, API_CHART_ALL)
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
        if not wait_for_new_response(all_api_data, API_CHART_ALL, before_chart_count, timeout=15):
            page.wait_for_timeout(2000)

        if chart_url and all_api_data[chart_url]:
            latest_idx = len(all_api_data[chart_url]) - 1
            parsed = json.loads(all_api_data[chart_url][latest_idx]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    all_chart_1h.extend(arr)
                    print(f"      ✓ 1 小时: {len(arr)} 条", flush=True)

        item_result["chart_1h"] = all_chart_1h

        # 8. 筹码分布 + 等待 chipData API 新响应
        print(f"  [8] 点击筹码分布图（等待 chipData API 新响应）...", flush=True)
        before_chip_count = get_api_count(all_api_data, API_CHIP_DATA)
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
        if not wait_for_new_response(all_api_data, API_CHIP_DATA, before_chip_count, timeout=15):
            # 超时，重试点击
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
            if not wait_for_new_response(all_api_data, API_CHIP_DATA, before_chip_count, timeout=10):
                page.wait_for_timeout(3000)

        chip_url = None
        for url in all_api_data:
            if API_CHIP_DATA in url:
                chip_url = url
                break

        if chip_url and all_api_data[chip_url]:
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
    parser = argparse.ArgumentParser(description="CSQAQ Playwright 抓取 V7")
    parser.add_argument("--items-json", default="", help="批量 JSON 数组")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("  CSQAQ Playwright 抓取 V7（响应计数等待）", flush=True)
    print("=" * 60, flush=True)

    items = []
    if args.items_json:
        try:
            items = json.loads(args.items_json)
        except json.JSONDecodeError as e:
            print(f"[ERROR] items_json 解析失败: {e}", flush=True)
            return
    else:
        print("[ERROR] 未提供 items_json", flush=True)
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

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    success_count = sum(1 for r in results if r["scrape_ok"])
    print(f"\n{'='*60}", flush=True)
    print(f"  汇总: {success_count}/{len(items)} 成功, 耗时 {duration:.0f}s", flush=True)
    for r in results:
        ok = "✓" if r["scrape_ok"] else "✗"
        print(f"    [{r['goods_id']}] {ok} {r['name']}", flush=True)


if __name__ == "__main__":
    main()
