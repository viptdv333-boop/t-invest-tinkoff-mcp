# t-invest-tinkoff-mcp

MCP-сервер для **T-Invest (Т-Инвестиции)**: рыночные данные, портфель и торговые операции по **фьючерсам MOEX**
для ИИ-клиентов (Claude и любых других, поддерживающих [Model Context Protocol](https://modelcontextprotocol.io)).
Вы подключаете сервер к своему ИИ — и просите его: «покажи свечи NGZ6», «что в моём портфеле», «поставь лимитку».

> English: an MCP server that gives AI clients access to T-Invest market data, your portfolio and trading tools
> for MOEX futures. See the [English section](#english) below.

## ⚠️ Предупреждение

Сервер даёт ИИ доступ к **реальному счёту**. Торговые инструменты (`place_market_order`, `place_limit_order`,
`place_stop_loss`, `place_take_profit`, `cancel_order`, `close_position`) **включены по умолчанию**: подключённый
ИИ может ставить и отменять ордера и закрывать позиции. Вы отвечаете за все сделки.

- Только чтение: `TINVEST_ALLOW_TRADING=0`.
- Проверка без риска: `TINVEST_SANDBOX=1` (песочница T-Invest, токен песочницы).
- Токен T-Invest бывает «только чтение» и «торговый»: для работы без ордеров выпустите токен только на чтение.
- Автор не даёт инвестиционных рекомендаций и не несёт ответственности за убытки (см. лицензию).

## Что нужно

- Python 3.10+.
- Токен T-Invest API: <https://www.tbank.ru/invest/settings/api/> и (по желанию) номер счёта.
- ИИ-клиент с поддержкой MCP (Claude Code, Claude Desktop и др.).

Работает с **фьючерсами MOEX**: тикер — код фьючерса, например `NGZ6`, `SiZ6`, `BRZ6`.

## Установка

```bash
pip install git+https://github.com/viptdv333-boop/t-invest-tinkoff-mcp.git
```

## Подключение к Claude

Claude Code:

```bash
claude mcp add tinvest -e TINKOFF_TOKEN=ваш_токен -e TINKOFF_ACCOUNT_ID=номер_счёта -- tinvest-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tinvest": {
      "command": "tinvest-mcp",
      "env": {
        "TINKOFF_TOKEN": "ваш_токен",
        "TINKOFF_ACCOUNT_ID": "номер_счёта"
      }
    }
  }
}
```

Токен хранится только у вас на машине. Сервер работает локально и обращается только к API T-Invest.

## Примеры запросов к ИИ

- «Покажи последние 100 часовых свечей NGZ6 и коротко опиши структуру».
- «Какая цена и стакан у BRZ6?»
- «Что у меня в портфеле, какая свободная маржа?»
- «Посчитай размер позиции для входа 3.05 со стопом 2.98 при риске 1% депозита».
- «Покажи активные заявки и стопы».
- (торговля включена) «Поставь лимитную заявку на покупку 1 контракта NGZ6 по 3.05».

## Инструменты

| Инструмент | Параметры | Что делает |
|---|---|---|
| `get_candles` | `ticker`, `interval` (`1m`,`5m`,`15m`,`1h`,`4h`,`1d`), `bars` | свечи OHLCV (время UTC) |
| `get_last_price` | `ticker` | последняя цена |
| `get_orderbook` | `ticker`, `depth` | стакан заявок |
| `get_instrument_specs` | `ticker` | ГО, шаг цены, стоимость шага, экспирация |
| `get_phase` | — | фаза торгов MOEX |
| `get_portfolio` | — | позиции, доходность, маржа |
| `get_active_orders` | — | активные заявки и стоп-ордера |
| `get_free_deposit` | — | ликвидный портфель, начальная и свободная маржа |
| `get_operations` | `ticker` (необязательно), `days` | история операций |
| `calculate_position_size` | `ticker`, `direction`, `entry_price`, `stop_price`, `risk_percent` | размер позиции по риску |
| `place_market_order` | `ticker`, `direction` (`buy`/`sell`), `qty` | рыночный ордер |
| `place_limit_order` | `ticker`, `direction`, `qty`, `price` | лимитный ордер |
| `place_stop_loss` | `ticker`, `direction`, `qty`, `stop_price` | стоп (по рынку после срабатывания); `direction` — сторона самого ордера: `sell` для защиты лонга, `buy` для шорта |
| `place_take_profit` | `ticker`, `direction`, `qty`, `take_price` | тейк-профит, `direction` — как у стопа |
| `cancel_order` | `order_id` | отмена заявки или стопа |
| `close_position` | `ticker` | закрыть позицию рыночным ордером |

`qty` — количество контрактов (лотов), цены — в пунктах инструмента.

## Настройки (переменные окружения)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TINKOFF_TOKEN` | — | токен T-Invest API (обязателен; допустимо и `TINVEST_TOKEN`) |
| `TINKOFF_ACCOUNT_ID` | первый счёт | номер счёта (допустимо и `TINVEST_ACCOUNT_ID`) |
| `TINVEST_SANDBOX` | `0` | `1` — песочница |
| `TINVEST_ALLOW_TRADING` | `1` | `0` — отключить торговые инструменты |
| `TINVEST_DROP_WEEKENDS` | `0` | `1` — не отдавать свечи субботы и воскресенья (МСК) |
| `TINVEST_STATE_DIR` | `~/.tinvest-mcp` | каталог служебных файлов песочницы |

## Если что-то не работает

- `TINKOFF_TOKEN not set` — токен не передан клиенту: проверьте блок `env` в настройках MCP.
- `Trading disabled` — торговля отключена (`TINVEST_ALLOW_TRADING=0`).
- Ошибка прав при постановке ордера — токен «только чтение»; нужен торговый токен.
- `<тикер> not found` — укажите код фьючерса MOEX; проверьте, что контракт не истёк.
- Команда `tinvest-mcp` не найдена — проверьте, что каталог со скриптами pip в `PATH`, или укажите полный путь в `command`.

## English

An MCP server for T-Invest (MOEX futures): candles, last price, order book, instrument specs, portfolio, free margin,
operations history, position sizing and trading tools (market/limit orders, stop-loss, take-profit, cancel, close).

Install: `pip install git+https://github.com/viptdv333-boop/t-invest-tinkoff-mcp.git`. Set `TINKOFF_TOKEN` (and optionally
`TINKOFF_ACCOUNT_ID`) in the MCP client config and run the `tinvest-mcp` command over stdio.

**Warning:** trading tools are enabled by default and operate on your real account. Set `TINVEST_ALLOW_TRADING=0` for
read-only mode or `TINVEST_SANDBOX=1` to use the T-Invest sandbox. Not investment advice; use at your own risk.

## Лицензия

MIT. См. [LICENSE](LICENSE).
