"""
fundamentals.py — Fase F1
Lee fundamentals.json, calcula scores cualitativos y cruza con Swing Score del estado técnico.
Genera fundamentals_state.json.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

FUNDAMENTALS_FILE       = "fundamentals.json"
STATE_FILE              = "portfolio_state.json"
FUNDAMENTALS_STATE_FILE = "fundamentals_state.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _get(f: dict, key: str) -> float | None:
    v = f.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Sub-scores 0–100 ──────────────────────────────────────────────────────────

def score_growth(f: dict) -> float | None:
    """Puntúa crecimiento: revenue, EPS, FCF YoY."""
    points: list[float] = []

    rev = _get(f, "revenue_growth_yoy")
    if rev is not None:
        if   rev >= 30:  points.append(100)
        elif rev >= 15:  points.append(80)
        elif rev >= 8:   points.append(60)
        elif rev >= 0:   points.append(40)
        else:            points.append(15)

    eps = _get(f, "eps_growth_yoy")
    if eps is not None:
        if   eps >= 30:  points.append(100)
        elif eps >= 15:  points.append(80)
        elif eps >= 5:   points.append(60)
        elif eps >= 0:   points.append(40)
        else:            points.append(15)

    fcf = _get(f, "fcf_growth_yoy")
    if fcf is not None:
        if   fcf >= 25:  points.append(100)
        elif fcf >= 10:  points.append(75)
        elif fcf >= 0:   points.append(50)
        else:            points.append(20)

    return round(sum(points) / len(points), 1) if points else None


def score_profitability(f: dict) -> float | None:
    """Puntúa rentabilidad: márgenes, ROE, ROIC, FCF margin."""
    points: list[float] = []

    gross = _get(f, "gross_margin")
    if gross is not None:
        if   gross >= 60:  points.append(100)
        elif gross >= 40:  points.append(80)
        elif gross >= 25:  points.append(60)
        elif gross >= 12:  points.append(40)
        else:              points.append(20)

    op = _get(f, "operating_margin")
    if op is not None:
        if   op >= 25:  points.append(100)
        elif op >= 15:  points.append(80)
        elif op >= 8:   points.append(55)
        elif op >= 3:   points.append(35)
        else:           points.append(10)

    net = _get(f, "net_margin")
    if net is not None:
        if   net >= 20:  points.append(100)
        elif net >= 10:  points.append(75)
        elif net >= 5:   points.append(50)
        elif net >= 0:   points.append(30)
        else:            points.append(5)

    roe = _get(f, "roe")
    if roe is not None:
        if   roe >= 30:  points.append(100)
        elif roe >= 20:  points.append(80)
        elif roe >= 12:  points.append(55)
        elif roe >= 5:   points.append(35)
        else:            points.append(15)

    roic = _get(f, "roic")
    if roic is not None:
        if   roic >= 25:  points.append(100)
        elif roic >= 15:  points.append(75)
        elif roic >= 8:   points.append(50)
        elif roic >= 3:   points.append(30)
        else:             points.append(10)

    fcfm = _get(f, "fcf_margin")
    if fcfm is not None:
        if   fcfm >= 20:  points.append(100)
        elif fcfm >= 10:  points.append(75)
        elif fcfm >= 5:   points.append(50)
        elif fcfm >= 0:   points.append(30)
        else:             points.append(10)

    return round(sum(points) / len(points), 1) if points else None


def score_balance(f: dict) -> float | None:
    """Puntúa solidez del balance: deuda, cobertura, liquidez."""
    points: list[float] = []

    dte = _get(f, "debt_to_equity")
    if dte is not None:
        if   dte < 0:      points.append(100)  # caja neta
        elif dte <= 0.3:   points.append(95)
        elif dte <= 0.8:   points.append(75)
        elif dte <= 1.5:   points.append(55)
        elif dte <= 3.0:   points.append(35)
        else:              points.append(15)

    nde = _get(f, "net_debt_ebitda")
    if nde is not None:
        if   nde < 0:     points.append(100)   # caja neta
        elif nde <= 0.5:  points.append(90)
        elif nde <= 1.5:  points.append(70)
        elif nde <= 3.0:  points.append(45)
        elif nde <= 5.0:  points.append(25)
        else:             points.append(10)

    ic = _get(f, "interest_coverage")
    if ic is not None:
        if   ic >= 20:  points.append(100)
        elif ic >= 8:   points.append(80)
        elif ic >= 4:   points.append(55)
        elif ic >= 2:   points.append(30)
        else:           points.append(10)

    cr = _get(f, "current_ratio")
    if cr is not None:
        if   cr >= 2.0:  points.append(100)
        elif cr >= 1.3:  points.append(75)
        elif cr >= 1.0:  points.append(50)
        elif cr >= 0.7:  points.append(30)
        else:            points.append(10)

    return round(sum(points) / len(points), 1) if points else None


def score_valuation(f: dict) -> float | None:
    """Puntúa valoración: cuanto más barata, mayor score."""
    points: list[float] = []

    fpe = _get(f, "forward_pe")
    if fpe is not None:
        if   fpe < 0:      points.append(20)   # pérdidas
        elif fpe <= 12:    points.append(100)
        elif fpe <= 18:    points.append(80)
        elif fpe <= 25:    points.append(60)
        elif fpe <= 35:    points.append(40)
        else:              points.append(20)

    peg = _get(f, "peg")
    if peg is not None:
        if   peg <= 0:     points.append(20)
        elif peg <= 1.0:   points.append(100)
        elif peg <= 1.5:   points.append(80)
        elif peg <= 2.5:   points.append(55)
        elif peg <= 3.5:   points.append(35)
        else:              points.append(15)

    ev_eb = _get(f, "ev_ebitda")
    if ev_eb is not None:
        if   ev_eb <= 8:   points.append(100)
        elif ev_eb <= 14:  points.append(75)
        elif ev_eb <= 20:  points.append(55)
        elif ev_eb <= 30:  points.append(35)
        else:              points.append(15)

    ps = _get(f, "price_sales")
    if ps is not None:
        if   ps <= 1.0:  points.append(100)
        elif ps <= 3.0:  points.append(75)
        elif ps <= 6.0:  points.append(50)
        elif ps <= 12:   points.append(30)
        else:            points.append(15)

    pfcf = _get(f, "price_fcf")
    if pfcf is not None:
        if   pfcf <= 10:  points.append(100)
        elif pfcf <= 18:  points.append(80)
        elif pfcf <= 25:  points.append(60)
        elif pfcf <= 35:  points.append(40)
        else:             points.append(20)

    return round(sum(points) / len(points), 1) if points else None


def score_momentum(f: dict) -> float | None:
    """Puntúa el momentum fundamental declarado."""
    m = str(f.get("fundamental_momentum") or "").upper()
    table = {
        "MEJORANDO":    85,
        "ESTABLE":      60,
        "MIXED":        45,
        "DETERIORANDO": 20,
    }
    return float(table[m]) if m in table else None


def calculate_fundamental_scores(f: dict) -> dict:
    """Calcula todos los sub-scores y el Fundamental Score ponderado."""
    g  = score_growth(f)
    pr = score_profitability(f)
    ba = score_balance(f)
    va = score_valuation(f)
    mo = score_momentum(f)

    weights = {"growth": 0.25, "profitability": 0.25, "balance": 0.20, "valuation": 0.20, "momentum": 0.10}
    values  = {"growth": g, "profitability": pr, "balance": ba, "valuation": va, "momentum": mo}

    total_w, weighted_sum = 0.0, 0.0
    for key, w in weights.items():
        v = values[key]
        if v is not None:
            weighted_sum += v * w
            total_w      += w

    fundamental = round(clamp(weighted_sum / total_w * (1 / 1)), 1) if total_w > 0 else None

    return {
        "fundamental_score": fundamental,
        "growth_score":      round(g,  1) if g  is not None else None,
        "profitability_score": round(pr, 1) if pr is not None else None,
        "balance_score":     round(ba, 1) if ba is not None else None,
        "valuation_score":   round(va, 1) if va is not None else None,
        "momentum_score":    round(mo, 1) if mo is not None else None,
    }


# ── Classification ────────────────────────────────────────────────────────────

def classify_fundamental(f: dict, scores: dict) -> str:
    fs  = scores.get("fundamental_score")
    vs  = scores.get("valuation_score")
    gs  = scores.get("growth_score")
    bs  = scores.get("balance_score")
    mom = str(f.get("fundamental_momentum") or "").upper()
    dte = _get(f, "debt_to_equity")
    nde = _get(f, "net_debt_ebitda")

    if fs is None:
        return "Sin datos"

    # Deuda crítica: D/E alto solo es Debt Risk si además el servicio es malo
    ic   = _get(f, "interest_coverage")
    de_bad  = dte is not None and dte > 4.0 and (ic is None or ic < 4.0)
    nde_bad = nde is not None and nde > 5.0
    if de_bad or nde_bad:
        return "Debt Risk"

    if fs >= 75:
        if vs is not None and vs >= 55:
            return "Quality Compounder"
        if gs is not None and gs >= 75:
            return "Growth Leader"
        return "Overvalued Quality"

    if fs >= 55:
        if vs is not None and vs >= 70:
            return "Value Candidate"
        if mom == "DETERIORANDO":
            return "Value Trap"
        return "Mixed"

    if fs >= 35:
        if vs is not None and vs >= 70:
            return "Value Candidate"
        return "Speculative"

    return "Speculative"


# ── Combined Score ────────────────────────────────────────────────────────────

def combined_with_swing(scores: dict, ticker_state: dict | None) -> dict:
    fs = scores.get("fundamental_score")
    swing_score = None
    if ticker_state:
        es = ticker_state.get("ema_swing") or {}
        swing_score = es.get("score")

    # Combined Score: 55% fundamental + 35% swing + 10% reservado
    combined = None
    if fs is not None and swing_score is not None:
        combined = round(clamp(fs * 0.55 + swing_score * 0.35), 1)
    elif fs is not None:
        combined = round(clamp(fs * 0.55 / 0.55), 1)  # normalizado si no hay swing

    # Etiqueta combinada
    label = "NEUTRAL"
    action_bias = "—"

    if fs is not None and swing_score is not None:
        if fs >= 75 and swing_score >= 70:
            label       = "PRIORIDAD_ALTA"
            action_bias = "Calidad + setup técnico"
        elif fs >= 75 and swing_score < 50:
            label       = "VIGILAR"
            action_bias = "Buena empresa, mal timing"
        elif fs < 50 and swing_score >= 70:
            label       = "SOLO_TACTICO"
            action_bias = "Técnico fuerte, fundamental débil"
        elif fs < 50 and swing_score < 50:
            label       = "EVITAR"
            action_bias = "Débil por técnico y fundamental"
    elif fs is not None:
        if fs >= 75:
            label       = "VIGILAR"
            action_bias = "Sin datos técnicos"
        elif fs < 50:
            label       = "EVITAR"
            action_bias = "Fundamental débil"

    return {
        "combined_score":  combined,
        "combined_label":  label,
        "action_bias":     action_bias,
        "swing_score_used": swing_score,
    }


# ── Summary ───────────────────────────────────────────────────────────────────

def build_summary(f: dict, scores: dict, combined: dict) -> str:
    parts: list[str] = []

    fs  = scores.get("fundamental_score")
    gs  = scores.get("growth_score")
    ps  = scores.get("profitability_score")
    bs  = scores.get("balance_score")
    vs  = scores.get("valuation_score")
    lab = classify_fundamental(f, scores)
    cb  = combined.get("combined_label", "NEUTRAL")
    ab  = combined.get("action_bias", "")

    if fs is None:
        return "Datos fundamentales insuficientes para generar resumen."

    # Calidad general
    if fs >= 75:
        parts.append("Fundamentales sólidos.")
    elif fs >= 55:
        parts.append("Fundamentales moderados.")
    else:
        parts.append("Fundamentales débiles.")

    # Crecimiento
    if gs is not None:
        if   gs >= 80:  parts.append("Crecimiento fuerte.")
        elif gs >= 55:  parts.append("Crecimiento moderado.")
        else:           parts.append("Crecimiento bajo o negativo.")

    # Rentabilidad
    if ps is not None:
        if   ps >= 80:  parts.append("Alta rentabilidad.")
        elif ps >= 55:  parts.append("Rentabilidad aceptable.")
        else:           parts.append("Rentabilidad presionada.")

    # Balance
    if bs is not None:
        if   bs >= 80:  parts.append("Balance saneado.")
        elif bs >= 55:  parts.append("Endeudamiento controlado.")
        else:           parts.append("Balance con presión de deuda.")

    # Valoración
    if vs is not None:
        if   vs >= 80:  parts.append("Valoración atractiva.")
        elif vs >= 55:  parts.append("Valoración razonable.")
        else:           parts.append("Valoración exigente.")

    # Etiqueta + acción
    parts.append(f"Etiqueta: {lab}.")
    if ab and ab != "—":
        parts.append(ab + ".")

    return " ".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    fund_data = load_json(FUNDAMENTALS_FILE, {})
    state     = load_json(STATE_FILE, {})
    state_tickers = state.get("tickers") or {}

    tickers_in = fund_data.get("tickers") or {}
    if not tickers_in:
        print("[WARN] fundamentals.json está vacío o sin tickers.")
        return

    result_tickers: dict = {}

    for ticker, f in tickers_in.items():
        scores   = calculate_fundamental_scores(f)
        label    = classify_fundamental(f, scores)

        # Buscar datos técnicos (Swing Score)
        ts = state_tickers.get(ticker)
        if ts is None:
            # Intentar búsqueda insensible a casing
            for k, v in state_tickers.items():
                if k.upper() == ticker.upper():
                    ts = v
                    break

        combined = combined_with_swing(scores, ts)
        summary  = build_summary(f, scores, combined)

        result_tickers[ticker] = {
            "ticker":   ticker,
            "name":     f.get("name", ticker),
            "label":    label,
            "summary":  summary,
            # sub-scores
            "fundamental_score":    scores["fundamental_score"],
            "growth_score":         scores["growth_score"],
            "profitability_score":  scores["profitability_score"],
            "balance_score":        scores["balance_score"],
            "valuation_score":      scores["valuation_score"],
            "momentum_score":       scores["momentum_score"],
            # combined
            "combined_score":       combined["combined_score"],
            "combined_label":       combined["combined_label"],
            "action_bias":          combined["action_bias"],
            "swing_score_used":     combined["swing_score_used"],
            # metadata
            "fundamental_momentum": f.get("fundamental_momentum"),
            "notes":                f.get("notes", ""),
            "updated_at":           fund_data.get("updated_at"),
        }

        print(f"  {ticker:12s}  F={scores['fundamental_score']:5}  "
              f"V={scores['valuation_score']:5}  "
              f"C={combined['combined_score']:5}  "
              f"{label}")

    fundamentals_state = {
        "version":    1,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "tickers":    result_tickers,
    }

    save_json(FUNDAMENTALS_STATE_FILE, fundamentals_state)
    print(f"[OK] fundamentals_state.json updated: {len(result_tickers)} tickers")


if __name__ == "__main__":
    main()
