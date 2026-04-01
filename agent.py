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

# Score mínimo para começar a considerar internamente um match
INITIAL_HASH_FILTER = 52

# Score mínimo para enviar no e-mail de revisão manual
REVIEW_THRESHOLD_PERCENT = 93

# Quantidade de imagens por produto do WooCommerce usadas como referência
IMAGES_PER_PRODUCT = 3

# Quantidade máxima de imagens candidatas por página suspeita
MAX_IMAGES_PER_SUSPECT_PAGE = 4

# Quantidade máxima de páginas por execução
MAX_PAGES_PER_RUN = 15

# Atualização do cache das referências
CACHE_REFRESH_SECONDS = 48 * 60 * 60

# Relatório semanal
WEEKLY_REPORT_SECONDS = 7 * 24 * 60 * 60

# Filtros mínimos de imagem
MIN_IMAGE_SIDE = 220
MIN_IMAGE_AREA = 120000

# Timeout das requisições
REQUEST_TIMEOUT = 12

# Limites de ranking
TOP_MATCHES_LIMIT = 5
TOP_SCORES_LIMIT = 5

# Regras de penalização / coerência
GENERIC_MATCH_SPREAD_LIMIT = 3
GENERIC_MATCH_SPREAD_PENALTY = 8.0
NO_THEME_OVERLAP_PENALTY = 6.0
LOW_THEME_OVERLAP_PENALTY = 3.0

# Regras novas de aceitação
MIN_THEME_OVERLAP_FOR_NORMAL_MATCH = 1
RAW_SCORE_FOR_THEMELESS_MATCH = 72.0
DOMINANCE_MIN_DIFF = 5.0
AMBIGUOUS_MATCH_PENALTY = 7.0

# Configurações de robustez do e-mail
EMAIL_SEND_MAX_ATTEMPTS = 3
EMAIL_RETRY_DELAY_SECONDS = 8
MAX_PENDING_EMAILS = 20

EMAIL_DESTINATION = "guilhermefariadeangeli@gmail.com"

# =========================
# CONTROLES
# =========================

EMAIL_TESTE = True
RESETAR_CACHE = True
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
# PÁGINAS / URLS RUIDOSAS
# =========================

NOISY_PAGE_HINTS = [
    "/category/",
    "/categoria/",
    "/tag/",
    "/tags/",
    "/pin/",
    "/search",
    "/busca",
    "/noticias/",
    "/noticia/",
    "/blog/",
    "/news/",
]

NOISY_QUERY_HINTS = [
    "page=",
    "pagina=",
    "limite=",
    "sort=",
    "order=",
    "filter=",
]

# =========================
# FILTROS DE IMAGEM / LAYOUT
# =========================

NOISY_IMAGE_HINTS = [
    "logo",
    "site-logo",
    "brand",
    "branding",
    "header",
    "footer",
    "favicon",
    "icon",
    "avatar",
    "profile",
    "sprite",
    "placeholder",
    "banner",
    "ads",
    "pixel",
    "loader",
    "watermark",
    "gravatar",
]

THUMBNAIL_HINTS = [
    "thumb",
    "thumbnail",
    "small",
    "mini",
    "resize",
    "resized",
    "crop",
    "cropped",
    "fit-in",
    "fitin",
    "140x140",
    "150x150",
    "160x160",
    "180x180",
    "200x200",
    "220x220",
    "240x240",
    "250x250",
    "300x300",
    "_rs",
]

PRODUCT_POSITIVE_HINTS = [
    "product",
    "produto",
    "kit",
    "digital",
    "arquivo",
    "png",
    "printable",
    "mockup",
    "gallery",
    "woocommerce",
]

GENERIC_PRODUCT_TOKENS = {
    "kit",
    "digital",
    "arquivo",
    "arquivos",
    "png",
    "arte",
    "artes",
    "produto",
    "produtos",
    "papelaria",
    "mimo",
    "mimos",
    "caixa",
    "caixinhas",
    "sacolinha",
    "sacolinhas",
    "topper",
    "adesivo",
    "adesivos",
    "imprimir",
    "printable",
    "encadernação",
    "encadernacao",
    "combo",
    "estampas",
    "arquivo-digital",
    "embalagem",
    "embalagens",
    "illustration",
    "illustrations",
    "ilustracao",
    "ilustrações",
    "ilustracoes",
    "cute",
    "mod",
    "modelo",
    "personalizado",
    "personalizada",
    "personalizados",
    "personalizadas",
    "lembrancinha",
    "lembrancinhas",
    "festa",
    "festas",
    "papéis",
    "papeis",
    "digitais",
    "miolos",
    "miolo",
}

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

def safe_domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""

def domain_matches(domain: str, pattern: str) -> bool:
    return domain == pattern or domain.endswith("." + pattern)

def normalize_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def truncate_text(s: str, limit: int = 180) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."

def update_top(scores: list[float] | None, new_score: float, limit: int = TOP_SCORES_LIMIT) -> list[float]:
    values = list(scores or [])
    values.append(float(new_score))
    values = sorted(values, reverse=True)
    return values[:limit]

def format_scores(scores: list[float] | None) -> str:
    if not scores:
        return "Nenhum"
    return ", ".join(f"{s:.1f}%" for s in scores)

def get_ref_product_name(ref: dict) -> str:
    return ref.get("product") or ref.get("product_name") or "(sem nome)"

def get_ref_product_url(ref: dict) -> str:
    return ref.get("url") or ref.get("product_url") or ""

def get_product_theme_key(ref: dict) -> str:
    return normalize_text(get_ref_product_name(ref))

def tokenize_text(text: str) -> list[str]:
    return re.findall(r"[a-z0-9çáéíóúâêôãõ]+", normalize_text(text), flags=re.I)

def product_name_tokens(name: str) -> set[str]:
    tokens = tokenize_text(name)

    stop = {
        "de", "da", "do", "das", "dos", "para", "com", "sem", "em", "e", "a", "o", "os", "as",
        "mod", "modelo", "arquivo", "arquivos", "digital", "kit", "png", "arte", "artes",
        "produto", "produtos", "combo", "estampas", "papelaria", "mimos", "mimo", "caixa",
        "caixinhas", "sacolinha", "sacolinhas", "encadernação", "encadernacao", "adesivo",
        "adesivos", "printable", "imprimir", "natal", "pascoa", "páscoa", "dia", "dos",
        "papeis", "papéis", "digitais", "miolo", "miolos"
    }

    cleaned = set()
    for token in tokens:
        if len(token) < 4:
            continue
        if token in stop:
            continue
        if token in GENERIC_PRODUCT_TOKENS:
            continue
        cleaned.add(token)

    return cleaned

# =========================
# ESTADO SEMANAL
# =========================

def build_default_weekly() -> dict:
    return {
        "start": now(),
        "analyzed": 0,
        "alerts": 0,
        "max": 0.0,
        "top_scores": [],
        "top_matches": [],
    }

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

def is_noisy_page_url(url: str) -> tuple[bool, str]:
    low = url.lower()

    for hint in NOISY_PAGE_HINTS:
        if hint in low:
            return True, f"page_hint:{hint}"

    for hint in NOISY_QUERY_HINTS:
        if hint in low:
            return True, f"query_hint:{hint}"

    return False, ""

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
# EMAIL ROBUSTO
# =========================

def send_email(subject: str, body: str, max_attempts: int = EMAIL_SEND_MAX_ATTEMPTS) -> bool:
    log(f"Enviando e-mail: {subject}")

    if not EMAIL_HOST or not EMAIL_USER or not EMAIL_PASSWORD:
        log("ERRO E-MAIL: EMAIL_HOST / EMAIL_USER / EMAIL_PASSWORD não configurados.")
        return False

    last_error = None

    for attempt in range(1, max_attempts + 1):
        smtp_conn = None
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = EMAIL_USER
            msg["To"] = EMAIL_DESTINATION

            smtp_conn = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30)
            smtp_conn.ehlo()
            smtp_conn.starttls()
            smtp_conn.ehlo()
            smtp_conn.login(EMAIL_USER, EMAIL_PASSWORD)
            smtp_conn.sendmail(EMAIL_USER, [EMAIL_DESTINATION], msg.as_string())
            smtp_conn.quit()

            log("E-mail enviado com sucesso.")
            return True

        except smtplib.SMTPAuthenticationError as e:
            last_error = e
            log(f"ERRO SMTP AUTH na tentativa {attempt}/{max_attempts}: {e}")
            if smtp_conn:
                try:
                    smtp_conn.quit()
                except Exception:
                    pass
            break

        except smtplib.SMTPException as e:
            last_error = e
            log(f"ERRO SMTP na tentativa {attempt}/{max_attempts}: {e}")
            if smtp_conn:
                try:
                    smtp_conn.quit()
                except Exception:
                    pass

        except Exception as e:
            last_error = e
            log(f"ERRO AO ENVIAR E-MAIL na tentativa {attempt}/{max_attempts}: {e}")
            if smtp_conn:
                try:
                    smtp_conn.quit()
                except Exception:
                    pass

        if attempt < max_attempts:
            log(f"Aguardando {EMAIL_RETRY_DELAY_SECONDS}s antes de nova tentativa de envio.")
            time.sleep(EMAIL_RETRY_DELAY_SECONDS)

    log(f"Falha definitiva no envio do e-mail: {subject} | Último erro: {last_error}")
    return False

def build_pending_email(key: str, subject: str, body: str, kind: str) -> dict:
    return {
        "key": key,
        "subject": subject,
        "body": body,
        "kind": kind,
        "created_at": now(),
        "attempts": 0,
    }

def enqueue_pending_email(state: dict, key: str, subject: str, body: str, kind: str) -> None:
    pending = state.setdefault("pending_emails", [])

    for item in pending:
        if item.get("key") == key:
            log(f"E-mail pendente já existe na fila: {key}")
            return

    pending.append(build_pending_email(key, subject, body, kind))

    if len(pending) > MAX_PENDING_EMAILS:
        pending[:] = pending[-MAX_PENDING_EMAILS:]

    log(f"E-mail adicionado à fila de pendências: {key}")

def flush_pending_emails(state: dict) -> dict:
    pending = state.get("pending_emails", [])
    if not pending:
        return state

    log(f"Tentando reenviar {len(pending)} e-mail(s) pendente(s).")

    remaining = []
    for item in pending:
        subject = item.get("subject", "(sem assunto)")
        body = item.get("body", "")
        key = item.get("key", "")
        item["attempts"] = int(item.get("attempts", 0)) + 1

        ok = send_email(subject, body)
        if ok:
            log(f"E-mail pendente reenviado com sucesso: {key}")
        else:
            log(f"E-mail pendente permaneceu na fila: {key}")
            remaining.append(item)

    state["pending_emails"] = remaining
    return state

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
        return w >= MIN_IMAGE_SIDE and h >= MIN_IMAGE_SIDE and (w * h) >= MIN_IMAGE_AREA
    except Exception:
        return False

def aspect_ratio(img: Image.Image) -> float:
    w, h = img.size
    if h == 0 or w == 0:
        return 999.0
    return max(w / h, h / w)

def compute_hash_triplet(img: Image.Image):
    return imagehash.phash(img), imagehash.dhash(img), imagehash.whash(img)

def hash_distance_to_percent(distance: int) -> float:
    distance = max(0, min(64, distance))
    return (1 - (distance / 64.0)) * 100

def hash_score_from_triplets(img_hashes, ref_hashes) -> float:
    ph, dh, wh = img_hashes
    rph, rdh, rwh = ref_hashes

    d1 = ph - rph
    d2 = dh - rdh
    d3 = wh - rwh

    return (
        hash_distance_to_percent(d1)
        + hash_distance_to_percent(d2)
        + hash_distance_to_percent(d3)
    ) / 3.0

def resize_with_minimum(img: Image.Image, min_side: int = 256) -> Image.Image:
    w, h = img.size
    if min(w, h) >= min_side:
        return img

    if w <= 0 or h <= 0:
        return img

    scale = min_side / float(min(w, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return img.resize((new_w, new_h), Image.LANCZOS)

def center_crop(img: Image.Image, crop_ratio: float = 0.58) -> Image.Image:
    w, h = img.size
    new_w = max(1, int(w * crop_ratio))
    new_h = max(1, int(h * crop_ratio))

    left = max(0, (w - new_w) // 2)
    top = max(0, (h - new_h) // 2)
    right = min(w, left + new_w)
    bottom = min(h, top + new_h)

    return img.crop((left, top, right, bottom))

def quadrant_crops(img: Image.Image, crop_ratio: float = 0.55) -> list[Image.Image]:
    w, h = img.size
    cw = max(1, int(w * crop_ratio))
    ch = max(1, int(h * crop_ratio))

    crops = []
    positions = [
        (0, 0),
        (max(0, w - cw), 0),
        (0, max(0, h - ch)),
        (max(0, w - cw), max(0, h - ch)),
    ]

    for left, top in positions:
        right = min(w, left + cw)
        bottom = min(h, top + ch)
        crops.append(img.crop((left, top, right, bottom)))

    return crops

def build_image_hash_views(img: Image.Image) -> dict:
    img = resize_with_minimum(img, 256)

    whole = compute_hash_triplet(img)
    center = compute_hash_triplet(center_crop(img, 0.58))

    quads = []
    for q in quadrant_crops(img, 0.55):
        quads.append(compute_hash_triplet(q))

    return {
        "whole": whole,
        "center": center,
        "quads": quads,
    }

def composite_similarity_score(suspect_views: dict, ref_views: dict) -> tuple[float, float, float]:
    whole_score = hash_score_from_triplets(suspect_views["whole"], ref_views["whole"])
    center_score = hash_score_from_triplets(suspect_views["center"], ref_views["center"])

    quad_scores = []
    for s_q in suspect_views["quads"]:
        for r_q in ref_views["quads"]:
            quad_scores.append(hash_score_from_triplets(s_q, r_q))

    best_quad = max(quad_scores) if quad_scores else 0.0

    final_raw = (whole_score * 0.25) + (center_score * 0.55) + (best_quad * 0.20)
    return final_raw, whole_score, center_score

def url_has_thumbnail_hint(url: str) -> bool:
    low = url.lower()
    return any(hint in low for hint in THUMBNAIL_HINTS)

def is_probable_preview_or_thumbnail_url(url: str) -> bool:
    low = url.lower()

    if url_has_thumbnail_hint(low):
        return True

    if re.search(r"/\d{2,4}x\d{2,4}\b", low):
        return True

    if re.search(r"[_-]\d{2,4}x\d{2,4}\b", low):
        return True

    return False

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
        r.raise_for_status()
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
            src = i.get("src", "")
            if not src:
                continue

            try:
                img_bytes = download(src)
                pimg = pil_from_bytes(img_bytes)
                if not is_valid_pil_image(pimg):
                    continue

                views = build_image_hash_views(pimg)

                refs.append({
                    "product": p.get("name", "(sem nome)"),
                    "url": p.get("permalink", ""),
                    "whole_phash": str(views["whole"][0]),
                    "whole_dhash": str(views["whole"][1]),
                    "whole_whash": str(views["whole"][2]),
                    "center_phash": str(views["center"][0]),
                    "center_dhash": str(views["center"][1]),
                    "center_whash": str(views["center"][2]),
                    "quad_hashes": [
                        {
                            "phash": str(q[0]),
                            "dhash": str(q[1]),
                            "whash": str(q[2]),
                        }
                        for q in views["quads"]
                    ],
                })
            except Exception as e:
                log(f"Falha ao criar referência do produto '{p.get('name', '(sem nome)')}': {e}")

    log(f"Referências criadas: {len(refs)}")
    return refs

def prepare_refs(refs: list[dict]) -> list[dict]:
    prepared = []
    for ref in refs:
        try:
            quad_views = []
            for q in ref.get("quad_hashes", []):
                quad_views.append((
                    imagehash.hex_to_hash(q["phash"]),
                    imagehash.hex_to_hash(q["dhash"]),
                    imagehash.hex_to_hash(q["whash"]),
                ))

            token_set = product_name_tokens(ref.get("product", ""))

            item = {
                **ref,
                "_tokens": token_set,
                "_theme_key": get_product_theme_key(ref),
                "_token_count": len(token_set),
                "_views": {
                    "whole": (
                        imagehash.hex_to_hash(ref["whole_phash"]),
                        imagehash.hex_to_hash(ref["whole_dhash"]),
                        imagehash.hex_to_hash(ref["whole_whash"]),
                    ),
                    "center": (
                        imagehash.hex_to_hash(ref["center_phash"]),
                        imagehash.hex_to_hash(ref["center_dhash"]),
                        imagehash.hex_to_hash(ref["center_whash"]),
                    ),
                    "quads": quad_views,
                },
            }
            prepared.append(item)
        except Exception:
            continue
    return prepared

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

    return prepare_refs(refs)

# =========================
# EXTRAÇÃO E FILTRO DE IMAGENS
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

def image_candidate_metadata(img_tag, final_url: str) -> dict | None:
    candidates = [
        img_tag.get("src"),
        img_tag.get("data-src"),
        img_tag.get("data-lazy-src"),
        img_tag.get("data-original"),
        img_tag.get("data-image"),
        extract_first_from_srcset(img_tag.get("srcset")),
        extract_first_from_srcset(img_tag.get("data-srcset")),
    ]

    src = None
    for c in candidates:
        if c:
            src = urljoin(final_url, c)
            break

    if not src:
        return None

    attrs_joined = " ".join([
        img_tag.get("alt", "") or "",
        img_tag.get("title", "") or "",
        " ".join(img_tag.get("class", []) if isinstance(img_tag.get("class"), list) else [img_tag.get("class", "")]),
        img_tag.get("id", "") or "",
        img_tag.get("src", "") or "",
        img_tag.get("data-src", "") or "",
    ]).lower()

    parent_text = ""
    parent = img_tag.parent
    if parent:
        parent_text = " ".join([
            getattr(parent, "name", "") or "",
            parent.get("class", "") if not isinstance(parent.get("class"), list) else " ".join(parent.get("class")),
            parent.get("id", "") or "",
        ]).lower()

    width = None
    height = None
    try:
        width = int(img_tag.get("width")) if img_tag.get("width") else None
    except Exception:
        width = None
    try:
        height = int(img_tag.get("height")) if img_tag.get("height") else None
    except Exception:
        height = None

    return {
        "url": src,
        "attrs": attrs_joined,
        "parent": parent_text,
        "width": width,
        "height": height,
    }

def is_noisy_image_candidate(meta: dict) -> tuple[bool, str]:
    url = (meta.get("url") or "").lower()
    attrs = (meta.get("attrs") or "").lower()
    parent = (meta.get("parent") or "").lower()
    joined = f"{url} {attrs} {parent}"

    for hint in NOISY_IMAGE_HINTS:
        if hint in joined:
            return True, f"hint:{hint}"

    if any(x in url for x in [".svg", "favicon", "gravatar"]):
        return True, "url_noise"

    if is_probable_preview_or_thumbnail_url(url):
        return True, "thumbnail_hint"

    width = meta.get("width")
    height = meta.get("height")
    if width and height:
        if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
            return True, "tag_too_small"
        if (width * height) < MIN_IMAGE_AREA:
            return True, "tag_too_small_area"
        ratio = max(width / max(height, 1), height / max(width, 1))
        if ratio >= 4.5:
            return True, "tag_extreme_ratio"

    return False, ""

def score_candidate_priority(meta: dict) -> int:
    score = 0
    joined = f"{meta.get('url', '')} {meta.get('attrs', '')} {meta.get('parent', '')}".lower()

    for hint in PRODUCT_POSITIVE_HINTS:
        if hint in joined:
            score += 2

    if "product" in joined:
        score += 2
    if "gallery" in joined:
        score += 2
    if "woocommerce" in joined:
        score += 2
    if "main" in joined:
        score += 1
    if "featured" in joined:
        score += 1

    return score

def extract_page_context(soup: BeautifulSoup, final_url: str) -> dict:
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    h1_tag = soup.find("h1")
    h1 = normalize_text(h1_tag.get_text(" ", strip=True) if h1_tag else "")
    og_title_tag = soup.find("meta", property="og:title")
    og_title = normalize_text(og_title_tag.get("content", "") if og_title_tag else "")

    text_chunks = [title, h1, og_title, normalize_text(final_url)]
    text_joined = " | ".join([t for t in text_chunks if t])

    return {
        "title": title,
        "h1": h1,
        "og_title": og_title,
        "text": text_joined,
    }

def extract_images(url: str):
    try:
        r, content_type = fetch_page(url)
    except Exception as e:
        return [], "", "fetch_error", {}, {"extract_error": str(e)}

    final_url = r.url

    if is_direct_image_url(final_url, content_type):
        direct_meta = {
            "url": final_url,
            "attrs": "",
            "parent": "",
            "width": None,
            "height": None,
        }
        return [direct_meta], "", content_type, {"title": "", "h1": "", "og_title": "", "text": normalize_text(final_url)}, {}

    if "html" not in content_type and "xml" not in content_type and content_type != "":
        return [], r.text if hasattr(r, "text") else "", content_type, {}, {"non_html_content_type": content_type}

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    page_context = extract_page_context(soup, final_url)

    candidates = []

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        candidates.append({
            "url": urljoin(final_url, og["content"]),
            "attrs": "og:image",
            "parent": "meta",
            "width": None,
            "height": None,
        })

    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        candidates.append({
            "url": urljoin(final_url, tw["content"]),
            "attrs": "twitter:image",
            "parent": "meta",
            "width": None,
            "height": None,
        })

    for img in soup.find_all("img"):
        meta = image_candidate_metadata(img, final_url)
        if meta:
            candidates.append(meta)

    unique_by_url = {}
    discard_reasons = {
        "duplicate_url": 0,
        "noisy_hint": 0,
        "thumbnail_hint": 0,
    }

    for meta in candidates:
        u = meta["url"]
        if u in unique_by_url:
            discard_reasons["duplicate_url"] += 1
            continue

        noisy, reason = is_noisy_image_candidate(meta)
        if noisy:
            if reason == "thumbnail_hint":
                discard_reasons["thumbnail_hint"] += 1
            else:
                discard_reasons["noisy_hint"] += 1
            continue

        unique_by_url[u] = meta

    cleaned = list(unique_by_url.values())
    cleaned.sort(key=score_candidate_priority, reverse=True)
    cleaned = cleaned[:MAX_IMAGES_PER_SUSPECT_PAGE]

    return cleaned, html, content_type or "text/html", page_context, discard_reasons

def suspicious_links(text: str) -> list[str]:
    return [
        l for l in re.findall(r"https?://\S+", text)
        if any(x in l.lower() for x in ["mega", "drive", "telegram", "t.me"])
    ]

# =========================
# TEXTO / CONTEXTO
# =========================

def page_specific_token_overlap(page_context: dict, ref: dict) -> int:
    text = normalize_text(page_context.get("text", ""))
    ref_tokens = ref.get("_tokens", set())

    if not ref_tokens:
        return 0

    overlap = 0
    for token in ref_tokens:
        if token in text:
            overlap += 1

    return overlap

def adjusted_confidence(raw_score: float, page_context: dict, ref: dict) -> tuple[float, int, float]:
    overlap = page_specific_token_overlap(page_context, ref)

    boost = 0.0
    penalty = 0.0

    if overlap >= 1:
        boost += min(overlap * 1.0, 3.0)
    elif ref.get("_tokens"):
        penalty += NO_THEME_OVERLAP_PENALTY
    else:
        penalty += LOW_THEME_OVERLAP_PENALTY

    if ref.get("_token_count", 0) == 0:
        penalty += 2.0

    adjusted = raw_score + boost - penalty
    adjusted = max(0.0, min(100.0, adjusted))
    return adjusted, overlap, penalty

def passes_minimum_coherence_gate(raw_score: float, theme_overlap: int) -> bool:
    if theme_overlap >= MIN_THEME_OVERLAP_FOR_NORMAL_MATCH:
        return True
    return raw_score >= RAW_SCORE_FOR_THEMELESS_MATCH

def dominance_penalty_for_ranked_results(sorted_results: list[dict]) -> float:
    if len(sorted_results) < 2:
        return 0.0

    best_score = float(sorted_results[0]["adjusted_base"])
    second_score = float(sorted_results[1]["adjusted_base"])
    diff = best_score - second_score

    if diff < DOMINANCE_MIN_DIFF:
        return AMBIGUOUS_MATCH_PENALTY
    return 0.0

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

    if "top" in weekly and not weekly.get("top_scores"):
        weekly["top_scores"] = weekly["top"]

    pending_emails = state.get("pending_emails")
    if pending_emails is None:
        pending_emails = []

    return {"seen": seen, "weekly": weekly, "pending_emails": pending_emails}

def save_state(state: dict) -> None:
    save_json(SEEN_FILE, state)

# =========================
# CONSOLIDAÇÃO
# =========================

def match_key(item: dict) -> tuple:
    return (
        item.get("page", ""),
        item.get("image", ""),
        item.get("product_url", "") or item.get("product", ""),
    )

def merge_match(existing: list[dict], new_match: dict, limit: int = TOP_MATCHES_LIMIT) -> list[dict]:
    grouped = {}
    for item in list(existing or []) + [new_match]:
        key = match_key(item)
        current = grouped.get(key)
        if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
            grouped[key] = item

    dedup = sorted(grouped.values(), key=lambda x: float(x.get("score", 0)), reverse=True)
    return dedup[:limit]

def sort_and_trim_matches(matches: list[dict], limit: int = TOP_MATCHES_LIMIT) -> list[dict]:
    grouped = {}
    for item in matches or []:
        key = match_key(item)
        current = grouped.get(key)
        if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
            grouped[key] = item

    out = sorted(grouped.values(), key=lambda x: float(x.get("score", 0)), reverse=True)
    return out[:limit]

def merge_alert(existing: list[dict], new_alert: dict) -> list[dict]:
    grouped = {}
    for item in list(existing or []) + [new_alert]:
        key = match_key(item)
        current = grouped.get(key)
        if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
            merged_links = sorted(set((current or {}).get("links", []) + item.get("links", [])))
            grouped[key] = {
                **item,
                "links": merged_links
            }

    return sorted(grouped.values(), key=lambda x: float(x.get("score", 0)), reverse=True)

# =========================
# RELATÓRIOS
# =========================

def format_match_list(matches: list[dict]) -> str:
    if not matches:
        return "Nenhum caso relevante registrado."

    lines = []
    for idx, m in enumerate(matches, start=1):
        lines.append(f"{idx}. Score ajustado: {float(m.get('score', 0)):.1f}%")
        lines.append(f"   Score bruto composto: {float(m.get('raw_score', 0)):.1f}%")
        lines.append(f"   Score imagem inteira: {float(m.get('whole_score', 0)):.1f}%")
        lines.append(f"   Score recorte central: {float(m.get('center_score', 0)):.1f}%")
        lines.append(f"   Sobreposição temática: {int(m.get('theme_overlap', 0))}")
        lines.append(f"   Penalização temática: {float(m.get('theme_penalty', 0)):.1f}")
        lines.append(f"   Penalização genérica: {float(m.get('generic_penalty', 0)):.1f}")
        lines.append(f"   Penalização por ambiguidade: {float(m.get('dominance_penalty', 0)):.1f}")
        lines.append(f"   Página: {m.get('page', '')}")
        lines.append(f"   Produto de referência: {m.get('product', '')}")
        lines.append(f"   Seu produto: {m.get('product_url', '')}")
        lines.append(f"   Imagem analisada: {m.get('image', '')}")
        if m.get("page_title"):
            lines.append(f"   Contexto da página: {truncate_text(m.get('page_title', ''))}")
    return "\n".join(lines)

def build_report_body(title: str, weekly: dict) -> str:
    analyzed = weekly.get("analyzed", 0)
    alerts = weekly.get("alerts", 0)
    best = float(weekly.get("max", 0.0))
    top_scores = weekly.get("top_scores", [])
    top_matches = weekly.get("top_matches", [])

    body = (
        f"{title}\n\n"
        f"Resumo:\n"
        f"- Links analisados no período: {analyzed}\n"
        f"- Casos enviados para revisão manual: {alerts}\n"
        f"- Maior score ajustado observado: {best:.1f}%\n"
        f"- Top scores ajustados: {format_scores(top_scores)}\n\n"
        f"Observação importante:\n"
        f"Este agente faz uma triagem automática de semelhança visual. "
        f"Os resultados abaixo representam candidatos para revisão manual e não confirmação automática de cópia.\n\n"
        f"Top matches detalhados:\n{format_match_list(top_matches)}\n"
    )

    return body

def maybe_send_test_report(state: dict) -> None:
    if not EMAIL_TESTE:
        return

    subject = "Relatório Semanal - Monitoramento (TESTE)"
    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento (TESTE)",
        state["weekly"],
    ) + "\nEste é um envio de teste.\nDepois do teste, volte EMAIL_TESTE para False no arquivo agent.py.\n"

    ok = send_email(subject, body)
    if not ok:
        log("Falha no envio do relatório de teste. O processo continuará sem interrupção.")

def maybe_send_weekly_report(state: dict) -> dict:
    if now() - state["weekly"]["start"] < WEEKLY_REPORT_SECONDS:
        return state

    subject = "Relatório Semanal - Monitoramento"
    body = build_report_body(
        "Relatório Semanal do Agente de Monitoramento",
        state["weekly"],
    )

    weekly_key = f"weekly:{state['weekly'].get('start', 0)}"

    ok = send_email(subject, body)
    if ok:
        state["weekly"] = build_default_weekly()
        return state

    log("Falha no envio do relatório semanal. O relatório permanecerá acumulado para nova tentativa.")
    enqueue_pending_email(state, weekly_key, subject, body, "weekly")
    return state

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
# MAIN
# =========================

def main():
    log("START")

    feeds = load_lines(FEEDS_FILE)
    whitelist = load_lines(WHITELIST_FILE)
    state = load_state()

    # tenta reenviar pendências logo no começo
    state = flush_pending_emails(state)

    log(f"Feeds carregados: {len(feeds)}")
    log(f"Whitelist entries: {len(whitelist)}")
    log(f"Links já vistos (cache): {len(state['seen'])}")
    log(f"E-mails pendentes na fila: {len(state.get('pending_emails', []))}")

    refs = load_cache()
    urls = read_rss(feeds)

    weekly = state["weekly"]
    best = float(weekly.get("max", 0.0))
    top_scores = weekly.get("top_scores", [])
    top_matches = weekly.get("top_matches", [])

    analyzed = 0
    pages_with_images = 0
    image_candidates_total = 0
    image_downloaded_ok = 0
    image_comparisons = 0
    alerts = []
    noisy_skipped = 0
    noisy_page_skipped = 0
    already_seen_skipped = 0
    whitelist_skipped = 0
    pages_failed = 0
    pages_without_images = 0

    discard_stats = {
        "download_error": 0,
        "duplicate_url": 0,
        "extreme_ratio": 0,
        "invalid_image": 0,
        "noisy_hint": 0,
        "thumbnail_hint": 0,
        "too_small": 0,
        "too_small_area": 0,
        "generic_penalty_applied": 0,
        "theme_penalty_applied": 0,
        "dominance_penalty_applied": 0,
        "coherence_gate_rejected": 0,
    }

    for idx, url in enumerate(urls[:MAX_PAGES_PER_RUN], start=1):
        log(f"Processando página {idx}/{min(len(urls), MAX_PAGES_PER_RUN)}: {url}")

        if url in state["seen"]:
            already_seen_skipped += 1
            continue

        if is_whitelisted(url, whitelist):
            whitelist_skipped += 1
            continue

        if is_noisy_domain(url):
            noisy_skipped += 1
            continue

        noisy_page, noisy_page_reason = is_noisy_page_url(url)
        if noisy_page:
            noisy_page_skipped += 1
            log(f"Página ignorada por perfil ruidoso: {noisy_page_reason}")
            continue

        try:
            imgs_meta, html, content_type, page_context, local_discards = extract_images(url)
        except Exception as e:
            pages_failed += 1
            log(f"Erro ao extrair imagens da página: {url} | {e}")
            continue

        if content_type == "fetch_error":
            pages_failed += 1
            log(f"Falha de fetch: {url}")
            continue

        for k, v in (local_discards or {}).items():
            discard_stats[k] = discard_stats.get(k, 0) + v

        state["seen"].append(url)
        analyzed += 1

        if not imgs_meta:
            pages_without_images += 1
            log("Nenhuma imagem candidata relevante extraída da página.")
            continue

        pages_with_images += 1
        image_candidates_total += len(imgs_meta)

        page_best_score = 0.0

        for meta in imgs_meta:
            im = meta["url"]

            if is_probable_preview_or_thumbnail_url(im):
                discard_stats["thumbnail_hint"] += 1
                continue

            try:
                img_bytes = download(im)
                pimg = pil_from_bytes(img_bytes)
                image_downloaded_ok += 1
            except Exception:
                discard_stats["download_error"] += 1
                continue

            w, h = pimg.size
            area = w * h

            if w < MIN_IMAGE_SIDE or h < MIN_IMAGE_SIDE:
                discard_stats["too_small"] += 1
                continue

            if area < MIN_IMAGE_AREA:
                discard_stats["too_small_area"] += 1
                continue

            if not is_valid_pil_image(pimg):
                discard_stats["invalid_image"] += 1
                continue

            ar = aspect_ratio(pimg)
            if ar >= 4.5:
                discard_stats["extreme_ratio"] += 1
                continue

            suspect_views = build_image_hash_views(pimg)

            per_ref_results = []

            for r in refs:
                image_comparisons += 1

                try:
                    raw_score, whole_score, center_score = composite_similarity_score(
                        suspect_views,
                        r["_views"]
                    )
                    adjusted_base, theme_overlap, theme_penalty = adjusted_confidence(raw_score, page_context, r)
                except Exception:
                    continue

                per_ref_results.append({
                    "ref": r,
                    "raw_score": raw_score,
                    "whole_score": whole_score,
                    "center_score": center_score,
                    "adjusted_base": adjusted_base,
                    "theme_overlap": theme_overlap,
                    "theme_penalty": theme_penalty,
                })

            if not per_ref_results:
                continue

            per_ref_results.sort(key=lambda x: float(x["adjusted_base"]), reverse=True)

            strong_results = [x for x in per_ref_results if x["adjusted_base"] >= INITIAL_HASH_FILTER]
            strong_theme_keys = set(
                x["ref"].get("_theme_key", "")
                for x in strong_results[:8]
                if x["ref"].get("_theme_key", "")
            )

            generic_penalty = 0.0
            if len(strong_theme_keys) >= GENERIC_MATCH_SPREAD_LIMIT:
                generic_penalty = GENERIC_MATCH_SPREAD_PENALTY
                discard_stats["generic_penalty_applied"] += 1

            dominance_penalty = dominance_penalty_for_ranked_results(per_ref_results)
            if dominance_penalty > 0:
                discard_stats["dominance_penalty_applied"] += 1

            for item in per_ref_results:
                r = item["ref"]
                raw_score = item["raw_score"]
                whole_score = item["whole_score"]
                center_score = item["center_score"]
                theme_overlap = item["theme_overlap"]
                theme_penalty = item["theme_penalty"]

                if theme_penalty > 0:
                    discard_stats["theme_penalty_applied"] += 1

                if not passes_minimum_coherence_gate(raw_score, theme_overlap):
                    discard_stats["coherence_gate_rejected"] += 1
                    continue

                final_score = max(
                    0.0,
                    min(100.0, item["adjusted_base"] - generic_penalty - dominance_penalty)
                )

                if final_score > page_best_score:
                    page_best_score = final_score

                best = max(best, final_score)
                top_scores = update_top(top_scores, final_score, limit=TOP_SCORES_LIMIT)

                if final_score < INITIAL_HASH_FILTER:
                    continue

                match_item = {
                    "page": url,
                    "product": get_ref_product_name(r),
                    "product_url": get_ref_product_url(r),
                    "image": im,
                    "score": final_score,
                    "raw_score": raw_score,
                    "whole_score": whole_score,
                    "center_score": center_score,
                    "theme_overlap": theme_overlap,
                    "theme_penalty": theme_penalty,
                    "generic_penalty": generic_penalty,
                    "dominance_penalty": dominance_penalty,
                    "page_title": page_context.get("text", ""),
                }
                top_matches = merge_match(top_matches, match_item, limit=TOP_MATCHES_LIMIT)

                if final_score >= REVIEW_THRESHOLD_PERCENT:
                    alert_item = {
                        "page": url,
                        "product": get_ref_product_name(r),
                        "product_url": get_ref_product_url(r),
                        "image": im,
                        "score": final_score,
                        "raw_score": raw_score,
                        "whole_score": whole_score,
                        "center_score": center_score,
                        "theme_overlap": theme_overlap,
                        "theme_penalty": theme_penalty,
                        "generic_penalty": generic_penalty,
                        "dominance_penalty": dominance_penalty,
                        "page_title": page_context.get("text", ""),
                        "links": suspicious_links(html),
                    }
                    alerts = merge_alert(alerts, alert_item)

        log(
            f"Parcial -> páginas com imagens: {pages_with_images}, "
            f"candidatas: {image_candidates_total}, "
            f"comparações: {image_comparisons}, "
            f"melhor score ajustado: {best:.1f}%, "
            f"melhor score da página: {page_best_score:.1f}%"
        )

    weekly["analyzed"] += analyzed
    weekly["alerts"] += len(alerts)
    weekly["max"] = max(float(weekly.get("max", 0.0)), best)
    weekly["top_scores"] = sorted(
        list(weekly.get("top_scores", [])) + top_scores,
        reverse=True
    )[:TOP_SCORES_LIMIT]
    weekly["top_matches"] = sort_and_trim_matches(
        list(weekly.get("top_matches", [])) + top_matches,
        TOP_MATCHES_LIMIT
    )

    log(f"Páginas analisadas: {analyzed}")
    log(f"Páginas ignoradas por domínio ruidoso: {noisy_skipped}")
    log(f"Páginas ignoradas por perfil de página ruidosa: {noisy_page_skipped}")
    log(f"Páginas ignoradas por whitelist: {whitelist_skipped}")
    log(f"Páginas já vistas: {already_seen_skipped}")
    log(f"Páginas com falha: {pages_failed}")
    log(f"Páginas sem imagens relevantes: {pages_without_images}")
    log(f"Páginas com imagens relevantes: {pages_with_images}")
    log(f"Imagens candidatas consideradas: {image_candidates_total}")
    log(f"Imagens baixadas com sucesso: {image_downloaded_ok}")
    log(f"Comparações de imagem realizadas: {image_comparisons}")
    log(f"Maior score ajustado nesta execução/semana: {best:.1f}%")
    log(f"Top scores ajustados da semana: {format_scores(weekly['top_scores'])}")
    log("Motivos de descarte de imagens:")
    for k, v in sorted(discard_stats.items(), key=lambda x: x[0]):
        log(f"- {k}: {v}")

    log("Top matches detalhados:")
    for line in format_match_list(weekly["top_matches"]).splitlines():
        log(line)

    log(f"Casos enviados para revisão manual (>= {REVIEW_THRESHOLD_PERCENT}%): {len(alerts)}")

    maybe_send_test_report(state)

    if alerts:
        body = (
            "Candidatos com maior semelhança para revisão manual\n\n"
            "Observação: estes casos representam triagem automática. "
            "Revise manualmente antes de considerar qualquer ação.\n\n"
        )

        for a in alerts:
            body += f"Página suspeita: {a['page']}\n"
            body += f"Produto de referência: {a['product']}\n"
            body += f"Seu produto: {a['product_url']}\n"
            body += f"Imagem suspeita: {a['image']}\n"
            body += f"Score ajustado: {a['score']:.1f}%\n"
            body += f"Score bruto composto: {a['raw_score']:.1f}%\n"
            body += f"Score imagem inteira: {a['whole_score']:.1f}%\n"
            body += f"Score recorte central: {a['center_score']:.1f}%\n"
            body += f"Sobreposição temática: {a['theme_overlap']}\n"
            body += f"Penalização temática: {a['theme_penalty']:.1f}\n"
            body += f"Penalização genérica: {a['generic_penalty']:.1f}\n"
            body += f"Penalização por ambiguidade: {a['dominance_penalty']:.1f}\n"
            if a.get("page_title"):
                body += f"Contexto da página: {truncate_text(a['page_title'])}\n"
            if a["links"]:
                body += "Links externos potencialmente suspeitos encontrados na página:\n"
                for l in a["links"]:
                    body += f"- {l}\n"
            body += "\n"

        alert_key = f"alerts:{now()}:{len(alerts)}"
        ok = send_email("Candidatos para Revisão Manual", body)
        if not ok:
            log("Falha no envio do e-mail de alertas. O conteúdo será colocado na fila de pendências.")
            enqueue_pending_email(state, alert_key, "Candidatos para Revisão Manual", body, "alerts")

    state = maybe_send_weekly_report(state)
    save_state(state)

if __name__ == "__main__":
    main()
