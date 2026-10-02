"""Tinkoff Invest API REST client — thin wrapper over httpx."""
from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("tinkoff")

_MSK = timezone(timedelta(hours=3))


def _is_weekend_msk(t) -> bool:
    """Суббота или воскресенье по МСК (26.09.2026). t — datetime или ISO-строка брокера;
    непонятное время — не выходной (свечу не теряем).
    Фильтр включается только TINVEST_DROP_WEEKENDS=1; по умолчанию данные отдаются как есть."""
    if os.getenv("TINVEST_DROP_WEEKENDS", "0").strip() not in ("1", "true", "yes"):
        return False
    try:
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if t is None:
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(_MSK).weekday() >= 5
    except Exception:
        return False

BASE = "https://invest-public-api.tinkoff.ru/rest"
SBX_BASE = "https://sandbox-invest-public-api.tinkoff.ru/rest"
# Виртуальные стопы песочницы (11.07.2026) — см. блок «Sandbox: эмуляция
# стоп-ордеров» в TinkoffClient. Файл общий для web и бота, запись атомарная.
_STATE_DIR = os.getenv("TINVEST_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".tinvest-mcp")
_VSTOPS_PATH = os.path.join(_STATE_DIR, "sandbox_vstops.json")
_VSTOPS_LOCK = threading.Lock()

# 15.07.2026: TTL (сек) кэша списка фьючерсов — см. TinkoffClient.get_futures_lookup.
# Список фьючерсов почти не меняется внутри дня; кэш убирает 429-шторм на
# InstrumentsService.Futures (раньше дёргался на КАЖДЫЙ тик из ~10 мест).
_FUT_LOOKUP_TTL = 600

SERVICE_MAP = {
    "InstrumentsService": "tinkoff.public.invest.api.contract.v1.InstrumentsService",
    "MarketDataService": "tinkoff.public.invest.api.contract.v1.MarketDataService",
    "OperationsService": "tinkoff.public.invest.api.contract.v1.OperationsService",
    "OrdersService": "tinkoff.public.invest.api.contract.v1.OrdersService",
    "StopOrdersService": "tinkoff.public.invest.api.contract.v1.StopOrdersService",
    "UsersService": "tinkoff.public.invest.api.contract.v1.UsersService",
    "SandboxService": "tinkoff.public.invest.api.contract.v1.SandboxService",
}


def _money(m) -> float:
    if m is None:
        return 0.0
    if isinstance(m, (int, float)):
        return float(m)
    if isinstance(m, dict):
        units = int(m.get("units", 0))
        nano = int(m.get("nano", 0))
        # round(…, 9): нано — ровно 9 знаков; без округления сумма давала хвост
        # плавающей точки (3.2640000000000002 против 3.264 у ISS, сверка NGU6
        # 26.09.2026) — сравнения «закрытие за уровнем» шли на этой пыли
        return round(units + nano / 1e9, 9)
    return float(m)


_quot = _money


@dataclass
class InstrumentSpec:
    ticker: str
    figi: str
    uid: str = ""
    name: str = ""
    logo_url: str = ""
    lot: int = 1
    min_price_increment: float = 0.0
    min_price_increment_amount: float = 0.0
    currency: str = "rub"
    exchange: str = ""
    instrument_type: str = "futures"


@dataclass
class Position:
    ticker: str
    figi: str
    quantity: int
    avg_price: float
    current_price: float
    instrument_uid: str = ""
    expected_yield: float = 0.0
    var_margin: float = 0.0
    instrument_type: str = ""
    currency: str = "rub"


class TinkoffClient:
    def __init__(self, token: str = "", account_id: str = "", sandbox: bool = False):
        import os
        self.token = token or os.getenv("TINKOFF_TOKEN", "")
        self.account_id = account_id or os.getenv("TINKOFF_ACCOUNT_ID", "")
        self.sandbox = sandbox
        self.base = SBX_BASE if sandbox else BASE
        self._http = httpx.Client(
            base_url=self.base,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15.0,
        )
        # 15.07.2026: кэш списка фьючерсов (см. get_futures_lookup) — раньше
        # без кэша InstrumentsService.Futures дёргался на КАЖДЫЙ тик → 429.
        self._fut_lookup_cache: tuple[dict, dict] | None = None
        self._fut_lookup_ts = 0.0
        self._fut_lookup_lock = threading.Lock()

    def _call(self, service: str, method: str, body: dict | None = None,
              _retries: int = 3) -> dict:
        """Аудит S5.1 (18.06.2026): автоповтор при сбоях сети/5xx с паузой.
        Ордера безопасны для повтора — у них orderId как ключ идемпотентности,
        биржа не исполнит дважды. 4xx (мало маржи, плохие параметры) — НЕ
        повторяем, отдаём ошибку сразу."""
        # 11.07.2026: песочница НЕ поддерживает StopOrdersService — эмулируем
        # виртуальными стопами (файл state/sandbox_vstops.json + тиковый разрядник
        # sandbox_poll_virtual_stops в боте). Перехват здесь — чтобы ВЕСЬ код
        # (place_stop_loss, get_stop_orders, сырые CancelStopOrder в боте/web/EOD)
        # работал в песочнице без правок.
        if self.sandbox and service == "StopOrdersService":
            return self._vstops_call(method, body or {})
        svc = SERVICE_MAP.get(service, service)
        url = f"/{svc}/{method}"
        for attempt in range(_retries):
            try:
                r = self._http.post(url, json=body or {})
            except (httpx.TransportError, httpx.TimeoutException) as e:
                # Сеть упала/таймаут — повтор с нарастающей паузой 0.5/1/2 сек
                if attempt < _retries - 1:
                    _time.sleep(0.5 * (2 ** attempt))
                    log.warning("retry %s (сеть: %s), попытка %d/%d", method, e, attempt + 2, _retries)
                    continue
                raise
            if r.status_code >= 500 and attempt < _retries - 1:
                # Биржа отдала 5xx — временный сбой, повтор
                _time.sleep(0.5 * (2 ** attempt))
                log.warning("retry %s (HTTP %d), попытка %d/%d", method, r.status_code, attempt + 2, _retries)
                continue
            if r.status_code >= 400:
                # 4xx — легитимная ошибка биржи, не повторяем. Логируем тело.
                try:
                    err_body = r.json()
                except Exception:
                    err_body = r.text[:500]
                log.error("%s %s -> %d: %s | request: %s", method, svc, r.status_code, err_body, body)
                raise httpx.HTTPStatusError(
                    f"{r.status_code} {err_body}",
                    request=r.request, response=r,
                )
            return r.json()

    # ── Instruments ──
    def resolve_futures(self, tickers: list[str]) -> dict[str, InstrumentSpec]:
        """Спеки по тикерам — ИЗ КЭША get_futures_lookup (15.09.2026). Раньше
        метод дёргал InstrumentsService.Futures напрямую при каждом вызове:
        retest_bot._v2_figi зовёт его на каждую идею каждый тик → 270 ответов
        429 за час (журнал 15.09), и на 429 бот получал пустой figi. Кэш тот
        же, что у резолва позиций (TTL _FUT_LOOKUP_TTL, при сбое — прошлая
        карта)."""
        figi_map, _ = self.get_futures_lookup()
        want = {t.upper() for t in tickers}
        out = {}
        for info in figi_map.values():
            tu = (info.get("ticker") or "").upper()
            if tu in want and tu not in out:
                out[tu] = InstrumentSpec(
                    ticker=tu, figi=info["figi"], uid=info.get("uid", ""),
                    name=info.get("name", ""), logo_url=info.get("logo_url", ""),
                    lot=info.get("lot", 1),
                    min_price_increment=info.get("min_price_increment"),
                    min_price_increment_amount=info.get("min_price_increment_amount"),
                    currency=info.get("currency", "rub"),
                    exchange=info.get("exchange", ""),
                )
        return out

    def get_futures_lookup(self, force: bool = False) -> tuple[dict, dict]:
        """One API call — returns (figi_map, uid_map) with ticker/name/logo_url/specs for all futures.

        15.07.2026 (CCQ6 без стопа/тейка): КЭШ (TTL _FUT_LOOKUP_TTL) + отдача
        ПРОШЛОЙ карты при сбое. Раньше вызывался из ~10 мест БЕЗ кэша →
        InstrumentsService.Futures на КАЖДЫЙ тик → T-Invest резал 429 (а 4xx НЕ
        ретраится → raise) → get_positions не мог сматчить figi→тикер → ручная
        позиция (CCQ6/figi FCOCOA082600) не опознавалась, усыновление её пропускало
        → позиция висела без стопа и тейка. Теперь: свежий кэш отдаём сразу (нет
        запроса → нет 429); при 429/сбое сети отдаём последнюю известную карту
        (пусть протухшую) вместо падения — слегка устаревший список лучше пустого,
        при котором позиции перестают резолвиться. force=True — принудительный обход
        кэша (для явного обновления перечня инструментов)."""
        now = _time.monotonic()
        with self._fut_lookup_lock:
            cache = self._fut_lookup_cache
            if cache is not None and not force and (now - self._fut_lookup_ts) < _FUT_LOOKUP_TTL:
                return cache
        try:
            data = self._call("InstrumentsService", "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        except Exception as e:
            # 429/сеть/5xx: не роняем резолв позиций — отдаём последнюю карту, если есть.
            with self._fut_lookup_lock:
                if self._fut_lookup_cache is not None:
                    log.warning("get_futures_lookup: %s — отдаю кэш (возраст %.0f c)",
                                str(e)[:120], now - self._fut_lookup_ts)
                    return self._fut_lookup_cache
            raise
        figi_map: dict = {}
        uid_map: dict = {}
        for f in data.get("instruments", []):
            figi = f.get("figi", "")
            uid = f.get("uid", "")
            brand = f.get("brand", {})
            logo_name = brand.get("logoName", "")
            logo_base = logo_name.rsplit(".", 1)[0] if logo_name else ""
            info = {
                "ticker": f.get("ticker", ""),
                "figi": figi,
                "uid": uid,
                "name": f.get("name", ""),
                "logo_url": f"https://invest-brands.cdn-tinkoff.ru/{logo_base}x160.png" if logo_base else "",
                "lot": f.get("lot", 1),
                "min_price_increment": _quot(f.get("minPriceIncrement")),
                "min_price_increment_amount": _money(f.get("minPriceIncrementAmount")),
                "currency": f.get("currency", "rub"),
                "exchange": f.get("exchange", ""),
                "basic_asset": f.get("basicAsset", ""),
                "asset_type": f.get("assetType", ""),
                "expiration_date": f.get("expirationDate", ""),
            }
            if figi:
                figi_map[figi] = info
            if uid:
                uid_map[uid] = info
        with self._fut_lookup_lock:
            self._fut_lookup_cache = (figi_map, uid_map)
            self._fut_lookup_ts = now
        return figi_map, uid_map

    def get_stock_lookup(self, kind: str) -> dict:
        """Справочник акций ("shares") или ETF ("etfs") из T-Invest: тикер → имя, figi, лот,
        валюта, иконка (29.09.2026). Кэш 12 часов, при
        сбое — прошлая карта. Один вызов на весь список."""
        with self._fut_lookup_lock:
            _c = getattr(self, "_stock_lookup_cache", None)
            if _c is None:
                _c = self._stock_lookup_cache = {}
            hit = _c.get(kind)
            if hit and (_time.monotonic() - hit[0]) < 12 * 3600:
                return hit[1]
        method = "Etfs" if kind == "etfs" else "Shares"
        try:
            data = self._call("InstrumentsService", method,
                              {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        except Exception:
            with self._fut_lookup_lock:
                if (getattr(self, "_stock_lookup_cache", {}) or {}).get(kind):
                    return self._stock_lookup_cache[kind][1]
            raise
        out: dict = {}
        _kind = "etf" if kind == "etfs" else "shares"
        for s in data.get("instruments", []):
            tk = (s.get("ticker") or "").upper()
            if not tk:
                continue
            logo_name = (s.get("brand") or {}).get("logoName", "")
            logo_base = logo_name.rsplit(".", 1)[0] if logo_name else ""
            _lot = int(s.get("lot", 1) or 1)
            _mpi = _quot(s.get("minPriceIncrement"))
            cur = {
                "ticker": tk, "figi": s.get("figi", ""), "uid": s.get("uid", ""),
                "name": s.get("name", ""),
                "lot": _lot, "currency": s.get("currency", "rub"),
                "class_code": s.get("classCode", ""),
                "exchange": s.get("exchange", ""),
                "logo_url": (f"https://invest-brands.cdn-tinkoff.ru/{logo_base}x160.png"
                             if logo_base else ""),
                # АКЦИИ И ETF КАК ФЬЮЧЕРСЫ (29.09.2026): поля в формате справочника
                # фьючерсов. Стоимость шага на ЛОТ = шаг × лот — тогда весь
                # код, считающий «₽ на пункт» как mpia/mpi, получает лот.
                "kind": _kind,
                "instrument_type": "etf" if _kind == "etf" else "share",
                "min_price_increment": _mpi,
                "min_price_increment_amount": round(_mpi * _lot, 10),
                "short_enabled": bool(s.get("shortEnabledFlag")),
                "dlong": _quot(s.get("dlong")), "dshort": _quot(s.get("dshort")),
            }
            # только основные режимы MOEX: TQBR — акции, TQTF — фонды; прочие
            # (СПБ, внебиржа, неосновные режимы) в терминал не идут
            if cur["class_code"] not in ("TQBR", "TQTF"):
                continue
            out[tk] = cur
        with self._fut_lookup_lock:
            self._stock_lookup_cache[kind] = (_time.monotonic(), out)
        return out

    def get_instrument_lookup(self) -> tuple[dict, dict]:
        """Единый справочник (figi_map, uid_map): фьючерсы + акции TQBR + фонды
        TQTF (29.09.2026). Поля фьючерса у всех;
        у каждой записи kind = futures | shares | etf. Тикер фьючерса при
        совпадении с тикером бумаги побеждает — робот торгует фьючерсы, его
        резолв не должен подмениться. Сбой справочника акций не роняет
        фьючерсы: карта просто без бумаг."""
        f_figi, f_uid = self.get_futures_lookup()
        figi_map = {k: dict(v, kind="futures") for k, v in f_figi.items()}
        uid_map = {k: dict(v, kind="futures") for k, v in f_uid.items()}
        fut_tk = {(v.get("ticker") or "").upper() for v in f_figi.values()}
        for kind in ("shares", "etfs"):
            try:
                lk = self.get_stock_lookup(kind)
            except Exception as e:
                log.warning("get_instrument_lookup %s: %s", kind, str(e)[:120])
                continue
            for tk, info in lk.items():
                if tk in fut_tk or not info.get("figi"):
                    continue
                figi_map[info["figi"]] = info
                if info.get("uid"):
                    uid_map[info["uid"]] = info
        return figi_map, uid_map

    def get_share_tickers(self) -> set[str]:
        """Множество тикеров акций (для определения «фьючерс на акции» по
        basicAsset фьючерса). Возвращает set("SBER", "GAZP", ...)."""
        data = self._call("InstrumentsService", "Shares",
                          {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        out: set[str] = set()
        for s in data.get("instruments", []):
            tk = (s.get("ticker") or "").upper()
            if tk:
                out.add(tk)
        return out

    def search_futures(self, query: str, limit: int = 30) -> list[dict]:
        data = self._call("InstrumentsService", "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        q = query.upper()
        results = []
        for f in data.get("instruments", []):
            tk = (f.get("ticker") or "").upper()
            nm = (f.get("name") or "").upper()
            if q in tk or q in nm:
                results.append({
                    "ticker": f.get("ticker"), "figi": f["figi"],
                    "name": f.get("name"), "lot": f.get("lot", 1),
                    "exchange": f.get("exchange", ""),
                    "currency": f.get("currency", ""),
                })
                if len(results) >= limit:
                    break
        return results

    def get_future_full(self, figi: str) -> dict:
        data = self._call("InstrumentsService", "FutureBy",
                          {"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi})
        inst = data.get("instrument", {})
        brand = inst.get("brand", {})
        logo_name = brand.get("logoName", "")
        logo_base = logo_name.rsplit(".", 1)[0] if logo_name else ""  # strip .png if present
        logo_url = f"https://invest-brands.cdn-tinkoff.ru/{logo_base}x160.png" if logo_base else ""
        return {
            "ticker": inst.get("ticker"), "figi": inst.get("figi"),
            "name": inst.get("name"), "lot": inst.get("lot", 1),
            "min_price_increment": _quot(inst.get("minPriceIncrement")),
            "min_price_increment_amount": _money(inst.get("minPriceIncrementAmount")),
            "basic_asset": inst.get("basicAsset"),
            "basic_asset_size": _quot(inst.get("basicAssetSize")),
            "expiration_date": inst.get("expirationDate"),
            "currency": inst.get("currency"),
            "exchange": inst.get("exchange"),
            "initial_margin_on_buy": _money(inst.get("initialMarginOnBuy")),
            "initial_margin_on_sell": _money(inst.get("initialMarginOnSell")),
            "first_trade_date": inst.get("firstTradeDate"),
            "last_trade_date": inst.get("lastTradeDate"),
            "logo_url": logo_url,
        }

    def list_all_futures(self) -> list[dict]:
        data = self._call("InstrumentsService", "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        return [{"ticker": f.get("ticker"), "figi": f.get("figi"), "name": f.get("name"),
                 "lot": f.get("lot", 1)} for f in data.get("instruments", [])]

    # ── Market data ──
    def get_last_prices(self, figis: list[str]) -> dict[str, float]:
        if not figis:
            return {}
        # ВЫХОДНЫЕ (26.09.2026) — цена в субботу и воскресенье = последнее закрытие будней,
        # а не торги выходного дня (CCX6 26.09: 479.3 против пятничных 479.5)
        if _is_weekend_msk(datetime.now(timezone.utc)):
            return {f: self._weekday_close(f) for f in figis}
        data = self._call("MarketDataService", "GetLastPrices",
                          {"figi": figis, "instrumentId": figis})
        out = {}
        for lp in data.get("lastPrices", []):
            f = lp.get("figi") or lp.get("instrumentUid")
            # понедельник до открытия (00:00–06:50 МСК): последняя сделка —
            # из воскресной сессии; цена = закрытие будней (аудит 27.09.2026)
            if lp.get("time") and _is_weekend_msk(str(lp.get("time"))):
                out[f] = self._weekday_close(f)
                continue
            out[f] = _quot(lp.get("price"))
        return out

    def get_close_prices(self, figis: list[str]) -> dict[str, float]:
        """Цена закрытия ПРОШЛОЙ торговой сессии по каждому figi — база для
        «изменения за день» в избранном (29.09.2026: раньше изменение считалось
        от первой цены, увиденной браузером, то есть от момента открытия
        страницы). {} при сбое — вызывающий показывает прочерк."""
        if not figis:
            return {}
        try:
            data = self._call("MarketDataService", "GetClosePrices",
                              {"instruments": [{"instrumentId": f} for f in figis]})
        except Exception as e:
            log.warning("get_close_prices: %s", str(e)[:120])
            return {}
        out = {}
        for cp in data.get("closePrices", []):
            f = cp.get("figi") or cp.get("instrumentUid")
            px = _quot(cp.get("price"))
            if f and px > 0:
                out[f] = px
        return out

    def _weekday_close(self, figi: str) -> float:
        """Закрытие последней будничной свечи — цена инструмента в выходные.
        Кэш до конца выходных: будни за это время не меняются."""
        _c = getattr(self, "_wd_close", None)
        if _c is None:
            _c = self._wd_close = {}
        _day = datetime.now(timezone.utc).date()
        if figi in _c and _c[figi][0] == _day:
            return _c[figi][1]
        px = 0.0
        try:
            bars = self.get_candles(figi, "1h", bars=24 * 4)
            if bars:
                px = float(bars[-1]["c"] or 0)
        except Exception:
            px = 0.0
        if px:
            _c[figi] = (_day, px)
        return px

    def get_last_price(self, figi: str) -> float:
        """Получить последнюю цену по одному figi."""
        prices = self.get_last_prices([figi])
        return prices.get(figi, 0.0)

    def get_order_book(self, figi: str, depth: int = 20) -> dict:
        data = self._call("MarketDataService", "GetOrderBook",
                          {"figi": figi, "depth": min(depth, 50)})
        bids = [{"price": _quot(b.get("price")), "qty": int(b.get("quantity", 0))}
                for b in data.get("bids", [])]
        asks = [{"price": _quot(a.get("price")), "qty": int(a.get("quantity", 0))}
                for a in data.get("asks", [])]
        return {
            "bids": bids, "asks": asks,
            "last_price": _quot(data.get("lastPrice")),
            "close_price": _quot(data.get("closePrice")),
            "limit_up": _quot(data.get("limitUp")),
            "limit_down": _quot(data.get("limitDown")),
        }

    def get_candles(self, figi: str, tf: str = "1h", bars: int = 300) -> list[dict]:
        tf_map = {
            "1m": ("CANDLE_INTERVAL_1_MIN", 1),
            "5m": ("CANDLE_INTERVAL_5_MIN", 5),
            "15m": ("CANDLE_INTERVAL_15_MIN", 15),
            "1h": ("CANDLE_INTERVAL_HOUR", 60),
            "4h": ("CANDLE_INTERVAL_4_HOUR", 240),
            "1d": ("CANDLE_INTERVAL_DAY", 1440),
        }
        # ТФ в любом регистре (25.09.2026): робот зовёт "1D"/"1H", таблица —
        # строчными; "1D" молча уходил в часовики, дневной ATR был 0
        tf = str(tf or "").lower()
        interval, mins = tf_map.get(tf, ("CANDLE_INTERVAL_HOUR", 60))
        now = datetime.now(timezone.utc)
        delta = timedelta(minutes=mins * bars)
        # API limits: 1m→1day, 5m→1day, 15m→1day, 1h→7days, 4h→30d, 1d→1yr
        frm = (now - delta).isoformat()
        to = now.isoformat()
        data = self._call("MarketDataService", "GetCandles", {
            "figi": figi, "from": frm, "to": to,
            "interval": interval,
        })
        candles = []
        for c in data.get("candles", []):
            if _is_weekend_msk(c.get("time")):
                continue          # торги выходного дня никуда не идут (26.09.2026)
            candles.append({
                "t": c.get("time"), "o": _quot(c.get("open")), "h": _quot(c.get("high")),
                "l": _quot(c.get("low")), "c": _quot(c.get("close")),
                "v": int(c.get("volume", 0)),
            })
        return candles

    def list_futures_all(self) -> list[dict]:
        """ВСЕ фьючерсы вкл. экспирированные (status ALL) — для склейки серий
        контрактов (basic_asset + expiration_date). Read-only."""
        data = self._call("InstrumentsService", "Futures",
                          {"instrumentStatus": "INSTRUMENT_STATUS_ALL"})
        out = []
        for f in data.get("instruments", []):
            out.append({
                "ticker": (f.get("ticker") or "").upper(),
                "figi": f.get("figi") or "",
                "basic_asset": (f.get("basicAsset") or "").strip(),
                "expiration_date": f.get("expirationDate") or "",
            })
        return out

    def get_candles_range(self, figi: str, tf: str, frm_iso: str, to_iso: str) -> list[dict]:
        """Свечи за ЯВНЫЙ интервал [frm,to] (для исторических контрактов серии).
        В отличие от get_candles (всегда «последние N от now») — берёт прошлое окно."""
        tf_map = {
            "1m": "CANDLE_INTERVAL_1_MIN", "5m": "CANDLE_INTERVAL_5_MIN",
            "15m": "CANDLE_INTERVAL_15_MIN", "1h": "CANDLE_INTERVAL_HOUR",
            "4h": "CANDLE_INTERVAL_4_HOUR", "1d": "CANDLE_INTERVAL_DAY",
        }
        interval = tf_map.get(str(tf or "").lower(), "CANDLE_INTERVAL_DAY")
        data = self._call("MarketDataService", "GetCandles", {
            "figi": figi, "from": frm_iso, "to": to_iso, "interval": interval,
        })
        out = []
        for c in data.get("candles", []):
            if _is_weekend_msk(c.get("time")):
                continue          # торги выходного дня никуда не идут (26.09.2026)
            out.append({
                "t": c.get("time"), "o": _quot(c.get("open")), "h": _quot(c.get("high")),
                "l": _quot(c.get("low")), "c": _quot(c.get("close")),
                "v": int(c.get("volume", 0)),
            })
        return out

    def get_last_trades(self, figi: str, minutes: int = 5) -> list[dict]:
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(minutes=minutes)).isoformat()
        data = self._call("MarketDataService", "GetLastTrades",
                          {"figi": figi, "from": frm, "to": now.isoformat()})
        return [{"price": _quot(t.get("price")), "qty": int(t.get("quantity", 0)),
                 "direction": t.get("direction"), "time": t.get("time")}
                for t in data.get("trades", [])]

    def get_trading_status(self, figi: str) -> dict:
        return self._call("MarketDataService", "GetTradingStatus", {"figi": figi})

    # ── Portfolio / positions ──
    # 07.07.2026 (ревью #14): короткий кэш get_portfolio на 2с — снижает нагрузку
    # на OperationsService/GetPortfolio (был участником 429-штормов, из-за которых был
    # инцидент GDU6). В одном тике executor_v2 портфель запрашивается 2 раза (депо-гейт
    # + get_positions() внутри которой get_portfolio()) — теперь второй вызов бесплатный.
    def get_portfolio(self) -> dict:
        now = _time.time()
        if getattr(self, "_pf_cache", None) and (now - self._pf_cache_ts) < 2.0:
            return self._pf_cache
        svc = "SandboxService" if self.sandbox else "OperationsService"
        method = "GetSandboxPortfolio" if self.sandbox else "GetPortfolio"
        data = self._call(svc, method, {"accountId": self.account_id})
        if self.sandbox:
            data = self._sandbox_fix_portfolio(data)
        pf = {
            "total_portfolio": _money(data.get("totalAmountPortfolio")),
            "cash": _money(data.get("totalAmountCurrencies")),
            "unrealized_pnl": _money(data.get("expectedYield")),
            "raw": data,
        }
        self._pf_cache = pf
        self._pf_cache_ts = now
        return pf

    def _sandbox_fix_portfolio(self, data: dict) -> dict:
        """11.07.2026: песочница учитывает фьючерс КАК АКЦИЮ — в
        totalAmountPortfolio входит ПОЛНАЯ стоимость контрактов (деньги при этом
        не списываются) → «сделка плюсуется к счёту»; expectedYield позиций
        нулевой → PnL 0.00 в открытой сделке. Приводим к боевой семантике:
        портфель = деньги + нереализованный PnL фьючей; PnL позиции считаем сами
        (пункты × стоимость шага); занятое ГО оцениваем по initial margin
        контрактов — его отдаёт get_margin_attributes (поле «ГО» в UI)."""
        try:
            cash = _money(data.get("totalAmountCurrencies"))
            try:    # невозвращённая подкачка ГО (см. _sandbox_pay_back) — не наши деньги
                _pd = os.path.join(os.path.dirname(_VSTOPS_PATH), "sandbox_boost_debt.json")
                if os.path.exists(_pd):
                    with open(_pd, encoding="utf-8") as _fh:
                        cash -= float((json.load(_fh) or {}).get("debt", 0))
            except Exception:
                pass
            pnl_sum = 0.0
            go_used = 0.0
            for ps in data.get("positions", []):
                if ps.get("instrumentType") not in ("futures", ""):
                    continue
                qty = _quot(ps.get("quantity", {}))
                if not qty:
                    continue
                avg = _quot(ps.get("averagePositionPriceFifo")) or _quot(ps.get("averagePositionPrice"))
                cur = _quot(ps.get("currentPrice"))
                step = step_amt = im_buy = im_sell = 0.0
                try:
                    info = self.get_instrument_info(ps.get("figi", ""))
                    step = info.get("min_price_increment", 0) or 0
                    step_amt = info.get("min_price_increment_amount", 0) or 0
                    im_buy = info.get("initial_margin_on_buy", 0) or 0
                    im_sell = info.get("initial_margin_on_sell", 0) or 0
                except Exception:
                    pass
                pts_mult = (step_amt / step) if (step and step_amt) else 1.0
                pnl = (cur - avg) * qty * pts_mult
                pnl_sum += pnl
                go_used += abs(qty) * (im_buy if qty > 0 else im_sell)
                if not _money(ps.get("expectedYield")):
                    ps["expectedYield"] = self._price_to_quotation(round(pnl, 2))
                # 14.07.2026: varMargin песочницы — МУСОР (как payment): у
                # прибыльного RIU6 (+1563 ₽) в «СЛЕД. МАРЖА (будет начислено)» показывал
                # −13624 → «закрыл в плюс, а портфель минус». В песочнице клиринга нет,
                # поэтому «маржа к начислению» = наш нереализованный PnL позиции.
                ps["varMargin"] = self._price_to_quotation(round(pnl, 2))
            data["totalAmountPortfolio"] = {**self._price_to_quotation(round(cash + pnl_sum, 2)),
                                            "currency": "rub"}
            data["expectedYield"] = self._price_to_quotation(round(pnl_sum, 2))
            data["_sandbox_go_used"] = round(go_used, 2)
        except Exception as e:
            log.warning("sandbox portfolio fix: %s", e)
        return data

    def get_positions(self, figi_to_ticker: dict[str, str] | None = None) -> list[Position]:
        figi_to_ticker = figi_to_ticker or {}
        p = self.get_portfolio()
        positions = []
        for ps in p["raw"].get("positions", []):
            figi = ps.get("figi", "")
            instrument_uid = ps.get("instrumentUid", "")
            ticker = figi_to_ticker.get(figi, figi_to_ticker.get(instrument_uid, ps.get("ticker", figi)))
            qty_units = int(_quot(ps.get("quantity", {})))
            # КОЛИЧЕСТВО — В ЛОТАХ (29.09.2026, акции как фьючерсы). По акциям
            # и фондам API отдаёт quantity В ШТУКАХ, а весь терминал считает
            # позицию в лотах: закрытие, объём стопа, усыновление. У фьючерса
            # лот = 1 и разницы нет, у акции с лотом 10 стоп вставал бы на
            # 10× объём и разворачивал позицию. Берём quantityLots, иначе
            # делим на лот бумаги.
            if ps.get("instrumentType") in ("share", "etf"):
                _ql = ps.get("quantityLots")
                if _ql is not None:
                    qty_units = int(_quot(_ql))
                else:
                    try:
                        _lot = int(self.get_instrument_info(figi).get("lot") or 1)
                        qty_units = int(qty_units / _lot) if _lot > 1 else qty_units
                    except Exception:
                        pass
            # Берём данные напрямую из T-Invest API — как на сайте T-Invest
            avg = _quot(ps.get("averagePositionPriceFifo")) or _quot(ps.get("averagePositionPrice"))
            cur = _quot(ps.get("currentPrice"))
            ey = _money(ps.get("expectedYield"))
            vm = _money(ps.get("varMargin"))
            # 08.08.2026 («позиции по факту в долларах, почини
            # мультивалютность»): валюта КАЖДОГО Money-поля летит в ответе
            # API, но _quot/_money отбрасывали её, оставляя голое число —
            # терминал (заточен под рублёвые фьючерсы MOEX) везде подписывал
            # ₽, хотя счёт держал доллары (TMF/TLT/GLD/SPXU). Валюту берём с
            # currentPrice — если позиция открыта, оно всегда заполнено;
            # averagePositionPrice* — запасной вариант.
            # 09.08.2026 («что за валюта такая PT»): по фьючерсам (ECD6,
            # NGQ6, CLQ6) API отдаёт currency="pt" — это котировка В ПУНКТАХ
            # (не валюта вовсе), а не рубли. "pt" — не код валюты, фильтруем
            # его так же, как пустое значение → рубль (FORTS маржируется и
            # рассчитывается в рублях независимо от того, что цена в пунктах).
            cur_code = (
                (ps.get("currentPrice") or {}).get("currency")
                or (ps.get("averagePositionPriceFifo") or {}).get("currency")
                or (ps.get("averagePositionPrice") or {}).get("currency")
                or "rub"
            )
            if (cur_code or "").strip().lower().rstrip(".") == "pt":
                cur_code = "rub"
            positions.append(Position(
                ticker=ticker, figi=figi, instrument_uid=instrument_uid,
                quantity=qty_units, avg_price=avg, current_price=cur,
                expected_yield=ey, var_margin=vm,
                instrument_type=ps.get("instrumentType", ""),
                currency=cur_code,
            ))
        return positions

    def get_margin_attributes(self) -> dict:
        if self.sandbox:
            # владелец 11.07: UsersService/GetMarginAttributes в песочнице не работает.
            # Оцениваем сами: ГО = Σ initial margin открытых контрактов (см.
            # _sandbox_fix_portfolio), ликвидный портфель = деньги + PnL фьючей.
            # minimal_margin ≈ половина ГО (как у Тинькофф для фьючерсов).
            pf = self.get_portfolio()
            go = float(pf["raw"].get("_sandbox_go_used") or 0)
            liq = float(pf.get("total_portfolio") or 0)
            return {"liquid_portfolio": liq, "starting_margin": go,
                    "minimal_margin": round(go / 2, 2),
                    "funds_sufficiency": round(liq / go, 2) if go else 0.0,
                    "amount_of_missing_funds": round(go - liq, 2) if go > liq else 0.0}
        data = self._call("UsersService", "GetMarginAttributes",
                          {"accountId": self.account_id})
        return {
            "liquid_portfolio": _money(data.get("liquidPortfolio")),
            "starting_margin": _money(data.get("startingMargin")),
            "minimal_margin": _money(data.get("minimalMargin")),
            "funds_sufficiency": _money(data.get("fundsSufficiencyLevel")),
            "amount_of_missing_funds": _money(data.get("amountOfMissingFunds")),
        }

    def get_withdraw_limits(self) -> dict:
        data = self._call("OperationsService", "GetWithdrawLimits",
                          {"accountId": self.account_id})
        return data

    def get_user_info(self) -> dict:
        return self._call("UsersService", "GetInfo", {})

    def get_accounts(self) -> list[dict]:
        """08.08.2026 («добавление новых счетов по новым токенам не
        работает»): список брокерских счетов, привязанных к self.token.
        Нужен для авто-разрешения Account ID при добавлении счёта в UI —
        без него добавление счёта без явного account_id молча падало на
        .env-account_id ЧУЖОГО токена (self.account_id = account_id or
        os.getenv(...) в __init__), и все запросы к API уходили с
        привязкой не к тому счёту."""
        data = self._call("UsersService", "GetAccounts", {})
        return data.get("accounts", [])

    # ── Orders ──
    def get_orders(self, strict: bool = False) -> list[dict]:
        """Список активных обычных заявок. strict=True — сбой брокера
        поднимается исключением (ревизия 23.09.2026): исполнителю «не
        знаю» и «заявок нет» — разные ответы, пустой список на 429
        снимал с присмотра живую лимитку."""
        svc = "SandboxService" if self.sandbox else "OrdersService"
        method = "GetSandboxOrders" if self.sandbox else "GetOrders"
        try:
            data = self._call(svc, method, {"accountId": self.account_id})
            return data.get("orders", [])
        except Exception:
            if strict:
                raise
            return []

    def cancel_order(self, order_id: str) -> dict:
        svc = "SandboxService" if self.sandbox else "OrdersService"
        method = "CancelSandboxOrder" if self.sandbox else "CancelOrder"
        return self._call(svc, method, {"accountId": self.account_id, "orderId": order_id})

    def get_order_state(self, order_id: str) -> dict:
        """Состояние заявки: {"status", "lots_executed", "lots_requested"}.
        status — EXECUTION_REPORT_STATUS_* брокера (FILL / REJECTED /
        CANCELLED / NEW / PARTIALLYFILL). Сбой — исключение: «не знаю»
        не равно «снята» (ревизия 23.09.2026)."""
        svc = "SandboxService" if self.sandbox else "OrdersService"
        method = "GetSandboxOrderState" if self.sandbox else "GetOrderState"
        d = self._call(svc, method, {"accountId": self.account_id, "orderId": order_id})
        return {"status": str(d.get("executionReportStatus") or ""),
                "lots_executed": int(d.get("lotsExecuted") or 0),
                "lots_requested": int(d.get("lotsRequested") or 0)}

    def place_market_order(self, figi: str, direction: str, lots: int, oid: str = "") -> dict:
        svc = "SandboxService" if self.sandbox else "OrdersService"
        method = "PostSandboxOrder" if self.sandbox else "PostOrder"
        d = "ORDER_DIRECTION_BUY" if direction == "buy" else "ORDER_DIRECTION_SELL"
        body = {
            "figi": figi, "quantity": str(lots), "direction": d,
            "accountId": self.account_id,
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": oid or str(__import__("uuid").uuid4()),
        }
        return self._sandbox_aware_post(svc, method, body, figi, direction, lots)

    def place_limit_order(self, figi: str, direction: str, lots: int,
                          price: float, oid: str = "", tif: str = "TIME_IN_FORCE_DAY") -> dict:
        svc = "SandboxService" if self.sandbox else "OrdersService"
        method = "PostSandboxOrder" if self.sandbox else "PostOrder"
        d = "ORDER_DIRECTION_BUY" if direction == "buy" else "ORDER_DIRECTION_SELL"
        step = self._get_price_step(figi)
        price = self._round_to_step(price, step) if step else price
        pq = self._price_to_quotation(price)
        body = {
            "figi": figi, "quantity": str(lots), "direction": d,
            "accountId": self.account_id,
            "orderType": "ORDER_TYPE_LIMIT",
            "orderId": oid or str(__import__("uuid").uuid4()),
            "price": pq,
            "timeInForce": tif,
        }
        return self._sandbox_aware_post(svc, method, body, figi, direction, lots)

    _instrument_cache: dict = {}  # figi → {min_price_increment, ...}

    def get_instrument_info(self, figi: str) -> dict:
        """Получить спеки инструмента (с кешем по figi)."""
        if figi in self._instrument_cache:
            return self._instrument_cache[figi]
        try:
            info = self.get_future_full(figi)
        except Exception:
            # АКЦИЯ / ФОНД (29.09.2026): FutureBy на figi бумаги падает, шаг
            # цены был 0 и лимитки/стопы уходили неокруглёнными (400 биржи).
            # GetInstrumentBy — общий для любого инструмента.
            d = self._call("InstrumentsService", "GetInstrumentBy",
                           {"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi})
            ins = d.get("instrument") or {}
            if str(ins.get("instrumentType") or "").lower() == "futures":
                # фьючерс со сбоем FutureBy — урезанную карточку (без ГО и
                # стоимости шага) навечно в кэш не кладём, пусть повторит
                raise
            info = {"figi": figi, "ticker": ins.get("ticker", ""),
                    "lot": int(ins.get("lot", 1) or 1),
                    "min_price_increment": _quot(ins.get("minPriceIncrement")),
                    "instrument_type": ins.get("instrumentType", ""),
                    "class_code": ins.get("classCode", ""),
                    "short_enabled": bool(ins.get("shortEnabledFlag")),
                    "dlong": _quot(ins.get("dlong")), "dshort": _quot(ins.get("dshort"))}
        self._instrument_cache[figi] = info
        return info

    @staticmethod
    def _round_to_step(price: float, step: float) -> float:
        """Округлить цену до ближайшего шага инструмента.
        Без этого биржа возвращает 400 — цена не кратна min_price_increment.
        """
        if step <= 0:
            return price
        return round(round(price / step) * step, 10)

    def _price_to_quotation(self, price: float) -> dict:
        """Конвертировать float-цену в Quotation {units, nano} для T-Invest API."""
        units = int(price)
        nano = int(round((price - units) * 1e9))
        # Защита от nano < 0 (при float-артефактах)
        if nano < 0:
            units -= 1
            nano += 1_000_000_000
        return {"units": str(units), "nano": nano}

    def _get_price_step(self, figi: str) -> float:
        """Получить min_price_increment для инструмента по figi."""
        try:
            info = self.get_instrument_info(figi)
            step = info.get("min_price_increment", 0)
            if step and step > 0:
                return step
        except Exception:
            pass
        return 0

    def place_stop_loss(self, figi: str, direction: str, lots: int,
                        trigger_price: float, expire: bool = False,
                        price_step: float = 0) -> str:
        """Стоп-маркет: после срабатывания триггера ордер исполняется по рынку.
        Без поля 'price' planka не помеха — биржа принимает триггер в любом месте."""
        import logging as _l
        _log = _l.getLogger("tinkoff")
        d = "STOP_ORDER_DIRECTION_BUY" if direction == "buy" else "STOP_ORDER_DIRECTION_SELL"
        step = price_step or self._get_price_step(figi)
        orig_price = trigger_price
        if step and step > 0:
            trigger_price = self._round_to_step(trigger_price, step)
        stop_q = self._price_to_quotation(trigger_price)
        _log.info("place_stop_loss figi=%s dir=%s lots=%s orig_price=%s step=%s rounded=%s quotation=%s",
                  figi, direction, lots, orig_price, step, trigger_price, stop_q)
        body = {
            "figi": figi, "quantity": str(lots), "direction": d,
            "accountId": self.account_id,
            "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
            "stopPrice": stop_q,
            # exchangeOrderType=MARKET — исполнение по рынку после триггера,
            # planka проверяется только в момент исполнения, не при постановке.
            "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
        }
        try:
            data = self._call("StopOrdersService", "PostStopOrder", body)
        except Exception as e:
            _log.error("place_stop_loss FAILED: %s. Body=%s", e, body)
            raise
        return data.get("stopOrderId", "")

    def place_take_profit(self, figi: str, direction: str, lots: int,
                          trigger_price: float, expire: bool = False,
                          price_step: float = 0) -> str:
        """Тейк-маркет: после срабатывания триггера ордер исполняется по рынку."""
        d = "STOP_ORDER_DIRECTION_BUY" if direction == "buy" else "STOP_ORDER_DIRECTION_SELL"
        step = price_step or self._get_price_step(figi)
        trigger_price = self._round_to_step(trigger_price, step) if step else trigger_price
        tp_q = self._price_to_quotation(trigger_price)
        body = {
            "figi": figi, "quantity": str(lots), "direction": d,
            "accountId": self.account_id,
            "stopOrderType": "STOP_ORDER_TYPE_TAKE_PROFIT",
            "stopPrice": tp_q,
            "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
        }
        data = self._call("StopOrdersService", "PostStopOrder", body)
        return data.get("stopOrderId", "")

    # ── Песочница: авто-ГО для ЗАКРЫВАЮЩИХ ордеров (12.07.2026) ──
    # Песочница НЕ неттингует маржу: ордер, ЗАКРЫВАЮЩИЙ позицию, требует ГО как
    # новая сделка → 400 30034 NOT_ENOUGH_BALANCE (нельзя ни закрыть позицию
    # кнопкой/стопом, ни поставить тейк). Для таких ордеров подкачиваем
    # виртуальный баланс (SandboxPayIn), исполняем и возвращаем подкачку
    # (PayIn с минусом). Не вернулась — долг в state/sandbox_boost_debt.json,
    # его вычитает _sandbox_fix_portfolio, чтобы депозит/лоты не искажались.
    @staticmethod
    def _is_not_enough_balance(e) -> bool:
        t = str(e)
        return "30034" in t or "NOT_ENOUGH_BALANCE" in t

    def _sandbox_pos_qty(self, figi: str) -> int:
        try:
            d = self._call("SandboxService", "GetSandboxPositions",
                           {"accountId": self.account_id})
            for f in d.get("futures", []) or []:
                if f.get("figi") == figi:
                    return int(float(f.get("balance", 0) or 0))
        except Exception as e:
            log.warning("sandbox positions: %s", e)
        return 0

    def _sandbox_aware_post(self, svc: str, method: str, body: dict,
                            figi: str, direction: str, lots: int) -> dict:
        if not self.sandbox:
            return self._call(svc, method, body)
        try:
            return self._call(svc, method, body)
        except httpx.HTTPStatusError as e:
            if not self._is_not_enough_balance(e):
                raise
            qty = self._sandbox_pos_qty(figi)
            reducing = ((qty > 0 and direction == "sell" and lots <= qty)
                        or (qty < 0 and direction == "buy" and lots <= -qty))
            if not reducing:
                raise               # реально нет ГО под НОВУЮ сделку — честный отказ
            boost, total, res, last = 1_000_000.0, 0.0, None, e
            for _ in range(5):      # подкачка до 5 млн
                try:
                    self.sandbox_pay_in(self.account_id, boost)
                    total += boost
                except Exception as pe:
                    log.warning("sandbox boost pay_in: %s", pe)
                    break
                body = {**body, "orderId": str(uuid.uuid4())}
                try:
                    res = self._call(svc, method, body)
                    break
                except httpx.HTTPStatusError as e2:
                    last = e2
                    if not self._is_not_enough_balance(e2):
                        break
            self._sandbox_pay_back(total)
            if res is None:
                raise last
            log.info("sandbox: закрывающий ордер %s исполнен с подкачкой ГО %.0f ₽",
                     figi, total)
            return res

    def _sandbox_pay_back(self, total: float):
        if total <= 0:
            return
        try:
            self.sandbox_pay_in(self.account_id, -total)
            return
        except Exception as e:
            log.warning("sandbox: возврат подкачки %.0f не прошёл (%s) — пишу в долг",
                        total, e)
        try:
            p = os.path.join(os.path.dirname(_VSTOPS_PATH), "sandbox_boost_debt.json")
            debt = 0.0
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    debt = float((json.load(fh) or {}).get("debt", 0))
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"debt": round(debt + total, 2)}, fh)
        except Exception as e:
            log.warning("sandbox debt store: %s", e)

    def close_position_market(self, figi: str, qty: int) -> dict:
        direction = "sell" if qty > 0 else "buy"
        return self.place_market_order(figi, direction, abs(qty))

    def get_operations(self, days: int = 30, figi: str | None = None,
                       date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        now = datetime.now(timezone.utc)
        if date_from and date_to:
            frm = date_from + "T00:00:00+03:00"
            to_ = date_to + "T23:59:59+03:00"
        else:
            d = max(days, 1)
            frm = (now - timedelta(days=d)).isoformat()
            to_ = now.isoformat()
        body = {"accountId": self.account_id, "from": frm, "to": to_}
        if figi:
            body["figi"] = figi
        data = self._call("OperationsService", "GetOperations", body)
        ops = []
        for op in data.get("operations", []):
            children = []
            for ch in op.get("childOperations", []):
                children.append({
                    "instrumentUid": ch.get("instrumentUid", ""),
                    "payment": _money(ch.get("payment")),
                })
            rec = {
                "id": op.get("id"), "type": op.get("operationType") or op.get("type"),
                "state": op.get("state"), "figi": op.get("figi"),
                "instrumentUid": op.get("instrumentUid", ""),
                "quantity": op.get("quantity"), "price": _quot(op.get("price")),
                "payment": _money(op.get("payment")), "currency": op.get("currency"),
                "ts": op.get("date"), "instrument_type": op.get("instrumentType"),
            }
            if children:
                rec["children"] = children
            ops.append(rec)
        return ops

    def get_stop_orders(self) -> list[dict]:
        """Список активных стоп-заявок."""
        try:
            data = self._call("StopOrdersService", "GetStopOrders", {"accountId": self.account_id})
            return data.get("stopOrders", [])
        except Exception:
            return []

    def get_stop_orders_strict(self) -> list[dict]:
        """То же самое, но БЕЗ глотания ошибки (21.08.2026). `get_stop_orders()` при сбое молча
        отдаёт [] — неотличимо от «заявок правда нет», и
        `_v2_resize_protection` на пустом списке тихо выходит: докупка лотов
        может остаться без досчёта стопа, если тик поймал сетевой сбой.
        Тот же класс бага чинили в nevrotrader 18.08 (GetStopOrders глушился
        → дубли тейков, механика SVU6). Метод НЕ заменяет `get_stop_orders`
        — им пользуются ещё 9 мест в коде, ожидающих [] на сбой; строгую
        версию зовёт только тик активного счёта, которому нужно ОТЛИЧИТЬ
        «заявок нет» от «брокер не ответил», чтобы не гадать с защитой."""
        data = self._call("StopOrdersService", "GetStopOrders", {"accountId": self.account_id})
        return data.get("stopOrders", [])

    def place_stop_order(self, figi: str, direction: str, lots: int,
                         trigger_price: float, order_type: str = "stop_loss") -> str:
        """Универсальный метод: stop_loss или take_profit."""
        if order_type == "take_profit":
            return self.place_take_profit(figi, direction, lots, trigger_price)
        return self.place_stop_loss(figi, direction, lots, trigger_price)

    # ── Sandbox: эмуляция стоп-ордеров (11.07.2026) ──────────────────
    # Песочница не умеет StopOrdersService. Виртуальные стопы лежат в
    # state/sandbox_vstops.json (общий файл web/бота, атомарная запись), бот
    # каждый тик зовёт sandbox_poll_virtual_stops(): триггер пробит по
    # last_price → market-ордер в песочницу, запись удаляется. Реальный счёт
    # это не затрагивает (перехват только при self.sandbox).

    def _vstops_read(self) -> list[dict]:
        try:
            with open(_VSTOPS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _vstops_write(self, items: list[dict]) -> None:
        os.makedirs(os.path.dirname(_VSTOPS_PATH), exist_ok=True)
        tmp = _VSTOPS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _VSTOPS_PATH)

    def _vstops_call(self, method: str, body: dict) -> dict:
        """Перехват StopOrdersService в песочнице: Post/Get/Cancel — по файлу."""
        with _VSTOPS_LOCK:
            items = self._vstops_read()
            if method == "PostStopOrder":
                vid = "vs-" + uuid.uuid4().hex[:10]
                rec = {
                    "stopOrderId": vid,
                    "accountId": body.get("accountId") or self.account_id,
                    "figi": body.get("figi", ""),
                    "direction": "sell" if body.get("direction") == "STOP_ORDER_DIRECTION_SELL" else "buy",
                    "lots": int(body.get("quantity") or 0),
                    "type": ("take_profit" if body.get("stopOrderType") == "STOP_ORDER_TYPE_TAKE_PROFIT"
                             else "stop_loss"),
                    "price": _quot(body.get("stopPrice")),
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                items.append(rec)
                self._vstops_write(items)
                log.info("sandbox vstop ПОСТАВЛЕН: %s %s %s x%s @ %s",
                         vid, rec["type"], rec["direction"], rec["lots"], rec["price"])
                return {"stopOrderId": vid}
            if method == "GetStopOrders":
                acc = body.get("accountId") or self.account_id
                out = [{
                    "stopOrderId": s.get("stopOrderId", ""),
                    "figi": s.get("figi", ""),
                    "lotsRequested": str(s.get("lots", 0)),
                    "direction": ("STOP_ORDER_DIRECTION_SELL" if s.get("direction") == "sell"
                                  else "STOP_ORDER_DIRECTION_BUY"),
                    "stopOrderType": ("STOP_ORDER_TYPE_TAKE_PROFIT" if s.get("type") == "take_profit"
                                      else "STOP_ORDER_TYPE_STOP_LOSS"),
                    "stopPrice": self._price_to_quotation(float(s.get("price") or 0)),
                    "createDate": s.get("created", ""),
                } for s in items if s.get("accountId") == acc]
                return {"stopOrders": out}
            if method == "CancelStopOrder":
                sid = body.get("stopOrderId", "")
                left = [s for s in items if s.get("stopOrderId") != sid]
                if len(left) != len(items):
                    self._vstops_write(left)
                    log.info("sandbox vstop СНЯТ: %s", sid)
                return {}
        return {}

    def sandbox_poll_virtual_stops(self) -> list[dict]:
        """Тик песочницы: пробить триггеры по last_price, исполнить рынком.
        Зовёт бот (executor_v2) каждые POLL_SEC. Возвращает исполненные записи.
        Логика срабатывания как у биржевых стоп/тейк-маркетов:
          stop_loss  sell → last ≤ триггер;  stop_loss  buy → last ≥ триггер;
          take_profit sell → last ≥ триггер; take_profit buy → last ≤ триггер."""
        if not self.sandbox:
            return []
        with _VSTOPS_LOCK:
            mine = [s for s in self._vstops_read() if s.get("accountId") == self.account_id]
        fired: list[dict] = []
        for s in mine:
            try:
                last = self.get_last_price(s.get("figi", ""))
            except Exception:
                continue
            trig = float(s.get("price") or 0)
            if not last or trig <= 0:
                continue
            sl = s.get("type") != "take_profit"
            sell = s.get("direction") == "sell"
            hit = ((sl and sell and last <= trig) or (sl and not sell and last >= trig)
                   or ((not sl) and sell and last >= trig)
                   or ((not sl) and not sell and last <= trig))
            if not hit:
                continue
            try:
                self.place_market_order(s["figi"], s["direction"], int(s.get("lots") or 0))
            except Exception as e:
                log.warning("sandbox vstop %s: market-исполнение не прошло: %s",
                            s.get("stopOrderId"), e)
                continue
            fired.append(s)
            log.info("sandbox vstop СРАБОТАЛ: %s %s %s x%s триггер=%s last=%s",
                     s.get("stopOrderId"), s.get("type"), s.get("direction"),
                     s.get("lots"), trig, last)
        if fired:
            gone = {s["stopOrderId"] for s in fired}
            with _VSTOPS_LOCK:
                items = self._vstops_read()
                self._vstops_write([x for x in items if x.get("stopOrderId") not in gone])
        return fired

    # ── Sandbox extras ──
    def margin_per_lot(self, figi: str, direction: str = "buy", price: float = 0.0) -> float:
        """Обеспечение на ОДИН лот: у фьючерса — ГО биржи (нет — 10% номинала),
        у акции и фонда — номинал лота × ставка риска брокера (dlong/dshort),
        без ставки — весь номинал (29.09.2026). 0 — не посчиталось."""
        try:
            fu = self.get_future_full(figi)
            im = float(fu.get("initial_margin_on_buy" if direction == "buy"
                              else "initial_margin_on_sell") or 0)
            if im > 0:
                return im
            mpi = float(fu.get("min_price_increment") or 0)
            mpia = float(fu.get("min_price_increment_amount") or 0)
            mult, rate = ((mpia / mpi) if mpi else 0.0), 0.10
        except Exception:
            info = self.get_instrument_info(figi)
            mult = float(info.get("lot") or 1)
            rate = float(info.get("dlong" if direction == "buy" else "dshort") or 0) or 1.0
        px = float(price or 0)
        if px <= 0:
            try:
                px = float(self.get_last_price(figi) or 0)
            except Exception:
                px = 0.0
        return px * mult * rate if px > 0 and mult > 0 else 0.0

    def sandbox_free_go(self):
        """СВОБОДНОЕ ГО песочницы НАШИМ расчётом (20.07.2026):
        депозит − Σ справочного ГО открытых позиций. None — не посчиталось
        (вызывающий не блокирует)."""
        if not self.sandbox:
            return None
        try:
            depo = float((self.get_portfolio() or {}).get("total_portfolio") or 0)
            used = 0.0
            for p in self.get_positions():
                q = abs(int(p.quantity or 0))
                if not q or not p.figi:
                    continue
                if p.instrument_type in ("share", "etf"):
                    # 29.09.2026: бумага — не ГО, а обеспечение по ставке риска
                    im = self.margin_per_lot(
                        p.figi, "buy" if int(p.quantity) > 0 else "sell",
                        float(getattr(p, "current_price", 0) or 0))
                    used += im * q
                    continue
                fu = self.get_future_full(p.figi)
                im = max(float(fu.get("initial_margin_on_buy") or 0),
                         float(fu.get("initial_margin_on_sell") or 0))
                if im <= 0:
                    # владелец 20.07 («наоткрывал в 20 раз больше портфеля»): ГО
                    # инструмента не отдалось → считаем 10% номинала, а не 0
                    # (иначе занятое ГО недосчитывалось и гейт пропускал всё).
                    mpi = float(fu.get("min_price_increment") or 0)
                    mpia = float(fu.get("min_price_increment_amount") or 0)
                    mult = (mpia / mpi) if mpi else 0.0
                    px = float(getattr(p, "current_price", 0) or 0)
                    im = px * mult * 0.10 if px > 0 and mult > 0 else 0.0
                used += im * q
            return max(0.0, depo - used)
        except Exception as e:
            log.warning("sandbox_free_go: %s", e)
            return None

    def sandbox_get_accounts(self) -> list[dict]:
        data = self._call("SandboxService", "GetSandboxAccounts", {})
        return data.get("accounts", [])

    def sandbox_open_account(self) -> str:
        data = self._call("SandboxService", "OpenSandboxAccount", {})
        return data.get("accountId", "")

    def sandbox_pay_in(self, account_id: str, amount: float) -> dict:
        units = int(amount)
        nano = int(round((amount - units) * 1e9))
        return self._call("SandboxService", "SandboxPayIn", {
            "accountId": account_id,
            "amount": {"units": str(units), "nano": nano, "currency": "rub"},
        })
