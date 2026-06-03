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
PAGE_SETTLE_MS = 5_000
CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
ML_OWN_CNPJ = "03.007.331/0001-41"

# 人工确认的 seller_id → CNPJ 映射表（优先于 Google 搜索）
# 数据来源：Brazilian Receita Federal (Portal da Transparência) 官方公开数据
SELLER_CNPJ_MAP = {
    "1204030353": {  # ML 卖家名: starshoppp, 实际是 MCM
        "cnpj": "30.597.577/0001-93",
        "razao_social": "MCM DISTRIBUIDORA DE ALIMENTOS LTDA",
        "nome_fantasia": "MCM DISTRIBUIDORA",
        "atividade_principal": "Comércio atacadista de produtos alimentícios em geral",
        "situacao_cadastral": "Ativa",
        "cidade": "Campina Grande",
        "estado": "PB",
        "match_confidence": "人工确认",
        "source": "Receita Federal 公开数据",
        "verified": True,
        "note": "经人工通过巴西联邦税务局官网核实。ML 店铺名 'starshoppp' 为马甲名。",
    },
    "2206301229": {  # ML 卖家名: RR20250112100026, 实际是 MCM
        "cnpj": "30.597.577/0001-93",
        "razao_social": "MCM DISTRIBUIDORA DE ALIMENTOS LTDA",
        "nome_fantasia": "MCM DISTRIBUIDORA",
        "atividade_principal": "Comércio atacadista de produtos alimentícios em geral",
        "situacao_cadastral": "Ativa",
        "cidade": "Campina Grande",
        "estado": "PB",
        "match_confidence": "人工确认",
        "source": "Receita Federal 公开数据",
        "verified": True,
        "note": "经人工通过巴西联邦税务局官网核实。ML 店铺名 'RR20250112100026' 为马甲名。",
    },
}

# 运行时缓存（自动从 Google/Brasil API 查到的结果）
_seller_cache: dict = {}


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
    """Create a fresh non-persistent context each time (avoids ML bot detection)."""
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-setuid-sandbox", "--no-first-run",
            "--disable-default-apps", "--disable-infobars",
            "--disable-background-networking", "--disable-sync",
            "--disable-features=TranslateUI",
            "--disable-extensions",
        ],
    )
    context = await browser.new_context(
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
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    )
    # Pre-set Brazil geo cookies (force Brazilian site)
    await context.add_cookies([
        {"name": "ml_geo", "value": "BR", "domain": ".mercadolivre.com.br", "path": "/"},
        {"name": "site_id", "value": "MLB", "domain": ".mercadolivre.com.br", "path": "/"},
        {"name": "locale", "value": "pt-BR", "domain": ".mercadolivre.com.br", "path": "/"},
    ])
    await context.add_init_script(STEALTH_SCRIPT)
    return context, browser

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
            "location": await _extract_seller_location(page),
        }

    raise ValueError("无法访问卖家页面，卖家可能不存在或页面已下线。")


# =========================================================================
# 第二步：Google 搜索反查 CNPJ
# =========================================================================


async def _extract_seller_location(page) -> Optional[dict]:
    """从 ML 页面提取卖家地理位置"""
    try:
        body = await page.evaluate("document.body.innerText")

        # 优先找明确的城市名
        # São Paulo
        if re.search(r'(?:São|Sao)\s+Paulo', body):
            return {"city": "São Paulo", "state": "SP"}
        # Rio de Janeiro
        if re.search(r'Rio\s+de\s+Janeiro', body):
            return {"city": "Rio de Janeiro", "state": "RJ"}
        # Belo Horizonte
        if re.search(r'Belo\s+Horizonte', body):
            return {"city": "Belo Horizonte", "state": "MG"}

        # 通用模式：城市名 + 州缩写，但必须是大写开头的真实地名
        STATES = r'(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)'
        m = re.search(
            r'(?:em|no|na|de)\s+([A-ZÀ-Ú][a-zà-ú]+(?:\s+(?:das?|dos?|de|da)\s+)?(?:\s+[A-ZÀ-Ú][a-zà-ú]+)*)\s+-\s+' + STATES,
            body
        )
        if m:
            return {"city": m.group(1).strip(), "state": m.group(2)}

        # 筛选面板中的 "Ubicación de retiro" 后面的城市
        m = re.search(r'(?:Ubicación|Localização|retirada?)\s*(?:de|em)?\s*\n\s*([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+)*)\s*\n\s*' + STATES, body)
        if m:
            return {"city": m.group(1).strip(), "state": m.group(2)}
    except Exception:
        pass
    return None


async def _google_search_cnpj(page, seller_name: str) -> dict:
    """
    通过 Google 搜索 "{seller_name} CNPJ"，从搜索结果中提取 CNPJ 和 Razão Social。
    """
    query = f'"{seller_name}" CNPJ'
    search_url = f"https://www.google.com/search?q={quote(query)}&hl=pt-BR"

    logger.info(f"Google 搜索: {query}")
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=12000)
    except PlaywrightTimeout:
        logger.warning("Google 搜索超时")
        return {}

    await page.wait_for_timeout(2000)

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

    # 统计每个 CNPJ 出现次数，排除 ML 自己的
    cnpj_counts = {}
    for c in cnpj_candidates:
        cnpj = c["cnpj"]
        if cnpj != ML_OWN_CNPJ:
            cnpj_counts[cnpj] = cnpj_counts.get(cnpj, 0) + 1

    # 返回所有候选（按频率排序），让调用方逐一验证
    sorted_cnpjs = sorted(cnpj_counts.items(), key=lambda x: -x[1])

    # 从上下文中提取每个候选的 Razão Social
    candidates = []
    for cnpj, count in sorted_cnpjs[:5]:  # 最多 5 个候选
        ctx = next((c["context"] for c in cnpj_candidates if c["cnpj"] == cnpj), "")
        razao = None
        patterns = [
            r"raz[aã]o social[:\s]+([A-Z][^.]{5,80}(?:LTDA|S/?A|EIRELI|MEI|LTDA ME))",
            r"sob a raz[aã]o social[:\s]+([A-Z][^.]{5,80}(?:LTDA|S/?A|EIRELI|MEI|LTDA ME))",
            r"raz[aã]o social[:\s]+([^.]{5,100}(?:LTDA|S/?A|EIRELI|MEI))",
            r"nome empresarial[:\s]+([^.]{5,100}(?:LTDA|S/?A|EIRELI|MEI))",
        ]
        for pat in patterns:
            m = re.search(pat, ctx, re.IGNORECASE)
            if m:
                razao = m.group(1).strip()
                break
        candidates.append({"cnpj": cnpj, "razao_social": razao, "count": count})

    if not candidates:
        logger.info(f"Google 搜索未找到 {seller_name} 的 CNPJ")
        return {"candidates": []}

    logger.info(f"Google 搜索结果: {len(candidates)} 个候选 CNPJ")
    return {"candidates": candidates, "cnpj_source": "Google 搜索反查"}


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

    for query in search_queries[:1]:  # 只搜 1 次，节省时间
        search_url = f"https://www.google.com/search?q={quote(query)}&hl=pt-BR"
        logger.info(f"Google 搜索联系方式: {query}")
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=10000)
        except PlaywrightTimeout:
            continue
        await page.wait_for_timeout(2000)

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
        await page.goto(api_url, wait_until="domcontentloaded", timeout=8000)
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

                # 用 estabelecimento.cnpj（完整 14 位）而非 cnpj_raiz（8 位）
                full_cnpj = est.get("cnpj") if isinstance(est, dict) else None
                if not full_cnpj:
                    full_cnpj = data.get("cnpj_raiz") or cnpj_raw

                return {
                    "cnpj": full_cnpj,
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


@app.get("/debug")
async def debug():
    """诊断接口：测试 Playwright 是否能正常启动"""
    import traceback as _tb
    result = {}

    result["python"] = __import__("sys").version
    result["platform"] = __import__("sys").platform

    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        result["pw_start"] = "ok"

        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
            )
            result["chromium_launch"] = "ok"
            result["chromium_version"] = browser.version

            page = await browser.new_page()
            await page.goto("https://httpbin.org/ip", wait_until="domcontentloaded", timeout=15000)
            body = await page.evaluate("document.body.innerText")
            result["test_page"] = body[:200]
            await browser.close()
            result["status"] = "fully_working"
        except Exception as e:
            result["error"] = str(e)[:300]
            result["traceback"] = _tb.format_exc()[-800:]

        await pw.stop()
    except Exception as e:
        result["pw_error"] = str(e)[:300]
        result["traceback"] = _tb.format_exc()[-800:]

    import shutil as _shutil
    usage = _shutil.disk_usage("/tmp") if __import__("os").path.exists("/tmp") else None
    if usage:
        result["disk_free_mb"] = usage.free // (1024 * 1024)

    return result


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
        context, browser = await _create_context(pw)
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
        seller_id = data.get("seller_id")  # 已在上面赋值，这里只读
        is_alias = False
        logger.info(f"第一步完成: 卖家名 = {seller_name}")

        # 提取卖家地理位置（从当前页面）
        seller_location = await _extract_seller_location(page)
        if seller_location:
            data["location"] = seller_location
            logger.info(f"卖家位置: {seller_location}")

        # ====== 第二步：Google 搜索反查 CNPJ + 联系方式 ======
        legal_info = {"cnpj": None, "razao_social": None, "source": None}
        contacts = {}
        verified = None
        seller_id = data.get("seller_id")

        # 先查人工映射表，再查缓存
        if seller_id and seller_id in SELLER_CNPJ_MAP:
            legal_info.update(SELLER_CNPJ_MAP[seller_id])
            logger.info(f"人工映射命中: seller_id={seller_id}, CNPJ={legal_info.get('cnpj')}")
        elif seller_id and seller_id in _seller_cache:
            cached = _seller_cache[seller_id]
            logger.info(f"缓存命中: seller_id={seller_id}, CNPJ={cached.get('cnpj')}")
            legal_info.update(cached)
            legal_info["source"] = "缓存（相同卖家ID）"

        # 验证 seller_name 是否合法
        invalid_names = ("not found", "não existe", "nao existe", "parece que")
        is_valid_name = bool(
            seller_name
            and len(seller_name) >= 3
            and not any(kw in seller_name.lower() for kw in invalid_names)
        )

        # 检查是否为马甲名（全小写、无空格、纯字母、不含 LTDA/SA 等后缀）
        is_alias = bool(
            seller_name
            and seller_name == seller_name.lower()  # 全小写
            and " " not in seller_name               # 无空格
            and not any(suffix in seller_name.lower()
                        for suffix in ["ltda", "s.a", "s/a", "eireli", "mei", "comercio", "import", "distribu"])
        )

        if is_valid_name and not is_alias and not legal_info.get("cnpj"):
            google_result = await _google_search_cnpj(page, seller_name)

            candidates = google_result.get("candidates", [])
            if candidates:
                legal_info["source"] = google_result.get("cnpj_source")
                legal_info["cnpj_candidates"] = len(candidates)

                # ====== 第三步：逐一验证所有候选 CNPJ，选最优 ======
                best_score = -1
                best_verified = None

                for cand in candidates[:3]:  # 最多验证 3 个候选
                    v = await _verify_cnpj_via_brasilapi(page, cand["cnpj"])
                    if not v:
                        continue

                    # 评分：对比 ML 卖家名和 Brasil API 返回的 nome_fantasia / razao_social
                    ml_norm = re.sub(r'[^a-z0-9]', '', seller_name.lower())
                    fantasia = (v.get("nome_fantasia") or "").lower()
                    fantasia_norm = re.sub(r'[^a-z0-9]', '', fantasia)
                    razao_norm = re.sub(r'[^a-z0-9]', '', (v.get("razao_social") or "").lower())

                    score = 0
                    if ml_norm == fantasia_norm:
                        score = 100
                    elif ml_norm in fantasia_norm or fantasia_norm in ml_norm:
                        score = 80
                    elif ml_norm in razao_norm:
                        score = 60
                    else:
                        ml_words = set(ml_norm.split())
                        razao_words = set(razao_norm.split())
                        common = ml_words & razao_words
                        score = len(common) * 10

                    # 地理位置匹配加分/扣分
                    seller_state = (seller_location or {}).get("state", "")
                    cand_state = v.get("estado", "")
                    if seller_state and cand_state:
                        if seller_state == cand_state:
                            score += 20  # 同州加分
                            logger.info(f"  地理位置匹配: seller={seller_state}, CNPJ={cand_state}, +20")
                        else:
                            score -= 30  # 不同州扣分
                            logger.info(f"  地理位置不匹配: seller={seller_state}, CNPJ={cand_state}, -30")

                    logger.info(f"  候选 {cand['cnpj']}: score={score}, fantasia={v.get('nome_fantasia','')}")
                    if score > best_score:
                        best_score = score
                        best_verified = v

                if best_verified:
                    verified = best_verified
                    # 格式化 CNPJ（严格验证：必须 14 位数字）
                    raw_cnpj = (verified.get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
                    if raw_cnpj and len(raw_cnpj) == 14 and raw_cnpj.isdigit():
                        formatted = f"{raw_cnpj[:2]}.{raw_cnpj[2:5]}.{raw_cnpj[5:8]}/{raw_cnpj[8:12]}-{raw_cnpj[12:14]}"
                    else:
                        formatted = cand.get("cnpj", "")
                    legal_info["cnpj"] = formatted
                    legal_info["razao_social"] = verified.get("razao_social") or cand.get("razao_social")
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

                    # 设定可信度
                    if best_score >= 100:
                        legal_info["match_confidence"] = "确认"
                    elif best_score >= 80:
                        legal_info["match_confidence"] = "基本确认"
                    elif best_score >= 50:
                        legal_info["match_confidence"] = "待确认"
                    else:
                        legal_info["match_confidence"] = "待确认"

                    # 存入缓存（下次同 seller_id 直接复用）
                    if seller_id and best_score >= 50:
                        _seller_cache[seller_id] = {
                            k: v for k, v in legal_info.items()
                            if k not in ("note", "source", "match_confidence", "cnpj_candidates")
                        }
                        _seller_cache[seller_id]["match_confidence"] = legal_info.get("match_confidence")

                # ====== 第四步：官网/联系（轻量模式，只搜一次）======
                razao = legal_info.get("razao_social", "")
                contacts = await _google_search_contacts(page, seller_name, razao)

                # 如果没找到或评分太低，尝试用 "+mercado livre" 再搜一次
                if not legal_info.get("cnpj"):
                    retry_result = await _google_search_cnpj(page, f"{seller_name} mercado livre")
                    retry_candidates = retry_result.get("candidates", [])
                    if retry_candidates:
                        # 快速验证第一个候选
                        verified2 = await _verify_cnpj_via_brasilapi(page, retry_candidates[0]["cnpj"])
                        if verified2:
                            raw = (verified2.get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
                            if raw and len(raw) == 14 and raw.isdigit():
                                legal_info["cnpj"] = f"{raw[:2]}.{raw[2:5]}.{raw[5:8]}/{raw[8:12]}-{raw[12:14]}"
                            legal_info["razao_social"] = verified2.get("razao_social") or legal_info["razao_social"]
                            legal_info["nome_fantasia"] = verified2.get("nome_fantasia")
                            legal_info["situacao_cadastral"] = verified2.get("situacao_cadastral")
                            legal_info["cidade"] = verified2.get("cidade")
                            legal_info["estado"] = verified2.get("estado")
                            legal_info["telefone"] = legal_info.get("telefone") or verified2.get("telefone")
                            legal_info["email"] = legal_info.get("email") or verified2.get("email")
                            legal_info["endereco"] = legal_info.get("endereco") or verified2.get("endereco")
                            legal_info["verified"] = True
                            legal_info["match_confidence"] = "二次验证"

        # 拼装最终数据
        data["legal_info"] = legal_info
        data["contacts"] = contacts
        if not legal_info.get("cnpj"):
            if is_alias:
                data["legal_info"]["note"] = (
                    f"卖家名 '{seller_name}' 是店铺马甲名（非公司注册名），无法通过 Google 准确匹配 CNPJ。"
                    "请尝试用 _CustId_ 链接查询该卖家的其他商品，或手动提供公司名。"
                )
            elif not is_valid_name:
                data["legal_info"]["note"] = "未能获取到有效的卖家名称，无法进行 CNPJ 反查。"
            elif legal_info.get("note"):
                pass  # 已在上面设置（如不匹配丢弃的情况）
            else:
                data["legal_info"]["note"] = (
                    "Google 搜索未找到该卖家的 CNPJ 注册信息。可能为个人卖家 (CPF) 或新注册企业。"
                )
        else:
            conf_map = {
                "确认": f"✅ 已确认：Nome Fantasia '{legal_info.get('nome_fantasia','')}' 与 ML 卖家名匹配",
                "基本确认": f"⚠️ 基本确认：公司名与卖家名高度相似",
                "待确认": f"⚠️ 待确认：公司名与卖家名部分匹配，CNPJ 可能属于相关但不完全相同的实体",
                "二次验证": f"✅ 二次搜索验证通过",
            }
            conf = legal_info.get("match_confidence", "")
            conf_note = conf_map.get(conf, "")
            base = "已通过 Brasil API 验证。" if verified else "未经验证。"
            data["legal_info"]["note"] = f"{conf_note} {base}" if conf_note else base

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
