import os
import hashlib
import requests

# ==============================
# CONFIG
# ==============================

REVIEW_THRESHOLD_HIGH = 80   # suspeita forte
REVIEW_THRESHOLD_LOW = 60    # suspeita moderada

# ==============================
# WORKER INTEGRATION
# ==============================

def make_case_id(suspect_page_url, product_url, suspect_image_url):
    base = f"{suspect_page_url}|{product_url}|{suspect_image_url}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def build_case_payload(suspects):
    cases = []

    for item in suspects:
        case_id = item.get("id") or make_case_id(
            item.get("suspect_page_url", ""),
            item.get("product_url", ""),
            item.get("suspect_image_url", "")
        )

        cases.append({
            "id": case_id,
            "suspect_image_url": item.get("suspect_image_url"),
            "reference_image_url": item.get("reference_image_url"),
            "suspect_page_url": item.get("suspect_page_url"),
            "product_url": item.get("product_url"),
            "product_name": item.get("product_name"),
            "score": item.get("score"),
            "score_percent": item.get("score"),
            "match_type": item.get("match_type"),
            "source": item.get("source"),
            "notes": item.get("notes"),
            "severity": item.get("severity", "medium"),
            "status": "pending"
        })

    return {"cases": cases}


def send_cases_to_worker(suspects):
    url = os.getenv("WORKER_INGEST_URL")
    token = os.getenv("WORKER_INGEST_TOKEN")

    if not url:
        print("WORKER_INGEST_URL não configurado. Pulando envio.")
        return

    if not token:
        print("WORKER_INGEST_TOKEN não configurado. Pulando envio.")
        return

    payload = build_case_payload(suspects)

    if not payload["cases"]:
        print("Nenhum caso para enviar ao Worker.")
        return

    response = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "X-Ingest-Token": token
        },
        json=payload,
        timeout=30
    )

    print("Worker ingest status:", response.status_code)
    print("Worker ingest body:", response.text)


# ==============================
# SIMULAÇÃO DE RESULTADOS (SUBSTITUA PELO SEU PIPELINE REAL)
# ==============================

def gerar_suspeitos_fake():
    """
    SUBSTITUA essa função pelo seu pipeline real.
    Aqui é só para garantir que o fluxo funcione.
    """

    return [
        {
            "suspect_page_url": "https://site-suspeito.com/item1",
            "product_url": "https://seusite.com/produto1",
            "suspect_image_url": "https://via.placeholder.com/400",
            "reference_image_url": "https://via.placeholder.com/400",
            "product_name": "Produto Teste",
            "score": 64,
            "match_type": "phash",
            "source": "teste",
        },
        {
            "suspect_page_url": "https://site-suspeito.com/item2",
            "product_url": "https://seusite.com/produto2",
            "suspect_image_url": "https://via.placeholder.com/400",
            "reference_image_url": "https://via.placeholder.com/400",
            "product_name": "Produto Forte",
            "score": 85,
            "match_type": "phash",
            "source": "teste",
        }
    ]


# ==============================
# MAIN
# ==============================

def main():
    suspects_raw = gerar_suspeitos_fake()

    alerts = []

    for item in suspects_raw:
        score = item.get("score", 0)

        if score >= REVIEW_THRESHOLD_HIGH:
            item["severity"] = "high"
            alerts.append(item)

        elif score >= REVIEW_THRESHOLD_LOW:
            item["severity"] = "medium"
            alerts.append(item)

    print(f"Casos enviados para revisão manual: {len(alerts)}")

    send_cases_to_worker(alerts)


if __name__ == "__main__":
    main()
