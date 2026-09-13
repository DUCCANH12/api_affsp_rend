import os
import re
import logging
from functools import wraps
from curl_cffi import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ==================== CONFIG ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

COOKIE = os.environ.get("SHOPEE_COOKIE", "")
API_KEY = os.environ.get("API_KEY", "salevn_2026_secret_key_v2")
PORT = int(os.environ.get("PORT", 5000))

# Giả lập Chrome 120 để qua mặt WAF của Shopee
session = requests.Session(impersonate="chrome120")

# ==================== HELPERS ====================
def clean_cookie(raw):
    return (raw or "").replace('"', "").replace("'", "").strip()

def resolve_url(url):
    try:
        if not url.startswith("http"): url = "https://" + url
        r = session.get(url, timeout=15, allow_redirects=True)
        return r.url
    except Exception as e:
        logger.warning(f"resolve_url failed: {e}")
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

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get('x-api-key') != API_KEY:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ==================== API PRODUCT INFO ====================
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
    
    # Headers chuẩn trình duyệt để không bị chặn
    headers = {
        "accept": "application/json",
        "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "cookie": cookie,
        "referer": "https://affiliate.shopee.vn/",
        "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        r = session.get(api_url, headers=headers, timeout=15)
        d = r.json()

        if d.get("code") != 0:
            err_code = d.get("detail", {}).get("error", "Unknown")
            return jsonify({"success": False, "error": f"Shopee API Error: {err_code}", "detail": d}), 400

        data = d.get("data", {})
        product = data.get("batch_item_for_item_card_full", {})
        comm_rate_detail = data.get("commission_rate_detail", {})
        comm_rate = data.get("commission_rate", {})

        # Parse chính xác theo cấu trúc JSON bạn đã cung cấp
        result = {
            "success": True,
            "item_id": product.get("itemid", item_id),
            "shop_id": product.get("shopid", shop_id),
            "product_name": product.get("name", "Unknown"),
            "image": f"https://cf.shopee.vn/file/{product.get('image', '')}" if product.get('image') else "",
            "price": format_money(product.get("price", 0)),
            "price_before_discount": format_money(product.get("price_before_discount", 0)),
            "commission": {
                "seller_rate": f"{comm_rate_detail.get('seller_commission_rate', 0) / 100:.1f}%",
                "shopee_rate": f"{comm_rate_detail.get('shopee_commission_rate', 0) / 100:.1f}%",
                "default_rate": f"{comm_rate_detail.get('default_commission_rate', 0) / 100:.1f}%",
                "seller_amount": comm_rate.get("seller_commission", "₫0"),
                "shopee_amount": comm_rate.get("shopee_commission", "₫0"),
                "default_amount": comm_rate.get("default_commission", "₫0"),
                "commission_cap": format_money(comm_rate_detail.get("commission_cap", 0))
            },
            "shop_name": product.get("shop_name", "Unknown"),
            "is_official_shop": product.get("is_official_shop", False)
        }
        return jsonify(result)

    except requests.RequestsError as e:
        return jsonify({"success": False, "error": f"Request failed: {str(e)}"}), 504
    except Exception as e:
        logger.error(f"Product info error: {e}")
        return jsonify({"success": False, "error": "Internal error", "detail": str(e)}), 500

# ==================== HEALTH CHECK ====================
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "OK", "service": "Shopee Aff API", "cookie_active": bool(COOKIE)})

if __name__ == "__main__":
    logger.info(f"🚀 Starting API on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
