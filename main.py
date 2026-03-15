"""
株式AI自動分析 — main.py
SESSION=730 / 830 / 1600 で呼ばれる
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

def load(filename):
    p = DATA / filename
    if not p.exists(): return None
    with open(p, encoding="utf-8") as f: return json.load(f)

def load_learning_ctx():
    logs = load("accuracy_log.json")
    if not logs: return "（初回）"
    recent = logs[-10:]
    avg = sum(r["accuracy_score"] for r in recent) / len(recent)
    weak = list({r.get("weakest_theme","") for r in recent if r.get("weakest_theme")})
    hints = []
    for r in recent[-3:]: hints.extend(r.get("improvement_hints", []))
    return f"過去{len(recent)}日平均精度:{avg:.0f}点 外れやすいテーマ:{','.join(weak[:3])} ヒント:{' / '.join(hints[-3:])}"

def fetch_yahoo():
    H = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"}
    out = {"top_gainers": [], "volume_surge": [], "top_losers": []}
    urls = {
        "top_gainers": "https://finance.yahoo.co.jp/stocks/ranking/rateUp?market=tse&term=daily&page=1",
        "volume_surge": "https://finance.yahoo.co.jp/stocks/ranking/volumeUp?market=tse&term=daily&page=1",
        "top_losers":  "https://finance.yahoo.co.jp/stocks/ranking/rateDown?market=tse&term=daily&page=1",
    }
    for key, url in urls.items():
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

# ── 7:30 予測 ────────────────────────────────────────────────
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

# ── 8:30 気配検証 ────────────────────────────────────────────
def run_830(today):
    pred = load("latest_730.json")
    if not pred: raise FileNotFoundError("latest_730.json がありません")
    theme_names = [t["name"] for t in pred.get("themes", [])]
    market = fetch_yahoo()
    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 8:30です。
7:30予測テーマ: {theme_names}
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
    return result

# ── 16:00 大引け総括 ─────────────────────────────────────────
def run_1600(today):
    pred_730 = load("latest_730.json")
    pred_830 = load("latest_830.json")
    if not pred_730: raise FileNotFoundError("latest_730.json がありません")

    market = fetch_yahoo()

    themes_730 = pred_730.get("themes", [])
    eval_830   = pred_830.get("evaluation", []) if pred_830 else []

    prompt = f"""今日は{today.strftime('%Y年%m月%d日')} 16:00、東証の大引け後です。
本日の相場を総括してください。

【7:30の予測テーマ】
{json.dumps(themes_730, ensure_ascii=False)}

【8:30時点の評価】
{json.dumps(eval_830, ensure_ascii=False)}

【Yahoo!ファイナンス 大引け値上がりTOP15】
{json.dumps(market['top_gainers'][:15], ensure_ascii=False)}

【値下がりTOP10】
{json.dumps(market['top_losers'][:10], ensure_ascii=False)}

【出来高急増TOP10】
{json.dumps(market['volume_surge'][:10], ensure_ascii=False)}

ウェブ検索で以下を調べてください:
1. 本日の日経平均の終値・変化率
2. 場中に出た重要ニュース（相場に影響したもの）
3. 注目銘柄（7:30予測のkey_stocks）の実際の終値と動き

以下の観点で総括してください:
- 朝の予測テーマは最終的に当たったか
- 注目銘柄は実際にどう動いたか
- 場中のニュースと相場の関係
- 翌日に活かせる学習ポイント

JSONのみ返してください（説明文・```不要）:
{{"date":"{today.isoformat()}","session":"1600","generated_at":"{datetime.now(JST).strftime('%H:%M')}",
"closing":{{"nikkei":"終値と変化率","topix":"終値と変化率","total_assessment":"全体評価ひとこと"}},
"theme_results":[{{"name":"テーマ名","morning_score":85,"final_result":"的中/外れ/部分的中","detail":"50字以内"}}],
"stock_results":[{{"name":"銘柄名","code":"コード","open":"寄り付き","close":"終値","change":"変化率","comment":"30字以内"}}],
"news_impact":[{{"news":"場中ニュース","impact":"相場への影響50字以内"}}],
"final_accuracy_score":75,
"strongest_theme":"最も当たったテーマ",
"weakest_theme":"最も外れたテーマ",
"learning_points":["翌日に活かす学習ポイント1","翌日に活かす学習ポイント2","翌日に活かす学習ポイント3"],
"tomorrow_hint":"明日の相場への示唆100字以内",
"summary":"本日の総括150字以内"}}"""

    print("[16:00] Claude 呼び出し中...")
    result = parse_json(call_claude(prompt))
    save("latest_1600.json", result)

    # 学習ログ更新（16:00の最終スコアで上書き）
    logs = load("accuracy_log.json") or []
    # 同じ日のエントリがあれば更新、なければ追加
    today_str = today.isoformat()
    existing = next((i for i,r in enumerate(logs) if r["date"]==today_str), None)
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

# ── エントリーポイント ────────────────────────────────────────
if __name__ == "__main__":
    now_jst = datetime.now(JST)
    today = now_jst.date()
    session = os.environ.get("SESSION", "").strip() or (
        "730" if now_jst.hour < 8 else
        "830" if now_jst.hour < 9 else
        "1600"
    )
    print(f"SESSION={session} date={today}")
    if not is_trading_day(today):
        print("非営業日 — スキップ"); sys.exit(0)
    if session == "730":
        run_730(today)
    elif session == "830":
        run_830(today)
    elif session == "1600":
        run_1600(today)
    else:
        print(f"不明: {session}"); sys.exit(1)
    print(f"[{session}] 完了")