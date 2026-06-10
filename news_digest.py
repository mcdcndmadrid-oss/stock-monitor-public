"""Resumen diario de noticias con impacto en mercados.
Ejecutado por GitHub Actions a las 07:30 y 15:30 (hora Madrid).
"""
import hashlib
import json
import os
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
MARKETAUX_API_KEY  = os.environ.get("MARKETAUX_API_KEY", "")
MARKETAUX_BASE     = "https://api.marketaux.com/v1/news/all"
DIGEST_STATE_FILE  = "digest_state.json"
MAX_ARTICLES       = 7
MAX_SEEN_HASHES    = 300

# ── Scoring de impacto en mercados ────────────────────────────────────────────
KEYWORDS: dict[str, int] = {
    # Política monetaria / macro — peso 3
    "inflation":        3, "cpi":             3, "pce":             3,
    "interest rate":    3, "rate hike":        3, "rate cut":        3,
    "monetary policy":  3, "federal reserve":  3, "fed ":            3,
    "ecb":              3, "boe":              3, "boj":             3,
    "central bank":     3, "recession":        3, "gdp":             3,
    "nonfarm":          3, "unemployment":     3, "jobs report":     3,
    # Geopolítica / energía — peso 2
    "war":              2, "conflict":         2, "sanctions":       2,
    "tariff":           2, "trade war":        2, "geopolit":        2,
    "oil":              2, "opec":             2, "crude":           2,
    "gas":              2, "energy crisis":    2,
    "china":            2, "russia":           2, "middle east":     2,
    "taiwan":           2, "ukraine":          2,
    # Mercados / empresas — peso 2
    "earnings":         2, "guidance":         2, "downgrade":       2,
    "upgrade":          2, "profit warning":   2, "outlook":         2,
    "s&p 500":          2, "nasdaq":           2, "dow jones":       2,
    "stock market":     2, "treasury":         2, "bond yield":      2,
    "dollar":           2, "euro":             2, "yen":             2,
    # Sectorial — peso 1
    "merger":           1, "acquisition":      1, "ipo":             1,
    "bankruptcy":       1, "default":          1, "debt ceiling":    1,
    "semiconductor":    1, "ai ":              1, "bitcoin":         1,
    "crypto":           1, "bank":             1, "tech":            1,
}

SENTIMENT_POSITIVE = {
    "surges", "jumps", "rallies", "rally", "gains", "rises", "climbs",
    "beats", "strong", "recovery", "growth", "record high", "optimism",
    "bullish", "boost", "soars", "rebound", "resilient", "upbeat",
}
SENTIMENT_NEGATIVE = {
    "falls", "drops", "slumps", "crash", "plunges", "tumbles", "sinks",
    "crisis", "weak", "misses", "cuts", "concern", "warning", "risk",
    "fears", "bearish", "recession", "default", "collapse", "turmoil",
    "selloff", "sell-off", "downturn", "contraction",
}

TOP_SOURCES = {
    "reuters", "bloomberg", "financial times", "ft.com", "wsj",
    "wall street journal", "cnbc", "the economist", "marketwatch",
    "barron's", "the ft", "ft",
}


# ── I/O ──────────────────────────────────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


# ── Obtención de noticias ────────────────────────────────────────────────────
def fetch_global_news(lookback_hours: int = 12) -> list[dict]:
    if not MARKETAUX_API_KEY:
        print("[DIGEST] MARKETAUX_API_KEY no configurada")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M")
    try:
        r = requests.get(
            MARKETAUX_BASE,
            params={
                "api_token":      MARKETAUX_API_KEY,
                "language":       "en",
                "published_after": cutoff,
                "sort":           "published_on",
                "limit":          50,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            print(f"[DIGEST] Marketaux error: {data['error']}")
            return []
        return data.get("data", [])
    except Exception as e:
        print(f"[DIGEST] Fetch fallido: {e}")
        return []


# ── Scoring y sentimiento ────────────────────────────────────────────────────
def score_article(title: str, source: str) -> int:
    t = title.lower()
    score = sum(w for kw, w in KEYWORDS.items() if kw in t)
    if source.lower() in TOP_SOURCES:
        score += 2
    return score


def impact_label(score: int) -> str:
    if score >= 7:
        return "🔴 Alto"
    if score >= 4:
        return "🟡 Medio"
    return "🟢 Bajo"


def sentiment_from_title(title: str) -> float:
    t = title.lower()
    pos = sum(1 for w in SENTIMENT_POSITIVE if w in t)
    neg = sum(1 for w in SENTIMENT_NEGATIVE if w in t)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 2)


def sentiment_from_entities(article: dict) -> float | None:
    entities = article.get("entities", [])
    scores = [float(e.get("sentiment_score", 0)) for e in entities if e.get("sentiment_score") is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


def sentiment_emoji(score: float) -> str:
    if score >= 0.2:
        return "🟢"
    if score <= -0.2:
        return "🔴"
    return "⚪"


# ── Selección de artículos ────────────────────────────────────────────────────
def select_articles(raw: list[dict], seen_hashes: set) -> list[dict]:
    seen_titles: set[str] = set()
    scored: list[tuple[int, dict]] = []

    for a in raw:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        h = _hash(title)
        if h in seen_hashes or title in seen_titles:
            continue
        seen_titles.add(title)

        src_raw = a.get("source")
        source  = src_raw if isinstance(src_raw, str) else (src_raw or {}).get("name", "")
        score = score_article(title, source)
        if score == 0:
            continue

        # Sentimiento: entidades de Marketaux > keywords del título
        sent = sentiment_from_entities(a)
        if sent is None:
            sent = sentiment_from_title(title)

        scored.append((score, {
            "title":     title,
            "source":    source,
            "url":       a.get("url", ""),
            "score":     score,
            "impact":    impact_label(score),
            "sentiment": sent,
            "_hash":     h,
        }))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [a for _, a in scored[:MAX_ARTICLES]]


# ── Formateo del mensaje Telegram ────────────────────────────────────────────
def format_digest(articles: list[dict], slot: str) -> str:
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # Sentimiento macro: media ponderada por score de impacto
    total_w = sum(a["score"] for a in articles)
    macro = sum(a["sentiment"] * a["score"] for a in articles) / total_w if total_w else 0.0
    m_emoji = sentiment_emoji(macro)
    m_label = "Positivo" if macro >= 0.2 else "Negativo" if macro <= -0.2 else "Neutral"

    lines = [
        f"📰 *{slot}*",
        f"_{now_str}_",
        f"Sentimiento global: {m_emoji} *{m_label}* ({macro:+.2f})",
        "────────────────────",
    ]

    for a in articles:
        s_emj = sentiment_emoji(a["sentiment"])
        src   = f" _{a['source']}_" if a["source"] else ""
        link  = f" [→]({a['url']})" if a["url"] else ""
        lines.append(f"{a['impact']} {s_emj}{src}")
        lines.append(f"*{a['title']}*{link}")
        lines.append("")

    return "\n".join(lines).strip()


def send_telegram(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=10,
    ).raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    now = datetime.now(timezone.utc)
    slot = "Apertura — 07:30 Madrid" if now.hour < 12 else "Cierre — 15:30 Madrid"
    print(f"[DIGEST] {now.isoformat()} | {slot}")

    state = load_json(DIGEST_STATE_FILE, {"seen_hashes": [], "last_run": None})
    if not isinstance(state, dict):
        state = {"seen_hashes": [], "last_run": None}

    # Normaliza el estado y garantiza que digest_state.json se cree/actualice
    # incluso cuando no haya noticias o no haya artículos nuevos relevantes.
    seen_hashes_list = [str(h) for h in state.get("seen_hashes", []) if h]
    seen_hashes_list = seen_hashes_list[-MAX_SEEN_HASHES:]
    seen_hashes = set(seen_hashes_list)

    state["seen_hashes"] = seen_hashes_list
    state.setdefault("last_run", None)
    state["last_checked"] = now.isoformat()

    raw = fetch_global_news(lookback_hours=12)
    if not raw:
        print("[DIGEST] Sin artículos")
        save_json(DIGEST_STATE_FILE, state)
        print("[DIGEST] Estado guardado")
        return

    articles = select_articles(raw, seen_hashes)
    if not articles:
        print("[DIGEST] Ningún artículo nuevo relevante")
        save_json(DIGEST_STATE_FILE, state)
        print("[DIGEST] Estado guardado")
        return

    msg = format_digest(articles, slot)
    send_telegram(msg)
    print(f"[DIGEST] Enviados {len(articles)} artículos")

    for article in articles:
        article_hash = article["_hash"]
        if article_hash not in seen_hashes:
            seen_hashes_list.append(article_hash)
            seen_hashes.add(article_hash)

    state["seen_hashes"] = seen_hashes_list[-MAX_SEEN_HASHES:]
    state["last_run"] = now.isoformat()
    save_json(DIGEST_STATE_FILE, state)
    print("[DIGEST] Estado guardado")


if __name__ == "__main__":
    main()