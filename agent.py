# =========================
# IMPORTS
# =========================
import os
import json
import time
import re
import smtplib
from io import BytesIO
from urllib.parse import urlparse, urljoin, parse_qs, unquote

import requests
import feedparser
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from PIL import Image
import imagehash

# =========================
# CONFIGURAÇÕES
# =========================

ALERT_THRESHOLD_PERCENT = 60
INITIAL_HASH_FILTER = 20   # <-- CORREÇÃO AQUI
IMAGES_PER_PRODUCT = 3
MAX_IMAGES_PER_SUSPECT_PAGE = 8
MAX_PAGES_PER_RUN = 15

CACHE_REFRESH_SECONDS = 48 * 60 * 60
WEEKLY_REPORT_SECONDS = 7 * 24 * 60 * 60

MIN_IMAGE_SIDE = 24
REQUEST_TIMEOUT = 12

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
]

# =========================
# ENV
# =========================

WC_BASE_URL = os.getenv("WC_BASE_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET", "")

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

USER_AGENT = {
    "User-Agent": "Mozilla/5.0"
}

# =========================
# LOG
# =========================

def log(msg):
    print(msg, flush=True)

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
        json.dump(obj, f, indent=2)

def now():
    return int(time.time())

def safe_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except:
        return ""

# =========================
# EMAIL
# =========================

def send_email(subject, body):
    log(f"Enviando e-mail: {subject}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_DESTINATION

    s = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
    s.starttls()
    s.login(EMAIL_USER, EMAIL_PASSWORD)
    s.sendmail(EMAIL_USER, [EMAIL_DESTINATION], msg.as_string())
    s.quit()

    log("E-mail enviado com sucesso")

# =========================
# IMAGEM
# =========================

def download(url):
    r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=USER_AGENT)
    return r.content

def pil_from_bytes(b):
    return Image.open(BytesIO(b)).convert("RGB")

def hash_score(img, ref):
    ph = imagehash.phash(img)
    return (1 - (ph - imagehash.hex_to_hash(ref["phash"])) / 64) * 100

# =========================
# CACHE
# =========================

def load_cache():
    cache = load_json(CACHE_FILE, {"refs": []})
    log(f"Cache carregado: {len(cache['refs'])} referências")
    return cache["refs"]

# =========================
# EXTRAÇÃO DE IMAGENS
# =========================

def extract_images(url):
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=USER_AGENT)
        soup = BeautifulSoup(r.text, "html.parser")
        imgs = []

        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                imgs.append(urljoin(url, src))

        return imgs[:MAX_IMAGES_PER_SUSPECT_PAGE], r.text

    except:
        return [], ""

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

    log(f"Links coletados do RSS: {len(urls)}")
    return urls

# =========================
# MAIN
# =========================

def main():
    log("START")

    feeds = load_lines(FEEDS_FILE)
    refs = load_cache()
    urls = read_rss(feeds)

    best = 0

    for i, url in enumerate(urls[:MAX_PAGES_PER_RUN]):
        log(f"Página {i+1}: {url}")

        imgs, html = extract_images(url)

        for im in imgs:
            try:
                img = pil_from_bytes(download(im))
            except:
                continue

            for r in refs:
                score = hash_score(img, r)

                best = max(best, score)

                if score < INITIAL_HASH_FILTER:
                    continue

    log(f"Maior score: {best:.1f}%")

    if EMAIL_TESTE:
        send_email(
            "TESTE DO AGENTE",
            f"Maior score encontrado: {best:.1f}%"
        )

if __name__ == "__main__":
    main()
