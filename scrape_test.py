#!/usr/bin/env python3
"""GitHub Actions Playwright 生产级抓取脚本 V11

在 GA runner 内运行，用 Playwright 抓取 CSQAQ 网页数据。
输出 result.json（list 格式，兼容旧方法）。

V11 优化（基于 scrape_test_v4.py 验证）：
- 移除 signal.alarm（破坏 Playwright 事件循环），改用 page.wait_for_timeout
- networkidle + API 响应计数双重保障（替代固定等待）
- V10（等待 3s + 重新激活日线 + 切换 1h）+ V9 补救（重新加载页面，先 1h）
- V5 筹码分布（优先点击 BUTTON + 不二次点击 + 40s 超时）
- page 无响应检测 + 自动恢复

用法：
  python scrape.py --items-json '[{"name":"AK-47","goods_id":"135"}]'
  python scrape.py --text "AK-47" --goods-id "135"
"""

import argparse
import datetime
import json
import os
import time
from playwright.sync_api import sync_playwright

DETAIL_URL = "https://csqaq.com/goods/{goods_id}"
RESULT_FILE = "result.json"

CHART_SCROLL_TIMES = int(os.environ.get("CHART_SCROLL_TIMES", "5"))
SINGLE_ITEM_TIMEOUT = int(os.environ.get("SINGLE_ITEM_TIMEOUT", "120"))

# API URL 模式（用于智能等待关键 API 返回）
API_CHART_ALL = "info/simple/chartAll"
API_CHIP_DATA = "info/chipData"


def wait_network_idle(page, timeout=15000):
    """等待网络空闲（所有 API 请求完成）"""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        # networkidle 超时，回退到短等待
        page.wait_for_timeout(2000)


def get_api_count(all_api_data, url_pattern):
    """获取特定 API 的响应总数"""
    return sum(len(all_api_data[u]) for u in all_api_data if url_pattern in u)


def wait_for_new_response(page, all_api_data, url_pattern, before_count, timeout=15):
    """等待特定 API 出现新响应（通过响应计数）

    networkidle 可能提前结束（500ms 无请求），但 API 可能还没返回。
    此函数确保关键 API 的新响应已到达，避免数据丢失。

    V11 修复：用 page.wait_for_timeout 替代 time.sleep，避免阻塞 Playwright 事件循环
    （time.sleep 会阻塞事件循环，导致 page.on("response") 回调不触发，all_api_data 不更新）
    """
    start = time.time()
    while time.time() - start < timeout:
        current_count = get_api_count(all_api_data, url_pattern)
        if current_count > before_count:
            return True
        page.wait_for_timeout(300)
    return False


def scrape_one(page, goods_id, item_name=None):
    """抓取单个饰品数据（V11：networkidle + V10+V9 补救 + V5 筹码分布）"""
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

    chart_url = "https://csqaq.com/proxies/api/v1/info/simple/chartAll"
    chip_url = "https://csqaq.com/proxies/api/v1/info/chipData"

    try:
        # 1. 访问详情页 + 等待网络空闲
        print(f"  [1] 访问详情页（等待网络空闲）...", flush=True)
        try:
            page.goto(detail_url, wait_until="networkidle", timeout=30000)
        except Exception:
            # networkidle 超时，回退到 domcontentloaded + 短等待
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            wait_network_idle(page)

        # 2. 提取基本信息（从已捕获的 API 响应中）
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

        # 3. 点击 K 线图 + 等待网络空闲
        print(f"  [3] 点击 K 线图（等待网络空闲）...", flush=True)
        page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.textContent.trim() === 'K线图') { btn.click(); return true; }
            }
            return false;
        }""")
        wait_network_idle(page)

        # 4. 切换平台到悠悠有品 + 等待网络空闲
        print(f"  [4] 切换平台到悠悠有品（等待网络空闲）...", flush=True)
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

        # 5. 切换日线 + 等待网络空闲
        print(f"  [5] 切换日线（等待网络空闲）...", flush=True)
        page.evaluate("""() => {
            const els = document.querySelectorAll('span, div, a, button');
            for (const el of els) {
                if (el.textContent.trim() === '日线' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        wait_network_idle(page)

        if chart_url in all_api_data and all_api_data[chart_url]:
            parsed = json.loads(all_api_data[chart_url][-1]["body"])
            if parsed.get("code") == 200:
                arr = parsed.get("data", [])
                if isinstance(arr, list):
                    all_chart_daily.extend(arr)
                    print(f"      初始日线: {len(arr)} 条", flush=True)

        # 6. 翻页（每次等待网络空闲 + API 新响应）
        print(f"  [6] 翻页（等待网络空闲 + chartAll API 新响应）...", flush=True)
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

                # 等待网络空闲 + chartAll API 新响应（双重保障）
                wait_network_idle(page, timeout=8000)
                if not wait_for_new_response(page, all_api_data, API_CHART_ALL, before_resp_count, timeout=8):
                    # API 没有新响应，短等待
                    page.wait_for_timeout(1000)

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

        # 7. 切换 1 小时（V11：V10 为主 + V9 补救）
        # V10：翻页后等待 3s + 重新激活日线 + 切换 1h（成功时 1h=346）
        # V9 补救：V10 失败时重新加载页面，先 1h（1h=150）
        print(f"  [7] 切换 1 小时（V11：V10 为主 + V9 补救）...", flush=True)

        # V10 方式：等待 3s 让页面状态稳定
        page.wait_for_timeout(3000)
        # 重新点击日线按钮（激活日线状态，翻页后页面 JS 状态可能损坏）
        page.evaluate("""() => {
            const els = document.querySelectorAll('span, div, a, button');
            for (const el of els) {
                if (el.textContent.trim() === '日线' && el.offsetParent !== null) { el.click(); return true; }
            }
            return false;
        }""")
        page.wait_for_timeout(1000)

        # 切换 1h
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

        # 轮询等待 chartAll API 新响应（V10：5s 超时，成功通常 0.5s）
        v10_start = time.time()
        v10_success = False
        while time.time() - v10_start < 5:
            current_count = get_api_count(all_api_data, API_CHART_ALL)
            if current_count > before_chart_count:
                v10_success = True
                break
            page.wait_for_timeout(500)

        if v10_success:
            # 等待数据完整
            page.wait_for_timeout(2000)
            if chart_url in all_api_data and all_api_data[chart_url]:
                latest_idx = len(all_api_data[chart_url]) - 1
                try:
                    parsed = json.loads(all_api_data[chart_url][latest_idx]["body"])
                    if parsed.get("code") == 200:
                        arr = parsed.get("data", [])
                        if isinstance(arr, list):
                            all_chart_1h.extend(arr)
                            print(f"      ✓ V10 成功 ({time.time()-v10_start:.1f}s): 1 小时 {len(arr)} 条", flush=True)
                except Exception as e:
                    print(f"      V10 解析失败: {e}", flush=True)
        else:
            # V9 补救：重新加载页面，先 1h（不翻页）
            print(f"      V10 失败 ({time.time()-v10_start:.1f}s)，启用 V9 补救...", flush=True)
            try:
                page.goto(detail_url, wait_until="networkidle", timeout=30000)
            except Exception:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                wait_network_idle(page)

            # 点击 K 线图
            page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.trim() === 'K线图') { btn.click(); return true; }
                }
                return false;
            }""")
            wait_network_idle(page)

            # 切换平台到悠悠有品
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

            # 切换日线（激活）
            page.evaluate("""() => {
                const els = document.querySelectorAll('span, div, a, button');
                for (const el of els) {
                    if (el.textContent.trim() === '日线' && el.offsetParent !== null) { el.click(); return true; }
                }
                return false;
            }""")
            wait_network_idle(page)

            # 切换 1h
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

            # 轮询等待 chartAll API 新响应（V9：20s 超时）
            if not wait_for_new_response(page, all_api_data, API_CHART_ALL, before_chart_count, timeout=20):
                print(f"      V9 补救也失败", flush=True)
            else:
                page.wait_for_timeout(2000)
                if chart_url in all_api_data and all_api_data[chart_url]:
                    latest_idx = len(all_api_data[chart_url]) - 1
                    try:
                        parsed = json.loads(all_api_data[chart_url][latest_idx]["body"])
                        if parsed.get("code") == 200:
                            arr = parsed.get("data", [])
                            if isinstance(arr, list):
                                all_chart_1h.extend(arr)
                                print(f"      ✓ V9 补救成功: 1 小时 {len(arr)} 条", flush=True)
                    except Exception as e:
                        print(f"      V9 解析失败: {e}", flush=True)

        item_result["chart_1h"] = all_chart_1h

        # 8. 筹码分布（V5：优先点击 BUTTON + 不二次点击 + 40 秒超时）
        print(f"  [8] 点击筹码分布图...", flush=True)
        try:
            # 点击前等待 1 秒（确保 JS 加载）
            page.wait_for_timeout(1000)

            # 优先点击 BUTTON 元素（.chip_tag___2aXfK 是 SPAN，点击它不触发 API）
            click_result = page.evaluate("""() => {
                // 1. 优先点击包含"筹码分布图"文本的 BUTTON
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    if (text === '筹码分布图' || text === '筹码分布') {
                        btn.click(); return 'button:' + text;
                    }
                }
                // 2. 点击 .chip_tag___2aXfK 的父元素（BUTTON）
                const chipEl = document.querySelector('.chip_tag___2aXfK');
                if (chipEl && chipEl.parentElement) {
                    chipEl.parentElement.click(); return 'parent';
                }
                // 3. 文本匹配兜底
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

            # 轮询等待 chipData API 响应（最多 40 秒，摩托手套需要 12-34 秒）
            # 不二次点击（二次点击会取消第一次的 API 请求）
            # 用 page.wait_for_timeout() 等待（不阻塞事件循环）
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

            elapsed = time.time() - chip_start
            if not chip_found:
                print(f"      chipData API 无响应（{elapsed:.1f}s），跳过", flush=True)

        except Exception as e:
            print(f"      [筹码分布异常] {type(e).__name__}: {e}，跳过", flush=True)
            item_result["scrape_fail"] = f"筹码分布异常: {type(e).__name__}"

        # 标记成功
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
    """带重试的抓取（移除 signal.alarm，避免破坏 Playwright 事件循环）"""
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
        "chip_data": None,
        "scrape_ok": False,
        "scrape_fail": "重试失败",
    }


def main():
    parser = argparse.ArgumentParser(description="CSQAQ Playwright 抓取 V11")
    parser.add_argument("--items-json", default="", help="批量 JSON 数组")
    parser.add_argument("--text", default="", help="单 item 饰品名称")
    parser.add_argument("--goods-id", default="", help="单 item 饰品ID")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("  CSQAQ Playwright 抓取 V11（networkidle + V10+V9 补救）", flush=True)
    print("=" * 60, flush=True)

    # 解析 items
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

                    # 检测页面是否仍然响应（防止 JS 卡死后继续操作）
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
                    # Playwright 事件循环损坏，重新创建 page
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
                        "chip_data": None,
                        "scrape_ok": False,
                        "scrape_fail": f"事件循环损坏: {type(e).__name__}",
                    }

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
