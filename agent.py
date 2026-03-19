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
    r = requests.get(url, headers=USER_AGENT, timeout=20)
    r.raise_for_status()
    return r.content

def pil(b):
    return Image.open(BytesIO(b)).convert("RGB")

def gray(p):
    return cv2.cvtColor(np.array(p), cv2.COLOR_RGB2GRAY)

def hash_triplet(p):
    return imagehash.phash(p), imagehash.dhash(p), imagehash.whash(p)

def hash_distance_to_percent(distance):
    distance = max(0, min(64, distance))
    return (1 - (distance / 64.0)) * 100

def hash_score(p, ref):
    ph, dh, wh = hash_triplet(p)
    d1 = ph - imagehash.hex_to_hash(ref["phash"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash"])
    d3 = wh - imagehash.hex_to_hash(ref["whash"])

    p1 = hash_distance_to_percent(d1)
    p2 = hash_distance_to_percent(d2)
    p3 = hash_distance_to_percent(d3)

    return (p1 + p2 + p3) / 3.0

def orb_compute(gray_img):
    orb = cv2.ORB_create(1200)
    kp, des = orb.detectAndCompute(gray_img, None)
    return des

def orb_matches(gray_img, ref_orb):
    if ref_orb is None:
        return 0

    des = orb_compute(gray_img)
    if des is None:
        return 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(np.array(ref_orb, dtype=np.uint8), des)
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
    for p in wc_products():
        imgs = (p.get("images") or [])[:IMAGES_PER_PRODUCT]
        for i in imgs:
            try:
                pimg = pil(download(i["src"]))
                ph, dh, wh = hash_triplet(pimg)
                refs.append({
                    "product": p.get("name", "(sem nome)"),
                    "url": p.get("permalink", ""),
                    "phash": str(ph),
                    "dhash": str(dh),
                    "whash": str(wh),
                    "orb": orb_compute(gray(pimg)).tolist() if orb_compute(gray(pimg)) is not None else None
                })
            except Exception:
                pass
    return refs

def load_cache():
    if RESETAR_CACHE:
        save_json(CACHE_FILE, {"last": 0, "refs": []})

    cache = load_json(CACHE_FILE, {"last": 0, "refs": []})

    if now() - cache.get("last", 0) > CACHE_REFRESH_SECONDS or not cache.get("refs"):
        refs = build_refs()
        cache = {"last": now(), "refs": refs}
        save_json(CACHE_FILE, cache)

    return cache["refs"]

# =========================
# SCRAPE
# =========================

def extract_first_from_srcset(srcset_value):
    if not srcset_value:
        return None
    parts = [p.strip() for p in srcset_value.split(",") if p.strip()]
    if not parts:
        return None
    first = parts[0].split(" ")[0].strip()
    return first if first else None

def extract_background_image_urls(style_value):
    if not style_value:
        return []
    matches = re.findall(r'background-image\s*:\s*url\((.*?)\)', style_value, flags=re.IGNORECASE)
    urls = []
    for m in matches:
        u = m.strip().strip('"').strip("'")
        if u:
            urls.append(u)
    return urls

def extract_images(url):
    html = requests.get(url, headers=USER_AGENT, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")

    imgs = []

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        imgs.append(urljoin(url, og["content"]))

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        imgs.append(urljoin(url, tw["content"]))

    for img in soup.find_all("img"):
        candidates = [
            img.get("src"),
            img.get("data-src"),
            img.get("data-lazy-src"),
            img.get("data-original"),
            extract_first_from_srcset(img.get("srcset")),
            extract_first_from_srcset(img.get("data-srcset"))
        ]
        for c in candidates:
            if c:
                imgs.append(urljoin(url, c))

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
                    extract_first_from_srcset(img.get("data-srcset"))
                ]
                for c in candidates:
                    if c:
                        imgs.append(urljoin(url, c))
        except Exception:
            pass

    for tag in soup.find_all(style=True):
        for bg in extract_background_image_urls(tag.get("style")):
            imgs.append(urljoin(url, bg))

    return unique(imgs)[:MAX_IMAGES_PER_SUSPECT_PAGE], html

def suspicious(text):
    return [
        l for l in re.findall(r"https?://\S+", text)
        if any(x in l.lower() for x in ["mega", "drive", "telegram", "t.me"])
    ]

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
# STATE
# =========================

def load_state():
    if RESETAR_LINKS_VISTOS:
        save_json(SEEN_FILE, {})

    state = load_json(SEEN_FILE, {})

    seen = state.get("seen")
    if seen is None:
        seen = state.get("seen_urls", [])

    weekly = state.get("weekly")
    if weekly is None:
        weekly = build_default_weekly()
    else:
        # compatibilidade com formatos antigos
        if "start" not in weekly:
            weekly["start"] = now()
        if "analyzed" not in weekly:
            weekly["analyzed"] = 0
        if "alerts" not in weekly:
            weekly["alerts"] = 0
        if "max" not in weekly:
            weekly["max"] = weekly.get("max_score_below_threshold", 0.0)
        if "top" not in weekly:
            weekly["top"] = weekly.get("top_scores_below_threshold", [])

    return {"seen": seen, "weekly": weekly}

def save_state(state):
    save_json(SEEN_FILE, state)

# =========================
# REPORTS
# =========================

def build_report_body(title, analyzed, alerts, best, top):
    if alerts == 0:
        return (
            f"{title}\n\n"
            f"O agente avaliou {analyzed} links suspeitos e não identificou nenhuma pirataria "
            f"com seus arquivos digitais neste período.\n\n"
            f"O maior percentual de semelhança encontrado entre as imagens avaliadas foi de {best:.1f}%.\n"
            f"Top 3 percentuais encontrados: {format_scores(top)}\n"
        )
    else:
        return (
            f"{title}\n\n"
            f"O agente avaliou {analyzed} links suspeitos neste período.\n"
            f"Foram gerados {alerts} alertas de possível semelhança com seus arquivos digitais.\n"
            f"O maior percentual abaixo do limiar observado foi de {best:.1f}%.\n"
            f"Top 3 percentuais abaixo do limiar: {format_scores(top)}\n"
        )

def maybe_send_test_report(state):
    if not EMAIL_TESTE:
        return

    weekly = state["weekly"]
    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento (TESTE)",
        weekly["analyzed"],
        weekly["alerts"],
        weekly["max"],
        weekly["top"]
    ) + "\nEste é um envio de teste.\nDepois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes (TESTE)", body)

def maybe_send_weekly_report(state):
    if now() - state["weekly"]["start"] < WEEKLY_REPORT_SECONDS:
        return state

    weekly = state["weekly"]
    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento",
        weekly["analyzed"],
        weekly["alerts"],
        weekly["max"],
        weekly["top"]
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

    print(f"Links coletados do RSS: {len(urls)}")

    best = state["weekly"]["max"]
    top = state["weekly"]["top"]
    analyzed = 0
    alerts = []

    for url in urls:
        if url in state["seen"]:
            continue

        state["seen"].append(url)

        if is_whitelisted(url, whitelist):
            continue

        try:
            imgs, html = extract_images(url)
        except Exception:
            continue

        analyzed += 1
        sus = suspicious(html)
        found = False

        for im in imgs:
            try:
                pimg = pil(download(im))
                g = gray(pimg)
            except Exception:
                continue

            for r in refs:
                h = hash_score(pimg, r)

                if h > best:
                    best = h
                top = update_top(top, h, limit=3)

                if h < INITIAL_HASH_FILTER:
                    continue

                orb_n = orb_matches(g, r["orb"])
                orb_score = min(100, (orb_n / 18) * 100)
                score = (h * 0.6) + (orb_score * 0.4)

                if score > best:
                    best = score
                top = update_top(top, score, limit=3)

                if score < ALERT_THRESHOLD_PERCENT:
                    continue

                alerts.append({
                    "page": url,
                    "product": r["product"],
                    "product_url": r["url"],
                    "image": im,
                    "score": score,
                    "links": sus
                })
                found = True
                break

            if found:
                break

    state["weekly"]["analyzed"] += analyzed
    state["weekly"]["alerts"] += len(alerts)
    state["weekly"]["max"] = best
    state["weekly"]["top"] = top

    print(f"Links analisados nesta execução: {analyzed}")
    print(f"Maior score nesta execução/semana: {best:.1f}%")
    print(f"Top 3 scores da semana: {format_scores(top)}")
    print(f"Alertas gerados (>= {ALERT_THRESHOLD_PERCENT}%): {len(alerts)}")

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
