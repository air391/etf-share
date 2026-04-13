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

# 沪市（上交所）ETF 代码集合
_SSE_CODES = {c for c in ETF_TARGETS if not c.startswith("1")}
# 深市（深交所）ETF 代码集合
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


def fetch_sse_scale(date_str: str) -> dict[str, dict]:
    """
    从上交所接口获取指定日期所有上交所 ETF 的份额数据。
    返回：{ETF代码: {"当日份额": float, "前一日份额": float, "份额变动": float}}
    """
    result: dict[str, dict] = {}
    try:
        df = ak.fund_etf_scale_sse(date=date_str)
        df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
        df_filtered = df[df["基金代码"].isin(_SSE_CODES)]
        for _, row in df_filtered.iterrows():
            code = str(row["基金代码"])
            current = _to_float(row.get("当日份额"))
            prev = _to_float(row.get("前一日份额"))
            result[code] = {
                "当日份额": current,
                "前一日份额": prev,
                "份额变动": (current - prev) if (current is not None and prev is not None) else None,
            }
    except Exception as exc:
        print(f"    ⚠ 获取上交所份额数据失败 ({date_str}): {exc}")
    return result


def fetch_szse_scale(date_str: str) -> dict[str, dict]:
    """
    从深交所接口获取指定日期所有深交所 ETF 的份额数据。
    返回：{ETF代码: {"当日份额": float, "前一日份额": float, "份额变动": float}}
    """
    result: dict[str, dict] = {}
    try:
        df = ak.fund_etf_scale_szse(date=date_str)
        df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
        df_filtered = df[df["基金代码"].isin(_SZSE_CODES)]
        for _, row in df_filtered.iterrows():
            code = str(row["基金代码"])
            current = _to_float(row.get("当日份额"))
            prev = _to_float(row.get("前一日份额"))
            result[code] = {
                "当日份额": current,
                "前一日份额": prev,
                "份额变动": (current - prev) if (current is not None and prev is not None) else None,
            }
    except Exception:
        # fund_etf_scale_szse 在部分 akshare 版本中可能不存在，静默跳过
        pass
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
    # 4. 逐日获取 ETF 份额数据并合并
    # ------------------------------------------------------------------
    new_data_list: list[dict] = []
    print("\n正在逐日获取ETF份额数据...")

    for i, d in enumerate(target_days, 1):
        d_str = d.strftime("%Y%m%d")
        print(f"  [{i}/{len(target_days)}] {d_str}")

        # 分别从两个交易所拉取份额数据
        scale_data: dict[str, dict] = {}
        scale_data.update(fetch_sse_scale(d_str))
        scale_data.update(fetch_szse_scale(d_str))

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

        time.sleep(0.5)

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
