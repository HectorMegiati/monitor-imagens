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
IMAGES_PER_PRODUCT = 3
MAX_IMAGES_PER_SUSPECT_PAGE = 20
CACHE_REFRESH_SECONDS = 48 * 60 * 60
WEEKLY_REPORT_SECONDS = 7 * 24 * 60 * 60

EMAIL_DESTINATION = "guilhermefariadeangeli@gmail.com"

# =========================
# CONTROLES MANUAIS
# =========================
# Quando True, envia imediatamente um e-mail com o formato do relatório semanal
EMAIL_TESTE = False

# Use True uma única vez se quiser reconstruir o cache de referências do site
RESETAR_CACHE = False

# Use True uma única vez se quiser reavaliar todos os links novamente
RESETAR_LINKS_VISTOS = False

# =========================
# PATHS
# =========================

FEEDS_FILE = "feeds.txt"
WHITELIST_FILE = "whitelist.txt"
SEEN_FILE = "state/seen.json"
CACHE_FILE = "state/ref_cache.json"

# =========================
# VARIÁVEIS DE AMBIENTE
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
# UTILIDADES
# =========================

def load_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = []
        for l in f.readlines():
            l = l.strip()
            if not l:
                continue
            if l.startswith("#"):
                continue
            lines.append(l)
        return lines

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def current_timestamp():
    return int(time.time())

def unique_keep_order(items):
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

# =========================
# EMAIL
# =========================

def send_email(subject, body):
    print(f"Enviando e-mail: {subject}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_DESTINATION

    server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
    server.starttls()
    server.login(EMAIL_USER, EMAIL_PASSWORD)

    server.sendmail(
        EMAIL_USER,
        [EMAIL_DESTINATION],
        msg.as_string()
    )

    server.quit()
    print("E-mail enviado.")

# =========================
# WHITELIST
# =========================

def safe_domain(url):
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except:
        return ""

def is_whitelisted(url, entries):
    domain = safe_domain(url)
    for w in entries:
        if "/" in w:
            if w in url:
                return True
        else:
            if domain == w or domain.endswith("." + w):
                return True
    return False

# =========================
# IMAGEM
# =========================

def download_bytes(url):
    r = requests.get(url, headers=USER_AGENT, timeout=20)
    r.raise_for_status()
    return r.content

def bytes_to_pil(b):
    return Image.open(BytesIO(b)).convert("RGB")

def pil_to_gray_np(pil):
    arr = np.array(pil)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

def hash_triplet(pil):
    return (
        imagehash.phash(pil),
        imagehash.dhash(pil),
        imagehash.whash(pil)
    )

# =========================
# ORB
# =========================

def orb_compute(gray):
    orb = cv2.ORB_create(nfeatures=1200)
    kp, des = orb.detectAndCompute(gray, None)
    return des

def orb_match(gray, ref_des):
    if ref_des is None:
        return 0

    des = orb_compute(gray)
    if des is None:
        return 0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(ref_des, des)
    good = [m for m in matches if m.distance < 60]
    return len(good)

# =========================
# HASH SCORE
# =========================

def similarity_hash_percent(pil, ref):
    ph, dh, wh = hash_triplet(pil)

    d1 = ph - imagehash.hex_to_hash(ref["phash"])
    d2 = dh - imagehash.hex_to_hash(ref["dhash"])
    d3 = wh - imagehash.hex_to_hash(ref["whash"])

    score = (1 - ((d1 + d2 + d3) / 30)) * 100
    return max(0, score)

# =========================
# WOO PRODUCTS
# =========================

def wc_products():
    products = []
    page = 1

    while True:
        url = f"{WC_BASE_URL}/wp-json/wc/v3/products"

        params = {
            "consumer_key": WC_CONSUMER_KEY,
            "consumer_secret": WC_CONSUMER_SECRET,
            "per_page": 100,
            "page": page
        }

        r = requests.get(url, params=params, headers=USER_AGENT, timeout=20)
        data = r.json()

        if not data:
            break

        products += data
        page += 1

    print(f"Produtos encontrados no WooCommerce: {len(products)}")
    return products

# =========================
# BUILD CACHE
# =========================

def build_refs():
    refs = []
    products = wc_products()

    for p in products:
        name = p.get("name", "(sem nome)")
        link = p.get("permalink", "")

        images = (p.get("images", []) or [])[:IMAGES_PER_PRODUCT]

        for img in images:
            try:
                url = img.get("src")
                if not url:
                    continue

                b = download_bytes(url)
                pil = bytes_to_pil(b)

                ph, dh, wh = hash_triplet(pil)
                gray = pil_to_gray_np(pil)
                des = orb_compute(gray)

                refs.append({
                    "product_name": name,
                    "product_url": link,
                    "phash": str(ph),
                    "dhash": str(dh),
                    "whash": str(wh),
                    "orb": des.tolist() if des is not None else None
                })

            except Exception:
                continue

    print(f"Referências de imagem criadas: {len(refs)}")
    return refs

# =========================
# LOAD CACHE
# =========================

def load_cache():
    if RESETAR_CACHE:
        print("RESETAR_CACHE=True -> limpando cache de referências do site...")
        save_json(CACHE_FILE, {"last": 0, "refs": []})

    cache = load_json(CACHE_FILE, {"last": 0, "refs": []})

    if time.time() - cache.get("last", 0) > CACHE_REFRESH_SECONDS or not cache.get("refs"):
        print("Atualizando cache de imagens do site...")
        refs = build_refs()
        cache = {
            "last": time.time(),
            "refs": refs
        }
        save_json(CACHE_FILE, cache)
    else:
        refs = cache["refs"]
        print(f"Cache carregado: {len(refs)} referências (sem atualizar)")

    return refs

# =========================
# EXTRAÇÃO MELHORADA DE IMAGENS
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

def extract_page_images(url):
    r = requests.get(url, headers=USER_AGENT, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")

    imgs = []

    # og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        imgs.append(urljoin(url, og["content"]))

    # twitter:image
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        imgs.append(urljoin(url, tw["content"]))

    # img tags
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

    # noscript
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
            continue

    # background-image inline
    for tag in soup.find_all(style=True):
        for bg in extract_background_image_urls(tag.get("style")):
            imgs.append(urljoin(url, bg))

    imgs = unique_keep_order(imgs)
    imgs = imgs[:MAX_IMAGES_PER_SUSPECT_PAGE]

    return imgs, r.text

# =========================
# LINKS SUSPEITOS
# =========================

def suspicious_links(text):
    links = set()

    for m in re.findall(r"https?://\S+", text):
        low = m.lower()
        if any(x in low for x in ["mega.nz", "drive.google.com", "telegram", "t.me"]):
            links.add(m)

    return list(links)

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

    urls = list(set(urls))
    print(f"Links coletados do RSS: {len(urls)}")
    return urls

# =========================
# ESTADO SEMANAL
# =========================

def load_seen_state():
    if RESETAR_LINKS_VISTOS:
        print("RESETAR_LINKS_VISTOS=True -> limpando lista de links já vistos...")
        save_json(SEEN_FILE, {})

    data = load_json(SEEN_FILE, {})

    seen_list = data.get("seen")
    if seen_list is None:
        seen_list = data.get("seen_urls", [])

    weekly = data.get("weekly")
    if weekly is None:
        weekly = {
            "start": current_timestamp(),
            "analyzed": 0,
            "alerts": 0,
            "max_score_below_threshold": 0
        }

    return {
        "seen": seen_list,
        "weekly": weekly
    }

def save_seen_state(state):
    save_json(SEEN_FILE, state)

def maybe_send_test_weekly_report(state):
    if not EMAIL_TESTE:
        return

    weekly = state["weekly"]
    analyzed = weekly.get("analyzed", 0)
    alerts = weekly.get("alerts", 0)
    max_score_below = weekly.get("max_score_below_threshold", 0)

    if alerts == 0:
        body = (
            "Relatório Semanal do Agente de Monitoramento (TESTE)\n\n"
            f"O agente avaliou {analyzed} links suspeitos e não identificou nenhuma pirataria "
            "com seus arquivos digitais neste período.\n\n"
            f"O maior percentual de semelhança encontrado entre as imagens avaliadas foi de {max_score_below:.1f}%.\n\n"
            "Este é um envio de teste para validar o formato do relatório semanal.\n"
            "Depois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"
        )
    else:
        body = (
            "Relatório Semanal do Agente de Monitoramento (TESTE)\n\n"
            f"O agente avaliou {analyzed} links suspeitos neste período.\n"
            f"Foram gerados {alerts} alertas de possível semelhança com seus arquivos digitais.\n"
            f"O maior percentual abaixo do limiar observado foi de {max_score_below:.1f}%.\n\n"
            "Este é um envio de teste para validar o formato do relatório semanal.\n"
            "Depois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"
        )

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes (TESTE)", body)

def maybe_send_weekly_report(state):
    now = current_timestamp()
    weekly = state["weekly"]

    if now - weekly["start"] < WEEKLY_REPORT_SECONDS:
        return state

    analyzed = weekly.get("analyzed", 0)
    alerts = weekly.get("alerts", 0)
    max_score_below = weekly.get("max_score_below_threshold", 0)

    if alerts == 0:
        body = (
            "Relatório Semanal do Agente de Monitoramento\n\n"
            f"O agente avaliou {analyzed} links suspeitos e não identificou nenhuma pirataria "
            "com seus arquivos digitais nesta semana.\n\n"
            f"O maior percentual de semelhança encontrado entre as imagens avaliadas foi de {max_score_below:.1f}%.\n\n"
            "Isso pode significar que:\n"
            "- não houve aparição pública de cópias não autorizadas;\n"
            "- ou os materiais encontrados apresentaram apenas semelhança parcial.\n"
        )
    else:
        body = (
            "Relatório Semanal do Agente de Monitoramento\n\n"
            f"O agente avaliou {analyzed} links suspeitos nesta semana.\n"
            f"Foram gerados {alerts} alertas de possível semelhança com seus arquivos digitais.\n"
            f"O maior percentual abaixo do limiar observado na semana foi de {max_score_below:.1f}%.\n"
        )

    send_email("Relatório Semanal - Monitoramento de Possíveis Fraudes", body)

    state["weekly"] = {
        "start": now,
        "analyzed": 0,
        "alerts": 0,
        "max_score_below_threshold": 0
    }

    return state

# =========================
# MAIN
# =========================

def main():
    print("=== MONITOR START ===")

    feeds = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)

    state = load_seen_state()
    seen_list = state["seen"]

    print(f"Feeds carregados: {len(feeds)}")
    print(f"Whitelist entries: {len(whitelist)}")
    print(f"Links já vistos (cache): {len(seen_list)}")

    refs = load_cache()
    urls = read_rss(feeds)

    alerts = []
    analyzed = 0
    best_below_threshold = state["weekly"].get("max_score_below_threshold", 0)

    for url in urls:
        if url in seen_list:
            continue

        seen_list.append(url)

        if is_whitelisted(url, whitelist):
            continue

        try:
            images, html = extract_page_images(url)
        except Exception:
            continue

        analyzed += 1
        sus_links = suspicious_links(html)

        found_for_url = False

        for img in images:
            try:
                b = download_bytes(img)
                pil = bytes_to_pil(b)
                gray = pil_to_gray_np(pil)
            except Exception:
                continue

            for ref in refs:
                score_hash = similarity_hash_percent(pil, ref)

                if score_hash < 40:
                    continue

                ref_des = None
                if ref["orb"]:
                    ref_des = np.array(ref["orb"], dtype=np.uint8)

                matches = orb_match(gray, ref_des)
                score_orb = min(100, (matches / 18) * 100)

                score = (score_hash * 0.6) + (score_orb * 0.4)

                if score < ALERT_THRESHOLD_PERCENT and score > best_below_threshold:
                    best_below_threshold = score

                if score >= ALERT_THRESHOLD_PERCENT:
                    alerts.append({
                        "page": url,
                        "product": ref["product_name"],
                        "product_url": ref["product_url"],
                        "image": img,
                        "score": score,
                        "links": sus_links
                    })
                    found_for_url = True
                    break

            if found_for_url:
                break

    state["weekly"]["analyzed"] += analyzed
    state["weekly"]["alerts"] += len(alerts)
    state["weekly"]["max_score_below_threshold"] = best_below_threshold

    maybe_send_test_weekly_report(state)

    print(f"Links analisados nesta execução: {analyzed}")
    print(f"Maior score abaixo do limiar nesta execução/semana: {best_below_threshold:.1f}%")
    print(f"Alertas gerados (>= {ALERT_THRESHOLD_PERCENT}%): {len(alerts)}")

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
    save_seen_state(state)

if __name__ == "__main__":
    main()
