"""
メインエントリーポイント — GitHub Actions から呼ばれる
分析後、data/ に latest_730.json / latest_830.json を更新する
"""
import os, sys, json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import anthropic, requests
from bs4 import BeautifulSoup
import jpholiday

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
        model="claude-sonnet-4-20250514", max_tokens=2500,
        tools=[{"type":"web_search_20250305","name":"web_search"}],
        messages=[{"role":"user","content":prompt}]
    )
    return "".join(b.text for b in res.content if b.type=="text")

def parse_json(raw):
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m: raise ValueError("JSON not found")
    return json.loads(m.group())

def save(filename, data):
    p = DATA / filename
    with open(p,"w",encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {p}")

def load_learning_ctx():
    p = DATA / "accuracy_log.json"
    if not p.exists(): return "（初回）"
    with open(p,encoding="utf-8") as f: logs = json.load(f)
    if not logs: return "（データなし）"
    recent = logs[-10:]
    avg = sum(r["accuracy_score"] for r in recent)/len(recent)
    weak = list({r.get("weakest_theme","") for r in recent if r.get("weakest_theme")})
    hints = []
    for r in recent[-3:]: hints.extend(r.get("improvement_hints",[]))
    return f"過去{len(recent)}日平均精度:{avg:.0f}点 外れやすいテーマ:{','.join(weak[:3])} 改善ヒント:{' / '.join(hints[-3:])}"

def fetch_yahoo_data():
    H = {"User-Agent":"Mozilla/5.0","Accept-Language":"ja"}
    results = {"top_gainers":[],"volume_surge":[]}
    for key, url in [
        ("top_gainers","https://finance.yahoo.co.jp/stocks/ranking/rateUp?market=tse&term=daily&page=1"),
        ("volume_surge","https://finance.yahoo.co.jp/stocks/ranking/volumeUp?market=tse&term=daily&page=1"),
    ]:
        try:
            r = requests.get(url, headers=H, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("table tbody tr")
            for row in rows[:15]:
                cols = row.select("td")
                if len(cols) < 4: continue
                results[key].append({
                    "code": cols[0].get_text(strip=True),
                    "name": cols[1].get_text(strip=True),
                    "price": cols[2].get_text(strip=True),
                    "change": cols[3].get_text(strip=True),
                })
        except Exception as e:
            print(f"Yahoo取得エラー({key}): {e}")
    return results

def run_730(today):
    ctx = load_learning_ctx()
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')}（東証営業日）です。
昨夜の米国株市場・日経先物・為替・世界ニュースを調べて、本日の東証テーマ予測を行ってください。
{ctx}
JSONのみ返してください:
{{"date":"{today.isoformat()}","session":"730","generated_at":"{datetime.now(JST).strftime('%H:%M')}",
"market_overview":{{"sp500":"","nasdaq":"","dow":"","nikkei_futures":"","usdjpy":"","key_news":["","",""]}},
"themes":[{{"rank":1,"name":"","confidence_score":0,"rationale":"","key_stocks":[],"risk_factors":""}}],
"summary":""}}
themes は confidence_score 降順で5〜7件。"""
    raw = call_claude(prompt)
    result = parse_json(raw)
    save("latest_730.json", result)
    return result

def run_830(today):
    # 7:30予測を読む
    p = DATA/"latest_730.json"
    if not p.exists(): raise FileNotFoundError("latest_730.json がありません")
    with open(p,encoding="utf-8") as f: pred = json.load(f)
    theme_names = [t["name"] for t in pred.get("themes",[])]

    market = fetch_yahoo_data()

    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 8:30です。
7:30予測テーマ: {theme_names}
詳細: {json.dumps(pred.get('themes',[]),ensure_ascii=False)}
Yahoo値上がりTOP15: {json.dumps(market['top_gainers'][:15],ensure_ascii=False)}
出来高急増TOP10: {json.dumps(market['volume_surge'][:10],ensure_ascii=False)}
寄り付き前ニュースも検索して補完し、予測を評価してください。
JSONのみ返してください:
{{"date":"{today.isoformat()}","session":"830","generated_at":"{datetime.now(JST).strftime('%H:%M')}",
"actual_themes":[{{"name":"","evidence":"","strength":"high"}}],
"evaluation":[{{"predicted_theme":"","hit":true,"accuracy_detail":""}}],
"accuracy_score":0,"strongest_theme":"","weakest_theme":"",
"improvement_hints":["",""],"summary":""}}"""
    raw = call_claude(prompt)
    result = parse_json(raw)
    save("latest_830.json", result)

    # accuracy_log 更新
    log_p = DATA/"accuracy_log.json"
    logs = json.load(open(log_p,encoding="utf-8")) if log_p.exists() else []
    logs.append({
        "date": result["date"],
        "accuracy_score": result.get("accuracy_score",0),
        "strongest_theme": result.get("strongest_theme",""),
        "weakest_theme": result.get("weakest_theme",""),
        "improvement_hints": result.get("improvement_hints",[]),
    })
    save("accuracy_log.json", logs[-90:])
    return result

def send_line(msg):
    token = os.environ.get("LINE_NOTIFY_TOKEN","")
    if not token: return
    requests.post("https://notify-api.line.me/api/notify",
        headers={"Authorization":f"Bearer {token}"},
        data={"message":f"\n{msg}"}, timeout=10)

def fmt_730(d):
    ov = d.get("market_overview",{})
    lines = [f"📊 7:30 分析 {d.get('date','')}",
             f"S&P500:{ov.get('sp500','—')} 日経先物:{ov.get('nikkei_futures','—')} USDJPY:{ov.get('usdjpy','—')}",""]
    for t in d.get("themes",[])[:5]:
        lines.append(f"{t['rank']}. {t['name']} {t.get('confidence_score',0)}点 — {t.get('rationale','')[:30]}")
    lines += ["", d.get("summary","")]
    return "\n".join(lines)

def fmt_830(d):
    score = d.get("accuracy_score",0)
    lines = [f"{'🎯' if score>=70 else '📉'} 8:30 評価 {d.get('date','')} 精度:{score}/100"]
    for a in d.get("actual_themes",[])[:4]:
        lines.append(f"{'🔴' if a.get('strength')=='high' else '🟡'} {a['name']} {a.get('evidence','')[:25]}")
    hits = sum(1 for e in d.get("evaluation",[]) if e.get("hit"))
    lines += [f"的中:{hits}/{len(d.get('evaluation',[]))}","", d.get("summary","")]
    return "\n".join(lines)

if __name__ == "__main__":
    now = datetime.now(JST)
    today = now.date()
    session = os.environ.get("SESSION","").strip() or ("730" if now.hour < 8 else "830")
    print(f"SESSION={session} date={today}")

    if not is_trading_day(today):
        print("非営業日 — スキップ"); sys.exit(0)

    if session == "730":
        r = run_730(today)
        send_line(fmt_730(r))
    elif session == "830":
        r = run_830(today)
        send_line(fmt_830(r))
    else:
        print(f"不明セッション: {session}"); sys.exit(1)
