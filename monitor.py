import hashlib
import json
import os
import sys
import time
import requests
import anthropic
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_API_KEY"]
MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY", "")

TD_BASE = "https://api.twelvedata.com"
MARKETAUX_BASE = "https://api.marketaux.com/v1/news/all"
YAHOO_SUFFIXES = {".MC", ".PA", ".AS", ".DE", ".L", ".MI", ".SW", ".BR", ".LS", ".OL", ".ST", ".HE", ".CO"}
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
PORTFOLIO_FILE = "portfolio.json"
STATE_FILE = "portfolio_state.json"
ALERTS_LOG_FILE = "alerts_log.json"
SWING_HISTORY_FILE = "swing_history.json"
MAX_ALERTS_LOG = 200
MAX_SWING_HISTORY_DAYS = 250
NEWS_LOOKBACK_HOURS = 24
URGENT_NEWS_LOOKBACK_MINUTES = 20
URGENT_NEWS_INTERVAL_MINUTES = 15
MAX_URGENT_NEWS_SEEN = 300
URGENT_NEWS_KEYWORDS: dict[str, int] = {
    # Resultados / guidance
    "earnings": 3, "results": 2, "revenue": 2, "profit": 2,
    "guidance": 4, "outlook": 3, "forecast": 2, "profit warning": 5,
    "misses": 3, "beats": 2, "cuts forecast": 4, "raises forecast": 3,
    # Dirección / gobierno corporativo
    "ceo": 3, "cfo": 3, "resigns": 4, "steps down": 4, "appointed": 2,
    # Legal / regulatorio
    "lawsuit": 4, "sues": 3, "investigation": 4, "probe": 3, "sec": 4,
    "doj": 4, "ftc": 4, "antitrust": 4, "regulator": 3, "fine": 3,
    "sanction": 4, "ban": 4,
    # M&A / balance
    "merger": 4, "acquisition": 4, "takeover": 4, "buyout": 4,
    "bankruptcy": 5, "default": 5, "debt": 3, "restructuring": 4,
    "layoffs": 3, "job cuts": 3,
    # Producto / seguridad / trading halt
    "recall": 4, "halts": 4, "halted": 4, "suspends": 4,
    "cyberattack": 4, "data breach": 4,
    # Rating / movimientos fuertes
    "downgrade": 4, "upgrade": 3, "price target": 2,
    "plunges": 4, "tumbles": 4, "crashes": 5, "slumps": 4,
    "surges": 3, "jumps": 3, "soars": 4,
}


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def calculate_ma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


def calculate_ema(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calculate_ema_series(closes: list[float], period: int) -> list[float | None]:
    """Serie EMA del mismo length que closes; los primeros (period-1) valores son None."""
    if len(closes) < period:
        return [None] * len(closes)
    result: list[float | None] = [None] * len(closes)
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result[period - 1] = round(ema, 4)
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = round(ema, 4)
    return result


def build_swing_history(values: list[dict], limit: int = MAX_SWING_HISTORY_DAYS) -> list[dict]:
    """Construye historial OHLCV + EMA20/50 desde registros Twelve Data (cronológicos, más antiguo primero)."""
    valid = [d for d in values
             if d.get("close") is not None
             and d.get("high")  is not None
             and d.get("low")   is not None]
    if not valid:
        return []
    closes = [float(d["close"]) for d in valid]
    ema20_series = calculate_ema_series(closes, 20)
    ema50_series = calculate_ema_series(closes, 50)
    history: list[dict] = []
    start = max(0, len(valid) - limit)
    for i, d in enumerate(valid[start:], start=start):
        close = float(d["close"])
        history.append({
            "date":   d.get("datetime") or d.get("date") or "",
            "open":   float(d.get("open") or close),
            "high":   float(d["high"]),
            "low":    float(d["low"]),
            "close":  close,
            "volume": int(float(d.get("volume") or 0)),
            "ema20":  ema20_series[i],
            "ema50":  ema50_series[i],
        })
    return history


def build_swing_history_from_rows(rows: list[dict], limit: int = MAX_SWING_HISTORY_DAYS) -> list[dict]:
    """Construye historial OHLCV + EMA20/50 desde rows de Yahoo Finance (ya alineados, con campo 'date')."""
    if not rows:
        return []
    closes = [r["close"] for r in rows]
    ema20_series = calculate_ema_series(closes, 20)
    ema50_series = calculate_ema_series(closes, 50)
    history: list[dict] = []
    start = max(0, len(rows) - limit)
    for i, r in enumerate(rows[start:], start=start):
        history.append({
            "date":   r.get("date", ""),
            "open":   r.get("open", r["close"]),
            "high":   r["high"],
            "low":    r["low"],
            "close":  r["close"],
            "volume": r.get("volume", 0),
            "ema20":  ema20_series[i],
            "ema50":  ema50_series[i],
        })
    return history


def calculate_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(sum(trs[-period:]) / period, 4)


def td_get(endpoint: str, params: dict) -> dict:
    params["apikey"] = TWELVE_DATA_KEY
    r = requests.get(f"{TD_BASE}/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "Twelve Data API error"))
    return data


def is_yahoo_ticker(ticker: str) -> bool:
    return any(ticker.upper().endswith(s) for s in YAHOO_SUFFIXES)


def fetch_yahoo(ticker: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = requests.get(url, params={"interval": "1d", "range": "1y"}, headers=YAHOO_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    meta = result["meta"]
    q = result["indicators"]["quote"][0]

    # Alinear por índice: solo filas donde close, high y low estén presentes
    raw_timestamps = result.get("timestamp", [])
    raw_closes  = q.get("close",  [])
    raw_volumes = q.get("volume", [])
    raw_highs   = q.get("high",   [])
    raw_lows    = q.get("low",    [])
    raw_opens   = q.get("open",   [])

    # Rellenar listas cortas para que zip no corte filas
    n = max(len(raw_closes), len(raw_highs), len(raw_lows))
    while len(raw_timestamps) < n: raw_timestamps.append(None)
    while len(raw_opens)      < n: raw_opens.append(None)

    rows = []
    for ts, close, volume, high, low, open_ in zip(
        raw_timestamps, raw_closes, raw_volumes, raw_highs, raw_lows, raw_opens
    ):
        if close is None or high is None or low is None:
            continue
        date_str = (
            datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            if ts is not None else ""
        )
        rows.append({
            "date":   date_str,
            "open":   open_ if open_ is not None else close,
            "close":  close,
            "volume": volume or 0,
            "high":   high,
            "low":    low,
        })

    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]
    highs   = [r["high"]   for r in rows]
    lows    = [r["low"]    for r in rows]

    current    = closes[-1]
    prev_close = closes[-2]
    return {
        "current":    current,
        "prev_close": prev_close,
        "change_pct": ((current - prev_close) / prev_close) * 100,
        "volume":     volumes[-1] if volumes else 0,
        "avg_volume": int(sum(volumes[-30:]) / min(30, len(volumes))) if volumes else 0,
        "week52_high": max(highs) if highs else None,
        "week52_low":  min(lows)  if lows  else None,
        "name":     meta.get("longName") or meta.get("shortName") or ticker,
        "currency": meta.get("currency", "USD"),
        "closes":   closes,
        "highs":    highs,
        "lows":     lows,
        "rows":     rows,
    }


def get_fx_rate(from_currency: str, to_currency: str) -> float | None:
    if from_currency == to_currency:
        return 1.0
    try:
        data = td_get("price", {"symbol": f"{from_currency}/{to_currency}"})
        return float(data["price"])
    except Exception as e:
        print(f"[WARN] FX rate {from_currency}/{to_currency} failed: {e}")
        return None


def get_ticker_data(ticker_config, thresholds: dict, fx_cache: dict) -> dict | None:
    if isinstance(ticker_config, str):
        ticker_sym = ticker_config
        purchase_price = None
        purchase_currency = None
        shares = None
    else:
        ticker_sym = ticker_config["ticker"]
        purchase_price = ticker_config.get("purchase_price")
        purchase_currency = ticker_config.get("purchase_currency")
        shares = ticker_config.get("shares")

    try:
        closes_hist = None   # historial para MA/RSI/EMA (solo disponible en Yahoo de entrada)
        highs_hist  = None
        lows_hist   = None
        rows_hist   = None   # rows con fecha+open para historial de velas del gráfico EMA

        if is_yahoo_ticker(ticker_sym):
            # Acciones europeas: Yahoo Finance directo
            q = fetch_yahoo(ticker_sym)
            current     = q["current"]
            prev_close  = q["prev_close"]
            change_pct  = q["change_pct"]
            volume      = q["volume"]
            avg_volume  = q["avg_volume"]
            week52_high = q["week52_high"]
            week52_low  = q["week52_low"]
            name        = q["name"]
            currency    = q["currency"]
            closes_hist = q["closes"]   # año completo disponible gratis
            highs_hist  = q["highs"]
            lows_hist   = q["lows"]
            rows_hist   = q["rows"]     # OHLCV + fecha para gráfico EMA
        else:
            # Acciones US y principales internacionales: Twelve Data
            quote = td_get("quote", {"symbol": ticker_sym})
            current     = float(quote["close"])
            prev_close  = float(quote["previous_close"])
            change_pct  = float(quote.get("percent_change", 0))
            volume      = int(float(quote.get("volume") or 0))
            avg_volume  = int(float(quote.get("average_volume") or 0))
            w52         = quote.get("fifty_two_week", {})
            week52_high = float(w52["high"]) if w52.get("high") else None
            week52_low  = float(w52["low"])  if w52.get("low")  else None
            name        = quote.get("name", ticker_sym)
            currency    = quote.get("currency", "USD")
            # closes_hist para TD se obtiene en enrich_with_technical_indicators()

        # RSI y MAs se calculan en enrich_with_technical_indicators() ANTES de evaluate_triggers()
        rsi  = None
        ma20 = None
        ma50 = None

        # Convertir precio actual a la divisa de compra si es necesario
        fx_rate_to_purchase_currency = 1.0
        current_in_purchase_currency = current
        if purchase_currency and purchase_currency != currency:
            pair = f"{currency}/{purchase_currency}"
            if pair not in fx_cache:
                time.sleep(8)
                fx_cache[pair] = get_fx_rate(currency, purchase_currency)
            rate = fx_cache.get(pair)
            if rate:
                fx_rate_to_purchase_currency = rate
                current_in_purchase_currency = current * rate

        pnl_pct = None
        pnl_value = None
        if purchase_price and purchase_price > 0:
            pnl_pct = round(((current_in_purchase_currency - purchase_price) / purchase_price) * 100, 2)
            if shares:
                pnl_value = round((current_in_purchase_currency - purchase_price) * shares, 2)

        data = {
            "ticker": ticker_sym,
            "name": name,
            "current": round(current, 4),
            "prev_close": round(prev_close, 4),
            "change_pct": round(change_pct, 2),
            "currency": currency,                          # divisa de cotización
            "purchase_currency": purchase_currency,        # divisa en que se compró
            "current_in_purchase_currency": round(current_in_purchase_currency, 4),
            "volume": volume,
            "avg_volume": avg_volume,
            "week52_high": week52_high,
            "week52_low": week52_low,
            "rsi": rsi,
            "ma20": ma20,
            "ma50": ma50,
            "ema20": None,
            "ema50": None,
            "atr": None,
            "ema20_in_purchase_currency": None,
            "ema50_in_purchase_currency": None,
            "atr_in_purchase_currency": None,
            "fx_rate_to_purchase_currency": fx_rate_to_purchase_currency,
            "ema_swing": None,
            "purchase_price": purchase_price,
            "shares": shares,
            "pnl_pct": pnl_pct,
            "pnl_value": pnl_value,
            "history": None,           # se rellena en enrich_with_technical_indicators()
            "_closes": closes_hist,   # prefijo _ = no se persiste en el estado
            "_highs":  highs_hist,
            "_lows":   lows_hist,
            "_rows":   rows_hist,
        }

        return data

    except Exception as e:
        print(f"[WARN] Error fetching {ticker_sym}: {e}")
        return None


def enrich_with_technical_indicators(data: dict) -> None:
    """Calcula RSI, MA20, MA50, EMA20, EMA50 y ATR in-place en data antes de evaluate_triggers().

    Reutiliza closes/highs/lows de Yahoo si están disponibles; si no, pide
    time_series a Twelve Data (outputsize 80 para tener margen en EMA50/ATR).
    """
    ticker = data["ticker"]
    closes = data.get("_closes")
    highs  = data.get("_highs")
    lows   = data.get("_lows")

    if not closes:
        try:
            time.sleep(8)
            ts     = td_get("time_series", {"symbol": ticker, "interval": "1day", "outputsize": MAX_SWING_HISTORY_DAYS})
            values = list(reversed(ts.get("values", [])))
            # Alinear: solo filas con close, high y low presentes para que ATR sea correcto
            valid  = [d for d in values
                      if d.get("close") is not None
                      and d.get("high")  is not None
                      and d.get("low")   is not None]
            closes = [float(d["close"]) for d in valid]
            highs  = [float(d["high"])  for d in valid]
            lows   = [float(d["low"])   for d in valid]
            data["_closes"] = closes
            data["_highs"]  = highs
            data["_lows"]   = lows
            # Historial de velas para el gráfico EMA (Twelve Data tiene fecha en "datetime")
            data["history"] = build_swing_history(valid, limit=MAX_SWING_HISTORY_DAYS)
        except Exception as e:
            print(f"[WARN] Historical prices fetch failed for {ticker}: {e}")
            return

    if closes:
        data["rsi"]  = calculate_rsi(closes)
        data["ma20"] = calculate_ma(closes, 20)
        data["ma50"] = calculate_ma(closes, 50)
        data["ema20"] = calculate_ema(closes, 20)
        data["ema50"] = calculate_ema(closes, 50)

    if highs and lows and closes:
        data["atr"] = calculate_atr(highs, lows, closes)

    # Historial de velas para Yahoo (rows ya tienen fecha y open)
    if data.get("_rows") and not data.get("history"):
        data["history"] = build_swing_history_from_rows(data["_rows"], limit=MAX_SWING_HISTORY_DAYS)

    # Convertir indicadores a divisa operativa (purchase_currency)
    rate = data.get("fx_rate_to_purchase_currency") or 1.0
    if rate != 1.0:
        if data.get("ema20") is not None:
            data["ema20_in_purchase_currency"] = round(data["ema20"] * rate, 4)
        if data.get("ema50") is not None:
            data["ema50_in_purchase_currency"] = round(data["ema50"] * rate, 4)
        if data.get("atr") is not None:
            data["atr_in_purchase_currency"] = round(data["atr"] * rate, 4)


def evaluate_triggers(data: dict, thresholds: dict) -> list[str]:
    triggers = []

    price_thr = thresholds.get("price_change_pct", 3.0)
    if abs(data["change_pct"]) >= price_thr:
        direction = "Subida" if data["change_pct"] > 0 else "Caída"
        triggers.append(f"{direction} del {abs(data['change_pct']):.2f}% hoy (umbral: {price_thr}%)")

    vol_mult = thresholds.get("volume_spike_multiplier", 2.0)
    if data["volume"] and data["avg_volume"] and data["avg_volume"] > 0:
        ratio = data["volume"] / data["avg_volume"]
        if ratio >= vol_mult:
            triggers.append(f"Volumen inusual: {ratio:.1f}x la media")

    if data["rsi"] is not None:
        if data["rsi"] >= thresholds.get("rsi_overbought", 70):
            triggers.append(f"RSI en sobrecompra: {data['rsi']}")
        elif data["rsi"] <= thresholds.get("rsi_oversold", 30):
            triggers.append(f"RSI en sobreventa: {data['rsi']}")

    prox = thresholds.get("week52_proximity_pct", 2.0)
    if data["week52_high"] and data["current"]:
        dist = ((data["week52_high"] - data["current"]) / data["week52_high"]) * 100
        if dist <= prox:
            triggers.append(f"Cerca del máximo de 52 semanas ({data['week52_high']:.2f}, a {dist:.1f}%)")
    if data["week52_low"] and data["current"]:
        dist = ((data["current"] - data["week52_low"]) / data["week52_low"]) * 100
        if dist <= prox:
            triggers.append(f"Cerca del mínimo de 52 semanas ({data['week52_low']:.2f}, a {dist:.1f}%)")

    if data["pnl_pct"] is not None:
        pc_label = data.get("purchase_currency") or data.get("currency") or ""
        if data["pnl_pct"] >= thresholds.get("profit_alert_pct", 20.0):
            triggers.append(
                f"Beneficio del {data['pnl_pct']:.1f}% sobre precio de compra "
                f"({data['purchase_price']} {pc_label})"
            )
        elif data["pnl_pct"] <= -thresholds.get("stop_loss_pct", 10.0):
            triggers.append(
                f"Stop loss activado: pérdida del {abs(data['pnl_pct']):.1f}% "
                f"sobre precio de compra ({data['purchase_price']} {pc_label})"
            )

    return triggers


def _parse_source(raw) -> str:
    """Marketaux puede devolver source como str o como {"name": ...}."""
    if isinstance(raw, dict):
        return raw.get("name", "")
    return raw or ""


def get_news_marketaux(ticker: str, name: str, lookback_hours: int = NEWS_LOOKBACK_HOURS) -> list[dict]:
    """Obtiene noticias de Marketaux con sentimiento por entidad.
    Devuelve lista de artículos: {title, description, url, source, published_at, sentiment}.
    """
    if not MARKETAUX_API_KEY:
        return []

    from datetime import timedelta
    base_ticker = ticker.split(".")[0].upper()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M")

    params_base = {
        "api_token": MARKETAUX_API_KEY,
        "filter_entities": "true",
        "language": "en",
        "published_after": cutoff,
        "limit": 5,
    }

    articles: list[dict] = []

    # Intento 1: búsqueda por símbolo exacto
    try:
        r = requests.get(MARKETAUX_BASE, params={**params_base, "symbols": base_ticker}, timeout=12)
        raw = r.json().get("data", [])
        articles = raw
    except Exception as e:
        print(f"[WARN] Marketaux symbol fetch failed for {ticker}: {e}")

    # Intento 2: si no hay resultados, buscar por nombre de empresa
    if not articles and name:
        search_term = name.split()[0]
        try:
            r = requests.get(MARKETAUX_BASE, params={**params_base, "search": search_term}, timeout=12)
            articles = r.json().get("data", [])
        except Exception as e:
            print(f"[WARN] Marketaux name search failed for {ticker}: {e}")

    # Normalizar: añadir campo 'sentiment' resuelto para nuestro ticker
    result = []
    for a in articles:
        entities = a.get("entities", [])
        score = 0.0
        # Buscar la entidad que coincide con nuestro símbolo
        for e in entities:
            if e.get("symbol", "").upper() == base_ticker:
                score = float(e.get("sentiment_score", 0.0))
                break
        else:
            # Fallback: media de todas las entidades del artículo
            scores = [float(e.get("sentiment_score", 0.0)) for e in entities]
            if scores:
                score = sum(scores) / len(scores)

        result.append({
            "title":        a.get("title", ""),
            "description":  (a.get("description") or "")[:200],
            "url":          a.get("url", ""),
            "source":       _parse_source(a.get("source")),
            "published_at": a.get("published_at", ""),
            "sentiment":    round(score, 3),
        })
    return result


def _sentiment_emoji(score: float) -> str:
    if score >= 0.25:
        return "🟢"
    if score <= -0.25:
        return "🔴"
    return "⚪"


def _sentiment_line(articles: list[dict]) -> str:
    """Devuelve una línea de texto con el sentimiento neto del conjunto de artículos."""
    scores = [a["sentiment"] for a in articles if a.get("sentiment", 0) != 0]
    if not scores:
        return "📊 Sentimiento: ⚪ Sin datos de sentimiento"
    net = sum(scores) / len(scores)
    emoji = _sentiment_emoji(net)
    label = "Positivo" if net >= 0.25 else "Negativo" if net <= -0.25 else "Neutral"
    return f"📊 Sentimiento: {emoji} {label} ({net:+.2f})"


def _headline_hash(h: str) -> str:
    """Hash determinista (MD5 truncado) para deduplicación persistente entre ejecuciones."""
    return hashlib.md5(h.encode("utf-8")).hexdigest()[:12]


def _urgent_news_score(article: dict) -> int:
    text = f"{article.get('title', '')} {article.get('description', '')}".lower()
    return sum(weight for keyword, weight in URGENT_NEWS_KEYWORDS.items() if keyword in text)


def get_urgent_news_for_all_tickers(tickers: list[str], lookback_minutes: int = URGENT_NEWS_LOOKBACK_MINUTES) -> list[dict]:
    """Consulta Marketaux una sola vez para todos los tickers y devuelve solo noticias urgentes."""
    if not MARKETAUX_API_KEY or not tickers:
        return []

    symbols = sorted({ticker.split(".")[0].upper() for ticker in tickers if ticker})
    if not symbols:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M")

    try:
        r = requests.get(
            MARKETAUX_BASE,
            params={
                "api_token": MARKETAUX_API_KEY,
                "filter_entities": "true",
                "language": "en",
                "published_after": cutoff,
                "symbols": ",".join(symbols),
                "sort": "published_on",
                "limit": 20,
            },
            timeout=12,
        )
        r.raise_for_status()
        raw = r.json().get("data", [])
    except Exception as e:
        print(f"[WARN] Urgent Marketaux fetch failed: {e}")
        return []

    urgent_articles: list[dict] = []
    for article in raw:
        title = (article.get("title") or "").strip()
        if not title:
            continue

        score = _urgent_news_score(article)
        if score < 3:
            continue

        entities = article.get("entities", [])
        matched_symbols = sorted({
            e.get("symbol", "").upper()
            for e in entities
            if e.get("symbol", "").upper() in symbols
        })

        sentiment_scores = [
            float(e.get("sentiment_score", 0))
            for e in entities
            if e.get("sentiment_score") is not None
        ]
        sentiment = round(sum(sentiment_scores) / len(sentiment_scores), 3) if sentiment_scores else 0.0

        urgent_articles.append({
            "title": title,
            "description": (article.get("description") or "")[:220],
            "url": article.get("url", ""),
            "source": _parse_source(article.get("source")),
            "published_at": article.get("published_at", ""),
            "sentiment": sentiment,
            "symbols": matched_symbols,
            "urgency_score": score,
            "hash": _headline_hash(title),
        })

    urgent_articles.sort(key=lambda a: a.get("urgency_score", 0), reverse=True)
    return urgent_articles


def check_urgent_news(config: dict, portfolio_state: dict, alerts_log: list) -> None:
    """Envía una alerta agrupada de noticias urgentes usando una sola request por ejecución."""
    if not portfolio_state.get("urgent_news_alerts", False):
        return

    now = datetime.now(timezone.utc)
    interval_minutes = int(portfolio_state.get("urgent_news_interval_minutes", URGENT_NEWS_INTERVAL_MINUTES))
    last_check = portfolio_state.get("last_urgent_news_check")

    if last_check:
        try:
            last_dt = datetime.fromisoformat(last_check)
            if (now - last_dt).total_seconds() < interval_minutes * 60:
                print("[URGENT] Skipping urgent news check; interval not elapsed")
                return
        except ValueError:
            pass

    tickers: list[str] = []

    for item in config.get("portfolio", []):
        tickers.append(item["ticker"] if isinstance(item, dict) else item)

    for item in config.get("watchlist", []):
        tickers.append(item["ticker"] if isinstance(item, dict) else item)

    lookback_minutes = int(portfolio_state.get("urgent_news_lookback_minutes", URGENT_NEWS_LOOKBACK_MINUTES))
    articles = get_urgent_news_for_all_tickers(tickers, lookback_minutes=lookback_minutes)

    portfolio_state["last_urgent_news_check"] = now.isoformat()

    if not articles:
        print("[URGENT] No urgent news")
        return

    seen = set(portfolio_state.get("urgent_news_seen", []))
    new_articles = [article for article in articles if article["hash"] not in seen]

    all_hashes = list(seen | {article["hash"] for article in articles})
    portfolio_state["urgent_news_seen"] = all_hashes[-MAX_URGENT_NEWS_SEEN:]

    if not new_articles:
        print("[URGENT] No new urgent news")
        return

    lines = [
        "🚨 *Noticias urgentes de cartera/radar*",
        f"_Últimos {lookback_minutes} min · 1 consulta agrupada_",
        "────────────────────",
    ]

    for article in new_articles[:5]:
        sentiment = _sentiment_emoji(article.get("sentiment", 0.0))
        symbols = ", ".join(article.get("symbols") or []) or "Portfolio"
        source = f" · {article['source']}" if article.get("source") else ""
        link = f"\n[Ver noticia →]({article['url']})" if article.get("url") else ""

        lines.append(f"{sentiment} *{symbols}*{source}")
        lines.append(f"{article['title']}{link}")
        lines.append("")

    send_telegram("\n".join(lines).strip())
    print(f"[URGENT] Sent {min(len(new_articles), 5)} urgent news alerts")

    for article in new_articles[:5]:
        alerts_log.append({
            "timestamp": now.isoformat(),
            "ticker": ",".join(article.get("symbols") or []),
            "name": "Noticias urgentes",
            "price": None,
            "change_pct": None,
            "pnl_pct": None,
            "triggers": ["Noticias urgentes"],
            "recommendation": "—",
            "analysis": article["title"],
            "type": "urgent_news",
            "url": article.get("url", ""),
        })

    if len(alerts_log) > MAX_ALERTS_LOG:
        alerts_log[:] = alerts_log[-MAX_ALERTS_LOG:]


def build_claude_prompt(data: dict, triggers: list[str], articles: list[dict], role: str) -> str:
    triggers_block = "\n".join(f"• {t}" for t in triggers)

    if articles:
        news_lines = []
        for a in articles:
            emoji = _sentiment_emoji(a["sentiment"])
            src = f" [{a['source']}]" if a["source"] else ""
            desc = f"\n  {a['description']}" if a["description"] else ""
            news_lines.append(f"{emoji}{src} {a['title']}{desc}")
        news_block = "\n".join(news_lines)
    else:
        news_block = "Sin noticias recientes disponibles."

    purchase_info = ""
    if data.get("purchase_price"):
        pc_label  = data.get("purchase_currency") or data.get("currency") or ""
        qty_label = data.get("currency") or ""
        pnl_str = f" ({data['pnl_pct']:+.1f}% P&L)" if data.get("pnl_pct") is not None else ""
        purchase_info = f"\n- Precio de compra: {data['purchase_price']:.2f} {pc_label}{pnl_str}"
        # Si hay conversión de divisa, mostrar también el precio actual convertido
        cip = data.get("current_in_purchase_currency")
        if pc_label and qty_label and pc_label != qty_label and cip is not None:
            purchase_info += (
                f"\n- Cotización actual: {data['current']:.2f} {qty_label}"
                f" → {cip:.2f} {pc_label} (convertido)"
            )

    return f"""Eres un analista financiero conciso. El usuario tiene {role} en {data['name']} ({data['ticker']}).

DATOS DE MERCADO HOY:
- Precio actual: {data['current']:.2f} {data.get('currency', '')}
- Cierre anterior: {data['prev_close']:.2f} {data.get('currency', '')}
- Variación: {data['change_pct']:+.2f}%{purchase_info}
- RSI: {data['rsi'] if data['rsi'] is not None else 'N/D'}

ALERTAS ACTIVADAS:
{triggers_block}

NOTICIAS RECIENTES (🟢 positivo | ⚪ neutral | 🔴 negativo):
{news_block}

Proporciona un análisis breve (máximo 3 oraciones) explicando el contexto de las alertas y termina con una recomendación clara: COMPRAR, VENDER o MANTENER, justificada en una frase.
Responde en español."""


def get_claude_analysis(data: dict, triggers: list[str], articles: list[dict], role: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        messages=[{"role": "user", "content": build_claude_prompt(data, triggers, articles, role)}],
    )
    return message.content[0].text.strip()


def extract_recommendation(analysis: str | None) -> str:
    if not analysis:
        return "—"
    upper = analysis.upper()
    if "COMPRAR" in upper:
        return "COMPRAR"
    if "VENDER" in upper:
        return "VENDER"
    return "MANTENER"


def synthesize_signal(data: dict) -> str | None:
    """Combina RSI, MAs, volumen y extremos anuales en una orientación alcista/bajista."""
    bullish: list[str] = []
    bearish: list[str] = []

    current = data.get("current", 0)
    change  = data.get("change_pct", 0)
    rsi     = data.get("rsi")
    ma20    = data.get("ma20")
    ma50    = data.get("ma50")
    volume  = data.get("volume")
    avg_vol = data.get("avg_volume")
    w52h    = data.get("week52_high")
    w52l    = data.get("week52_low")

    # RSI
    if rsi is not None:
        if rsi <= 35:
            bullish.append(f"RSI en sobreventa ({rsi})")
        elif rsi >= 65:
            bearish.append(f"RSI en sobrecompra ({rsi})")

    # Precio vs MAs (tolerancia 0.1% para evitar ruido en zona plana)
    if current and ma20:
        if current > ma20 * 1.001:
            bullish.append(f"precio sobre MA20 ({ma20:.2f})")
        elif current < ma20 * 0.999:
            bearish.append(f"precio bajo MA20 ({ma20:.2f})")

    if current and ma50:
        if current > ma50 * 1.001:
            bullish.append(f"precio sobre MA50 ({ma50:.2f})")
        elif current < ma50 * 0.999:
            bearish.append(f"precio bajo MA50 ({ma50:.2f})")

    # Cruce MA20/MA50 — señal de tendencia de mayor peso
    if ma20 and ma50:
        if ma20 > ma50 * 1.002:
            bullish.append("MA20 por encima de MA50")
        elif ma20 < ma50 * 0.998:
            bearish.append("MA20 por debajo de MA50")

    # Volumen con dirección: amplifica el movimiento del día
    if volume and avg_vol and avg_vol > 0:
        ratio = volume / avg_vol
        if ratio >= 1.8:
            if change > 0:
                bullish.append(f"volumen {ratio:.1f}× la media acompañando subida")
            elif change < 0:
                bearish.append(f"volumen {ratio:.1f}× la media acompañando caída")

    # Extremos anuales + dirección del día
    if w52l and current:
        dist_l = ((current - w52l) / w52l) * 100
        if dist_l <= 5:
            if change >= 0:
                bullish.append(f"rebote en zona de mínimo anual ({w52l:.2f})")
            else:
                bearish.append(f"presión bajista cerca del mínimo anual ({w52l:.2f})")

    if w52h and current:
        dist_h = ((w52h - current) / w52h) * 100
        if dist_h <= 5:
            if change >= 0:
                bullish.append(f"posible ruptura del máximo anual ({w52h:.2f})")
            else:
                bearish.append(f"rechazo en zona del máximo anual ({w52h:.2f})")

    if not bullish and not bearish:
        return None

    nb, nd = len(bullish), len(bearish)
    if nb > nd:
        params = ", ".join(bullish[:3])
        return f"📈 La variación actual de {params} podría ser señal de posible subida o recuperación."
    elif nd > nb:
        params = ", ".join(bearish[:3])
        return f"📉 La variación actual de {params} podría ser señal de posible bajada o corrección."
    else:
        # Señales equilibradas — mostrar ambos lados
        params = " y ".join(bullish[:1] + bearish[:1])
        return f"⚖️ Señales mixtas — {params}. Sin orientación clara."


def evaluate_ema_swing_strategy(data: dict) -> dict:
    """Evalúa la estructura EMA20/EMA50 y devuelve un dict con trend, setup, score y riesgo."""
    current    = data.get("current")
    ema20      = data.get("ema20")
    ema50      = data.get("ema50")
    rsi        = data.get("rsi")
    volume     = data.get("volume")
    avg_volume = data.get("avg_volume")
    atr        = data.get("atr")

    if not current or not ema20 or not ema50:
        return {
            "trend": "SIN_DATOS",
            "setup": "SIN_DATOS",
            "score": 0,
            "risk": "No hay suficientes datos históricos.",
            "reason": "Faltan EMA20/EMA50.",
            "dist_ema20_pct": None,
            "dist_ema50_pct": None,
            "volume_ratio": None,
            "suggested_stop": None,
            "display_currency": data.get("purchase_currency") or data.get("currency"),
            "display_price": data.get("current_in_purchase_currency") or current,
            "display_ema20": None,
            "display_ema50": None,
            "display_atr": None,
        }

    dist_ema20    = ((current - ema20) / ema20) * 100
    dist_ema50    = ((current - ema50) / ema50) * 100
    volume_ratio  = volume / avg_volume if volume and avg_volume else None

    score   = 50
    reasons = []
    setup   = "NEUTRAL"
    trend   = "NEUTRAL"
    risk    = "Riesgo normal."

    # ── Tendencia ─────────────────────────────────────────────────────────────
    if ema20 > ema50 and current > ema50:
        trend = "ALCISTA"
        score += 20
        reasons.append("EMA20 por encima de EMA50")
    elif ema20 < ema50 and current < ema50:
        trend = "DEBIL"
        score -= 20
        reasons.append("EMA20 por debajo de EMA50")
    else:
        trend = "CONSOLIDANDO"
        reasons.append("Estructura mixta entre EMA20 y EMA50")

    # ── Setup ─────────────────────────────────────────────────────────────────
    if trend == "ALCISTA" and -2 <= dist_ema20 <= 3:
        setup  = "PULLBACK_EMA20"
        score += 15
        reasons.append("Precio cerca de EMA20 en tendencia alcista")
    elif trend == "ALCISTA" and dist_ema20 > 10:
        setup  = "EXTENDIDO"
        score -= 10
        risk   = f"Activo extendido: {dist_ema20:.1f}% sobre EMA20"
        reasons.append("Precio demasiado alejado de EMA20")
    elif ema20 > ema50 and abs(dist_ema50) < 3:
        setup  = "CRUCE_RECIENTE"
        score += 8
        reasons.append("Cruce o estructura reciente sobre EMA50")
    elif current < ema50:
        setup  = "PERDIDA_ESTRUCTURA"
        score -= 20
        risk   = "Precio por debajo de EMA50"
        reasons.append("Pérdida de estructura EMA50")

    # ── RSI ───────────────────────────────────────────────────────────────────
    if rsi is not None:
        if 45 <= rsi <= 65:
            score += 8
            reasons.append("RSI saludable para swing")
        elif rsi > 75:
            score -= 8
            reasons.append("RSI sobrecomprado")
        elif rsi < 35:
            score -= 8
            reasons.append("RSI débil o en sobreventa")

    # ── Volumen ───────────────────────────────────────────────────────────────
    if volume_ratio is not None:
        if volume_ratio >= 1.5 and current > ema20:
            score += 7
            reasons.append("Volumen superior a la media acompañando fortaleza")
        elif volume_ratio < 0.8 and setup == "PULLBACK_EMA20":
            score += 5
            reasons.append("Volumen decreciente durante pullback")

    score = max(0, min(100, round(score)))

    # Stop en divisa operativa (purchase_currency si existe)
    current_display  = data.get("current_in_purchase_currency") or current
    atr_display      = data.get("atr_in_purchase_currency") or atr
    suggested_stop   = round(current_display - 1.5 * atr_display, 2) if atr_display and current_display else None
    display_currency = data.get("purchase_currency") or data.get("currency")
    display_ema20    = data.get("ema20_in_purchase_currency") or ema20
    display_ema50    = data.get("ema50_in_purchase_currency") or ema50

    return {
        "trend": trend,
        "setup": setup,
        "score": score,
        "risk": risk,
        "reason": "; ".join(reasons),
        "dist_ema20_pct": round(dist_ema20, 2),
        "dist_ema50_pct": round(dist_ema50, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "suggested_stop": suggested_stop,
        "display_currency": display_currency,
        "display_price": round(current_display, 4),
        "display_ema20": round(display_ema20, 4) if display_ema20 else None,
        "display_ema50": round(display_ema50, 4) if display_ema50 else None,
        "display_atr": round(atr_display, 4) if atr_display else None,
    }


def detect_ema_alerts(ticker: str, data: dict, portfolio_state: dict) -> list[dict]:
    """Detecta alertas inteligentes basadas en EMA20/EMA50 y Swing Score."""
    if not portfolio_state.get("ema_alerts", True):
        return []
    s = data.get("ema_swing") or {}
    if not s or s.get("trend") == "SIN_DATOS":
        return []

    current_setup  = s.get("setup")
    current_trend  = s.get("trend")
    current_score  = s.get("score") or 0
    dist_ema20     = s.get("dist_ema20_pct")
    volume_ratio   = s.get("volume_ratio")

    previous       = portfolio_state.get("ema_swing_last", {}).get(ticker, {})
    previous_score = previous.get("score")
    previous_setup = previous.get("setup")

    min_score     = portfolio_state.get("ema_alert_min_score",        70)
    extension_pct = portfolio_state.get("ema_alert_extension_pct",    10.0)
    improve_from  = portfolio_state.get("ema_alert_score_improve_from", 60)
    improve_to    = portfolio_state.get("ema_alert_score_improve_to",   75)
    drop_from     = portfolio_state.get("ema_alert_score_drop_from",    70)
    drop_to       = portfolio_state.get("ema_alert_score_drop_to",      45)

    alerts: list[dict] = []

    if (current_setup == "PULLBACK_EMA20" and current_trend == "ALCISTA"
            and current_score >= min_score and previous_setup != "PULLBACK_EMA20"):
        alerts.append({"type": "PULLBACK_EMA20", "title": "Pullback EMA20", "emoji": "📌",
                        "severity": "opportunity",
                        "message": "Precio cerca de EMA20 en tendencia alcista. Posible setup de continuación."})

    if (current_setup == "PERDIDA_ESTRUCTURA" and previous_setup != "PERDIDA_ESTRUCTURA"
            and current_score <= drop_to):
        msg = "Precio por debajo de EMA50. La estructura swing se debilita."
        if volume_ratio and volume_ratio >= 1.3:
            msg += f" Volumen relativo elevado: {volume_ratio:.1f}×."
        alerts.append({"type": "PERDIDA_ESTRUCTURA", "title": "Pérdida de estructura",
                        "emoji": "⚠️", "severity": "risk", "message": msg})

    if (current_setup == "EXTENDIDO" and current_trend == "ALCISTA"
            and dist_ema20 is not None and dist_ema20 >= extension_pct
            and previous_setup != "EXTENDIDO"):
        alerts.append({"type": "EXTENDIDO", "title": "Activo extendido", "emoji": "🔥",
                        "severity": "risk",
                        "message": f"Precio {dist_ema20:+.1f}% sobre EMA20. Aumenta el riesgo de entrada por FOMO."})

    if (current_setup == "CRUCE_RECIENTE" and current_score >= 60
            and previous_setup != "CRUCE_RECIENTE"):
        alerts.append({"type": "CRUCE_RECIENTE", "title": "Cruce EMA20/EMA50", "emoji": "🟡",
                        "severity": "watch",
                        "message": "Posible cambio de tendencia swing. Vigilar confirmación con precio y volumen."})

    if (previous_score is not None and previous_score < improve_from and current_score >= improve_to):
        alerts.append({"type": "SCORE_MEJORA", "title": "Mejora de setup", "emoji": "📈",
                        "severity": "opportunity",
                        "message": f"Swing Score sube de {previous_score} a {current_score}."})

    if (previous_score is not None and previous_score >= drop_from and current_score <= drop_to):
        alerts.append({"type": "SCORE_DETERIORO", "title": "Deterioro de setup", "emoji": "📉",
                        "severity": "risk",
                        "message": f"Swing Score cae de {previous_score} a {current_score}."})

    return alerts


def ema_alert_should_send(ticker: str, alert: dict, data: dict, portfolio_state: dict) -> bool:
    """Evita repetir la misma alerta EMA demasiado a menudo."""
    last = portfolio_state.get("last_ema_alerts", {}).get(ticker)
    if not last:
        return True

    now = datetime.now(timezone.utc)
    if last.get("alert_type") != alert.get("type"):
        return True

    realert_hours = portfolio_state.get("ema_alert_realert_hours", 12)
    last_time_raw = last.get("timestamp")
    if last_time_raw:
        try:
            last_time = datetime.fromisoformat(last_time_raw)
            if (now - last_time).total_seconds() / 3600 >= realert_hours:
                return True
        except ValueError:
            return True

    last_price    = last.get("price")
    current_price = data.get("current")
    realert_pct   = portfolio_state.get("ema_alert_realert_move_pct", 3.0)
    if last_price and current_price and last_price > 0:
        if abs((current_price - last_price) / last_price) * 100 >= realert_pct:
            return True

    return False


def format_ema_alert_message(data: dict, alert: dict) -> str:
    s = data.get("ema_swing") or {}
    ticker   = data.get("ticker", "")
    name     = data.get("name", "")
    display_currency = data.get("purchase_currency") or data.get("currency") or ""
    display_price    = data.get("current_in_purchase_currency") or data.get("current") or 0
    score     = s.get("score", 0)
    trend     = s.get("trend", "SIN_DATOS")
    setup     = s.get("setup", "SIN_DATOS")
    dist_ema20     = s.get("dist_ema20_pct")
    volume_ratio   = s.get("volume_ratio")
    suggested_stop = s.get("suggested_stop")

    dist_line = f"\nDistancia EMA20: {dist_ema20:+.1f}%" if dist_ema20 is not None else ""
    vol_line  = f"\nVolumen relativo: {volume_ratio:.1f}×" if volume_ratio is not None else ""
    stop_line = f"\nStop sugerido: {suggested_stop:.2f} {display_currency}" if suggested_stop is not None else ""

    return (
        f"{alert.get('emoji','📌')} *{ticker}* — {alert.get('title','Alerta EMA')}\n"
        f"{name}\n\n"
        f"Precio: {display_price:.2f} {display_currency} | {data.get('change_pct',0):+.2f}% hoy\n"
        f"Tendencia: {trend}\n"
        f"Setup: {setup}\n"
        f"Swing Score: {score}"
        f"{dist_line}{vol_line}{stop_line}\n\n"
        f"{alert.get('message','')}"
    )


def update_ema_swing_last(ticker: str, data: dict, portfolio_state: dict) -> None:
    """Guarda la última lectura EMA por ticker para comparar en la siguiente ejecución."""
    s = data.get("ema_swing") or {}
    portfolio_state.setdefault("ema_swing_last", {})[ticker] = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "price":         data.get("current"),
        "setup":         s.get("setup"),
        "trend":         s.get("trend"),
        "score":         s.get("score"),
        "dist_ema20_pct": s.get("dist_ema20_pct"),
        "dist_ema50_pct": s.get("dist_ema50_pct"),
    }


def merge_swing_history(
    swing_history: dict,
    ticker: str,
    data: dict,
    max_days: int = MAX_SWING_HISTORY_DAYS,
) -> None:
    """Fusiona el history actual del ticker con swing_history.json.

    Deduplica por date y conserva solo las últimas max_days sesiones.
    Los campos de la última vela se enriquecen con setup/score/atr/rsi del run actual.
    """
    history = data.get("history") or []
    if not history:
        return

    now = datetime.now(timezone.utc).isoformat()
    root = swing_history.setdefault("tickers", {})
    entry = root.setdefault(ticker, {
        "ticker":             ticker,
        "name":               data.get("name", ticker),
        "currency":           data.get("currency", ""),
        "last_seen_in_config": now,
        "last_updated":        now,
        "active":              True,
        "history":             [],
    })

    # Actualizar metadatos
    entry["ticker"]              = ticker
    entry["name"]                = data.get("name", entry.get("name", ticker))
    entry["currency"]            = data.get("currency", entry.get("currency", ""))
    entry["last_seen_in_config"] = now
    entry["last_updated"]        = now
    entry["active"]              = True

    # Índice existente por fecha
    existing_by_date: dict[str, dict] = {
        row["date"]: row
        for row in entry.get("history", [])
        if row.get("date")
    }

    # Vela más reciente del run actual para añadir indicadores del día
    last_date = history[-1].get("date") if history else None
    s = data.get("ema_swing") or {}

    for row in history:
        date = row.get("date")
        if not date:
            continue
        enriched = dict(row)
        if date == last_date:
            enriched["atr"]   = data.get("atr")
            enriched["rsi"]   = data.get("rsi")
            enriched["setup"] = s.get("setup")
            enriched["score"] = s.get("score")
        existing_by_date[date] = enriched

    merged = sorted(existing_by_date.values(), key=lambda r: r.get("date", ""))
    entry["history"] = merged[-max_days:]
    swing_history["last_update"] = now


def mark_inactive_swing_history_tickers(
    swing_history: dict,
    active_tickers: set[str],
) -> None:
    """Marca active=False en tickers que ya no están en portfolio/watchlist."""
    now = datetime.now(timezone.utc).isoformat()
    for ticker, entry in swing_history.get("tickers", {}).items():
        if ticker in active_tickers:
            entry["active"] = True
            entry["last_seen_in_config"] = now
        else:
            entry["active"] = False


def format_telegram_message(data: dict, triggers: list[str], analysis: str | None,
                            role: str, signal: str | None = None) -> str:
    arrow = "🔴" if data["change_pct"] < 0 else "🟢"
    sign = "+" if data["change_pct"] > 0 else ""
    tag = "📁 CARTERA" if role == "posición" else "👁 RADAR"

    pnl_line = ""
    if data.get("pnl_pct") is not None:
        pnl_sign = "+" if data["pnl_pct"] > 0 else ""
        pnl_line = f"\n💰 P&L: {pnl_sign}{data['pnl_pct']:.1f}%"
        if data.get("pnl_value") is not None:
            # pnl_value está en divisa de compra, no de cotización
            pc_label = data.get("purchase_currency") or data.get("currency") or ""
            pnl_line += f" ({pnl_sign}{data['pnl_value']:.0f} {pc_label})"

    ma_parts = []
    if data.get("ma20"): ma_parts.append(f"MA20 {data['ma20']:.2f}")
    if data.get("ma50"): ma_parts.append(f"MA50 {data['ma50']:.2f}")
    ma_line = f"\n📐 {' | '.join(ma_parts)}" if ma_parts else ""

    triggers_block = "\n".join(f"  • {t}" for t in triggers)
    analysis_block = f"\n\n📊 *Análisis:*\n{analysis}" if analysis else ""
    signal_block   = f"\n\n{signal}" if signal else ""

    return (
        f"{arrow} *{data['ticker']}* | {data['name']} | {sign}{data['change_pct']:.2f}% hoy\n"
        f"{tag} | Precio: {data['current']:.2f} {data['currency']}{pnl_line}{ma_line}\n\n"
        f"⚡ *Alertas:*\n{triggers_block}{analysis_block}{signal_block}"
    )


def send_telegram(text: str) -> bool:
    """Envía un mensaje Telegram con Markdown; reintenta como texto plano si el parse falla.
    Devuelve True si el mensaje se entregó, False en caso contrario (nunca lanza excepción).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.ok:
            return True
        # Telegram rechaza el Markdown (400 "can't parse entities") → reintentar sin formato
        print(f"[WARN] Telegram Markdown failed ({resp.status_code}); retrying as plain text")
        resp2 = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        if resp2.ok:
            return True
        print(f"[ERROR] Telegram plain text also failed: {resp2.status_code} {resp2.text[:200]}")
        return False
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return False


def send_telegram_with_button(text: str, ticker: str) -> bool:
    """Envía un mensaje con botón inline para solicitar análisis Claude a demanda.
    Reintenta sin Markdown si el parse falla. Devuelve True si se entregó.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": [[
            {"text": "🤖 Analizar con Claude", "callback_data": f"analyze:{ticker}"}
        ]]},
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            return True
        print(f"[WARN] Telegram button Markdown failed ({resp.status_code}); retrying as plain text")
        payload_plain = {k: v for k, v in payload.items() if k != "parse_mode"}
        resp2 = requests.post(url, json=payload_plain, timeout=10)
        if resp2.ok:
            return True
        print(f"[ERROR] Telegram button plain also failed: {resp2.status_code} {resp2.text[:200]}")
        return False
    except Exception as e:
        print(f"[ERROR] Telegram button send failed: {e}")
        return False


def _answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text, "show_alert": False},
            timeout=10,
        )
        result = r.json()
        if not result.get("ok"):
            # Expirado (>60s) — normal si el monitor tarda en ejecutarse; el análisis se sigue enviando
            print(f"[INFO] answerCallbackQuery expirado o fallido: {result.get('description', '')}")
    except Exception as e:
        print(f"[WARN] answerCallbackQuery exception: {e}")


def process_callback_queries(portfolio_state: dict, config: dict) -> None:
    """Al inicio de cada run, procesa peticiones de análisis pendientes del botón de Telegram."""
    offset = portfolio_state.get("last_update_id", 0)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": offset + 1, "timeout": 3},
            timeout=10,
        )
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[WARN] getUpdates failed: {e}")
        return

    print(f"[CALLBACK] {len(updates)} update(s) pendiente(s)")
    if not updates:
        return

    # Guardar el update_id más alto para no reprocesarlo en el siguiente run
    portfolio_state["last_update_id"] = max(u["update_id"] for u in updates)

    thresholds = config.get("thresholds", {})
    fx_cache: dict = {}

    for update in updates:
        cb = update.get("callback_query")
        if not cb:
            continue

        # Verificar que el botón fue pulsado en nuestro chat autorizado
        # (comparamos el chat del mensaje, no el user_id del pulsador)
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[WARN] Callback ignorado de chat no autorizado: {chat_id}")
            _answer_callback(cb["id"])
            continue

        cb_id   = cb["id"]
        cb_data = cb.get("data", "")
        if not cb_data.startswith("analyze:"):
            _answer_callback(cb_id)
            continue

        ticker = cb_data.split(":", 1)[1].upper()
        print(f"[CALLBACK] Análisis Claude solicitado para {ticker}")
        _answer_callback(cb_id, f"Analizando {ticker}… recibirás el resultado en breve ⏳")

        # Localizar config del ticker (cartera o radar)
        ticker_config = None
        role = "seguimiento"
        for item in config.get("portfolio", []):
            if isinstance(item, dict) and item.get("ticker", "").upper() == ticker:
                ticker_config = item
                role = "posición"
                break
        if ticker_config is None:
            wl = config.get("watchlist", [])
            if ticker in [w.upper() if isinstance(w, str) else w.get("ticker","").upper() for w in wl]:
                ticker_config = ticker
                role = "seguimiento"

        if ticker_config is None:
            send_telegram(f"⚠️ *{ticker}* no está en la configuración actual.")
            continue

        try:
            # Obtener datos actuales
            print(f"[CALLBACK] Obteniendo datos de mercado para {ticker}…")
            data = get_ticker_data(ticker_config, thresholds, fx_cache)
            if data is None:
                send_telegram(f"⚠️ No se pudieron obtener datos de *{ticker}*.")
                continue

            # Calcular RSI, MAs, EMAs y ATR (reutiliza closes si ya están en data)
            enrich_with_technical_indicators(data)
            data["ema_swing"] = evaluate_ema_swing_strategy(data)
            data["triggers"]  = evaluate_triggers(data, thresholds)
            print(f"[CALLBACK] RSI={data['rsi']} MA20={data['ma20']} MA50={data['ma50']} "
                  f"EMA20={data['ema20']} EMA50={data['ema50']} score={( data['ema_swing'] or {}).get('score')}")

            # Noticias
            print(f"[CALLBACK] Obteniendo noticias para {ticker}…")
            articles = get_news_marketaux(ticker, data["name"])
            print(f"[CALLBACK] {len(articles)} artículo(s) encontrado(s)")

            # Análisis Claude
            triggers = data.get("triggers") or ["Análisis solicitado manualmente"]
            print(f"[CALLBACK] Llamando a Claude para {ticker}…")
            analysis = get_claude_analysis(data, triggers, articles, role)
            print(f"[CALLBACK] Respuesta Claude recibida ({len(analysis)} chars)")

            signal  = synthesize_signal(data)
            ma_parts = []
            if data.get("ma20"): ma_parts.append(f"MA20 {data['ma20']:.2f}")
            if data.get("ma50"): ma_parts.append(f"MA50 {data['ma50']:.2f}")
            ma_line = f"\n📐 {' | '.join(ma_parts)}" if ma_parts else ""

            msg = (
                f"🤖 *Análisis Claude — {ticker}* | {data['name']}\n"
                f"Precio: {data['current']:.2f} {data['currency']} | {data['change_pct']:+.2f}% hoy"
                f"{ma_line}\n\n"
                f"📊 {analysis}"
            )
            if signal:
                msg += f"\n\n{signal}"

            send_telegram(msg)
            print(f"[SENT] On-demand Claude analysis for {ticker}")

        except Exception as e:
            err = f"[ERROR] Callback {ticker}: {e}"
            print(err)
            try:
                send_telegram(f"⚠️ Error generando análisis de *{ticker}*: {e}")
            except Exception:
                pass


def _trigger_type(trigger: str) -> str:
    """Extrae el tipo de trigger ignorando los valores numéricos concretos.
    Así "Subida del 3.12%" y "Subida del 3.18%" son el mismo trigger."""
    t = trigger.lower()
    if "subida" in t:       return "price_up"
    if "caída" in t:        return "price_down"
    if "volumen" in t:      return "volume_spike"
    if "sobrecompra" in t:  return "rsi_overbought"
    if "sobreventa" in t:   return "rsi_oversold"
    if "máximo de 52" in t: return "near_52w_high"
    if "mínimo de 52" in t: return "near_52w_low"
    if "beneficio" in t:    return "profit_alert"
    if "stop loss" in t:    return "stop_loss"
    return t


def triggers_changed(ticker: str, current_triggers: list[str], current_price: float,
                     portfolio_state: dict, thresholds: dict) -> bool:
    """Re-alerta si:
    - Cambia el TIPO de trigger.
    - Han pasado >= 6 horas con el mismo trigger activo.
    - El precio se ha movido >= realert_change_pct% desde la última alerta.
    """
    last = portfolio_state.get("last_alerts", {}).get(ticker, {})
    if not last:
        return True

    current_types = {_trigger_type(t) for t in current_triggers}
    last_types    = {_trigger_type(t) for t in last.get("triggers", [])}
    if current_types != last_types:
        return True

    last_time   = datetime.fromisoformat(last["timestamp"])
    hours_since = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
    if hours_since >= 6:
        return True

    last_price   = last.get("price")
    realert_pct  = thresholds.get("realert_change_pct", 2.0)
    if last_price and last_price > 0 and realert_pct > 0:
        move = abs((current_price - last_price) / last_price) * 100
        if move >= realert_pct:
            return True

    return False


def check_news(ticker: str, name: str, portfolio_state: dict, use_claude: bool,
               articles: list[dict] | None = None) -> str | None:
    """Evalúa noticias nuevas para un ticker."""
    if articles is None:
        articles = get_news_marketaux(ticker, name)
    if not articles:
        return None

    seen = portfolio_state.setdefault("seen_headlines", {})
    seen_hashes = set(seen.get(ticker, []))
    new_articles = [a for a in articles if _headline_hash(a["title"]) not in seen_hashes]

    all_hashes = list(seen_hashes | {_headline_hash(a["title"]) for a in articles})
    seen[ticker] = all_hashes[-50:]

    if not new_articles:
        return None

    if use_claude:
        articles_block = "\n\n".join(
            f"{i+1}. {_sentiment_emoji(a['sentiment'])} [{a['source']}] {a['title']}"
            + (f"\n   {a['description']}" if a["description"] else "")
            for i, a in enumerate(new_articles)
        )
        prompt = f"""Eres un analista financiero especializado en bolsa. Evalúa estas noticias nuevas sobre {name} ({ticker}).

NOTICIAS (🟢 positivo | ⚪ neutral | 🔴 negativo según fuente financiera):
{articles_block}

CRITERIOS para considerar una noticia RELEVANTE para un inversor:
- Afecta directamente a resultados financieros, ingresos o beneficios
- Cambio en liderazgo ejecutivo (CEO, CFO, etc.)
- Fusiones, adquisiciones, desinversiones o restructuraciones
- Problemas regulatorios, legales o sanciones importantes
- Lanzamiento de producto o servicio con impacto significativo en ingresos
- Cambio en guidance o previsiones de la empresa

NO son relevantes: artículos de opinión genéricos, listas ("mejores acciones para..."), noticias de sector sin mención directa.

Si alguna noticia cumple los criterios, responde con un resumen concreto en 2-3 frases de por qué importa para el inversor.
Si ninguna es relevante, responde ÚNICAMENTE: NO_RELEVANTE
Responde en español."""
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        assessment = msg.content[0].text.strip()
        return None if "NO_RELEVANTE" in assessment.upper() else assessment

    significant = []
    for a in new_articles:
        if abs(a["sentiment"]) >= 0.25:
            emoji = _sentiment_emoji(a["sentiment"])
            line = f"{emoji} *{a['title']}*"
            if a["source"]:
                line += f"\n_{a['source']}_"
            if a["description"]:
                line += f"\n{a['description']}"
            if a["url"]:
                line += f"\n[Leer →]({a['url']})"
            significant.append(line)

    return "\n\n".join(significant) if significant else None


def process_ticker(ticker_config, thresholds: dict, role: str, portfolio_state: dict,
                   alerts_log: list, fx_cache: dict,
                   swing_history: dict | None = None) -> None:
    data = get_ticker_data(ticker_config, thresholds, fx_cache)
    if data is None:
        return

    enrich_with_technical_indicators(data)
    data["ema_swing"] = evaluate_ema_swing_strategy(data)
    data["triggers"]  = evaluate_triggers(data, thresholds)

    # Persistir historial de velas en swing_history.json
    if swing_history is not None:
        merge_swing_history(swing_history, data["ticker"], data)

    ticker = data["ticker"]
    print(f"[INFO] {ticker}: {data['change_pct']:+.2f}%, RSI={data['rsi']}, "
          f"EMA20={data['ema20']}, score={( data['ema_swing'] or {}).get('score')}, "
          f"triggers={len(data['triggers'])}")

    state_entry = {k: v for k, v in data.items() if k not in ("triggers", "_closes", "_highs", "_lows", "_rows")}
    state_entry["role"] = "portfolio" if role == "posición" else "watchlist"
    portfolio_state["tickers"][ticker] = state_entry
    use_claude = portfolio_state.get("use_claude", True)

    # ── Alertas EMA inteligentes ─────────────────────────────────────────────
    ema_trigger_list = detect_ema_alerts(ticker, data, portfolio_state)
    for ema_alert in ema_trigger_list:
        if not ema_alert_should_send(ticker, ema_alert, data, portfolio_state):
            continue
        sent = send_telegram(format_ema_alert_message(data, ema_alert))
        if not sent:
            print(f"[WARN] EMA alert not sent for {ticker} ({ema_alert.get('type')}); state not marked as alerted")
            continue
        portfolio_state.setdefault("last_ema_alerts", {})[ticker] = {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "price":      data.get("current"),
            "alert_type": ema_alert.get("type"),
            "setup":      (data.get("ema_swing") or {}).get("setup"),
            "score":      (data.get("ema_swing") or {}).get("score"),
        }
        alerts_log.append({
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "ticker":         ticker,
            "name":           data["name"],
            "role":           "portfolio" if role == "posición" else "watchlist",
            "price":          data["current"],
            "display_price":  data.get("current_in_purchase_currency") or data.get("current"),
            "currency":       data.get("currency", ""),
            "purchase_currency": data.get("purchase_currency", ""),
            "display_currency": data.get("purchase_currency") or data.get("currency", ""),
            "change_pct":     data["change_pct"],
            "rsi":            data.get("rsi"),
            "ema20":          data.get("ema20"),
            "ema50":          data.get("ema50"),
            "ema20_in_purchase_currency": data.get("ema20_in_purchase_currency"),
            "ema50_in_purchase_currency": data.get("ema50_in_purchase_currency"),
            "atr":            data.get("atr"),
            "atr_in_purchase_currency": data.get("atr_in_purchase_currency"),
            "ema_swing":      data.get("ema_swing"),
            "triggers":       [ema_alert.get("title")],
            "recommendation": "—",
            "analysis":       ema_alert.get("message"),
            "type":           "ema_swing",
            "ema_alert_type": ema_alert.get("type"),
            "severity":       ema_alert.get("severity"),
        })
        if len(alerts_log) > MAX_ALERTS_LOG:
            alerts_log[:] = alerts_log[-MAX_ALERTS_LOG:]
        print(f"[SENT] EMA alert ({ema_alert.get('type')}) for {ticker}")

    # ── Alertas de precio ────────────────────────────────────────────────────
    if data["triggers"] and triggers_changed(ticker, data["triggers"], data["current"], portfolio_state, thresholds):
        signal = synthesize_signal(data)

        # Noticias por ticker solo si están activadas.
        # En tu configuración estable deben estar desactivadas para no gastar Marketaux por cada ticker.
        price_articles = []
        if portfolio_state.get("news_alerts", True):
            lookback = portfolio_state.get("news_lookback_hours", NEWS_LOOKBACK_HOURS)
            price_articles = get_news_marketaux(ticker, data["name"], lookback_hours=lookback)

        analysis = get_claude_analysis(data, data["triggers"], price_articles, role) if use_claude else None
        msg      = format_telegram_message(data, data["triggers"], analysis, role, signal)

        if use_claude:
            send_telegram(msg)
        else:
            send_telegram_with_button(msg, ticker)

        portfolio_state.setdefault("last_alerts", {})[ticker] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggers": data["triggers"],
            "price": data["current"],
        }

        alerts_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "name": data["name"],
            "role": "portfolio" if role == "posición" else "watchlist",
            "price": data["current"],
            "currency": data.get("currency", ""),             # divisa de cotización
            "purchase_currency": data.get("purchase_currency", ""),  # divisa de compra
            "change_pct": data["change_pct"],
            "pnl_pct": data.get("pnl_pct"),
            "pnl_value": data.get("pnl_value"),               # en divisa de compra
            "purchase_price": data.get("purchase_price"),
            "rsi": data.get("rsi"),
            "ma20": data.get("ma20"),
            "ma50": data.get("ma50"),
            "ema20": data.get("ema20"),
            "ema50": data.get("ema50"),
            "atr": data.get("atr"),
            "ema_swing": data.get("ema_swing"),
            "week52_high": data.get("week52_high"),
            "week52_low": data.get("week52_low"),
            "triggers": data["triggers"],
            "signal": signal,
            "recommendation": extract_recommendation(analysis),
            "analysis": analysis,
            "type": "price",
        })

        if len(alerts_log) > MAX_ALERTS_LOG:
            alerts_log[:] = alerts_log[-MAX_ALERTS_LOG:]

        print(f"[SENT] Price alert for {ticker}")

        # Reutilizar artículos ya descargados para la sección de noticias
        if portfolio_state.get("news_alerts", True):
            news_assessment = check_news(ticker, data["name"], portfolio_state, use_claude, articles=price_articles)
            if news_assessment:
                tag = "📁 CARTERA" if role == "posición" else "👁 RADAR"
                first_url = next((a["url"] for a in price_articles if a.get("url")), "")
                url_line = f"\n[Ver noticia →]({first_url})" if first_url else ""
                sent_line = _sentiment_line(price_articles)
                msg_news = f"📰 *{ticker}* — {data['name']}\n{tag}\n{sent_line}\n\n{news_assessment}{url_line}"
                send_telegram(msg_news)
                alerts_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "ticker": ticker,
                    "name": data["name"],
                    "price": data["current"],
                    "change_pct": data["change_pct"],
                    "pnl_pct": data.get("pnl_pct"),
                    "triggers": ["Noticias relevantes"],
                    "recommendation": "—",
                    "analysis": news_assessment,
                    "type": "news",
                })
                print(f"[SENT] News alert (bundled) for {ticker}")

        update_ema_swing_last(ticker, data, portfolio_state)
        return

    elif data["triggers"]:
        print(f"[SKIP] {ticker}: same triggers as last alert, skipping")

    # ── Alertas de noticias sin alerta de precio activa ──────────────────────
    if not portfolio_state.get("news_alerts", True):
        update_ema_swing_last(ticker, data, portfolio_state)
        return

    lookback = portfolio_state.get("news_lookback_hours", NEWS_LOOKBACK_HOURS)
    news_articles = get_news_marketaux(ticker, data["name"], lookback_hours=lookback)
    news_assessment = check_news(ticker, data["name"], portfolio_state, use_claude, articles=news_articles)

    if news_assessment:
        tag = "📁 CARTERA" if role == "posición" else "👁 RADAR"
        first_url = next((a["url"] for a in news_articles if a.get("url")), "")
        url_line = f"\n[Ver noticia →]({first_url})" if first_url else ""
        sent_line = _sentiment_line(news_articles)
        msg = f"📰 *{ticker}* — {data['name']}\n{tag}\n{sent_line}\n\n{news_assessment}{url_line}"
        send_telegram(msg)
        alerts_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "name": data["name"],
            "price": data["current"],
            "change_pct": data["change_pct"],
            "pnl_pct": data.get("pnl_pct"),
            "triggers": ["Noticias relevantes"],
            "recommendation": "—",
            "analysis": news_assessment,
            "type": "news",
        })

        if len(alerts_log) > MAX_ALERTS_LOG:
            alerts_log[:] = alerts_log[-MAX_ALERTS_LOG:]

        print(f"[SENT] News alert for {ticker}")

    update_ema_swing_last(ticker, data, portfolio_state)


def fetch_global_news(lookback_hours: int = 10, limit: int = 25) -> list[dict]:
    """Obtiene noticias financieras globales de Marketaux sin filtro de ticker."""
    if not MARKETAUX_API_KEY:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M")

    try:
        r = requests.get(MARKETAUX_BASE, params={
            "api_token":      MARKETAUX_API_KEY,
            "filter_entities": "true",
            "language":       "en",
            "published_after": cutoff,
            "sort":           "relevance_score",
            "limit":          limit,
        }, timeout=15)
        raw = r.json().get("data", [])
    except Exception as e:
        print(f"[WARN] fetch_global_news failed: {e}")
        return []

    articles = []
    for a in raw:
        entities = a.get("entities", [])
        scores = [float(e.get("sentiment_score", 0)) for e in entities]
        sentiment = round(sum(scores) / len(scores), 3) if scores else 0.0
        category = next((e.get("type", "") for e in entities if e.get("type")), "")

        articles.append({
            "title":        a.get("title", ""),
            "description":  (a.get("description") or "")[:250],
            "url":          a.get("url", ""),
            "source":       _parse_source(a.get("source")),
            "published_at": a.get("published_at", ""),
            "sentiment":    sentiment,
            "category":     category,
        })

    return articles

def send_daily_briefing() -> None:
    """Envía el resumen diario de mercados por Telegram."""
    now_utc  = datetime.now(timezone.utc)
    hour_utc = now_utc.hour

    lookback = 14 if hour_utc < 12 else 8
    session_emoji = "🌅" if hour_utc < 12 else "🌆"
    session_label = "Apertura" if hour_utc < 12 else "Cierre europeo"

    print(f"[BRIEFING] Fetching global news (last {lookback}h)…")
    articles = fetch_global_news(lookback_hours=lookback)
    if not articles:
        print("[BRIEFING] No articles — aborting.")
        return

    scores = [a["sentiment"] for a in articles if a["sentiment"] != 0]
    global_score = round(sum(scores) / len(scores), 3) if scores else 0.0
    global_emoji = _sentiment_emoji(global_score)
    global_label = "Positivo" if global_score >= 0.25 else "Negativo" if global_score <= -0.25 else "Neutral"

    articles_block = "\n\n".join(
        f"{i+1}. [{a['source']}] {a['title']}"
        + (f"\n   {a['description']}" if a["description"] else "")
        for i, a in enumerate(articles)
    )

    prompt = f"""Eres un analista financiero de mercados globales. Analiza estas noticias financieras publicadas en las últimas {lookback} horas.

NOTICIAS ({len(articles)} artículos):
{articles_block}

Tu tarea:
1. Selecciona las 5-7 noticias más relevantes para inversores en bolsa.
2. Para cada una, escribe en UNA frase el posible impacto en mercados.
3. Termina con un bloque "ANÁLISIS" de 3 frases: sentimiento general, sectores/mercados a vigilar hoy, y principal riesgo o catalizador.

Responde con este formato exacto:
NOTICIAS CLAVE:
• [emoji] Resumen de noticia e impacto
• ...

ANÁLISIS:
Texto del análisis.

Responde en español. Sé directo y concreto."""

    print("[BRIEFING] Calling Claude for market summary…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = msg.content[0].text.strip()

    date_str = now_utc.strftime("%d/%m/%Y %H:%M UTC")
    text = (
        f"{session_emoji} *RESUMEN DE MERCADOS — {session_label}*\n"
        f"_{date_str}_\n"
        f"📊 Sentimiento global: {global_emoji} *{global_label}* ({global_score:+.2f})\n\n"
        f"{summary}"
    )
    send_telegram(text)
    print(f"[BRIEFING] Sent. Articles analyzed: {len(articles)}")


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    print(f"[RUN] {now_utc.isoformat()}")

    config = load_json(PORTFOLIO_FILE, {})
    thresholds = config.get("thresholds", {})
    alerts_log = load_json(ALERTS_LOG_FILE, [])
    prev_state = load_json(STATE_FILE, {})
    swing_history = load_json(SWING_HISTORY_FILE, {"version": 1, "last_update": None, "tickers": {}})

    portfolio_state = {
        "last_update": now_utc.isoformat(),
        "use_claude": config.get("use_claude", True),
        "news_alerts": config.get("news_alerts", True),
        "news_lookback_hours": config.get("news_lookback_hours", NEWS_LOOKBACK_HOURS),
        "urgent_news_alerts": config.get("urgent_news_alerts", False),
        "urgent_news_interval_minutes": config.get("urgent_news_interval_minutes", URGENT_NEWS_INTERVAL_MINUTES),
        "urgent_news_lookback_minutes": config.get("urgent_news_lookback_minutes", URGENT_NEWS_LOOKBACK_MINUTES),
        # Configuración alertas EMA (leída desde portfolio.json)
        "ema_alerts":                   config.get("ema_alerts", True),
        "ema_alert_min_score":          config.get("ema_alert_min_score", 70),
        "ema_alert_realert_hours":      config.get("ema_alert_realert_hours", 12),
        "ema_alert_realert_move_pct":   config.get("ema_alert_realert_move_pct", 3.0),
        "ema_alert_extension_pct":      config.get("ema_alert_extension_pct", 10.0),
        "ema_alert_score_improve_from": config.get("ema_alert_score_improve_from", 60),
        "ema_alert_score_improve_to":   config.get("ema_alert_score_improve_to", 75),
        "ema_alert_score_drop_from":    config.get("ema_alert_score_drop_from", 70),
        "ema_alert_score_drop_to":      config.get("ema_alert_score_drop_to", 45),
        "tickers": {},
        # Estado persistente — general
        "last_alerts":           prev_state.get("last_alerts", {}),
        "seen_headlines":        prev_state.get("seen_headlines", {}),
        "urgent_news_seen":      prev_state.get("urgent_news_seen", []),
        "last_urgent_news_check": prev_state.get("last_urgent_news_check"),
        "last_update_id":        prev_state.get("last_update_id", 0),
        # Estado persistente — EMA (imprescindible para anti-spam entre ejecuciones)
        "ema_swing_last":  prev_state.get("ema_swing_last", {}),
        "last_ema_alerts": prev_state.get("last_ema_alerts", {}),
    }

    # ── Callbacks pendientes del botón "Analizar con Claude" ─────────────────
    process_callback_queries(portfolio_state, config)

    fx_cache: dict = {}

    # Conjunto de tickers activos para marcar inactivos en swing_history
    active_tickers: set[str] = set()
    for item in config.get("portfolio", []):
        sym = item["ticker"] if isinstance(item, dict) else item
        active_tickers.add(sym)
    for item in config.get("watchlist", []):
        sym = item["ticker"] if isinstance(item, dict) else item
        active_tickers.add(sym)

    for item in config.get("portfolio", []):
        process_ticker(item, thresholds, "posición", portfolio_state, alerts_log, fx_cache,
                       swing_history=swing_history)
        time.sleep(8)

    for item in config.get("watchlist", []):
        process_ticker(item, thresholds, "seguimiento", portfolio_state, alerts_log, fx_cache,
                       swing_history=swing_history)
        time.sleep(8)

    check_urgent_news(config, portfolio_state, alerts_log)

    mark_inactive_swing_history_tickers(swing_history, active_tickers)

    save_json(STATE_FILE, portfolio_state)
    save_json(ALERTS_LOG_FILE, alerts_log)
    save_json(SWING_HISTORY_FILE, swing_history)
    print(f"[DONE] State saved. Alerts logged: {len(alerts_log)}")


if __name__ == "__main__":
    if "--briefing" in sys.argv:
        send_daily_briefing()
    else:
        main()