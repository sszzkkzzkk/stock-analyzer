"""
株式AI自動分析 v3
SESSION=600 / 905 / 1535
6:00  データ収集 + テーマ予測
9:05  寄り付き確認 + 予測差分を即学習 → 当日15:35に反映
15:35 大引け総括 + 最終学習 → 翌日6:00に反映
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
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in res.content if b.type == "text")

def parse_json(raw):
    clean = re.sub(r"```json|```", "", raw).strip()
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m: raise ValueError(f"JSON not found: {raw[:300]}")
    return json.loads(m.group())

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
    if not logs: return "（初回 — 学習データなし）"
    recent = logs[-10:]
    avg = sum(r["accuracy_score"] for r in recent) / len(recent)
    weak  = list({r.get("weakest_theme","")   for r in recent if r.get("weakest_theme")})
    strong= list({r.get("strongest_theme","") for r in recent if r.get("strongest_theme")})
    hints = []
    for r in recent[-3:]: hints.extend(r.get("improvement_hints", []))
    return f"""【過去{len(recent)}営業日の学習データ】
平均精度: {avg:.0f}点
的中しやすいテーマ: {', '.join(strong[:3]) or 'なし'}
外れやすいテーマ: {', '.join(weak[:3]) or 'なし'}
改善ヒント: {' / '.join(hints[-4:]) or 'なし'}
→ 外れやすいテーマは confidence_score を低めに、rationale を慎重に記述すること"""

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
            for row in soup.select("table tbody tr")[:20]:
                cols = row.select("td")
                if len(cols) < 4: continue
                out[key].append({
                    "code":   cols[0].get_text(strip=True),
                    "name":   cols[1].get_text(strip=True),
                    "price":  cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True),
                })
        except Exception as e:
            print(f"Yahoo({key})エラー: {e}")
    try:
        r = requests.get("https://finance.yahoo.co.jp/stocks/ranking/industry", headers=H, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tbody tr")[:15]:
            cols = row.select("td")
            if len(cols) < 2: continue
            out["sector"].append({"name": cols[0].get_text(strip=True), "change": cols[1].get_text(strip=True)})
    except Exception as e:
        print(f"Yahoo(sector)エラー: {e}")
    return out

def run_600(today):
    ctx = load_learning_ctx()
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')}（東証営業日）です。
以下を全てウェブ検索して収集し、本日の東証テーマ予測まで行ってください。

【収集項目 — 株式市場に関わる情報を全て取得】
1. 米国株式（S&P500・NASDAQ・ダウ・Russell2000 終値・変化率）
2. 米国主要セクターの動き（テック・金融・エネルギー・ヘルスケア・素材等）
3. 先物（日経225先物・SGX・CME）
4. 為替（USD/JPY・EUR/JPY）
5. 商品（原油WTI・金・銅）
6. 債券・金利（米10年国債利回り・日本10年国債・VIX）
7. 米国株で特に動いた銘柄とその理由
8. 本日の重要ニュース（地政学・金利・決算・政策）
9. 本日の経済指標スケジュール（日米）

{ctx}

上記を全て収集した上で、本日の東証テーマ予測を行ってください。
資金が集まりそうなテーマ・銘柄を具体的に示してください。

JSONのみ返してください（説明文・```不要）:
{{
  "date": "{today.isoformat()}",
  "session": "600",
  "generated_at": "{datetime.now(JST).strftime('%H:%M')}",
  "market_data": {{
    "us_stocks": {{"sp500": "", "nasdaq": "", "dow": "", "russell2000": ""}},
    "futures": {{"nikkei225": "", "sgx": "", "cme_sp500": ""}},
    "forex": {{"usdjpy": "", "eurjpy": ""}},
    "commodities": {{"oil_wti": "", "gold": "", "copper": ""}},
    "bonds": {{"us10y": "", "jp10y": "", "vix": ""}},
    "us_sector_moves": [{{"sector": "", "change": "", "reason": ""}}],
    "us_hot_stocks": [{{"name": "", "change": "", "reason": ""}}],
    "key_news": [{{"title": "", "impact": "high/medium/low", "detail": ""}}],
    "economic_calendar": [{{"time": "", "event": "", "forecast": ""}}]
  }},
  "themes": [
    {{
      "rank": 1,
      "name": "テーマ名10字以内",
      "confidence_score": 85,
      "rationale": "根拠70字以内",
      "key_stocks": [{{"name": "銘柄名", "code": "コード", "reason": "選定理由30字以内"}}],
      "risk_factors": "リスク40字以内",
      "us_connection": "米国市場との連動性30字以内"
    }}
  ],
  "big_picture": "本日の相場を動かす最重要ファクター100字以内",
  "summary": "本日の相場展望150字以内"
}}
themes は confidence_score 降順で6〜8件。key_stocks は各テーマ2〜4銘柄。"""
    print("[6:00] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_600.json", result)
    return result

def run_905(today):
    pred = load("latest_600.json")
    if not pred: raise FileNotFoundError("latest_600.json がありません")
    market = fetch_yahoo()
    themes_600 = pred.get("themes", [])
    theme_names = [t["name"] for t in themes_600]
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 9:05、東証が寄り付いた直後です。

【6:00の予測テーマ（信頼度順）】
{json.dumps(theme_names, ensure_ascii=False)}

【6:00の詳細予測】
{json.dumps(themes_600, ensure_ascii=False)}

【Yahoo!ファイナンス 寄り付き後 値上がりTOP20】
{json.dumps(market['top_gainers'][:20], ensure_ascii=False)}

【出来高急増TOP20】
{json.dumps(market['volume_surge'][:20], ensure_ascii=False)}

【業種別騰落】
{json.dumps(market['sector'], ensure_ascii=False)}

ウェブ検索で寄り付き後のニュース・日経平均の動きも確認してください。

【分析指示】
1. 実際に資金が入ったテーマ・銘柄を特定する
2. 6:00予測との差を分析する
3. 外れた理由を具体的に特定する
4. 15:35分析への修正ヒントを生成する

JSONのみ返してください（説明文・```不要）:
{{
  "date": "{today.isoformat()}",
  "session": "905",
  "generated_at": "{datetime.now(JST).strftime('%H:%M')}",
  "opening": {{
    "nikkei_open": "寄り付き値",
    "nikkei_change": "変化率",
    "market_tone": "強い/弱い/中立",
    "dominant_theme": "寄り付きで最も資金が入ったテーマ"
  }},
  "actual_flow": [
    {{"theme": "実際に動いたテーマ", "evidence": "根拠となる銘柄・出来高", "strength": "high/medium/low"}}
  ],
  "prediction_gap": [
    {{
      "predicted_theme": "予測テーマ名",
      "predicted_score": 85,
      "actual_result": "的中/外れ/部分的中",
      "gap_reason": "差が生じた理由50字以内",
      "missed_factor": "見落としたファクター30字以内"
    }}
  ],
  "intraday_correction": {{
    "themes_to_watch": ["午後も継続しそうなテーマ1", "テーマ2"],
    "themes_faded": ["朝だけで終わりそうなテーマ"],
    "correction_hints": ["15:35分析への修正ヒント1", "ヒント2", "ヒント3"]
  }},
  "morning_accuracy_score": 70,
  "summary": "寄り付き総評120字以内"
}}"""
    print("[9:05] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_905.json", result)
    return result

def run_1535(today):
    pred_600 = load("latest_600.json")
    pred_905 = load("latest_905.json")
    if not pred_600: raise FileNotFoundError("latest_600.json がありません")
    market = fetch_yahoo()
    themes_600 = pred_600.get("themes", [])
    gap_905    = pred_905.get("prediction_gap", []) if pred_905 else []
    correction = pred_905.get("intraday_correction", {}) if pred_905 else {}
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 15:35、東証の大引け直後です。

【6:00の予測テーマ】
{json.dumps(themes_600, ensure_ascii=False)}

【9:05時点の予測差分・修正ヒント】
差分: {json.dumps(gap_905, ensure_ascii=False)}
修正ヒント: {json.dumps(correction, ensure_ascii=False)}

【Yahoo!ファイナンス 大引け値上がりTOP20】
{json.dumps(market['top_gainers'][:20], ensure_ascii=False)}

【値下がりTOP10】
{json.dumps(market['top_losers'][:10], ensure_ascii=False)}

【出来高急増TOP20】
{json.dumps(market['volume_surge'][:20], ensure_ascii=False)}

【業種別騰落】
{json.dumps(market['sector'], ensure_ascii=False)}

ウェブ検索で以下を調べてください:
1. 日経平均・TOPIXの終値・変化率
2. 場中の重要ニュース
3. 6:00予測のkey_stocksの実際の終値・変化率
4. 明日に影響しそうなニュース・イベント

JSONのみ返してください（説明文・```不要）:
{{
  "date": "{today.isoformat()}",
  "session": "1535",
  "generated_at": "{datetime.now(JST).strftime('%H:%M')}",
  "closing": {{
    "nikkei": "終値と変化率",
    "topix": "終値と変化率",
    "total_assessment": "全体評価ひとこと"
  }},
  "theme_results": [
    {{"name": "テーマ名", "morning_score": 85, "final_result": "的中/外れ/部分的中", "detail": "50字以内"}}
  ],
  "stock_results": [
    {{"name": "銘柄名", "code": "コード", "close": "終値", "change": "変化率", "comment": "30字以内"}}
  ],
  "news_impact": [
    {{"news": "場中ニュース", "impact": "相場への影響50字以内"}}
  ],
  "correction_evaluation": "9:05の修正ヒントが活きたか評価60字以内",
  "tomorrow_outlook": {{
    "key_events": ["明日の重要イベント1", "イベント2"],
    "watch_themes": ["明日注目すべきテーマ1", "テーマ2"],
    "hint": "明日の相場への示唆100字以内"
  }},
  "final_accuracy_score": 75,
  "strongest_theme": "最も当たったテーマ",
  "weakest_theme": "最も外れたテーマ",
  "learning_points": ["学習ポイント1", "学習ポイント2", "学習ポイント3"],
  "summary": "本日の総括150字以内"
}}"""
    print("[15:35] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_1535.json", result)
    logs = load("accuracy_log.json") or []
    today_str = today.isoformat()
    existing = next((i for i,r in enumerate(logs) if r["date"]==today_str), None)
    entry = {"date": today_str, "accuracy_score": result.get("final_accuracy_score", 0),
             "strongest_theme": result.get("strongest_theme", ""), "weakest_theme": result.get("weakest_theme", ""),
             "improvement_hints": result.get("learning_points", [])}
    if existing is not None: logs[existing] = entry
    else: logs.append(entry)
    save("accuracy_log.json", logs[-90:])
    return result

if __name__ == "__main__":
    now_jst = datetime.now(JST)
    today = now_jst.date()
    session = os.environ.get("SESSION", "").strip() or (
        "600"  if now_jst.hour < 7  else
        "905"  if now_jst.hour < 10 else
        "1535"
    )
    print(f"SESSION={session} date={today}")
    if not is_trading_day(today):
        print("非営業日 — スキップ"); sys.exit(0)
    if   session == "600":  run_600(today)
    elif session == "905":  run_905(today)
    elif session == "1535": run_1535(today)
    else: print(f"不明: {session}"); sys.exit(1)
    print(f"[{session}] 完了")
