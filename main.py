"""
株式AI自動分析 — main.py（通知なし・ダッシュボードのみ版）
GitHub Actions から SESSION=730 or 830 で呼ばれる
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
        max_tokens=2500,
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

def load_learning_ctx():
    p = DATA / "accuracy_log.json"
    if not p.exists(): return "（初回）"
    with open(p, encoding="utf-8") as f: logs = json.load(f)
    if not logs: return "（データなし）"
    recent = logs[-10:]
    avg = sum(r["accuracy_score"] for r in recent) / len(recent)
    weak = list({r.get("weakest_theme","") for r in recent if r.get("weakest_theme")})
    hints = []
    for r in recent[-3:]: hints.extend(r.get("improvement_hints", []))
    return f"過去{len(recent)}日平均精度:{avg:.0f}点 外れやすいテーマ:{','.join(weak[:3])} ヒント:{' / '.join(hints[-3:])}"

def fetch_yahoo():
    H = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"}
    out = {"top_gainers": [], "volume_surge": []}
    for key, url in [
        ("top_gainers", "https://finance.yahoo.co.jp/stocks/ranking/rateUp?market=tse&term=daily&page=1"),
        ("volume_surge", "https://finance.yahoo.co.jp/stocks/ranking/volumeUp?market=tse&term=daily&page=1"),
    ]:
        try:
            r = requests.get(url, headers=H, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tbody tr")[:15]:
                cols = row.select("td")
                if len(cols) < 4: continue
                out[key].append({"code": cols[0].get_text(strip=True), "name": cols[1].get_text(strip=True),
                                 "price": cols[2].get_text(strip=True), "change": cols[3].get_text(strip=True)})
        except Exception as e:
            print(f"Yahoo({key})エラー: {e}")
    return out

def run_730(today):
    ctx = load_learning_ctx()
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')}（東証営業日）です。
ウェブ検索で以下を調査して本日のテーマ予測を行ってください:
1. 昨夜の米国株（S&P500・NASDAQ・ダウ 終値・変化率・動いたセクター）
2. 日経先物・SGX先物の現在値
3. USD/JPYの現在値
4. 本日の主要ニュース3件

{ctx}

JSONのみ返してください（説明文・```不要）:
{{"date":"{today.isoformat()}","session":"730","generated_at":"{datetime.now(JST).strftime('%H:%M')}",
"market_overview":{{"sp500":"","nasdaq":"","dow":"","nikkei_futures":"","usdjpy":"","key_news":["","",""]}},
"themes":[{{"rank":1,"name":"テーマ名10字以内","confidence_score":85,"rationale":"根拠60字以内","key_stocks":["銘柄(コード)"],"risk_factors":"リスク40字以内"}}],
"summary":"展望120字以内"}}
themes は confidence_score 降順で5〜7件。"""
    print("[7:30] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_730.json", result)
    return result

def run_830(today):
    p = DATA / "latest_730.json"
    if not p.exists(): raise FileNotFoundError("latest_730.json がありません")
    with open(p, encoding="utf-8") as f: pred = json.load(f)
    theme_names = [t["name"] for t in pred.get("themes", [])]
    market = fetch_yahoo()
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 8:30です。
7:30予測: {theme_names}
詳細: {json.dumps(pred.get('themes',[]), ensure_ascii=False)}
Yahoo値上がりTOP15: {json.dumps(market['top_gainers'][:15], ensure_ascii=False)}
出来高急増TOP10: {json.dumps(market['volume_surge'][:10], ensure_ascii=False)}
ウェブ検索で寄り付き前後のニュースも確認し、予測を評価してください。

JSONのみ返してください（説明文・```不要）:
{{"date":"{today.isoformat()}","session":"830","generated_at":"{datetime.now(JST).strftime('%H:%M')}",
"actual_themes":[{{"name":"","evidence":"50字以内","strength":"high/medium/low"}}],
"evaluation":[{{"predicted_theme":"","hit":true,"accuracy_detail":"50字以内"}}],
"accuracy_score":75,"strongest_theme":"","weakest_theme":"",
"improvement_hints":["",""],"summary":"120字以内"}}"""
    print("[8:30] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_830.json", result)
    log_p = DATA / "accuracy_log.json"
    logs = json.load(open(log_p, encoding="utf-8")) if log_p.exists() else []
    logs.append({"date": result["date"], "accuracy_score": result.get("accuracy_score", 0),
                 "strongest_theme": result.get("strongest_theme", ""), "weakest_theme": result.get("weakest_theme", ""),
                 "improvement_hints": result.get("improvement_hints", [])})
    save("accuracy_log.json", logs[-90:])
    return result

if __name__ == "__main__":
    now_jst = datetime.now(JST)
    today = now_jst.date()
    session = os.environ.get("SESSION", "").strip() or ("730" if now_jst.hour < 8 else "830")
    print(f"SESSION={session} date={today}")
    if not is_trading_day(today):
        print("非営業日 — スキップ"); sys.exit(0)
    if session == "730":
        run_730(today)
    elif session == "830":
        run_830(today)
    else:
        print(f"不明: {session}"); sys.exit(1)
    print(f"[{session}] 完了")
