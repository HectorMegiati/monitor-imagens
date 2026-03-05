import os, json, time, re, base64, zlib, smtplib
import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from email.mime.text import MIMEText

import numpy as np
import cv2
from PIL import Image
from io import BytesIO
import imagehash

# =========================
# Arquivos do projeto
# =========================
FEEDS_FILE = "feeds.txt"
WHITELIST_FILE = "whitelist.txt"
SEEN_FILE = "state/seen.json"
CACHE_FILE = "state/ref_cache.json"

# =========================
# Configurações principais
# =========================
IMAGES_PER_PRODUCT = 3
MAX_IMAGES_PER_SUSPECT_PAGE = 12

ALERT_THRESHOLD_PERCENT = 75  # alerta >= 75%

# Cache: atualizar referências do site a cada 48h (dia sim/dia não)
REFRESH_EVERY_SECONDS = 48 * 60 * 60

# Hash tolerâncias
PHASH_MAX_DIST = 10
DHASH_MAX_DIST = 12
WHASH_MAX_DIST = 10

# ORB (para recorte/logo tapada)
ORB_MIN_GOOD_MATCHES = 18
ORB_DISTANCE_CUTOFF = 60

TIMEOUT = 25
USER_AGENT = {"User-Agent": "Mozilla/5.0"}

# =========================
# Variáveis de ambiente (Secrets do GitHub)
# =========================
WC_BASE_URL = os.getenv("WC_BASE_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET", "")

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

# =========================
# Helpers: IO
# =========================
def load_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f.readlines() if l.strip() and not l.strip().startswith("#")]

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# =========================
# Email
# =========================
def send_email(subject: str, body: str):
    if not EMAIL_HOST or not EMAIL_USER or not EMAIL_PASSWORD:
        print("Email não configurado (secrets EMAIL_* faltando).")
        print(body)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_USER

    server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=TIMEOUT)
    server.starttls()
    server.login(EMAIL_USER, EMAIL_PASSWORD)
    server.sendmail(EMAIL_USER, [EMAIL_USER], msg.as_string())
    server.quit()

# =========================
# Whitelist
# =========================
def safe_domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except:
        return ""

def is_whitelisted(url: str, whitelist_entries) -> bool:
    url_norm = url.strip()
    d = safe_domain(url_norm)

    for w in whitelist_entries:
        w = w.strip()
        if not w:
            continue

        # entrada com path (ex: instagram.com/usuario)
        if "/" in w:
            if w in url_norm:
                return True
            continue

        # entrada só domínio
        if d == w or d.endswith("." + w):
            return True

    return False

# =========================
# Download e conversões de imagem
# =========================
def download_bytes(url: str) -> bytes:
    r = requests.get(url, headers=USER_AGENT, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content

def bytes_to_pil(b: bytes) -> Image.Image:
    return Image.open(BytesIO(b)).convert("RGB")

def pil_to_gray_np(pil: Image.Image) -> np.ndarray:
    arr = np.array(pil)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

# =========================
# Hashes + Score
# =========================
def hash_triplet(pil: Image.Image):
    return (imagehash.phash(pil), imagehash.dhash(pil), imagehash.whash(pil))

def dist_to_percent(dist: int, max_dist: int) -> float:
    dist = max(0, dist)
    if dist >= max_dist:
        return 0.0
    return 100.0 * (1.0 - (dist / max_dist))

def hash_similarity_percent(pil: Image.Image, ref) -> float:
    ph, dh, wh = hash_triplet(pil)
    d1 = ph - imagehash.hex_to_hash(ref["phash_hex"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash_hex"])
    d3 = wh - imagehash.hex_to_hash(ref["whash_hex"])

    p1 = dist_to_percent(d1, PHASH_MAX_DIST)
    p2 = dist_to_percent(d2, DHASH_MAX_DIST)
    p3 = dist_to_percent(d3, WHASH_MAX_DIST)

    return (p1 + p2 + p3) / 3.0

# =========================
# ORB
# =========================
def orb_compute_des(gray: np.ndarray):
    orb = cv2.ORB_create(nfeatures=1200)
    kp, des = orb.detectAndCompute(gray, None)
    return des

def compress_des(des: np.ndarray) -> dict:
    if des is None:
        return {"rows": 0, "b64z": ""}
    raw = des.tobytes()
    packed = zlib.compress(raw, level=6)
    b64 = base64.b64encode(packed).decode("ascii")
    return {"rows": int(des.shape[0]), "b64z": b64}

def decompress_des(obj: dict) -> np.ndarray:
    rows = int(obj.get("rows", 0))
    b64z = obj.get("b64z", "")
    if rows <= 0 or not b64z:
        return None
    packed = base64.b64decode(b64z.encode("ascii"))
    raw = zlib.decompress(packed)
    return np.frombuffer(raw, dtype=np.uint8).reshape((rows, 32))

def orb_good_matches_count(gray_candidate: np.ndarray, ref_des: np.ndarray) -> int:
    if ref_des is None:
        return 0
    des2 = orb_compute_des(gray_candidate)
    if des2 is None:
        return 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    try:
        matches = bf.match(ref_des, des2)
    except Exception:
        return 0

    good = [m for m in matches if m.distance < 60]
    return len(good)

def orb_similarity_percent(good_matches: int) -> float:
    return max(0.0, min(100.0, (good_matches / ORB_MIN_GOOD_MATCHES) * 100.0))

def hybrid_similarity_percent(hash_pct: float, orb_pct: float) -> float:
    return (0.60 * hash_pct) + (0.40 * orb_pct)

# =========================
# WooCommerce: produtos e imagens
# =========================
def wc_get_products():
    if not WC_BASE_URL or not WC_CONSUMER_KEY or not WC_CONSUMER_SECRET:
        raise RuntimeError("Secrets do WooCommerce faltando (WC_BASE_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET).")

    products = []
    page = 1
    while True:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products"
        params = {
            "consumer_key": WC_CONSUMER_KEY,
            "consumer_secret": WC_CONSUMER_SECRET,
            "per_page": 100,
            "page": page,
            "status": "publish",
        }
        r = requests.get(url, params=params, headers=USER_AGENT, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        products.extend(data)
        page += 1
    return products

def build_refs_from_site():
    products = wc_get_products()
    refs = []

    for p in products:
        product_name = p.get("name", "(sem nome)")
        product_url = p.get("permalink", "")
        images = (p.get("images", []) or [])[:IMAGES_PER_PRODUCT]

        for img_obj in images:
            img_url = img_obj.get("src")
            if not img_url:
                continue
            try:
                b = download_bytes(img_url)
                pil = bytes_to_pil(b)

                ph, dh, wh = hash_triplet(pil)
                gray = pil_to_gray_np(pil)
                des = orb_compute_des(gray)

                refs.append({
                    "product_name": product_name,
                    "product_url": product_url,
                    "ref_image": img_url,
                    "phash_hex": str(ph),
                    "dhash_hex": str(dh),
                    "whash_hex": str(wh),
                    "orb": compress_des(des),
                })
            except Exception:
                continue

    if not refs:
        raise RuntimeError("Não consegui montar referências do site.")
    return refs

def load_or_refresh_cache():
    cache = load_json(CACHE_FILE, {"last_refresh_epoch": 0, "refs": []})
    last = int(cache.get("last_refresh_epoch", 0))
    now = int(time.time())

    if (now - last) >= (48 * 60 * 60) or not cache.get("refs"):
        refs = build_refs_from_site()
        cache = {"last_refresh_epoch": now, "refs": refs}
        save_json(CACHE_FILE, cache)

    return cache["refs"]

# =========================
# Página suspeita: imagens + links suspeitos
# =========================
SUSPICIOUS_KEYWORDS = [
    "mega", "mega.nz", "drive", "google drive", "telegram",
    "grátis", "gratis", "download", "link", "pacote", "coleção", "colecao"
]

def extract_suspicious_links(html: str, base_url: str):
    links = set()
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        full = urljoin(base_url, href)
        low = full.lower()
        if "mega.nz" in low or "drive.google.com" in low or "t.me" in low or "telegram" in low:
            links.add(full)

    text = soup.get_text(" ", strip=True)
    for m in re.findall(r"(https?://\S+)", text):
        low = m.lower()
        if "mega.nz" in low or "drive.google.com" in low or "t.me" in low:
            links.add(m)

    return sorted(list(links))[:10]

def extract_images_from_page(url: str):
    r = requests.get(url, headers=USER_AGENT, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    img_urls = []

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        img_urls.append(urljoin(url, og["content"].strip()))

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        img_urls.append(urljoin(url, tw["content"].strip()))

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        img_urls.append(urljoin(url, src.strip()))

    out = []
    seen = set()
    for u in img_urls:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= MAX_IMAGES_PER_SUSPECT_PAGE:
            break

    suspect_links = extract_suspicious_links(html, url)
    return out, suspect_links

# =========================
# RSS
# =========================
def read_rss_candidates(feed_urls):
    urls = []
    for feed in feed_urls:
        d = feedparser.parse(feed)
        for e in d.entries:
            link = getattr(e, "link", None)
            if link:
                urls.append(link)
    out, s = [], set()
    for u in urls:
        if u not in s:
            s.add(u)
            out.append(u)
    return out

def main():
    feed_urls = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)
    seen_obj = load_json(SEEN_FILE, {"seen_urls": []})
    seen = set(seen_obj.get("seen_urls", []))

    if not feed_urls:
        raise RuntimeError("feeds.txt está vazio. Cole os RSS do Google Alerts (1 por linha).")

    refs = load_or_refresh_cache()
    candidates = read_rss_candidates(feed_urls)

    alerts = []

    for page_url in candidates:
        if page_url in seen:
            continue
        seen.add(page_url)

        if is_whitelisted(page_url, whitelist):
            continue

        try:
            img_urls, suspect_links = extract_images_from_page(page_url)
        except Exception:
            continue

        best_hit = None

        for img_url in img_urls:
            try:
                b = download_bytes(img_url)
                pil = bytes_to_pil(b)
                gray = pil_to_gray_np(pil)
            except Exception:
                continue

            for ref in refs:
                try:
                    h_pct = hash_similarity_percent(pil, ref)
                except Exception:
                    continue

                if h_pct < 35:
                    continue

                ref_des = decompress_des(ref.get("orb", {}))
                good = orb_good_matches_count(gray, ref_des)
                o_pct = orb_similarity_percent(good)

                score = hybrid_similarity_percent(h_pct, o_pct)

                if best_hit is None or score > best_hit["score"]:
                    best_hit = {
                        "suspect_page": page_url,
                        "suspect_image": img_url,
                        "product_name": ref["product_name"],
                        "product_url": ref["product_url"],
                        "score": score,
                        "suspect_links": suspect_links,
                    }

            if best_hit and best_hit["score"] >= 90:
                break

        if best_hit and best_hit["score"] >= ALERT_THRESHOLD_PERCENT:
            alerts.append(best_hit)

    save_json(SEEN_FILE, {"seen_urls": sorted(list(seen))})

    if alerts:
        lines = ["Alertas de Possíveis Fraudes", ""]
        for a in alerts[:20]:
            lines.append(f"Página suspeita (URL): {a['suspect_page']}")
            lines.append(f"Produto parecido (Título): {a['product_name']}")
            lines.append(f"Seu produto (URL): {a['product_url']}")
            lines.append(f"Imagem suspeita: {a['suspect_image']}")
            lines.append(f"Score de similaridade: {a['score']:.1f}%")
            if a["suspect_links"]:
                lines.append("Links suspeitos encontrados (mega/drive/telegram):")
                for l in a["suspect_links"]:
                    lines.append(f"  - {l}")
            lines.append("")

        send_email("Alertas de Possíveis Fraudes", "\n".join(lines))

if __name__ == "__main__":
    main()
