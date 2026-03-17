"""
株式AI自動分析 v6
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

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

YEAR_END = {(12, 31), (1, 1), (1, 2), (1, 3)}

H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in YEAR_END:
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def call_claude(prompt):
    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in res.content if b.type == "text")


def parse_json(raw):
    clean = re.sub(r"```json|```", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m:
        raise ValueError(f"JSON not found: {raw[:200]}")
    text = m.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {
                "date": datetime.now(JST).date().isoformat(),
                "generated_at": datetime.now(JST).strftime("%H:%M"),
                "summary": "JSON解析エラー",
                "themes": [],
                "market_data": {},
                "data_sources": [],
            }


def save(filename, data):
    p = DATA / filename
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {p}")


def load(filename):
    p = DATA / filename
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_learning_ctx():
    logs = load("accuracy_log.json")
    if not logs:
        return "初回実行"

    recent = logs[-10:]
    avg = sum(r.get("accuracy_score", 0) for r in recent) / len(recent)
    weak = list({r.get("weakest_theme", "") for r in recent if r.get("weakest_theme")})
    strong = list({r.get("strongest_theme", "") for r in recent if r.get("strongest_theme")})
    hints = []
    for r in recent[-3:]:
        hints.extend(r.get("improvement_hints", []))

    return (
        f"過去{len(recent)}日平均精度:{avg:.0f}点 "
        f"的中:{','.join(strong[:2])} "
        f"外れ:{','.join(weak[:2])} "
        f"ヒント:{' / '.join(hints[-2:])}"
    )


def safe_get(url, timeout=15):
    r = requests.get(url, headers=H, timeout=timeout)
    r.raise_for_status()
    return r


def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_table_rows(table, limit=30):
    rows = []
    for tr in table.select("tr"):
        cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.select("th, td")]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
        if len(rows) >= limit:
            break
    return rows


def find_first_code(row):
    for cell in row:
        m = re.fullmatch(r"\d{4}", cell)
        if m:
            return cell
    return ""


def parse_rank_rows(rows, mode="gainers"):
    parsed = []
    for row in rows[1:]:
        if len(row) < 4:
            continue

        code = find_first_code(row)
        if not code:
            continue

        try:
            code_idx = row.index(code)
        except ValueError:
            continue

        name = row[code_idx + 1] if code_idx + 1 < len(row) else ""
        if not name:
            continue

        item = {
            "code": code,
            "name": name,
        }

        if mode in ("gainers", "losers"):
            item["price"] = row[-2] if len(row) >= 2 else ""
            item["change"] = row[-1] if len(row) >= 1 else ""
        elif mode == "volume":
            item["volume"] = row[-2] if len(row) >= 2 else ""
            item["change"] = row[-1] if len(row) >= 1 else ""

        parsed.append(item)

    return parsed[:15]


def fetch_kabutan_news():
    news = []
    urls = [
        "https://kabutan.jp/news/marketnews/",
        "https://kabutan.jp/news/?b=n1",
    ]

    for url in urls:
        try:
            r = safe_get(url)
            soup = BeautifulSoup(r.text, "html.parser")

            for a in soup.select("a[href*='/news/']"):
                text = clean_text(a.get_text(" ", strip=True))
                href = a.get("href", "")
                if "/news/" in href and len(text) >= 12 and text not in news:
                    news.append(text)
                if len(news) >= 12:
                    return news[:12]

            time.sleep(0.5)
        except Exception as e:
            print(f"かぶたんニュース取得エラー {url}: {e}")

    return news[:12]


def fetch_kabutan_ranking():
    result = {
        "top_gainers": [],
        "top_losers": [],
        "volume_surge": [],
    }

    pages = [
        ("gainers", "https://kabutan.jp/stock/ranking/?market=0&updown=1"),
        ("losers", "https://kabutan.jp/stock/ranking/?market=0&updown=2"),
        ("volume", "https://kabutan.jp/stock/ranking/?market=0&info=volume"),
        ("fallback", "https://kabutan.jp/stock/ranking/"),
    ]

    for kind, url in pages:
        try:
            r = safe_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            tables = soup.find_all("table")

            for table in tables:
                rows = extract_table_rows(table, limit=25)
                if len(rows) < 3:
                    continue

                header = " | ".join(rows[0])

                if kind == "gainers" and not result["top_gainers"]:
                    if "コード" in header and "銘柄名" in header:
                        parsed = parse_rank_rows(rows, "gainers")
                        if parsed:
                            result["top_gainers"] = parsed

                elif kind == "losers" and not result["top_losers"]:
                    if "コード" in header and "銘柄名" in header:
                        parsed = parse_rank_rows(rows, "losers")
                        if parsed:
                            result["top_losers"] = parsed

                elif kind == "volume" and not result["volume_surge"]:
                    if "コード" in header and ("出来高" in header or "売買高" in header):
                        parsed = parse_rank_rows(rows, "volume")
                        if parsed:
                            result["volume_surge"] = parsed

                elif kind == "fallback":
                    if not result["top_gainers"] and "コード" in header and "銘柄名" in header:
                        result["top_gainers"] = parse_rank_rows(rows, "gainers")
                    if not result["volume_surge"] and "コード" in header and ("出来高" in header or "売買高" in header):
                        result["volume_surge"] = parse_rank_rows(rows, "volume")

            time.sleep(0.5)
        except Exception as e:
            print(f"ランキング取得エラー {url}: {e}")

    return result


def fetch_kabutan_market():
    result = {
        "indices": [],
        "world_indices": [],
        "forex": [],
        "sector": [],
        "themes": [],
    }

    try:
        r = safe_get("https://kabutan.jp/market/")
        soup = BeautifulSoup(r.text, "html.parser")

        for tr in soup.select("tr"):
            row = [clean_text(x.get_text(" ", strip=True)) for x in tr.select("th, td")]
            row = [x for x in row if x]
            if not row:
                continue

            joined = " | ".join(row)

            if any(k in joined for k in ["日経平均", "TOPIX", "東証グロース", "JPX日経"]):
                result["indices"].append(joined)

            if any(k in joined for k in ["NYダウ", "NASDAQ", "S&P500", "SOX", "DAX", "上海総合"]):
                result["world_indices"].append(joined)

            if any(k in joined for k in ["ドル円", "ユーロ円", "ユーロドル"]):
                result["forex"].append(joined)

            if any(k in joined for k in ["水産・農林", "鉱業", "建設", "電気機器", "銀行業", "輸送用機器", "情報・通信", "不動産業"]):
                result["sector"].append(joined)

        result["indices"] = list(dict.fromkeys(result["indices"]))[:10]
        result["world_indices"] = list(dict.fromkeys(result["world_indices"]))[:10]
        result["forex"] = list(dict.fromkeys(result["forex"]))[:10]
        result["sector"] = list(dict.fromkeys(result["sector"]))[:20]

    except Exception as e:
        print(f"market取得エラー: {e}")

    try:
        r = safe_get("https://kabutan.jp/themes/")
        soup = BeautifulSoup(r.text, "html.parser")

        theme_candidates = []
        for a in soup.select("a[href*='/themes/']"):
            text = clean_text(a.get_text(" ", strip=True))
            if 2 <= len(text) <= 30:
                theme_candidates.append(text)

        result["themes"] = list(dict.fromkeys(theme_candidates))[:20]

    except Exception as e:
        print(f"themes取得エラー: {e}")

    return result


def fetch_kabutan():
    result = {
        "indices": [],
        "world_indices": [],
        "forex": [],
        "sector": [],
        "top_gainers": [],
        "top_losers": [],
        "volume_surge": [],
        "themes": [],
        "news": [],
        "source": "kabutan.jp",
    }

    market_data = fetch_kabutan_market()
    ranking_data = fetch_kabutan_ranking()
    news_data = fetch_kabutan_news()

    result.update(market_data)
    result.update(ranking_data)
    result["news"] = news_data

    print("=== kabutan summary ===")
    print(f"indices: {len(result['indices'])}")
    print(f"world_indices: {len(result['world_indices'])}")
    print(f"forex: {len(result['forex'])}")
    print(f"sector: {len(result['sector'])}")
    print(f"top_gainers: {len(result['top_gainers'])}")
    print(f"top_losers: {len(result['top_losers'])}")
    print(f"volume_surge: {len(result['volume_surge'])}")
    print(f"themes: {len(result['themes'])}")
    print(f"news: {len(result['news'])}")

    return result


def fetch_nhk_news():
    news = []
    try:
        r = safe_get("https://www3.nhk.or.jp/news/catnew.html")
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("ul.content--list li, div.content--header, a"):
            text = clean_text(item.get_text(" ", strip=True))
            if len(text) > 15 and text not in news:
                news.append(text[:120])
            if len(news) >= 10:
                break
        print(f"NHKニュース: {len(news)}件")
    except Exception as e:
        print(f"NHKエラー: {e}")
    return news


def fetch_reuters_news():
    news = []
    urls = [
        "https://jp.reuters.com/world/",
        "https://jp.reuters.com/business/",
        "https://jp.reuters.com/markets/",
    ]
    for url in urls:
        try:
            r = safe_get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            for item in soup.select("a, h2, h3"):
                text = clean_text(item.get_text(" ", strip=True))
                if len(text) > 15 and text not in news:
                    news.append(text[:120])
                if len(news) >= 10:
                    print(f"ロイターニュース: {len(news)}件")
                    return news
            time.sleep(0.5)
        except Exception as e:
            print(f"ロイターエラー {url}: {e}")
    print(f"ロイターニュース: {len(news)}件")
    return news


def build_data_sources_summary(kabutan, nhk, reuters):
    sources = []
    if kabutan.get("indices"):
        sources.append(f"かぶたん国内指数{len(kabutan['indices'])}件")
    if kabutan.get("world_indices"):
        sources.append(f"かぶたん海外指数{len(kabutan['world_indices'])}件")
    if kabutan.get("sector"):
        sources.append(f"業種別騰落{len(kabutan['sector'])}件")
    if kabutan.get("top_gainers"):
        sources.append(f"値上がり{len(kabutan['top_gainers'])}件")
    if kabutan.get("top_losers"):
        sources.append(f"値下がり{len(kabutan['top_losers'])}件")
    if kabutan.get("volume_surge"):
        sources.append(f"出来高急増{len(kabutan['volume_surge'])}件")
    if kabutan.get("themes"):
        sources.append(f"テーマ{len(kabutan['themes'])}件")
    if kabutan.get("news"):
        sources.append(f"かぶたんニュース{len(kabutan['news'])}件")
    if nhk:
        sources.append(f"NHKニュース{len(nhk)}件")
    if reuters:
        sources.append(f"ロイター{len(reuters)}件")
    return sources


def run_600(today):
    ctx = load_learning_ctx()
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    print("[6:00] データ収集中...")
    kabutan = fetch_kabutan()
    nhk = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(kabutan, nhk, reuters)

    key_news = (kabutan["news"] + nhk + reuters)[:8]

    prompt = f"""IMPORTANT: Return ONLY valid JSON.
No explanation. No markdown. Just JSON.
Today is {ymd}. You are a Japanese stock market analyst.
Based on the collected market data, predict today's TSE themes.

Learning data: {ctx}

Return this JSON:
{{
  "date": "{iso}",
  "session": "600",
  "generated_at": "{now}",
  "data_sources": {json.dumps(data_sources, ensure_ascii=False)},
  "market_data": {{
    "indices": {json.dumps(kabutan['indices'][:6], ensure_ascii=False)},
    "world_indices": {json.dumps(kabutan['world_indices'][:6], ensure_ascii=False)},
    "forex": {json.dumps(kabutan['forex'][:4], ensure_ascii=False)},
    "key_news": {json.dumps(key_news, ensure_ascii=False)}
  }},
  "themes": [
    {{
      "rank": 1,
      "name": "テーマ名",
      "confidence_score": 85,
      "rationale": "根拠",
      "data_basis": ["参照したデータ1", "参照したデータ2"],
      "key_stocks": [
        {{"name": "銘柄名", "code": "コード", "reason": "理由"}}
      ],
      "risk_factors": "リスク",
      "us_connection": "米国との連動"
    }}
  ],
  "big_picture": "本日の最重要ファクター",
  "summary": "相場展望"
}}
ONLY JSON, nothing else.
"""

    print("[6:00] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_600.json", result)
    return result


def run_905(today):
    pred = load("latest_600.json")
    if not pred:
        raise FileNotFoundError("latest_600.json なし")

    print("[9:05] データ収集中...")
    kabutan = fetch_kabutan()
    nhk = fetch_nhk_news()
    data_sources = build_data_sources_summary(kabutan, nhk, [])

    theme_names = [t.get("name", "") for t in pred.get("themes", [])]
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON.
No explanation. No markdown. Just JSON.
Today is {ymd} 9:05. Analyze opening market vs 6:00 prediction.

6:00 Predicted Themes: {json.dumps(theme_names, ensure_ascii=False)}
Top Gainers: {json.dumps(kabutan['top_gainers'][:15], ensure_ascii=False)}
Volume Surge: {json.dumps(kabutan['volume_surge'][:10], ensure_ascii=False)}
Sector: {json.dumps(kabutan['sector'][:15], ensure_ascii=False)}
Hot Themes: {json.dumps(kabutan['themes'][:10], ensure_ascii=False)}
News: {json.dumps((kabutan['news'] + nhk)[:10], ensure_ascii=False)}

Return this JSON:
{{
  "date": "{iso}",
  "session": "905",
  "generated_at": "{now}",
  "data_sources": {json.dumps(data_sources, ensure_ascii=False)},
  "opening": {{
    "nikkei_open": "値",
    "nikkei_change": "変化率",
    "market_tone": "強い/弱い/中立",
    "dominant_theme": "主役テーマ"
  }},
  "actual_flow": [
    {{"theme": "テーマ名", "evidence": "根拠", "strength": "high/medium/low"}}
  ],
  "prediction_gap": [
    {{
      "predicted_theme": "予測テーマ",
      "predicted_score": 85,
      "actual_result": "的中/外れ/部分的中",
      "gap_reason": "理由",
      "missed_factor": "見落とし"
    }}
  ],
  "intraday_correction": {{
    "themes_to_watch": ["テーマ1", "テーマ2"],
    "themes_faded": ["テーマ"],
    "correction_hints": ["ヒント1", "ヒント2"]
  }},
  "morning_accuracy_score": 70,
  "summary": "寄り付き総評"
}}
ONLY JSON.
"""

    print("[9:05] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_905.json", result)
    return result


def run_1535(today):
    pred_600 = load("latest_600.json")
    pred_905 = load("latest_905.json")

    if not pred_600:
        raise FileNotFoundError("latest_600.json なし")

    print("[15:35] データ収集中...")
    kabutan = fetch_kabutan()
    nhk = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(kabutan, nhk, reuters)

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
Top Gainers: {json.dumps(kabutan['top_gainers'][:15], ensure_ascii=False)}
Top Losers: {json.dumps(kabutan['top_losers'][:10], ensure_ascii=False)}
Volume Surge: {json.dumps(kabutan['volume_surge'][:10], ensure_ascii=False)}
Sector: {json.dumps(kabutan['sector'][:20], ensure_ascii=False)}
Hot Themes: {json.dumps(kabutan['themes'][:15], ensure_ascii=False)}
News: {json.dumps((kabutan['news'] + nhk + reuters)[:12], ensure_ascii=False)}

Return this JSON:
{{
  "date": "{iso}",
  "session": "1535",
  "generated_at": "{now}",
  "data_sources": {json.dumps(data_sources, ensure_ascii=False)},
  "closing": {{
    "nikkei": "終値と変化率",
    "topix": "終値と変化率",
    "total_assessment": "総評"
  }},
  "theme_results": [
    {{"name": "テーマ名", "morning_score": 85, "final_result": "的中/外れ/部分的中", "detail": "詳細"}}
  ],
  "stock_results": [
    {{"name": "銘柄名", "code": "コード", "close": "終値", "change": "変化率", "comment": "コメント"}}
  ],
  "news_impact": [
    {{"news": "ニュース", "impact": "影響"}}
  ],
  "correction_evaluation": "9:05修正の評価",
  "tomorrow_outlook": {{
    "key_events": ["イベント1", "イベント2"],
    "watch_themes": ["テーマ1", "テーマ2"],
    "hint": "明日のヒント"
  }},
  "final_accuracy_score": 75,
  "strongest_theme": "最も的中したテーマ",
  "weakest_theme": "最も外れたテーマ",
  "learning_points": ["学習1", "学習2", "学習3"],
  "summary": "本日の総括"
}}
ONLY JSON.
"""

    print("[15:35] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_1535.json", result)

    logs = load("accuracy_log.json") or []
    today_str = today.isoformat()

    existing = next((i for i, r in enumerate(logs) if r["date"] == today_str), None)
    entry = {
        "date": today_str,
        "accuracy_score": result.get("final_accuracy_score", 0),
        "strongest_theme": result.get("strongest_theme", ""),
        "weakest_theme": result.get("weakest_theme", ""),
        "improvement_hints": result.get("learning_points", []),
    }

    if existing is not None:
        logs[existing] = entry
    else:
        logs.append(entry)

    save("accuracy_log.json", logs[-90:])
    return result


if __name__ == "__main__":
    now_jst = datetime.now(JST)
    today = now_jst.date()

    session = os.environ.get("SESSION", "").strip()
    if not session:
        hour = now_jst.hour
        session = "600" if hour < 7 else "905" if hour < 10 else "1535"

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
        print(f"不明: {session}")
        sys.exit(1)

    print(f"[{session}] 完了")