"""
ETF Share Incremental Updater
=============================
追踪核心宽基ETF份额动向。

数据来源：
  上交所 ETF 份额：akshare fund_etf_scale_sse（仅上交所）
  ETF 收盘价：akshare fund_etf_hist_em

运行逻辑（增量）：
  1. 从 config.yml 加载配置（开始日期、时间间隔、ETF列表）
  2. 根据开始日期与间隔生成全部目标日期序列
  3. 读取已有 CSV，去除已存在的日期
  4. 对剩余日期逐一拉取份额与价格数据，追加写入 CSV

用法：
  python update_data.py
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, date, timedelta

import akshare as ak
import pandas as pd
import yaml

# ---------------------------------------------------------------------------
# 加载配置
# ---------------------------------------------------------------------------

with open("config.yml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

START_DATE: str = _cfg["start_date"]          # e.g. "20260101"
INTERVAL_DAYS: int = int(_cfg["interval_days"])  # e.g. 7
DATA_FILE: str = _cfg["data_file"]            # e.g. "data/etf_scale.csv"
ETF_TARGETS: dict[str, str] = {              # {code: name}
    str(k).zfill(6): str(v) for k, v in _cfg["etf_targets"].items()
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _to_float(val) -> float | None:
    """将任意类型安全转换为 float，失败返回 None。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def get_all_target_dates() -> list[str]:
    """
    根据配置中的开始日期与间隔，生成从 START_DATE 到今天的全部目标日期列表。
    返回 YYYYMMDD 字符串列表。
    """
    start = datetime.strptime(START_DATE, "%Y%m%d").date()
    today = date.today()
    dates: list[str] = []
    d = start
    while d <= today:
        dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=INTERVAL_DAYS)
    return dates


def fetch_etf_price_cache(dates: list[str]) -> dict[str, pd.Series]:
    """
    按日期列表拉取目标 ETF 的单位净值（同花顺 fund_etf_category_ths 接口）。
    对大型指数 ETF，单位净值与场内收盘价偏差极小（套利机制保证），可直接替代。
    返回：{ETF代码: pd.Series(单位净值, index=日期字符串 YYYYMMDD)}
    """
    # {code: {date_str: price}}
    price_map: dict[str, dict[str, float]] = {code: {} for code in ETF_TARGETS}

    total = len(dates)
    for idx, d_str in enumerate(dates, 1):
        print(f"  [{idx}/{total}] 获取 {d_str} ETF净值...")
        for attempt in range(1, 4):
            try:
                df = ak.fund_etf_category_ths(symbol="ETF", date=d_str)
                df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
                for code in ETF_TARGETS:
                    row = df[df["基金代码"] == code]
                    if not row.empty:
                        val = _to_float(row["当前-单位净值"].iloc[0])
                        if val is not None:
                            price_map[code][d_str] = val
                break
            except Exception as exc:
                print(
                    f"    ⚠ 获取ETF净值失败 ({d_str}) "
                    f"[第 {attempt}/3 次]: {type(exc).__name__}: {exc}"
                )
                if attempt < 3:
                    time.sleep(0.8)
        time.sleep(0.3)

    return {code: pd.Series(price_map[code]) for code in ETF_TARGETS}


def fetch_sse_scale_web(date_str: str) -> dict[str, float | None]:
    """
    直接调用上交所查询接口（query.sse.com.cn）获取指定日期的ETF份额数据。
    当 akshare 接口连续失败时作为备用数据源。
    date_str: YYYYMMDD 格式
    返回：{ETF代码: 份额(亿份)}。若当日无数据返回空字典。
    """
    date_formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    sse_cookie = os.getenv("SSE_COOKIE", "").strip()

    def _build_page_url(page_no: int, page_size: int, ts: int, cb: str) -> str:
        return (
            "https://query.sse.com.cn/commonQuery.do"
            f"?jsonCallBack={cb}"
            "&isPagination=true"
            f"&pageHelp.pageSize={page_size}"
            f"&pageHelp.pageNo={page_no}"
            f"&pageHelp.beginPage={page_no}"
            "&pageHelp.cacheSize=1"
            f"&pageHelp.endPage={page_no}"
            "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
            f"&STAT_DATE={date_formatted}"
            f"&_={ts}"
        )

    def _parse_jsonp(text: str) -> dict:
        # 兼容 callbackName({...}) 与前后空白字符
        m = re.search(r"^[^(]+\((.*)\)\s*$", text, flags=re.S)
        if not m:
            raise ValueError("响应不是有效 JSONP")
        return json.loads(m.group(1))

    def _fetch_page_with_curl(page_no: int, page_size: int) -> dict:
        ts = int(time.time() * 1000)
        cb = f"jsonpCallback{ts}"
        url = _build_page_url(page_no, page_size, ts, cb)

        cmd = [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--compressed",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--retry",
            "2",
            "--retry-delay",
            "1",
            "--retry-all-errors",
            url,
            "-H",
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0",
            "-H",
            "Accept: */*",
            "-H",
            "Accept-Language: zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
            "-H",
            "Accept-Encoding: gzip, deflate",
            "-H",
            "Referer: https://www.sse.com.cn/",
            "-H",
            "Connection: keep-alive",
            "-H",
            "Sec-Fetch-Dest: script",
            "-H",
            "Sec-Fetch-Mode: no-cors",
            "-H",
            "Sec-Fetch-Site: same-site",
            "-H",
            "Pragma: no-cache",
            "-H",
            "Cache-Control: no-cache",
        ]
        if sse_cookie:
            cmd += ["-H", f"Cookie: {sse_cookie}"]

        completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _parse_jsonp(completed.stdout)

    result: dict[str, float | None] = {}
    page = 1
    total_pages = 1
    page_size = 100

    while page <= total_pages:
        data = _fetch_page_with_curl(page, page_size)

        page_help = data.get("pageHelp", {})
        total_pages = int(page_help.get("pageCount", 1))
        rows = page_help.get("data", [])

        if not rows:
            break

        for row in rows:
            code = str(row.get("SEC_CODE", "")).zfill(6)
            if code in ETF_TARGETS:
                val = _to_float(row.get("TOT_VOL"))
                # TOT_VOL unit is 万份; divide by 10000 to get 亿份
                result[code] = val / 1e4 if val is not None else None

        page += 1
        time.sleep(0.2)

    # Fill None for target codes not found in the response
    for code in ETF_TARGETS:
        if code not in result:
            result[code] = None

    return result


# Track consecutive akshare failures so we can switch to the web fallback
# after _AKSHARE_FAIL_THRESHOLD consecutive errors.
_consecutive_akshare_failures: int = 0
_AKSHARE_FAIL_THRESHOLD: int = 1
_AKSHARE_RETRY_PER_DATE: int = 3


def fetch_sse_scale_for_date(date_str: str) -> dict[str, float | None]:
    """
    调用 akshare fund_etf_scale_sse 获取指定日期的上交所 ETF 份额数据。
    若 akshare 连续失败达到阈值，切换为直接调用上交所网页接口。
    返回：{ETF代码: 份额(亿份)}。若该日无数据（如非交易日）返回空字典。
    """
    global _consecutive_akshare_failures

    if _consecutive_akshare_failures < _AKSHARE_FAIL_THRESHOLD:
        for attempt in range(1, _AKSHARE_RETRY_PER_DATE + 1):
            try:
                df = ak.fund_etf_scale_sse(date=date_str)
                df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
                result: dict[str, float | None] = {}
                for code in ETF_TARGETS:
                    row = df[df["基金代码"] == code]
                    if not row.empty:
                        # akshare fund_etf_scale_sse 返回的"基金份额"单位为份（原始万份 × 10000）
                        # 除以 1e8 转换为亿份
                        val = _to_float(row["基金份额"].iloc[0])
                        result[code] = val / 1e8 if val is not None else None
                    else:
                        result[code] = None
                _consecutive_akshare_failures = 0
                return result
            except Exception as exc:
                print(
                    f"    ⚠ akshare获取上交所份额数据失败 ({date_str}) "
                    f"[第 {attempt}/{_AKSHARE_RETRY_PER_DATE} 次]: "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt < _AKSHARE_RETRY_PER_DATE:
                    time.sleep(0.8)

        _consecutive_akshare_failures += 1
        print(
            f"    ↳ akshare当日重试后仍失败，连续失败计数={_consecutive_akshare_failures}，"
            f"切换为上交所网页接口"
        )
    else:
        print(f"    ℹ 使用上交所网页接口获取 {date_str} 份额数据")

    try:
        return fetch_sse_scale_web(date_str)
    except Exception as exc:
        print(f"    ⚠ 上交所网页接口也失败 ({date_str}): {exc}")
        return {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def update_data() -> None:
    """根据配置的起始日期和间隔，增量更新 ETF 份额与价格数据。"""

    # ------------------------------------------------------------------
    # 1. 生成全部目标日期，去除已有日期
    # ------------------------------------------------------------------
    all_dates = get_all_target_dates()

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    if os.path.exists(DATA_FILE):
        df_old = pd.read_csv(DATA_FILE, dtype={"基金代码": str, "日期": str})
        df_old["日期"] = df_old["日期"].astype(str)

        # 若历史数据中某一天的份额或价格全为空，则整天删除并重新抓取。
        missing_scale_dates = set(
            df_old.groupby("日期")["基金份额(亿份)"].apply(lambda s: s.isna().all()).loc[lambda s: s].index
        )
        missing_price_dates = set(
            df_old.groupby("日期")["ETF收盘价"].apply(lambda s: s.isna().all()).loc[lambda s: s].index
        )
        bad_dates = missing_scale_dates | missing_price_dates
        if bad_dates:
            print(f"发现 {len(bad_dates)} 个数据缺失日期，将重新获取：{sorted(bad_dates)}")
            df_old = df_old[~df_old["日期"].isin(bad_dates)].copy()

        existing_dates = set(df_old["日期"].unique())
    else:
        df_old = pd.DataFrame()
        existing_dates = set()

    dates_to_fetch = [d for d in all_dates if d not in existing_dates]

    if not dates_to_fetch:
        print("数据已是最新，无需更新。")
        return

    print(
        f"需要获取 {len(dates_to_fetch)} 个日期"
        f"（{dates_to_fetch[0]} 至 {dates_to_fetch[-1]}）"
    )

    # ------------------------------------------------------------------
    # 2. 按日期列表批量获取 ETF 单位净值（同花顺，用作收盘价代理）
    # ------------------------------------------------------------------
    print("\n正在批量获取ETF净值数据（同花顺）...")
    etf_price_cache = fetch_etf_price_cache(dates_to_fetch)

    # ------------------------------------------------------------------
    # 3. 逐日获取上交所 ETF 份额数据
    # ------------------------------------------------------------------
    new_data_list: list[dict] = []
    total = len(dates_to_fetch)
    for i, d_str in enumerate(dates_to_fetch, 1):
        print(f"\n[{i}/{total}] 获取 {d_str} 份额数据...")
        scale_data = fetch_sse_scale_for_date(d_str)

        # 若当日所有 ETF 份额均无数据（非交易日或接口异常），跳过该日期
        if not scale_data or all(v is None for v in scale_data.values()):
            print(f"    ↳ 无有效份额数据，跳过 {d_str}")
            continue

        for code, name in ETF_TARGETS.items():
            etf_series = etf_price_cache.get(code)
            # 优先使用目标日价格；若目标日非交易日则向前填充最近交易日价格
            # 注意：若目标日超出可用价格数据范围，此处不填充，保留 None
            etf_price: float | None = None
            if etf_series is not None:
                if d_str in etf_series.index:
                    etf_price = float(etf_series[d_str])
                else:
                    # 仅在目标日 ≤ 已有价格数据最新日时前向填充，避免引入未来数据
                    available = etf_series.index[etf_series.index <= d_str]
                    if len(available) > 0 and d_str <= etf_series.index.max():
                        etf_price = float(etf_series[available[-1]])

            new_data_list.append(
                {
                    "日期": d_str,
                    "基金代码": code,
                    "基金名称": name,
                    "基金份额(亿份)": scale_data.get(code),
                    "ETF收盘价": etf_price,
                }
            )
        time.sleep(0.3)

    if not new_data_list:
        print("\n未获取到有效的新数据（数据源可能尚未更新）。")
        return

    # ------------------------------------------------------------------
    # 4. 合并新旧数据、去重、排序并写回 CSV
    # ------------------------------------------------------------------
    df_new = pd.DataFrame(new_data_list)
    df_final = pd.concat([df_old, df_new], ignore_index=True)

    df_final.drop_duplicates(subset=["日期", "基金代码"], keep="last", inplace=True)
    df_final.sort_values(by=["日期", "基金代码"], inplace=True)
    df_final.reset_index(drop=True, inplace=True)

    df_final.to_csv(DATA_FILE, index=False, encoding="utf-8-sig")
    print(f"\n✓ 成功追加 {len(df_new)} 条新记录，数据已保存至 {DATA_FILE}")


if __name__ == "__main__":
    update_data()
