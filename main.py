"""
株式AI自動分析 v4
SESSION=600 / 905 / 1535
"""
import os, sys, json, re
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
                "summary": "JSON解析エラー — 再実行してください",
                "themes": [],
                "market_data": {}
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

def fetch_yahoo():
    H = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"}
    out = {"top_gainers": [], "top_losers": [], "volume_surge": [], "sector": []}
    urls = {
        "top_gainers":  "https://finance.yahoo.co.jp/stocks/ranking/rateUp?market=tse&term=daily&page=1",
        "top_losers":   "https://finance.yahoo.co.jp/stocks/ranking/rateDown?market=tse&term=daily&page=1",
        "volume_surge": "https://finance.yahoo.co.jp/stocks/ranking/volumeUp?market=tse&term=daily&page=1",
    }
    for key, url in urls.items():
        try:
            r = requests.get(url, headers=H, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tbody tr")[:15]:
                cols = row.select("td")
                if len(cols) < 4: continue
                out[key].append({
                    "code": cols[0].get_text(strip=True),
                    "name": cols[1].get_text(strip=True),
                    "price": cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True),
                })
        except Exception as e:
            print(f"Yahoo({key})error: {e}")
    try:
        r = requests.get("https://finance.yahoo.co.jp/stocks/ranking/industry", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tbody tr")[:10]:
            cols = row.select("td")
            if len(cols) < 2: continue
            out["sector"].append({"name": cols[0].get_text(strip=True), "change": cols[1].get_text(strip=True)})
    except Exception as e:
        print(f"Yahoo(sector)error: {e}")
    return out
def run_600(today):
    ctx = load_learning_ctx()
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    prompt = f"""IMPORTANT: Return ONLY valid JSON. No explanation. No markdown. Just JSON.

Today is {ymd}. Predict Tokyo Stock Exchange themes for today.

Return this exact JSON structure:
{{
  "date": "{iso}",
  "session": "600",
  "generated_at": "{now}",
  "market_data": {{"us_stocks": {{"sp500": "value", "nasdaq": "value", "dow": "value"}}, "futures": {{"nikkei225": "value"}}, "forex": {{"usdjpy": "value"}}, "bonds": {{"vix": "value"}}, "key_news": [{{"title": "news", "impact": "high", "detail": "detail"}}]}},
  "themes": [{{"rank": 1, "name": "theme", "confidence_score": 85, "rationale": "reason", "key_stocks": [{{"name": "stock", "code": "code", "reason": "reason"}}], "risk_factors": "risk", "us_connection": "connection"}}],
  "big_picture": "key factor today",
  "summary": "market outlook"
}}

学習データ: {ctx}
Return 6-8 themes. ONLY JSON, nothing else.


学習データ: {ctx}

以下のJSON形式のみで返答してください。説明文・前置き・```は不要です。

{{
  "date": "{iso}",
  "session": "600",
  "generated_at": "{now}",
  "market_data": {{
    "us_stocks": {{"sp500": "値と変化率", "nasdaq": "値と変化率", "dow": "値と変化率"}},
    "futures": {{"nikkei225": "予想値", "sgx": "予想値"}},
    "forex": {{"usdjpy": "値", "eurjpy": "値"}},
    "commodities": {{"oil_wti": "値", "gold": "値"}},
    "bonds": {{"us10y": "値", "jp10y": "値", "vix": "値"}},
    "us_sector_moves": [{{"sector": "セクター名", "change": "変化率", "reason": "理由"}}],
    "us_hot_stocks": [{{"name": "銘柄名", "change": "変化率", "reason": "理由"}}],
    "key_news": [{{"title": "ニュース", "impact": "high", "detail": "詳細"}}],
    "economic_calendar": [{{"time": "時刻", "event": "指標名", "forecast": "予想"}}]
  }},
  "themes": [
    {{
      "rank": 1,
      "name": "テーマ名",
      "confidence_score": 85,
      "rationale": "根拠",
      "key_stocks": [{{"name": "銘柄名", "code": "コード", "reason": "理由"}}],
      "risk_factors": "リスク",
      "us_connection": "米国との連動"
    }}
  ],
  "big_picture": "本日の最重要ファクター",
  "summary": "相場展望"
}}

themesは6〜8件。必ずJSONのみ返すこと。"""

    print("[6:00] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_600.json", result)
    return result

def run_905(today):
    pred = load("latest_600.json")
    if not pred: raise FileNotFoundError("latest_600.json なし")
    market = fetch_yahoo()
    theme_names = [t.get("name","") for t in pred.get("themes", [])]
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    gainers_short = [{"name": x["name"], "change": x["change"]} for x in market["top_gainers"][:10]]
    volume_short = [{"name": x["name"], "change": x["change"]} for x in market["volume_surge"][:10]]
    sector_short = market["sector"][:8]

    prompt = f"""あなたは株式アナリストです。{ymd} 9:05の寄り付き分析を行ってください。

6:00予測テーマ: {json.dumps(theme_names, ensure_ascii=False)}
値上がりTOP10: {json.dumps(gainers_short, ensure_ascii=False)}
出来高急増TOP10: {json.dumps(volume_short, ensure_ascii=False)}
業種別: {json.dumps(sector_short, ensure_ascii=False)}

以下のJSON形式のみで返答してください。説明文・前置き・```は不要です。

{{
  "date": "{iso}",
  "session": "905",
  "generated_at": "{now}",
  "opening": {{"nikkei_open": "値", "nikkei_change": "変化率", "market_tone": "強い/弱い/中立", "dominant_theme": "主役テーマ"}},
  "actual_flow": [{{"theme": "テーマ名", "evidence": "根拠", "strength": "high/medium/low"}}],
  "prediction_gap": [{{"predicted_theme": "予測テーマ", "predicted_score": 85, "actual_result": "的中/外れ/部分的中", "gap_reason": "理由", "missed_factor": "見落とし"}}],
  "intraday_correction": {{"themes_to_watch": ["テーマ1", "テーマ2"], "themes_faded": ["テーマ"], "correction_hints": ["ヒント1", "ヒント2"]}},
  "morning_accuracy_score": 70,
  "summary": "寄り付き総評"
}}

必ずJSONのみ返すこと。"""

    print("[9:05] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_905.json", result)
    return result
def run_1535(today):
    pred_600 = load("latest_600.json")
    pred_905 = load("latest_905.json")
    if not pred_600: raise FileNotFoundError("latest_600.json なし")
    market = fetch_yahoo()
    theme_names = [t.get("name","") for t in pred_600.get("themes", [])]
    gap_905 = pred_905.get("prediction_gap", []) if pred_905 else []
    hints_905 = pred_905.get("intraday_correction", {}).get("correction_hints", []) if pred_905 else []
    iso = today.isoformat()
    now = datetime.now(JST).strftime("%H:%M")
    ymd = today.strftime("%Y年%m月%d日")

    gainers_short = [{"name": x["name"], "change": x["change"]} for x in market["top_gainers"][:10]]
    losers_short = [{"name": x["name"], "change": x["change"]} for x in market["top_losers"][:8]]
    sector_short = market["sector"][:8]

    prompt = f"""あなたは株式アナリストです。{ymd} 15:35の大引け総括を行ってください。

6:00予測テーマ: {json.dumps(theme_names, ensure_ascii=False)}
9:05差分: {json.dumps(gap_905, ensure_ascii=False)}
9:05修正ヒント: {json.dumps(hints_905, ensure_ascii=False)}
値上がりTOP10: {json.dumps(gainers_short, ensure_ascii=False)}
値下がりTOP8: {json.dumps(losers_short, ensure_ascii=False)}
業種別: {json.dumps(sector_short, ensure_ascii=False)}

以下のJSON形式のみで返答してください。説明文・前置き・```は不要です。

{{
  "date": "{iso}",
  "session": "1535",
  "generated_at": "{now}",
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

必ずJSONのみ返すこと。"""

    print("[15:35] Claude呼び出し中...")
    result = parse_json(call_claude(prompt))
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
        raise ValueError("SESSIONが設定されていません。workflow_dispatchまたはcronで設定してください。")
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
