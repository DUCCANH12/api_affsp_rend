import os
import re
import time
import logging
import traceback
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ==================== CONFIG ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Cho phép PHP trên hosting khác gọi đến (CORS)
CORS(app, resources={r"/api/*": {"origins": "*"}})

COOKIE = os.environ.get("SHOPEE_COOKIE", "")
PORT = int(os.environ.get("PORT", 5000))
API_KEY = os.environ.get("API_KEY", "your_secret_api_key_here")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

# ==================== HELPERS ====================
def clean_cookie(raw):
    return (raw or "").replace('"', "").replace("'", "").strip()

def resolve_url(url):
    """Giải nén short link (s.shopee.vn, shp.ee) thành URL đầy đủ"""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        r = session.get(url, timeout=15, allow_redirects=True)
        return r.url
    except Exception as e:
        logger.warning(f"resolve_url failed: {e}")
        return url

def extract_ids(url):
    """Trích xuất shopid và itemid từ URL Shopee"""
    # Định dạng: shopee.vn/product/{shopid}/{itemid}
    m = re.search(r"/(\d+)/(\d+)(?:\?|$|&)", url)
    if m:
        return m.group(1), m.group(2)
    # Định dạng khác có thể gặp
    m = re.search(r"[?&]item_id=(\d+)", url)
    if m:
        return None, m.group(1)
    return None, None

def format_money(num):
    """Shopee trả về giá trị nhỏ nhất, chia 100000 để ra VND"""
    try:
        vnd = int(num) / 100000
        return f"₫{int(vnd):,}".replace(",", ".")
    except Exception:
        return "₫0"

def format_rate(rate):
    """Chuyển đổi basis points (ví dụ: 7000) thành phần trăm (7.0%)"""
    try:
        return f"{float(rate) / 100:.1f}%"
    except Exception:
        return "0%"

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('x-api-key')
        if key != API_KEY:
            return jsonify({"error": "Unauthorized: Invalid or missing API key"}), 401
        return f(*args, **kwargs)
    return decorated

# ==================== 1. API CONVERT LINK ====================
@app.route("/api/convert", methods=["POST"])
@require_api_key
def convert():
    data = request.get_json() or {}
    url = str(data.get("url", "")).strip()
    sub_id = str(data.get("sub_id", "")).strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    # 1. Resolve short link
    resolved_url = resolve_url(url)
    
    # 2. Build payload
    lp = [{"originalLink": resolved_url}]
    if sub_id:
        lp[0]["advancedLinkParams"] = {"subId1": str(sub_id)}

    payload = {
        "operationName": "batchGetCustomLink",
        "query": "query batchGetCustomLink($linkParams: [CustomLinkParam!], $sourceCaller: SourceCaller){batchCustomLink(linkParams: $linkParams, sourceCaller: $sourceCaller){shortLink longLink failCode}}",
        "variables": {"linkParams": lp, "sourceCaller": "CUSTOM_LINK_CALLER"}
    }
    
    headers = {
        "content-type": "application/json",
        "cookie": clean_cookie(COOKIE),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        r = session.post("https://affiliate.shopee.vn/api/v3/gql?q=batchCustomLink", headers=headers, json=payload, timeout=20)
        d = r.json()
        
        batch = d.get("data", {}).get("batchCustomLink", [])
        if not batch or batch[0].get("failCode") != 0:
            return jsonify({"error": "Convert failed", "detail": d}), 500
            
        return jsonify({
            "success": True,
            "affiliate_url": batch[0].get("shortLink"),
            "original_url": resolved_url
        })
    except Exception as e:
        logger.error(f"Convert error: {e}")
        return jsonify({"error": "Internal server error"}), 500

# ==================== 2. API PRODUCT INFO (MỚI) ====================
# ==================== 2. API PRODUCT INFO (ĐÃ TỐI ƯU LỖI) ====================
# ==================== 2. API PRODUCT INFO (ĐÃ CẬP NHẬT HEADERS CHỐNG BLOCK) ====================
@app.route("/api/product-info", methods=["GET"])
@require_api_key
def product_info():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"success": False, "error": "Missing url parameter"}), 400

    cookie = clean_cookie(COOKIE)
    if not cookie:
        return jsonify({
            "success": False, 
            "error": "Server chưa được cấu hình SHOPEE_COOKIE."
        }), 500

    try:
        resolved_url = resolve_url(url)
        shop_id, item_id = extract_ids(resolved_url)
    except Exception as e:
        return jsonify({"success": False, "error": f"Lỗi xử lý URL: {str(e)}"}), 400
    
    if not item_id:
        return jsonify({
            "success": False, 
            "error": f"Không thể trích xuất item_id. Link: {resolved_url}"
        }), 400

    api_url = f"https://affiliate.shopee.vn/api/v3/offer/product?item_id={item_id}"
    
    # 🛡️ BỘ HEADERS "NGỤY TRANG" GIỐNG HỆT TRÌNH DUYỆT (Lấy cảm hứng từ repo TypeScript)
    headers = {
        "accept": "*/*",
        "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "cookie": cookie,
        "referer": "https://affiliate.shopee.vn/", # 👈 CỰC KỲ QUAN TRỌNG
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        # ⚠️ LƯU Ý: User-Agent này PHẢI KHỚP với trình duyệt bạn dùng để lấy Cookie
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    try:
        r = session.get(api_url, headers=headers, timeout=15)
        
        try:
            d = r.json()
        except Exception:
            return jsonify({
                "success": False, 
                "error": "Shopee trả về HTML (Bị chặn/Captcha). Hãy kiểm tra lại Cookie.",
                "raw_response": r.text[:200]
            }), 502

        if d.get("code") != 0:
            # Bắt đúng lỗi 90309999 để thông báo rõ ràng cho người dùng
            if d.get("detail", {}).get("error") == 90309999:
                return jsonify({
                    "success": False, 
                    "error": "Shopee chặn yêu cầu (Lỗi 90309999). Cookie có thể đã hết hạn hoặc User-Agent không khớp với trình duyệt lấy cookie."
                }), 403
                
            return jsonify({
                "success": False, 
                "error": f"Shopee API error: {d.get('msg', 'Unknown')}",
                "detail": d
            }), 400

        data = d.get("data", {})
        product = data.get("batch_item_for_item_card_full", {})
        comm_rate = data.get("commission_rate_detail", {})
        comm_val = data.get("commission_rate", {})

        result = {
            "success": True,
            "item_id": item_id,
            "shop_id": product.get("shopid", shop_id),
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
                "default_amount": format_money(comm_val.get("default_commission", 0)),
                "commission_cap": format_money(comm_rate.get("commission_cap", 0))
            },
            "stock": product.get("stock", 0),
            "sold": product.get("historical_sold_text", "0"),
            "shop_name": product.get("shop_name", "Unknown"),
            "is_official_shop": product.get("is_official_shop", False)
        }
        return jsonify(result)

    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": f"Lỗi kết nối đến Shopee: {str(e)}"}), 504
    except Exception as e:
        logger.error(f"Product info unhandled error: {e}")
        return jsonify({"success": False, "error": f"Lỗi server nội bộ: {str(e)}"}), 500

# ==================== 3. API ORDERS REPORT ====================
@app.route("/api/orders", methods=["GET"])
@require_api_key
def orders():
    sub_id = request.args.get("sub_id")
    if not sub_id:
        return jsonify({"error": "Missing sub_id"}), 400

    start_ts = request.args.get("start", int(time.time()) - 7 * 24 * 3600)
    end_ts = request.args.get("end", int(time.time()))
    page_num = request.args.get("page_num", "1")
    page_size = request.args.get("page_size", "20")

    qs = f"page_size={page_size}&page_num={page_num}&sub_id={sub_id}&purchase_time_s={start_ts}&purchase_time_e={end_ts}&version=1"
    
    headers = {
        "content-type": "application/json",
        "cookie": clean_cookie(COOKIE),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        r = session.get(f"https://affiliate.shopee.vn/api/v3/report/list?{qs}", headers=headers, timeout=20)
        d = r.json()
        
        if d.get("code") != 0:
            return jsonify({"error": "Shopee report error", "detail": d}), 500

        data = d.get("data", {})
        checkout_list = data.get("list", [])
        
        out = []
        for checkout in checkout_list:
            purchase_dt = datetime.fromtimestamp(checkout.get("purchase_time", 0)).strftime("%Y-%m-%d %H:%M:%S") if checkout.get("purchase_time") else ""
            
            for order in (checkout.get("orders") or []):
                order_sn = order.get("order_sn", "")
                # Xác định trạng thái
                if order.get("order_status") == "CANCEL" or checkout.get("conversion_status") == 3:
                    status = "cancelled"
                elif order.get("order_status") == "COMPLETED" or checkout.get("conversion_status") == 2:
                    status = "confirmed"
                else:
                    status = "pending"

                for item in (order.get("items") or []):
                    # Lưu ý: Giá trị commission của Shopee trả về trong report cần chia 100000
                    comm_raw = item.get("estimated_commission", 0) or item.get("actual_commission", 0) or 0
                    try:
                        comm_vnd = int(float(str(comm_raw))) / 100000
                    except:
                        comm_vnd = 0

                    out.append({
                        "order_sn": order_sn,
                        "item_id": str(item.get("item_id", "")),
                        "product_name": item.get("item_name", ""),
                        "amount": f"₫{int(item.get('actual_amount', 0) / 100000):,}".replace(",", "."),
                        "commission": f"₫{int(comm_vnd):,}".replace(",", "."),
                        "status": status,
                        "purchase_time": purchase_dt,
                        "shop_name": item.get("shop_name", ""),
                        "sub_id": sub_id
                    })

        return jsonify({
            "success": True,
            "total_count": data.get("total_count", 0),
            "orders": out
        })
    except Exception as e:
        logger.error(f"Orders exception: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== HEALTH CHECK ====================
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "OK", "service": "Shopee Aff Proxy API", "cookie_active": bool(COOKIE)})

if __name__ == "__main__":
    logger.info(f"🚀 Starting API on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
