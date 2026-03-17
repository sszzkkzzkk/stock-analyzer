"""
株式AI自動分析 v5
SESSION=600 / 905 / 1535
データソース: かぶたん + NHK + ロイター
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
                "summary": "JSON解析エラー — 再実行してください",
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

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "Accept-Language": "ja"}

def fetch_kabutan():
    """かぶたんから市場データを全取得"""
    result = {
        "indices": [],
        "world_indices": [],
        "forex": [],
        "commodities": [],
        "sector": [],
        "top_gainers": [],
        "top_losers": [],
        "volume_surge": [],
        "themes": [],
        "news": [],
        "source": "kabutan.jp"
    }
    
    # 指数・為替・商品
    try:
        r = requests.get("https://kabutan.jp/market/", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # 国内指数
        for row in soup.select("table.market_index tr")[:10]:
            cols = row.select("td")
            if len(cols) >= 3:
                result["indices"].append({
                    "name": cols[0].get_text(strip=True),
                    "price": cols[1].get_text(strip=True),
                    "change": cols[2].get_text(strip=True)
                })
        
        # 海外指数
        for row in soup.select("table.world_index tr")[:10]:
            cols = row.select("td")
            if len(cols) >= 3:
                result["world_indices"].append({
                    "name": cols[0].get_text(strip=True),
                    "price": cols[1].get_text(strip=True),
                    "change": cols[2].get_text(strip=True)
                })
        
        # 為替
        for row in soup.select("table.forex tr")[:8]:
            cols = row.select("td")
            if len(cols) >= 2:
                result["forex"].append({
                    "pair": cols[0].get_text(strip=True),
                    "rate": cols[1].get_text(strip=True)
                })
        
        print(f"かぶたん市場データ: 国内{len(result['indices'])}件 海外{len(result['world_indices'])}件 為替{len(result['forex'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたん市場エラー: {e}")

    # 業種別騰落
    try:
        r = requests.get("https://kabutan.jp/market/sector/", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tr")[1:35]:
            cols = row.select("td")
            if len(cols) >= 3:
                result["sector"].append({
                    "name": cols[0].get_text(strip=True),
                    "change": cols[1].get_text(strip=True),
                    "change_pct": cols[2].get_text(strip=True)
                })
        print(f"かぶたん業種: {len(result['sector'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたん業種エラー: {e}")

    # 値上がりランキング
    try:
        r = requests.get("https://kabutan.jp/stock/ranking/?type=increase_rate", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.stock_ranking tr")[1:21]:
            cols = row.select("td")
            if len(cols) >= 4:
                result["top_gainers"].append({
                    "code": cols[0].get_text(strip=True),
                    "name": cols[1].get_text(strip=True),
                    "price": cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True)
                })
        print(f"かぶたん値上がり: {len(result['top_gainers'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたん値上がりエラー: {e}")

    # 値下がりランキング
    try:
        r = requests.get("https://kabutan.jp/stock/ranking/?type=decrease_rate", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.stock_ranking tr")[1:11]:
            cols = row.select("td")
            if len(cols) >= 4:
                result["top_losers"].append({
                    "code": cols[0].get_text(strip=True),
                    "name": cols[1].get_text(strip=True),
                    "price": cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True)
                })
        print(f"かぶたん値下がり: {len(result['top_losers'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたん値下がりエラー: {e}")

    # 出来高急増
    try:
        r = requests.get("https://kabutan.jp/stock/ranking/?type=volume_increase", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table.stock_ranking tr")[1:16]:
            cols = row.select("td")
            if len(cols) >= 4:
                result["volume_surge"].append({
                    "code": cols[0].get_text(strip=True),
                    "name": cols[1].get_text(strip=True),
                    "price": cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True)
                })
        print(f"かぶたん出来高: {len(result['volume_surge'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたん出来高エラー: {e}")

    # テーマ別ランキング
    try:
        r = requests.get("https://kabutan.jp/theme/", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tr")[1:21]:
            cols = row.select("td")
            if len(cols) >= 2:
                result["themes"].append({
                    "name": cols[0].get_text(strip=True),
                    "change": cols[1].get_text(strip=True) if len(cols) > 1 else ""
                })
        print(f"かぶたんテーマ: {len(result['themes'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたんテーマエラー: {e}")

    # ニュース
    try:
        r = requests.get("https://kabutan.jp/news/", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("div.news_list li, table.news tr")[:15]:
            text = item.get_text(strip=True)
            if len(text) > 10:
                result["news"].append(text[:100])
        print(f"かぶたんニュース: {len(result['news'])}件")
        time.sleep(1)
    except Exception as e:
        print(f"かぶたんニュースエラー: {e}")

    return result

def fetch_nhk_news():
    """NHKニュースから重要ニュースを取得"""
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
    """ロイター日本語版からニュースを取得"""
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
    """何を見て分析したかのサマリーを生成"""
    sources = []
    if kabutan.get("indices"): sources.append(f"かぶたん国内指数{len(kabutan['indices'])}件")
    if kabutan.get("world_indices"): sources.append(f"かぶたん海外指数{len(kabutan['world_indices'])}件")
    if kabutan.get("forex"): sources.append(f"為替{len(kabutan['forex'])}件")
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
Analyze the following real market data and predict today's TSE themes.

=== MARKET DATA (kabutan.jp) ===
Domestic Indices: {json.dumps(kabutan['indices'][:8], ensure_ascii=False)}
World Indices: {json.dumps(kabutan['world_indices'][:8], ensure_ascii=False)}
Forex: {json.dumps(kabutan['forex'][:6], ensure_ascii=False)}
Sector Performance: {json.dumps(kabutan['sector'][:20], ensure_ascii=False)}
Top Gainers: {json.dumps(kabutan['top_gainers'][:15], ensure_ascii=False)}
Volume Surge: {json.dumps(kabutan['volume_surge'][:10], ensure_ascii=False)}
Hot Themes: {json.dumps(kabutan['themes'][:15], ensure_ascii=False)}
Market News: {json.dumps(kabutan['news'][:10], ensure_ascii=False)}

=== NEWS ===
NHK: {json.dumps(nhk[:8], ensure_ascii=False)}
Reuters: {json.dumps(reuters[:8], ensure_ascii=False)}

=== LEARNING DATA ===
{ctx}

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
    "sector_top3": {json.dumps(sorted(kabutan['sector'], key=lambda x: x.get('change',''), reverse=True)[:3] if kabutan['sector'] else [], ensure_ascii=False)},
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
