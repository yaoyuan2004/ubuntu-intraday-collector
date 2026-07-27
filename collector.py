#!/usr/bin/env python3
"""Ubuntu 轻量行情采集器 v1.1。

目标：
- 单一 instruments.csv 维护池，只需增加六位代码或 AU999；
- A 股/场内基金/指数通过新浪批量行情采集；
- Au99.99 通过上海黄金交易所网页数据接口采集；
- 场外基金自动识别为 reference_only，不伪造盘中价格；
- SQLite 单文件保存；
- 仅依赖 Python 标准库；
- systemd 常驻，内部按 Asia/Shanghai 调度。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import shutil
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.json"
CODE_RE = re.compile(r"^\d{6}$")
SINA_LINE_RE = re.compile(r'var hq_str_(\w+)="(.*?)";')
STOP_REQUESTED = False

SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

SGE_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://www.sge.com.cn",
    "Referer": "https://www.sge.com.cn/sjzx/mrhq",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

GOLD_ALIASES = {"AU999", "AU9999", "AU99.99", "AU99_99", "AU99-99"}


@dataclass(frozen=True)
class PoolItem:
    raw_code: str
    canonical_code: str
    kind: str  # numeric / gold


@dataclass(frozen=True)
class Instrument:
    instrument_key: str
    code: str
    symbol: str
    asset_type: str  # market_security / index / gold / reference_only
    exchange: str
    configured_name: str = ""
    origin: str = "pool"  # pool / builtin


@dataclass(frozen=True)
class Quote:
    instrument_key: str
    symbol: str
    code: str
    asset_type: str
    exchange: str
    name: str
    quote_date: str
    quote_time: str
    open_price: float | None
    previous_close: float | None
    last_price: float | None
    high_price: float | None
    low_price: float | None
    change_amount: float | None
    change_pct: float | None
    volume: float | None
    amount: float | None
    interval_minutes: float | None
    interval_volume: float | None
    interval_amount: float | None
    turnover_rate: float | None
    source: str
    valid: int
    error: str


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def normalize_pool_code(raw: Any) -> PoolItem:
    value = str(raw or "").strip().lstrip("\ufeff")
    upper = value.upper().replace(" ", "")
    if upper in GOLD_ALIASES:
        return PoolItem(value, "Au99.99", "gold")
    if CODE_RE.fullmatch(value):
        return PoolItem(value, value, "numeric")
    raise ValueError(f"代码必须是六位数字或 AU999：{value!r}")


def load_pool(path: Path) -> list[PoolItem]:
    """读取一列代码文件，兼容表头、注释、空行和重复项。"""
    if not path.exists():
        raise FileNotFoundError(f"池文件不存在：{path}")
    result: list[PoolItem] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row_number, row in enumerate(reader, start=1):
            if not row:
                continue
            first = row[0].strip()
            if not first or first.startswith("#"):
                continue
            if first.lower() in {"code", "symbol", "代码", "基金代码", "股票代码"}:
                continue
            try:
                item = normalize_pool_code(first)
            except ValueError as exc:
                raise ValueError(f"{path}:{row_number}: {exc}") from exc
            if item.canonical_code not in seen:
                seen.add(item.canonical_code)
                result.append(item)
    return result


def infer_exchange(code: str) -> str:
    if not CODE_RE.fullmatch(code):
        raise ValueError(f"无法推断非六位代码：{code}")
    if code.startswith(("5", "6", "9")):
        return "sh"
    if code.startswith(("0", "1", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")) or code.startswith("92"):
        return "bj"
    raise ValueError(f"无法根据代码推断交易所：{code}")


def numeric_candidate(code: str) -> Instrument:
    exchange = infer_exchange(code)
    return Instrument(
        instrument_key=f"pool:{code}",
        code=code,
        symbol=exchange + code,
        asset_type="market_security",
        exchange=exchange,
        origin="pool",
    )


def gold_instrument() -> Instrument:
    return Instrument(
        instrument_key="gold:Au99.99",
        code="Au99.99",
        symbol="Au99.99",
        asset_type="gold",
        exchange="sge",
        configured_name="Au99.99",
        origin="pool",
    )


def load_builtin_indices(config: dict[str, Any]) -> list[Instrument]:
    result: list[Instrument] = []
    for row in config.get("builtin_indices", []):
        symbol = str(row.get("symbol", "")).strip().lower()
        code = str(row.get("code", "")).strip()
        name = str(row.get("name", "")).strip()
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
            raise ValueError(f"非法内置指数 symbol：{symbol!r}")
        if not CODE_RE.fullmatch(code):
            raise ValueError(f"非法内置指数 code：{code!r}")
        result.append(
            Instrument(
                instrument_key=f"index:{symbol}",
                code=code,
                symbol=symbol,
                asset_type="index",
                exchange=symbol[:2],
                configured_name=name,
                origin="builtin",
            )
        )
    return result


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"none", "null", "nan", "--"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_field(fields: list[str], index: int) -> float | None:
    return parse_float(fields[index]) if index < len(fields) else None


def invalid_quote(inst: Instrument, error: str, source: str) -> Quote:
    return Quote(
        instrument_key=inst.instrument_key,
        symbol=inst.symbol,
        code=inst.code,
        asset_type=inst.asset_type,
        exchange=inst.exchange,
        name=inst.configured_name,
        quote_date="",
        quote_time="",
        open_price=None,
        previous_close=None,
        last_price=None,
        high_price=None,
        low_price=None,
        change_amount=None,
        change_pct=None,
        volume=None,
        amount=None,
        interval_minutes=None,
        interval_volume=None,
        interval_amount=None,
        turnover_rate=None,
        source=source,
        valid=0,
        error=error,
    )


def parse_sina_response(text: str, instruments: list[Instrument], china_date: date) -> list[Quote]:
    by_symbol = {inst.symbol: inst for inst in instruments}
    parsed: dict[str, Quote] = {}
    for symbol, raw in SINA_LINE_RE.findall(text):
        inst = by_symbol.get(symbol)
        if inst is None:
            continue
        if not raw:
            parsed[symbol] = invalid_quote(inst, "接口返回空行情", "sina")
            continue
        fields = raw.split(",")
        name = fields[0].strip() if fields else inst.configured_name
        open_price = parse_field(fields, 1)
        previous_close = parse_field(fields, 2)
        last_price = parse_field(fields, 3)
        high_price = parse_field(fields, 4)
        low_price = parse_field(fields, 5)
        volume = parse_field(fields, 8)
        amount = parse_field(fields, 9)
        quote_date = fields[30].strip() if len(fields) > 30 else ""
        quote_time = fields[31].strip() if len(fields) > 31 else ""
        change_amount = None
        change_pct = None
        if last_price is not None and previous_close not in (None, 0):
            change_amount = last_price - previous_close
            change_pct = change_amount / previous_close * 100.0

        errors: list[str] = []
        if not name:
            errors.append("缺少名称")
        if last_price is None or previous_close is None:
            errors.append("缺少价格")
        if quote_date:
            try:
                if datetime.strptime(quote_date, "%Y-%m-%d").date() != china_date:
                    errors.append(f"行情日期陈旧:{quote_date}")
            except ValueError:
                errors.append(f"行情日期格式异常:{quote_date}")
        else:
            errors.append("缺少行情日期")

        parsed[symbol] = Quote(
            instrument_key=inst.instrument_key,
            symbol=symbol,
            code=inst.code,
            asset_type=inst.asset_type,
            exchange=inst.exchange,
            name=name or inst.configured_name,
            quote_date=quote_date,
            quote_time=quote_time,
            open_price=open_price,
            previous_close=previous_close,
            last_price=last_price,
            high_price=high_price,
            low_price=low_price,
            change_amount=change_amount,
            change_pct=change_pct,
            volume=volume,
            amount=amount,
            interval_minutes=None,
            interval_volume=None,
            interval_amount=None,
            turnover_rate=None,
            source="sina",
            valid=0 if errors else 1,
            error=";".join(errors),
        )
    return [parsed.get(inst.symbol, invalid_quote(inst, "接口未返回该代码", "sina")) for inst in instruments]


def chunks(values: list[Instrument], size: int) -> Iterable[list[Instrument]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def fetch_sina_quotes(
    instruments: list[Instrument],
    china_date: date,
    timeout: int,
    retries: int,
    batch_size: int,
) -> list[Quote]:
    if not instruments:
        return []
    output: list[Quote] = []
    for batch in chunks(instruments, max(1, batch_size)):
        url = "https://hq.sinajs.cn/list=" + ",".join(x.symbol for x in batch)
        text = ""
        last_error = ""
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(url, headers=SINA_HEADERS)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    text = response.read().decode("gbk", errors="ignore")
                last_error = ""
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(min(2**attempt, 4))
        if last_error:
            output.extend(invalid_quote(inst, last_error, "sina") for inst in batch)
        else:
            output.extend(parse_sina_response(text, batch, china_date))
    return output


def parse_sge_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y年%m月%d日 %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in payload:
            number = parse_float(payload.get(key))
            if number is not None:
                return number
    return None


def parse_sge_payload(
    payload: dict[str, Any],
    now_cn: datetime,
    max_delay_minutes: int,
    previous_close: float | None,
) -> Quote:
    inst = gold_instrument()
    times = payload.get("times") or []
    values = payload.get("data") or []
    if not isinstance(times, list) or not isinstance(values, list):
        return invalid_quote(inst, "SGE返回格式缺少times/data", "sge")

    delay_text = str(payload.get("delaystr") or payload.get("updateTime") or "").strip()
    quote_dt_naive = parse_sge_datetime(delay_text)
    quote_dt = quote_dt_naive.replace(tzinfo=now_cn.tzinfo) if quote_dt_naive else None
    target_hm = quote_dt.strftime("%H:%M") if quote_dt else ""

    paired: list[tuple[str, float]] = []
    for t, v in zip(times, values):
        number = parse_float(v)
        if number is not None and number > 0:
            paired.append((str(t), number))
    if not paired:
        return invalid_quote(inst, "SGE分时序列为空", "sge")

    current_price: float | None = None
    current_time = ""
    if target_hm:
        exact = [(t, v) for t, v in paired if t == target_hm]
        if exact:
            current_time, current_price = exact[-1]
    if current_price is None:
        current_time, current_price = paired[-1]

    prices = [v for _, v in paired]
    open_price = first_number(payload, ("open", "openPrice", "openprice")) or prices[0]
    high_price = first_number(payload, ("max", "high", "highPrice")) or max(prices)
    low_price = first_number(payload, ("min", "low", "lowPrice")) or min(prices)
    prev = first_number(payload, ("preclose", "prevClose", "lastclose", "yclose"))
    if prev is None:
        prev = previous_close
    volume = first_number(payload, ("volume", "vol", "totalVolume"))
    amount = first_number(payload, ("amount", "turnover", "totalAmount"))
    change_amount = None
    change_pct = None
    if current_price is not None and prev not in (None, 0):
        change_amount = current_price - prev
        change_pct = change_amount / prev * 100.0

    errors: list[str] = []
    if quote_dt is None:
        errors.append("缺少或无法解析更新时间")
        quote_date = ""
        quote_time = current_time
    else:
        quote_date = quote_dt.strftime("%Y-%m-%d")
        quote_time = quote_dt.strftime("%H:%M:%S")
        delay_minutes = (now_cn - quote_dt).total_seconds() / 60.0
        if delay_minutes < -5:
            errors.append(f"行情时间超前:{delay_minutes:.1f}分钟")
        elif delay_minutes > max_delay_minutes:
            errors.append(f"行情延迟过长:{delay_minutes:.1f}分钟")

    return Quote(
        instrument_key=inst.instrument_key,
        symbol=inst.symbol,
        code=inst.code,
        asset_type=inst.asset_type,
        exchange=inst.exchange,
        name=str(payload.get("heyue") or "Au99.99"),
        quote_date=quote_date,
        quote_time=quote_time,
        open_price=open_price,
        previous_close=prev,
        last_price=current_price,
        high_price=high_price,
        low_price=low_price,
        change_amount=change_amount,
        change_pct=change_pct,
        volume=volume,
        amount=amount,
        interval_minutes=None,
        interval_volume=None,
        interval_amount=None,
        turnover_rate=None,
        source="sge",
        valid=0 if errors else 1,
        error=";".join(errors),
    )


def fetch_sge_gold(
    now_cn: datetime,
    timeout: int,
    retries: int,
    max_delay_minutes: int,
    previous_close: float | None,
) -> Quote:
    url = "https://www.sge.com.cn/graph/quotations"
    body = urllib.parse.urlencode({"instid": "Au99.99"}).encode("utf-8")
    last_error = ""
    raw = b""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=SGE_HEADERS, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            last_error = ""
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(min(2**attempt, 4))
    if last_error:
        return invalid_quote(gold_instrument(), last_error, "sge")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return invalid_quote(gold_instrument(), f"JSONDecodeError:{exc}", "sge")
    if not isinstance(payload, dict):
        return invalid_quote(gold_instrument(), "SGE返回不是JSON对象", "sge")
    return parse_sge_payload(payload, now_cn, max_delay_minutes, previous_close)


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_key TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            exchange TEXT NOT NULL,
            name TEXT,
            origin TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            live_enabled INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_probe_date TEXT,
            last_probe_error TEXT
        );

        CREATE TABLE IF NOT EXISTS quote_snapshots (
            slot_time TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            instrument_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            code TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            exchange TEXT NOT NULL,
            name TEXT,
            quote_date TEXT,
            quote_time TEXT,
            open_price REAL,
            previous_close REAL,
            last_price REAL,
            high_price REAL,
            low_price REAL,
            change_amount REAL,
            change_pct REAL,
            volume REAL,
            amount REAL,
            interval_minutes REAL,
            interval_volume REAL,
            interval_amount REAL,
            turnover_rate REAL,
            source TEXT NOT NULL,
            valid INTEGER NOT NULL,
            error TEXT,
            PRIMARY KEY (slot_time, instrument_key)
        );

        CREATE TABLE IF NOT EXISTS collector_runs (
            slot_time TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            scopes TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_count INTEGER NOT NULL,
            valid_count INTEGER NOT NULL,
            live_pool_count INTEGER NOT NULL,
            reference_count INTEGER NOT NULL,
            index_count INTEGER NOT NULL,
            gold_count INTEGER NOT NULL,
            pool_hash TEXT NOT NULL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_quote_code_time
            ON quote_snapshots(code, slot_time);
        CREATE INDEX IF NOT EXISTS idx_quote_type_time
            ON quote_snapshots(asset_type, slot_time);
        CREATE INDEX IF NOT EXISTS idx_instruments_active
            ON instruments(active, asset_type);
        """
    )
    return conn


def pool_digest(items: list[PoolItem], indices: list[Instrument]) -> str:
    payload = {
        "pool": [x.canonical_code for x in items],
        "indices": [x.symbol for x in indices],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def upsert_configured_pool(
    conn: sqlite3.Connection,
    items: list[PoolItem],
    indices: list[Instrument],
    now_iso: str,
) -> None:
    active_keys: set[str] = set()
    with conn:
        for item in items:
            if item.kind == "gold":
                inst = gold_instrument()
                live_enabled = 1
            else:
                inst = numeric_candidate(item.canonical_code)
                row = conn.execute(
                    "SELECT live_enabled,asset_type,name FROM instruments WHERE instrument_key=?",
                    (inst.instrument_key,),
                ).fetchone()
                live_enabled = int(row["live_enabled"]) if row else 0
                if row and row["asset_type"]:
                    inst = replace(inst, asset_type=str(row["asset_type"]), configured_name=str(row["name"] or ""))
            active_keys.add(inst.instrument_key)
            conn.execute(
                """
                INSERT INTO instruments(
                    instrument_key,code,symbol,asset_type,exchange,name,origin,active,
                    live_enabled,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_key) DO UPDATE SET
                    code=excluded.code,
                    symbol=excluded.symbol,
                    exchange=excluded.exchange,
                    origin=excluded.origin,
                    active=1,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    inst.instrument_key,
                    inst.code,
                    inst.symbol,
                    inst.asset_type,
                    inst.exchange,
                    inst.configured_name,
                    inst.origin,
                    1,
                    live_enabled,
                    now_iso,
                    now_iso,
                ),
            )
        for inst in indices:
            active_keys.add(inst.instrument_key)
            conn.execute(
                """
                INSERT INTO instruments(
                    instrument_key,code,symbol,asset_type,exchange,name,origin,active,
                    live_enabled,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_key) DO UPDATE SET
                    name=excluded.name,active=1,live_enabled=1,last_seen_at=excluded.last_seen_at
                """,
                (
                    inst.instrument_key,
                    inst.code,
                    inst.symbol,
                    inst.asset_type,
                    inst.exchange,
                    inst.configured_name,
                    inst.origin,
                    1,
                    1,
                    now_iso,
                    now_iso,
                ),
            )
        if active_keys:
            placeholders = ",".join("?" for _ in active_keys)
            conn.execute(
                f"UPDATE instruments SET active=0 WHERE instrument_key NOT IN ({placeholders})",
                tuple(sorted(active_keys)),
            )


def load_market_plan(
    conn: sqlite3.Connection,
    items: list[PoolItem],
    indices: list[Instrument],
    probe_date: str,
) -> tuple[list[Instrument], list[Instrument], int]:
    live: list[Instrument] = []
    probes: list[Instrument] = []
    references = 0
    for item in items:
        if item.kind != "numeric":
            continue
        inst = numeric_candidate(item.canonical_code)
        row = conn.execute(
            "SELECT * FROM instruments WHERE instrument_key=?", (inst.instrument_key,)
        ).fetchone()
        if row and int(row["live_enabled"]):
            live.append(
                replace(
                    inst,
                    asset_type=str(row["asset_type"] or "market_security"),
                    configured_name=str(row["name"] or ""),
                )
            )
        elif row and str(row["asset_type"]) == "reference_only" and str(row["last_probe_date"] or "") == probe_date:
            references += 1
        else:
            probes.append(inst)
    live.extend(indices)
    return live, probes, references


def apply_probe_results(
    conn: sqlite3.Connection,
    quotes: list[Quote],
    probe_keys: set[str],
    probe_date: str,
    now_iso: str,
) -> tuple[list[Quote], int]:
    saved: list[Quote] = []
    reference_count = 0
    with conn:
        for q in quotes:
            if q.instrument_key not in probe_keys:
                saved.append(q)
                continue
            if q.valid:
                conn.execute(
                    """
                    UPDATE instruments SET
                        asset_type='market_security',live_enabled=1,name=?,last_probe_date=?,
                        last_probe_error='',last_seen_at=?
                    WHERE instrument_key=?
                    """,
                    (q.name, probe_date, now_iso, q.instrument_key),
                )
                saved.append(replace(q, asset_type="market_security"))
            else:
                reference_count += 1
                conn.execute(
                    """
                    UPDATE instruments SET
                        asset_type='reference_only',live_enabled=0,last_probe_date=?,
                        last_probe_error=?,last_seen_at=?
                    WHERE instrument_key=?
                    """,
                    (probe_date, q.error, now_iso, q.instrument_key),
                )
    return saved, reference_count


def previous_gold_close(conn: sqlite3.Connection, current_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT last_price FROM quote_snapshots
        WHERE instrument_key='gold:Au99.99' AND valid=1 AND last_price IS NOT NULL
          AND quote_date<>?
        ORDER BY slot_time DESC LIMIT 1
        """,
        (current_date,),
    ).fetchone()
    return parse_float(row["last_price"]) if row else None


def enrich_intervals(conn: sqlite3.Connection, quotes: list[Quote], slot_time: str) -> list[Quote]:
    output: list[Quote] = []
    current_dt = datetime.fromisoformat(slot_time)
    for q in quotes:
        if not q.valid:
            output.append(q)
            continue
        row = conn.execute(
            """
            SELECT slot_time,quote_date,volume,amount FROM quote_snapshots
            WHERE instrument_key=? AND valid=1 AND slot_time<?
            ORDER BY slot_time DESC LIMIT 1
            """,
            (q.instrument_key, slot_time),
        ).fetchone()
        if not row or str(row["quote_date"] or "") != q.quote_date:
            output.append(q)
            continue
        prev_dt = datetime.fromisoformat(str(row["slot_time"]))
        interval_minutes = (current_dt - prev_dt).total_seconds() / 60.0
        prev_volume = parse_float(row["volume"])
        prev_amount = parse_float(row["amount"])
        interval_volume = None
        interval_amount = None
        if q.volume is not None and prev_volume is not None and q.volume >= prev_volume:
            interval_volume = q.volume - prev_volume
        if q.amount is not None and prev_amount is not None and q.amount >= prev_amount:
            interval_amount = q.amount - prev_amount
        output.append(
            replace(
                q,
                interval_minutes=interval_minutes,
                interval_volume=interval_volume,
                interval_amount=interval_amount,
            )
        )
    return output


def save_capture(
    conn: sqlite3.Connection,
    slot_time: str,
    started_at: str,
    collected_at: str,
    scopes: set[str],
    quotes: list[Quote],
    live_pool_count: int,
    reference_count: int,
    index_count: int,
    gold_count: int,
    digest: str,
    run_error: str = "",
) -> None:
    valid_count = sum(q.valid for q in quotes)
    status = "success" if quotes and valid_count == len(quotes) else "partial"
    if not quotes or valid_count == 0:
        status = "failed"
    if run_error:
        status = "failed"
    with conn:
        for q in quotes:
            conn.execute(
                """
                INSERT INTO quote_snapshots(
                    slot_time,collected_at,instrument_key,symbol,code,asset_type,exchange,name,
                    quote_date,quote_time,open_price,previous_close,last_price,high_price,low_price,
                    change_amount,change_pct,volume,amount,interval_minutes,interval_volume,
                    interval_amount,turnover_rate,source,valid,error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_time,instrument_key) DO UPDATE SET
                    collected_at=excluded.collected_at,name=excluded.name,quote_date=excluded.quote_date,
                    quote_time=excluded.quote_time,open_price=excluded.open_price,
                    previous_close=excluded.previous_close,last_price=excluded.last_price,
                    high_price=excluded.high_price,low_price=excluded.low_price,
                    change_amount=excluded.change_amount,change_pct=excluded.change_pct,
                    volume=excluded.volume,amount=excluded.amount,
                    interval_minutes=excluded.interval_minutes,interval_volume=excluded.interval_volume,
                    interval_amount=excluded.interval_amount,turnover_rate=excluded.turnover_rate,
                    source=excluded.source,valid=excluded.valid,error=excluded.error
                """,
                (
                    slot_time,
                    collected_at,
                    q.instrument_key,
                    q.symbol,
                    q.code,
                    q.asset_type,
                    q.exchange,
                    q.name,
                    q.quote_date,
                    q.quote_time,
                    q.open_price,
                    q.previous_close,
                    q.last_price,
                    q.high_price,
                    q.low_price,
                    q.change_amount,
                    q.change_pct,
                    q.volume,
                    q.amount,
                    q.interval_minutes,
                    q.interval_volume,
                    q.interval_amount,
                    q.turnover_rate,
                    q.source,
                    q.valid,
                    q.error,
                ),
            )
            conn.execute(
                "UPDATE instruments SET name=CASE WHEN ?<>'' THEN ? ELSE name END,last_seen_at=? WHERE instrument_key=?",
                (q.name, q.name, collected_at, q.instrument_key),
            )
        conn.execute(
            """
            INSERT INTO collector_runs(
                slot_time,started_at,finished_at,scopes,status,requested_count,valid_count,
                live_pool_count,reference_count,index_count,gold_count,pool_hash,error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slot_time) DO UPDATE SET
                started_at=excluded.started_at,finished_at=excluded.finished_at,
                scopes=excluded.scopes,status=excluded.status,requested_count=excluded.requested_count,
                valid_count=excluded.valid_count,live_pool_count=excluded.live_pool_count,
                reference_count=excluded.reference_count,index_count=excluded.index_count,
                gold_count=excluded.gold_count,pool_hash=excluded.pool_hash,error=excluded.error
            """,
            (
                slot_time,
                started_at,
                collected_at,
                ",".join(sorted(scopes)),
                status,
                len(quotes),
                valid_count,
                live_pool_count,
                reference_count,
                index_count,
                gold_count,
                digest,
                run_error,
            ),
        )


def capture(
    config: dict[str, Any],
    slot_dt: datetime,
    scopes: set[str],
    force: bool = False,
) -> tuple[int, int, str]:
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    if slot_dt.tzinfo is None:
        slot_dt = slot_dt.replace(tzinfo=timezone)
    slot_dt = slot_dt.astimezone(timezone)
    slot_time = slot_dt.isoformat(timespec="minutes")
    started_at = datetime.now(timezone).isoformat(timespec="seconds")
    now_cn = datetime.now(timezone)

    pool_path = resolve_path(str(config.get("pool_path", "config/instruments.csv")))
    items = load_pool(pool_path)
    indices = load_builtin_indices(config)
    has_gold = any(x.kind == "gold" for x in items)
    if not items and "equity" in scopes:
        raise RuntimeError("instruments.csv 为空")

    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    conn = connect_db(db_path)
    try:
        existing = conn.execute(
            "SELECT status FROM collector_runs WHERE slot_time=?", (slot_time,)
        ).fetchone()
        if existing and not force and str(existing["status"]) != "failed":
            return 0, 0, f"slot {slot_time} 已存在，跳过"

        now_iso = now_cn.isoformat(timespec="seconds")
        upsert_configured_pool(conn, items, indices, now_iso)
        quotes: list[Quote] = []
        reference_count = conn.execute(
            "SELECT COUNT(*) AS n FROM instruments WHERE active=1 AND asset_type='reference_only'"
        ).fetchone()["n"]
        live_pool_count = 0

        if "equity" in scopes:
            live, probes, references_today = load_market_plan(conn, items, indices, slot_dt.date().isoformat())
            fetched = fetch_sina_quotes(
                live + probes,
                china_date=slot_dt.date(),
                timeout=int(config.get("request_timeout_seconds", 8)),
                retries=int(config.get("request_retries", 2)),
                batch_size=int(config.get("batch_size", 60)),
            )
            probe_keys = {x.instrument_key for x in probes}
            fetched, new_refs = apply_probe_results(
                conn,
                fetched,
                probe_keys,
                slot_dt.date().isoformat(),
                now_iso,
            )
            reference_count = references_today + new_refs
            quotes.extend(fetched)
            live_pool_count = sum(q.asset_type == "market_security" for q in fetched)

        if "gold" in scopes and has_gold:
            prev_close = previous_gold_close(conn, slot_dt.date().isoformat())
            quotes.append(
                fetch_sge_gold(
                    now_cn=now_cn,
                    timeout=int(config.get("request_timeout_seconds", 8)),
                    retries=int(config.get("request_retries", 2)),
                    max_delay_minutes=int(config.get("gold_max_delay_minutes", 45)),
                    previous_close=prev_close,
                )
            )

        quotes = enrich_intervals(conn, quotes, slot_time)
        collected_at = datetime.now(timezone).isoformat(timespec="seconds")
        save_capture(
            conn,
            slot_time,
            started_at,
            collected_at,
            scopes,
            quotes,
            live_pool_count=live_pool_count,
            reference_count=int(reference_count),
            index_count=sum(q.asset_type == "index" for q in quotes),
            gold_count=sum(q.asset_type == "gold" for q in quotes),
            digest=pool_digest(items, indices),
        )
        return len(quotes), sum(q.valid for q in quotes), slot_time
    finally:
        conn.close()


def parse_hhmm(value: str) -> int:
    hour, minute = value.split(":", 1)
    h = int(hour)
    m = int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"非法时间：{value}")
    return h * 60 + m


def window_minutes(window: dict[str, Any]) -> list[int]:
    start = parse_hhmm(str(window["start"]))
    end = parse_hhmm(str(window["end"]))
    every = int(window["every_minutes"])
    if every <= 0 or end < start:
        raise ValueError(f"非法 schedule 配置：{window}")
    return list(range(start, end + 1, every))


def build_schedule_map(day: date, config: dict[str, Any], tz: ZoneInfo) -> dict[datetime, set[str]]:
    output: dict[datetime, set[str]] = {}
    schedules = config.get("schedules", {})

    if day.weekday() < 5:
        for window in schedules.get("equity", []):
            for minute in window_minutes(window):
                dt = datetime.combine(day, dt_time(minute // 60, minute % 60), tzinfo=tz)
                output.setdefault(dt, set()).add("equity")

    # 黄金早盘/日盘/夜盘：00:00-02:30 只在周二至周六；其余只在周一至周五。
    for window in schedules.get("gold", []):
        start_minute = parse_hhmm(str(window["start"]))
        allow = day.weekday() in ({1, 2, 3, 4, 5} if start_minute < 180 else {0, 1, 2, 3, 4})
        if not allow:
            continue
        for minute in window_minutes(window):
            dt = datetime.combine(day, dt_time(minute // 60, minute % 60), tzinfo=tz)
            output.setdefault(dt, set()).add("gold")
    return output


def slot_status(db_path: Path, slot_time: str) -> str | None:
    if not db_path.exists():
        return None
    conn = connect_db(db_path)
    try:
        row = conn.execute("SELECT status FROM collector_runs WHERE slot_time=?", (slot_time,)).fetchone()
        return str(row["status"]) if row else None
    finally:
        conn.close()


def find_due_slot(now: datetime, config: dict[str, Any], has_gold: bool) -> tuple[datetime, set[str]] | None:
    grace = int(config.get("slot_grace_minutes", 5))
    delay = int(config.get("capture_delay_seconds", 20))
    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    for slot, scopes in build_schedule_map(now.date(), config, now.tzinfo).items():
        scopes = set(scopes)
        if not has_gold:
            scopes.discard("gold")
        if not scopes:
            continue
        due = slot + timedelta(seconds=delay)
        deadline = slot + timedelta(minutes=grace)
        if due <= now <= deadline:
            status = slot_status(db_path, slot.isoformat(timespec="minutes"))
            if status is None or status == "failed":
                return slot, scopes
    return None


def state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def state_set(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (key, value, now),
    )


def backup_database(config: dict[str, Any], backup_date: date | None = None) -> Path:
    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    backup_dir = resolve_path(str(config.get("backup_dir", "data/backups")))
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_date = backup_date or date.today()
    destination = backup_dir / f"intraday_{backup_date:%Y%m%d}.db"
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    source_conn = sqlite3.connect(db_path, timeout=30)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    shutil.copy2(destination, backup_dir / "intraday_latest.db")
    keep = int(config.get("backup_keep_daily", 7))
    for old in sorted(backup_dir.glob("intraday_20*.db"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return destination


def maybe_daily_backup(config: dict[str, Any], now: datetime) -> None:
    backup_at = parse_hhmm(str(config.get("backup_at", "15:40")))
    if now.hour * 60 + now.minute < backup_at:
        return
    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    if not db_path.exists():
        return
    conn = connect_db(db_path)
    today = now.date().isoformat()
    try:
        if state_get(conn, "last_backup_date") == today:
            return
    finally:
        conn.close()
    destination = backup_database(config, now.date())
    conn = connect_db(db_path)
    try:
        with conn:
            state_set(conn, "last_backup_date", today, now.isoformat(timespec="seconds"))
    finally:
        conn.close()
    logging.info("每日SQLite备份完成：%s", destination)


def run_service(config_path: Path) -> int:
    global STOP_REQUESTED

    def request_stop(signum: int, _frame: Any) -> None:
        global STOP_REQUESTED
        logging.info("收到停止信号 %s", signum)
        STOP_REQUESTED = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    config = load_json(config_path)
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    logging.info("采集服务启动，时区=%s", timezone.key)
    logging.info("唯一维护文件：%s", resolve_path(str(config.get("pool_path", "config/instruments.csv"))))

    while not STOP_REQUESTED:
        try:
            try:
                config = load_json(config_path)
                timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
            except Exception:
                logging.exception("重新加载config.json失败，沿用上一份")
            now = datetime.now(timezone)
            items = load_pool(resolve_path(str(config.get("pool_path", "config/instruments.csv"))))
            has_gold = any(x.kind == "gold" for x in items)
            due = find_due_slot(now, config, has_gold)
            if due is not None:
                slot, scopes = due
                requested, valid, key = capture(config, slot, scopes)
                logging.info("节点 %s scopes=%s 有效 %d/%d", key, ",".join(sorted(scopes)), valid, requested)
            maybe_daily_backup(config, now)
        except Exception:
            logging.exception("服务循环发生错误，将继续运行")
        sleep_seconds = max(5, int(config.get("loop_sleep_seconds", 15)))
        for _ in range(sleep_seconds):
            if STOP_REQUESTED:
                break
            time.sleep(1)
    logging.info("采集服务已停止")
    return 0


def validate_config(config_path: Path) -> int:
    config = load_json(config_path)
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    items = load_pool(resolve_path(str(config.get("pool_path", "config/instruments.csv"))))
    numeric = [x for x in items if x.kind == "numeric"]
    gold = [x for x in items if x.kind == "gold"]
    indices = load_builtin_indices(config)
    schedule = build_schedule_map(datetime.now(timezone).date(), config, timezone)
    equity_slots = [x for x, scopes in schedule.items() if "equity" in scopes]
    gold_slots = [x for x, scopes in schedule.items() if "gold" in scopes]
    print("配置检查通过")
    print(f"池内六位代码：{len(numeric)}")
    print(f"Au99.99：{'已启用' if gold else '未启用'}")
    print(f"内置指数：{len(indices)}")
    print(f"当日A股节点：{len(equity_slots)}")
    print(f"当日黄金节点：{len(gold_slots) if gold else 0}")
    print("数据库：" + str(resolve_path(str(config.get("database_path", "data/intraday.db")))))
    print("说明：六位代码在首次联网采集时自动识别为实时证券或参考基金。")
    return 0


def print_status(config_path: Path) -> int:
    config = load_json(config_path)
    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    if not db_path.exists():
        print(f"数据库尚未创建：{db_path}")
        return 0
    conn = connect_db(db_path)
    try:
        last = conn.execute("SELECT * FROM collector_runs ORDER BY slot_time DESC LIMIT 1").fetchone()
        quote_count = conn.execute("SELECT COUNT(*) AS n FROM quote_snapshots").fetchone()["n"]
        run_count = conn.execute("SELECT COUNT(*) AS n FROM collector_runs").fetchone()["n"]
        live_count = conn.execute(
            "SELECT COUNT(*) AS n FROM instruments WHERE active=1 AND live_enabled=1 AND origin='pool'"
        ).fetchone()["n"]
        ref_count = conn.execute(
            "SELECT COUNT(*) AS n FROM instruments WHERE active=1 AND asset_type='reference_only'"
        ).fetchone()["n"]
    finally:
        conn.close()
    print(f"数据库：{db_path}")
    print(f"大小：{db_path.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"采集节点：{run_count}")
    print(f"行情行数：{quote_count}")
    print(f"实时池项目：{live_count}")
    print(f"参考基金项目：{ref_count}")
    if last:
        print(
            "最后节点：{slot_time} scopes={scopes} status={status} valid={valid_count}/{requested_count}".format(
                **dict(last)
            )
        )
        if last["error"]:
            print("错误：" + str(last["error"]))
    return 0


def export_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...], path: Path) -> int:
    rows = conn.execute(query, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        if not rows:
            return 0
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)


def export_date(config_path: Path, day: str) -> int:
    datetime.strptime(day, "%Y-%m-%d")
    config = load_json(config_path)
    db_path = resolve_path(str(config.get("database_path", "data/intraday.db")))
    export_dir = resolve_path(str(config.get("export_dir", "data/exports"))) / day
    conn = connect_db(db_path)
    try:
        quote_count = export_rows(
            conn,
            "SELECT * FROM quote_snapshots WHERE substr(slot_time,1,10)=? ORDER BY slot_time,asset_type,code",
            (day,),
            export_dir / "quotes.csv",
        )
        run_count = export_rows(
            conn,
            "SELECT * FROM collector_runs WHERE substr(slot_time,1,10)=? ORDER BY slot_time",
            (day,),
            export_dir / "runs.csv",
        )
        instrument_count = export_rows(
            conn,
            "SELECT * FROM instruments ORDER BY origin,asset_type,code",
            (),
            export_dir / "instruments.csv",
        )
    finally:
        conn.close()
    print(f"导出目录：{export_dir}")
    print(f"行情 {quote_count} 行，节点 {run_count} 行，元数据 {instrument_count} 行")
    return 0


def self_test() -> int:
    sample = (
        'var hq_str_sh600519="贵州茅台,1500.00,1490.00,1510.00,1520.00,1480.00,'
        '0,0,123456,987654321,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
        '2026-09-15,14:30:00,00";\n'
        'var hq_str_sh000001="上证指数,3900.00,3880.00,3920.00,3930.00,3870.00,'
        '0,0,222222,333333333,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
        '2026-09-15,14:30:00,00";'
    )
    instruments = [
        Instrument("pool:600519", "600519", "sh600519", "market_security", "sh"),
        Instrument("index:sh000001", "000001", "sh000001", "index", "sh", "上证指数", "builtin"),
    ]
    quotes = parse_sina_response(sample, instruments, date(2026, 9, 15))
    assert len(quotes) == 2
    assert quotes[0].valid == 1
    assert round(quotes[0].change_pct or 0, 4) == round(20 / 1490 * 100, 4)

    gold_payload = {
        "heyue": "Au99.99",
        "times": ["09:00", "09:01", "09:02"],
        "data": [800.0, 801.0, 802.0],
        "min": 799.0,
        "max": 803.0,
        "delaystr": "2026年09月15日 09:02:30",
    }
    now_cn = datetime(2026, 9, 15, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    gold = parse_sge_payload(gold_payload, now_cn, 45, 790.0)
    assert gold.valid == 1 and gold.last_price == 802.0
    assert round(gold.change_pct or 0, 4) == round(12 / 790 * 100, 4)

    assert normalize_pool_code("au999").canonical_code == "Au99.99"
    assert normalize_pool_code("600519").canonical_code == "600519"
    print("self-test passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ubuntu轻量股票/基金池与Au99.99盘中采集器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="config.json路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="常驻运行，由systemd调用")
    once = sub.add_parser("once", help="立即采集一次，用于联网测试")
    once.add_argument("--force", action="store_true", help="覆盖相同时间槽")
    once.add_argument("--slot", help="指定北京时间槽位，如2026-09-15T14:30")
    once.add_argument(
        "--scope",
        choices=["equity", "gold", "all"],
        default="all",
        help="测试采集范围",
    )
    sub.add_parser("validate", help="检查配置和池文件")
    sub.add_parser("status", help="显示数据库和最后采集状态")
    sub.add_parser("backup", help="立即生成SQLite安全备份")
    export = sub.add_parser("export", help="按日期导出CSV")
    export.add_argument("date", help="YYYY-MM-DD")
    sub.add_parser("self-test", help="不联网运行解析器自检")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_json(config_path)
    setup_logging(str(config.get("log_level", "INFO")))
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    if args.command == "run":
        return run_service(config_path)
    if args.command == "validate":
        return validate_config(config_path)
    if args.command == "status":
        return print_status(config_path)
    if args.command == "backup":
        print(backup_database(config, datetime.now(timezone).date()))
        return 0
    if args.command == "export":
        return export_date(config_path, args.date)
    if args.command == "self-test":
        return self_test()
    if args.command == "once":
        if args.slot:
            slot = datetime.fromisoformat(args.slot)
            if slot.tzinfo is None:
                slot = slot.replace(tzinfo=timezone)
        else:
            slot = datetime.now(timezone).replace(second=0, microsecond=0)
        scopes = {"equity", "gold"} if args.scope == "all" else {args.scope}
        requested, valid, key = capture(config, slot, scopes, force=args.force)
        print(f"{key}: valid={valid}/{requested}")
        return 0 if requested == 0 or valid > 0 else 2
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("[ERROR] %s", exc)
        raise SystemExit(1)
