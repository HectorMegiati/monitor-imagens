import os, json, time, re, smtplib
from urllib.parse import urlparse, urljoin
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
# ENV
# =========================

WC_BASE_URL = os.getenv("WC_BASE_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET", "")

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

USER_AGENT = {"User-Agent": "Mozilla/5.0"}

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
    return json.load(open(path, "r", encoding="utf-8"))

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), indent=2)

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
    return requests.get(url, headers=USER_AGENT, timeout=20).content

def pil(b):
    return Image.open(BytesIO(b)).convert("RGB")

def gray(p):
    return cv2.cvtColor(np.array(p), cv2.COLOR_RGB2GRAY)

def hash_triplet(p):
    return imagehash.phash(p), imagehash.dhash(p), imagehash.whash(p)

def hash_score(p, ref):
    ph, dh, wh = hash_triplet(p)
    d1 = ph - imagehash.hex_to_hash(ref["phash"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash"])
    d3 = wh - imagehash.hex_to_hash(ref["whash"])

    def conv(d): return (1 - (d / 64)) * 100

    return (conv(d1) + conv(d2) + conv(d3)) / 3

def orb(gray_img, ref):
    if not ref:
        return 0
    orb = cv2.ORB_create(1200)
    kp, des = orb.detectAndCompute(gray_img, None)
    if des is None:
        return 0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(np.array(ref, dtype=np.uint8), des)
    good = [m for m in matches if m.distance < 60]
    return len(good)

# =========================
# WOO
# =========================

def wc_products():
    products = []
    page = 1

    while True:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products"

        r = requests.get(url, params={
            "consumer_key": WC_CONSUMER_KEY,
            "consumer_secret": WC_CONSUMER_SECRET,
            "per_page": 100,
            "page": page
        }, timeout=20)

        data = r.json()
        if not data:
            break

        products += data
        page += 1

    print("Produtos:", len(products))
    return products

# =========================
# CACHE
# =========================

def build_refs():
    refs = []
    for p in wc_products():
        imgs = (p.get("images") or [])[:IMAGES_PER_PRODUCT]
        for i in imgs:
            try:
                pimg = pil(download(i["src"]))
                ph, dh, wh = hash_triplet(pimg)
                refs.append({
                    "product": p["name"],
                    "url": p["permalink"],
                    "phash": str(ph),
                    "dhash": str(dh),
                    "whash": str(wh),
                    "orb": orb(gray(pimg), None)
                })
            except:
                pass
    return refs

def load_cache():
    if RESETAR_CACHE:
        save_json(CACHE_FILE, {"last": 0, "refs": []})

    cache = load_json(CACHE_FILE, {"last": 0, "refs": []})

    if now() - cache["last"] > CACHE_REFRESH_SECONDS or not cache["refs"]:
        refs = build_refs()
        cache = {"last": now(), "refs": refs}
        save_json(CACHE_FILE, cache)

    return cache["refs"]

# =========================
# SCRAPE
# =========================

def extract_images(url):
    html = requests.get(url, headers=USER_AGENT, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")

    imgs = []

    for img in soup.find_all("img"):
        if img.get("src"):
            imgs.append(urljoin(url, img["src"]))

    return unique(imgs)[:MAX_IMAGES_PER_SUSPECT_PAGE], html

def suspicious(text):
    return [l for l in re.findall(r"https?://\S+", text)
            if any(x in l for x in ["mega", "drive", "telegram"])]

# =========================
# RSS
# =========================

def read_rss(feeds):
    urls = []
    for f in feeds:
        d = feedparser.parse(f)
        for e in d.entries:
            if hasattr(e, "link"):
                urls.append(e.link)
    return list(set(urls))

# =========================
# MAIN
# =========================

def main():
    print("START")

    feeds = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)

    state = load_json(SEEN_FILE, {"seen": [], "weekly": {
        "start": now(), "analyzed": 0,
        "alerts": 0, "max": 0, "top": []
    }})

    refs = load_cache()
    urls = read_rss(feeds)

    best = state["weekly"]["max"]
    top = state["weekly"]["top"]

    analyzed = 0

    for url in urls:
        if url in state["seen"]:
            continue

        state["seen"].append(url)

        imgs, html = extract_images(url)
        analyzed += 1

        for im in imgs:
            try:
                pimg = pil(download(im))
                g = gray(pimg)
            except:
                continue

            for r in refs:
                h = hash_score(pimg, r)

                best = max(best, h)
                top = update_top(top, h)

                if h < INITIAL_HASH_FILTER:
                    continue

                score = h

                if score < ALERT_THRESHOLD_PERCENT:
                    continue

                send_email(
                    "Possível fraude",
                    f"{url}\nScore: {score:.1f}%"
                )

    state["weekly"]["analyzed"] += analyzed
    state["weekly"]["max"] = best
    state["weekly"]["top"] = top

    if EMAIL_TESTE:
        send_email(
            "Relatório TESTE",
            f"Links analisados: {analyzed}\n"
            f"Maior score: {best:.1f}%\n"
            f"Top: {format_scores(top)}"
        )

    save_json(SEEN_FILE, state)

if __name__ == "__main__":
    main()
