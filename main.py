"""
株式AI自動分析
SESSION=600 / 905 / 1535
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
    cleaned = re.sub(r"```json|```", "", raw).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSONオブジェクトの外形が見つかりません")

    body = cleaned[start:end + 1]
    body = body.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    body = re.sub(r",\s*}", "}", body)
    body = re.sub(r",\s*]", "]", body)

    return json.loads(body)


def call_claude(prompt):
    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2200,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in res.content if block.type == "text")


def load_learning_ctx():
    logs = load("accuracy_log.json")
    if not logs:
        return "初回実行"

    recent = logs[-10:]
    avg = sum(x.get("accuracy_score", 0) for x in recent) / max(len(recent), 1)
    strong = [x.get("strongest_theme", "") for x in recent if x.get("strongest_theme")]
    weak = [x.get("weakest_theme", "") for x in recent if x.get("weakest_theme")]
    hints = []
    for x in recent[-3:]:
        hints.extend(x.get("improvement_hints", []))

    strong = list(dict.fromkeys(strong))[:3]
    weak = list(dict.fromkeys(weak))[:3]
    hints = hints[-3:]

    return (
        f"過去{len(recent)}日平均精度:{avg:.0f}点 "
        f"強かったテーマ:{' / '.join(strong) if strong else 'なし'} "
        f"弱かったテーマ:{' / '.join(weak) if weak else 'なし'} "
        f"改善ヒント:{' / '.join(hints) if hints else 'なし'}"
    )


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

    ng_patterns = [
        r"すでに会員の方はログイン",
        r"プレミアム会員限定",
        r"ログイン",
        r"銘柄検索",
        r"メニュー",
        r"PC版を表示",
        r"人気テーマ",
        r"人気株",
        r"ベスト30を見る",
        r"お知らせ",
        r"会員限定",
        r"^\d+$",
        r"^(TOP|決算|開示|人気|コラム)$",
        r"日経平均",
        r"ドル円",
        r"NYダウ",
        r"上海総合",
        r"日経先物",
        r"日経225先物",
        r"^\s*PR\s*$",
    ]

    for p in ng_patterns:
        if re.search(p, text):
            return False
    return True


def yahoo_quote(symbol, label):
    """
    Yahoo Finance chart API から厳密取得
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1m", "range": "1d"}

    headers = dict(HEADERS)
    headers["Referer"] = f"https://finance.yahoo.com/quote/{symbol}"

    try:
        r = safe_get(url, timeout=20, headers=headers, params=params)
        data = r.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")
        market_time = meta.get("regularMarketTime")

        if price is None or prev is None or market_time is None:
            return None

        change = round(float(price) - float(prev), 2)
        sign = "+" if change >= 0 else ""
        jst_time = datetime.fromtimestamp(int(market_time), JST).strftime("%H:%M")

        return {
            "name": label,
            "value": f"{float(price):,.2f}",
            "change": f"{sign}{change:,.2f}",
            "percent": "",
            "time": jst_time,
            "source": f"Yahoo Finance:{symbol}",
        }
    except Exception as e:
        print(f"yahoo quote error {symbol}: {e}")
        return None


def fetch_strict_market_quotes():
    quotes = {
        "indices": [],
        "world_indices": [],
        "forex": [],
        "futures": [],
        "search_results": [],
    }

    targets = [
        ("indices", "^N225", "日経平均"),
        ("futures", "NIY=F", "日経先物"),
        ("forex", "JPY=X", "ドル円"),
        ("world_indices", "^DJI", "NYダウ"),
    ]

    ok = 0
    for bucket, symbol, label in targets:
        item = yahoo_quote(symbol, label)
        if item:
            quotes[bucket].append(item)
            quotes["search_results"].append(f"Yahoo:{label} 取得成功")
            ok += 1
        else:
            quotes["search_results"].append(f"Yahoo:{label} 未取得")

    print(f"strict quotes success: {ok}/{len(targets)}")
    return quotes


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

        item = {
            "code": code,
            "name": name,
        }

        if mode in ("gainers", "losers"):
            item["price"] = row[idx + 2] if idx + 2 < len(row) else ""
            item["change"] = row[-1] if row else ""
        else:
            item["volume"] = row[-2] if len(row) >= 2 else ""
            item["change"] = row[-1] if row else ""

        parsed.append(item)

    return parsed[:15]


def fetch_kabutan_theme_news():
    result = {
        "themes": [],
        "news": [],
        "top_gainers": [],
        "top_losers": [],
        "volume_surge": [],
    }

    try:
        r = safe_get("https://kabutan.jp/")
        soup = BeautifulSoup(r.text, "html.parser")

        theme_candidates = []
        for a in soup.select("a[href*='theme'], a[href*='/themes/']"):
            t = clean_text(a.get_text(" ", strip=True))
            if 2 <= len(t) <= 30:
                theme_candidates.append(t)
        result["themes"] = list(dict.fromkeys(theme_candidates))[:10]

        news_candidates = []
        for a in soup.select("a[href]"):
            t = clean_text(a.get_text(" ", strip=True))
            if is_valid_news_text(t):
                news_candidates.append(t)
        result["news"] = list(dict.fromkeys(news_candidates))[:12]

    except Exception as e:
        print(f"kabutan home error: {e}")

    pages = [
        ("gainers", "https://kabutan.jp/warning/?mode=2_1"),
        ("losers", "https://kabutan.jp/warning/?mode=2_2"),
        ("volume", "https://kabutan.jp/warning/?mode=25_1"),
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


def fetch_all_market_data():
    strict = fetch_strict_market_quotes()
    kabu = fetch_kabutan_theme_news()

    result = {
        "indices": strict["indices"],
        "world_indices": strict["world_indices"],
        "forex": strict["forex"],
        "futures": strict["futures"],
        "sector": [],
        "top_gainers": kabu["top_gainers"],
        "top_losers": kabu["top_losers"],
        "volume_surge": kabu["volume_surge"],
        "themes": kabu["themes"],
        "news": kabu["news"],
        "search_results": strict["search_results"],
        "source": "Yahoo Finance + kabutan.jp",
    }

    print("=== market summary ===")
    print(f"indices: {len(result['indices'])}")
    print(f"world_indices: {len(result['world_indices'])}")
    print(f"forex: {len(result['forex'])}")
    print(f"futures: {len(result['futures'])}")
    print(f"top_gainers: {len(result['top_gainers'])}")
    print(f"top_losers: {len(result['top_losers'])}")
    print(f"volume_surge: {len(result['volume_surge'])}")
    print(f"themes: {len(result['themes'])}")
    print(f"news: {len(result['news'])}")

    return result


def build_data_sources_summary(market, nhk, reuters):
    sources = []
    if market.get("indices"):
        sources.append(f"日経平均{len(market['indices'])}件")
    if market.get("futures"):
        sources.append(f"日経先物{len(market['futures'])}件")
    if market.get("world_indices"):
        sources.append(f"NYダウ{len(market['world_indices'])}件")
    if market.get("forex"):
        sources.append(f"ドル円{len(market['forex'])}件")
    if market.get("top_gainers"):
        sources.append(f"値上がり{len(market['top_gainers'])}件")
    if market.get("top_losers"):
        sources.append(f"値下がり{len(market['top_losers'])}件")
    if market.get("volume_surge"):
        sources.append(f"出来高急増{len(market['volume_surge'])}件")
    if market.get("themes"):
        sources.append(f"テーマ{len(market['themes'])}件")
    if market.get("news"):
        sources.append(f"かぶたんニュース{len(market['news'])}件")
    if nhk:
        sources.append(f"NHKニュース{len(nhk)}件")
    if reuters:
        sources.append(f"ロイター{len(reuters)}件")
    return sources


def build_trader_focus(result):
    themes = result.get("themes", []) or []
    summary = result.get("summary", "")
    top_theme_names = [t.get("name", "") for t in themes[:3] if t.get("name")]
    top_stocks = []
    for t in themes[:3]:
        for s in t.get("key_stocks", [])[:2]:
            if s.get("name"):
                top_stocks.append({
                    "name": s.get("name", ""),
                    "code": s.get("code", ""),
                    "reason": s.get("reason", ""),
                })
    top_stocks = top_stocks[:6]

    result["trader_focus"] = {
        "top_themes": top_theme_names,
        "top_stocks": top_stocks,
        "headline": summary[:140] if isinstance(summary, str) else "",
    }
    return result


def validate_600_analysis_json(obj):
    if not isinstance(obj, dict):
        raise ValueError("rootがdictではありません")

    required = ["themes", "big_picture", "summary"]
    for k in required:
        if k not in obj:
            raise ValueError(f"必須キー不足: {k}")

    themes = obj.get("themes")
    if not isinstance(themes, list) or len(themes) == 0:
        raise ValueError("themesが空です")

    for i, t in enumerate(themes[:3]):
        if not isinstance(t, dict):
            raise ValueError(f"theme[{i}]がdictではありません")
        for k in ["rank", "name", "confidence_score", "rationale", "key_stocks", "risk_factors"]:
            if k not in t:
                raise ValueError(f"theme[{i}] 必須キー不足: {k}")

    return True


def build_analysis_prompt_600(today_str, learning_ctx, market, key_news):
    compact_market = {
        "indices": market["indices"][:2],
        "futures": market["futures"][:1],
        "forex": market["forex"][:1],
        "world_indices": market["world_indices"][:1],
        "top_gainers": market["top_gainers"][:8],
        "top_losers": market["top_losers"][:8],
        "volume_surge": market["volume_surge"][:6],
        "themes": market["themes"][:8],
        "key_news": key_news[:8],
    }

    schema = {
        "themes": [
            {
                "rank": 1,
                "name": "テーマ名",
                "confidence_score": 80,
                "rationale": "80字以内",
                "key_stocks": [
                    {"name": "銘柄名", "code": "1234", "reason": "40字以内"},
                    {"name": "銘柄名", "code": "5678", "reason": "40字以内"}
                ],
                "risk_factors": "60字以内"
            }
        ],
        "big_picture": "120字以内",
        "summary": "160字以内"
    }

    return f"""
You are a Japanese stock market analyst.
Today is {today_str}.

Learning context:
{learning_ctx}

Market data:
{json.dumps(compact_market, ensure_ascii=False)}

Task:
- Predict today's strongest 3 themes in Japanese equities.
- Use only the provided market data.
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include explanation outside JSON.
- Keep strings short.
- themes must contain exactly 3 items.
- key_stocks must contain at most 2 items per theme.
- If unsure, still output best-effort valid JSON.

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def run_claude_analysis_600(today, now, market, data_sources, key_news):
    learning_ctx = load_learning_ctx()
    today_str = today.strftime("%Y年%m月%d日")
    prompt = build_analysis_prompt_600(today_str, learning_ctx, market, key_news)

    raw = call_claude(prompt)
    save_text("claude_raw_600.txt", raw)

    parsed = parse_json(raw)
    validate_600_analysis_json(parsed)

    result = {
        "date": today.isoformat(),
        "session": "600",
        "generated_at": now,
        "data_sources": data_sources,
        "market_data": {
            "indices": market["indices"][:6],
            "world_indices": market["world_indices"][:6],
            "forex": market["forex"][:4],
            "futures": market["futures"][:4],
            "search_results": market["search_results"][:10],
            "key_news": key_news[:10],
        },
        "themes": parsed["themes"][:3],
        "big_picture": parsed["big_picture"],
        "summary": parsed["summary"],
    }

    return build_trader_focus(result)


def build_analysis_failure_600(today, now, market, data_sources, key_news, reason):
    return {
        "date": today.isoformat(),
        "session": "600",
        "generated_at": now,
        "data_sources": data_sources,
        "market_data": {
            "indices": market["indices"][:6],
            "world_indices": market["world_indices"][:6],
            "forex": market["forex"][:4],
            "futures": market["futures"][:4],
            "search_results": market["search_results"][:10],
            "key_news": key_news[:10],
        },
        "themes": [],
        "big_picture": "分析失敗",
        "summary": f"AI分析に失敗しました: {reason}",
        "analysis_status": "failed",
    }


def run_600(today):
    now = datetime.now(JST).strftime("%H:%M")

    print("[6:00] データ収集中...")
    market = fetch_all_market_data()
    nhk = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    key_news = (market["news"] + nhk + reuters)[:10]

    try:
        print("[6:00] Claude分析中...")
        result = run_claude_analysis_600(today, now, market, data_sources, key_news)
    except Exception as e:
        print(f"[6:00] Claude分析失敗: {e}")
        result = build_analysis_failure_600(today, now, market, data_sources, key_news, str(e))
        save_text("analysis_error_600.txt", str(e))

    save("latest_600.json", result)
    return result


def run_905(today):
    pred = load("latest_600.json")
    if not pred:
        raise FileNotFoundError("latest_600.json なし")

    print("[9:05] データ収集中...")
    market = fetch_all_market_data()
    nhk = fetch_nhk_news()
    data_sources = build_data_sources_summary(market, nhk, [])

    theme_names = [t.get("name", "") for t in pred.get("themes", [])]
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON.
No explanation. No markdown. Just JSON.
Today is {ymd} 9:05. Analyze opening market vs 6:00 prediction.

6:00 Predicted Themes: {json.dumps(theme_names, ensure_ascii=False)}
Top Gainers: {json.dumps(market['top_gainers'][:12], ensure_ascii=False)}
Volume Surge: {json.dumps(market['volume_surge'][:8], ensure_ascii=False)}
Hot Themes: {json.dumps(market['themes'][:8], ensure_ascii=False)}
News: {json.dumps((market['news'] + nhk)[:8], ensure_ascii=False)}

Return this JSON:
{{
  "opening": {{
    "nikkei_open": "値",
    "nikkei_change": "変化率",
    "market_tone": "強い/弱い/中立",
    "dominant_theme": "主役テーマ"
  }},
  "actual_flow": [
    {{"theme": "テーマ名", "evidence": "80字以内", "strength": "high"}}
  ],
  "prediction_gap": [
    {{
      "predicted_theme": "予測テーマ",
      "predicted_score": 80,
      "actual_result": "的中/外れ/部分的中",
      "gap_reason": "60字以内",
      "missed_factor": "40字以内"
    }}
  ],
  "intraday_correction": {{
    "themes_to_watch": ["テーマ1", "テーマ2"],
    "themes_faded": ["テーマ"],
    "correction_hints": ["ヒント1", "ヒント2"]
  }},
  "morning_accuracy_score": 70,
  "summary": "120字以内"
}}
ONLY JSON.
"""

    try:
        raw = call_claude(prompt)
        save_text("claude_raw_905.txt", raw)
        parsed = parse_json(raw)

        result = {
            "date": iso,
            "session": "905",
            "generated_at": now,
            "data_sources": data_sources,
            **parsed,
        }
    except Exception as e:
        print(f"[9:05] Claude分析失敗: {e}")
        result = {
            "date": iso,
            "session": "905",
            "generated_at": now,
            "data_sources": data_sources,
            "opening": {
                "nikkei_open": "",
                "nikkei_change": "",
                "market_tone": "未分析",
                "dominant_theme": "",
            },
            "actual_flow": [],
            "prediction_gap": [],
            "intraday_correction": {
                "themes_to_watch": [],
                "themes_faded": [],
                "correction_hints": [],
            },
            "morning_accuracy_score": 0,
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_905.txt", str(e))

    save("latest_905.json", result)
    return result


def run_1535(today):
    pred_600 = load("latest_600.json")
    pred_905 = load("latest_905.json")

    if not pred_600:
        raise FileNotFoundError("latest_600.json なし")

    print("[15:35] データ収集中...")
    market = fetch_all_market_data()
    nhk = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)

    theme_names = [t.get("name", "") for t in pred_600.get("themes", [])]
    gap_905 = pred_905.get("prediction_gap", []) if pred_905 else []
    hints_905 = pred_905.get("intraday_correction", {}).get("correction_hints", []) if pred_905 else []

    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON.
No explanation. No markdown. Just JSON.
Today is {ymd} 15:35. Summarize the full day's market.

6:00 Predicted Themes: {json.dumps(theme_names, ensure_ascii=False)}
9:05 Gap Analysis: {json.dumps(gap_905, ensure_ascii=False)}
9:05 Correction Hints: {json.dumps(hints_905, ensure_ascii=False)}
Top Gainers: {json.dumps(market['top_gainers'][:12], ensure_ascii=False)}
Top Losers: {json.dumps(market['top_losers'][:8], ensure_ascii=False)}
Volume Surge: {json.dumps(market['volume_surge'][:8], ensure_ascii=False)}
Hot Themes: {json.dumps(market['themes'][:10], ensure_ascii=False)}
News: {json.dumps((market['news'] + nhk + reuters)[:10], ensure_ascii=False)}

Return this JSON:
{{
  "closing": {{
    "nikkei": "終値と変化率",
    "topix": "終値と変化率",
    "total_assessment": "100字以内"
  }},
  "theme_results": [
    {{"name": "テーマ名", "morning_score": 80, "final_result": "的中/外れ/部分的中", "detail": "60字以内"}}
  ],
  "stock_results": [
    {{"name": "銘柄名", "code": "1234", "close": "終値", "change": "変化率", "comment": "50字以内"}}
  ],
  "news_impact": [
    {{"news": "ニュース", "impact": "60字以内"}}
  ],
  "correction_evaluation": "80字以内",
  "tomorrow_outlook": {{
    "key_events": ["イベント1", "イベント2"],
    "watch_themes": ["テーマ1", "テーマ2"],
    "hint": "80字以内"
  }},
  "final_accuracy_score": 75,
  "strongest_theme": "最も的中したテーマ",
  "weakest_theme": "最も外れたテーマ",
  "learning_points": ["学習1", "学習2", "学習3"],
  "summary": "120字以内"
}}
ONLY JSON.
"""

    try:
        raw = call_claude(prompt)
        save_text("claude_raw_1535.txt", raw)
        parsed = parse_json(raw)

        result = {
            "date": iso,
            "session": "1535",
            "generated_at": now,
            "data_sources": data_sources,
            **parsed,
        }
    except Exception as e:
        print(f"[15:35] Claude分析失敗: {e}")
        result = {
            "date": iso,
            "session": "1535",
            "generated_at": now,
            "data_sources": data_sources,
            "closing": {
                "nikkei": "",
                "topix": "",
                "total_assessment": "未分析",
            },
            "theme_results": [],
            "stock_results": [],
            "news_impact": [],
            "correction_evaluation": "",
            "tomorrow_outlook": {
                "key_events": [],
                "watch_themes": [],
                "hint": "",
            },
            "final_accuracy_score": 0,
            "strongest_theme": "",
            "weakest_theme": "",
            "learning_points": [],
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_1535.txt", str(e))

    save("latest_1535.json", result)

    logs = load("accuracy_log.json") or []
    today_str = today.isoformat()

    entry = {
        "date": today_str,
        "accuracy_score": result.get("final_accuracy_score", 0),
        "strongest_theme": result.get("strongest_theme", ""),
        "weakest_theme": result.get("weakest_theme", ""),
        "improvement_hints": result.get("learning_points", []),
    }

    updated = False
    for i, row in enumerate(logs):
        if row.get("date") == today_str:
            logs[i] = entry
            updated = True
            break
    if not updated:
        logs.append(entry)

    save("accuracy_log.json", logs[-90:])
    return result


if __name__ == "__main__":
    now_jst = datetime.now(JST)
    today = now_jst.date()

    session = os.environ.get("SESSION", "").strip()
    if not session:
        hour = now_jst.hour
        if hour < 7:
            session = "600"
        elif hour < 10:
            session = "905"
        else:
            session = "1535"

    print(f"SESSION={session} date={today}")

    if not is_trading_day(today):
        print("非営業日 — スキップ")
        sys.exit(0)

    if session == "600":
        run_600(today)
    elif session == "905":
        run_905(today)
    elif session == "1535":
        run_1535(today)
    else:
        print(f"不明なSESSION: {session}")
        sys.exit(1)

    print(f"[{session}] 完了")