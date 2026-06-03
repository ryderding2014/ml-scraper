"""Patch app.py with anti-block features: human simulation, cooldown, persistent cache"""

# Read file
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# === 1. Add imports (time, json, random) ===
old_import = '''import re
import logging
import tempfile
import os
import asyncio'''
new_import = '''import re
import logging
import tempfile
import os
import json as _json
import time as _time
import random as _random
import asyncio'''
content = content.replace(old_import, new_import)

# === 2. Add persistent cache file path and cooldown settings ===
old_cache = '''# 运行时缓存（自动从 Google/Brasil API 查到的结果）
_seller_cache: dict = {}'''
new_cache = '''# 运行时缓存（自动从 Google/Brasil API 查到的结果）
_seller_cache: dict = {}

# 持久化缓存文件路径
_CACHE_FILE = os.path.join(tempfile.gettempdir(), "ml-seller-cache.json")

# 请求冷却：最小间隔秒数
_MIN_REQUEST_INTERVAL = 30
_last_request_time = 0

def _load_persistent_cache():
    """从磁盘加载持久化缓存"""
    global _seller_cache
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                _seller_cache = _json.load(f)
            logger.info(f"加载持久化缓存: {len(_seller_cache)} 条")
    except Exception:
        pass

def _save_persistent_cache():
    """保存缓存到磁盘"""
    try:
        with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
            _json.dump(_seller_cache, f, ensure_ascii=False)
    except Exception:
        pass

# 启动时加载缓存
_load_persistent_cache()'''
content = content.replace(old_cache, new_cache)

# === 3. Add human-like behavior function ===
old_behavior = '''PAGE_TIMEOUT_MS = 30_000
PAGE_SETTLE_MS = 5_000'''
new_behavior = '''PAGE_TIMEOUT_MS = 30_000
PAGE_SETTLE_MS = 5_000


async def _simulate_human(page):
    """模拟人类浏览行为：随机滚动 + 短暂停顿，避免被检测为机器人"""
    try:
        # 随机停顿 0.5-2 秒
        await page.wait_for_timeout(_random.randint(500, 2000))
        # 随机滚动页面
        scroll_y = _random.randint(100, 500)
        await page.evaluate(f"window.scrollBy(0, {scroll_y})")
        await page.wait_for_timeout(_random.randint(300, 1000))
    except Exception:
        pass'''
content = content.replace(old_behavior, new_behavior)

# === 4. Add request cooldown check in api_scrape ===
old_cooldown = '''    context = None
    try:
        pw = await async_playwright().start()
        context, browser = await _create_context(pw)'''
new_cooldown = '''    # 请求冷却检查
    global _last_request_time
    elapsed = _time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        wait_time = int(_MIN_REQUEST_INTERVAL - elapsed)
        logger.info(f"请求冷却中，等待 {wait_time} 秒...")
        await asyncio.sleep(wait_time)

    context = None
    browser = None
    try:
        pw = await async_playwright().start()
        context, browser = await _create_context(pw)'''
content = content.replace(old_cooldown, new_cooldown)

# === 5. Add _last_request_time update after successful query ===
old_update = '''        # 拼装最终数据
        data["legal_info"] = legal_info
        data["contacts"] = contacts'''
new_update = '''        _last_request_time = _time.time()

        # 拼装最终数据
        data["legal_info"] = legal_info
        data["contacts"] = contacts'''
content = content.replace(old_update, new_update)

# === 6. Add _save_persistent_cache after cache write ===
old_save = '''                    # 存入缓存（下次同 seller_id 直接复用）
                    if seller_id and best_score >= 50:'''
new_save = '''                    # 存入缓存（下次同 seller_id 直接复用）
                    if seller_id and best_score >= 50:
                        _save_persistent_cache()'''
content = content.replace(old_save, new_save)

# === 7. Add simulate_human calls in key navigation points ===
# After page.goto, add simulate_human
old_goto1 = '''            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(PAGE_SETTLE_MS)'''
new_goto1 = '''            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(PAGE_SETTLE_MS)
        await _simulate_human(page)'''
content = content.replace(old_goto1, new_goto1)

# In profile scraper
old_goto2 = '''            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(PAGE_SETTLE_MS)

        if await _detect_block(page):
            continue'''
new_goto2 = '''            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(PAGE_SETTLE_MS)
        await _simulate_human(page)

        if await _detect_block(page):
            continue'''
content = content.replace(old_goto2, new_goto2)

# In seller list scraper
old_goto3 = '''    await page.goto(original_url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(PAGE_SETTLE_MS)'''
new_goto3 = '''    await page.goto(original_url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(PAGE_SETTLE_MS)
    await _simulate_human(page)'''
content = content.replace(old_goto3, new_goto3)

# === 8. Improve browser cleanup ===
old_cleanup = '''        await context.close()
        await pw.stop()'''
new_cleanup = '''        await context.close()
        await browser.close()
        await pw.stop()'''
content = content.replace(old_cleanup, new_cleanup)

# === 9. Add _save_persistent_cache in the API flow after setting legal_info ===
# Find where we save to cache in the API flow
old_cache_save = '''                        _seller_cache[seller_id] = {
                            k: v for k, v in legal_info.items()
                            if k not in ("note", "source", "match_confidence", "cnpj_candidates")
                        }
                        _seller_cache[seller_id]["match_confidence"] = legal_info.get("match_confidence")'''
new_cache_save = '''                        _seller_cache[seller_id] = {
                            k: v for k, v in legal_info.items()
                            if k not in ("note", "source", "match_confidence", "cnpj_candidates")
                        }
                        _seller_cache[seller_id]["match_confidence"] = legal_info.get("match_confidence")
                        _save_persistent_cache()'''
content = content.replace(old_cache_save, new_cache_save)

# Write back
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch applied successfully")
