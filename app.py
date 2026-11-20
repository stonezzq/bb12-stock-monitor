"""BB12 iPhone scanner. Informational signals only; never places trades."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
import requests
import hmac
import re

DEFAULT_TICKERS = ["AAPL", "AMZN", "ASML", "AXP", "COST", "DIS", "GLD", "GOOGL", "IBIT", "JNJ", "JPM", "MCD", "META", "MSFT", "NFLX", "PDD", "TSLA", "V", "WMT"]
ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="BB12 信号监控", page_icon="📈", layout="wide")
st.title("📈 BB12 · 股票信号监控")

# Password gate protects the public Streamlit app. Do not put secrets in GitHub.
try:
    CONFIG = st.secrets["supabase"]
    SUPABASE_URL = str(CONFIG["url"]).rstrip("/")
    SUPABASE_KEY = str(CONFIG["service_role_key"])
    APP_PASSWORD = str(st.secrets["app_password"])
    if not (SUPABASE_URL.startswith("https://") and SUPABASE_KEY and APP_PASSWORD):
        raise ValueError("配置值为空或URL不是https")
except Exception:
    st.error("尚未配置Supabase和访问密码。请按部署说明在Streamlit Secrets中填写。")
    st.stop()

if not st.session_state.get("authenticated", False):
    with st.form("login"):
        password = st.text_input("访问密码", type="password")
        submitted = st.form_submit_button("进入BB12")
    if submitted:
        if hmac.compare_digest(password, APP_PASSWORD):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("访问密码错误")
    st.stop()

if st.button("退出登录"):
    st.session_state.clear()
    st.rerun()

st.caption("自定义股票池｜12周布林带｜Yahoo Finance 日线｜只发出候选信号，不连接券商、不下单")

with st.expander("信号定义与重要说明", expanded=False):
    st.markdown("""- **NEW_TOUCH**：今日收盘价 ≤ 今日参考下轨，且上一交易日收盘价 > 上一交易日参考下轨。\n- **BELOW_LOWER**：价格仍在下轨或下方，但不是首次跌破。\n- **NEAR_LOWER**：距离下轨在0%至3%之间，尚未触发。\n- **NO_SIGNAL**：未触发。\n- 每个交易日使用**上一个完整周**的12周布林带（12周收盘价SMA ± 2倍总体标准差），不使用本周未结束的周线。\n- 采用Yahoo复权价格（auto_adjust=True）。数据可能延迟、缺失或修订；开盘前和盘中不把当天未完成K线当作收盘信号。\n- 本程序与历史回测的信号定义应做逐日核对；特别是历史脚本的触发方式、复权方法和周线定义。\n- 不提供实时期权报价；Call行权价仅参考固定中轨，不是可交易合约。""")


def completed_daily(prices: pd.DataFrame) -> pd.DataFrame:
    """Discard the NY current day's bar before 17:00 ET (conservative)."""
    now = datetime.now(ET)
    df = prices.copy().sort_index()
    df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
    if now.hour < 17:
        df = df[df.index.date < now.date()]
    return df


def calculate(prices: pd.DataFrame) -> dict:
    df = completed_daily(prices)
    if len(df) < 80:
        raise ValueError("有效日线不足80根")
    close = df["Close"].dropna().astype(float)
    if len(close) < 80:
        raise ValueError("收盘价数据不足")
    # Weekly close for each week ending Friday, including holiday-shortened weeks.
    weekly = close.resample("W-FRI").last().dropna()
    mid = weekly.rolling(12, min_periods=12).mean()
    std = weekly.rolling(12, min_periods=12).std(ddof=0)
    bands = pd.DataFrame({"mid": mid, "lower": mid - 2 * std})
    # For a date in week W, use the band from W-1; this avoids look-ahead.
    week_end = close.index.to_period("W-FRI").to_timestamp(how="end").normalize()
    prev_week_bands = bands.shift(1).reindex(week_end)
    prev_week_bands.index = close.index
    joined = pd.DataFrame({"close": close}).join(prev_week_bands)
    joined = joined.dropna(subset=["close", "mid", "lower"])
    if len(joined) < 2:
        raise ValueError("布林带历史不足")
    last, prev = joined.iloc[-1], joined.iloc[-2]
    new_touch = last.close <= last.lower and prev.close > prev.lower
    below = last.close <= last.lower
    distance = (last.close / last.lower - 1) * 100
    if new_touch:
        status = "NEW_TOUCH"
    elif below:
        status = "BELOW_LOWER"
    elif 0 < distance <= 3:
        status = "NEAR_LOWER"
    else:
        status = "NO_SIGNAL"
    return {
        "ticker": "", "date": joined.index[-1].strftime("%Y-%m-%d"),
        "status": status, "close": round(float(last.close), 2),
        "lower": round(float(last.lower), 2), "mid": round(float(last.mid), 2),
        "distance_pct": round(float(distance), 2),
        "mid_gap_pct": round(float((last.mid / last.close - 1) * 100), 2),
        "call_strike_reference": round(float(last.mid), 2),
    }


MAX_TICKERS = 60


def normalize_ticker(raw: str) -> str:
    symbol = raw.strip().upper()
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}", symbol):
        raise ValueError("股票代码格式错误（只允许字母、数字、点、横杠、^、=）")
    return symbol


def parse_tickers(raw: str) -> list[str]:
    tickers = []
    for item in re.split(r"[,，;；\s]+", raw.strip()):
        if item:
            symbol = normalize_ticker(item)
            if symbol not in tickers:
                tickers.append(symbol)
    if len(tickers) > MAX_TICKERS:
        raise ValueError(f"最多监控{MAX_TICKERS}只股票")
    return tickers


def db_request(method: str, *, params=None, payload=None):
    url = f"{SUPABASE_URL}/rest/v1/bb12_watchlists"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }
    response = requests.request(method, url, headers=headers, params=params,
                                json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def load_watchlist():
    rows = db_request("GET", params={"id": "eq.1", "select": "tickers,revision"})
    if len(rows) != 1:
        raise RuntimeError("数据库中缺少id=1的股票池记录，请执行SQL初始化脚本")
    raw = rows[0]["tickers"]
    if not isinstance(raw, list):
        raise ValueError("数据库股票池格式不正确")
    tickers = parse_tickers(",".join(raw))
    return tickers, int(rows[0]["revision"])


def save_watchlist(tickers, expected_revision):
    if len(tickers) > MAX_TICKERS or len(tickers) != len(set(tickers)):
        raise ValueError("股票池无效")
    rows = db_request("PATCH", params={"id": "eq.1", "revision": f"eq.{expected_revision}"},
                      payload={"tickers": tickers, "revision": expected_revision + 1})
    if len(rows) != 1:
        raise RuntimeError("另一台设备已修改股票池。请点击‘从云端刷新’，再重新操作。")
    return expected_revision + 1


def refresh_cloud():
    tickers, revision = load_watchlist()
    st.session_state.watchlist = tickers
    st.session_state.revision = revision
    st.session_state.pop("result", None)
    st.session_state.pop("errors", None)


def commit_watchlist(next_tickers):
    next_revision = save_watchlist(next_tickers, st.session_state.revision)
    st.session_state.watchlist = next_tickers
    st.session_state.revision = next_revision
    st.session_state.pop("result", None)
    st.session_state.pop("errors", None)


# Read cloud on every full page load. During interactive reruns keep local state
# to avoid discarding a form action; the refresh button explicitly re-syncs.
if "watchlist" not in st.session_state:
    try:
        refresh_cloud()
    except Exception as exc:
        st.error(f"无法读取云端股票池：{exc}")
        st.stop()

st.subheader(f"📋 我的监控股票（{len(st.session_state.watchlist)}只）")
st.caption("☁️ 股票池保存在Supabase；添加、删除会立即写入云端。电脑与iPhone使用同一个访问密码。")
if st.button("🔄 从云端刷新股票池"):
    try:
        refresh_cloud()
        st.rerun()
    except Exception as exc:
        st.error(f"刷新失败：{exc}")

with st.form("add_ticker", clear_on_submit=True):
    add_raw = st.text_input("添加股票代码", placeholder="例如 NVDA，或 NVDA, AMD, KO")
    add_clicked = st.form_submit_button("➕ 添加股票")
if add_clicked:
    try:
        incoming = parse_tickers(add_raw)
        if not incoming:
            st.warning("请输入股票代码")
        else:
            merged = st.session_state.watchlist + [x for x in incoming if x not in st.session_state.watchlist]
            if len(merged) > MAX_TICKERS:
                st.error(f"股票池最多{MAX_TICKERS}只")
            elif merged == st.session_state.watchlist:
                st.info("股票已在观察池中")
            else:
                commit_watchlist(merged)
                st.rerun()
    except Exception as exc:
        st.error(f"保存失败，原列表未修改：{exc}")

with st.expander("管理 / 移除股票", expanded=False):
    to_remove = st.multiselect("选择要移除的代码", st.session_state.watchlist)
    a, b = st.columns(2)
    if a.button("移除所选", disabled=not to_remove, use_container_width=True):
        try:
            commit_watchlist([x for x in st.session_state.watchlist if x not in to_remove])
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")
    if b.button("恢复默认19只", use_container_width=True):
        try:
            commit_watchlist(DEFAULT_TICKERS.copy())
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")
    st.code(", ".join(st.session_state.watchlist) or "（空）", language=None)
    st.download_button("⬇️ 备份股票清单", "\n".join(st.session_state.watchlist) + "\n",
                       file_name="bb12_watchlist.txt", mime="text/plain")
    uploaded = st.file_uploader("从TXT恢复股票清单（每行一个代码）", type=["txt"])
    if uploaded is not None and st.button("导入并替换当前清单"):
        try:
            new_list = parse_tickers(uploaded.getvalue().decode("utf-8-sig"))
            commit_watchlist(new_list)
            st.rerun()
        except Exception as exc:
            st.error(f"导入失败：{exc}")

if not st.session_state.watchlist:
    st.info("当前没有监控股票。请先添加股票。")

@st.cache_data(ttl=900, show_spinner=False)
def scan_all(tickers: tuple[str, ...]) -> tuple[pd.DataFrame, list[str]]:
    results, errors = [], []
    for ticker in tickers:
        try:
            data = yf.download(ticker, period="2y", interval="1d", auto_adjust=True, progress=False, threads=False, timeout=15)
            if data.empty:
                raise ValueError("Yahoo未返回数据")
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            result = calculate(data)
            result["ticker"] = ticker
            results.append(result)
        except Exception as exc:
            errors.append(f"{ticker}: {str(exc)[:140]}")
    return pd.DataFrame(results), errors

st.write("点击检测按钮后才会请求Yahoo Finance数据。")
col_a, col_b = st.columns([1, 2])
with col_a:
    run = st.button(f"🔎 检测{len(st.session_state.watchlist)}只股票",
                    type="primary", use_container_width=True,
                    disabled=not st.session_state.watchlist)
with col_b:
    st.caption("15分钟内重复检测可能使用缓存。")
    if st.button("清除缓存并重新获取", disabled=not st.session_state.watchlist):
        scan_all.clear()
        run = True

if run:
    with st.spinner("正在获取行情并计算信号……"):
        df, errors = scan_all(tuple(st.session_state.watchlist))
    st.session_state["result"] = df
    st.session_state["errors"] = errors
    st.session_state["scanned_at"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

if "result" in st.session_state:
    df = st.session_state["result"]
    errors = st.session_state["errors"]
    st.caption(f"检测时间：{st.session_state['scanned_at']}。每只股票的实际行情日期见表格。")
    if not df.empty:
        dates = df["date"].unique()
        if len(dates) > 1:
            st.warning("不同股票返回的最新日线日期不一致，请核对行情日期。")
        count = int((df.status == "NEW_TOUCH").sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("新触发", count)
        c2.metric("仍在下轨以下", int((df.status == "BELOW_LOWER").sum()))
        c3.metric("临近下轨", int((df.status == "NEAR_LOWER").sum()))
        if count:
            st.error(f"🔔 今日发现 {count} 个新触发信号。请在IBKR核对报价后人工决策。")
        else:
            st.success("本次检测未发现 NEW_TOUCH 新触发信号。")
        sort_mode = st.radio("排序方式", ["距离下轨最近优先", "新信号优先"], horizontal=True)
        priority = {"NEW_TOUCH": 0, "BELOW_LOWER": 1, "NEAR_LOWER": 2, "NO_SIGNAL": 3}
        if sort_mode == "距离下轨最近优先":
            # Distance is signed: negative means already below the lower band.
            # Use absolute distance so stocks nearest the band appear first.
            df = df.assign(_abs_distance=df.distance_pct.abs()).sort_values(
                ["_abs_distance", "ticker"]).drop(columns="_abs_distance")
        else:
            df = df.assign(_rank=df.status.map(priority)).sort_values(
                ["_rank", "distance_pct", "ticker"]).drop(columns="_rank")
        st.caption("距下轨% = (收盘价 ÷ 下轨 − 1) × 100；负数代表已跌破下轨。距离最近排序按绝对值排序。")
        st.dataframe(df.rename(columns={"ticker":"代码", "date":"行情日期", "status":"状态", "close":"收盘价", "lower":"12周下轨", "mid":"12周中轨", "distance_pct":"距下轨%", "mid_gap_pct":"距中轨涨幅%", "call_strike_reference":"Call行权价参考"}), hide_index=True, use_container_width=True)
        st.download_button("⬇️ 下载本次检测CSV", df.to_csv(index=False).encode("utf-8-sig"), file_name="bb12_scan.csv", mime="text/csv")
        latest = df[df.status == "NEW_TOUCH"]
        if not latest.empty:
            st.subheader("新触发候选")
            for _, r in latest.iterrows():
                st.info(f"{r.ticker} · {r.date}｜收盘 {r.close}｜下轨 {r.lower}｜固定中轨 {r.mid}｜回归空间 {r.mid_gap_pct:+.2f}%｜Call参考行权价约 {r.call_strike_reference}，到期参考150–210天")
    else:
        st.error("未获取到有效行情，请稍后重试。")
    if errors:
        st.warning("以下股票行情获取失败（不能将失败视为无信号）：\n\n" + "\n\n".join(errors))
else:
    st.info("尚未执行扫描。打开网页后点击“检测股票”即可开始。")

st.divider()
st.caption("研究工具，不构成投资建议。仅在打开网页并点击按钮时扫描；无后台运行、无推送、无自动交易。")


# Call position management (same authenticated session / Supabase credentials)
from call_positions import render_call_positions
render_call_positions(SUPABASE_URL, SUPABASE_KEY)
