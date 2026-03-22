"""
株式AI自動分析 v2 - 学習システム搭載版
SESSION=600 / 905 / 1200 / 1535

【学習の仕組み】
1. 600: 朝の分析 + 過去の類似パターンをプロンプトに注入
2. 1535: 予測vs実際を詳細に記録、パターンタグ付け、的中率計算
3. 蓄積データ: learning_db.json に全履歴を保存
4. 毎朝: 類似パターンの過去結果を参照して分析精度を上げる
"""

import os
import sys
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import jpholiday
import anthropic
import yfinance as yf

JST = timezone(timedelta(hours=9))
DATA = Path("data")
DATA.mkdir(exist_ok=True)
YEAR_END = {(12, 31), (1, 1), (1, 2), (1, 3)}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in YEAR_END:
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def safe_get(url, timeout=20, headers=None, params=None):
    r = requests.get(url, headers=headers or HEADERS, timeout=timeout, params=params)
    r.raise_for_status()
    return r


def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def save(filename, data):
    path = DATA / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {path}")


def load(filename):
    path = DATA / filename
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_text(filename, text):
    path = DATA / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"保存: {path}")


def parse_json(raw):
    """JSON解析。壊れたJSONも複数の方法でリカバリ"""
    cleaned = re.sub(r"```json|```", "", raw).strip()
    cleaned = (
        cleaned.replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
    )
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("JSONオブジェクトが見つかりません")

    # 方法1: そのままパース
    body = cleaned[start:]
    end = body.rfind("}")
    if end != -1:
        candidate = body[:end+1]
        candidate = re.sub(r",\s*}", "}", candidate)
        candidate = re.sub(r",\s*]", "]", candidate)
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 方法2: 括弧の深さを追って正しい末尾を探す
    depth = 0
    for i, ch in enumerate(body):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = body[:i+1]
                candidate = re.sub(r",\s*}", "}", candidate)
                candidate = re.sub(r",\s*]", "]", candidate)
                try:
                    return json.loads(candidate)
                except Exception:
                    break

    # 方法3: 末尾を削りながらパース（途中で切れた場合）
    for trim in range(0, min(500, len(body)), 10):
        candidate = body[:len(body)-trim].rstrip().rstrip(",")
        # 未閉じの括弧を補完
        opens = candidate.count("{") - candidate.count("}")
        arr_opens = candidate.count("[") - candidate.count("]")
        candidate += "]" * max(0, arr_opens) + "}" * max(0, opens)
        candidate = re.sub(r",\s*}", "}", candidate)
        candidate = re.sub(r",\s*]", "]", candidate)
        try:
            return json.loads(candidate)
        except Exception:
            continue

    raise ValueError("JSONの解析に失敗しました")


def call_claude(prompt, max_tokens=2400):
    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in res.content if block.type == "text")


def arr(v):
    return v if isinstance(v, list) else []


# ═══════════════════════════════════════════════
# 指標取得（yfinance）
# ═══════════════════════════════════════════════

def yahoo_quote(symbol, label):
    try:
        ticker = yf.Ticker(symbol)
        fast = ticker.fast_info
        price = fast.last_price
        prev = fast.previous_close
        if price is None or prev is None:
            return None
        price = float(price)
        prev = float(prev)
        change = round(price - prev, 2)
        sign = "+" if change >= 0 else ""
        pct = round((change / prev) * 100, 2) if prev != 0 else 0
        pct_sign = "+" if pct >= 0 else ""
        try:
            # fast_infoから時刻取得を試みる（複数の属性名を試す）
            last_time = (
                getattr(fast, "regularMarketTime", None) or
                getattr(fast, "lastTradeTime", None)
            )
            if last_time and isinstance(last_time, (int, float)) and last_time > 0:
                jst_time = datetime.fromtimestamp(int(last_time), JST).strftime("%H:%M")
            elif last_time and hasattr(last_time, "timestamp"):
                # datetimeオブジェクトの場合
                jst_time = last_time.astimezone(JST).strftime("%H:%M")
            else:
                # historyから最新の時刻を取得（フォールバック）
                try:
                    hist = ticker.history(period="1d", interval="1m")
                    if not hist.empty:
                        last_idx = hist.index[-1]
                        if hasattr(last_idx, "to_pydatetime"):
                            jst_time = last_idx.to_pydatetime().astimezone(JST).strftime("%H:%M")
                        else:
                            jst_time = "—"
                    else:
                        jst_time = "—"
                except Exception:
                    jst_time = "—"
        except Exception:
            jst_time = "—"
        return {
            "name": label,
            "label": label,
            "value": f"{price:,.2f}",
            "value_raw": price,
            "change": f"{sign}{change:,.2f}",
            "change_raw": change,
            "percent": f"{pct_sign}{pct:.2f}%",
            "percent_raw": pct,
            "time": jst_time,
            "source": f"yfinance:{symbol}",
        }
    except Exception as e:
        print(f"yfinance error {symbol}: {e}")
        return None


def fetch_strict_market_quotes():
    quotes = {
        "indices": [], "world_indices": [], "forex": [],
        "futures": [], "sox": [], "oil": [], "search_results": [],
    }
    targets = [
        ("indices",       "^N225",  "日経平均"),
        ("futures",       "NIY=F",  "日経先物"),
        ("forex",         "JPY=X",  "ドル円"),
        ("world_indices", "^DJI",   "NYダウ"),
        ("sox",           "^SOX",   "SOX"),
        ("oil",           "CL=F",   "WTI原油"),
    ]
    ok = 0
    for bucket, symbol, label in targets:
        item = yahoo_quote(symbol, label)
        if item:
            quotes[bucket].append(item)
            quotes["search_results"].append(f"yfinance:{label} 取得成功")
            ok += 1
        else:
            quotes["search_results"].append(f"yfinance:{label} 未取得")
    print(f"strict quotes success: {ok}/{len(targets)}")
    return quotes


# ═══════════════════════════════════════════════
# 米国テーマ翻訳テーブル
# ═══════════════════════════════════════════════

US_THEME_MAP = {
    "defense": {
        "label": "防衛・宇宙",
        "etfs": ["ITA", "XAR"],
        "us_stocks": ["LMT", "RTX", "NOC", "GE"],
        "jp_sectors": ["防衛・宇宙関連"],
        "jp_stocks": [
            {"code": "7011", "name": "三菱重工"},
            {"code": "7013", "name": "IHI"},
            {"code": "6503", "name": "三菱電機"},
            {"code": "7012", "name": "川崎重工"},
            {"code": "6688", "name": "QPS研究所"},
        ],
        "jp_note": "地政学リスク・防衛予算増額の文脈で連動しやすい。大型3社が先導するか確認。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "低",
        "fx_sensitivity": "中",
    },
    "nuclear_power": {
        "label": "原子力・電力インフラ",
        "etfs": ["URA", "XLU", "GRID"],
        "us_stocks": ["CEG", "GEV", "VST", "ETN"],
        "jp_sectors": ["原子力関連", "電力設備・送配電"],
        "jp_stocks": [
            {"code": "7011", "name": "三菱重工"},
            {"code": "6503", "name": "三菱電機"},
            {"code": "5803", "name": "フジクラ"},
            {"code": "5801", "name": "古河電工"},
            {"code": "1942", "name": "関電工"},
        ],
        "jp_note": "AIデータセンター電力需要×原子力再稼働の文脈。電線・重電・設備工事に波及。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "中",
        "fx_sensitivity": "低",
    },
    "rare_earth": {
        "label": "重要鉱物・レアアース",
        "etfs": ["REMX", "PICK"],
        "us_stocks": ["MP", "LTHM", "ALB"],
        "jp_sectors": ["非鉄金属", "素材・化学", "商社"],
        "jp_stocks": [
            {"code": "5711", "name": "三菱マテリアル"},
            {"code": "5713", "name": "住友金属鉱山"},
            {"code": "8001", "name": "伊藤忠商事"},
            {"code": "8053", "name": "住友商事"},
            {"code": "4185", "name": "JSR"},
        ],
        "jp_note": "思惑先行でボラ高め。商社→非鉄→素材の順に波及確認。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "低",
        "fx_sensitivity": "中",
    },
    "ai_infra": {
        "label": "AIインフラ",
        "etfs": ["GRID", "COPX", "AIQ"],
        "us_stocks": ["ETN", "VRT", "GEV", "CARR"],
        "jp_sectors": ["電線・ケーブル", "変圧器・電力設備", "設備工事"],
        "jp_stocks": [
            {"code": "5803", "name": "フジクラ"},
            {"code": "5801", "name": "古河電工"},
            {"code": "5802", "name": "住友電工"},
            {"code": "1942", "name": "関電工"},
            {"code": "6361", "name": "荏原製作所"},
        ],
        "jp_note": "AI電力需要→電線・冷却・変圧器の流れ。半導体本丸より翻訳精度が高い。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "中",
        "fx_sensitivity": "低",
    },
    "semiconductor": {
        "label": "半導体",
        "etfs": ["SOXX", "SMH"],
        "us_stocks": ["NVDA", "AMD", "MU", "AVGO"],
        "jp_sectors": ["半導体製造装置", "半導体材料"],
        "jp_stocks": [
            {"code": "8035", "name": "東京エレクトロン"},
            {"code": "6857", "name": "アドバンテスト"},
            {"code": "4063", "name": "信越化学"},
            {"code": "6146", "name": "ディスコ"},
            {"code": "7741", "name": "HOYA"},
        ],
        "jp_note": "NVDAの動きに東エレク・アドバンテストが連動。SOXが-1%超なら見送り優先。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "高",
        "fx_sensitivity": "高",
    },
    "energy_shipping": {
        "label": "海運・エネルギー輸送",
        "etfs": ["XLE", "OIH"],
        "us_stocks": ["XOM", "CVX", "SLB", "MPC"],
        "jp_sectors": ["海運", "石油・資源", "エネルギー輸送"],
        "jp_stocks": [
            {"code": "9104", "name": "商船三井"},
            {"code": "9101", "name": "日本郵船"},
            {"code": "1605", "name": "INPEX"},
            {"code": "5020", "name": "ENEOSホールディングス"},
            {"code": "9107", "name": "川崎汽船"},
        ],
        "jp_note": "原油・天然ガス価格と連動。WTI95ドル超で海運・資源株に資金集中しやすい。",
        "oil_sensitivity": "高",
        "rate_sensitivity": "低",
        "fx_sensitivity": "中",
    },
    "utility_grid": {
        "label": "公益・送配電",
        "etfs": ["XLU", "GRID", "FUTY"],
        "us_stocks": ["NEE", "ETN", "AEP", "SO"],
        "jp_sectors": ["電力・ガス", "電力設備"],
        "jp_stocks": [
            {"code": "9501", "name": "東京電力HD"},
            {"code": "9503", "name": "関西電力"},
            {"code": "1942", "name": "関電工"},
            {"code": "1944", "name": "きんでん"},
            {"code": "5803", "name": "フジクラ"},
        ],
        "jp_note": "金利低下局面で買われやすい。原子力再稼働テーマと重なりやすい。",
        "oil_sensitivity": "低",
        "rate_sensitivity": "高",
        "fx_sensitivity": "低",
    },
    "resource_dev": {
        "label": "資源開発",
        "etfs": ["XLE", "GDX", "PICK"],
        "us_stocks": ["FCX", "NEM", "BHP", "RIO"],
        "jp_sectors": ["非鉄金属", "資源開発", "商社"],
        "jp_stocks": [
            {"code": "5713", "name": "住友金属鉱山"},
            {"code": "1605", "name": "INPEX"},
            {"code": "8031", "name": "三井物産"},
            {"code": "8053", "name": "住友商事"},
            {"code": "5711", "name": "三菱マテリアル"},
        ],
        "jp_note": "資源価格全般の上昇時に商社・非鉄が連動。円安が重なると効果が増幅。",
        "oil_sensitivity": "高",
        "rate_sensitivity": "低",
        "fx_sensitivity": "高",
    },
}

# 米国テーマ用シンボルリスト（全ETF + 主要個別株）
US_SYMBOLS = [
    # マクロ
    ("macro", "^VIX",   "VIX"),
    ("macro", "^TNX",   "US10Y金利"),
    ("macro", "NG=F",   "天然ガス"),
    ("macro", "GC=F",   "金"),
    # 防衛
    ("defense",    "ITA",  "ITA(防衛ETF)"),
    ("defense",    "LMT",  "ロッキード"),
    ("defense",    "RTX",  "レイセオン"),
    # 原子力・電力
    ("nuclear_power", "URA",  "URA(ウランETF)"),
    ("nuclear_power", "XLU",  "XLU(公益ETF)"),
    ("nuclear_power", "GRID", "GRID(電力設備ETF)"),
    ("nuclear_power", "CEG",  "コンステレーション"),
    ("nuclear_power", "GEV",  "GEベルノバ"),
    # レアアース
    ("rare_earth", "REMX", "REMX(レアアースETF)"),
    ("rare_earth", "MP",   "MPマテリアルズ"),
    # AIインフラ
    ("ai_infra",   "ETN",  "イートン"),
    ("ai_infra",   "VRT",  "バーティブ"),
    # 半導体
    ("semiconductor", "SOXX", "SOXX(半導体ETF)"),
    ("semiconductor", "NVDA", "エヌビディア"),
    ("semiconductor", "AMD",  "AMD"),
    # エネルギー・海運
    ("energy_shipping", "XLE",  "XLE(エネルギーETF)"),
    ("energy_shipping", "XOM",  "エクソン"),
    # 公益
    ("utility_grid", "FUTY", "FUTY(公益ETF)"),
    # 資源
    ("resource_dev", "GDX",  "GDX(金鉱ETF)"),
    ("resource_dev", "FCX",  "フリーポートマク"),
]


# ═══════════════════════════════════════════════
# 米国テーマデータ取得
# ═══════════════════════════════════════════════

def fetch_us_theme_data():
    """米国ETF・代表株・マクロ指標をyfinanceで取得してテーマ別に集計"""
    print("[US] 米国テーマデータ取得中...")
    results = {}   # theme_key -> list of quotes
    macro   = []

    for theme_key, symbol, label in US_SYMBOLS:
        item = yahoo_quote(symbol, label)
        if not item:
            continue
        if theme_key == "macro":
            macro.append(item)
        else:
            results.setdefault(theme_key, []).append(item)

    # テーマ別スコア計算（ETFの平均騰落率で強弱を判定）
    theme_scores = {}
    for key, items in results.items():
        etf_items  = [i for i in items if i["label"].endswith("ETF)") or i["label"].endswith("ETF")]
        all_items  = items
        # ETFがあればETF優先、なければ全銘柄の平均
        base = etf_items if etf_items else all_items
        if not base:
            continue
        pcts = []
        for i in base:
            try:
                p = float(str(i.get("percent","0")).replace("%","").replace("+",""))
                pcts.append(p)
            except:
                pass
        avg = sum(pcts)/len(pcts) if pcts else 0
        theme_scores[key] = {
            "avg_pct":   round(avg, 2),
            "strength":  "強" if avg >= 1.0 else "中" if avg >= 0.2 else "弱" if avg >= -0.5 else "下落",
            "quotes":    items,
            "label":     US_THEME_MAP[key]["label"],
            "jp_stocks": US_THEME_MAP[key]["jp_stocks"],
            "jp_note":   US_THEME_MAP[key]["jp_note"],
            "jp_sectors":US_THEME_MAP[key]["jp_sectors"],
        }

    # VIX・金利・天然ガスをマクロとして整理
    macro_summary = {}
    for m in macro:
        macro_summary[m["label"]] = {
            "value":   m.get("value","—"),
            "change":  m.get("change","—"),
            "percent": m.get("percent","—"),
        }

    print(f"[US] 取得テーマ数: {len(theme_scores)}, マクロ指標: {len(macro_summary)}")
    return {"themes": theme_scores, "macro": macro_summary}


# ═══════════════════════════════════════════════
# PTS取得（株探PTSニュース）
# ═══════════════════════════════════════════════

def fetch_pts_data():
    """株探のPTSランキングを取得"""
    print("[PTS] PTSデータ取得中...")
    pts = {"gainers": [], "losers": [], "source": "kabutan_pts", "status": "ok"}

    try:
        from bs4 import BeautifulSoup

        # 株探のPTS夜間取引ランキングページ
        url = "https://kabutan.jp/warning/?mode=2_9"
        res = safe_get(url, timeout=15)
        if not res:
            # フォールバック：値上がりランキングページ
            pts["status"] = "fallback"
            return pts

        soup = BeautifulSoup(res.text, "html.parser")

        def parse_pts_table(soup, table_idx):
            rows = []
            tables = soup.find_all("table", class_="s-table")
            if len(tables) <= table_idx:
                # class指定なしのtableを試す
                tables = soup.find_all("table")
            if len(tables) <= table_idx:
                return rows

            for tr in tables[table_idx].find_all("tr")[1:16]:
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                try:
                    name = tds[0].get_text(strip=True)
                    code = tds[1].get_text(strip=True) if len(tds) > 1 else ""
                    price= tds[2].get_text(strip=True) if len(tds) > 2 else ""
                    chg  = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                    vol  = tds[4].get_text(strip=True) if len(tds) > 4 else ""
                    if name and (code or price):
                        rows.append({
                            "name": name, "code": code,
                            "pts_price": price, "pts_change": chg,
                            "pts_volume": vol,
                        })
                except:
                    continue
            return rows

        # 値上がり・値下がり両方取得
        gainers = parse_pts_table(soup, 0)
        losers  = parse_pts_table(soup, 1)

        if not gainers:
            # 別URLパターンを試す
            url2 = "https://kabutan.jp/warning/?mode=2_9&market=1"
            res2 = safe_get(url2, timeout=15)
            if res2:
                soup2 = BeautifulSoup(res2.text, "html.parser")
                gainers = parse_pts_table(soup2, 0)

        pts["gainers"] = gainers[:15]
        pts["losers"]  = losers[:10]
        pts["status"]  = "ok" if gainers else "empty"
        print(f"[PTS] 値上がり: {len(gainers)}件, 値下がり: {len(losers)}件")

    except Exception as e:
        print(f"[PTS] 取得エラー: {e}")
        pts["status"] = "error"

    return pts


# ═══════════════════════════════════════════════
# 米国テーマ × PTS 統合コンテキスト生成
# ═══════════════════════════════════════════════

def build_us_context(us_data, pts_data):
    """米国テーマ + PTS反応をClaudeプロンプト用に変換"""

    # 米国テーマを強い順にソート
    themes = us_data.get("themes", {})
    macro  = us_data.get("macro",  {})
    sorted_themes = sorted(themes.items(), key=lambda x: x[1]["avg_pct"], reverse=True)

    # マクロサマリー
    vix = macro.get("VIX", {})
    us10y = macro.get("US10Y金利", {})
    ng = macro.get("天然ガス", {})
    gold = macro.get("金", {})

    macro_lines = []
    if vix: macro_lines.append(f"VIX: {vix.get('value','—')} ({vix.get('percent','—')})")
    if us10y: macro_lines.append(f"US10Y: {us10y.get('value','—')} ({us10y.get('change','—')})")
    if ng: macro_lines.append(f"天然ガス: {ng.get('value','—')} ({ng.get('percent','—')})")
    if gold: macro_lines.append(f"金: {gold.get('value','—')} ({gold.get('percent','—')})")

    # PTSの値上がりランキング（テーマ翻訳テーブルの銘柄コードと照合）
    pts_gainers = pts_data.get("gainers", [])
    pts_losers  = pts_data.get("losers",  [])

    # PTS反応をテーマ別に分類
    pts_by_theme = {}
    for theme_key, theme_info in US_THEME_MAP.items():
        jp_codes = {s["code"] for s in theme_info["jp_stocks"]}
        theme_gainers = [p for p in pts_gainers if p.get("code","") in jp_codes]
        theme_losers  = [p for p in pts_losers  if p.get("code","") in jp_codes]
        if theme_gainers or theme_losers:
            pts_by_theme[theme_key] = {
                "label":   theme_info["label"],
                "gainers": theme_gainers,
                "losers":  theme_losers,
            }

    # テキスト生成
    lines = ["=== 米国テーマ連動分析 ==="]

    # マクロ
    if macro_lines:
        lines.append("[マクロ] " + " / ".join(macro_lines))

    # テーマ強弱
    lines.append("\n[米国テーマ強弱]")
    for key, info in sorted_themes[:6]:
        strength = info["strength"]
        avg = info["avg_pct"]
        label = info["label"]
        jp_stocks_str = "・".join([s["name"] for s in info["jp_stocks"][:3]])
        lines.append(
            f"  {strength} {label} ({avg:+.1f}%)"
            f" → 日本波及: {jp_stocks_str}"
            f" ※{info['jp_note'][:30]}"
        )

    # PTS反応
    if pts_gainers:
        lines.append(f"\n[PTS値上がり上位] (status: {pts_data.get('status','?')})")
        for p in pts_gainers[:10]:
            lines.append(f"  {p['name']}({p.get('code','')}) {p.get('pts_change','—')} 出来高:{p.get('pts_volume','—')}")

    # テーマ別PTS反応
    if pts_by_theme:
        lines.append("\n[テーマ別PTS反応]")
        for key, info in pts_by_theme.items():
            g_names = [p["name"] for p in info["gainers"]]
            l_names = [p["name"] for p in info["losers"]]
            if g_names:
                lines.append(f"  {info['label']}: 上昇={','.join(g_names[:3])}")
            if l_names:
                lines.append(f"  {info['label']}: 下落={','.join(l_names[:2])}")

    lines.append("=== 米国テーマ連動分析ここまで ===")

    return "\n".join(lines), {
        "sorted_themes": [(k, v["label"], v["avg_pct"], v["strength"]) for k,v in sorted_themes],
        "pts_by_theme": pts_by_theme,
        "macro": macro,
    }


# ═══════════════════════════════════════════════
# ニュース・テーマ取得
# ═══════════════════════════════════════════════

def extract_table_rows(table, limit=40):
    rows = []
    for tr in table.select("tr"):
        cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.select("th, td")]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
        if len(rows) >= limit:
            break
    return rows


def is_valid_news_text(text):
    text = clean_text(text)
    if len(text) < 14:
        return False
    ng = [
        r"すでに会員の方はログイン", r"プレミアム会員限定", r"ログイン",
        r"銘柄検索", r"メニュー", r"PC版を表示", r"人気テーマ", r"人気株",
        r"ベスト30を見る", r"お知らせ", r"会員限定", r"^\d+$",
        r"^(TOP|決算|開示|人気|コラム)$", r"日経平均", r"ドル円", r"NYダウ",
        r"上海総合", r"日経先物", r"日経225先物", r"^\s*PR\s*$",
    ]
    for p in ng:
        if re.search(p, text):
            return False
    return True


def parse_warning_table(rows, mode="gainers"):
    parsed = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        code = ""
        for cell in row:
            if re.fullmatch(r"\d{4}[A-Z]?", cell):
                code = cell
                break
        if not code:
            continue
        try:
            idx = row.index(code)
        except ValueError:
            continue
        name = row[idx + 1] if idx + 1 < len(row) else ""
        if not name:
            continue
        item = {"code": code, "name": name}
        if mode in ("gainers", "losers"):
            item["price"] = row[idx + 2] if idx + 2 < len(row) else ""
            item["change"] = row[-1] if row else ""
        else:
            item["volume"] = row[-2] if len(row) >= 2 else ""
            item["change"] = row[-1] if row else ""
        parsed.append(item)
    return parsed[:15]


def fetch_kabutan_theme_news():
    result = {"themes": [], "news": [], "top_gainers": [], "top_losers": [], "volume_surge": []}
    try:
        r = safe_get("https://kabutan.jp/")
        soup = BeautifulSoup(r.text, "html.parser")
        theme_candidates = []
        for a in soup.select("a[href*='theme'], a[href*='/themes/']"):
            t = clean_text(a.get_text(" ", strip=True))
            if 2 <= len(t) <= 30:
                theme_candidates.append(t)
        result["themes"] = list(dict.fromkeys(theme_candidates))[:12]
        news_candidates = []
        for a in soup.select("a[href]"):
            t = clean_text(a.get_text(" ", strip=True))
            if is_valid_news_text(t):
                news_candidates.append(t)
        result["news"] = list(dict.fromkeys(news_candidates))[:14]
    except Exception as e:
        print(f"kabutan home error: {e}")

    pages = [
        ("gainers", "https://kabutan.jp/warning/?mode=2_1"),
        ("losers",  "https://kabutan.jp/warning/?mode=2_2"),
        ("volume",  "https://kabutan.jp/warning/?mode=25_1"),
    ]
    for kind, url in pages:
        try:
            r = safe_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for table in soup.find_all("table"):
                rows = extract_table_rows(table, limit=40)
                if len(rows) < 3:
                    continue
                header = " | ".join(rows[0])
                if kind == "gainers" and "コード" in header and "銘柄名" in header:
                    result["top_gainers"] = parse_warning_table(rows, "gainers")
                    break
                if kind == "losers" and "コード" in header and "銘柄名" in header:
                    result["top_losers"] = parse_warning_table(rows, "losers")
                    break
                if kind == "volume" and "コード" in header and ("出来高" in header or "売買高" in header):
                    result["volume_surge"] = parse_warning_table(rows, "volume")
                    break
            time.sleep(0.4)
        except Exception as e:
            print(f"ランキング取得エラー {url}: {e}")
    return result


def fetch_nhk_news():
    news = []
    try:
        r = safe_get("https://www3.nhk.or.jp/news/")
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a"):
            text = clean_text(a.get_text(" ", strip=True))
            if len(text) >= 15 and text not in news:
                news.append(text[:120])
            if len(news) >= 10:
                break
        print(f"NHKニュース: {len(news)}件")
    except Exception as e:
        print(f"NHKエラー: {e}")
    return news[:10]


def fetch_reuters_news():
    news = []
    urls = [
        "https://jp.reuters.com/markets/",
        "https://jp.reuters.com/business/",
        "https://jp.reuters.com/world/",
    ]
    for url in urls:
        try:
            r = safe_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("a, h2, h3"):
                text = clean_text(el.get_text(" ", strip=True))
                if len(text) >= 15 and text not in news:
                    news.append(text[:120])
                if len(news) >= 10:
                    print(f"ロイターニュース: {len(news)}件")
                    return news[:10]
            time.sleep(0.4)
        except Exception as e:
            print(f"ロイターエラー {url}: {e}")
    print(f"ロイターニュース: {len(news)}件")
    return news[:10]


def classify_news(news_items):
    classified = {"macro": [], "industry": [], "stock_specific": [], "supply_demand": [], "noise": []}
    for text in news_items:
        t = clean_text(text)
        lower = t.lower()
        if any(k in t for k in ["米国", "金利", "FOMC", "雇用", "CPI", "為替", "原油", "地政学", "中東", "関税"]):
            classified["macro"].append(t)
        elif any(k in t for k in ["半導体", "AI", "電力", "防衛", "資源", "原油", "量子", "データセンター"]):
            classified["industry"].append(t)
        elif any(k in t for k in ["上方修正", "受注", "提携", "承認", "決算", "受賞", "新製品"]):
            classified["stock_specific"].append(t)
        elif any(k in lower for k in ["自社株", "増担", "売り禁", "大量保有", "需給", "ストップ高", "出来高"]):
            classified["supply_demand"].append(t)
        else:
            classified["noise"].append(t)
    for k in classified:
        classified[k] = list(dict.fromkeys(classified[k]))[:6]
    return classified


def fetch_all_market_data():
    strict = fetch_strict_market_quotes()
    kabu = fetch_kabutan_theme_news()
    us_data = fetch_us_theme_data()
    pts_data = fetch_pts_data()
    combined_news = kabu["news"]
    result = {
        "indices":         strict["indices"],
        "world_indices":   strict["world_indices"],
        "forex":           strict["forex"],
        "futures":         strict["futures"],
        "sox":             strict["sox"],
        "oil":             strict["oil"],
        "sector":          [],
        "us_themes":       us_data.get("themes", {}),
        "us_macro":        us_data.get("macro",  {}),
        "pts":             pts_data,
        "top_gainers":     kabu["top_gainers"],
        "top_losers":      kabu["top_losers"],
        "volume_surge":    kabu["volume_surge"],
        "themes":          kabu["themes"],
        "news":            combined_news,
        "classified_news": classify_news(combined_news),
        "search_results":  strict["search_results"],
        "source":          "yfinance + kabutan.jp",
    }
    print("=== market summary ===")
    for key in ["indices", "world_indices", "forex", "futures", "sox", "oil",
                "top_gainers", "top_losers", "volume_surge", "themes", "news"]:
        print(f"{key}: {len(result[key])}")

    live_items = []
    for bucket in ["indices", "futures", "forex", "world_indices", "sox", "oil"]:
        live_items.extend(result.get(bucket, []))
    save("market_live.json", {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "items": live_items,
    })
    return result


def build_data_sources_summary(market, nhk, reuters):
    sources = []
    if market.get("indices"):       sources.append(f"日経平均{len(market['indices'])}件")
    if market.get("futures"):       sources.append(f"日経先物{len(market['futures'])}件")
    if market.get("world_indices"): sources.append(f"NYダウ{len(market['world_indices'])}件")
    if market.get("forex"):         sources.append(f"ドル円{len(market['forex'])}件")
    if market.get("sox"):           sources.append(f"SOX{len(market['sox'])}件")
    if market.get("oil"):           sources.append(f"原油{len(market['oil'])}件")
    if market.get("top_gainers"):   sources.append(f"値上がり{len(market['top_gainers'])}件")
    if market.get("top_losers"):    sources.append(f"値下がり{len(market['top_losers'])}件")
    if market.get("volume_surge"):  sources.append(f"出来高急増{len(market['volume_surge'])}件")
    if market.get("themes"):        sources.append(f"テーマ{len(market['themes'])}件")
    if market.get("news"):          sources.append(f"かぶたんニュース{len(market['news'])}件")
    if nhk:                         sources.append(f"NHKニュース{len(nhk)}件")
    if reuters:                     sources.append(f"ロイター{len(reuters)}件")
    return sources


# ═══════════════════════════════════════════════
# 学習データベース
# ═══════════════════════════════════════════════

def load_learning_db():
    db = load("learning_db.json")
    if not db:
        db = {
            "version": 3,
            "records": [],
            "pattern_stats": {},
            "theme_stats": {},
            "us_theme_stats": {},      # 米国テーマ→日本波及の的中率
            "pts_reaction_stats": {},  # PTS反応→寄り後継続率
            "total_days": 0,
            "hit_days": 0,
        }
    # バージョン移行
    if db.get("version", 1) < 3:
        db.setdefault("us_theme_stats", {})
        db.setdefault("pts_reaction_stats", {})
        db["version"] = 3
    return db


def save_learning_db(db):
    save("learning_db.json", db)


def tag_market_condition(market):
    tags = []
    dji = next((x for x in market.get("world_indices", []) if x.get("name") == "NYダウ"), None)
    if dji:
        pct = dji.get("percent_raw", 0) or 0
        if pct <= -2:    tags.append("NY大幅下落")
        elif pct <= -1:  tags.append("NY下落")
        elif pct >= 2:   tags.append("NY大幅上昇")
        elif pct >= 1:   tags.append("NY上昇")
        else:            tags.append("NY横ばい")
    sox = next((x for x in market.get("sox", []) if x.get("name") == "SOX"), None)
    if sox:
        pct = sox.get("percent_raw", 0) or 0
        if pct <= -1.5:  tags.append("SOX弱い")
        elif pct >= 1.5: tags.append("SOX強い")
        else:            tags.append("SOX横ばい")
    fx = next((x for x in market.get("forex", []) if x.get("name") == "ドル円"), None)
    if fx:
        chg = fx.get("change_raw", 0) or 0
        if chg >= 0.5:   tags.append("円安")
        elif chg <= -0.5:tags.append("円高")
        else:            tags.append("為替横ばい")
    oil = next((x for x in market.get("oil", []) if x.get("name") == "WTI原油"), None)
    if oil:
        pct = oil.get("percent_raw", 0) or 0
        if pct >= 2:     tags.append("原油高騰")
        elif pct <= -2:  tags.append("原油急落")
    return tags


def find_similar_patterns(db, current_tags, limit=5):
    if not db["records"]:
        return []
    scored = []
    for rec in db["records"]:
        past_tags = set(rec.get("market_tags", []))
        current_set = set(current_tags)
        if not past_tags:
            continue
        overlap = len(past_tags & current_set)
        score = overlap / max(len(past_tags | current_set), 1)
        if score > 0:
            scored.append((score, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def build_learning_context(market):
    db = load_learning_db()
    current_tags = tag_market_condition(market)

    if not db["records"]:
        return "初回実行（学習データなし）", current_tags

    similar = find_similar_patterns(db, current_tags, limit=5)
    theme_stats = db.get("theme_stats", {})
    top_themes = sorted(theme_stats.items(), key=lambda x: x[1].get("hit_rate", 0), reverse=True)[:5]
    bad_themes  = sorted(theme_stats.items(), key=lambda x: x[1].get("hit_rate", 0))[:3]
    recent = db["records"][-10:]
    ban_rules  = [r.get("next_ban_rule", "") for r in recent if r.get("next_ban_rule")][-3:]
    next_focus = [r.get("next_focus", "") for r in recent if r.get("next_focus")][-3:]

    ctx_parts = []
    ctx_parts.append(f"今日の相場タグ: {' / '.join(current_tags) if current_tags else 'なし'}")

    total = db.get("total_days", 0)
    hit   = db.get("hit_days", 0)
    if total > 0:
        ctx_parts.append(f"累計成績: {total}日中{hit}日的中 ({round(hit/total*100,1)}%)")

    if similar:
        ctx_parts.append("【類似パターンの過去結果】")
        for rec in similar[:3]:
            date        = rec.get("date", "")
            tags        = " ".join(rec.get("market_tags", []))
            pred_theme  = rec.get("predicted_top_theme", "不明")
            actual_theme= rec.get("actual_top_theme", "不明")
            hit_str     = "的中" if rec.get("theme_hit") else "外れ"
            lesson      = rec.get("key_lesson", "")
            tomorrow_h  = rec.get("tomorrow_hint", "")
            sign_v      = rec.get("sign_visible_at_600", "")
            sign_h      = rec.get("sign_hidden_at_600", "")
            ctx_parts.append(
                f"  {date}({tags}): 予測={pred_theme} 実際={actual_theme} [{hit_str}]"
                + (f" 教訓={lesson}" if lesson else "")
            )
            if sign_v:
                ctx_parts.append(f"    朝に見えたサイン: {sign_v}")
            if sign_h:
                ctx_parts.append(f"    朝に見えなかったサイン: {sign_h}")
            if tomorrow_h:
                ctx_parts.append(f"    翌朝ヒント: {tomorrow_h}")

    if top_themes:
        ctx_parts.append("【過去に強かったテーマ（的中率順）】")
        for name, stat in top_themes[:3]:
            n  = stat.get("count", 0)
            hr = stat.get("hit_rate", 0)
            ctx_parts.append(f"  {name}: {n}回中{round(hr*n)}回的中({round(hr*100)}%)")

    if bad_themes:
        ctx_parts.append("【外れやすいテーマ（注意）】")
        for name, stat in bad_themes[:2]:
            n  = stat.get("count", 0)
            hr = stat.get("hit_rate", 0)
            ctx_parts.append(f"  {name}: 的中率{round(hr*100)}%({n}回)")

    if ban_rules:
        ctx_parts.append(f"直近の禁止ルール: {' / '.join(ban_rules)}")
    if next_focus:
        ctx_parts.append(f"直近の注目点: {' / '.join(next_focus)}")

    return "\n".join(ctx_parts), current_tags


# ═══════════════════════════════════════════════
# バリデーション
# ═══════════════════════════════════════════════

def validate_600_analysis_json(obj):
    """新スキーマ: themes / watchlist / strategy / avoid_themes / skip_rule / summary"""
    required = ["strategy", "themes", "watchlist", "avoid_themes", "skip_rule", "summary"]
    for k in required:
        if k not in obj:
            raise ValueError(f"必須キー不足: {k}")
    s = obj.get("strategy")
    if not isinstance(s, dict):
        raise ValueError("strategyがdictではありません")
    for k in ["market_regime", "market_regime_label", "entry_style", "entry_style_label", "danger_level"]:
        if k not in s:
            raise ValueError(f"strategy 必須キー不足: {k}")
    if s["market_regime"] not in ["attack", "selective", "avoid"]:
        raise ValueError("market_regimeが規定値外")
    if s["market_regime_label"] not in ["攻めやすい", "選別相場", "見送り寄り"]:
        raise ValueError("market_regime_labelが規定値外")
    if s["entry_style"] not in ["breakout", "pullback", "rebound", "skip"]:
        raise ValueError("entry_styleが規定値外")
    if s["entry_style_label"] not in ["初動ブレイク狙い", "寄り後押し目待ち", "リバ狙い", "見送り"]:
        raise ValueError("entry_style_labelが規定値外")
    if s["danger_level"] not in ["低", "中", "高"]:
        raise ValueError("danger_levelが規定値外")
    return True


# ═══════════════════════════════════════════════
# プロンプト構築
# ═══════════════════════════════════════════════

def build_analysis_prompt_600(today_str, learning_ctx, market, key_news, us_ctx=""):
    """
    6:00 本番画面用プロンプト
    目的: 「今日、何に資金が集まるか」を即断できる情報を生成する
    テーマをA本命/B対抗/C監視の3段階 + 資金集中スコアで評価
    """
    compact_market = {
        "us_index":       market["world_indices"][:1],
        "sox":            market["sox"][:1],
        "nikkei_futures": market["futures"][:1],
        "dollar_yen":     market["forex"][:1],
        "oil":            market["oil"][:1],
        "top_gainers":    market["top_gainers"][:8],
        "top_losers":     market["top_losers"][:8],
        "volume_surge":   market["volume_surge"][:6],
        "recent_themes":  market["themes"][:10],
        "news":           key_news[:10],
        "classified_news":market["classified_news"],
        "pts_gainers":    market.get("pts",{}).get("gainers",[])[:10],
    }
    schema = {
        "strategy": {
            "market_regime": "selective",
            "market_regime_label": "選別相場",
            "entry_style": "pullback",
            "entry_style_label": "寄り後押し目待ち",
            "danger_level": "中",
            "battlefield": "今日の主戦場テーマ名（12字以内の断定ラベル）",
            "one_line": "今日の相場を一言で（40字以内）"
        },
        "conclusion": {
            "battlefield": "主戦場（12字以内）",
            "top_stock": {"code": "2038", "name": "本命銘柄名"},
            "second_stock": {"code": "8035", "name": "次点銘柄名"},
            "watch_codes": ["9104", "1605"],
            "avoid_summary": "回避テーマを15字以内で（例：防衛・材料不明GU）",
            "ban_summary": "今日の禁止事項を30字以内で（例：材料不明GU追撃禁止）"
        },
        "themes": [
            {
                "rank": "A",
                "rank_label": "本命",
                "name": "テーマ名（10字以内）",
                "score": 85,
                "reason": "資金が集まる根拠（60字以内）",
                "continuity": "継続/新規/終息",
                "leader_strength": "強/中/弱",
                "ripple": "広/中/狭",
                "leader_stock": "先導株名",
                "entry_hint": "仕掛けヒント（40字以内）",
                "invalidation_signs": [
                    "テーマ失効サイン1（例: 先導株がVWAP割れ）",
                    "テーマ失効サイン2（例: 9:20時点で連想株が追随しない）"
                ]
            }
        ],
        "watchlist": [
            {
                "role": "本命",
                "name": "銘柄名",
                "code": "1234",
                "theme": "所属テーマ",
                "purpose": "利益狙い/先導確認/連想本線/連想補助/監視のみ/触らない",
                "trigger": "必ず数値条件で: 例「寄り+2%以上かつ出来高前日比150%超」",
                "invalidation": "必ず明確条件で: 例「9:30までに前日比マイ転」または「前日高値割れ」",
                "time_window": "9:00-9:30"
            }
        ],
        "pts_judgments": [
            {
                "code": "2038",
                "name": "銘柄名",
                "pts_reaction": "強い/弱い/薄い/反応なし",
                "judgment": "本命維持/補強/ノイズ/見送り",
                "reason": "判断理由（30字以内）"
            }
        ],
        "avoid_themes": [{"name": "テーマ名", "reason": "避ける理由（40字以内）"}],
        "danger_summary": ["危険株要約1（例: 前日急騰テーマ株 → 寄り天リスク）"],
        "skip_rules": [
            "禁止事項1（例: 材料不明GU追撃禁止）",
            "禁止事項2（例: 防衛株は逆風確認まで見送り）"
        ],
        "top_conclusion": {
            "battlefield": "主戦場ラベル（12字以内）",
            "top_theme": "本命テーマ名",
            "top_stocks": ["本命銘柄コード1", "次点銘柄コード2", "監視銘柄コード3"],
            "avoid_themes_short": ["回避テーマ1", "回避テーマ2"],
            "ban_rules": ["禁止事項1（20字以内）", "禁止事項2（20字以内）"],
            "yesterday_hint": "昨日の学びから今日に活かす一言（なければ空文字）"
        },
        "summary": "今日の資金集中先まとめ（100字以内）",
        "us_theme_signals": [
            {
                "theme_key": "defense",
                "theme_label": "防衛・宇宙",
                "us_strength": "強/中/弱",
                "pts_reaction": "あり/なし/不明",
                "jp_translation": "日本株で注目すべきセクター・銘柄群",
                "priority": "本命/対抗/様子見/見送り"
            }
        ]
    }
    return f"""
You are a Japanese short-term stock trader's AI. Your ONLY job at 6:00 AM:
Identify WHERE capital will concentrate today.

Today is {today_str}.

=== LEARNING CONTEXT (past performance - use this to improve accuracy) ===
{learning_ctx}
=========================================================================

Market data:
{json.dumps(compact_market, ensure_ascii=False)}

{us_ctx}

PRIMARY MISSION:
Answer "Where will capital concentrate today?" with maximum precision.

Theme evaluation criteria:
- score (0-100): capital concentration probability
  90+: near-certain theme day  80+: strong  70+: possible  below 60: weak
- continuity: 継続(was strong yesterday too) / 新規(new catalyst today) / 終息(fading)
- leader_strength: strength of the leading stock in that theme
- ripple: how many related stocks will follow (広=many, 中=some, 狭=few)

Rank themes:
- A本命: THE theme of the day. Capital will concentrate here. Score 75+.
- B対抗: Strong but secondary. Score 55-74.
- C監視: Possible but uncertain. Score 40-54.
- Max 1 A-rank, max 2 B-rank, max 2 C-rank (total max 5 themes)

Watchlist purpose: NOT just to predict. Use them to VERIFY theme is real.
- 先導株: confirms theme is moving
- 連想1軍: confirms theme is spreading
- 連想2軍: confirms theme has depth
- Max 5 stocks total

Rules:
- Return ONLY valid JSON, no markdown
- Use learning context to AVOID repeating past mistakes
- battlefield: 12文字以内の断定ラベル（例: 原油集中, 半導体選別, 個別材料相場）
- theme name: 10文字以内（短く断定的に）
- themes max 5 total (1 A-rank, max 2 B-rank, max 2 C-rank)
- watchlist件数はmarket_regimeに応じて調整すること:
    attack(攻めやすい)   → 先導株3+連想1軍3+連想2軍2+危険株2 = 最大10件
    selective(選別相場)  → 先導株2+連想1軍2+連想2軍1+危険株2 = 最大7件
    avoid(見送り寄り)    → 先導株1+連想1軍1+危険株2 = 最大4件（危険株を目立たせる）
- avoid_themes max 3
- market_regime: attack/selective/avoid
- market_regime_label: 攻めやすい/選別相場/見送り寄り
- entry_style: breakout/pullback/rebound/skip
- entry_style_label: 初動ブレイク狙い/寄り後押し目待ち/リバ狙い/見送り
- danger_level: 低/中/高
- continuity: 継続/新規/終息
- leader_strength: 強/中/弱
- ripple: 広/中/狭
- watchlist role（必須）: 本命/先導確認/連想本線/連想補助/監視のみ/触らない
- watchlist purpose（必須）: 利益狙い/先導確認/連想本線/連想補助/監視のみ/触らない
- trigger は必ず数値条件または時間条件を含めること（例: 寄り+2%以上、9:30までに〇〇）
- invalidation は必ず明確な条件を含めること（例: 前日比マイ転、VWAP割れ）
- invalidation_signs: テーマ失効サインを2〜3件、具体的に記述
- skip_rules は配列で最大3件（各30字以内）
- danger_summary は配列で要約（各20字以内）
- pts_judgments: PTSデータがある場合は必ず本命維持/補強/ノイズ/見送りで判定
- conclusion は必ず埋めること（top_stock/second_stock/avoid_summary/ban_summary）
- 米国テーマ連動分析がある場合は必ず参照すること:
    1. 米国で強いテーマ → 日本株での波及可能性を評価
    2. PTSで先行反応している銘柄 → 先導株候補として優先
    3. 米国強い × PTS反応あり → 本命テーマの根拠として活用
    4. 米国強い × PTS反応なし → 見送り候補または翌日以降に様子見
- us_theme_signals: 米国起点で日本株に波及が見込まれるテーマ（最大3件）をJSONに追加

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_905(today_str, morning_data, market, news):
    """
    9:10 寄り後記録 + 次の売買行動判断
    目的: 朝仮説の検証 + 後場・残りの前場への行動方針を即断
    """
    morning_themes = [t.get("name","") for t in arr(morning_data.get("themes",[]))]
    morning_bf     = morning_data.get("strategy",{}).get("battlefield","")
    morning_top    = morning_data.get("top_conclusion", {})

    compact = {
        "morning_battlefield": morning_bf,
        "morning_top_theme":   morning_themes[:3],
        "morning_ban_rules":   morning_top.get("ban_rules",[]),
        "actual_gainers":      market["top_gainers"][:10],
        "actual_losers":       market["top_losers"][:5],
        "volume_surge":        market["volume_surge"][:6],
        "actual_themes":       market["themes"][:8],
        "news":                news[:6],
    }
    schema = {
        "verdict": "朝仮説維持/半分修正/全面撤回",
        "verdict_reason": "判定根拠（40字以内）",
        "actual_battlefield": "実際の主戦場テーマ名",
        "capital_flow": "資金の流れ（40字以内）",
        "theme_status": [
            {
                "name": "テーマ名",
                "status": "継続/弱まり/終息/新規",
                "pm_action": "継続狙い/押し目待ち/見送り/撤退"
            }
        ],
        "next_action": {
            "primary": "今すぐ最優先でやること（30字以内）",
            "secondary": "次点でやること（30字以内）",
            "stop": "今すぐやめること（30字以内）"
        },
        "watch_update": [
            {
                "code": "銘柄コード",
                "name": "銘柄名",
                "pts_verdict": "本命維持/補強/ノイズ/見送り",
                "reason": "理由（30字以内）"
            }
        ],
        "hint_for_afternoon": "後場への引き継ぎ（40字以内）",
        "hint_for_tomorrow": "明日の朝6:00へのヒント（40字以内）"
    }
    return f"""
You are a Japanese day trader's AI assistant. It is 9:10 AM - markets just opened.
Your job: Verify morning hypothesis and give IMMEDIATE next action.

Today is {today_str}.

Morning prediction vs actual market:
{json.dumps(compact, ensure_ascii=False)}

CRITICAL: Give a clear verdict first, then next actions.
- verdict: 朝仮説維持 / 半分修正 / 全面撤回
- next_action must be specific and actionable RIGHT NOW
- pm_action per theme: 継続狙い/押し目待ち/見送り/撤退
- pts_verdict per watchlist stock: 本命維持/補強/ノイズ/見送り

Rules:
- Return ONLY valid JSON
- Be decisive. No ambiguous language.
- next_action.primary must be actionable in the next 5 minutes

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()

def build_analysis_prompt_1200(today_str, morning_data, open_data, market, news):
    """
    12:00 前場答え合わせ + 後場の売買行動方針
    目的: 前場を総括し、後場の具体的な行動を即断できる形で出力
    """
    morning_themes = [t.get("name","") for t in arr(morning_data.get("themes",[]))]
    opening_verdict = (open_data or {}).get("verdict","")
    compact = {
        "morning_battlefield":  morning_data.get("strategy",{}).get("battlefield",""),
        "morning_themes":       morning_themes[:3],
        "opening_verdict":      opening_verdict,
        "am_top_gainers":       market["top_gainers"][:10],
        "am_top_losers":        market["top_losers"][:5],
        "am_themes":            market["themes"][:10],
        "news":                 news[:6],
    }
    schema = {
        "log": {
            "am_winner_theme":       "前場で強かったテーマ",
            "am_loser_theme":        "前場で弱かったテーマ",
            "new_theme_emerged":     "新規浮上テーマ（なければ空文字）",
            "hypothesis_correction": "朝仮説の修正点（60字）",
            "capital_flow_am":       "前場の資金フロー特徴（60字）",
            "sign_that_was_visible": "朝に見抜けたはずのサイン（40字）",
            "sign_that_was_hidden":  "朝に見えなかったサイン（40字）",
            "hint_for_tomorrow_600": "明日朝6:00最重要ヒント（60字）"
        },
        "pm_plan": {
            "pm_regime": "attack/selective/avoid",
            "pm_regime_label": "後場は攻めやすい/選別相場/見送り",
            "summary": "後場の方針一言（40字以内）"
        },
        "theme_pm_verdict": [
            {
                "name": "テーマ名",
                "verdict": "継続狙い/押し目待ち/前場限り/見送り",
                "reason": "判定根拠（30字以内）"
            }
        ],
        "pm_watchlist": [
            {
                "bucket": "本命/先導確認/連想本線/連想補助/監視のみ/触らない",
                "name": "銘柄名",
                "code": "1234",
                "theme": "所属テーマ",
                "reason": "後場注目理由（40字以内）",
                "trigger": "後場仕掛け条件（数値条件必須）",
                "invalidation": "失効条件（明確条件必須）",
                "time_window": "12:30-14:00"
            }
        ],
        "do_not_do_pm": ["後場でやらないこと（20字以内）"],
        "summary": "後場方針まとめ（80字以内）"
    }
    return f"""
You are a Japanese day trader's AI. It is noon. Morning session just ended.
Your job: Review morning and give CLEAR afternoon trading plan.

Today is {today_str}.

Morning vs actual data:
{json.dumps(compact, ensure_ascii=False)}

CRITICAL OUTPUT REQUIREMENTS:
- theme_pm_verdict: for EACH theme from morning, give verdict: 継続狙い/押し目待ち/前場限り/見送り
- pm_watchlist: concrete stocks for afternoon with numeric trigger conditions
- pm_regime: attack/selective/avoid based on afternoon outlook
- do_not_do_pm: max 3 items, 20 chars each, absolute prohibitions

Rules:
- Return ONLY valid JSON
- pm_watchlist trigger MUST include numeric condition (e.g. 12:45以降+1%維持で参戦)
- pm_watchlist invalidation MUST be specific (e.g. VWAP割れ, 13:00時点でマイ転)
- pm_watchlist件数: attack→最大8件, selective→最大5件, avoid→最大2件

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()

def build_analysis_prompt_1535(today_str, pred_600, pred_905, pred_1200, market, news):
    """
    15:35 翌朝6:00への教師データ生成プロンプト
    目的: 一日の完全な答え合わせ + 翌朝6:00が使える構造化データを作る
    感想ではなく、明日の精度向上に直結する情報を残す
    """
    morning_themes    = [t.get("name","") for t in arr(pred_600.get("themes", pred_600.get("priority_themes",[])))]
    morning_watchlist = [w.get("name","") for w in arr(pred_600.get("watchlist",[]))]
    log_905  = pred_905.get("log",{})  if pred_905  else {}
    log_1200 = pred_1200.get("log",{}) if pred_1200 else {}
    actual_gainers = [f"{g.get('name','')}({g.get('change','')})" for g in market["top_gainers"][:12]]
    actual_losers  = [f"{l.get('name','')}({l.get('change','')})" for l in market["top_losers"][:6]]

    compact = {
        "morning_prediction": {
            "battlefield":  pred_600.get("strategy",{}).get("battlefield",""),
            "one_line":     pred_600.get("strategy",{}).get("one_line",""),
            "themes":       morning_themes,
            "watchlist":    morning_watchlist,
            "danger_level": pred_600.get("strategy",{}).get("danger_level",""),
        },
        "log_905":  log_905,
        "log_1200": log_1200,
        "actual_results": {
            "top_gainers":    actual_gainers,
            "top_losers":     actual_losers,
            "active_themes":  market["themes"][:10],
            "volume_leaders": [f"{v.get('name','')}({v.get('volume','')})" for v in market["volume_surge"][:8]],
        },
        "news": news[:8],
    }

    schema = {
        "review": {
            # 予測精度の検証
            "theme_hit":              True,
            "predicted_top_theme":    "朝に予測した本命テーマ名",
            "actual_top_theme":       "実際に一番資金が集まったテーマ",
            "theme_match_reason":     "的中/外れの理由（80字）",

            # 朝に見抜けたか/見抜けなかったか
            "sign_visible_at_600":    "朝6:00の時点で見抜けたはずのサイン（60字）",
            "sign_hidden_at_600":     "朝6:00では見えなかったサイン（60字）",
            "fake_theme":             "ダマシだったテーマ（なければ空文字）",
            "fake_reason":            "ダマシの理由（40字）",

            # 先導株の特徴
            "leader_stock_name":      "本日の真の先導株",
            "leader_characteristics": "先導株の特徴（50字）",

            # 次回の6:00に活かすデータ
            "next_ban_rule":          "次回の禁止ルール（具体的に60字）",
            "next_focus":             "明日の朝に注目すべき視点（60字）",
            "key_lesson":             "今日の最重要学習（一文で50字以内）",
            "pattern_tags":           ["相場タグ例:NY下落","円安","原油高"],

            # スコア
            "theme_score":    0,
            "execution_score":0,
            "skip_score":     0,
            "overall_score":  0,
        },
        "tomorrow_hint": "明日の朝6:00分析で最優先で考慮すべきこと（100字以内）",
        "summary": "120字以内"
    }

    return f"""
You are generating structured teaching data for tomorrow's 6:00 AM prediction.
Today is {today_str} 15:35.

Your job: Create a complete answer sheet for today that maximizes tomorrow's prediction accuracy.
This is NOT a summary for humans. This is training data for an AI prediction system.

Data:
{json.dumps(compact, ensure_ascii=False)}

Critical tasks:
1. Was the morning battlefield prediction correct? (theme_hit: true/false)
2. What signs WERE visible at 6:00 that pointed to the correct theme?
3. What signs were NOT visible at 6:00 that would have helped?
4. What was the fake/trap theme today?
5. What is the ONE most important thing to check tomorrow morning? (tomorrow_hint)

Rules:
- Return ONLY valid JSON
- Be brutally honest - wrong predictions help learning more than vague answers
- key_lesson must be usable TOMORROW, not general advice
- tomorrow_hint is the single most important output of this analysis
- pattern_tags: short tags describing today's market condition
- scores: 0-100 integers

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


# ═══════════════════════════════════════════════
# 学習DBの更新
# ═══════════════════════════════════════════════

def update_learning_db(today, review, market_tags, market):
    db = load_learning_db()

    record = {
        "date":                  today.isoformat(),
        "market_tags":           market_tags,
        # 予測精度
        "predicted_top_theme":   review.get("predicted_top_theme", ""),
        "actual_top_theme":      review.get("actual_top_theme", ""),
        "theme_hit":             review.get("theme_hit", False),
        "theme_match_reason":    review.get("theme_match_reason", ""),
        # 朝に見えた/見えなかったサイン
        "sign_visible_at_600":   review.get("sign_visible_at_600", ""),
        "sign_hidden_at_600":    review.get("sign_hidden_at_600", ""),
        "fake_theme":            review.get("fake_theme", ""),
        "fake_reason":           review.get("fake_reason", ""),
        # 先導株
        "leader_stock_name":     review.get("leader_stock_name", ""),
        "leader_characteristics":review.get("leader_characteristics", ""),
        # 次回への学習
        "next_ban_rule":         review.get("next_ban_rule", ""),
        "next_focus":            review.get("next_focus", ""),
        "key_lesson":            review.get("key_lesson", ""),
        "tomorrow_hint":         review.get("tomorrow_hint", ""),
        # スコア
        "theme_score":           review.get("theme_score", 0),
        "execution_score":       review.get("execution_score", 0),
        "skip_score":            review.get("skip_score", 0),
        "overall_score":         review.get("overall_score", 0),
        "market_snapshot": {
            "nikkei_change": next((x.get("percent","") for x in market.get("indices",[]) if x.get("name")=="日経平均"), ""),
            "dji_change":    next((x.get("percent","") for x in market.get("world_indices",[]) if x.get("name")=="NYダウ"), ""),
            "sox_change":    next((x.get("percent","") for x in market.get("sox",[]) if x.get("name")=="SOX"), ""),
            "fx_change":     next((x.get("percent","") for x in market.get("forex",[]) if x.get("name")=="ドル円"), ""),
        },
    }

    db["records"] = [r for r in db["records"] if r.get("date") != today.isoformat()]
    db["records"].append(record)
    db["total_days"] = len(db["records"])
    db["hit_days"]   = sum(1 for r in db["records"] if r.get("theme_hit"))

    if not db.get("theme_stats"):
        db["theme_stats"] = {}
    pred_theme = review.get("predicted_top_theme", "")
    if pred_theme:
        if pred_theme not in db["theme_stats"]:
            db["theme_stats"][pred_theme] = {"count": 0, "hits": 0, "hit_rate": 0.0}
        db["theme_stats"][pred_theme]["count"] += 1
        if review.get("theme_hit"):
            db["theme_stats"][pred_theme]["hits"] += 1
        stat = db["theme_stats"][pred_theme]
        stat["hit_rate"] = stat["hits"] / stat["count"]

    if not db.get("pattern_stats"):
        db["pattern_stats"] = {}
    for tag in market_tags:
        if tag not in db["pattern_stats"]:
            db["pattern_stats"][tag] = {"count": 0, "hits": 0, "hit_rate": 0.0}
        db["pattern_stats"][tag]["count"] += 1
        if review.get("theme_hit"):
            db["pattern_stats"][tag]["hits"] += 1
        stat = db["pattern_stats"][tag]
        stat["hit_rate"] = stat["hits"] / stat["count"]

    # ── us_theme_stats 更新 ──
    db.setdefault("us_theme_stats", {})
    db.setdefault("pts_reaction_stats", {})
    # run_600で保存したus_theme_signalsを1535レビュー時に取得して更新
    pred_600 = load("latest_600.json") or {}
    us_signals = pred_600.get("us_theme_signals", [])
    for sig in us_signals:
        key = sig.get("theme_key", "")
        if not key: continue
        uts = db["us_theme_stats"].setdefault(key, {
            "label": "", "total": 0, "jp_hit": 0, "pts_correct": 0, "hit_rate": 0.0
        })
        uts["label"] = sig.get("theme_label", key)
        uts["total"] += 1
        # 予測優先度が本命/対抗でテーマ的中していれば jp_hit
        priority = sig.get("priority", "")
        if priority in ("本命", "対抗") and review.get("theme_hit"):
            uts["jp_hit"] += 1
        # PTS反応あり予測 → 実際にテーマ的中で pts_correct
        if sig.get("pts_reaction") == "あり" and review.get("theme_hit"):
            uts["pts_correct"] += 1
        uts["hit_rate"] = round(uts["jp_hit"] / uts["total"], 3) if uts["total"] else 0.0

    save_learning_db(db)
    total = db["total_days"]
    hit   = db["hit_days"]
    rate  = round(hit / total * 100, 1) if total > 0 else 0
    print(f"学習DB更新: {total}日分 / テーマ的中率 {rate}% ({hit}/{total})")
    return db


# ═══════════════════════════════════════════════
# セッション実行
# ═══════════════════════════════════════════════

def run_600(today):
    now = datetime.now(JST).strftime("%H:%M")
    print("[6:00] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    key_news = (market["news"] + nhk + reuters)[:12]
    learning_ctx, market_tags = build_learning_context(market)
    print(f"[6:00] 相場タグ: {market_tags}")
    today_str = today.strftime("%Y年%m月%d日")

    # 米国テーマ + PTS コンテキスト生成（トークン節約のため上位5件に絞る）
    us_themes_top = dict(sorted(
        market.get("us_themes", {}).items(),
        key=lambda x: x[1].get("avg_pct", 0), reverse=True
    )[:5])
    us_ctx_text, us_meta = build_us_context(
        {"themes": us_themes_top, "macro": market.get("us_macro", {})},
        market.get("pts", {})
    )
    print(f"[6:00] 米国テーマ: {len(market.get('us_themes',{}))}件, PTS: {len(market.get('pts',{}).get('gainers',[]))}件")

    try:
        print("[6:00] Claude分析中（学習データ参照 + 米国テーマ連動）...")
        prompt = build_analysis_prompt_600(today_str, learning_ctx, market, key_news, us_ctx_text)
        raw    = call_claude(prompt, max_tokens=3000)
        save_text("claude_raw_600.txt", raw)
        try:
            parsed = parse_json(raw)
        except Exception as je:
            print(f"[6:00] JSON解析失敗（リカバリ試行）: {je}")
            parsed = {}
        if parsed:
            try:
                validate_600_analysis_json(parsed)
            except Exception as ve:
                print(f"[6:00] バリデーション警告（続行）: {ve}")
        result = {
            "date":          today.isoformat(),
            "session":       "600",
            "generated_at":  now,
            "data_sources":  data_sources,
            "market_tags":   market_tags,
            "conclusion":       parsed.get("conclusion", {}),
            "pts_judgments":    parsed.get("pts_judgments", []),
            "skip_rules":       parsed.get("skip_rules", []),
            "danger_summary":   parsed.get("danger_summary", []),
            "us_theme_signals": parsed.get("us_theme_signals", []),
            "top_conclusion": parsed.get("top_conclusion", {}),
            "pts_judgments":  parsed.get("pts_judgments", []),
            "pts_gainers":    market.get("pts",{}).get("gainers",[])[:10],
            "us_themes_raw":  [
                {"key": k, "label": v["label"], "avg_pct": v["avg_pct"], "strength": v["strength"]}
                for k, v in market.get("us_themes",{}).items()
            ],
            "market_data": {
                "indices":        market["indices"][:6],
                "world_indices":  market["world_indices"][:6],
                "forex":          market["forex"][:4],
                "futures":        market["futures"][:4],
                "sox":            market["sox"][:2],
                "oil":            market["oil"][:2],
                "search_results": market["search_results"][:10],
                "key_news":       key_news[:10],
                "classified_news":market["classified_news"],
            },
            "strategy":      parsed["strategy"],
            "themes":         parsed.get("themes", [])[:5],
            "watchlist":      parsed.get("watchlist", [])[:10],
            "avoid_themes":   parsed.get("avoid_themes", [])[:3],
            "skip_rule":      parsed.get("skip_rule", ""),
            "summary":        parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[6:00] Claude分析失敗: {e}")
        save_text("analysis_error_600.txt", str(e))
        result = {
            "date":          today.isoformat(),
            "session":       "600",
            "generated_at":  now,
            "data_sources":  data_sources,
            "market_tags":   market_tags,
            "conclusion":       parsed.get("conclusion", {}),
            "pts_judgments":    parsed.get("pts_judgments", []),
            "skip_rules":       parsed.get("skip_rules", []),
            "danger_summary":   parsed.get("danger_summary", []),
            "us_theme_signals": parsed.get("us_theme_signals", []),
            "top_conclusion": parsed.get("top_conclusion", {}),
            "pts_judgments":  parsed.get("pts_judgments", []),
            "pts_gainers":    market.get("pts",{}).get("gainers",[])[:10],
            "us_themes_raw":  [
                {"key": k, "label": v["label"], "avg_pct": v["avg_pct"], "strength": v["strength"]}
                for k, v in market.get("us_themes",{}).items()
            ],
            "market_data": {
                "indices": market["indices"][:6], "world_indices": market["world_indices"][:6],
                "forex": market["forex"][:4], "futures": market["futures"][:4],
                "sox": market["sox"][:2], "oil": market["oil"][:2],
                "search_results": market["search_results"][:10],
                "key_news": key_news[:10], "classified_news": market["classified_news"],
            },
            "strategy": {
                "market_regime": "avoid", "market_regime_label": "見送り寄り",
                "entry_style": "skip", "entry_style_label": "見送り",
                "danger_level": "高",
                "battlefield": "分析失敗", "one_line": "AI分析に失敗。今日は見送り優先。"
            },
            "themes": [], "watchlist": [], "avoid_themes": [],
            "skip_rule": "AI分析失敗日は無理に入らない",
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
    save("latest_600.json", result)
    return result


def run_905(today):
    morning = load("latest_600.json")
    if not morning:
        raise FileNotFoundError("latest_600.json なし")
    print("[9:10] データ収集中...")
    market = fetch_all_market_data()
    nhk    = fetch_nhk_news()
    data_sources = build_data_sources_summary(market, nhk, [])
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    try:
        prompt = build_analysis_prompt_905(today_str, morning, market, market["news"] + nhk)
        raw    = call_claude(prompt, max_tokens=1600)
        save_text("claude_raw_905.txt", raw)
        parsed = parse_json(raw)
        result = {
            "date":             today.isoformat(),
            "session":          "905",
            "generated_at":     now,
            "data_sources":     data_sources,
            "log":          parsed.get("log", {}),
            "pm_watchlist": parsed.get("pm_watchlist", [])[:8],
            "summary":      parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[9:10] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "905", "generated_at": now,
            "data_sources": data_sources,
            "log": {
                "battlefield_correct": False, "predicted_battlefield": "",
                "actual_battlefield": "", "theme_accuracy": "外れ",
                "what_moved_as_predicted": "", "what_was_different": "",
                "capital_flow_note": "", "hint_for_afternoon": "",
                "hint_for_tomorrow": f"AI分析失敗のため不明: {e}"
            },
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_905.txt", str(e))
    save("latest_905.json", result)
    return result


def run_1200(today):
    morning = load("latest_600.json")
    opening = load("latest_905.json") or {}
    if not morning:
        raise FileNotFoundError("latest_600.json なし")
    print("[12:00] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    try:
        prompt = build_analysis_prompt_1200(today_str, morning, opening, market,
                                            market["news"] + nhk + reuters)
        raw    = call_claude(prompt, max_tokens=1800)
        save_text("claude_raw_1200.txt", raw)
        parsed = parse_json(raw)
        result = {
            "date": today.isoformat(), "session": "1200", "generated_at": now,
            "data_sources": data_sources,
            "log":          parsed.get("log", {}),
            "pm_watchlist": parsed.get("pm_watchlist", [])[:8],
            "summary":      parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[12:00] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "1200", "generated_at": now,
            "data_sources": data_sources,
            "log": {
                "am_winner_theme": "", "am_loser_theme": "", "new_theme_emerged": "",
                "hypothesis_correction": f"AI分析失敗: {e}",
                "capital_flow_am": "", "sign_that_was_visible": "",
                "sign_that_was_hidden": "", "hint_for_tomorrow_600": ""
            },
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_1200.txt", str(e))
    save("latest_1200.json", result)
    return result


def run_1535(today):
    pred_600  = load("latest_600.json")
    pred_905  = load("latest_905.json")
    pred_1200 = load("latest_1200.json")
    if not pred_600:
        raise FileNotFoundError("latest_600.json なし")
    print("[15:35] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    market_tags = pred_600.get("market_tags", tag_market_condition(market))

    try:
        prompt = build_analysis_prompt_1535(today_str, pred_600, pred_905, pred_1200,
                                            market, market["news"] + nhk + reuters)
        raw    = call_claude(prompt, max_tokens=2400)
        save_text("claude_raw_1535.txt", raw)
        parsed = parse_json(raw)
        review = parsed["review"]
        result = {
            "date": today.isoformat(), "session": "1535", "generated_at": now,
            "data_sources": data_sources,
            "review":        review,
            "tomorrow_hint": parsed.get("tomorrow_hint", ""),
            "summary":       parsed.get("summary", ""),
        }
        # 学習DB更新（最重要）
        update_learning_db(today, review, market_tags, market)

    except Exception as e:
        print(f"[15:35] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "1535", "generated_at": now,
            "data_sources": data_sources,
            "review": {}, "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_1535.txt", str(e))
    save("latest_1535.json", result)
    return result


# ═══════════════════════════════════════════════
# メイン
# ═══════════════════════════════════════════════

def main():
    session = os.environ.get("SESSION", "")
    now_jst = datetime.now(JST)
    today   = now_jst.date()

    if not session:
        h = now_jst.hour
        if h < 7:    session = "600"
        elif h < 10: session = "905"
        elif h < 13: session = "1200"
        else:        session = "1535"

    # 手動実行（FORCE=1）の場合は営業日チェックをスキップ
    force = os.environ.get("FORCE", "0") == "1"
    print(f"=== SESSION={session} / {today} / force={force} ===")

    if not force and not is_trading_day(today):
        print("本日は取引日ではありません。終了します。")
        sys.exit(0)

    if session == "600":
        run_600(today)
    elif session == "905":
        run_905(today)
    elif session == "1200":
        run_1200(today)
    elif session == "1535":
        run_1535(today)
    else:
        print(f"不明なSESSION: {session}")
        sys.exit(1)


if __name__ == "__main__":
    main()
