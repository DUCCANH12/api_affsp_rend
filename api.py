import os
import re
import time
import logging
import traceback
from datetime import datetime
from functools import wraps

from curl_cffi import requests as curl_requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ==================== CONFIG ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

COOKIE = os.environ.get("SHOPEE_COOKIE", "")
PORT = int(os.environ.get("PORT", 5000))
API_KEY = os.environ.get("API_KEY", "salevn_2026_secret_key_v2")

# 🌐 CẤU HÌNH PROXY SOCKS5 (Mặc định lấy từ IP bạn cung cấp, có thể override bằng Env Var trên Render)
PROXY_URL = os.environ.get("PROXY_URL", "socks5h://171.248.223.110")
proxies = {"http": PROXY_URL, "https": PROXY_URL}

# 🛡️ KHỞI TẠO SESSION: Giả lập Chrome 120 + Đi qua Proxy
session = curl_requests.Session(impersonate="chrome120", proxies=proxies)

# ==================== HELPERS ====================
def clean_cookie(raw):
    return (raw or "").replace('"', "").replace("'", "").strip()

def resolve_url(url):
    """Giải nén short link qua Proxy"""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        r = session.get(url, timeout=20, allow_redirects=True)
        return r.url
    except Exception as e:
        logger.warning(f"resolve_url failed via proxy: {e}")
        return url

def extract_ids(url):
    m = re.search(r"/(\d+)/(\d+)(?:\?|$|&)", url)
    if m: return m.group(1), m.group(2)
    m = re.search(r"[?&]item_id=(\d+)", url)
    if m: return None, m.group(1)
    return None, None

def format_money(num):
    try:
        vnd = int(num) / 100000
        return f"₫{int(vnd):,}".replace(",", ".")
    except Exception: return "₫0"

def format_rate(rate):
    try: return f"{float(rate) / 100:.1f}%"
    except Exception: return "0%"

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('x-api-key')
        if key != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ==================== 1. API CONVERT LINK ====================
@app.route("/api/convert", methods=["POST"])
@require_api_key
def convert():
    data = request.get_json() or {}
    url = str(data.get("url", "")).strip()
    sub_id = str(data.get("sub_id", "")).strip()
    if not url: return jsonify({"success": False, "error": "Missing url"}), 400

    cookie = clean_cookie(COOKIE)
    if not cookie: return jsonify({"success": False, "error": "Missing SHOPEE_COOKIE"}), 500

    resolved_url = resolve_url(url)
    lp = [{"originalLink": resolved_url}]
    if sub_id: lp[0]["advancedLinkParams"] = {"subId1": str(sub_id)}

    payload = {
        "operationName": "batchGetCustomLink",
        "query": "query batchGetCustomLink($linkParams: [CustomLinkParam!], $sourceCaller: SourceCaller){batchCustomLink(linkParams: $linkParams, sourceCaller: $sourceCaller){shortLink longLink failCode}}",
        "variables": {"linkParams": lp, "sourceCaller": "CUSTOM_LINK_CALLER"}
    }
    headers = {
        "content-type": "application/json", "cookie": cookie,
        "referer": "https://affiliate.shopee.vn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        r = session.post("https://affiliate.shopee.vn/api/v3/gql?q=batchCustomLink", headers=headers, json=payload, timeout=25)
        d = r.json()
        batch = d.get("data", {}).get("batchCustomLink", [])
        if not batch or batch[0].get("failCode") != 0:
            return jsonify({"success": False, "error": "Convert failed", "detail": d}), 500
        return jsonify({"success": True, "affiliate_url": batch[0].get("shortLink"), "original_url": resolved_url})
    except Exception as e:
        logger.error(f"Convert error: {e}")
        return jsonify({"success": False, "error": "Proxy or API error", "detail": str(e)}), 504

# ==================== 2. API PRODUCT INFO ====================
@app.route("/api/product-info", methods=["GET"])
@require_api_key
def product_info():
    url = request.args.get("url", "")
    if not url: return jsonify({"success": False, "error": "Missing url"}), 400

    cookie = clean_cookie(COOKIE)
    if not cookie: return jsonify({"success": False, "error": "Missing SHOPEE_COOKIE"}), 500

    try:
        resolved_url = resolve_url(url)
        shop_id, item_id = extract_ids(resolved_url)
    except Exception as e:
        return jsonify({"success": False, "error": f"URL resolve error: {str(e)}"}), 400
    
    if not item_id:
        return jsonify({"success": False, "error": f"Cannot extract item_id. Resolved: {resolved_url}"}), 400

    api_url = f"https://affiliate.shopee.vn/api/v3/offer/product?item_id={item_id}"
    headers = {
        "accept": "*/*", "content-type": "application/json", "cookie": cookie,
        "referer": "https://affiliate.shopee.vn/",
        "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        # Tăng timeout lên 25s vì đi qua Proxy SOCKS5 thường chậm hơn trực tiếp
        r = session.get(api_url, headers=headers, timeout=25)
        try:
            d = r.json()
        except Exception:
            return jsonify({"success": False, "error": "Shopee returned HTML (Blocked/Captcha)", "raw": r.text[:200]}), 502

        if d.get("code") != 0:
            err_code = d.get("detail", {}).get("error", "Unknown")
            return jsonify({"success": False, "error": f"Shopee API Error: {err_code}", "detail": d}), 400

        data = d.get("data", {})
        product = data.get("batch_item_for_item_card_full", {})
        comm_rate = data.get("commission_rate_detail", {})
        comm_val = data.get("commission_rate", {})

        return jsonify({
            "success": True,
            "item_id": item_id, "shop_id": product.get("shopid", shop_id),
            "product_name": product.get("name", "Unknown"),
            "image": f"https://cf.shopee.vn/file/{product.get('image', '')}" if product.get('image') else "",
            "price": format_money(product.get("price", 0)),
            "price_before_discount": format_money(product.get("price_before_discount", 0)),
            "commission": {
                "seller_rate": format_rate(comm_rate.get("seller_commission_rate", 0)),
                "shopee_rate": format_rate(comm_rate.get("shopee_commission_rate", 0)),
                "default_rate": format_rate(comm_rate.get("default_commission_rate", 0)),
                "seller_amount": format_money(comm_val.get("seller_commission", 0)),
                "shopee_amount": format_money(comm_val.get("shopee_commission", 0)),
                "commission_cap": format_money(comm_rate.get("commission_cap", 0))
            },
            "shop_name": product.get("shop_name", "Unknown"),
            "is_official_shop": product.get("is_official_shop", False)
        })
    except Exception as e:
        err_str = str(e).lower()
        if "proxy" in err_str or "timeout" in err_str or "connection" in err_str:
            return jsonify({"success": False, "error": "Proxy connection failed or timed out. Proxy might be dead.", "detail": str(e)}), 504
        logger.error(f"Product info error: {e}")
        return jsonify({"success": False, "error": "Internal error", "detail": str(e)}), 500

# ==================== 3. API ORDERS REPORT ====================
@app.route("/api/orders", methods=["GET"])
@require_api_key
def orders():
    sub_id = request.args.get("sub_id")
    if not sub_id: return jsonify({"success": False, "error": "Missing sub_id"}), 400

    start_ts = request.args.get("start", int(time.time()) - 7 * 24 * 3600)
    end_ts = request.args.get("end", int(time.time()))
    qs = f"page_size=20&page_num=1&sub_id={sub_id}&purchase_time_s={start_ts}&purchase_time_e={end_ts}&version=1"
    
    cookie = clean_cookie(COOKIE)
    headers = {
        "content-type": "application/json", "cookie": cookie,
        "referer": "https://affiliate.shopee.vn/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        r = session.get(f"https://affiliate.shopee.vn/api/v3/report/list?{qs}", headers=headers, timeout=25)
        d = r.json()
        if d.get("code") != 0: return jsonify({"success": False, "error": "Shopee report error", "detail": d}), 500

        data = d.get("data", {})
        out = []
        for checkout in (data.get("list") or []):
            purchase_dt = datetime.fromtimestamp(checkout.get("purchase_time", 0)).strftime("%Y-%m-%d %H:%M:%S") if checkout.get("purchase_time") else ""
            for order in (checkout.get("orders") or []):
                status = "cancelled" if order.get("order_status") == "CANCEL" or checkout.get("conversion_status") == 3 else ("confirmed" if order.get("order_status") == "COMPLETED" or checkout.get("conversion_status") == 2 else "pending")
                for item in (order.get("items") or []):
                    comm_raw = item.get("estimated_commission", 0) or item.get("actual_commission", 0) or 0
                    try: comm_vnd = int(float(str(comm_raw))) / 100000
                    except: comm_vnd = 0
                    out.append({
                        "order_sn": order.get("order_sn", ""), "item_id": str(item.get("item_id", "")),
                        "product_name": item.get("item_name", ""),
                        "amount": format_money(item.get('actual_amount', 0)),
                        "commission": f"₫{int(comm_vnd):,}".replace(",", "."),
                        "status": status, "purchase_time": purchase_dt, "shop_name": item.get("shop_name", "")
                    })
        return jsonify({"success": True, "total_count": data.get("total_count", 0), "orders": out})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 504

# ==================== HEALTH CHECK ====================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "OK", "service": "Shopee Aff Proxy API (via SOCKS5)", 
        "proxy_active": PROXY_URL, "cookie_configured": bool(COOKIE)
    })

if __name__ == "__main__":
    logger.info(f"🚀 Starting API on 0.0.0.0:{PORT}")
    logger.info(f"🌐 Routing traffic through Proxy: {PROXY_URL}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
