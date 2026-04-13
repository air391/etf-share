"""
ETF Share Incremental Updater
=============================
追踪"国家队"（中央汇金、证金公司）核心宽基ETF份额动向。

监控标的（6只）：
  159903  中证100ETF南方   → 对应指数 sz399903 中证100
  510500  中证500ETF南方   → 对应指数 sh000500 中证500
  510050  上证50ETF华夏    → 对应指数 sh000016 上证50
  510330  沪深300ETF华夏   → 对应指数 sh000300 沪深300
  510310  沪深300ETF易方达  → 对应指数 sh000300 沪深300
  510300  沪深300ETF华泰柏瑞 → 对应指数 sh000300 沪深300

运行逻辑（增量）：
  1. 读取已有 national_team_data.csv，找到最后日期
  2. 从最后日期的下一个交易日开始拉取新数据
  3. 合并去重后写回 CSV，供可视化分析使用

用法：
  python update_data.py
"""

import os
import time
from datetime import datetime, date

import akshare as ak
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DATA_FILE = "national_team_data.csv"

# 首次运行（CSV 不存在）时从该日期开始获取历史数据
DEFAULT_START_DATE = "20230101"

# 6 只核心 ETF：代码 → {名称, 对应指数代码}
ETF_TARGETS: dict[str, dict] = {
    "159903": {"name": "中证100ETF南方",   "index": "sz399903"},
    "510500": {"name": "中证500ETF南方",   "index": "sh000500"},
    "510050": {"name": "上证50ETF华夏",    "index": "sh000016"},
    "510330": {"name": "沪深300ETF华夏",   "index": "sh000300"},
    "510310": {"name": "沪深300ETF易方达", "index": "sh000300"},
    "510300": {"name": "沪深300ETF华泰柏瑞","index": "sh000300"},
}

# 沪市（上交所）ETF 代码集合：以 "5" 开头的六位代码（如 510xxx）
_SSE_CODES = {c for c in ETF_TARGETS if c.startswith("5")}
# 深市（深交所）ETF 代码集合：以 "1" 开头的六位代码（如 159xxx）
_SZSE_CODES = {c for c in ETF_TARGETS if c.startswith("1")}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _to_float(val) -> float | None:
    """将任意类型安全转换为 float，失败返回 None。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_index_cache(start_date_str: str, end_date_str: str) -> dict[str, pd.Series]:
    """
    批量拉取所有对应指数的历史收盘价，以字典形式缓存。
    返回：{指数代码: pd.Series(收盘价, index=日期字符串)}
    """
    cache: dict[str, pd.Series] = {}
    unique_indices = {v["index"] for v in ETF_TARGETS.values()}

    for idx_code in unique_indices:
        print(f"  获取指数行情 {idx_code} ...")
        try:
            df = ak.stock_zh_index_daily_em(symbol=idx_code)
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
            df = df[(df["date"] >= start_date_str) & (df["date"] <= end_date_str)]
            cache[idx_code] = df.set_index("date")["close"]
        except Exception as exc:
            print(f"    ⚠ 获取指数 {idx_code} 失败: {exc}")
        time.sleep(0.5)

    return cache


def fetch_etf_price_cache(start_date_str: str, end_date_str: str) -> dict[str, pd.Series]:
    """
    批量拉取所有 ETF 的历史收盘价（东方财富接口），以字典形式缓存。
    返回：{ETF代码: pd.Series(收盘价, index=日期字符串)}
    """
    cache: dict[str, pd.Series] = {}

    for code in ETF_TARGETS:
        print(f"  获取ETF行情 {code} ...")
        try:
            df = ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start_date_str,
                end_date=end_date_str,
                adjust="",
            )
            df["date"] = pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d")
            cache[code] = df.set_index("date")["收盘"]
        except Exception as exc:
            print(f"    ⚠ 获取ETF {code} 行情失败: {exc}")
        time.sleep(0.5)

    return cache


_SSE_SCALE_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_SCALE_HEADERS = {
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
}


def fetch_sse_scale_cache(
    target_days: list, prev_date_str: str | None = None
) -> dict[str, dict[str, float | None]]:
    """
    直接调用上交所接口，批量获取目标日期的 SSE ETF 份额数据（支持历史查询）。
    同时额外获取 prev_date_str（第一个目标日的前一交易日），以便计算首日份额变动量。
    接口：https://query.sse.com.cn/commonQuery.do (COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L)
    返回：{日期字符串: {ETF代码: 份额(亿份)}}
    """
    dates_to_fetch: list[str] = []
    if prev_date_str:
        dates_to_fetch.append(prev_date_str)
    for d in target_days:
        dates_to_fetch.append(d.strftime("%Y%m%d"))

    cache: dict[str, dict[str, float | None]] = {}
    total = len(dates_to_fetch)
    for idx, d_str in enumerate(dates_to_fetch, 1):
        print(f"  [SSE份额 {idx}/{total}] {d_str}")
        stat_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": stat_date,
        }
        try:
            r = requests.get(
                _SSE_SCALE_URL, params=params, headers=_SSE_SCALE_HEADERS, timeout=15
            )
            data = r.json()
            day_data: dict[str, float | None] = {}
            for record in data.get("result", []):
                code = str(record.get("SEC_CODE", "")).zfill(6)
                if code in _SSE_CODES:
                    # TOT_VOL: raw value from SSE API, in 万份 (10,000 shares)
                    # Divide by 10,000 to convert to 亿份 (100 million shares)
                    tot_vol = _to_float(record.get("TOT_VOL"))
                    day_data[code] = tot_vol / 10000.0 if tot_vol is not None else None
            cache[d_str] = day_data
        except Exception as exc:
            print(f"    ⚠ 获取上交所份额数据失败 ({d_str}): {exc}")
            cache[d_str] = {}
        time.sleep(0.5)
    return cache


def fetch_szse_scale(date_str: str) -> dict[str, dict]:
    """
    从深交所接口获取深交所 ETF 的份额数据（接口不支持历史查询，仅返回当前最新数据）。
    返回：{ETF代码: {"当日份额": float, "前一日份额": float, "份额变动": float}}
    """
    result: dict[str, dict] = {}
    try:
        df = ak.fund_etf_scale_szse()
        df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
        df_filtered = df[df["基金代码"].isin(_SZSE_CODES)]
        for _, row in df_filtered.iterrows():
            code = str(row["基金代码"])
            # 当前接口只提供 "基金份额"，不提供前一日数据
            current = _to_float(row.get("当日份额") or row.get("基金份额"))
            prev = _to_float(row.get("前一日份额"))
            result[code] = {
                "当日份额": current,
                "前一日份额": prev,
                "份额变动": (current - prev) if (current is not None and prev is not None) else None,
            }
    except AttributeError:
        pass
    except Exception as exc:
        print(f"    ⚠ 获取深交所份额数据失败 ({date_str}): {exc}")
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def update_data() -> None:
    """增量更新 ETF 份额与指数行情数据，写入 national_team_data.csv。"""
    today = date.today()
    today_str = today.strftime("%Y%m%d")

    # ------------------------------------------------------------------
    # 1. 读取已有数据，确定起始日期
    # ------------------------------------------------------------------
    if os.path.exists(DATA_FILE):
        df_old = pd.read_csv(DATA_FILE, dtype={"基金代码": str, "日期": str})
        df_old["日期"] = df_old["日期"].astype(str)
        last_date_str = df_old["日期"].max()
        start_dt = datetime.strptime(last_date_str, "%Y%m%d").date()
        print(f"已有数据最新日期: {last_date_str}")
    else:
        df_old = pd.DataFrame()
        start_dt = datetime.strptime(DEFAULT_START_DATE, "%Y%m%d").date()
        print(f"首次运行，从 {DEFAULT_START_DATE} 开始获取全量历史数据")

    if start_dt >= today:
        print("数据已是最新，无需更新。")
        return

    start_date_str = start_dt.strftime("%Y%m%d")

    # ------------------------------------------------------------------
    # 2. 获取交易日历，筛选出需要补充的交易日
    # ------------------------------------------------------------------
    print("\n正在获取交易日历...")
    try:
        df_cal = ak.tool_trade_date_hist_sina()
        df_cal["trade_date"] = pd.to_datetime(df_cal["trade_date"]).dt.date
        mask = (df_cal["trade_date"] > start_dt) & (df_cal["trade_date"] <= today)
        target_days = df_cal.loc[mask, "trade_date"].tolist()
    except Exception as exc:
        print(f"获取交易日历失败: {exc}")
        return

    if not target_days:
        print("没有新的交易日需要更新。")
        return

    print(
        f"需要更新 {len(target_days)} 个交易日"
        f"（{target_days[0].strftime('%Y-%m-%d')} 至 {target_days[-1].strftime('%Y-%m-%d')}）"
    )

    # ------------------------------------------------------------------
    # 3. 批量预取指数行情 & ETF 收盘价
    # ------------------------------------------------------------------
    print("\n正在批量获取指数行情数据...")
    index_cache = fetch_index_cache(start_date_str, today_str)

    print("\n正在批量获取ETF历史收盘价数据...")
    etf_price_cache = fetch_etf_price_cache(start_date_str, today_str)

    # ------------------------------------------------------------------
    # 3b. 预取上交所 ETF 历史份额（含前一交易日，用于计算首日变动量）
    # ------------------------------------------------------------------
    all_trade_dates = df_cal.loc[df_cal["trade_date"] <= today, "trade_date"].tolist()
    try:
        first_idx = all_trade_dates.index(target_days[0])
        prev_day_str: str | None = (
            all_trade_dates[first_idx - 1].strftime("%Y%m%d")
            if first_idx > 0
            else None
        )
    except ValueError:
        prev_day_str = None

    print("\n正在批量获取上交所ETF历史份额数据...")
    sse_scale_cache = fetch_sse_scale_cache(target_days, prev_day_str)

    # ------------------------------------------------------------------
    # 4. 逐日获取 ETF 份额数据并合并
    # ------------------------------------------------------------------
    new_data_list: list[dict] = []
    print("\n正在逐日汇总数据...")

    last_day_str = target_days[-1].strftime("%Y%m%d")
    szse_scale_today = fetch_szse_scale(last_day_str)

    for i, d in enumerate(target_days):
        d_str = d.strftime("%Y%m%d")
        print(f"  [{i + 1}/{len(target_days)}] {d_str}")

        # SSE ETFs: 从预取缓存中获取当日和前一日份额，计算变动
        current_sse = sse_scale_cache.get(d_str, {})
        prev_d_str = (
            prev_day_str if i == 0 and prev_day_str
            else target_days[i - 1].strftime("%Y%m%d") if i > 0
            else None
        )
        prev_sse = sse_scale_cache.get(prev_d_str, {}) if prev_d_str else {}

        scale_data: dict[str, dict] = {}
        for code in _SSE_CODES:
            current = current_sse.get(code)
            prev = prev_sse.get(code)
            scale_data[code] = {
                "当日份额": current,
                "前一日份额": prev,
                "份额变动": (current - prev) if (current is not None and prev is not None) else None,
            }

        # SZSE ETFs: 深交所接口不支持历史查询，仅最近交易日填充份额数据
        if d_str == last_day_str:
            scale_data.update(szse_scale_today)

        # 为每只 ETF 构建一条记录
        for code, info in ETF_TARGETS.items():
            idx_code = info["index"]

            # 对应指数收盘价
            idx_series = index_cache.get(idx_code)
            idx_price = (
                float(idx_series[d_str])
                if idx_series is not None and d_str in idx_series.index
                else None
            )

            # ETF 收盘价
            etf_series = etf_price_cache.get(code)
            etf_price = (
                float(etf_series[d_str])
                if etf_series is not None and d_str in etf_series.index
                else None
            )

            shares = scale_data.get(code, {})
            new_data_list.append(
                {
                    "日期": d_str,
                    "基金代码": code,
                    "基金名称": info["name"],
                    "对应指数代码": idx_code,
                    "ETF收盘价": etf_price,
                    "当日份额(亿份)": shares.get("当日份额"),
                    "前一日份额(亿份)": shares.get("前一日份额"),
                    "份额变动(亿份)": shares.get("份额变动"),
                    "对应指数收盘价": idx_price,
                }
            )

    if not new_data_list:
        print("\n未获取到有效的新数据（数据源可能尚未更新）。")
        return

    # ------------------------------------------------------------------
    # 5. 合并新旧数据、去重、排序并写回 CSV
    # ------------------------------------------------------------------
    df_new = pd.DataFrame(new_data_list)
    df_final = pd.concat([df_old, df_new], ignore_index=True)

    # 同一日期+代码保留最新一条（多次运行时修正当天数据）
    df_final.drop_duplicates(subset=["日期", "基金代码"], keep="last", inplace=True)
    df_final.sort_values(by=["日期", "基金代码"], inplace=True)
    df_final.reset_index(drop=True, inplace=True)

    df_final.to_csv(DATA_FILE, index=False, encoding="utf-8-sig")
    print(f"\n✓ 成功追加 {len(df_new)} 条新记录，数据已保存至 {DATA_FILE}")


if __name__ == "__main__":
    update_data()
