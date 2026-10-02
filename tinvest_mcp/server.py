"""T-Invest MCP server — market data, portfolio and trading tools for AI clients.

Подключается к ИИ-клиенту (Claude и др.) по протоколу MCP через stdio.
Токен и счёт — из переменных окружения TINKOFF_TOKEN / TINKOFF_ACCOUNT_ID.
Торговые инструменты включены по умолчанию; выключить: TINVEST_ALLOW_TRADING=0.
"""
from __future__ import annotations

import logging
import os
import uuid

from mcp.server.fastmcp import FastMCP

from .client import TinkoffClient

log = logging.getLogger("tinvest-mcp")

mcp = FastMCP("tinvest")

_client_cache: dict = {}


def get_client() -> TinkoffClient:
    token = os.getenv("TINKOFF_TOKEN", "") or os.getenv("TINVEST_TOKEN", "")
    account_id = os.getenv("TINKOFF_ACCOUNT_ID", "") or os.getenv("TINVEST_ACCOUNT_ID", "")
    sandbox = os.getenv("TINVEST_SANDBOX", "0").strip() in ("1", "true", "yes")
    if not token:
        raise RuntimeError("TINKOFF_TOKEN not set")
    key = (token, account_id, sandbox)
    if key not in _client_cache:
        _client_cache[key] = TinkoffClient(token=token, account_id=account_id, sandbox=sandbox)
    return _client_cache[key]


ALLOW_TRADING = os.getenv("TINVEST_ALLOW_TRADING", "1").strip() in ("1", "true", "yes")


def require_trading():
    if not ALLOW_TRADING:
        raise RuntimeError("Trading disabled. Set TINVEST_ALLOW_TRADING=1")


@mcp.tool()
def get_candles(ticker: str, interval: str = "1h", bars: int = 100) -> dict:
    """Свечи фьючерса MOEX (OHLCV, время UTC). ticker — код фьючерса (например NGZ6); interval: 1m, 5m, 15m, 1h, 4h, 1d; bars — сколько последних свечей. / Candles of a MOEX future."""
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    candles = client.get_candles(spec.figi, interval, bars=bars)
    return {"ticker": ticker, "interval": interval, "bars": len(candles), "candles": candles}


@mcp.tool()
def get_last_price(ticker: str) -> dict:
    """Последняя цена фьючерса (в пунктах). / Last price of a future."""
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    prices = client.get_last_prices([spec.figi])
    price = prices.get(spec.figi, 0)
    return {"ticker": ticker, "price": price}


@mcp.tool()
def get_orderbook(ticker: str, depth: int = 20) -> dict:
    """Стакан заявок фьючерса; depth — число уровней (по умолчанию 20). / Order book."""
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    book = client.get_orderbook(spec.figi, depth=depth)
    return {"ticker": ticker, "orderbook": book}


@mcp.tool()
def get_instrument_specs(ticker: str) -> dict:
    """Спецификация фьючерса: гарантийное обеспечение, шаг цены, стоимость шага, дата экспирации. / Instrument specification."""
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    full = client.get_future_full(spec.figi)
    return {"ticker": ticker, "figi": spec.figi, "spec": full}


# ═══════════════════════════════════════════
# Портфель и счёт
# ═══════════════════════════════════════════

@mcp.tool()
def get_portfolio() -> dict:
    """Портфель выбранного счёта: позиции, доходность, маржа. / Portfolio of the selected account."""
    client = get_client()
    return client.get_portfolio()


@mcp.tool()
def get_active_orders() -> dict:
    """Активные лимитные заявки и стоп-ордера счёта. / Active orders and stop orders."""
    client = get_client()
    orders = client.get_orders()
    stops = client.get_stop_orders()
    return {"orders": orders, "stop_orders": stops, "total": len(orders) + len(stops)}


@mcp.tool()
def get_free_deposit() -> dict:
    """Ликвидный портфель, начальная маржа и свободная маржа для новых сделок (руб.). / Free margin."""
    client = get_client()
    ma = client.get_margin_attributes()
    liquid = float(ma.get("liquid_portfolio", 0) or 0)
    starting = float(ma.get("starting_margin", 0) or 0)
    free = max(0, liquid - starting)
    return {"liquid_portfolio": liquid, "starting_margin": starting, "free_margin": free}


@mcp.tool()
def get_operations(ticker: str = "", days: int = 7) -> dict:
    """История операций за days дней; ticker необязателен (пусто — все инструменты). / Operations history."""
    client = get_client()
    figi = None
    if ticker:
        specs = client.resolve_futures([ticker.upper()])
        spec = specs.get(ticker.upper())
        figi = spec.figi if spec else None
    ops = client.get_operations(days=days, figi=figi)
    return ops


@mcp.tool()
def get_phase() -> dict:
    """Текущая фаза торгов на MOEX (сессия, клиринг, закрытие). / Trading phase."""
    client = get_client()
    return client.get_trading_status()


# ═══════════════════════════════════════════
# Размер позиции
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# Ордера
# ═══════════════════════════════════════════

@mcp.tool()
def place_market_order(ticker: str, direction: str, qty: int) -> dict:
    """Рыночный ордер. ТОРГОВЛЯ НА РЕАЛЬНОМ СЧЁТЕ. direction: buy или sell; qty — количество контрактов (лотов). / Market order (real money)."""
    require_trading()
    if direction.lower() not in ("buy", "sell"):
        return {"error": "direction: buy or sell"}
    if qty <= 0:
        return {"error": "qty > 0"}
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    oid = str(uuid.uuid4())
    resp = client.place_market_order(spec.figi, direction.lower(), qty, oid)
    return {"ok": True, "ticker": ticker, "kind": "market", "order": resp}


@mcp.tool()
def place_limit_order(ticker: str, direction: str, qty: int, price: float) -> dict:
    """Лимитный ордер. ТОРГОВЛЯ НА РЕАЛЬНОМ СЧЁТЕ. direction: buy или sell; qty — контрактов; price — цена в пунктах. / Limit order (real money)."""
    require_trading()
    if direction.lower() not in ("buy", "sell"):
        return {"error": "direction: buy or sell"}
    if qty <= 0 or price <= 0:
        return {"error": "qty and price > 0"}
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    oid = str(uuid.uuid4())
    resp = client.place_limit_order(spec.figi, direction.lower(), qty, float(price), oid)
    return {"ok": True, "ticker": ticker, "kind": "limit", "price": price, "order": resp}


@mcp.tool()
def place_stop_loss(ticker: str, direction: str, qty: int, stop_price: float) -> dict:
    """Стоп-ордер (после срабатывания исполняется по рынку). direction — сторона самого ордера: sell для защиты лонга, buy для защиты шорта; stop_price — цена срабатывания. / Stop-loss order."""
    require_trading()
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    resp = client.place_stop_loss(spec.figi, direction.lower(), qty, stop_price)
    return {"ok": True, "ticker": ticker, "kind": "stop_loss", "stop_price": stop_price, "order": resp}


@mcp.tool()
def place_take_profit(ticker: str, direction: str, qty: int, take_price: float) -> dict:
    """Тейк-профит. direction — сторона самого ордера: sell для закрытия лонга, buy для закрытия шорта; take_price — цена срабатывания. / Take-profit order."""
    require_trading()
    client = get_client()
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    resp = client.place_take_profit(spec.figi, direction.lower(), qty, take_price)
    return {"ok": True, "ticker": ticker, "kind": "take_profit", "take_price": take_price, "order": resp}


@mcp.tool()
def cancel_order(order_id: str) -> dict:
    """Отменить лимитную заявку или стоп-ордер по ID (ID берётся из get_active_orders). / Cancel an order by ID."""
    require_trading()
    client = get_client()
    try:
        client.cancel_order(order_id)
        return {"ok": True, "kind": "order", "order_id": order_id}
    except Exception as e1:
        first_err = str(e1)
    try:
        client.cancel_stop_order(order_id)
        return {"ok": True, "kind": "stop_order", "order_id": order_id}
    except Exception as e2:
        return {"error": f"order: {first_err}; stop_order: {e2}"}


@mcp.tool()
def close_position(ticker: str) -> dict:
    """Закрыть всю позицию по тикеру рыночным ордером (противоположной стороной). ТОРГОВЛЯ НА РЕАЛЬНОМ СЧЁТЕ. / Close a position at market."""
    require_trading()
    client = get_client()
    portfolio = client.get_portfolio()
    positions = portfolio.get("raw", {}).get("positions", [])
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}
    target = None
    for p in positions:
        if p.get("figi") == spec.figi:
            target = p
            break
    if not target:
        return {"error": f"No position for {ticker}"}
    qty_units = int(target.get("quantity", {}).get("units", 0))
    if qty_units == 0:
        return {"error": "Position quantity is 0"}
    direction = "sell" if qty_units > 0 else "buy"
    abs_qty = abs(qty_units)
    oid = str(uuid.uuid4())
    resp = client.place_market_order(spec.figi, direction, abs_qty, oid)
    return {"ok": True, "ticker": ticker, "quantity_closed": abs_qty, "result": resp}




@mcp.tool()
def calculate_position_size(ticker: str, direction: str,
                             entry_price: float, stop_price: float,
                             risk_percent: float = 1.0) -> dict:
    """Размер позиции по риску: risk_percent % ликвидного портфеля / (расстояние до стопа × стоимость пункта).
    Возвращает qty (лоты), risk_rub, risk_pct."""
    client = get_client()

    # Ликвидный портфель
    try:
        ma = client.get_margin_attributes()
        liquid = float(ma.get("liquid_portfolio") or 0)
    except Exception:
        liquid = 0

    if liquid <= 0:
        return {"error": "Не удалось получить размер депозита (liquid_portfolio = 0)"}

    # Спецификация инструмента (мультипликатор)
    specs = client.resolve_futures([ticker.upper()])
    spec = specs.get(ticker.upper())
    if not spec:
        return {"error": f"{ticker} not found"}

    full = client.get_future_full(spec.figi)
    mpi = float(full.get("min_price_increment") or 0)
    mpia = float(full.get("min_price_increment_amount") or 0)
    multiplier = (mpia / mpi) if mpi else 0

    if multiplier <= 0:
        return {"error": f"Не удалось определить мультипликатор для {ticker}"}

    price_diff = abs(entry_price - stop_price)
    if price_diff <= 0:
        return {"error": "entry_price == stop_price"}

    risk_rub_target = liquid * (risk_percent / 100)
    risk_per_lot = price_diff * multiplier
    qty = max(1, int(risk_rub_target / risk_per_lot))
    actual_risk = risk_per_lot * qty

    return {
        "ticker": ticker,
        "qty": qty,
        "risk_rub": round(actual_risk, 2),
        "risk_pct": round(actual_risk / liquid * 100, 3),
        "deposit_liquid": round(liquid, 2),
        "multiplier": multiplier,
        "price_diff": price_diff,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
