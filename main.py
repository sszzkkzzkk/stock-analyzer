“””
株式AI自動分析 v3
SESSION=600 / 905 / 1535
“””
import os, sys, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import jpholiday
import anthropic

JST = timezone(timedelta(hours=9))
DATA = Path(“data”)
DATA.mkdir(exist_ok=True)
client = anthropic.Anthropic(api_key=os.environ[“ANTHROPIC_API_KEY”])
YEAR_END = {(12,31),(1,1),(1,2),(1,3)}

def is_trading_day(d):
if d.weekday() >= 5: return False
if (d.month, d.day) in YEAR_END: return False
if jpholiday.is_holiday(d): return False
return True

def call_claude(prompt):
res = client.messages.create(
model=“claude-sonnet-4-20250514”,
max_tokens=3000,
tools=[{“type”: “web_search_20250305”, “name”: “web_search”, “max_uses”: 5}],
messages=[{“role”: “user”, “content”: prompt}],
)
return “”.join(b.text for b in res.content if b.type == “text”)

def parse_json(raw):
clean = re.sub(r”`json|`”, “”, raw).strip()
m = re.search(r”{[\s\S]*}”, clean)
if not m: raise ValueError(f”JSON not found: {raw[:300]}”)
return json.loads(m.group())

def save(filename, data):
p = DATA / filename
with open(p, “w”, encoding=“utf-8”) as f:
json.dump(data, f, ensure_ascii=False, indent=2)
print(f”保存: {p}”)

def load(filename):
p = DATA / filename
if not p.exists(): return None
with open(p, encoding=“utf-8”) as f: return json.load(f)

def load_learning_ctx():
logs = load(“accuracy_log.json”)
if not logs: return “（初回 — 学習データなし）”
recent = logs[-10:]
avg = sum(r[“accuracy_score”] for r in recent) / len(recent)
weak = list({r.get(“weakest_theme”,””) for r in recent if r.get(“weakest_theme”)})
strong = list({r.get(“strongest_theme”,””) for r in recent if r.get(“strongest_theme”)})
hints = []
for r in recent[-3:]: hints.extend(r.get(“improvement_hints”, []))
return (
f”【過去{len(recent)}営業日の学習データ】\n”
f”平均精度: {avg:.0f}点\n”
f”的中しやすいテーマ: {’, ‘.join(strong[:3]) or ‘なし’}\n”
f”外れやすいテーマ: {’, ‘.join(weak[:3]) or ‘なし’}\n”
f”改善ヒント: {’ / ’.join(hints[-4:]) or ‘なし’}\n”
f”→ 外れやすいテーマは confidence_score を低めに設定すること”
)

def fetch_yahoo():
H = {“User-Agent”: “Mozilla/5.0”, “Accept-Language”: “ja”}
out = {“top_gainers”: [], “top_losers”: [], “volume_surge”: [], “sector”: []}
urls = {
“top_gainers”:  “https://finance.yahoo.co.jp/stocks/ranking/rateUp?market=tse&term=daily&page=1”,
“top_losers”:   “https://finance.yahoo.co.jp/stocks/ranking/rateDown?market=tse&term=daily&page=1”,
“volume_surge”: “https://finance.yahoo.co.jp/stocks/ranking/volumeUp?market=tse&term=daily&page=1”,
}
for key, url in urls.items():
try:
r = requests.get(url, headers=H, timeout=15)
soup = BeautifulSoup(r.text, “html.parser”)
for row in soup.select(“table tbody tr”)[:20]:
cols = row.select(“td”)
if len(cols) < 4: continue
out[key].append({
“code”:   cols[0].get_text(strip=True),
“name”:   cols[1].get_text(strip=True),
“price”:  cols[2].get_text(strip=True),
“change”: cols[3].get_text(strip=True),
})
except Exception as e:
print(f”Yahoo({key})エラー: {e}”)
try:
r = requests.get(“https://finance.yahoo.co.jp/stocks/ranking/industry”, headers=H, timeout=15)
soup = BeautifulSoup(r.text, “html.parser”)
for row in soup.select(“table tbody tr”)[:15]:
cols = row.select(“td”)
if len(cols) < 2: continue
out[“sector”].append({
“name”: cols[0].get_text(strip=True),
“change”: cols[1].get_text(strip=True),
})
except Exception as e:
print(f”Yahoo(sector)エラー: {e}”)
return out

def run_600(today):
ctx = load_learning_ctx()
d = today.isoformat()
t = datetime.now(JST).strftime(’%H:%M’)
prompt = (
f”今日は{today.strftime(’%Y年%m月%d日’)}（東証営業日）です。\n”
“以下を全てウェブ検索して収集し、本日の東証テーマ予測まで行ってください。\n\n”
“1. 米国株式（S&P500・NASDAQ・ダウ・Russell2000 終値・変化率）\n”
“2. 米国主要セクターの動き\n”
“3. 先物（日経225先物・SGX・CME）\n”
“4. 為替（USD/JPY・EUR/JPY）\n”
“5. 商品（原油WTI・金・銅）\n”
“6. 債券・金利（米10年国債・日本10年国債・VIX）\n”
“7. 米国株で特に動いた銘柄とその理由\n”
“8. 本日の重要ニュース\n”
“9. 本日の経済指標スケジュール\n\n”
f”{ctx}\n\n”
“JSONのみ返してください（説明文不要）:\n”
“{\n”
f’  “date”: “{d}”,\n’
’  “session”: “600”,\n’
f’  “generated_at”: “{t}”,\n’
’  “market_data”: {\n’
’    “us_stocks”: {“sp500”: “”, “nasdaq”: “”, “dow”: “”, “russell2000”: “”},\n’
’    “futures”: {“nikkei225”: “”, “sgx”: “”, “cme_sp500”: “”},\n’
’    “forex”: {“usdjpy”: “”, “eurjpy”: “”},\n’
’    “commodities”: {“oil_wti”: “”, “gold”: “”, “copper”: “”},\n’
’    “bonds”: {“us10y”: “”, “jp10y”: “”, “vix”: “”},\n’
’    “us_sector_moves”: [{“sector”: “”, “change”: “”, “reason”: “”}],\n’
’    “us_hot_stocks”: [{“name”: “”, “change”: “”, “reason”: “”}],\n’
’    “key_news”: [{“title”: “”, “impact”: “high/medium/low”, “detail”: “”}],\n’
’    “economic_calendar”: [{“time”: “”, “event”: “”, “forecast”: “”}]\n’
’  },\n’
’  “themes”: [\n’
’    {\n’
’      “rank”: 1,\n’
’      “name”: “テーマ名10字以内”,\n’
’      “confidence_score”: 85,\n’
’      “rationale”: “根拠70字以内”,\n’
’      “key_stocks”: [{“name”: “銘柄名”, “code”: “コード”, “reason”: “選定理由30字以内”}],\n’
’      “risk_factors”: “リスク40字以内”,\n’
’      “us_connection”: “米国市場との連動性30字以内”\n’
’    }\n’
’  ],\n’
’  “big_picture”: “本日の相場を動かす最重要ファクター100字以内”,\n’
’  “summary”: “本日の相場展望150字以内”\n’
‘}\n’
“themes は confidence_score 降順で6〜8件。key_stocks は各テーマ2〜4銘柄。”
)
print(”[6:00] Claude 呼び出し中…”)
result = parse_json(call_claude(prompt))
save(“latest_600.json”, result)
return result

def run_905(today):
pred = load(“latest_600.json”)
if not pred: raise FileNotFoundError(“latest_600.json がありません”)
market = fetch_yahoo()
themes_600 = pred.get(“themes”, [])
theme_names = [t[“name”] for t in themes_600]
d = today.isoformat()
t = datetime.now(JST).strftime(’%H:%M’)
prompt = (
f”今日は{today.strftime(’%Y年%m月%d日’)} 9:05、東証が寄り付いた直後です。\n\n”
f”【6:00の予測テーマ】\n{json.dumps(theme_names, ensure_ascii=False)}\n\n”
f”【6:00の詳細予測】\n{json.dumps(themes_600, ensure_ascii=False)}\n\n”
f”【値上がりTOP20】\n{json.dumps(market[‘top_gainers’][:20], ensure_ascii=False)}\n\n”
f”【出来高急増TOP20】\n{json.dumps(market[‘volume_surge’][:20], ensure_ascii=False)}\n\n”
f”【業種別騰落】\n{json.dumps(market[‘sector’], ensure_ascii=False)}\n\n”
“ウェブ検索で寄り付き後のニュース・日経平均の動きも確認してください。\n\n”
“JSONのみ返してください（説明文不要）:\n”
“{\n”
f’  “date”: “{d}”,\n’
’  “session”: “905”,\n’
f’  “generated_at”: “{t}”,\n’
’  “opening”: {“nikkei_open”: “”, “nikkei_change”: “”, “market_tone”: “強い/弱い/中立”, “dominant_theme”: “”},\n’
’  “actual_flow”: [{“theme”: “”, “evidence”: “”, “strength”: “high/medium/low”}],\n’
’  “prediction_gap”: [{“predicted_theme”: “”, “predicted_score”: 85, “actual_result”: “的中/外れ/部分的中”, “gap_reason”: “”, “missed_factor”: “”}],\n’
’  “intraday_correction”: {“themes_to_watch”: [””, “”], “themes_faded”: [””], “correction_hints”: [””, “”, “”]},\n’
’  “morning_accuracy_score”: 70,\n’
’  “summary”: “”\n’
“}”
)
print(”[9:05] Claude 呼び出し中…”)
result = parse_json(call_claude(prompt))
save(“latest_905.json”, result)
return result

def run_1535(today):
pred_600 = load(“latest_600.json”)
pred_905 = load(“latest_905.json”)
if not pred_600: raise FileNotFoundError(“latest_600.json がありません”)
market = fetch_yahoo()
themes_600 = pred_600.get(“themes”, [])
gap_905 = pred_905.get(“prediction_gap”, []) if pred_905 else []
correction = pred_905.get(“intraday_correction”, {}) if pred_905 else {}
d = today.isoformat()
t = datetime.now(JST).strftime(’%H:%M’)
prompt = (
f”今日は{today.strftime(’%Y年%m月%d日’)} 15:35、東証の大引け直後です。\n\n”
f”【6:00の予測テーマ】\n{json.dumps(themes_600, ensure_ascii=False)}\n\n”
f”【9:05時点の差分】\n{json.dumps(gap_905, ensure_ascii=False)}\n\n”
f”【9:05修正ヒント】\n{json.dumps(correction, ensure_ascii=False)}\n\n”
f”【値上がりTOP20】\n{json.dumps(market[‘top_gainers’][:20], ensure_ascii=False)}\n\n”
f”【値下がりTOP10】\n{json.dumps(market[‘top_losers’][:10], ensure_ascii=False)}\n\n”
f”【出来高急増TOP20】\n{json.dumps(market[‘volume_surge’][:20], ensure_ascii=False)}\n\n”
f”【業種別騰落】\n{json.dumps(market[‘sector’], ensure_ascii=False)}\n\n”
“ウェブ検索で日経平均終値・場中ニュース・key_stocksの終値を調べてください。\n\n”
“JSONのみ返してください（説明文不要）:\n”
“{\n”
f’  “date”: “{d}”,\n’
’  “session”: “1535”,\n’
f’  “generated_at”: “{t}”,\n’
’  “closing”: {“nikkei”: “”, “topix”: “”, “total_assessment”: “”},\n’
’  “theme_results”: [{“name”: “”, “morning_score”: 85, “final_result”: “的中/外れ/部分的中”, “detail”: “”}],\n’
’  “stock_results”: [{“name”: “”, “code”: “”, “close”: “”, “change”: “”, “comment”: “”}],\n’
’  “news_impact”: [{“news”: “”, “impact”: “”}],\n’
’  “correction_evaluation”: “”,\n’
’  “tomorrow_outlook”: {“key_events”: [””, “”], “watch_themes”: [””, “”], “hint”: “”},\n’
’  “final_accuracy_score”: 75,\n’
’  “strongest_theme”: “”,\n’
’  “weakest_theme”: “”,\n’
’  “learning_points”: [””, “”, “”],\n’
’  “summary”: “”\n’
“}”
)
print(”[15:35] Claude 呼び出し中…”)
result = parse_json(call_claude(prompt))
save(“latest_1535.json”, result)
logs = load(“accuracy_log.json”) or []
today_str = today.isoformat()
existing = next((i for i,r in enumerate(logs) if r[“date”]==today_str), None)
entry = {
“date”: today_str,
“accuracy_score”: result.get(“final_accuracy_score”, 0),
“strongest_theme”: result.get(“strongest_theme”, “”),
“weakest_theme”: result.get(“weakest_theme”, “”),
“improvement_hints”: result.get(“learning_points”, []),
}
if existing is not None: logs[existing] = entry
else: logs.append(entry)
save(“accuracy_log.json”, logs[-90:])
return result

if **name** == “**main**”:
now_jst = datetime.now(JST)
today = now_jst.date()
session = os.environ.get(“SESSION”, “”).strip() or (
“600”  if now_jst.hour < 7  else
“905”  if now_jst.hour < 10 else
“1535”
)
print(f”SESSION={session} date={today}”)
if not is_trading_day(today):
print(“非営業日 — スキップ”); sys.exit(0)
if   session == “600”:  run_600(today)
elif session == “905”:  run_905(today)
elif session == “1535”: run_1535(today)
else: print(f”不明: {session}”); sys.exit(1)
print(f”[{session}] 完了”)