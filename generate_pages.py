"""
GitHub Pages 静态页面生成器
===========================
从 data/etf_scale.csv 读取数据，生成包含以下图表的 docs/index.html：
    1. 四只 ETF 基金份额的堆叠面积图（左轴，亿份）
    2. 四只 ETF 的相对价格走势（右轴，价格/各自最大值）

用法：
  python generate_pages.py
"""

import os
import yaml
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# 加载配置
# ---------------------------------------------------------------------------

with open("config.yml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

DATA_FILE: str = _cfg["data_file"]
ETF_TARGETS: dict[str, str] = {
    str(k).zfill(6): str(v) for k, v in _cfg["etf_targets"].items()
}
OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.html")

# 颜色方案（与 ETF 顺序对应）
COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]


def generate_pages() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # 读取数据
    # ------------------------------------------------------------------
    if not os.path.exists(DATA_FILE):
        _write_no_data_page()
        print(f"数据文件 {DATA_FILE} 不存在，已生成占位页面。")
        return

    df = pd.read_csv(DATA_FILE, dtype={"基金代码": str, "日期": str})
    if df.empty:
        _write_no_data_page()
        print("数据文件为空，已生成占位页面。")
        return

    df["日期"] = pd.to_datetime(df["日期"], format="%Y%m%d")
    df = df.sort_values("日期")

    # ------------------------------------------------------------------
    # 构建图表
    # ------------------------------------------------------------------
    fig = make_subplots(
        rows=1,
        cols=1,
        subplot_titles=("ETF 基金份额与相对价格走势",),
        specs=[[{"secondary_y": True}]],
    )

    codes = list(ETF_TARGETS.keys())

    # 图1：堆叠面积图（份额）
    for idx, code in enumerate(codes):
        sub = df[df["基金代码"] == code].copy()
        name = ETF_TARGETS[code]
        color = COLORS[idx % len(COLORS)]
        fig.add_trace(
            go.Scatter(
                x=sub["日期"],
                y=sub["基金份额(亿份)"],
                name=name,
                mode="lines",
                fill="tonexty" if idx > 0 else "tozeroy",
                stackgroup="shares",
                line=dict(color=color, width=1.5),
                hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f} 亿份<extra>" + name + "</extra>",
            ),
            row=1, col=1, secondary_y=False,
        )

    # 图2：相对价格折线图（价格 / 各自最大值）
    for idx, code in enumerate(codes):
        sub = df[df["基金代码"] == code].copy()
        sub = sub.dropna(subset=["ETF收盘价"])
        name = ETF_TARGETS[code]
        color = COLORS[idx % len(COLORS)]

        if sub.empty:
            continue

        max_price = sub["ETF收盘价"].max()
        if pd.isna(max_price) or max_price <= 0:
            continue

        sub["相对价格"] = sub["ETF收盘价"] / max_price
        fig.add_trace(
            go.Scatter(
                x=sub["日期"],
                y=sub["相对价格"],
                name=f"{name}（相对价格）",
                mode="lines+markers",
                marker=dict(size=4),
                line=dict(color=color, width=1.5, dash="dot"),
                hovertemplate=(
                    "%{x|%Y-%m-%d}<br>相对价格 %{y:.4f}"
                    f"<br>归一化基准(最大值): {max_price:.4f}"
                    "<extra>" + name + "</extra>"
                ),
            ),
            row=1, col=1, secondary_y=True,
        )

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    fig.update_layout(
        title=dict(
            text="核心 ETF 监控（上交所）",
            font=dict(size=20),
            x=0.5,
        ),
        height=680,
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        margin=dict(l=60, r=40, t=100, b=60),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eeeeee")
    fig.update_yaxes(showgrid=True, gridcolor="#eeeeee", secondary_y=False)
    fig.update_yaxes(showgrid=False, secondary_y=True)
    fig.update_yaxes(title_text="基金份额 (亿份)", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="相对价格 (价格/各自最大值)", row=1, col=1, secondary_y=True)

    # ------------------------------------------------------------------
    # 写出 HTML
    # ------------------------------------------------------------------
    last_update = df["日期"].max().strftime("%Y-%m-%d") if not df.empty else "—"
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>核心 ETF 监控</title>
  <style>
    body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
            margin: 0; padding: 20px; background: #f5f5f5; }}
    .container {{ max-width: 1200px; margin: 0 auto; background: white;
                  padding: 20px; border-radius: 8px;
                  box-shadow: 0 2px 8px rgba(0,0,0,.1); }}
    h1 {{ color: #333; margin-top: 0; }}
    .meta {{ color: #888; font-size: 14px; margin-bottom: 20px; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>核心 ETF 监控</h1>
    <div class="meta">
      监控标的：{" | ".join(f"{c} {n}" for c, n in ETF_TARGETS.items())}
      &nbsp;&nbsp;|&nbsp;&nbsp;数据最新日期：{last_update}
    </div>
    {chart_html}
  </div>
</body>
</html>
"""

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✓ 静态页面已生成：{OUTPUT_FILE}（数据最新日期：{last_update}）")


def _write_no_data_page() -> None:
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>核心 ETF 监控</title>
</head>
<body>
  <h1>核心 ETF 监控</h1>
  <p>暂无数据，请等待首次数据采集完成。</p>
</body>
</html>
"""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    generate_pages()
