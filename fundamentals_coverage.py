"""
fundamentals_coverage.py — F2.0
Diagnostica qué métricas fundamentales pueden obtenerse automáticamente
para cada ticker de la cartera y watchlist.

Genera coverage_report.json con el nivel de cobertura por ticker:
  COMPLETE     >= 14 de 18 métricas derivables automáticamente
  PARTIAL      >= 6  de 18 métricas derivables automáticamente
  MANUAL_ONLY  <  6  métricas derivables (actualización manual recomendada)
  ERROR        fallo al obtener datos (ticker inválido o sin soporte)

Fuentes:
  - yfinance  (gratuito, sin clave, primera fuente)
  - FMP       (Financial Modeling Prep, solo si FMP_API_KEY está en entorno)

F2.0 no modifica: fundamentals.py, fundamentals.json,
                  fundamentals_state.json, LCS Stock Monitor.html
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("[ERROR] yfinance no instalado. Ejecuta: pip install yfinance")
    sys.exit(1)

# ── Constantes ────────────────────────────────────────────────────────────────

PORTFOLIO_FILE = "portfolio.json"
OUTPUT_FILE    = "coverage_report.json"

# Las 18 métricas que consume fundamentals.py
METRICS: list[str] = [
    "revenue_growth_yoy",
    "eps_growth_yoy",
    "fcf_growth_yoy",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "roe",
    "roic",
    "fcf_margin",
    "debt_to_equity",
    "net_debt_ebitda",
    "interest_coverage",
    "current_ratio",
    "forward_pe",
    "peg",
    "ev_ebitda",
    "price_sales",
    "price_fcf",
]

COMPLETE_THRESHOLD = 14   # ≥ 14/18 → COMPLETE
PARTIAL_THRESHOLD  = 6    # ≥  6/18 → PARTIAL; < 6 → MANUAL_ONLY


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


def _num(v) -> float | None:
    """Convierte a float o devuelve None si es None, NaN o no numérico."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (f != f) else f   # NaN → None
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    """Convierte fracción [0,1] a porcentaje redondeado."""
    n = _num(v)
    return round(n * 100, 2) if n is not None else None


def _looks_premium_or_blocked(text: str) -> bool:
    """Detecta respuestas FMP que indican restricción de plan o bloqueo."""
    s = (text or "").lower()
    return (
        "premium" in s
        or "upgrade" in s
        or "not available" in s
        or ("limit" in s and "plan" in s)
    )


def _fmp_get_json(url: str, endpoint_name: str) -> tuple:
    """
    Realiza una petición GET a un endpoint FMP y devuelve (data, error).
    No lanza excepciones hacia fuera; todos los fallos se capturan y se
    devuelven como string de error.

    Posibles valores de error:
      - "<endpoint>: PREMIUM_OR_BLOCKED"
      - "<endpoint>: HTTP_<código>"
      - "<endpoint>: EMPTY"
      - "<endpoint>: JSON inválido: <detalle>"
      - "<endpoint>: <excepción>"
    """
    try:
        import requests as req
    except ImportError:
        return None, f"{endpoint_name}: requests no instalado"

    try:
        r    = req.get(url, timeout=12)
        text = r.text or ""

        if _looks_premium_or_blocked(text):
            return None, f"{endpoint_name}: PREMIUM_OR_BLOCKED"

        if not r.ok:
            return None, f"{endpoint_name}: HTTP_{r.status_code}"

        try:
            data = r.json()
        except Exception as exc:
            return None, f"{endpoint_name}: JSON inválido: {exc}"

        if not data:
            return None, f"{endpoint_name}: EMPTY"

        return data, None

    except Exception as exc:
        return None, f"{endpoint_name}: {exc}"


# ── Extracción yfinance ───────────────────────────────────────────────────────

def fetch_yfinance(ticker: str) -> dict:
    """
    Consulta yfinance para el ticker y mapea los campos disponibles a las 18
    métricas estándar.  Devuelve {values, derivable, error}.

    Campos no disponibles directamente en .info:
      - fcf_growth_yoy   (requiere histórico de cashflow, no en .info)
      - roic             (no expuesto en .info; se estima si hay datos de balance)
      - interest_coverage (no expuesto en .info; requiere EBIT / interest expense)
    """
    values = {m: None for m in METRICS}
    error  = None

    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}

        if not info or info.get("trailingPE") is None and info.get("marketCap") is None:
            # Ticker desconocido: yfinance devuelve un dict casi vacío
            raise ValueError(f"yfinance devolvió datos vacíos para {ticker!r}")

        # ── Crecimiento ───────────────────────────────────────────────────────
        values["revenue_growth_yoy"] = _pct(info.get("revenueGrowth"))
        values["eps_growth_yoy"]     = _pct(info.get("earningsGrowth"))
        # fcf_growth_yoy: no hay campo directo; se necesita histórico → None

        # ── Márgenes ──────────────────────────────────────────────────────────
        values["gross_margin"]     = _pct(info.get("grossMargins"))
        values["operating_margin"] = _pct(info.get("operatingMargins"))
        values["net_margin"]       = _pct(info.get("profitMargins"))

        # ── Rentabilidad ──────────────────────────────────────────────────────
        values["roe"] = _pct(info.get("returnOnEquity"))

        # ROIC: no disponible en .info; intento de estimación:
        #   ROIC ≈ returnOnAssets * (1 + debtToEquity_ratio)  — muy aproximado
        # Lo dejamos None para no introducir valores erróneos.
        values["roic"] = None

        # FCF margin: freeCashflow / totalRevenue
        fcf = _num(info.get("freeCashflow"))
        rev = _num(info.get("totalRevenue"))
        if fcf is not None and rev is not None and rev != 0:
            values["fcf_margin"] = round((fcf / rev) * 100, 2)

        # ── Balance ───────────────────────────────────────────────────────────
        # yfinance devuelve debtToEquity en distintas unidades según versión:
        # algunos builds lo dan como ratio (0.4), otros como % (40).
        # Heurística: si > 20 probablemente es % → dividir entre 100.
        dte = _num(info.get("debtToEquity"))
        if dte is not None:
            values["debt_to_equity"] = round(dte / 100, 4) if dte > 20 else round(dte, 4)

        # Net Debt / EBITDA: (totalDebt - totalCash) / ebitda
        td     = _num(info.get("totalDebt"))
        cash   = _num(info.get("totalCash"))
        ebitda = _num(info.get("ebitda"))
        if td is not None and cash is not None and ebitda is not None and ebitda != 0:
            values["net_debt_ebitda"] = round((td - cash) / ebitda, 2)

        # interest_coverage: EBIT / interestExpense — no en .info → None
        values["interest_coverage"] = None

        values["current_ratio"] = _num(info.get("currentRatio"))
        if values["current_ratio"] is not None:
            values["current_ratio"] = round(values["current_ratio"], 2)

        # ── Valoración ────────────────────────────────────────────────────────
        values["forward_pe"]  = _num(info.get("forwardPE"))
        if values["forward_pe"] is not None:
            values["forward_pe"] = round(values["forward_pe"], 2)

        values["peg"]         = _num(info.get("pegRatio"))
        if values["peg"] is not None:
            values["peg"] = round(values["peg"], 2)

        values["ev_ebitda"]   = _num(info.get("enterpriseToEbitda"))
        if values["ev_ebitda"] is not None:
            values["ev_ebitda"] = round(values["ev_ebitda"], 2)

        values["price_sales"] = _num(info.get("priceToSalesTrailing12Months"))
        if values["price_sales"] is not None:
            values["price_sales"] = round(values["price_sales"], 2)

        # Price/FCF: marketCap / freeCashflow
        mc = _num(info.get("marketCap"))
        if mc is not None and fcf is not None and fcf > 0:
            values["price_fcf"] = round(mc / fcf, 2)

    except Exception as exc:
        error = str(exc)

    derivable = [m for m in METRICS if values[m] is not None]
    return {"values": values, "derivable": derivable, "error": error}


# ── Extracción FMP (opcional) ─────────────────────────────────────────────────

# F2.1: ampliar yfinance con t.income_stmt, t.balance_sheet, t.cashflow para
# calcular fcf_growth_yoy, roic e interest_coverage sin depender de FMP.

def fetch_fmp(ticker: str, api_key: str) -> dict:
    """
    Consulta Financial Modeling Prep para complementar huecos de yfinance.
    Solo se invoca si FMP_API_KEY está definido en el entorno.

    Cada endpoint se prueba de forma independiente: un fallo en uno no bloquea
    los demás.  Endpoints consultados:
      - ratios-ttm       → márgenes, rentabilidad, balance, valoración
      - financial-growth → tasas de crecimiento YoY
      - key-metrics-ttm  → net_debt_ebitda, roic (fallback), fcf_margin

    Devuelve {values, derivable, error, endpoints_ok, endpoints_failed}.
    """
    values: dict            = {m: None for m in METRICS}
    errors: list[str]       = []
    endpoints_ok: list[str]     = []
    endpoints_failed: list[str] = []

    BASE = "https://financialmodelingprep.com/api/v3"

    # ── Ratios TTM ────────────────────────────────────────────────────────────
    ratios_data, ratios_error = _fmp_get_json(
        f"{BASE}/ratios-ttm/{ticker}?apikey={api_key}", "ratios-ttm"
    )

    if ratios_error:
        errors.append(ratios_error)
        endpoints_failed.append("ratios-ttm")
    else:
        endpoints_ok.append("ratios-ttm")
        d = ratios_data[0] if isinstance(ratios_data, list) else ratios_data

        if isinstance(d, dict):
            values["gross_margin"]     = _pct(d.get("grossProfitMarginTTM"))
            values["operating_margin"] = _pct(d.get("operatingProfitMarginTTM"))
            values["net_margin"]       = _pct(d.get("netProfitMarginTTM"))
            values["roe"]              = _pct(d.get("returnOnEquityTTM"))
            values["roic"]             = _pct(d.get("returnOnCapitalEmployedTTM"))

            values["debt_to_equity"] = _num(d.get("debtEquityRatioTTM"))
            if values["debt_to_equity"] is not None:
                values["debt_to_equity"] = round(values["debt_to_equity"], 4)

            values["current_ratio"] = _num(d.get("currentRatioTTM"))
            if values["current_ratio"] is not None:
                values["current_ratio"] = round(values["current_ratio"], 2)

            values["interest_coverage"] = _num(d.get("interestCoverageTTM"))
            if values["interest_coverage"] is not None:
                values["interest_coverage"] = round(values["interest_coverage"], 2)

            # En FMP esto es P/E TTM, no forward P/E.
            # Se usa como proxy solo para diagnóstico de cobertura.
            # F2.1/F2.2: separar en forward_pe y pe_ttm si se adopta el esquema.
            values["forward_pe"] = _num(d.get("peRatioTTM"))
            if values["forward_pe"] is not None:
                values["forward_pe"] = round(values["forward_pe"], 2)

            values["peg"] = _num(d.get("priceEarningsToGrowthRatioTTM"))
            if values["peg"] is not None:
                values["peg"] = round(values["peg"], 2)

            values["ev_ebitda"] = _num(d.get("enterpriseValueMultipleTTM"))
            if values["ev_ebitda"] is not None:
                values["ev_ebitda"] = round(values["ev_ebitda"], 2)

            values["price_sales"] = _num(d.get("priceToSalesRatioTTM"))
            if values["price_sales"] is not None:
                values["price_sales"] = round(values["price_sales"], 2)

            values["price_fcf"] = _num(d.get("priceToFreeCashFlowsRatioTTM"))
            if values["price_fcf"] is not None:
                values["price_fcf"] = round(values["price_fcf"], 2)

    # ── Financial Growth ──────────────────────────────────────────────────────
    growth_data, growth_error = _fmp_get_json(
        f"{BASE}/financial-growth/{ticker}?limit=1&apikey={api_key}", "financial-growth"
    )

    if growth_error:
        errors.append(growth_error)
        endpoints_failed.append("financial-growth")
    else:
        endpoints_ok.append("financial-growth")
        g = growth_data[0] if isinstance(growth_data, list) else growth_data

        if isinstance(g, dict):
            values["revenue_growth_yoy"] = _pct(g.get("revenueGrowth"))
            values["eps_growth_yoy"]     = _pct(g.get("epsgrowth"))
            values["fcf_growth_yoy"]     = _pct(g.get("freeCashFlowGrowth"))

    # ── Key Metrics TTM ───────────────────────────────────────────────────────
    key_metrics_data, key_metrics_error = _fmp_get_json(
        f"{BASE}/key-metrics-ttm/{ticker}?apikey={api_key}", "key-metrics-ttm"
    )

    if key_metrics_error:
        errors.append(key_metrics_error)
        endpoints_failed.append("key-metrics-ttm")
    else:
        endpoints_ok.append("key-metrics-ttm")
        k = key_metrics_data[0] if isinstance(key_metrics_data, list) else key_metrics_data

        if isinstance(k, dict):
            nde = _num(k.get("netDebtToEBITDATTM"))
            if nde is not None:
                values["net_debt_ebitda"] = round(nde, 2)

            # ROIC desde key-metrics si no vino de ratios.
            if values["roic"] is None:
                roic_km = _num(k.get("roicTTM"))
                if roic_km is not None:
                    values["roic"] = round(roic_km * 100, 2)

            # FCF margin: freeCashFlowPerShareTTM / revenuePerShareTTM.
            fcfps = _num(k.get("freeCashFlowPerShareTTM"))
            revps = _num(k.get("revenuePerShareTTM"))
            if fcfps is not None and revps is not None and revps != 0:
                values["fcf_margin"] = round((fcfps / revps) * 100, 2)

    derivable = [m for m in METRICS if values[m] is not None]

    return {
        "values":            values,
        "derivable":         derivable,
        "error":             "; ".join(errors) if errors else None,
        "endpoints_ok":      endpoints_ok,
        "endpoints_failed":  endpoints_failed,
    }


# ── Fusión de fuentes ─────────────────────────────────────────────────────────

def merge_sources(yf_res: dict, fmp_res: dict | None) -> dict:
    """
    Combina yfinance y FMP.  FMP rellena huecos donde yfinance devolvió None.
    yfinance prevalece cuando ambas fuentes tienen valor.
    """
    if fmp_res is None:
        return yf_res

    merged = dict(yf_res["values"])
    for m in METRICS:
        if merged[m] is None and fmp_res["values"].get(m) is not None:
            merged[m] = fmp_res["values"][m]

    derivable = [m for m in METRICS if merged[m] is not None]
    errors    = [e for e in (yf_res.get("error"), fmp_res.get("error")) if e]
    return {
        "values":    merged,
        "derivable": derivable,
        "error":     "; ".join(errors) if errors else None,
    }


# ── Clasificación ─────────────────────────────────────────────────────────────

def classify_coverage(n: int, error: str | None) -> str:
    if error and n == 0:
        return "ERROR"
    if n >= COMPLETE_THRESHOLD:
        return "COMPLETE"
    if n >= PARTIAL_THRESHOLD:
        return "PARTIAL"
    return "MANUAL_ONLY"


# ── Recomendación por métrica faltante ────────────────────────────────────────

# Campos estructuralmente difíciles para yfinance/FMP free tier
HARD_METRICS = {
    "fcf_growth_yoy":   "Requiere dos años de cashflow histórico; calcular desde yf.Ticker.cashflow",
    "roic":             "No expuesto en .info; calcular como NOPAT / Capital Invertido desde balance",
    "interest_coverage":"Requiere EBIT e intereses pagados; disponible en FMP income statement",
}


def recommendation(missing: list[str]) -> str:
    hard = [m for m in missing if m in HARD_METRICS]
    soft = [m for m in missing if m not in HARD_METRICS]
    parts = []
    if soft:
        parts.append(f"Completar manualmente: {', '.join(soft)}")
    if hard:
        details = "; ".join(f"{m} ({HARD_METRICS[m]})" for m in hard)
        parts.append(f"Métricas derivables con lógica adicional: {details}")
    return " | ".join(parts) if parts else "Sin huecos"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Cargar cartera y watchlist
    portfolio_data = load_json(PORTFOLIO_FILE, {})
    tickers: list[str] = []
    for item in portfolio_data.get("portfolio", []):
        t = (item.get("ticker") or "").strip()
        if t and t not in tickers:
            tickers.append(t)
    for t in portfolio_data.get("watchlist", []):
        t = (t or "").strip()
        if t and t not in tickers:
            tickers.append(t)

    if not tickers:
        print(f"[ERROR] No se encontraron tickers en {PORTFOLIO_FILE}")
        sys.exit(1)

    fmp_key = os.environ.get("FMP_API_KEY", "").strip() or None
    sources = ["yfinance"] + (["fmp"] if fmp_key else [])

    print(f"[INFO] Diagnóstico de cobertura fundamental")
    print(f"[INFO] Tickers: {len(tickers)} | Fuentes: {', '.join(sources)}")
    print(f"[INFO] Umbrales: COMPLETE>={COMPLETE_THRESHOLD}  PARTIAL>={PARTIAL_THRESHOLD}  MANUAL_ONLY<{PARTIAL_THRESHOLD}")
    print()

    report_tickers: dict = {}
    summary = {"COMPLETE": 0, "PARTIAL": 0, "MANUAL_ONLY": 0, "ERROR": 0}

    for ticker in tickers:
        print(f"  [{ticker}]", end=" ", flush=True)

        yf_res  = fetch_yfinance(ticker)
        fmp_res = fetch_fmp(ticker, fmp_key) if fmp_key else None
        merged  = merge_sources(yf_res, fmp_res)

        n_derivable = len(merged["derivable"])
        coverage    = classify_coverage(n_derivable, merged["error"])
        summary[coverage] += 1

        missing = [m for m in METRICS if merged["values"][m] is None]
        rec     = recommendation(missing)

        label_pad = f"{coverage} ({n_derivable}/{len(METRICS)})"
        print(label_pad)
        if merged["error"]:
            print(f"     ⚠ Error: {merged['error']}")
        if missing:
            print(f"     Faltantes: {', '.join(missing)}")

        report_tickers[ticker] = {
            "coverage":              coverage,
            "derivable_count":       n_derivable,
            "total_metrics":         len(METRICS),
            "derivable_metrics":     merged["derivable"],
            "missing_metrics":       missing,
            "recommendation":        rec,
            "auto_values":           merged["values"],
            "sources_used":          sources,
            "yf_error":              yf_res.get("error"),
            "fmp_error":             fmp_res.get("error")             if fmp_res else None,
            "fmp_endpoints_ok":      fmp_res.get("endpoints_ok")      if fmp_res else [],
            "fmp_endpoints_failed":  fmp_res.get("endpoints_failed")  if fmp_res else [],
        }

    report = {
        "version":      1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "complete":    COMPLETE_THRESHOLD,
            "partial":     PARTIAL_THRESHOLD,
        },
        "sources_used":  sources,
        "summary":       summary,
        "tickers":       report_tickers,
    }

    save_json(OUTPUT_FILE, report)

    total = len(tickers)
    print()
    print(f"[OK] {OUTPUT_FILE} generado — {total} tickers procesados")
    print(f"     COMPLETE={summary['COMPLETE']}  PARTIAL={summary['PARTIAL']}"
          f"  MANUAL_ONLY={summary['MANUAL_ONLY']}  ERROR={summary['ERROR']}")


if __name__ == "__main__":
    main()
