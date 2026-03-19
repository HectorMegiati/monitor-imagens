import os
import json
import time
import re
import smtplib
from io import BytesIO
from urllib.parse import urlparse, urljoin, parse_qs, unquote

import requests
import feedparser
import numpy as np
import cv2
from PIL import Image
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
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
MIN_IMAGE_SIDE_FOR_ANALYSIS = 24

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
    "g1.globo.com",
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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# =========================
# UTIL
# =========================

def load_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def unique(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out

def now() -> int:
    return int(time.time())

def update_top(scores: list[float] | None, new_score: float, limit: int = 3) -> list[float]:
    items = list(scores or [])
    items.append(float(new_score))
    return sorted(items, reverse=True)[:limit]

def format_scores(scores: list[float] | None) -> str:
    if not scores:
        return "Nenhum"
    return ", ".join(f"{s:.1f}%" for s in scores)

def build_default_weekly() -> dict:
    return {
        "start": now(),
        "analyzed": 0,
        "alerts": 0,
        "max": 0.0,
        "top": [],
    }

def get_ref_product_name(ref: dict) -> str:
    return ref.get("product") or ref.get("product_name") or "(sem nome)"

def get_ref_product_url(ref: dict) -> str:
    return ref.get("url") or ref.get("product_url") or ""

# =========================
# WHITELIST
# =========================

def safe_domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""

def domain_matches(domain: str, pattern: str) -> bool:
    return domain == pattern or domain.endswith("." + pattern)

def is_whitelisted(url: str, entries: list[str]) -> bool:
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

def is_noisy_domain(url: str) -> bool:
    d = safe_domain(url)
    for nd in NOISY_DOMAINS:
        if domain_matches(d, nd):
            return True
    return False

# =========================
# GOOGLE URL UNWRAP
# =========================

def unwrap_google_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = safe_domain(url)

        if domain in {"google.com", "google.com.br"} and parsed.path == "/url":
            qs = parse_qs(parsed.query)
            target = qs.get("url") or qs.get("q")
            if target and target[0]:
                return unquote(target[0])

        return url
    except Exception:
        return url

# =========================
# EMAIL
# =========================

def send_email(subject: str, body: str) -> None:
    print(f"Enviando e-mail: {subject}")
    print(f"Remetente: {EMAIL_USER}")
    print(f"Destino: {EMAIL_DESTINATION}")

    if not EMAIL_HOST or not EMAIL_USER or not EMAIL_PASSWORD:
        raise RuntimeError("Configuração de e-mail incompleta: EMAIL_HOST / EMAIL_USER / EMAIL_PASSWORD.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_DESTINATION

    s = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30)
    s.starttls()
    s.login(EMAIL_USER, EMAIL_PASSWORD)
    s.sendmail(EMAIL_USER, [EMAIL_DESTINATION], msg.as_string())
    s.quit()

    print("E-mail enviado com sucesso.")

# =========================
# IMAGEM
# =========================

def download(url: str) -> bytes:
    r = requests.get(url, headers=USER_AGENT, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return r.content

def pil_from_bytes(b: bytes) -> Image.Image:
    img = Image.open(BytesIO(b))
    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def is_valid_pil_image(img: Image.Image) -> bool:
    try:
        w, h = img.size
        return w >= MIN_IMAGE_SIDE_FOR_ANALYSIS and h >= MIN_IMAGE_SIDE_FOR_ANALYSIS
    except Exception:
        return False

def gray(img: Image.Image):
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

def hash_triplet(img: Image.Image):
    return imagehash.phash(img), imagehash.dhash(img), imagehash.whash(img)

def hash_distance_to_percent(distance: int) -> float:
    distance = max(0, min(64, distance))
    return (1 - (distance / 64.0)) * 100

def hash_score(img: Image.Image, ref: dict) -> float:
    ph, dh, wh = hash_triplet(img)
    d1 = ph - imagehash.hex_to_hash(ref["phash"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash"])
    d3 = wh - imagehash.hex_to_hash(ref["whash"])

    return (
        hash_distance_to_percent(d1)
        + hash_distance_to_percent(d2)
        + hash_distance_to_percent(d3)
    ) / 3.0

def orb_compute(gray_img):
    if gray_img is None:
        return None
    try:
        h, w = gray_img.shape[:2]
        if h < MIN_IMAGE_SIDE_FOR_ANALYSIS or w < MIN_IMAGE_SIDE_FOR_ANALYSIS:
            return None
        orb = cv2.ORB_create(1200)
        _, des = orb.detectAndCompute(gray_img, None)
        return des
    except Exception:
        return None

def orb_matches(gray_img, ref_orb) -> int:
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

def wc_products() -> list[dict]:
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
                "page": page,
            },
            timeout=20,
            headers=USER_AGENT,
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

def build_refs() -> list[dict]:
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
                    "orb": des.tolist() if des is not None else None,
                })
            except Exception:
                pass

    print(f"Referências criadas: {len(refs)}")
    return refs

def load_cache() -> list[dict]:
    if RESETAR_CACHE:
        save_json(CACHE_FILE, {"last": 0, "refs": []})

    cache = load_json(CACHE_FILE, {"last": 0, "refs": []})

    if now() - cache.get("last", 0) > CACHE_REFRESH_SECONDS or not cache.get("refs"):
        refs = build_refs()
        cache = {"last": now(), "refs": refs}
        save_json(CACHE_FILE, cache)
    else:
        refs = cache["refs"]
        print(f"Cache carregado: {len(refs)} referências")

    return refs

# =========================
# EXTRAÇÃO ROBUSTA DE IMAGENS
# =========================

def extract_first_from_srcset(srcset_value: str | None):
    if not srcset_value:
        return None
    parts = [p.strip() for p in srcset_value.split(",") if p.strip()]
    if not parts:
        return None
    first = parts[0].split(" ")[0].strip()
    return first if first else None

def extract_background_image_urls(style_value: str | None) -> list[str]:
    if not style_value:
        return []
    matches = re.findall(
        r"background-image\s*:\s*url\((.*?)\)",
        style_value,
        flags=re.IGNORECASE,
    )
    urls = []
    for m in matches:
        u = m.strip().strip('"').strip("'")
        if u:
            urls.append(u)
    return urls

def is_direct_image_url(url: str, content_type: str) -> bool:
    low = url.lower()
    if any(low.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]):
        return True
    if content_type and content_type.startswith("image/"):
        return True
    return False

def fetch_page(url: str):
    r = requests.get(url, headers=USER_AGENT, timeout=20, allow_redirects=True)
    content_type = (r.headers.get("Content-Type") or "").lower()
    return r, content_type

def extract_images(url: str):
    try:
        r, content_type = fetch_page(url)
    except Exception:
        return [], "", "fetch_error"

    final_url = r.url

    if is_direct_image_url(final_url, content_type):
        return [final_url], "", content_type

    if "html" not in content_type and "xml" not in content_type and content_type != "":
        return [], r.text if hasattr(r, "text") else "", content_type

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    imgs = []

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        imgs.append(urljoin(final_url, og["content"]))

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        imgs.append(urljoin(final_url, tw["content"]))

    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])) if isinstance(link.get("rel"), list) else str(link.get("rel", ""))
        href = link.get("href")
        if not href:
            continue
        low_rel = rel.lower()
        low_href = href.lower()
        if "image" in low_rel or any(low_href.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            imgs.append(urljoin(final_url, href))

    for img in soup.find_all("img"):
        candidates = [
            img.get("src"),
            img.get("data-src"),
            img.get("data-lazy-src"),
            img.get("data-original"),
            img.get("data-image"),
            extract_first_from_srcset(img.get("srcset")),
            extract_first_from_srcset(img.get("data-srcset")),
        ]
        for c in candidates:
            if c:
                imgs.append(urljoin(final_url, c))

    for noscript in soup.find_all("noscript"):
        try:
            inner = BeautifulSoup(noscript.decode_contents(), "html.parser")
            for img in inner.find_all("img"):
                candidates = [
                    img.get("src"),
                    img.get("data-src"),
                    img.get("data-lazy-src"),
                    img.get("data-original"),
                    extract_first_from_srcset(img.get("srcset")),
                    extract_first_from_srcset(img.get("data-srcset")),
                ]
                for c in candidates:
                    if c:
                        imgs.append(urljoin(final_url, c))
        except Exception:
            pass

    for tag in soup.find_all(style=True):
        for bg in extract_background_image_urls(tag.get("style")):
            imgs.append(urljoin(final_url, bg))

    imgs = unique(imgs)

    filtered = []
    for i in imgs:
        low = i.lower()
        if any(x in low for x in [".svg", "logo", "avatar", "favicon", "icon"]):
            continue
        filtered.append(i)

    return filtered[:MAX_IMAGES_PER_SUSPECT_PAGE], html, content_type or "text/html"

def suspicious(text: str) -> list[str]:
    return [
        l for l in re.findall(r"https?://\S+", text)
        if any(x in l.lower() for x in ["mega", "drive", "telegram", "t.me"])
    ]

# =========================
# RSS
# =========================

def read_rss(feeds: list[str]) -> list[str]:
    urls = []
    for f in feeds:
        d = feedparser.parse(f)
        for e in d.entries:
            if hasattr(e, "link"):
                urls.append(unwrap_google_url(e.link))

    urls = unique(urls)
    print(f"Links coletados do RSS: {len(urls)}")
    return urls

# =========================
# STATE
# =========================

def load_state() -> dict:
    if RESETAR_LINKS_VISTOS:
        save_json(SEEN_FILE, {})

    state = load_json(SEEN_FILE, {})

    seen = state.get("seen")
    if seen is None:
        seen = state.get("seen_urls", [])

    weekly = state.get("weekly")
    if weekly is None:
        weekly = build_default_weekly()

    weekly.setdefault("start", now())
    weekly.setdefault("analyzed", 0)
    weekly.setdefault("alerts", 0)
    weekly.setdefault("max", weekly.get("max_score_below_threshold", 0.0))
    weekly.setdefault("top", weekly.get("top_scores_below_threshold", []))

    return {"seen": seen, "weekly": weekly}

def save_state(state: dict) -> None:
    save_json(SEEN_FILE, state)

# =========================
# REPORTS
# =========================

def build_report_body(title: str, analyzed: int, alerts: int, best: float, top: list[float]) -> str:
    if alerts == 0:
        return (
            f"{title}\n\n"
            f"O agente avaliou {analyzed} links suspeitos e não identificou nenhuma pirataria "
            f"com seus arquivos digitais neste período.\n\n"
            f"O maior percentual de semelhança encontrado entre as imagens avaliadas foi de {best:.1f}%.\n"
            f"Top 3 percentuais encontrados: {format_scores(top)}\n"
        )
    return (
        f"{title}\n\n"
        f"O agente avaliou {analyzed} links suspeitos neste período.\n"
        f"Foram gerados {alerts} alertas de possível semelhança com seus arquivos digitais.\n"
        f"O maior percentual observado foi de {best:.1f}%.\n"
        f"Top 3 percentuais encontrados: {format_scores(top)}\n"
    )

def maybe_send_test_report(state: dict) -> None:
    if not EMAIL_TESTE:
        return

    weekly = state["weekly"]
    body = (
        build_report_body(
            "Relatório Semanal do Agente de Monitoramento (TESTE)",
            weekly["analyzed"],
            weekly["alerts"],
            weekly["max"],
            weekly["top"],
        )
        + "\nEste é um envio de teste.\n"
        + "Depois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"
    )

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes (TESTE)", body)

def maybe_send_weekly_report(state: dict) -> dict:
    if now() - state["weekly"]["start"] < WEEKLY_REPORT_SECONDS:
        return state

    weekly = state["weekly"]
    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento",
        weekly["analyzed"],
        weekly["alerts"],
        weekly["max"],
        weekly["top"],
    )

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes", body)
    state["weekly"] = build_default_weekly()
    return state

# =========================
# MAIN
# =========================

def main():
    print("START")

    feeds = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)
    state = load_state()

    print(f"Feeds carregados: {len(feeds)}")
    print(f"Whitelist entries: {len(whitelist)}")
    print(f"Links já vistos (cache): {len(state['seen'])}")

    refs = load_cache()
    urls = read_rss(feeds)

    best = state["weekly"]["max"]
    top = state["weekly"]["top"]

    analyzed = 0
    pages_with_images = 0
    image_comparisons = 0
    alerts = []
    content_type_counter = {}
    no_image_examples = []
    noisy_skipped = 0

    for url in urls:
        if url in state["seen"]:
            continue

        state["seen"].append(url)

        if is_whitelisted(url, whitelist):
            continue

        if is_noisy_domain(url):
            noisy_skipped += 1
            continue

        try:
            imgs, html, content_type = extract_images(url)
        except Exception:
            continue

        analyzed += 1
        content_type_counter[content_type] = content_type_counter.get(content_type, 0) + 1

        if imgs:
            pages_with_images += 1
        elif len(no_image_examples) < 5:
            no_image_examples.append(f"{url} [{content_type}]")

        for im in imgs:
            try:
                img_bytes = download(im)
                pimg = pil_from_bytes(img_bytes)
                if not is_valid_pil_image(pimg):
                    continue

                g = gray(pimg)
            except Exception:
                continue

            for r in refs:
                image_comparisons += 1

                h = hash_score(pimg, r)
                best = max(best, h)
                top = update_top(top, h)

                if h < INITIAL_HASH_FILTER:
                    continue

                orb_n = orb_matches(g, r["orb"])
                orb_score = min(100, (orb_n / 18) * 100)
                score = (h * 0.6) + (orb_score * 0.4)

                best = max(best, score)
                top = update_top(top, score)

                if score >= ALERT_THRESHOLD_PERCENT:
                    alerts.append({
                        "page": url,
                        "product": get_ref_product_name(r),
                        "product_url": get_ref_product_url(r),
                        "image": im,
                        "score": score,
                        "links": suspicious(html),
                    })

    state["weekly"]["analyzed"] += analyzed
    state["weekly"]["alerts"] += len(alerts)
    state["weekly"]["max"] = best
    state["weekly"]["top"] = top

    print(f"Páginas analisadas: {analyzed}")
    print(f"Páginas ignoradas por domínio ruidoso: {noisy_skipped}")
    print(f"Páginas com imagens extraídas: {pages_with_images}")
    print(f"Comparações de imagem realizadas: {image_comparisons}")
    print(f"Maior score nesta execução/semana: {best:.1f}%")
    print(f"Top 3 scores da semana: {format_scores(top)}")
    print(f"Alertas gerados (>= {ALERT_THRESHOLD_PERCENT}%): {len(alerts)}")
    print(f"Tipos de conteúdo encontrados: {content_type_counter}")
    print(f"Exemplos sem imagens: {no_image_examples}")

    maybe_send_test_report(state)

    if alerts:
        body = "Alertas de Possíveis Fraudes\n\n"
        for a in alerts:
            body += f"Página suspeita: {a['page']}\n"
            body += f"Produto parecido: {a['product']}\n"
            body += f"Seu produto: {a['product_url']}\n"
            body += f"Imagem suspeita: {a['image']}\n"
            body += f"Score de similaridade: {a['score']:.1f}%\n"
            if a["links"]:
                body += "Links suspeitos encontrados:\n"
                for l in a["links"]:
                    body += f"- {l}\n"
            body += "\n"

        send_email("Alertas de Possíveis Fraudes", body)

    state = maybe_send_weekly_report(state)
    save_state(state)

if __name__ == "__main__":
    main()
