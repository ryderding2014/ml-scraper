"""
Mercado Livre 卖家信息快照 + CNPJ 反查工具
==========================================
流程：
  1. 解析 ML 商品/店铺链接，提取卖家名称 & 信誉数据
  2. 通过 Google 搜索 "{卖家名} CNPJ"，从搜索结果提取 CNPJ 和 Razão Social
  3. 通过 Brasil API (publica.cnpj.ws) 验证 CNPJ 并获取完整法律信息

技术栈: FastAPI + Playwright (无头 Chromium + 反检测)
"""

import re
import logging
import tempfile
import os
import asyncio
from urllib.parse import urlparse, unquote, quote
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ml-scraper")

app = FastAPI(title="ML Seller Snapshot + CNPJ")
templates = Jinja2Templates(directory="templates")

PAGE_TIMEOUT_MS = 30_000
PAGE_SETTLE_MS = 4_000
CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
ML_OWN_CNPJ = "03.007.331/0001-41"


class ScrapeRequest(BaseModel):
    url: str = Field(..., min_length=10)


# =========================================================================
# 反检测脚本
# =========================================================================

STEALTH_SCRIPT = r"""
delete Object.getPrototypeOf(navigator).webdriver;
(function () {
    var makePlugin = function (name, filename, desc) {
        var p = { name: name, filename: filename, description: desc, length: 1 };
        p.item = function (i) { return p[i] || null; };
        p.namedItem = function (n) { return p.name === n ? p : null; };
        Object.setPrototypeOf(p, Plugin.prototype);
        return p;
    };
    var arr = [
        makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
        makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
        makePlugin('Native Client', 'internal-nacl-plugin', ''),
    ];
    arr.item = function (i) { return arr[i] || null; };
    arr.namedItem = function (n) { return arr.find(function (p) { return p.name === n; }) || null; };
    arr.refresh = function () {};
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(navigator, 'plugins', { get: function () { return arr; }, configurable: true });
})();
Object.defineProperty(navigator, 'mimeTypes', {
    get: function () {
        var arr = [{ type: 'application/pdf', suffixes: 'pdf', description: '' }];
        arr.item = function (i) { return arr[i] || null; };
        arr.namedItem = function (n) { return arr.find(function (m) { return m.type === n; }) || null; };
        arr.refresh = function () {};
        Object.setPrototypeOf(arr, MimeTypeArray.prototype);
        return arr;
    },
});
Object.defineProperty(navigator, 'languages', { get: function () { return ['pt-BR', 'pt', 'en-US', 'en']; } });
Object.defineProperty(navigator, 'language', { get: function () { return 'pt-BR'; } });
window.chrome = { runtime: { connect: function () {}, sendMessage: function () {} }, loadTimes: function () { return {}; }, csi: function () { return {}; }, app: {} };
var origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = function (params) {
    if (params.name === 'notifications') return Promise.resolve({ state: Notification.permission, onchange: null });
    return origQuery.call(this, params);
};
Object.defineProperty(navigator, 'hardwareConcurrency', { get: function () { return 8; } });
Object.defineProperty(navigator, 'deviceMemory', { get: function () { return 8; } });
Object.defineProperty(navigator, 'platform', { get: function () { return 'Win32'; } });
Object.defineProperty(navigator, 'vendor', { get: function () { return 'Google Inc.'; } });
(function () {
    var handler = {
        apply: function (target, ctx, args) {
            if (args[0] === 37445) return 'Google Inc. (Intel)';
            if (args[0] === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return Reflect.apply(target, ctx, args);
        },
    };
    if (WebGLRenderingContext && WebGLRenderingContext.prototype.getParameter) {
        WebGLRenderingContext.prototype.getParameter = new Proxy(WebGLRenderingContext.prototype.getParameter, handler);
    }
    if (WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter) {
        WebGL2RenderingContext.prototype.getParameter = new Proxy(WebGL2RenderingContext.prototype.getParameter, handler);
    }
})();
if (window.__playwright__) delete window.__playwright__;
if (window.__pw_manual__) delete window.__pw_manual__;
"""

# =========================================================================
# URL 解析
# =========================================================================


def parse_url(url: str) -> tuple[str, str]:
    """返回 (type, identifier)"""
    url_clean = url.strip()
    parsed = urlparse(url_clean)
    path = unquote(parsed.path)
    host = parsed.hostname or ""

    if "perfil.mercadolivre" in host:
        parts = [p for p in path.strip("/").split("/") if p]
        if parts:
            return ("profile", parts[0])

    if "loja.mercadolivre" in host:
        parts = [p for p in path.strip("/").split("/") if p]
        if parts:
            return ("profile", parts[0])

    for pattern in [r"/perfil/([^/?#]+)", r"/loja/([^/?#]+)", r"/pagina/([^/?#]+)"]:
        m = re.search(pattern, path)
        if m:
            return ("profile", m.group(1))

    m = re.search(r"/p/MLB(\d+)", url_clean, re.IGNORECASE)
    if m:
        return ("product", f"MLB{m.group(1)}")

    m = re.search(r"MLB-?(\d+)", url_clean, re.IGNORECASE)
    if m:
        return ("product", f"MLB{m.group(1)}")

    # _CustId_XXXXX (卖家商品列表页)
    m = re.search(r"_CustId_(\d+)", url_clean, re.IGNORECASE)
    if m:
        return ("seller_list", m.group(1))

    raise ValueError(
        "URL 无法识别。请提供商品链接 (MLB-...)、店铺链接 "
        "(loja.mercadolivre.com.br/...) 或卖家主页链接。"
    )


# =========================================================================
# 封锁检测
# =========================================================================

BLOCK_KEYWORDS = [
    "hubo un error accediendo", "ocorreu um erro ao acessar",
    "captcha", "verificacao", "desafio", "verify you are human",
    "nao sou um robo", "pressione e segure", "press and hold",
    "cf-challenge", "challenge-platform", "attention required",
]


async def _detect_block(page) -> Optional[str]:
    try:
        text = (await page.evaluate("document.body.innerText.substring(0, 3000)")).lower()
        for kw in BLOCK_KEYWORDS:
            if kw in text:
                return f"检测到反爬拦截 (关键词: '{kw}')"
    except Exception:
        pass
    return None


# =========================================================================
# 浏览器工厂
# =========================================================================

_USER_DATA_DIR = os.path.join(tempfile.gettempdir(), "ml-scraper-profile")


async def _create_context(pw):
    os.makedirs(_USER_DATA_DIR, exist_ok=True)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=_USER_DATA_DIR,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-setuid-sandbox", "--no-first-run",
            "--disable-default-apps", "--disable-infobars",
            "--disable-background-networking", "--disable-sync",
            # 内存优化
            "--disable-features=TranslateUI",
            "--disable-extensions",
        ],
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        geolocation={"latitude": -23.5505, "longitude": -46.6333},
        permissions=["geolocation"],
        ignore_https_errors=True,
    )
    await context.add_init_script(STEALTH_SCRIPT)
    return context


# =========================================================================
# 第一步：从 ML 商品页提取卖家信息
# =========================================================================


async def _extract_seller_from_product(page, mlb_id: str, original_url: str) -> dict:
    """访问 ML 商品页，提取卖家公开信息"""
    urls_to_try = []
    if original_url:
        urls_to_try.append(original_url)
    urls_to_try.extend([
        f"https://www.mercadolivre.com.br/p/MLB{mlb_id.replace('MLB', '')}",
        f"https://www.mercadolivre.com.br/anuncio/{mlb_id}",
    ])

    for url in urls_to_try:
        logger.info(f"访问商品页: {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        except PlaywrightTimeout:
            continue
        await page.wait_for_timeout(PAGE_SETTLE_MS)

        block = await _detect_block(page)
        if block:
            raise RuntimeError(f"CAPTCHA: {block}")

        html = await page.evaluate("document.documentElement.outerHTML")

        # seller_id
        sid_match = re.search(r'"seller_id":(\d+)', html)
        seller_id = sid_match.group(1) if sid_match else None

        # 卖家名 & 链接
        link_info = await page.evaluate("""() => {
            var a = document.querySelector('.ui-pdp-seller__link');
            if (a) return {name: a.textContent.trim(), url: a.href};
            return null;
        }""")
        if not link_info:
            continue

        seller_name = link_info["name"]
        store_url = link_info["url"]

        # 信誉数据
        reputation = await _extract_reputation(page)

        # profile nick
        nick = await page.evaluate("""(name) => {
            var a = document.querySelector('.ui-pdp-seller__link');
            if (!a) return name;
            var h = a.href;
            var m = h.match(/perfil\\.mercadolivre\\.com\\.br\\/([^/?#]+)/);
            if (m) return m[1];
            return name;
        }""", seller_name)

        return {
            "seller_name": seller_name,
            "seller_id": seller_id,
            "store_url": store_url,
            "profile_url": f"https://perfil.mercadolivre.com.br/{nick}",
            "profile_nick": nick,
            "reputation": reputation,
            "source": "商品页卖家信息区域",
        }

    raise ValueError("无法在商品页找到卖家信息，请检查 URL 是否正确。")


async def _extract_reputation(page) -> dict:
    """从商品页卖家区域提取信誉数据"""
    raw = await page.evaluate("""() => {
        var el = document.querySelector('.ui-pdp-container__row--seller-data');
        if (!el) el = document.querySelector('.ui-pdp-seller');
        if (!el) return '';
        return el.textContent.trim();
    }""")

    result = {}
    for level in ["MercadoLíder Platinum", "MercadoLíder Gold", "MercadoLíder Silver", "MercadoLíder"]:
        if level in raw:
            result["level"] = level
            break

    if "um dos melhores do site" in raw.lower():
        result["description"] = "É um dos melhores do site!"

    sales_match = re.search(r"\+?([\d.]+\s*(?:mil|mi|k)?)\s*Vendas?", raw, re.IGNORECASE)
    if sales_match:
        result["sales"] = sales_match.group(1).strip() + " Vendas"

    fm = re.search(r"\+?([\d.]+\s*(?:mil|mi|k)?)\s*Seguidores?", raw, re.IGNORECASE)
    if fm:
        result["followers"] = fm.group(1).strip() + " Seguidores"

    pm = re.search(r"\+?([\d.]+\s*(?:mil|mi|k)?)\s*Produtos?", raw, re.IGNORECASE)
    if pm:
        result["products"] = pm.group(1).strip() + " Produtos"

    if "Bom atendimento" in raw:
        result["service"] = "Bom atendimento"
    if "Entrega no prazo" in raw:
        result["delivery"] = "Entrega no prazo"

    return result


async def _scrape_seller_list_page(page, seller_id: str, original_url: str) -> dict:
    """
    访问 _CustId_ 卖家商品列表页，提取卖家名，然后用第一个商品获取完整信息。
    """
    logger.info(f"访问卖家列表页: {original_url}")
    await page.goto(original_url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(PAGE_SETTLE_MS)

    block = await _detect_block(page)
    if block:
        raise RuntimeError(f"CAPTCHA: {block}")

    # 从页面标题提取卖家名（如 "Anúncios de ROBOTPARTS"）
    seller_name = await page.evaluate("""() => {
        var h1 = document.querySelector('h1');
        if (h1) {
            var t = h1.textContent.trim();
            // "Anúncios de XXXXX" -> "XXXXX"
            var m = t.match(/Anúncios? de (.+)/i);
            return m ? m[1] : t;
        }
        return null;
    }""")

    # 找第一个商品链接，从商品页获取完整的卖家信息
    first_product = await page.evaluate("""() => {
        var links = document.querySelectorAll('a');
        for (var i = 0; i < links.length; i++) {
            if (links[i].href.includes('/p/MLB')) return links[i].href;
        }
        return null;
    }""")

    if first_product:
        logger.info(f"从列表页第一个商品获取卖家详情: {first_product}")
        await page.goto(first_product, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(PAGE_SETTLE_MS)

        seller_info = await page.evaluate("""() => {
            var a = document.querySelector('.ui-pdp-seller__link');
            if (a) return {name: a.textContent.trim(), href: a.href};
            return null;
        }""")

        if seller_info:
            seller_name = seller_info["name"]
            store_url = seller_info["href"]
        else:
            store_url = original_url

        # seller_id from product page HTML
        html = await page.evaluate("document.documentElement.outerHTML")
        sid = re.search(r'"seller_id":(\d+)', html)
        if sid and sid.group(1) != seller_id:
            seller_id = sid.group(1)

        # 信誉数据
        reputation = await _extract_reputation(page)

        # profile nick
        nick = await page.evaluate("""(name) => {
            var a = document.querySelector('.ui-pdp-seller__link');
            if (!a) return name;
            var m = a.href.match(/perfil\\.mercadolivre\\.com\\.br\\/([^/?#]+)/);
            return m ? m[1] : name;
        }""", seller_name)
    else:
        store_url = original_url
        reputation = {}
        nick = seller_name

    return {
        "seller_name": seller_name,
        "seller_id": seller_id,
        "store_url": store_url,
        "profile_url": f"https://perfil.mercadolivre.com.br/{nick}",
        "profile_nick": nick,
        "reputation": reputation,
        "source": "卖家商品列表页 (_CustId_)",
    }


async def _scrape_profile_page(page, nick: str, original_url: str = "") -> dict:
    """访问卖家资料页，尝试多种 URL 格式"""
    url_patterns = []
    if original_url:
        url_patterns.append(original_url)
    url_patterns.extend([
        f"https://perfil.mercadolivre.com.br/{nick}",
        f"https://www.mercadolivre.com.br/perfil/{nick}",
        f"https://www.mercadolivre.com.br/loja/{nick}",
        f"https://loja.mercadolivre.com.br/{nick}",
    ])

    for url in url_patterns:
        logger.info(f"访问资料页: {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        except PlaywrightTimeout:
            continue
        await page.wait_for_timeout(PAGE_SETTLE_MS)

        if await _detect_block(page):
            continue

        body_preview = await page.evaluate("document.body.innerText.substring(0, 500)")
        if "nao existe" in body_preview.lower():
            continue

        seller_name = await page.evaluate("""() => {
            var h = document.querySelector('h1');
            if (h) {
                var t = h.textContent.trim();
                // 去掉 "Anúncios de" 前缀
                return t.replace(/^Anúncios?\s+de\s+/i, '').trim();
            }
            var t = document.querySelector('title');
            if (t) {
                return t.textContent.trim().replace(/^Anúncios?\s+de\s+/i, '').trim();
            }
            return null;
        }""")

        store_link = await page.evaluate("""() => {
            var a = document.querySelector('a[href*="/pagina/"], a[href*="/loja/"]');
            if (a) return a.href;
            return null;
        }""")

        body = await page.evaluate("document.body.innerText")
        fm = re.search(r"\+?([\d.]+\s*(?:mil|mi|k)?)\s*seguidores?", body, re.IGNORECASE)
        followers = fm.group(0).strip() if fm else None

        return {
            "seller_name": seller_name,
            "store_url": store_link,
            "profile_url": url,
            "profile_nick": nick,
            "reputation": {"followers": followers},
            "source": "卖家资料页",
        }

    raise ValueError("无法访问卖家页面，卖家可能不存在或页面已下线。")


# =========================================================================
# 第二步：Google 搜索反查 CNPJ
# =========================================================================


async def _google_search_cnpj(page, seller_name: str) -> dict:
    """
    通过 Google 搜索 "{seller_name} CNPJ"，从搜索结果中提取 CNPJ 和 Razão Social。
    """
    query = f'"{seller_name}" CNPJ'
    search_url = f"https://www.google.com/search?q={quote(query)}&hl=pt-BR"

    logger.info(f"Google 搜索: {query}")
    try:
        await page.goto(search_url, wait_until="networkidle", timeout=25000)
    except PlaywrightTimeout:
        logger.warning("Google 搜索超时")
        return {}

    await page.wait_for_timeout(3000)

    # 检查是否被 Google 拦截
    block = await _detect_block(page)
    if block:
        logger.warning(f"Google 搜索被拦截: {block}")
        return {}

    body = await page.evaluate("document.body.innerText")
    if not body or len(body) < 100:
        return {}

    # 提取 CNPJ（排除 ML 自己的）
    cnpj_candidates = []
    for m in CNPJ_RE.finditer(body):
        cnpj = m.group(0)
        if cnpj != ML_OWN_CNPJ:
            # 提取 CNPJ 周围的上下文（前后各 150 字符）
            start = max(0, m.start() - 150)
            end = min(len(body), m.end() + 150)
            context = body[start:end]
            cnpj_candidates.append({"cnpj": cnpj, "context": context})

    if not cnpj_candidates:
        logger.info(f"Google 搜索未找到 {seller_name} 的 CNPJ")
        return {}

    # 统计每个 CNPJ 出现次数（高频的更可能是正确的）
    cnpj_counts = {}
    for c in cnpj_candidates:
        cnpj = c["cnpj"]
        cnpj_counts[cnpj] = cnpj_counts.get(cnpj, 0) + 1

    # 取出现次数最多的 CNPJ
    best_cnpj = max(cnpj_counts, key=cnpj_counts.get)
    best_context = next(c["context"] for c in cnpj_candidates if c["cnpj"] == best_cnpj)

    # 从上下文中提取 Razão Social
    razao_social = None
    patterns = [
        r"raz[aã]o social[:\s]+([A-Z][^.]{5,80}(?:LTDA|S/?A|EIRELI|MEI|LTDA ME))",
        r"sob a raz[aã]o social[:\s]+([A-Z][^.]{5,80}(?:LTDA|S/?A|EIRELI|MEI|LTDA ME))",
        r"raz[aã]o social[:\s]+([^.]{5,100}(?:LTDA|S/?A|EIRELI|MEI))",
        r"nome empresarial[:\s]+([^.]{5,100}(?:LTDA|S/?A|EIRELI|MEI))",
    ]
    for pat in patterns:
        m = re.search(pat, best_context, re.IGNORECASE)
        if m:
            razao_social = m.group(1).strip()
            break

    # 如果没找到 Razão Social，从搜索结果中通过关键字提取
    if not razao_social:
        # 查找包含最佳 CNPJ 的那行文字的更多上下文
        for line in body.split('\n'):
            if best_cnpj in line and 'CNPJ' in line:
                # 尝试从中提取公司名
                m = re.search(r'(?:empresa|companhia|raz[aã]o social|nome)[:\s]+([A-Z][^.]{10,120})', line, re.IGNORECASE)
                if m:
                    razao_social = m.group(1).strip().rstrip(',.')
                    break

    logger.info(f"Google 搜索结果: CNPJ={best_cnpj}, Razão={razao_social}")
    return {
        "cnpj": best_cnpj,
        "razao_social": razao_social,
        "cnpj_source": "Google 搜索反查",
        "cnpj_candidates": len(cnpj_candidates),
    }


# =========================================================================
# 第二步 B：Google 搜索联系方式和官网
# =========================================================================


async def _google_search_contacts(page, seller_name: str, razao_social: str = "") -> dict:
    """
    通过 Google 搜索卖家的官网和社交媒体链接。
    搜索 "{seller_name} site" 和 "{seller_name} instagram"。
    """
    result = {"website": None, "instagram": None, "facebook": None}

    # 用卖家名搜索官网
    search_queries = [
        f'"{seller_name}" site contato',
        f'"{seller_name}" instagram',
    ]
    if razao_social:
        search_queries.insert(0, f'"{razao_social}" site')

    all_domains = []
    all_emails = []

    for query in search_queries[:2]:  # 最多搜 2 次，避免太慢
        search_url = f"https://www.google.com/search?q={quote(query)}&hl=pt-BR"
        logger.info(f"Google 搜索联系方式: {query}")
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=20000)
        except PlaywrightTimeout:
            continue
        await page.wait_for_timeout(2500)

        body = await page.evaluate("document.body.innerText")

        # 提取域名（排除 Google 自己的域名和常见通用域名）
        exclude_domains = {
            "www.google.com", "google.com", "maps.google.com", "play.google.com",
            "support.google.com", "accounts.google.com", "pt.wikipedia.org",
            "reclameaqui.com.br", "www.reclameaqui.com.br",
            "cnpj.biz", "www.cnpj.biz", "cnpj.info", "www.cnpj.info",
            "portaldatransparencia.gov.br", "empresas.serasaexperian.com.br",
            "jusbrasil.com.br", "www.jusbrasil.com.br",
            "linkedin.com", "www.linkedin.com",
        }
        urls = re.findall(r'https?://[^\s"<>]+', body)
        for u in urls:
            m = re.match(r'https?://([^/\s?:]+)', u)
            if m:
                domain = m.group(1)
                if domain not in exclude_domains and not domain.endswith('.gov.br'):
                    all_domains.append(domain)

        # 提取 email
        emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', body)
        for e in emails:
            clean_email = e.rstrip('.')
            if len(clean_email) > 8 and not any(skip in clean_email.lower() for skip in ['example', 'test', 'xxx']):
                all_emails.append(clean_email)

        # 提取 Instagram
        insta_match = re.search(r'instagram\.com/([a-z0-9_.]+)', body, re.IGNORECASE)
        if insta_match and not result.get("instagram"):
            result["instagram"] = f"@{insta_match.group(1)}"

        # 提取 Facebook
        fb_match = re.search(r'facebook\.com/([a-z0-9_.]+)', body, re.IGNORECASE)
        if fb_match and not result.get("facebook"):
            fb_page = fb_match.group(1)
            if fb_page not in ['pages', 'groups', 'sharer', 'share', 'plugins', 'login']:
                result["facebook"] = fb_match.group(0)

        # 提取巴西手机号（9 位格式: 9XXXX-XXXX，DDD 在前后）
        phones_9dig = re.findall(
            r'(?:\(?0?(\d{2})\)?\s*)?(9\d{4}[-.\s]?\d{4})', body
        )
        for ddd, num in phones_9dig:
            clean_num = re.sub(r'[^\d]', '', num)
            if len(clean_num) == 9 and clean_num.startswith('9'):
                formatted = f"({ddd}) {clean_num[:5]}-{clean_num[5:]}" if ddd else clean_num
                if not result.get("phones"):
                    result["phones"] = []
                if formatted not in result["phones"]:
                    result["phones"].append(formatted)

    # 选最佳官网：域名硬匹配（必须包含卖家名的核心词）
    seller_words = re.sub(r'[^a-z0-9]', '', seller_name.lower())
    razao_first_word = razao_social.strip().split()[0].lower() if razao_social else ""

    best_website = None
    website_confidence = None

    for domain in all_domains:
        domain_clean = re.sub(r'^www\.', '', domain.lower())
        domain_core = domain_clean.replace('.com.br', '').replace('.com', '').replace('.net', '')

        # 精确匹配：域名核心 = 卖家名核心
        if domain_core == seller_words:
            best_website = f"https://{domain}"
            website_confidence = "确认"
            break
        # 包含匹配
        if seller_words in domain_core or domain_core in seller_words:
            if not best_website:
                best_website = f"https://{domain}"
                website_confidence = "基本确认"
        # 公司名首词匹配（如 "castro" 匹配 "castrocase.com.br"）
        if razao_first_word and razao_first_word in domain_core and not best_website:
            best_website = f"https://{domain}"
            website_confidence = "待确认"

    result["website"] = best_website
    if website_confidence:
        result["website_confidence"] = website_confidence

    # 选最佳 email（去重）
    unique_emails = list(set(all_emails))[:3]
    if unique_emails:
        result["emails"] = unique_emails

    return result


# =========================================================================
# 第四步：验证官网是否存活
# =========================================================================


async def _verify_website(page, url: str, seller_name: str) -> dict:
    """访问候选官网，验证是否真的是该卖家的网站，以及是否在线。"""
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(2000)
        status = resp.status if resp else 0
        title = await page.title()
        body_preview = (await page.evaluate("document.body.innerText"))[:500]

        seller_core = re.sub(r'[^a-z0-9]', '', seller_name.lower())
        title_lower = title.lower() if title else ""
        body_lower = body_preview.lower()

        # 检查标题或内容是否包含卖家名
        is_related = seller_core in title_lower or seller_core in body_lower

        # 检查是否下线
        offline_keywords = [
            "fora de serviço", "fora do ar", "temporariamente",
            "em manutenção", "em breve", "under construction",
            "coming soon", "out of service", "loja indisponível",
            "temporarily unavailable", "temporarily out",
        ]
        is_offline = any(kw in body_lower for kw in offline_keywords)

        if is_related:
            return {
                "verified": True,
                "status": "offline" if is_offline else "online",
                "title": title[:120],
                "confidence": "确认" if not is_offline else "确认（已下线）",
            }
        else:
            return {
                "verified": False,
                "status": "online",
                "title": title[:120],
                "confidence": "不匹配",
            }
    except PlaywrightTimeout:
        return {"verified": False, "status": "timeout", "confidence": "无法验证"}
    except Exception as e:
        return {"verified": False, "status": str(e)[:60], "confidence": "错误"}


# =========================================================================
# 第三步：Brasil API 验证（可选）
# =========================================================================


async def _verify_cnpj_via_brasilapi(page, cnpj_raw: str) -> Optional[dict]:
    """
    通过 Brasil API 验证 CNPJ 并获取完整法律信息。
    使用 publica.cnpj.ws 作为数据源（比 receitaws 更稳定）。
    """
    # 清理 CNPJ 格式：去掉 . / -
    cnpj_digits = re.sub(r"[.\-/]", "", cnpj_raw)
    api_url = f"https://publica.cnpj.ws/cnpj/{cnpj_digits}"

    logger.info(f"验证 CNPJ: {api_url}")
    try:
        await page.goto(api_url, wait_until="domcontentloaded", timeout=15000)
        body = await page.evaluate("document.body.innerText")

        # 尝试解析 JSON
        import json as _json
        try:
            data = _json.loads(body)
            if isinstance(data, dict):
                est = data.get("estabelecimento", {})
                cidade = est.get("cidade", {}) if isinstance(est, dict) else {}
                estado = est.get("estado", {}) if isinstance(est, dict) else {}
                atividade = est.get("atividade_principal", {}) if isinstance(est, dict) else {}

                # 组装完整地址
                rua = est.get("logradouro", "") or ""
                num = est.get("numero", "") or ""
                bairro = est.get("bairro", "") or ""
                cep = est.get("cep", "") or ""
                endereco_parts = [p for p in [rua, num, bairro] if p]
                endereco = ", ".join(endereco_parts) if endereco_parts else None

                # 电话（DDD + 号码）
                ddd1 = est.get("ddd1", "") or ""
                tel1 = est.get("telefone1", "") or ""
                telefone = f"({ddd1}) {tel1}" if ddd1 and tel1 else (tel1 or None)

                return {
                    "cnpj": data.get("cnpj_raiz") or cnpj_raw,
                    "razao_social": data.get("razao_social"),
                    "nome_fantasia": est.get("nome_fantasia") if isinstance(est, dict) else None,
                    "situacao_cadastral": est.get("situacao_cadastral") if isinstance(est, dict) else None,
                    "data_abertura": est.get("data_inicio_atividade") if isinstance(est, dict) else None,
                    "cidade": cidade.get("nome") if isinstance(cidade, dict) else None,
                    "estado": estado.get("sigla") if isinstance(estado, dict) else None,
                    "cep": cep if cep else None,
                    "endereco": endereco,
                    "telefone": telefone,
                    "email": est.get("email") if isinstance(est, dict) else None,
                    "atividade_principal": (
                        atividade.get("descricao", "")
                        if isinstance(atividade, dict) else ""
                    ),
                    "capital_social": data.get("capital_social"),
                    "socios": [
                        {"nome": s.get("nome", ""), "qualificacao": (s.get("qualificacao_socio", {}) or {}).get("descricao", "")}
                        for s in (data.get("socios") or [])[:3]
                    ] if data.get("socios") else [],
                    "verified": True,
                }
        except _json.JSONDecodeError:
            pass

        # 非 JSON 响应，尝试从 HTML 提取
        # 有些 CNPJ API 返回 HTML
        cnpj_match = CNPJ_RE.search(body)
        razao_match = re.search(r"Raz[aã]o Social[:\s]+([^<\n]{5,100})", body, re.IGNORECASE)
        if cnpj_match and razao_match:
            return {
                "cnpj": cnpj_match.group(0),
                "razao_social": razao_match.group(1).strip(),
                "verified": False,
            }

    except Exception as e:
        logger.warning(f"CNPJ 验证失败: {e}")

    return None


# =========================================================================
# API 路由
# =========================================================================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/scrape")
async def api_scrape(payload: ScrapeRequest):
    # --- 解析 URL ---
    try:
        url_type, identifier = parse_url(payload.url)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "data": None})

    context = None
    try:
        pw = await async_playwright().start()
        context = await _create_context(pw)
        page = await context.new_page()

        # ====== 第一步：从 ML 提取卖家信息 ======
        if url_type == "product":
            data = await _extract_seller_from_product(page, identifier, payload.url)
        elif url_type == "seller_list":
            # _CustId_ URL：访问列表页获取卖家名，再走正常流程
            data = await _scrape_seller_list_page(page, identifier, payload.url)
        else:
            data = await _scrape_profile_page(page, identifier, payload.url)

        seller_name = data.get("seller_name") or ""
        logger.info(f"第一步完成: 卖家名 = {seller_name}")

        # ====== 第二步：Google 搜索反查 CNPJ + 联系方式 ======
        legal_info = {"cnpj": None, "razao_social": None, "source": None}
        contacts = {}
        verified = None

        # 验证 seller_name 是否合法（排除 404 页面标题等）
        invalid_names = ("not found", "não existe", "nao existe", "parece que")
        is_valid_name = bool(
            seller_name
            and len(seller_name) >= 3
            and not any(kw in seller_name.lower() for kw in invalid_names)
        )

        if is_valid_name:
            google_result = await _google_search_cnpj(page, seller_name)

            if google_result.get("cnpj"):
                legal_info["cnpj"] = google_result["cnpj"]
                legal_info["razao_social"] = google_result["razao_social"]
                legal_info["source"] = google_result.get("cnpj_source")
                legal_info["cnpj_candidates"] = google_result.get("cnpj_candidates", 0)

                # ====== 第三步：Brasil API 验证 ======
                verified = await _verify_cnpj_via_brasilapi(page, google_result["cnpj"])
                if verified:
                    # 格式化 CNPJ
                    raw_cnpj = verified.get("cnpj") or ""
                    if raw_cnpj and len(raw_cnpj) >= 14:
                        formatted = f"{raw_cnpj[:2]}.{raw_cnpj[2:5]}.{raw_cnpj[5:8]}/{raw_cnpj[8:12]}-{raw_cnpj[12:14]}"
                    elif raw_cnpj and len(raw_cnpj) == 8:
                        formatted = legal_info.get("cnpj")
                    else:
                        formatted = raw_cnpj or legal_info.get("cnpj")
                    legal_info["cnpj"] = formatted
                    legal_info["razao_social"] = verified.get("razao_social") or legal_info["razao_social"]
                    legal_info["nome_fantasia"] = verified.get("nome_fantasia")
                    legal_info["situacao_cadastral"] = verified.get("situacao_cadastral")
                    legal_info["data_abertura"] = verified.get("data_abertura")
                    legal_info["cidade"] = verified.get("cidade")
                    legal_info["estado"] = verified.get("estado")
                    legal_info["endereco"] = verified.get("endereco")
                    legal_info["cep"] = verified.get("cep")
                    legal_info["atividade_principal"] = verified.get("atividade_principal")
                    legal_info["capital_social"] = verified.get("capital_social")
                    legal_info["socios"] = verified.get("socios", [])
                    legal_info["telefone"] = verified.get("telefone")
                    legal_info["email"] = verified.get("email")
                    legal_info["verified"] = True

                # ====== 第四步：Google 搜索官网和社交链接 ======
                razao = legal_info.get("razao_social", "")
                contacts = await _google_search_contacts(page, seller_name, razao)

                # ====== 第五步：验证官网是否存活 ======
                if contacts.get("website"):
                    site_verify = await _verify_website(page, contacts["website"], seller_name)
                    contacts["website_verified"] = site_verify
                    # 如果验证不匹配，尝试下一个候选
                    if not site_verify.get("verified") and contacts.get("website_confidence") != "确认":
                        logger.info(f"官网验证失败({contacts['website']})，清除")
                        contacts["website"] = None
                        contacts["website_confidence"] = "无官网"

        # 拼装最终数据
        data["legal_info"] = legal_info
        data["contacts"] = contacts
        if not legal_info.get("cnpj"):
            if not is_valid_name:
                data["legal_info"]["note"] = "未能获取到有效的卖家名称，无法进行 CNPJ 反查。请确认 URL 是否正确。"
            else:
                data["legal_info"]["note"] = (
                    "Mercado Livre 不公开显示卖家 CNPJ/Razão Social，"
                    "且 Google 搜索未找到相关注册信息。"
                    "该卖家可能为个人卖家 (CPF) 或新注册企业。"
                )
        else:
            data["legal_info"]["note"] = (
                "CNPJ 通过 Google 搜索反查获得，已通过 Brasil API 验证。"
                if verified else
                "CNPJ 通过 Google 搜索反查获得，未经第三方验证。"
            )

        await context.close()
        await pw.stop()
        return {"error": None, "data": data}

    except RuntimeError as e:
        status = 422 if "CAPTCHA" in str(e) else 500
        return JSONResponse(status_code=status, content={"error": str(e), "data": None})
    except PlaywrightTimeout:
        return JSONResponse(status_code=504, content={"error": "超时：页面响应过慢，请重试。", "data": None})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "data": None})
    except Exception:
        logger.exception("抓取出错")
        return JSONResponse(status_code=500, content={"error": "服务器内部错误，请查看日志。", "data": None})
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    import os as _os
    port = int(_os.environ.get("PORT", "8000"))
    host = _os.environ.get("HOST", "127.0.0.1")
    reload = _os.environ.get("RELOAD", "1") == "1"
    uvicorn.run("app:app", host=host, port=port, reload=reload)
