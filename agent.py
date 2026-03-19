import os, json, time, re, smtplib
from urllib.parse import urlparse, urljoin, parse_qs, unquote
import requests
import feedparser
from bs4 import BeautifulSoup
from email.mime.text import MIMEText

import numpy as np
import cv2
from PIL import Image
from io import BytesIO
import imagehash

# =========================
# CONFIGURAÇÕES
# =========================

ALERT_THRESHOLD_PERCENT = 60
INITIAL_HASH_FILTER = 25
IMAGES_PER_PRODUCT = 3
MAX_IMAGES_PER_SUSPECT_PAGE = 20
CACHE_REFRESH_SECONDS = 48 * 60 * 60
WEEKLY_REPORT_SECONDS = 7 * 24 * 60 * 60
MIN_IMAGE_SIDE_FOR_ANALYSIS = 24  # ignora imagens minúsculas

EMAIL_DESTINATION = "guilhermefariadeangeli@gmail.com"

# =========================
# CONTROLES
# =========================

EMAIL_TESTE = True
RESETAR_CACHE = False
RESETAR_LINKS_VISTOS = True

# =========================
# PATHS
# =========================

FEEDS_FILE = "feeds.txt"
WHITELIST_FILE = "whitelist.txt"
SEEN_FILE = "state/seen.json"
CACHE_FILE = "state/ref_cache.json"

# =========================
# DOMÍNIOS RUIDOSOS
# =========================

NOISY_DOMAINS = [
    "google.com",
    "google.com.br",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "jobrapido.com",
    "folha.uol.com.br",
    "uol.com.br",
    "globo.com",
    "g1.globo.com"
]

# =========================
# ENV
# =========================

WC_BASE_URL = os.getenv("WC_BASE_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET", "")

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

USER_AGENT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# =========================
# UTIL
# =========================

def load_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def unique(items):
    seen, out = set(), []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out

def now():
    return int(time.time())

def update_top(scores, new, limit=3):
    scores = list(scores or [])
    scores.append(float(new))
    return sorted(scores, reverse=True)[:limit]

def format_scores(scores):
    return ", ".join([f"{s:.1f}%" for s in scores]) if scores else "Nenhum"

def build_default_weekly():
    return {
        "start": now(),
        "analyzed": 0,
        "alerts": 0,
        "max": 0.0,
        "top": []
    }

def get_ref_product_name(ref):
    return ref.get("product") or ref.get("product_name") or "(sem nome)"

def get_ref_product_url(ref):
    return ref.get("url") or ref.get("product_url") or ""

# =========================
# WHITELIST
# =========================

def safe_domain(url):
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except:
        return ""

def domain_matches(domain, pattern):
    return domain == pattern or domain.endswith("." + pattern)

def is_whitelisted(url, entries):
    domain = safe_domain(url)

    for w in entries:
        w = w.strip().lower()
        if not w:
            continue

        if "/" not in w:
            if domain_matches(domain, w):
                return True
        else:
            if w in url.lower():
                return True

    return False

def is_noisy_domain(url):
    d = safe_domain(url)
    for nd in NOISY_DOMAINS:
        if domain_matches(d, nd):
            return True
    return False

# =========================
# GOOGLE URL UNWRAP
# =========================

def unwrap_google_url(url):
    try:
        parsed = urlparse(url)
        domain = safe_domain(url)

        if domain in ["google.com", "google.com.br"] and parsed.path == "/url":
            qs = parse_qs(parsed.query)
            target = qs.get("url") or qs.get("q")
            if target and target[0]:
                return unquote(target[0])

        return url
    except:
        return url

# =========================
# EMAIL
# =========================

def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_DESTINATION

    s = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
    s.starttls()
    s.login(EMAIL_USER, EMAIL_PASSWORD)
    s.sendmail(EMAIL_USER, [EMAIL_DESTINATION], msg.as_string())
    s.quit()

# =========================
# IMAGEM
# =========================

def download(url):
    r = requests.get(url, headers=USER_AGENT, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return r.content

def pil_from_bytes(b):
    img = Image.open(BytesIO(b))
    # trata transparência/paleta de forma segura
    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def is_valid_pil_image(img):
    try:
        w, h = img.size
        return w >= MIN_IMAGE_SIDE_FOR_ANALYSIS and h >= MIN_IMAGE_SIDE_FOR_ANALYSIS
    except:
        return False

def gray(img):
    arr = np.array(img)
    if arr is None or arr.size == 0:
        return None
    if len(arr.shape) == 2:
        gray_img = arr
    else:
        gray_img = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    if gray_img is None or gray_img.size == 0:
        return None

    h, w = gray_img.shape[:2]
    if h < MIN_IMAGE_SIDE_FOR_ANALYSIS or w < MIN_IMAGE_SIDE_FOR_ANALYSIS:
        return None

    return gray_img

def hash_triplet(img):
    return imagehash.phash(img), imagehash.dhash(img), imagehash.whash(img)

def hash_distance_to_percent(distance):
    distance = max(0, min(64, distance))
    return (1 - (distance / 64.0)) * 100

def hash_score(img, ref):
    ph, dh, wh = hash_triplet(img)
    d1 = ph - imagehash.hex_to_hash(ref["phash"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash"])
    d3 = wh - imagehash.hex_to_hash(ref["whash"])

    return (
        hash_distance_to_percent(d1) +
        hash_distance_to_percent(d2) +
        hash_distance_to_percent(d3)
    ) / 3.0

def orb_compute(gray_img):
    if gray_img is None:
        return None
    try:
        h, w = gray_img.shape[:2]
        if h < MIN_IMAGE_SIDE_FOR_ANALYSIS or w < MIN_IMAGE_SIDE_FOR_ANALYSIS:
            return None
        orb = cv2.ORB_create(1200)
        kp, des = orb.detectAndCompute(gray_img, None)
        return des
    except Exception:
        return None

def orb_matches(gray_img, ref_orb):
    if ref_orb is None or gray_img is None:
        return 0

    try:
        des = orb_compute(gray_img)
        if des is None:
            return 0

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(np.array(ref_orb, dtype=np.uint8), des)
        good = [m for m in matches if m.distance < 60]
        return len(good)
    except Exception:
        return 0

# =========================
# WOO
# =========================

def wc_products():
    products = []
    page = 1

    while True:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products"

        r = requests.get(
            url,
            params={
                "consumer_key": WC_CONSUMER_KEY,
                "consumer_secret": WC_CONSUMER_SECRET,
                "per_page": 100,
                "page": page
            },
            timeout=20,
            headers=USER_AGENT
        )

        data = r.json()
        if not data:
            break

        products += data
        page += 1

    print(f"Produtos encontrados no WooCommerce: {len(products)}")
    return products

# =========================
# CACHE
# =========================

def build_refs():
    refs = []
    products = wc_products()

    for p in products:
        imgs = (p.get("images") or [])[:IMAGES_PER_PRODUCT]
        for i in imgs:
            try:
                img_bytes = download(i["src"])
                pimg = pil_from_bytes(img_bytes)
                if not is_valid_pil_image(pimg):
                    continue

                ph, dh, wh = hash_triplet(pimg)
                des = orb_compute(gray(pimg))

                refs.append({
                    "product": p.get("name", "(sem nome)"),
                    "url": p.get("permalink", ""),
                    "phash": str(ph),
                    "dhash": str(dh),
                    "whash": str(wh),
                    "orb": des.tolist() if des is not None else None
                })
            except Exception:
                pass

    print(f"Referências criadas: {len(refs)}")
    return refs

def load_cache():
    if RESETAR_CACHE:
        save_json(CACHE_FILE, {"last": 0, "refs": []})

    cache = load_json(CACHE_FILE, {"last": 0, "refs": []})

    if now() - cache.get("last", 0) > CACHE_REFRESH_SECONDS or not cache.get("refs"):
        refs = build_refs()
        cache = {"last": now(), "refs": refs}
