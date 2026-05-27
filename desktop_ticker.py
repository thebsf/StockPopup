import ctypes
import json
import math
import os
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, font, ttk
from urllib.error import URLError, HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


REFRESH_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CONFIG_PATH = Path(os.getenv("APPDATA", str(Path.home()))) / "DesktopMarketTicker" / "settings.json"
TRANSPARENT_COLOR = "#010203"
COLOR_KEYS = ["background", "border", "text", "muted", "button", "accent", "price", "up", "down", "flat"]
NO_COLOR_VALUES = {"", "none", "transparent", "null", "无", "无颜色"}
SINGLE_INSTANCE_MUTEX_NAME = r"Local\DesktopMarketTicker.SingleInstance"
ERROR_ALREADY_EXISTS = 183
INSTANCE_MUTEX_HANDLE = None

DEFAULT_SETTINGS = {
    "width": 282,
    "height": 260,
    "window_x": 80,
    "window_y": 80,
    "background": "#202328",
    "border": "#30343a",
    "text": "#a1a8b1",
    "muted": "#666d76",
    "button": "#68717a",
    "accent": "#7f8790",
    "price": "#b6bdc6",
    "up": "#ff6464",
    "down": "#29d391",
    "flat": "#aab1ba",
    "background_opacity": 0.62,
    "trend_opacity": 0.38,
    "text_opacity": 0.68,
    "font_size": 8,
    "price_font_size": 10,
    "price_decimals": 2,
    "refresh_seconds": 30,
    "always_on_top": True,
    "sina_codes": [],
    "removed_preset_keys": [],
    "quote_order": ["xau", "cn", "nq", "sh", "star", "oil", "usd", "jpy"],
    "quote_visible": ["xau", "cn", "nq", "sh", "star", "oil", "usd", "jpy"],
}

BASE_QUOTE_DEFS = {
    "xau": {"name": "纽约金", "symbol": "hf_GC"},
    "cn": {"name": "沪金99", "symbol": "SGE_AU9999"},
    "nq": {"name": "纳指期货", "symbol": "hf_NQ"},
    "sh": {"name": "上证指数", "symbol": "sh000001"},
    "star": {"name": "科创50", "symbol": "sh000688"},
    "oil": {"name": "纽约原油", "symbol": "hf_CL"},
    "usd": {"name": "美元汇率", "symbol": "fx_susdcny"},
    "jpy": {"name": "日元汇率", "symbol": "fx_sjpycny"},
}
QUOTE_DEFS = dict(BASE_QUOTE_DEFS)
CUSTOM_KEY_PREFIX = "custom:"
GLOBAL_TREND_SYMBOLS = {"xau": "GC", "nq": "NQ", "oil": "CL"}


def acquire_single_instance() -> bool:
    global INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    INSTANCE_MUTEX_HANDLE = handle
    return True


def release_single_instance() -> None:
    global INSTANCE_MUTEX_HANDLE
    if os.name == "nt" and INSTANCE_MUTEX_HANDLE is not None:
        ctypes.windll.kernel32.CloseHandle(INSTANCE_MUTEX_HANDLE)
        INSTANCE_MUTEX_HANDLE = None


@dataclass
class Quote:
    name: str
    symbol: str
    price: float | None = None
    change: float | None = None
    percent: float | None = None
    unit: str = ""
    source: str = ""
    error: str | None = None
    decimals: int = 2
    trend: list[float] = field(default_factory=list)
    trend_progress: float = 1.0

    @property
    def direction(self) -> str:
        value = self.change if self.change is not None else self.percent
        if value is None or value == 0:
            return "flat"
        return "up" if value > 0 else "down"


def http_get(url: str, encoding: str = "utf-8", headers: dict | None = None) -> str:
    cache_key = int(time.time() * 1000)
    if url.startswith("https://hq.sinajs.cn/list="):
        url = f"https://hq.sinajs.cn/?rn={cache_key}&list={url.rsplit('=', 1)[1]}"
    else:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}_={cache_key}"
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://finance.sina.com.cn/",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=8) as response:
        return response.read().decode(encoding, errors="replace")


def parse_sina_assignment(text: str) -> list[str]:
    start = text.find('="')
    end = text.rfind('";')
    if start == -1:
        return []
    if end == -1:
        end = len(text)
    return [item.strip() for item in text[start + 2:end].split(",")]


def parse_sina_assignments(text: str) -> dict[str, list[str]]:
    return {
        symbol: [item.strip() for item in body.split(",")]
        for symbol, body in re.findall(r'var\s+hq_str_([^=]+)="([^"]*)";?', text)
    }


def as_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = str(value).replace("%", "").strip()
    if not value or value == "--":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_a_stock_code(value) -> str:
    code = str(value or "").strip().lower().replace(".", "")
    if not code:
        return ""
    if re.fullmatch(r"(sh|sz|bj)\d{6}", code):
        return code
    if not re.fullmatch(r"\d{6}", code):
        return ""
    if code.startswith(("5", "600", "601", "603", "605", "688", "689", "900")):
        return f"sh{code}"
    if code.startswith(("000", "001", "002", "003", "15", "16", "18", "200", "300", "301")):
        return f"sz{code}"
    if code.startswith(("4", "8", "920")):
        return f"bj{code}"
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def normalize_sina_code(value) -> str:
    code = str(value or "").strip().replace(" ", "")
    if not code:
        return ""
    aliases = {
        "NKY": "znb_NKY",
        "HSTECH": "rt_hkHSTECH",
        "HSKTECH": "rt_hkHSTECH",
    }
    if code.upper() in aliases:
        return aliases[code.upper()]
    dotted = re.fullmatch(r"([A-Za-z]+)\.(.+)", code)
    if dotted:
        market, market_code = dotted.groups()
        market = market.upper()
        if market == "US":
            return f"gb_{market_code.lstrip('$').lstrip('.').lower()}"
        if market == "HK":
            return f"rt_hk{market_code.upper()}"
        if market in {"SH", "SZ", "BJ"} and re.fullmatch(r"\d{6}", market_code):
            return f"{market.lower()}{market_code}"
        if market == "CN":
            return normalize_a_stock_code(market_code)
        if market == "SGE":
            return f"SGE_{market_code.upper()}"
        if market in {"HF", "NF"}:
            return f"{market.lower()}_{market_code.upper()}"
        if market == "FX":
            return f"fx_{market_code.lower()}"
    stock_code = normalize_a_stock_code(code)
    if stock_code:
        return stock_code
    upper = code.upper()
    lower = code.lower()
    if upper in {"AU9999", "AUTD"}:
        return f"SGE_{upper}"
    if lower.startswith("sge_"):
        return f"SGE_{code[4:].upper()}"
    if lower.startswith(("hf_", "nf_")):
        return f"{lower[:3]}{code[3:].upper()}"
    if lower.startswith("fx_"):
        return lower
    if lower.startswith("rt_hk"):
        return f"rt_hk{code[5:].upper()}"
    if lower.startswith("hk"):
        return f"hk{code[2:].upper()}"
    if lower.startswith("znb_"):
        return f"znb_{code[4:].upper()}"
    if lower.startswith(("gb_", "usr_")):
        return lower
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{1,30}", code):
        return code
    return ""


def normalize_sina_codes(value) -> list[str]:
    if isinstance(value, list):
        raw_codes = value
    else:
        raw_codes = re.split(r"[\s,，;；]+", str(value or ""))
    codes: list[str] = []
    for raw_code in raw_codes:
        code = normalize_sina_code(raw_code)
        if code and code not in codes:
            codes.append(code)
    return codes


def suggestion_symbol(fields: list[str]) -> str:
    if len(fields) < 4:
        return ""
    market_type = fields[1]
    code = fields[2]
    backend = fields[3]
    if market_type in {"11", "12"}:
        return backend
    if market_type == "31":
        return f"rt_hk{code.upper()}"
    if market_type == "33":
        return f"rt_hk{code.upper()}"
    if market_type == "41":
        return f"gb_{code.lstrip('.').lower()}"
    if market_type == "86":
        return f"hf_{code.upper()}"
    if market_type == "87":
        return f"nf_{code.upper()}"
    return backend


def fetch_sina_suggestions(query: str) -> list[list[str]]:
    url = f"https://suggest3.sinajs.cn/suggest/type=&key={quote(query)}"
    text = http_get(url, "gbk")
    match = re.search(r'="(.*)"\s*;?\s*$', text, re.S)
    if not match or not match.group(1):
        return []
    return [item.split(",") for item in match.group(1).split(";") if item]


def quote_symbol_has_data(symbol: str) -> bool:
    text = http_get(f"https://hq.sinajs.cn/list={symbol}", "gbk")
    assignments = parse_sina_assignments(text)
    return bool(assignments.get(symbol))


def resolve_sina_code(value) -> str:
    raw = str(value or "").strip()
    normalized = normalize_sina_code(raw)
    direct_backend = re.match(r"^(?:sh|sz|bj|SGE_|hf_|nf_|fx_|rt_hk|hk|gb_|usr_|znb_)", normalized, re.I)
    if direct_backend:
        return normalized
    try:
        suggestions = fetch_sina_suggestions(raw)
        lower_raw = raw.lower()
        preferred = [
            fields for fields in suggestions
            if any(lower_raw == str(field).lower() for field in fields[:7])
        ]
        for fields in preferred or suggestions[:1]:
            symbol = suggestion_symbol(fields)
            if symbol and quote_symbol_has_data(symbol):
                return symbol
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9._$-]{1,30}", raw):
            upper = raw.upper()
            candidates = [
                f"znb_{upper}",
                f"hf_{upper}",
                f"rt_hk{upper}",
                f"gb_{raw.lstrip('.').lower()}",
            ]
            for symbol in candidates:
                if quote_symbol_has_data(symbol):
                    return symbol
    except (HTTPError, URLError, TimeoutError):
        pass
    return normalized


def resolve_sina_codes(value) -> list[str]:
    if isinstance(value, list):
        raw_codes = value
    else:
        raw_codes = re.split(r"[,，;；\n]+", str(value or ""))
    codes: list[str] = []
    for raw_code in raw_codes:
        code = resolve_sina_code(raw_code)
        if code and code not in codes:
            codes.append(code)
    return codes


def custom_quote_key(symbol: str) -> str:
    return f"{CUSTOM_KEY_PREFIX}{symbol}"


def custom_symbol_from_key(key: str) -> str:
    return key.removeprefix(CUSTOM_KEY_PREFIX)


def sync_quote_defs(sina_codes: list[str]) -> None:
    QUOTE_DEFS.clear()
    for code in sina_codes:
        QUOTE_DEFS[custom_quote_key(code)] = {"name": "自选行情", "symbol": code}
    QUOTE_DEFS.update(BASE_QUOTE_DEFS)


def parse_sina_sge(values: list[str], name: str, symbol: str) -> Quote:
    price = as_float(values[3] if len(values) > 3 else None)
    previous_close = as_float(values[9] if len(values) > 9 else None)
    percent = as_float(values[17] if len(values) > 17 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    change = price - previous_close if previous_close is not None else None
    quote_name = name or (values[1].replace(" ", "") if len(values) > 1 else symbol)
    return Quote(quote_name, symbol, price, change, percent, "CNY/g", "新浪")


def parse_sina_index(values: list[str], key: str, name: str, symbol: str) -> Quote:
    price = as_float(values[3] if len(values) > 3 else None)
    previous_close = as_float(values[2] if len(values) > 2 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    change = price - previous_close if previous_close is not None else None
    percent = (change / previous_close * 100) if change is not None and previous_close else None
    return Quote(name, symbol, price, change, percent, "点", "新浪")


def parse_sina_a_stock(values: list[str], symbol: str) -> Quote:
    name = values[0] if values else "自选行情"
    price = as_float(values[3] if len(values) > 3 else None)
    previous_close = as_float(values[2] if len(values) > 2 else None)
    if price is None:
        raise RuntimeError("行情价格为空，请检查新浪代码")
    change = price - previous_close if previous_close is not None else None
    percent = (change / previous_close * 100) if change is not None and previous_close else None
    return Quote(name or "自选行情", symbol, price, change, percent, "元", "新浪")


def parse_sina_global_future(values: list[str], name: str, symbol: str, unit: str) -> Quote:
    price = as_float(values[0] if values else None)
    previous_close = as_float(values[7] if len(values) > 7 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    change = price - previous_close if previous_close is not None else None
    percent = (change / previous_close * 100) if change is not None and previous_close else None
    quote_name = name
    if name == symbol and len(values) > 13 and values[13]:
        quote_name = values[13]
    return Quote(quote_name, symbol, price, change, percent, unit, "新浪")


def parse_sina_fx(values: list[str], name: str, symbol: str) -> Quote:
    price = as_float(values[8] if len(values) > 8 else None)
    previous_close = as_float(values[3] if len(values) > 3 else None)
    percent = as_float(values[10] if len(values) > 10 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    change = price - previous_close if previous_close is not None else None
    quote_name = values[9] if name == symbol and len(values) > 9 and values[9] else name
    return Quote(quote_name, symbol, price, change, percent, "CNY", "新浪", decimals=4)


def parse_sina_inner_future(values: list[str], name: str, symbol: str) -> Quote:
    quote_name = values[0] if values else name
    price = as_float(values[6] if len(values) > 6 else None)
    previous_close = as_float(values[10] if len(values) > 10 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    change = price - previous_close if previous_close is not None else None
    percent = (change / previous_close * 100) if change is not None and previous_close else None
    return Quote(quote_name or name, symbol, price, change, percent, "CNY", "新浪")


def parse_sina_hk(values: list[str], symbol: str) -> Quote:
    name = values[1] if len(values) > 1 and values[1] else symbol
    price = as_float(values[6] if len(values) > 6 else None)
    change = as_float(values[7] if len(values) > 7 else None)
    percent = as_float(values[8] if len(values) > 8 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    return Quote(name, symbol, price, change, percent, "HKD", "新浪")


def parse_sina_us(values: list[str], symbol: str) -> Quote:
    name = values[0] if values and values[0] else symbol
    price = as_float(values[1] if len(values) > 1 else None)
    percent = as_float(values[2] if len(values) > 2 else None)
    change = as_float(values[4] if len(values) > 4 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    return Quote(name, symbol, price, change, percent, "USD", "新浪")


def parse_sina_global_index(values: list[str], symbol: str) -> Quote:
    name = values[0] if values else symbol
    price = as_float(values[1] if len(values) > 1 else None)
    change = as_float(values[2] if len(values) > 2 else None)
    percent = as_float(values[3] if len(values) > 3 else None)
    if price is None:
        raise RuntimeError(f"{name}价格为空")
    return Quote(name, symbol, price, change, percent, "点", "新浪")


def china_trading_progress(moment: datetime) -> float:
    minutes = moment.hour * 60 + moment.minute
    if minutes <= 9 * 60 + 30:
        completed = 0
    elif minutes <= 11 * 60 + 30:
        completed = minutes - (9 * 60 + 30)
    elif minutes < 13 * 60:
        completed = 120
    elif minutes <= 15 * 60:
        completed = 120 + minutes - 13 * 60
    else:
        completed = 240
    return max(0.0, min(1.0, completed / 240))


def fetch_sina_intraday_trend(symbol: str) -> tuple[list[float], float]:
    url = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        f"CN_MarketDataService.getKLineData?symbol={quote(symbol)}&scale=5&ma=no&datalen=48"
    )
    data = json.loads(http_get(url, "utf-8", {"Accept": "application/json"}))
    values = data.get("result", {}).get("data") or []
    today = datetime.now().strftime("%Y-%m-%d")
    values = [
        item for item in values
        if isinstance(item, dict) and str(item.get("day", "")).startswith(today)
    ]
    trend = [
        value
        for item in values
        if isinstance(item, dict) and (value := as_float(item.get("close"))) is not None
    ]
    if not values:
        return [], 0.0
    try:
        latest = datetime.strptime(str(values[-1]["day"]), "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return trend, min(1.0, len(trend) / 48)
    return trend, china_trading_progress(latest)


def fetch_sina_global_intraday_trend(symbol: str) -> tuple[list[float], float]:
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data=/"
        f"GlobalFuturesService.getGlobalFuturesMinLine?symbol={quote(symbol)}"
    )
    text = http_get(url, "utf-8", {"Accept": "application/json"})
    match = re.search(r"var\s+_data=\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        return [], 0.0
    data = json.loads(match.group(1))
    values = data.get("minLine_1d") or []
    trend = [
        value
        for item in values
        if isinstance(item, list) and len(item) > 1 and (value := as_float(item[1])) is not None
    ]
    if not values:
        return trend, 0.0
    try:
        first = datetime.strptime(values[0][-1], "%Y-%m-%d %H:%M:%S")
        latest = datetime.strptime(values[-1][-1], "%Y-%m-%d %H:%M:%S")
    except (IndexError, TypeError, ValueError):
        return trend, 1.0
    progress = (latest - first).total_seconds() / (23 * 60 * 60)
    return trend, max(0.0, min(1.0, progress))


def fetch_sina_inner_intraday_trend(symbol: str) -> tuple[list[float], float]:
    contract = symbol.removeprefix("nf_")
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data=/"
        f"InnerFuturesNewService.getMinLine?symbol={quote(contract)}"
    )
    text = http_get(url, "utf-8", {"Accept": "application/json"})
    match = re.search(r"var\s+_data=\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        return [], 0.0
    values = json.loads(match.group(1)) or []
    trend = [
        value
        for item in values
        if isinstance(item, list) and len(item) > 1 and (value := as_float(item[1])) is not None
    ]
    # Domestic futures trading calendars vary by contract; use returned minute
    # count conservatively instead of presenting an incomplete line as full day.
    return trend, min(1.0, max(0.02, len(trend) / 555)) if trend else 0.0


def fetch_sina_us_intraday_trend(symbol: str) -> tuple[list[float], float]:
    url = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_data=/"
        f"US_MinlineService.getMinline?symbol={quote(symbol)}"
    )
    text = http_get(url, "utf-8", {"Accept": "application/json"})
    match = re.search(r"var\s+_data=\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        return [], 0.0
    data = json.loads(match.group(1))
    sessions = data.get("minline_1") or []
    if not sessions:
        return [], 0.0
    latest_session = sessions[-1]
    first = latest_session.get("first_min") or []
    other = latest_session.get("other_min") or []
    trend: list[float] = []
    if len(first) > 3 and (value := as_float(first[3])) is not None:
        trend.append(value)
    trend.extend(
        value for item in other
        if isinstance(item, list) and item and (value := as_float(item[0])) is not None
    )
    return trend, min(1.0, len(trend) / 391) if trend else 0.0


def fetch_sina_forex_intraday_trend(symbol: str) -> tuple[list[float], float]:
    url = (
        "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_data=/"
        f"NewForexService.getMinKline?symbol={quote(symbol)}&scale=1&datalen=1440"
    )
    text = http_get(url, "utf-8", {"Accept": "application/json"})
    match = re.search(r"var\s+_data\s*=\s*\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        return [], 0.0
    values = json.loads(match.group(1)) or []
    today = datetime.now().strftime("%Y-%m-%d")
    values = [
        item for item in values
        if isinstance(item, dict) and str(item.get("d", "")).startswith(today)
    ]
    trend = [
        value
        for item in values
        if (value := as_float(item.get("c"))) is not None
    ]
    if not values:
        return [], 0.0
    try:
        latest = datetime.strptime(str(values[-1]["d"]), "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return trend, 0.0
    progress = (latest.hour * 60 + latest.minute) / (24 * 60)
    return trend, max(0.0, min(1.0, progress))


def trading_session_progress(latest_time: str, sessions: list[list[str]]) -> float:
    try:
        latest = datetime.strptime(latest_time, "%H:%M:%S").hour * 60 + datetime.strptime(
            latest_time, "%H:%M:%S"
        ).minute
    except ValueError:
        try:
            latest = datetime.strptime(latest_time, "%H:%M").hour * 60 + datetime.strptime(
                latest_time, "%H:%M"
            ).minute
        except ValueError:
            return 0.0
    ranges: list[tuple[int, int]] = []
    for start_text, end_text in sessions:
        try:
            start = datetime.strptime(start_text, "%H:%M").hour * 60 + datetime.strptime(
                start_text, "%H:%M"
            ).minute
            end = datetime.strptime(end_text, "%H:%M").hour * 60 + datetime.strptime(
                end_text, "%H:%M"
            ).minute
        except ValueError:
            continue
        ranges.append((start, end))
    total = sum(end - start + 1 for start, end in ranges)
    if total <= 0:
        return 0.0
    completed = 0
    for start, end in ranges:
        if latest < start:
            break
        completed += min(latest, end) - start + 1
        if latest <= end:
            break
    return max(0.0, min(1.0, completed / total))


def fetch_sina_hk_intraday_trend(symbol: str) -> tuple[list[float], float]:
    code = re.sub(r"^(?:rt_)?hk", "", symbol, flags=re.I)
    url = (
        "https://stock.finance.sina.com.cn/hkstock/api/jsonp_v2.php/var%20_data=/"
        f"HK_StockService.getHKMinline?symbol={quote(code)}"
    )
    text = http_get(url, "utf-8", {"Accept": "application/json"})
    match = re.search(r"var\s+_data\s*=\s*\((.*)\)\s*;?\s*$", text, re.S)
    if not match:
        return [], 0.0
    values = json.loads(match.group(1)) or []
    trend = [
        value
        for item in values
        if isinstance(item, dict) and (value := as_float(item.get("p"))) is not None
    ]
    if not values:
        return [], 0.0
    progress = trading_session_progress(
        str(values[-1].get("m", "")),
        [["09:30", "11:59"], ["13:00", "16:00"]],
    )
    return trend, progress


def fetch_sina_global_index_intraday_trend(symbol: str) -> tuple[list[float], float]:
    code = re.sub(r"^znb_", "", symbol, flags=re.I)
    min_url = f"https://gi.finance.sina.com.cn/hq/min?symbol={quote(code)}"
    time_url = f"https://gi.finance.sina.com.cn/hq/time?symbol={quote(code)}"
    data = json.loads(http_get(min_url, "utf-8", {"Accept": "application/json"}))
    values = data.get("result", {}).get("data") or []
    trend = [
        value
        for item in values
        if isinstance(item, list) and len(item) > 1 and (value := as_float(item[1])) is not None
    ]
    if not values:
        return [], 0.0
    sessions_data = json.loads(http_get(time_url, "utf-8", {"Accept": "application/json"}))
    sessions = sessions_data.get("result", {}).get("data", {}).get("time") or []
    progress = trading_session_progress(str(values[-1][0]), sessions)
    return trend, progress


def quote_trend_type(symbol: str) -> tuple[str, str] | None:
    lower = symbol.lower()
    if lower.startswith(("sh", "sz", "bj")):
        return "cn", symbol
    if lower.startswith("hf_"):
        return "global", symbol[3:].upper()
    if lower.startswith("nf_"):
        return "inner", symbol
    if lower.startswith(("gb_", "usr_")):
        code = symbol.split("_", 1)[1].lstrip("$")
        us_index_codes = {"ixic", "dji", "ndx", "inx"}
        return "us", f".{code.upper()}" if code.lower() in us_index_codes else code.upper()
    if lower.startswith("fx_"):
        return "forex", symbol
    if lower.startswith(("hk", "rt_hk")):
        return "hk", symbol
    if lower.startswith("znb_"):
        return "global_index", symbol
    if lower.startswith("sge_"):
        return "cn", symbol
    return None


def fallback_trend_span_seconds(symbol: str) -> int:
    lower = symbol.lower()
    if lower.startswith("sge_"):
        return 11 * 60 * 60
    if lower.startswith(("sh", "sz", "bj")):
        return 4 * 60 * 60
    return 23 * 60 * 60


def fetch_sina_trends(keys: list[str]) -> dict[str, tuple[list[float], float]]:
    trends: dict[str, tuple[list[float], float]] = {}
    for key in keys:
        definition = QUOTE_DEFS.get(key)
        if definition is None:
            continue
        try:
            trend_type = quote_trend_type(definition["symbol"])
            if key in GLOBAL_TREND_SYMBOLS:
                trend_type = ("global", GLOBAL_TREND_SYMBOLS[key])
            if trend_type is None:
                continue
            kind, symbol = trend_type
            if kind == "global":
                values, progress = fetch_sina_global_intraday_trend(symbol)
            elif kind == "inner":
                values, progress = fetch_sina_inner_intraday_trend(symbol)
            elif kind == "us":
                values, progress = fetch_sina_us_intraday_trend(symbol)
            elif kind == "forex":
                values, progress = fetch_sina_forex_intraday_trend(symbol)
            elif kind == "hk":
                values, progress = fetch_sina_hk_intraday_trend(symbol)
            elif kind == "global_index":
                values, progress = fetch_sina_global_index_intraday_trend(symbol)
            else:
                values, progress = fetch_sina_intraday_trend(symbol)
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            continue
        if values:
            trends[key] = (values, progress)
    return trends


def parse_custom_sina_quote(values: list[str], symbol: str) -> Quote:
    lower = symbol.lower()
    if lower.startswith("sge_"):
        return parse_sina_sge(values, "", symbol)
    if lower.startswith("hf_"):
        return parse_sina_global_future(values, symbol, symbol, "USD")
    if lower.startswith("nf_"):
        return parse_sina_inner_future(values, symbol, symbol)
    if lower.startswith("fx_"):
        return parse_sina_fx(values, symbol, symbol)
    if lower.startswith(("hk", "rt_hk")):
        return parse_sina_hk(values, symbol)
    if lower.startswith(("gb_", "usr_")):
        return parse_sina_us(values, symbol)
    if lower.startswith("znb_"):
        return parse_sina_global_index(values, symbol)
    return parse_sina_a_stock(values, symbol)


def fetch_sina_quotes(
    sina_codes=None,
    trends: dict[str, tuple[list[float], float]] | None = None,
) -> dict[str, Quote]:
    custom_symbols = normalize_sina_codes(sina_codes)
    sync_quote_defs(custom_symbols)
    symbols = ["hf_GC", "SGE_AU9999", "hf_NQ", "sh000001", "sh000688", "hf_CL", "fx_susdcny", "fx_sjpycny"]
    symbols.extend(custom_symbols)
    assignments = parse_sina_assignments(http_get(f"https://hq.sinajs.cn/list={','.join(symbols)}", "gbk"))
    parsers = {
        "xau": lambda: parse_sina_global_future(assignments.get("hf_GC", []), "纽约金", "hf_GC", "USD/oz"),
        "cn": lambda: parse_sina_sge(assignments.get("SGE_AU9999", []), "沪金99", "SGE_AU9999"),
        "nq": lambda: parse_sina_global_future(assignments.get("hf_NQ", []), "纳指期货", "hf_NQ", "USD"),
        "sh": lambda: parse_sina_index(assignments.get("sh000001", []), "sh", "上证指数", "sh000001"),
        "star": lambda: parse_sina_index(assignments.get("sh000688", []), "star", "科创50", "sh000688"),
        "oil": lambda: parse_sina_global_future(assignments.get("hf_CL", []), "纽约原油", "hf_CL", "USD/bbl"),
        "usd": lambda: parse_sina_fx(assignments.get("fx_susdcny", []), "美元汇率", "fx_susdcny"),
        "jpy": lambda: parse_sina_fx(assignments.get("fx_sjpycny", []), "日元汇率", "fx_sjpycny"),
    }
    for symbol in custom_symbols:
        parsers[custom_quote_key(symbol)] = lambda custom_symbol=symbol: parse_custom_sina_quote(
            assignments.get(custom_symbol, []),
            custom_symbol,
        )
    quotes: dict[str, Quote] = {}
    for key, parser in parsers.items():
        definition = QUOTE_DEFS[key]
        try:
            quotes[key] = parser()
            if key in (trends or {}):
                quotes[key].trend, quotes[key].trend_progress = trends[key]
                quotes[key].trend = list(quotes[key].trend)
        except RuntimeError as exc:
            quotes[key] = Quote(definition["name"], definition["symbol"], error=str(exc))
    return quotes


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def clamp_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend_hex(foreground: str, background: str, opacity: float) -> str:
    opacity = clamp_float(opacity, 1.0, 0.0, 1.0)
    fg = hex_to_rgb(foreground)
    bg = hex_to_rgb(background)
    mixed = [round(fg[index] * opacity + bg[index] * (1 - opacity)) for index in range(3)]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def is_no_color(value) -> bool:
    return str(value or "").strip().lower() in NO_COLOR_VALUES


def normalize_color(value, default: str) -> str:
    if is_no_color(value):
        return ""
    value = str(value or "").strip()
    return value.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default


def normalize_removed_preset_keys(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [key for key in BASE_QUOTE_DEFS if key in value]


def normalize_quote_order(value, first_custom_key: str = "", removed_preset_keys: list[str] | None = None) -> list[str]:
    removed = set(removed_preset_keys or [])
    known = [key for key in QUOTE_DEFS if key not in removed]
    if not isinstance(value, list):
        value = []
    ordered = []
    for item in value:
        if isinstance(item, str) and item.startswith("a_stock:"):
            item = custom_quote_key(item.removeprefix("a_stock:"))
        if isinstance(item, str) and item.startswith(CUSTOM_KEY_PREFIX):
            item = custom_quote_key(normalize_sina_code(custom_symbol_from_key(item)))
        key = first_custom_key if item == "a_stock" and first_custom_key else item
        if key in QUOTE_DEFS and key not in removed and key not in ordered:
            ordered.append(key)
    return ordered + [item for item in known if item not in ordered]


def normalize_quote_visible(value, first_custom_key: str = "", removed_preset_keys: list[str] | None = None) -> list[str]:
    removed = set(removed_preset_keys or [])
    if not isinstance(value, list):
        return [key for key in QUOTE_DEFS if key not in removed]
    visible = []
    for item in value:
        if isinstance(item, str) and item.startswith("a_stock:"):
            item = custom_quote_key(item.removeprefix("a_stock:"))
        if isinstance(item, str) and item.startswith(CUSTOM_KEY_PREFIX):
            item = custom_quote_key(normalize_sina_code(custom_symbol_from_key(item)))
        key = first_custom_key if item == "a_stock" and first_custom_key else item
        if key in QUOTE_DEFS and key not in removed and key not in visible:
            visible.append(key)
    return visible


def normalize_settings(settings: dict) -> dict:
    normalized = {**DEFAULT_SETTINGS, **settings}
    if "quote_order" not in settings and "quote_visible" not in settings:
        normalized["height"] = max(clamp_int(normalized.get("height"), DEFAULT_SETTINGS["height"], 90, 500), 260)
    if normalized.get("up") == "#93b7a2" and normalized.get("down") == "#b99a9a":
        normalized["up"] = DEFAULT_SETTINGS["up"]
        normalized["down"] = DEFAULT_SETTINGS["down"]
    if "background_opacity" not in settings and "opacity" in settings:
        normalized["background_opacity"] = settings["opacity"]
    normalized.pop("opacity", None)
    normalized["width"] = clamp_int(normalized.get("width"), DEFAULT_SETTINGS["width"], 220, 800)
    normalized["height"] = clamp_int(normalized.get("height"), DEFAULT_SETTINGS["height"], 90, 1000)
    normalized["window_x"] = clamp_int(normalized.get("window_x"), DEFAULT_SETTINGS["window_x"], -10000, 10000)
    normalized["window_y"] = clamp_int(normalized.get("window_y"), DEFAULT_SETTINGS["window_y"], -10000, 10000)
    normalized["background_opacity"] = clamp_float(
        normalized.get("background_opacity"),
        DEFAULT_SETTINGS["background_opacity"],
        0.0,
        1.0,
    )
    normalized["trend_opacity"] = clamp_float(
        normalized.get("trend_opacity"),
        DEFAULT_SETTINGS["trend_opacity"],
        0.0,
        1.0,
    )
    normalized["text_opacity"] = clamp_float(
        normalized.get("text_opacity"),
        DEFAULT_SETTINGS["text_opacity"],
        0.0,
        1.0,
    )
    normalized["font_size"] = clamp_int(normalized.get("font_size"), DEFAULT_SETTINGS["font_size"], 6, 20)
    normalized["price_font_size"] = clamp_int(
        normalized.get("price_font_size"),
        DEFAULT_SETTINGS["price_font_size"],
        8,
        32,
    )
    normalized["price_decimals"] = clamp_int(
        normalized.get("price_decimals"),
        DEFAULT_SETTINGS["price_decimals"],
        0,
        6,
    )
    normalized["refresh_seconds"] = clamp_int(
        normalized.get("refresh_seconds"),
        DEFAULT_SETTINGS["refresh_seconds"],
        5,
        600,
    )
    normalized["always_on_top"] = bool(normalized.get("always_on_top"))
    normalized["removed_preset_keys"] = normalize_removed_preset_keys(normalized.get("removed_preset_keys"))
    sina_codes = normalize_sina_codes(
        normalized.get("sina_codes") or normalized.get("a_stock_codes") or normalized.get("a_stock_code")
    )
    normalized["sina_codes"] = sina_codes
    normalized.pop("a_stock_codes", None)
    normalized.pop("a_stock_code", None)
    sync_quote_defs(sina_codes)
    first_custom_key = custom_quote_key(sina_codes[0]) if sina_codes else ""
    normalized["quote_order"] = normalize_quote_order(
        normalized.get("quote_order"),
        first_custom_key,
        normalized["removed_preset_keys"],
    )
    normalized["quote_visible"] = normalize_quote_visible(
        normalized.get("quote_visible"),
        first_custom_key,
        normalized["removed_preset_keys"],
    )
    for key in COLOR_KEYS:
        normalized[key] = normalize_color(normalized.get(key, DEFAULT_SETTINGS[key]), DEFAULT_SETTINGS[key])
    return normalized


def load_settings() -> dict:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            return normalize_settings(json.load(file))
    except (OSError, json.JSONDecodeError):
        return normalize_settings({})


def save_settings(settings: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(normalize_settings(settings), file, ensure_ascii=False, indent=2)


class TickerApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("桌面实时行情")
        self.root.geometry(
            f"{self.settings['width']}x{self.settings['height']}"
            f"{self.settings['window_x']:+d}{self.settings['window_y']:+d}"
        )
        self.root.minsize(220, 90)
        self.root.configure(bg=TRANSPARENT_COLOR)
        self.root.attributes("-topmost", self.settings["always_on_top"])
        self.root.attributes("-alpha", 1.0)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.root.overrideredirect(True)
        self.bg_root = tk.Toplevel(self.root)
        self.bg_root.withdraw()
        self.bg_root.overrideredirect(True)
        self.bg_root.configure(bg=self.settings["background"] or TRANSPARENT_COLOR)
        self.bg_root.attributes("-topmost", self.settings["always_on_top"])
        self.bg_root.attributes("-alpha", self.settings["background_opacity"])
        self.bg_root.bind("<ButtonPress-1>", self.start_background_pointer)
        self.bg_root.bind("<B1-Motion>", self.drag_background_pointer)
        self.bg_root.bind("<ButtonRelease-1>", self.finish_background_pointer)
        self.bg_root.bind("<Double-Button-1>", lambda _event: self.request_refresh())

        self.drag_start = (0, 0)
        self.drag_mode = "move"
        self.resize_start = (0, 0, 0, 0)
        self.rows: dict[str, dict[str, tk.Widget]] = {}
        self.bg_widgets: list[tk.Widget] = []
        self.text_widgets: list[tk.Widget] = []
        self.muted_widgets: list[tk.Widget] = []
        self.button_widgets: list[tk.Button] = []
        self.latest_quotes: list[tuple[str, Quote]] = []
        self.sina_trends: dict[str, tuple[list[float], float]] = {}
        self.sina_trend_times: dict[str, float] = {}
        self.local_trends: dict[str, list[float]] = {}
        self.local_trend_starts: dict[str, datetime] = {}
        self.local_trend_day = datetime.now().date()
        self.refreshing = False
        self.refresh_pending = False
        self.refresh_job: str | None = None
        self.settings_window: tk.Toplevel | None = None
        self.locked = False
        self.position_restored = False
        self.starting = True

        self.font_title = font.Font(family="Microsoft YaHei UI", size=self.settings["font_size"])
        self.font_small = font.Font(family="Microsoft YaHei UI", size=max(6, self.settings["font_size"] - 1))
        self.font_price = font.Font(family="Segoe UI", size=self.settings["price_font_size"], weight="bold")

        self.build_ui()
        self.apply_settings()
        self.starting = False
        self.root.deiconify()
        self.root.update_idletasks()
        self.sync_background_window()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.bg_root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def build_ui(self) -> None:
        self.shell = tk.Frame(self.root, bg=self.settings["background"], highlightthickness=0)
        self.bg_widgets.append(self.shell)
        self.shell.pack(fill="both", expand=True, padx=2, pady=2)
        self.shell.bind("<ButtonPress-1>", self.start_drag)
        self.shell.bind("<B1-Motion>", self.drag)
        self.shell.bind("<Double-Button-1>", lambda _event: self.request_refresh())

        top = tk.Frame(self.shell, bg=self.settings["background"], height=28)
        self.bg_widgets.append(top)
        top.pack(fill="x")
        top.pack_propagate(False)
        top.bind("<ButtonPress-1>", self.start_drag)
        top.bind("<B1-Motion>", self.drag)

        title_box = tk.Frame(top, bg=self.settings["background"])
        self.bg_widgets.append(title_box)
        title_box.pack(side="left", fill="x", expand=True)
        title_box.bind("<ButtonPress-1>", self.start_drag)
        title_box.bind("<B1-Motion>", self.drag)

        self.status_label = tk.Label(title_box, text="行情", bg=self.settings["background"], fg=self.settings["muted"], font=self.font_small)
        self.muted_widgets.append(self.status_label)
        self.status_label.pack(anchor="w", padx=7, pady=(7, 0))

        self.refresh_button = self.action_button(top, "↻", self.refresh)
        self.pin_button = self.action_button(top, "⌖", self.toggle_top, active=True)
        self.settings_button = self.action_button(top, "⚙", self.open_settings)
        self.lock_button = self.action_button(top, "🔒", self.toggle_lock)
        self.close_button = self.action_button(top, "×", self.close, danger=True)

        self.content = tk.Frame(self.shell, bg=self.settings["background"])
        self.bg_widgets.append(self.content)
        self.content.pack(fill="both", expand=True, padx=6, pady=(0, 2))
        self.content.bind("<ButtonPress-1>", self.start_drag)
        self.content.bind("<B1-Motion>", self.drag)
        self.content.bind("<Double-Button-1>", lambda _event: self.request_refresh())

        self.empty_label = tk.Label(
            self.content,
            text="暂无行情，请在设置中添加",
            bg=self.settings["background"],
            fg=self.settings["muted"],
            font=self.font_title,
        )
        self.muted_widgets.append(self.empty_label)

        for key, definition in QUOTE_DEFS.items():
            self.rows[key] = self.create_row(self.content, definition["name"], definition["symbol"])
        self.apply_quote_visibility()

        footer = tk.Frame(self.shell, bg=self.settings["background"], height=14)
        self.bg_widgets.append(footer)
        footer.pack(fill="x", padx=7, pady=(0, 3))
        self.updated_label = tk.Label(footer, text="--", bg=self.settings["background"], fg=self.settings["muted"], font=self.font_small)
        self.muted_widgets.append(self.updated_label)
        self.updated_label.pack(side="left")
        self.resize_handle = tk.Label(footer, text="◢", bg=self.settings["background"], fg=self.settings["button"], font=self.font_small, cursor="size_nw_se")
        self.muted_widgets.append(self.resize_handle)
        self.resize_handle.pack(side="right", padx=(5, 0))
        self.resize_handle.bind("<ButtonPress-1>", self.start_resize)
        self.resize_handle.bind("<B1-Motion>", self.resize)
        self.resize_handle.bind("<ButtonRelease-1>", self.finish_resize)
        self.latency_label = tk.Label(footer, text="右键退出", bg=self.settings["background"], fg=self.settings["muted"], font=self.font_small)
        self.muted_widgets.append(self.latency_label)
        self.latency_label.pack(side="right")

    def action_button(self, parent, text, command, active=False, danger=False, width=3):
        color = self.settings["accent"] if active else self.settings["button"]
        if danger:
            color = self.settings["button"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.settings["background"],
            fg=color,
            activebackground=self.settings["border"],
            activeforeground=self.settings["text"],
            borderwidth=0,
            width=width,
            height=1,
            font=self.font_small,
            cursor="hand2",
            padx=2,
            pady=2,
        )
        self.button_widgets.append(button)
        button.pack(side="left", padx=(0, 2), pady=2)
        return button

    def create_row(self, parent, name, symbol):
        row = tk.Frame(parent, bg=self.settings["background"])
        self.bg_widgets.append(row)
        row.pack(fill="x", expand=True, pady=0)
        row.bind("<ButtonPress-1>", self.start_drag)
        row.bind("<B1-Motion>", self.drag)
        row.bind("<Double-Button-1>", lambda _event: self.request_refresh())

        left = tk.Frame(row, bg=self.settings["background"])
        self.bg_widgets.append(left)
        left.pack(side="left", fill="x", expand=True, padx=2, pady=1)
        name_label = tk.Label(left, text=name, bg=self.settings["background"], fg=self.settings["text"], font=self.font_title)
        self.text_widgets.append(name_label)
        name_label.pack(anchor="w")

        right = tk.Frame(row, bg=self.settings["background"])
        self.bg_widgets.append(right)
        right.pack(side="right", padx=2, pady=1)
        price_label = tk.Label(right, text="--", bg=self.settings["background"], fg=self.settings["price"], font=self.font_price, width=10, anchor="e")
        price_label.pack(side="left")
        change_label = tk.Label(right, text="--", bg=self.settings["background"], fg=self.settings["muted"], font=self.font_small, width=9, anchor="e")
        self.muted_widgets.append(change_label)
        change_label.pack(side="left", padx=(4, 0))

        trend_canvas = tk.Canvas(
            row,
            bg=self.settings["background"],
            width=50,
            height=15,
            highlightthickness=0,
            borderwidth=0,
        )
        self.bg_widgets.append(trend_canvas)
        trend_canvas.pack(side="right", padx=(2, 2), pady=1)
        trend_canvas.bind("<ButtonPress-1>", self.start_drag)
        trend_canvas.bind("<B1-Motion>", self.drag)
        trend_canvas.bind("<Double-Button-1>", lambda _event: self.request_refresh())
        return {"row": row, "name": name_label, "trend": trend_canvas, "price": price_label, "change": change_label}

    def ordered_visible_quote_keys(self) -> list[str]:
        visible = set(self.settings["quote_visible"])
        return [key for key in self.settings["quote_order"] if key in visible and key in QUOTE_DEFS]

    def apply_quote_visibility(self) -> None:
        for row in self.rows.values():
            row["row"].pack_forget()
        visible_keys = self.ordered_visible_quote_keys()
        self.empty_label.pack_forget()
        if not visible_keys:
            self.empty_label.pack(anchor="w", padx=4, pady=(10, 6))
            return
        for key in visible_keys:
            self.rows[key]["row"].pack(fill="x", expand=True, pady=0)

    def ensure_quote_rows(self) -> None:
        for key, definition in QUOTE_DEFS.items():
            if key not in self.rows:
                self.rows[key] = self.create_row(self.content, definition["name"], definition["symbol"])
            else:
                self.rows[key]["name"].config(text=definition["name"])

    def apply_settings(self) -> None:
        self.settings = normalize_settings(self.settings)
        width = self.settings["width"]
        height = self.settings["height"]
        if self.position_restored:
            window_x = self.root.winfo_x()
            window_y = self.root.winfo_y()
        else:
            window_x = self.settings["window_x"]
            window_y = self.settings["window_y"]
            self.position_restored = True
        self.root.geometry(f"{width}x{height}{window_x:+d}{window_y:+d}")
        background = self.display_background()
        self.root.configure(bg=background)
        self.root.attributes("-alpha", 1.0)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.root.attributes("-topmost", self.settings["always_on_top"])
        self.sync_background_window()
        self.shell.config(
            highlightbackground=self.ui_color("border"),
            highlightthickness=0,
        )

        self.font_title.config(size=self.settings["font_size"])
        self.font_small.config(size=max(6, self.settings["font_size"] - 1))
        self.font_price.config(size=self.settings["price_font_size"])
        self.ensure_quote_rows()
        self.apply_quote_visibility()

        for widget in self.bg_widgets:
            widget.config(bg=background)
        for widget in self.text_widgets:
            widget.config(bg=background, fg=self.text_color("text"))
        for widget in self.muted_widgets:
            widget.config(bg=background, fg=self.text_color("muted"))
        for button in self.button_widgets:
            button.config(
                bg=background,
                activebackground=self.ui_color("border"),
                activeforeground=self.text_color("text"),
            )
        self.refresh_button.config(fg=self.text_color("button"))
        self.settings_button.config(fg=self.text_color("button"))
        self.close_button.config(fg=self.text_color("button"))
        self.resize_handle.config(fg=self.text_color("button"))
        self.pin_button.config(fg=self.text_color("accent") if self.settings["always_on_top"] else self.text_color("button"))
        self.lock_button.config(fg=self.text_color("accent") if self.locked else self.text_color("button"))

        if self.latest_quotes:
            for key, quote in self.latest_quotes:
                if key in self.rows:
                    self.update_row(key, quote)
        else:
            for row in self.rows.values():
                row["price"].config(bg=background, fg=self.text_color("price"))

    def is_background_transparent(self) -> bool:
        return self.settings["background_opacity"] <= 0.0 or is_no_color(self.settings["background"])

    def display_background(self) -> str:
        return TRANSPARENT_COLOR

    def sync_background_window(self) -> None:
        if self.starting or self.is_background_transparent():
            self.bg_root.withdraw()
            return
        self.root.update_idletasks()
        self.bg_root.configure(bg=self.settings["background"])
        self.bg_root.geometry(
            f"{self.root.winfo_width()}x{self.root.winfo_height()}+{self.root.winfo_x()}+{self.root.winfo_y()}"
        )
        self.bg_root.attributes("-alpha", self.settings["background_opacity"])
        self.bg_root.attributes("-topmost", self.settings["always_on_top"])
        self.bg_root.deiconify()
        self.keep_background_behind()

    def keep_background_behind(self) -> None:
        if self.bg_root.winfo_exists() and self.root.winfo_exists():
            self.bg_root.lower(self.root)

    def text_color(self, key: str) -> str:
        value = self.settings[key]
        if is_no_color(value):
            return self.display_background()
        background = self.settings["background"]
        if is_no_color(background):
            return value
        return blend_hex(value, background, self.settings["text_opacity"])

    def ui_color(self, key: str) -> str:
        value = self.settings[key]
        return self.display_background() if is_no_color(value) else value

    def open_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("设置")
        window.geometry("460x660")
        window.resizable(False, False)
        window.configure(bg="#eef2f7")
        window.attributes("-topmost", bool(self.root.attributes("-topmost")))
        window.bind("<Button-3>", lambda _event: window.focus_force())

        values = {
            "background_opacity": tk.IntVar(value=int(self.settings["background_opacity"] * 100)),
            "trend_opacity": tk.IntVar(value=int(self.settings["trend_opacity"] * 100)),
            "text_opacity": tk.IntVar(value=int(self.settings["text_opacity"] * 100)),
            "font_size": tk.StringVar(value=str(self.settings["font_size"])),
            "price_font_size": tk.StringVar(value=str(self.settings["price_font_size"])),
            "price_decimals": tk.StringVar(value=str(self.settings["price_decimals"])),
            "refresh_seconds": tk.StringVar(value=str(self.settings["refresh_seconds"])),
            "sina_codes": tk.StringVar(value=""),
            "always_on_top": tk.BooleanVar(value=self.settings["always_on_top"]),
        }
        colors = {
            key: tk.StringVar(value=self.settings[key])
            for key in ["background", "text", "price", "up", "down", "flat", "border"]
        }
        quote_order = list(self.settings["quote_order"])
        removed_preset_keys = set(self.settings["removed_preset_keys"])
        quote_visible = {
            key: tk.BooleanVar(value=key in self.settings["quote_visible"])
            for key in QUOTE_DEFS
        }

        style = ttk.Style(window)
        style.configure("Settings.TNotebook", background="#eef2f7", borderwidth=0)
        style.configure("Settings.TNotebook.Tab", padding=(14, 7))

        header = tk.Frame(window, bg="#eef2f7")
        header.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(header, text="行情窗口设置", bg="#eef2f7", fg="#111827", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(header, text="滑条是 0-100，拖动时只更新数字，松开后生效。", bg="#eef2f7", fg="#64748b").pack(anchor="w", pady=(3, 0))

        notebook = ttk.Notebook(window, style="Settings.TNotebook")
        notebook.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        def make_scrollable_tab(parent) -> tuple[tk.Frame, tk.Frame]:
            outer = tk.Frame(parent, bg="#ffffff")
            canvas = tk.Canvas(outer, bg="#ffffff", highlightthickness=0)
            scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            content = tk.Frame(canvas, bg="#ffffff")
            content_window = canvas.create_window((0, 0), window=content, anchor="nw")

            def update_scroll_region(_event=None) -> None:
                canvas.configure(scrollregion=canvas.bbox("all"))

            def resize_content(event) -> None:
                canvas.itemconfigure(content_window, width=event.width)

            def scroll_with_wheel(event) -> None:
                canvas.yview_scroll(int(-event.delta / 120), "units")

            def bind_mousewheel(_event) -> None:
                canvas.bind_all("<MouseWheel>", scroll_with_wheel)

            def unbind_mousewheel(_event) -> None:
                canvas.unbind_all("<MouseWheel>")

            content.bind("<Configure>", update_scroll_region)
            canvas.bind("<Configure>", resize_content)
            outer.bind("<Enter>", bind_mousewheel)
            outer.bind("<Leave>", unbind_mousewheel)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            return outer, content

        display_tab = tk.Frame(notebook, bg="#ffffff")
        quotes_tab_outer, quotes_tab = make_scrollable_tab(notebook)
        color_tab = tk.Frame(notebook, bg="#ffffff")
        behavior_tab = tk.Frame(notebook, bg="#ffffff")
        notebook.add(quotes_tab_outer, text="行情")
        notebook.add(display_tab, text="显示")
        notebook.add(color_tab, text="颜色")
        notebook.add(behavior_tab, text="行为")

        def apply_from_form(
            close_after: bool = False,
            refresh_quote_list: bool = True,
            sina_codes_override: list[str] | None = None,
            clear_sina_input: bool = True,
        ) -> None:
            previous_sina_codes = list(self.settings["sina_codes"])
            previous_removed_preset_keys = list(self.settings["removed_preset_keys"])
            previous_visible_quotes = set(self.settings["quote_visible"])
            if sina_codes_override is None:
                next_sina_codes = list(previous_sina_codes)
                for code in resolve_sina_codes(values["sina_codes"].get()):
                    if code not in next_sina_codes:
                        next_sina_codes.append(code)
            else:
                next_sina_codes = normalize_sina_codes(sina_codes_override)
            sync_quote_defs(next_sina_codes)
            custom_keys = [custom_quote_key(code) for code in next_sina_codes]
            current_custom_indexes = [
                index for index, key in enumerate(quote_order)
                if key.startswith(CUSTOM_KEY_PREFIX) and key in custom_keys
            ]
            next_order = [
                key for key in quote_order
                if key in QUOTE_DEFS
                and key not in removed_preset_keys
                and (not key.startswith(CUSTOM_KEY_PREFIX) or key in custom_keys)
            ]
            new_custom_keys = [key for key in custom_keys if key not in next_order]
            insert_at = (max(current_custom_indexes) + 1) if current_custom_indexes else 0
            for key in new_custom_keys:
                next_order.insert(insert_at, key)
                insert_at += 1
            quote_order[:] = next_order + [
                key for key in QUOTE_DEFS
                if key not in removed_preset_keys and key not in next_order
            ]
            for old_key in list(quote_visible):
                if old_key.startswith(CUSTOM_KEY_PREFIX) and old_key not in custom_keys:
                    quote_visible.pop(old_key, None)
            for key in custom_keys:
                quote_visible.setdefault(key, tk.BooleanVar(value=True))
            selected_quotes = [
                key for key, variable in quote_visible.items()
                if variable.get() and key in quote_order
            ]
            next_settings = {
                **self.settings,
                "background_opacity": clamp_float(values["background_opacity"].get(), 62, 0, 100) / 100,
                "trend_opacity": clamp_float(values["trend_opacity"].get(), 38, 0, 100) / 100,
                "text_opacity": clamp_float(values["text_opacity"].get(), 68, 0, 100) / 100,
                "font_size": values["font_size"].get(),
                "price_font_size": values["price_font_size"].get(),
                "price_decimals": values["price_decimals"].get(),
                "refresh_seconds": values["refresh_seconds"].get(),
                "sina_codes": next_sina_codes,
                "removed_preset_keys": list(removed_preset_keys),
                "always_on_top": values["always_on_top"].get(),
                "quote_order": quote_order,
                "quote_visible": selected_quotes,
            }
            next_settings.update({key: variable.get() for key, variable in colors.items()})
            self.settings = normalize_settings(next_settings)
            save_settings(self.settings)
            self.apply_settings()
            if clear_sina_input:
                values["sina_codes"].set("")
            if self.refresh_job is not None:
                self.root.after_cancel(self.refresh_job)
                self.refresh_job = self.root.after(self.settings["refresh_seconds"] * 1000, self.refresh)
            quote_list_changed = (
                self.settings["sina_codes"] != previous_sina_codes
                or self.settings["removed_preset_keys"] != previous_removed_preset_keys
            )
            visible_quotes_changed = set(self.settings["quote_visible"]) != previous_visible_quotes
            if quote_list_changed:
                if refresh_quote_list:
                    render_quote_settings()
            if quote_list_changed or visible_quotes_changed:
                self.refresh()
            if close_after:
                window.destroy()

        def section(parent, title: str, hint: str = "") -> tk.Frame:
            box = tk.Frame(parent, bg="#ffffff", highlightbackground="#d8dee9", highlightthickness=1)
            box.pack(fill="x", padx=12, pady=(12, 0))
            tk.Label(box, text=title, bg="#ffffff", fg="#111827", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
            if hint:
                tk.Label(box, text=hint, bg="#ffffff", fg="#64748b", wraplength=370, justify="left").pack(anchor="w", padx=12, pady=(2, 0))
            return box

        def add_entry(parent, label: str, key: str, suffix: str = "") -> None:
            row = tk.Frame(parent, bg="#ffffff")
            row.pack(fill="x", padx=12, pady=8)
            tk.Label(row, text=label, bg="#ffffff", fg="#334155", width=12, anchor="w").pack(side="left")
            entry = tk.Entry(row, textvariable=values[key], width=10)
            entry.pack(side="left", padx=(6, 4))
            entry.bind("<Return>", lambda _event: apply_from_form(False))
            entry.bind("<FocusOut>", lambda _event: apply_from_form(False))
            if suffix:
                tk.Label(row, text=suffix, bg="#ffffff", fg="#64748b").pack(side="left")

        def add_wide_entry(parent, label: str, key: str) -> None:
            row = tk.Frame(parent, bg="#ffffff")
            row.pack(fill="x", padx=12, pady=8)
            tk.Label(row, text=label, bg="#ffffff", fg="#334155", width=12, anchor="w").pack(side="left")
            entry = tk.Entry(row, textvariable=values[key], width=26)
            entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
            entry.bind("<Return>", lambda _event: apply_from_form(False))
            tk.Button(row, text="添加", command=lambda: apply_from_form(False), width=6).pack(side="left", padx=(6, 0))

        def add_scale(parent, label: str, key: str, suffix: str = "") -> None:
            row = tk.Frame(parent, bg="#ffffff")
            row.pack(fill="x", padx=12, pady=10)
            tk.Label(row, text=label, bg="#ffffff", fg="#334155", width=12, anchor="w").pack(side="left")
            value_label = tk.Label(row, text=f"{values[key].get()}{suffix}", bg="#ffffff", fg="#0f172a", width=7)
            value_label.pack(side="right")
            scale = tk.Scale(
                row,
                from_=0,
                to=100,
                orient="horizontal",
                variable=values[key],
                showvalue=False,
                length=245,
                resolution=1,
                bg="#ffffff",
                highlightthickness=0,
                troughcolor="#e2e8f0",
                command=lambda value: value_label.config(text=f"{int(float(value))}{suffix}"),
            )
            scale.pack(side="left", padx=(6, 4))
            scale.bind("<ButtonRelease-1>", lambda _event: apply_from_form(False))
            scale.bind("<KeyRelease>", lambda _event: apply_from_form(False))

        def add_color(parent, label: str, key: str) -> None:
            row = tk.Frame(parent, bg="#ffffff")
            row.pack(fill="x", padx=12, pady=7)
            tk.Label(row, text=label, bg="#ffffff", fg="#334155", width=12, anchor="w").pack(side="left")
            sample_bg = colors[key].get() if colors[key].get() else "#f8fafc"
            sample = tk.Label(row, text="无" if not colors[key].get() else "", bg=sample_bg, fg="#6b7280", width=3, relief="groove")
            sample.pack(side="left", padx=(6, 4))
            entry = tk.Entry(row, textvariable=colors[key], width=11)
            entry.pack(side="left", padx=(0, 6))
            entry.bind("<Return>", lambda _event: apply_from_form(False))
            entry.bind("<FocusOut>", lambda _event: apply_from_form(False))

            def sync_sample(*_args) -> None:
                value = colors[key].get()
                if is_no_color(value):
                    sample.config(text="无", bg="#f8fafc")
                elif re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    sample.config(text="", bg=value)

            colors[key].trace_add("write", sync_sample)

            def choose() -> None:
                was_topmost = bool(self.root.attributes("-topmost"))
                self.root.attributes("-topmost", False)
                window.attributes("-topmost", False)
                window.lift()
                try:
                    result = colorchooser.askcolor(color=colors[key].get() or DEFAULT_SETTINGS[key], parent=window, title=f"选择{label}")
                finally:
                    self.root.attributes("-topmost", was_topmost)
                    window.attributes("-topmost", was_topmost)
                if result and result[1]:
                    colors[key].set(result[1])
                    apply_from_form(False)

            tk.Button(row, text="选择", command=choose, width=7).pack(side="left")
            tk.Button(row, text="无", command=lambda: (colors[key].set(""), apply_from_form(False)), width=4).pack(side="left", padx=(4, 0))

        drag_state = {"key": None, "target": None, "after": False}
        quote_row_keys: dict[tk.Widget, str] = {}
        quote_rows: dict[str, tk.Frame] = {}

        def find_drop_target_at_pointer(y_root: int) -> tuple[str | None, bool]:
            fallback_key = None
            for key in quote_order:
                row = quote_rows.get(key)
                if row is None:
                    continue
                top = row.winfo_rooty()
                bottom = top + row.winfo_height()
                if y_root < top:
                    return key, False
                if top <= y_root <= bottom:
                    return key, y_root >= top + row.winfo_height() / 2
                fallback_key = key
            if fallback_key:
                return fallback_key, True
            return None

        def set_row_drag_highlight(quote_key: str | None) -> None:
            for key, row in quote_rows.items():
                row.config(bg="#eef2ff" if key == quote_key else "#ffffff")

        def move_quote_to_target(quote_key: str, target_key: str, after_target: bool) -> None:
            if quote_key == target_key or quote_key not in quote_order or target_key not in quote_order:
                return
            quote_order.remove(quote_key)
            target_index = quote_order.index(target_key)
            if after_target:
                target_index += 1
            quote_order.insert(target_index, quote_key)
            render_quote_settings()
            apply_from_form(False, refresh_quote_list=False)

        def start_quote_drag(quote_key: str) -> None:
            drag_state["key"] = quote_key
            drag_state["target"] = None
            drag_state["after"] = False
            set_row_drag_highlight(quote_key)
            window.bind_all("<B1-Motion>", drag_quote)
            window.bind_all("<ButtonRelease-1>", finish_quote_drag)

        def drag_quote(event) -> None:
            quote_key = drag_state["key"]
            if not quote_key:
                return
            target_key, after_target = find_drop_target_at_pointer(event.y_root)
            if target_key and target_key != quote_key:
                drag_state["target"] = target_key
                drag_state["after"] = after_target
                set_row_drag_highlight(target_key)

        def finish_quote_drag(_event) -> None:
            quote_key = drag_state["key"]
            target_key = drag_state["target"]
            after_target = bool(drag_state["after"])
            drag_state["key"] = None
            drag_state["target"] = None
            drag_state["after"] = False
            set_row_drag_highlight(None)
            window.unbind_all("<B1-Motion>")
            window.unbind_all("<ButtonRelease-1>")
            if quote_key and target_key:
                move_quote_to_target(quote_key, target_key, after_target)

        def remove_custom_quote(symbol: str) -> None:
            remaining_codes = [code for code in self.settings["sina_codes"] if code != symbol]
            apply_from_form(
                False,
                sina_codes_override=remaining_codes,
            )

        def remove_quote(quote_key: str) -> None:
            if quote_key.startswith(CUSTOM_KEY_PREFIX):
                remove_custom_quote(custom_symbol_from_key(quote_key))
                return
            removed_preset_keys.add(quote_key)
            if quote_key in quote_order:
                quote_order.remove(quote_key)
            quote_visible.pop(quote_key, None)
            apply_from_form(False, refresh_quote_list=False)
            render_quote_settings()

        def render_quote_settings() -> None:
            quote_row_keys.clear()
            quote_rows.clear()
            for child in quote_list_box.winfo_children():
                child.destroy()
            if not quote_order:
                tk.Label(
                    quote_list_box,
                    text="暂无行情，请在下方添加代码或名称。",
                    bg="#ffffff",
                    fg="#64748b",
                ).pack(anchor="w", padx=12, pady=10)
                return
            for key in quote_order:
                definition = QUOTE_DEFS[key]
                row = tk.Frame(quote_list_box, bg="#ffffff")
                quote_row_keys[row] = key
                quote_rows[key] = row
                row.pack(fill="x", padx=12, pady=4)
                tk.Checkbutton(
                    row,
                    text=f"{definition['name']}  {definition['symbol']}",
                    variable=quote_visible[key],
                    command=lambda: apply_from_form(False),
                    bg="#ffffff",
                    fg="#111827",
                    anchor="w",
                    width=23,
                ).pack(side="left")

                handle = tk.Label(row, text="☰", bg="#ffffff", fg="#64748b", width=3, cursor="sb_v_double_arrow")
                handle.pack(side="right", padx=(4, 0))
                handle.bind("<ButtonPress-1>", lambda _event, quote_key=key: start_quote_drag(quote_key))
                tk.Button(row, text="删除", command=lambda quote_key=key: remove_quote(quote_key), width=4).pack(side="right")

        quote_list_box = section(quotes_tab, "显示行情", "勾选要显示的品种，拖动排序；删除预设后可点“恢复默认”找回。")
        render_quote_settings()
        stock_box = section(quotes_tab, "自选新浪行情", "可输入 App 里看到的代码或名称，例如 NKY、HSKTECH、纳斯达克、纽约黄金；程序会自动查找真实行情代码。")
        add_wide_entry(stock_box, "代码或名称", "sina_codes")

        opacity_box = section(display_tab, "透明度", "0 表示完全透明，100 表示完全显示。")
        add_scale(opacity_box, "背景透明度", "background_opacity", "%")
        add_scale(opacity_box, "趋势线透明度", "trend_opacity", "%")
        add_scale(opacity_box, "文字透明度", "text_opacity", "%")
        tk.Label(display_tab, text="窗口大小：拖动主窗口右下角的小角标调整，松开后自动保存。", bg="#ffffff", fg="#64748b", wraplength=380, justify="left").pack(anchor="w", padx=24, pady=(12, 0))

        font_box = section(display_tab, "字号")
        add_entry(font_box, "普通字号", "font_size")
        add_entry(font_box, "价格字号", "price_font_size")
        add_entry(font_box, "小数位数", "price_decimals", "位")

        color_box = section(color_tab, "颜色", "可输入 #RRGGBB，点“选择”打开颜色面板，点“无”隐藏这个颜色。")
        add_color(color_box, "背景颜色", "background")
        add_color(color_box, "文字颜色", "text")
        add_color(color_box, "价格颜色", "price")
        add_color(color_box, "上涨颜色", "up")
        add_color(color_box, "下跌颜色", "down")
        add_color(color_box, "平盘颜色", "flat")
        add_color(color_box, "边框颜色", "border")

        behavior_box = section(behavior_tab, "刷新和窗口")
        add_entry(behavior_box, "刷新间隔", "refresh_seconds", "秒")
        tk.Checkbutton(
            behavior_box,
            text="启动后默认置顶",
            variable=values["always_on_top"],
            command=lambda: apply_from_form(False),
            bg="#ffffff",
            fg="#111827",
        ).pack(anchor="w", padx=12, pady=10)

        actions = tk.Frame(window, bg="#eef2f7")
        actions.pack(fill="x", padx=18, pady=(0, 16))

        def reset_defaults() -> None:
            self.settings = normalize_settings({})
            save_settings(self.settings)
            self.apply_settings()
            window.destroy()

        tk.Button(actions, text="恢复默认", command=reset_defaults, width=10).pack(side="left")
        tk.Button(actions, text="应用", command=lambda: apply_from_form(False), width=10).pack(side="right")
        tk.Button(actions, text="保存关闭", command=lambda: apply_from_form(True), width=10).pack(side="right", padx=(0, 8))

    def request_refresh(self) -> None:
        if not self.locked:
            self.refresh()

    def refresh(self) -> None:
        if self.refreshing:
            self.refresh_pending = True
            return
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        self.refreshing = True
        self.refresh_pending = False
        self.status_label.config(text="刷新")
        threading.Thread(target=self.fetch_in_background, daemon=True).start()

    def fetch_in_background(self) -> None:
        start = time.perf_counter()
        visible_keys = set(self.ordered_visible_quote_keys())
        if not visible_keys:
            elapsed = int((time.perf_counter() - start) * 1000)
            self.root.after(0, lambda: self.apply_quotes([], elapsed))
            return
        trend_keys = list(visible_keys)
        now = time.time()
        stale_keys = [
            key for key in trend_keys if now - self.sina_trend_times.get(key, 0) >= 300
        ]
        if stale_keys:
            self.sina_trends.update(fetch_sina_trends(stale_keys))
            for key in stale_keys:
                self.sina_trend_times[key] = now
        try:
            sina_quotes = fetch_sina_quotes(self.settings["sina_codes"], self.sina_trends)
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            sina_quotes = {
                key: Quote(definition["name"], definition["symbol"], error=str(exc))
                for key, definition in QUOTE_DEFS.items()
            }
        except Exception as exc:
            sina_quotes = {
                key: Quote(definition["name"], definition["symbol"], error=f"未知错误：{exc}")
                for key, definition in QUOTE_DEFS.items()
            }
        quotes = [
            *[
                (
                    custom_quote_key(symbol),
                    sina_quotes.get(custom_quote_key(symbol)) or Quote("自选行情", symbol, error="未设置新浪代码"),
                )
                for symbol in self.settings["sina_codes"]
            ],
            ("xau", sina_quotes["xau"]),
            ("cn", sina_quotes["cn"]),
            ("nq", sina_quotes["nq"]),
            ("sh", sina_quotes["sh"]),
            ("star", sina_quotes["star"]),
            ("oil", sina_quotes["oil"]),
            ("usd", sina_quotes["usd"]),
            ("jpy", sina_quotes["jpy"]),
        ]
        elapsed = int((time.perf_counter() - start) * 1000)
        self.root.after(0, lambda: self.apply_quotes(quotes, elapsed))

    def apply_quotes(self, quotes, elapsed: int) -> None:
        today = datetime.now().date()
        if today != self.local_trend_day:
            self.local_trends.clear()
            self.local_trend_starts.clear()
            self.local_trend_day = today
        for key, quote in quotes:
            if quote.price is None:
                continue
            local_values = self.local_trends.setdefault(key, [])
            local_start = self.local_trend_starts.setdefault(key, datetime.now())
            local_values.append(quote.price)
            if len(local_values) > 2000:
                del local_values[:-2000]
            if quote.trend:
                if quote.trend[-1] != quote.price:
                    quote.trend = [*quote.trend, quote.price]
            else:
                quote.trend = list(local_values)
                elapsed = max(0.0, (datetime.now() - local_start).total_seconds())
                quote.trend_progress = min(1.0, max(0.04, elapsed / fallback_trend_span_seconds(quote.symbol)))
        self.latest_quotes = quotes
        ok_count = 0
        visible = set(self.ordered_visible_quote_keys())
        visible_count = len(visible)
        for key, quote in quotes:
            self.update_row(key, quote)
            if key in visible and quote.error is None:
                ok_count += 1
        if visible_count == 0:
            self.status_label.config(text="暂无行情")
        else:
            self.status_label.config(text="行情" if ok_count == visible_count else f"{ok_count}/{visible_count}")
        self.updated_label.config(text=datetime.now().strftime("%H:%M:%S"))
        self.latency_label.config(text=f"{elapsed}ms")
        self.refreshing = False
        if self.refresh_pending:
            self.refresh_pending = False
            self.refresh()
            return
        self.refresh_job = self.root.after(self.settings["refresh_seconds"] * 1000, self.refresh)

    def update_row(self, key: str, quote: Quote) -> None:
        if key not in self.rows:
            return
        row = self.rows[key]
        background = self.display_background()
        row["name"].config(text=quote.name)
        row["price"].config(bg=background)
        row["change"].config(bg=background)
        self.draw_trend(row["trend"], quote.trend, quote.trend_progress, quote.direction)
        if quote.error:
            row["price"].config(text="--", fg=self.text_color("muted"))
            row["change"].config(text="失败", fg=self.text_color("muted"))
            return

        color = {
            "up": self.text_color("up"),
            "down": self.text_color("down"),
            "flat": self.text_color("flat"),
        }[quote.direction]
        decimals = self.settings.get("price_decimals", quote.decimals)
        row["price"].config(text=f"{quote.price:,.{decimals}f}", fg=color)

        parts = []
        if quote.percent is not None:
            parts.append(f"{quote.percent:+.2f}%")
        elif quote.change is not None:
            parts.append(f"{quote.change:+,.2f}")
        row["change"].config(text=" / ".join(parts) if parts else "--", fg=color)

    def draw_trend(
        self,
        canvas: tk.Canvas,
        values: list[float],
        progress: float = 1.0,
        direction: str | None = None,
    ) -> None:
        canvas.delete("all")
        values = [value for value in values if math.isfinite(value)]
        if not values:
            return
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        draw_width = max(2, round((width - 2) * max(0.0, min(1.0, progress))))
        if len(values) > draw_width:
            values = [
                values[round(index * (len(values) - 1) / (draw_width - 1))]
                for index in range(draw_width)
            ]
        color_key = direction if direction in {"up", "down", "flat"} else "flat"
        if direction not in {"up", "down", "flat"}:
            if values[-1] > values[0]:
                color_key = "up"
            elif values[-1] < values[0]:
                color_key = "down"
        color = self.trend_color(color_key)
        if len(values) == 1:
            canvas.create_line(1, height // 2, draw_width, height // 2, fill=color, width=1)
            return
        low = min(values)
        high = max(values)
        span = high - low
        points = []
        for index, value in enumerate(values):
            x = 1 + index * (draw_width - 1) / (len(values) - 1)
            y = height / 2 if span == 0 else 1 + (high - value) * (height - 2) / span
            points.extend((x, y))
        canvas.create_line(*points, fill=color, width=1, smooth=True)

    def trend_color(self, color_key: str) -> str:
        color = self.settings[color_key]
        if is_no_color(color):
            return self.display_background()
        background = self.settings["background"]
        if is_no_color(background):
            return color
        return blend_hex(color, background, self.settings["trend_opacity"])

    def toggle_top(self) -> None:
        current = bool(self.root.attributes("-topmost"))
        next_value = not current
        self.root.attributes("-topmost", next_value)
        self.bg_root.attributes("-topmost", next_value)
        self.settings["always_on_top"] = next_value
        save_settings(self.settings)
        self.pin_button.config(fg=self.settings["accent"] if next_value else self.settings["button"])
        self.sync_background_window()

    def toggle_lock(self) -> None:
        self.locked = not self.locked
        self.lock_button.config(
            text="🔓" if self.locked else "🔒",
            fg=self.text_color("accent") if self.locked else self.text_color("button"),
        )

    def invoke_action_button_at(self, x_root: int, y_root: int) -> bool:
        for button in self.button_widgets:
            if str(button.cget("state")) != "normal":
                continue
            left = button.winfo_rootx()
            top = button.winfo_rooty()
            if left <= x_root < left + button.winfo_width() and top <= y_root < top + button.winfo_height():
                button.invoke()
                return True
        return False

    def start_drag(self, event) -> None:
        if self.locked:
            return
        self.drag_mode = "move"
        self.drag_start = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event) -> None:
        if self.locked:
            return
        x = event.x_root - self.drag_start[0]
        y = event.y_root - self.drag_start[1]
        self.root.geometry(f"+{x}+{y}")
        if not self.is_background_transparent():
            self.bg_root.geometry(f"{self.root.winfo_width()}x{self.root.winfo_height()}+{x}+{y}")
            self.keep_background_behind()

    def start_resize(self, event) -> None:
        if self.locked:
            return
        self.drag_mode = "resize"
        self.resize_start = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def start_background_pointer(self, event) -> None:
        self.keep_background_behind()
        if self.invoke_action_button_at(event.x_root, event.y_root):
            return
        if self.locked:
            return
        if event.x >= self.bg_root.winfo_width() - 18 and event.y >= self.bg_root.winfo_height() - 18:
            self.start_resize(event)
        else:
            self.start_drag(event)

    def drag_background_pointer(self, event) -> None:
        if self.locked:
            return
        if self.drag_mode == "resize":
            self.resize(event)
        else:
            self.drag(event)

    def finish_background_pointer(self, event) -> None:
        if self.locked:
            return
        if self.drag_mode == "resize":
            self.finish_resize(event)

    def resize(self, event) -> None:
        if self.locked:
            return
        start_x, start_y, start_width, start_height = self.resize_start
        width = max(220, min(800, start_width + event.x_root - start_x))
        height = max(90, min(1000, start_height + event.y_root - start_y))
        self.root.geometry(f"{width}x{height}")
        if not self.is_background_transparent():
            self.bg_root.geometry(f"{width}x{height}+{self.root.winfo_x()}+{self.root.winfo_y()}")
            self.keep_background_behind()

    def finish_resize(self, _event) -> None:
        if self.locked:
            return
        self.settings["width"] = self.root.winfo_width()
        self.settings["height"] = self.root.winfo_height()
        save_settings(self.settings)
        self.sync_background_window()

    def close(self) -> None:
        try:
            if self.root.winfo_exists():
                self.settings["window_x"] = self.root.winfo_x()
                self.settings["window_y"] = self.root.winfo_y()
                save_settings(self.settings)
        except tk.TclError:
            pass
        if self.refresh_job is not None:
            try:
                self.root.after_cancel(self.refresh_job)
            except tk.TclError:
                pass
            self.refresh_job = None
        for window in (self.settings_window, self.bg_root, self.root):
            try:
                if window is not None and window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            try:
                if self.bg_root.winfo_exists():
                    self.bg_root.destroy()
            except tk.TclError:
                pass


if __name__ == "__main__" and acquire_single_instance():
    try:
        TickerApp().run()
    finally:
        release_single_instance()
