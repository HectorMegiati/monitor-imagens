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
INITIAL_HASH_FILTER = 20
IMAGES_PER_PRODUCT = 3
MAX_IMAGES_PER_SUSPECT_PAGE = 8
MAX_PAGES_PER_RUN = 15
CACHE_REFRESH_SECONDS = 48 * 60 * 60
WEEKLY_REPORT_SECONDS = 7 * 24 * 60 * 60
MIN_IMAGE_SIDE = 24
REQUEST_TIMEOUT = 12
TOP_MATCHES_LIMIT = 3

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

def log(msg: str) -> None:
    print(msg, flush=True)

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

def build_default_weekly() -> dict:
    return {
        "start": now(),
        "analyzed": 0,
        "alerts": 0,
        "max": 0.0,
        "top_scores": [],
        "top_matches": []
    }

def format_scores(scores: list[float] | None) -> str:
    if not scores:
        return "Nenhum"
    return ", ".join(f"{s:.1f}%" for s in scores)

def safe_domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""

def domain_matches(domain: str, pattern: str) -> bool:
    return domain == pattern or domain.endswith("." + pattern)

def get_ref_product_name(ref: dict) -> str:
    return ref.get("product") or ref.get("product_name") or "(sem nome)"

def get_ref_product_url(ref: dict) -> str:
    return ref.get("url") or ref.get("product_url") or ""

def sort_and_trim_matches(matches: list[dict], limit: int = TOP_MATCHES_LIMIT) -> list[dict]:
    sorted_matches = sorted(matches, key=lambda x: float(x.get("score", 0)), reverse=True)
    return sorted_matches[:limit]

def merge_match(existing: list[dict], new_match: dict, limit: int = TOP_MATCHES_LIMIT) -> list[dict]:
    combined = list(existing or [])
    combined.append(new_match)
    dedup = []
    seen_keys = set()

    for item in sorted(combined, key=lambda x: float(x.get("score", 0)), reverse=True):
        key = (
            item.get("page", ""),
            item.get("image", ""),
            item.get("product", ""),
            round(float(item.get("score", 0)), 2),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        dedup.append(item)

    return dedup[:limit]

# =========================
# WHITELIST / RUIDO
# =========================

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
    log(f"Enviando e-mail: {subject}")

    if not EMAIL_HOST or not EMAIL_USER or not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_HOST / EMAIL_USER / EMAIL_PASSWORD não configurados.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_DESTINATION

    s = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30)
    s.starttls()
    s.login(EMAIL_USER, EMAIL_PASSWORD)
    s.sendmail(EMAIL_USER, [EMAIL_DESTINATION], msg.as_string())
    s.quit()

    log("E-mail enviado com sucesso.")

# =========================
# IMAGEM
# =========================

def download(url: str) -> bytes:
    r = requests.get(url, headers=USER_AGENT, timeout=REQUEST_TIMEOUT, allow_redirects=True)
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
        return w >= MIN_IMAGE_SIDE and h >= MIN_IMAGE_SIDE
    except Exception:
        return False

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
            timeout=REQUEST_TIMEOUT,
            headers=USER_AGENT,
        )
        data = r.json()
        if not data:
            break
        products += data
        page += 1

    log(f"Produtos encontrados no WooCommerce: {len(products)}")
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
                refs.append({
                    "product": p.get("name", "(sem nome)"),
                    "url": p.get("permalink", ""),
                    "phash": str(ph),
                    "dhash": str(dh),
                    "whash": str(wh),
                })
            except Exception:
                pass

    log(f"Referências criadas: {len(refs)}")
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
        log(f"Cache carregado: {len(refs)} referências")

    return refs

# =========================
# EXTRAÇÃO DE IMAGENS
# =========================

def extract_first_from_srcset(srcset_value: str | None):
    if not srcset_value:
        return None
    parts = [p.strip() for p in srcset_value.split(",") if p.strip()]
    if not parts:
        return None
    first = parts[0].split(" ")[0].strip()
    return first if first else None

def fetch_page(url: str):
    r = requests.get(url, headers=USER_AGENT, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    content_type = (r.headers.get("Content-Type") or "").lower()
    return r, content_type

def is_direct_image_url(url: str, content_type: str) -> bool:
    low = url.lower()
    if any(low.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]):
        return True
    if content_type and content_type.startswith("image/"):
        return True
    return False

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
    log(f"Links coletados do RSS: {len(urls)}")
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
    weekly.setdefault("max", 0.0)
    weekly.setdefault("top_scores", [])
    weekly.setdefault("top_matches", [])

    # compatibilidade com formatos anteriores
    if "top" in weekly and not weekly.get("top_scores"):
        weekly["top_scores"] = weekly["top"]

    return {"seen": seen, "weekly": weekly}

def save_state(state: dict) -> None:
    save_json(SEEN_FILE, state)

# =========================
# RELATÓRIOS
# =========================

def format_match_list(matches: list[dict]) -> str:
    if not matches:
        return "Nenhum match relevante registrado."

    lines = []
    for idx, m in enumerate(matches, start=1):
        lines.append(f"{idx}. Score: {float(m.get('score', 0)):.1f}%")
        lines.append(f"   Página: {m.get('page', '')}")
        lines.append(f"   Produto: {m.get('product', '')}")
        lines.append(f"   Seu produto: {m.get('product_url', '')}")
        lines.append(f"   Imagem analisada: {m.get('image', '')}")
    return "\n".join(lines)

def build_report_body(title: str, weekly: dict) -> str:
    analyzed = weekly.get("analyzed", 0)
    alerts = weekly.get("alerts", 0)
    best = float(weekly.get("max", 0.0))
    top_scores = weekly.get("top_scores", [])
    top_matches = weekly.get("top_matches", [])

    if alerts == 0:
        body = (
            f"{title}\n\n"
            f"O agente avaliou {analyzed} links suspeitos e não identificou nenhuma pirataria "
            f"com seus arquivos digitais neste período.\n\n"
            f"O maior percentual de semelhança encontrado entre as imagens avaliadas foi de {best:.1f}%.\n"
            f"Top 3 percentuais encontrados: {format_scores(top_scores)}\n\n"
            f"Top matches detalhados:\n{format_match_list(top_matches)}\n"
        )
    else:
        body = (
            f"{title}\n\n"
            f"O agente avaliou {analyzed} links suspeitos neste período.\n"
            f"Foram gerados {alerts} alertas de possível semelhança com seus arquivos digitais.\n"
            f"O maior percentual observado foi de {best:.1f}%.\n"
            f"Top 3 percentuais encontrados: {format_scores(top_scores)}\n\n"
            f"Top matches detalhados:\n{format_match_list(top_matches)}\n"
        )

    return body

def maybe_send_test_report(state: dict) -> None:
    if not EMAIL_TESTE:
        return

    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento (TESTE)",
        state["weekly"],
    ) + "\nEste é um envio de teste.\nDepois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes (TESTE)", body)

def maybe_send_weekly_report(state: dict) -> dict:
    if now() - state["weekly"]["start"] < WEEKLY_REPORT_SECONDS:
        return state

    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento",
        state["weekly"],
    )

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes", body)
    state["weekly"] = build_default_weekly()
    return state

# =========================
# MAIN
# =========================

def main():
    log("START")

    feeds = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)
    state = load_state()

    log(f"Feeds carregados: {len(feeds)}")
    log(f"Whitelist entries: {len(whitelist)}")
    log(f"Links já vistos (cache): {len(state['seen'])}")

    refs = load_cache()
    urls = read_rss(feeds)

    weekly = state["weekly"]
    best = float(weekly.get("max", 0.0))
    top_scores = weekly.get("top_scores", [])
    top_matches = weekly.get("top_matches", [])

    analyzed = 0
    pages_with_images = 0
    image_comparisons = 0
    alerts = []
    noisy_skipped = 0

    for idx, url in enumerate(urls[:MAX_PAGES_PER_RUN], start=1):
        log(f"Processando página {idx}/{min(len(urls), MAX_PAGES_PER_RUN)}: {url}")

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

        if imgs:
            pages_with_images += 1

        for im in imgs:
            try:
                img_bytes = download(im)
                pimg = pil_from_bytes(img_bytes)
                if not is_valid_pil_image(pimg):
                    continue
            except Exception:
                continue

            for r in refs:
                image_comparisons += 1

                try:
                    score = hash_score(pimg, r)
                except Exception:
                    continue

                best = max(best, score)
                top_scores = update_top(top_scores, score)

                match_item = {
                    "page": url,
                    "product": get_ref_product_name(r),
                    "product_url": get_ref_product_url(r),
                    "image": im,
                    "score": score,
                }
                top_matches = merge_match(top_matches, match_item, limit=TOP_MATCHES_LIMIT)

                if score < INITIAL_HASH_FILTER:
                    continue

                if score >= ALERT_THRESHOLD_PERCENT:
                    alerts.append({
                        "page": url,
                        "product": get_ref_product_name(r),
                        "product_url": get_ref_product_url(r),
                        "image": im,
                        "score": score,
                        "links": suspicious(html),
                    })

        log(
            f"Parcial -> páginas com imagens: {pages_with_images}, "
            f"comparações: {image_comparisons}, "
            f"melhor score: {best:.1f}%"
        )

    weekly["analyzed"] += analyzed
    weekly["alerts"] += len(alerts)
    weekly["max"] = best
    weekly["top_scores"] = top_scores
    weekly["top_matches"] = sort_and_trim_matches(top_matches, TOP_MATCHES_LIMIT)

    log(f"Páginas analisadas: {analyzed}")
    log(f"Páginas ignoradas por domínio ruidoso: {noisy_skipped}")
    log(f"Páginas com imagens extraídas: {pages_with_images}")
    log(f"Comparações de imagem realizadas: {image_comparisons}")
    log(f"Maior score nesta execução/semana: {best:.1f}%")
    log(f"Top 3 scores da semana: {format_scores(top_scores)}")
    log("Top matches detalhados:")
    for line in format_match_list(weekly["top_matches"]).splitlines():
        log(line)
    log(f"Alertas gerados (>= {ALERT_THRESHOLD_PERCENT}%): {len(alerts)}")

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
