import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

STATE_FILE         = "portfolio_state.json"
SWING_HISTORY_FILE = "swing_history.json"
ANALYSIS_LOG_FILE  = "analysis_log.json"

CLAUDE_MODEL            = "claude-sonnet-4-6"
MAX_HISTORY_ROWS_FOR_PROMPT = 90
MAX_ANALYSIS_LOG        = 100

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")


# ── JSON helpers ─────────────────────────────────────────────────────────────

def load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: str, data) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Data helpers ──────────────────────────────────────────────────────────────

def find_ticker(ticker: str, state: dict) -> dict | None:
    ticker = ticker.upper().strip()
    tickers = state.get("tickers") or {}
    if ticker in tickers:
        return tickers[ticker]
    for item in tickers.values():
        if str(item.get("ticker", "")).upper() == ticker:
            return item
    return None


def get_history(ticker: str, data: dict, swing_history: dict) -> list[dict]:
    ticker = ticker.upper().strip()
    sh_entry = (swing_history.get("tickers") or {}).get(ticker)
    if sh_entry and sh_entry.get("history"):
        return sh_entry["history"]
    return data.get("history") or []


# ── Prompt ────────────────────────────────────────────────────────────────────

def build_ema_prompt(ticker_data: dict, history: list[dict]) -> str:
    s = ticker_data.get("ema_swing") or {}

    recent = history[-MAX_HISTORY_ROWS_FOR_PROMPT:]
    compact_history = [
        {
            "date":   row.get("date"),
            "open":   row.get("open"),
            "high":   row.get("high"),
            "low":    row.get("low"),
            "close":  row.get("close"),
            "volume": row.get("volume"),
            "ema20":  row.get("ema20"),
            "ema50":  row.get("ema50"),
            "atr":    row.get("atr"),
            "rsi":    row.get("rsi"),
            "setup":  row.get("setup"),
            "score":  row.get("score"),
        }
        for row in recent
    ]

    payload = {
        "ticker":                        ticker_data.get("ticker"),
        "name":                          ticker_data.get("name"),
        "role":                          ticker_data.get("role"),
        "currency":                      ticker_data.get("purchase_currency") or ticker_data.get("currency"),
        "current":                       ticker_data.get("current"),
        "current_in_purchase_currency":  ticker_data.get("current_in_purchase_currency"),
        "change_pct":                    ticker_data.get("change_pct"),
        "purchase_price":                ticker_data.get("purchase_price"),
        "purchase_currency":             ticker_data.get("purchase_currency"),
        "shares":                        ticker_data.get("shares"),
        "pnl_pct":                       ticker_data.get("pnl_pct"),
        "pnl_value":                     ticker_data.get("pnl_value"),
        "ema20":                         ticker_data.get("ema20"),
        "ema50":                         ticker_data.get("ema50"),
        "ema20_in_purchase_currency":    ticker_data.get("ema20_in_purchase_currency"),
        "ema50_in_purchase_currency":    ticker_data.get("ema50_in_purchase_currency"),
        "atr":                           ticker_data.get("atr"),
        "atr_in_purchase_currency":      ticker_data.get("atr_in_purchase_currency"),
        "rsi":                           ticker_data.get("rsi"),
        "volume":                        ticker_data.get("volume"),
        "avg_volume":                    ticker_data.get("avg_volume"),
        "ema_swing":                     s,
        "history":                       compact_history,
    }

    return f"""Eres un analista técnico especializado en swing trading con estrategia EMA 20/50.

Analiza este activo usando exclusivamente la información proporcionada.
No inventes datos externos.
No des una predicción garantizada.
No presentes esto como asesoramiento financiero personalizado.
Usa un tono claro, práctico y orientado a gestión de riesgo.

Estrategia:
- EMA20 por encima de EMA50 favorece estructura alcista.
- Pullback hacia EMA20 puede ser setup de continuación.
- Pérdida de EMA50 indica deterioro de estructura.
- Precio demasiado alejado de EMA20 indica riesgo de sobreextensión.
- El volumen ayuda a confirmar o invalidar el movimiento.
- ATR sirve para pensar en stop/riesgo.
- RSI ayuda a detectar sobrecompra/sobreventa, pero no manda sobre la estructura.

Debes responder exactamente con estas secciones:

1. Diagnóstico técnico
2. Lectura EMA 20/50
3. Setup actual
4. Riesgo principal
5. Nivel de invalidación
6. Plan de actuación
7. Recomendación final

En la recomendación final elige solo una:
COMPRAR, ESPERAR, MANTENER, REDUCIR, VENDER, EVITAR.

Datos del activo en JSON:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


# ── Claude ────────────────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada")

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":           api_key,
            "anthropic-version":   "2023-06-01",
            "content-type":        "application/json",
        },
        json={
            "model":      CLAUDE_MODEL,
            "max_tokens": 1500,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )

    if not resp.ok:
        raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:400]}")

    parts = resp.json().get("content") or []
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram no configurado")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok  = True

    for chunk in [text[i:i + 3500] for i in range(0, len(text), 3500)]:
        try:
            r = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
                timeout=20,
            )
            if not r.ok:
                print(f"[ERROR] Telegram failed: {r.status_code} {r.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[ERROR] Telegram exception: {e}")
            ok = False

    return ok


# ── Log ───────────────────────────────────────────────────────────────────────

def append_analysis_log(ticker: str, strategy: str, prompt: str, analysis: str) -> None:
    log = load_json(ANALYSIS_LOG_FILE, [])
    log.insert(0, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker":    ticker,
        "strategy":  strategy,
        "model":     CLAUDE_MODEL,
        "prompt":    prompt,
        "analysis":  analysis,
        "source":    "github_actions",
    })
    save_json(ANALYSIS_LOG_FILE, log[:MAX_ANALYSIS_LOG])


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Análisis EMA 20/50 con Claude")
    parser.add_argument("--ticker",   required=True)
    parser.add_argument("--strategy", default="ema_20_50")
    args = parser.parse_args()

    ticker   = args.ticker.upper().strip()
    strategy = args.strategy

    state         = load_json(STATE_FILE, {})
    swing_history = load_json(SWING_HISTORY_FILE, {"version": 1, "last_update": None, "tickers": {}})

    ticker_data = find_ticker(ticker, state)
    if not ticker_data:
        raise RuntimeError(f"Ticker no encontrado en {STATE_FILE}: {ticker}")

    history  = get_history(ticker, ticker_data, swing_history)
    prompt   = build_ema_prompt(ticker_data, history)
    analysis = call_claude(prompt)

    header = f"🤖 Análisis EMA 20/50 — {ticker}\nModelo: {CLAUDE_MODEL}\n\n"
    send_telegram(header + analysis)

    append_analysis_log(ticker, strategy, prompt, analysis)
    print(f"[OK] Analysis generated for {ticker}")


if __name__ == "__main__":
    main()
