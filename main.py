"""
株式AI自動分析 v5
SESSION=600 / 905 / 1535
"""
import os, sys, json, re, time
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
YEAR_END = {(12,31),(1,1),(1,2),(1,3)}

def is_trading_day(d):
    if d.weekday() >= 5: return False
    if (d.month, d.day) in YEAR_END: return False
    if jpholiday.is_holiday(d): return False
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
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*]', ']', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {
                "date": datetime.now(JST).date().isoformat(),
                "generated_at": datetime.now(JST).strftime("%H:%M"),
                "summary": "JSON解析エラー",
                "themes": [], "market_data": {}, "data_sources": []
            }

def save(filename, data):
    p = DATA / filename
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {p}")

def load(filename):
    p = DATA / filename
    if not p.exists(): return None
    with open(p, encoding="utf-8") as f: return json.load(f)

def load_learning_ctx():
    logs = load("accuracy_log.json")
    if not logs: return "初回実行"
    recent = logs[-10:]
    avg = sum(r["accuracy_score"] for r in recent) / len(recent)
    weak = list({r.get("weakest_theme","") for r in recent if r.get("weakest_theme")})
    strong = list({r.get("strongest_theme","") for r in recent if r.get("strongest_theme")})
    hints = []
    for r in recent[-3:]: hints.extend(r.get("improvement_hints", []))
    return f"過去{len(recent)}日平均精度:{avg:.0f}点 的中:{','.join(strong[:2])} 外れ:{','.join(weak[:2])} ヒント:{' / '.join(hints[-2:])}"

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept-Language": "ja,en;q=0.9"}

def fetch_kabutan():
    result = {
        "indices": [], "world_indices": [], "forex": [],
        "sector": [], "top_gainers": [], "top_losers": [],
        "volume_surge": [], "themes": [], "news": [],
        "source": "kabutan.jp"
    }

    urls_to_check = [
        ("news", "https://kabutan.jp/news/?b=n1"),
        ("gainers", "https://kabutan.jp/stock/ranking/?type=increase_rate"),
        ("losers", "https://kabutan.jp/stock/ranking/?type=decrease_rate"),
        ("volume", "https://kabutan.jp/stock/ranking/?type=volume_increase_rate"),
        ("theme", "https://kabutan.jp/themes/"),
        ("sector", "https://kabutan.jp/market/?b=sector"),
    ]

    for key, url in urls_to_check:
        try:
            r = requests.get(url, headers=H, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            print(f"\n=== {key} (status:{r.status_code}) ===")
            for i, t in enumerate(soup.find_all("table")[:5]):
                cls = t.get("class", [])
                rows = t.find_all("tr")
                print(f"  table[{i}] class={cls} rows={len(rows)}")
                if rows and len(rows) > 1:
                    print(f"  row1: {rows[1].get_text(strip=True)[:80]}")
            time.sleep(1)
        except Exception as e:
            print(f"エラー {key}: {e}")

    return result

def fetch_nhk_news():
    news = []
    try:
        r = requests.get("https://www3.nhk.or.jp/news/catnew.html", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("ul.content--list li, div.content--header")[:10]:
            text = item.get_text(strip=True)
            if len(text) > 15:
                news.append(text[:120])
        print(f"NHKニュース: {len(news)}件")
    except Exception as e:
        print(f"NHKエラー: {e}")
    return news

def fetch_reuters_news():
    news = []
    try:
        r = requests.get("https://jp.reuters.com/news/archive/businessNews", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("a.text__heading, h3.heading")[:10]:
            text = item.get_text(strip=True)
            if len(text) > 15:
                news.append(text[:120])
        print(f"ロイターニュース: {len(news)}件")
    except Exception as e:
        print(f"ロイターエラー: {e}")
    return news

def build_data_sources_summary(kabutan, nhk, reuters):
    sources = []
    if kabutan.get("indices"): sources.append(f"かぶたん国内指数{len(kabutan['indices'])}件")
    if kabutan.get("world_indices"): sources.append(f"かぶたん海外指数{len(kabutan['world_indices'])}件")
    if kabutan.get("sector"): sources.append(f"業種別騰落{len(kabutan['sector'])}件")
    if kabutan.get("top_gainers"): sources.append(f"値上がり{len(kabutan['top_gainers'])}件")
    if kabutan.get("top_losers"): sources.append(f"値下がり{len(kabutan['top_losers'])}件")
    if kabutan.get("volume_surge"): sources.append(f"出来高急増{len(kabutan['volume_surge'])}件")
    if kabutan.get("themes"): sources.append(f"テーマ{len(kabutan['themes'])}件")
    if kabutan.get("news"): sources.append(f"かぶたんニュース{len(kabutan['news'])}件")
    if nhk: sources.append(f"NHKニュース{len(nhk)}件")
    if reuters: sources.append(f"ロイター{len(reuters)}件")
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

    prompt = f"""IMPORTANT: Return ONLY valid JSON. No explanation. No markdown. Just JSON.

Today is {ymd}. You are a Japanese stock market analyst.
Based on your knowledge, predict today's TSE themes.

Learning data: {ctx}

Return this JSON (6-8 themes, descending confidence):
{{
  "date": "{iso}",
  "session": "600",
  "generated_at": "{now}",
  "data_sources": {json.dumps(data_sources, ensure_ascii=False)},
  "market_data": {{
    "indices": {json.dumps(kabutan['indices'][:6], ensure_ascii=False)},
    "world_indices": {json.dumps(kabutan['world_indices'][:6], ensure_ascii=False)},
    "forex": {json.dumps(kabutan['forex'][:4], ensure_ascii=False)},
    "key_news": ["ニュース1", "ニュース2", "ニュース3"]
  }},
  "themes": [
    {{
      "rank": 1,
      "name": "テーマ名",
      "confidence_score": 85,
      "rationale": "根拠",
      "data_basis": ["参照したデータ1", "参照したデータ2"],
      "key_stocks": [{{"name": "銘柄名", "code": "コード", "reason": "理由"}}],
      "risk_factors": "リスク",
      "us_connection": "米国との連動"
    }}
  ],
  "big_picture": "本日の最重要ファクター",
  "summary": "相場展望"
}}

ONLY JSON, nothing else."""

    print("[6:00] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_600.json", result)
    return result

def run_905(today):
    pred = load("latest_600.json")
    if not pred: raise FileNotFoundError("latest_600.json なし")

    print("[9:05] データ収集中...")
    kabutan = fetch_kabutan()
    nhk = fetch_nhk_news()
    data_sources = build_data_sources_summary(kabutan, nhk, [])

    theme_names = [t.get("name","") for t in pred.get("themes", [])]
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON. No explanation. No markdown. Just JSON.

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
  "opening": {{"nikkei_open": "値", "nikkei_change": "変化率", "market_tone": "強い/弱い/中立", "dominant_theme": "主役テーマ"}},
  "actual_flow": [{{"theme": "テーマ名", "evidence": "根拠", "strength": "high/medium/low"}}],
  "prediction_gap": [{{"predicted_theme": "予測テーマ", "predicted_score": 85, "actual_result": "的中/外れ/部分的中", "gap_reason": "理由", "missed_factor": "見落とし"}}],
  "intraday_correction": {{"themes_to_watch": ["テーマ1", "テーマ2"], "themes_faded": ["テーマ"], "correction_hints": ["ヒント1", "ヒント2"]}},
  "morning_accuracy_score": 70,
  "summary": "寄り付き総評"
}}

ONLY JSON."""

    print("[9:05] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_905.json", result)
    return result

def run_1535(today):
    pred_600 = load("latest_600.json")
    pred_905 = load("latest_905.json")
    if not pred_600: raise FileNotFoundError("latest_600.json なし")

    print("[15:35] データ収集中...")
    kabutan = fetch_kabutan()
    nhk = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(kabutan, nhk, reuters)

    theme_names = [t.get("name","") for t in pred_600.get("themes", [])]
    gap_905 = pred_905.get("prediction_gap", []) if pred_905 else []
    hints_905 = pred_905.get("intraday_correction", {}).get("correction_hints", []) if pred_905 else []
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON. No explanation. No markdown. Just JSON.

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
  "closing": {{"nikkei": "終値と変化率", "topix": "終値と変化率", "total_assessment": "総評"}},
  "theme_results": [{{"name": "テーマ名", "morning_score": 85, "final_result": "的中/外れ/部分的中", "detail": "詳細"}}],
  "stock_results": [{{"name": "銘柄名", "code": "コード", "close": "終値", "change": "変化率", "comment": "コメント"}}],
  "news_impact": [{{"news": "ニュース", "impact": "影響"}}],
  "correction_evaluation": "9:05修正の評価",
  "tomorrow_outlook": {{"key_events": ["イベント1", "イベント2"], "watch_themes": ["テーマ1", "テーマ2"], "hint": "明日のヒント"}},
  "final_accuracy_score": 75,
  "strongest_theme": "最も的中したテーマ",
  "weakest_theme": "最も外れたテーマ",
  "learning_points": ["学習1", "学習2", "学習3"],
  "summary": "本日の総括"
}}

ONLY JSON."""

    print("[15:35] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    result["data_sources"] = data_sources
    save("latest_1535.json", result)

    logs = load("accuracy_log.json") or []
    today_str = today.isoformat()
    existing = next((i for i,r in enumerate(logs) if r["date"]==today_str), None)
    entry = {
        "date": today_str,
        "accuracy_score": result.get("final_accuracy_score", 0),
        "strongest_theme": result.get("strongest_theme", ""),
        "weakest_theme": result.get("weakest_theme", ""),
        "improvement_hints": result.get("learning_points", []),
    }
    if existing is not None: logs[existing] = entry
    else: logs.append(entry)
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
    if   session == "600":  run_600(today)
    elif session == "905":  run_905(today)
    elif session == "1535": run_1535(today)
    else:
        print(f"不明: {session}")
        sys.exit(1)
    print(f"[{session}] 完了")
