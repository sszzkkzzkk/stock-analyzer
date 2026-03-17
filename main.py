"""
株式AI自動分析
SESSION=600 / 905 / 1535
個人用・日本株短期売買補助ツール版
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


def call_claude(prompt, max_tokens=2200):
    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in res.content if block.type == "text")


def load_learning_ctx():
    logs = load("review_log.json")
    if not logs:
        return "初回実行"

    recent = logs[-10:]
    useful = [x.get("usefulness", "") for x in recent if x.get("usefulness")]
    easy = [x.get("easy_pattern", "") for x in recent if x.get("easy_pattern")]
    danger = [x.get("danger_pattern", "") for x in recent if x.get("danger_pattern")]
    note = [x.get("next_note", "") for x in recent if x.get("next_note")]

    return (
        f"直近メモ "
        f"使えた判断:{' / '.join(useful[-3:]) if useful else 'なし'} "
        f"取りやすかった型:{' / '.join(easy[-3:]) if easy else 'なし'} "
        f"危険だった型:{' / '.join(danger[-3:]) if danger else 'なし'} "
        f"注意点:{' / '.join(note[-3:]) if note else 'なし'}"
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
        "sox": [],
        "oil": [],
        "search_results": [],
    }

    targets = [
        ("indices", "^N225", "日経平均"),
        ("futures", "NIY=F", "日経先物"),
        ("forex", "JPY=X", "ドル円"),
        ("world_indices", "^DJI", "NYダウ"),
        ("sox", "^SOX", "SOX"),
        ("oil", "CL=F", "WTI原油"),
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
        "sox": strict["sox"],
        "oil": strict["oil"],
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
    print(f"sox: {len(result['sox'])}")
    print(f"oil: {len(result['oil'])}")
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
    if market.get("sox"):
        sources.append(f"SOX{len(market['sox'])}件")
    if market.get("oil"):
        sources.append(f"原油{len(market['oil'])}件")
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


def build_trader_focus_from_decision(result):
    decision = result.get("decision", {}) or {}
    strong = result.get("strong_themes", []) or []
    watch = result.get("watchlist", []) or []

    result["trader_focus"] = {
        "headline": decision.get("conclusion", "") or result.get("summary", ""),
        "top_themes": [x.get("name", "") for x in strong[:3] if x.get("name")],
        "top_stocks": [
            {
                "name": x.get("name", ""),
                "code": x.get("code", ""),
                "reason": x.get("reason", ""),
            }
            for x in watch[:5]
            if x.get("name")
        ],
    }
    return result


def validate_600_analysis_json(obj):
    if not isinstance(obj, dict):
        raise ValueError("rootがdictではありません")

    required = [
        "decision",
        "strong_themes",
        "weak_themes",
        "watchlist",
        "action_plan",
        "risk_notes",
    ]
    for k in required:
        if k not in obj:
            raise ValueError(f"必須キー不足: {k}")

    decision = obj.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("decisionがdictではありません")

    for k in ["stance", "market_tone", "trading_style", "conclusion"]:
        if k not in decision:
            raise ValueError(f"decision 必須キー不足: {k}")

    if decision["stance"] not in ["攻めやすい", "慎重", "見送り寄り"]:
        raise ValueError("stanceが規定値外です")

    if decision["market_tone"] not in ["強め", "中立", "弱め"]:
        raise ValueError("market_toneが規定値外です")

    if decision["trading_style"] not in ["順張り向き", "様子見寄り", "個別材料だけ見る日"]:
        raise ValueError("trading_styleが規定値外です")

    if not isinstance(obj.get("strong_themes"), list):
        raise ValueError("strong_themesがlistではありません")
    if not isinstance(obj.get("weak_themes"), list):
        raise ValueError("weak_themesがlistではありません")
    if not isinstance(obj.get("watchlist"), list):
        raise ValueError("watchlistがlistではありません")
    if not isinstance(obj.get("action_plan"), list):
        raise ValueError("action_planがlistではありません")
    if not isinstance(obj.get("risk_notes"), list):
        raise ValueError("risk_notesがlistではありません")

    return True


def build_analysis_prompt_600(today_str, learning_ctx, market, key_news):
    compact_market = {
        "us_index": market["world_indices"][:1],
        "sox": market["sox"][:1],
        "nikkei_futures": market["futures"][:1],
        "dollar_yen": market["forex"][:1],
        "oil": market["oil"][:1],
        "top_gainers": market["top_gainers"][:8],
        "top_losers": market["top_losers"][:8],
        "volume_surge": market["volume_surge"][:6],
        "recent_themes": market["themes"][:8],
        "key_news": key_news[:8],
    }

    schema = {
        "decision": {
            "stance": "攻めやすい",
            "market_tone": "強め",
            "trading_style": "順張り向き",
            "conclusion": "120字以内"
        },
        "strong_themes": [
            {"name": "半導体", "reason": "60字以内"}
        ],
        "weak_themes": [
            {"name": "材料小型", "reason": "60字以内"}
        ],
        "watchlist": [
            {"role": "本命", "name": "銘柄名", "code": "1234", "reason": "50字以内"}
        ],
        "action_plan": ["寄りで飛び乗らない"],
        "risk_notes": ["GUしすぎなら見送り"],
        "summary": "160字以内"
    }

    return f"""
You are a Japanese stock trader's personal morning assistant.
Today is {today_str}.

Purpose:
- reduce hesitation
- narrow what to watch
- help skip dangerous days
- output only practical trading judgment

Learning context:
{learning_ctx}

Market data:
{json.dumps(compact_market, ensure_ascii=False)}

Important priorities:
- half semiconductor / AI / electric power / defense / geopolitics / oil-resources
- then index heavyweights / supply-demand stocks / material small caps

Rules:
- Return ONLY valid JSON
- No markdown
- No explanation outside JSON
- Keep strings short
- strong_themes: max 3
- weak_themes: max 3
- watchlist: max 5
- action_plan: max 3
- risk_notes: max 3
- watchlist roles should be chosen from 本命 / 次点 / 地合い確認用
- stance must be one of: 攻めやすい / 慎重 / 見送り寄り
- market_tone must be one of: 強め / 中立 / 弱め
- trading_style must be one of: 順張り向き / 様子見寄り / 個別材料だけ見る日

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def run_claude_analysis_600(today, now, market, data_sources, key_news):
    learning_ctx = load_learning_ctx()
    today_str = today.strftime("%Y年%m月%d日")
    prompt = build_analysis_prompt_600(today_str, learning_ctx, market, key_news)

    raw = call_claude(prompt, max_tokens=1800)
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
            "sox": market["sox"][:2],
            "oil": market["oil"][:2],
            "search_results": market["search_results"][:10],
            "key_news": key_news[:10],
        },
        "decision": parsed["decision"],
        "strong_themes": parsed["strong_themes"][:3],
        "weak_themes": parsed["weak_themes"][:3],
        "watchlist": parsed["watchlist"][:5],
        "action_plan": parsed["action_plan"][:3],
        "risk_notes": parsed["risk_notes"][:3],
        "summary": parsed.get("summary", ""),
    }

    return build_trader_focus_from_decision(result)


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
            "sox": market["sox"][:2],
            "oil": market["oil"][:2],
            "search_results": market["search_results"][:10],
            "key_news": key_news[:10],
        },
        "decision": {
            "stance": "慎重",
            "market_tone": "中立",
            "trading_style": "様子見寄り",
            "conclusion": "AI分析に失敗したため、今日は無理に攻めず主要指標とニュース確認を優先。",
        },
        "strong_themes": [],
        "weak_themes": [],
        "watchlist": [],
        "action_plan": ["無理に飛び乗らない", "主要指標確認を優先", "見送り寄り"],
        "risk_notes": [f"AI分析失敗: {reason}"],
        "summary": f"AI分析に失敗しました: {reason}",
        "analysis_status": "failed",
    }


def build_analysis_prompt_905(today_str, theme_names, market, news):
    compact = {
        "predicted_themes": theme_names[:3],
        "top_gainers": market["top_gainers"][:10],
        "volume_surge": market["volume_surge"][:6],
        "recent_themes": market["themes"][:8],
        "news": news[:8],
    }

    schema = {
        "correction_memo": {
            "actual_leader": "テーマ名",
            "gap_from_morning": "80字以内",
            "tradable_now": "80字以内",
            "skip_or_not": "80字以内"
        },
        "summary": "120字以内"
    }

    return f"""
You are a Japanese stock trader's opening memo assistant.
Today is {today_str} 9:05.

Task:
- make a short correction memo
- do not write a report
- focus on action correction only

Data:
{json.dumps(compact, ensure_ascii=False)}

Rules:
- Return ONLY valid JSON
- No markdown
- Keep strings short

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_1535(today_str, pred_600, pred_905, market, news):
    compact = {
        "morning_decision": pred_600.get("decision", {}),
        "morning_strong_themes": pred_600.get("strong_themes", [])[:3],
        "opening_correction": pred_905.get("correction_memo", {}) if pred_905 else {},
        "top_gainers": market["top_gainers"][:10],
        "top_losers": market["top_losers"][:8],
        "volume_surge": market["volume_surge"][:6],
        "recent_themes": market["themes"][:10],
        "news": news[:8],
    }

    schema = {
        "review": {
            "actually_strong": ["テーマ1", "テーマ2"],
            "morning_gap": "100字以内",
            "easy_pattern": "80字以内",
            "danger_pattern": "80字以内",
            "next_note": "80字以内"
        },
        "summary": "120字以内"
    }

    return f"""
You are a Japanese stock trader's end-of-day review assistant.
Today is {today_str} 15:35.

Task:
- write a short memo that helps tomorrow morning
- focus on what was useful, dangerous, and what to note next day

Data:
{json.dumps(compact, ensure_ascii=False)}

Rules:
- Return ONLY valid JSON
- No markdown
- Keep strings short

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


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
    now = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")

    try:
        prompt = build_analysis_prompt_905(
            today_str,
            [x.get("name", "") for x in pred.get("strong_themes", [])],
            market,
            market["news"] + nhk,
        )
        raw = call_claude(prompt, max_tokens=1200)
        save_text("claude_raw_905.txt", raw)
        parsed = parse_json(raw)

        result = {
            "date": today.isoformat(),
            "session": "905",
            "generated_at": now,
            "data_sources": data_sources,
            "correction_memo": parsed["correction_memo"],
            "summary": parsed["summary"],
        }
    except Exception as e:
        print(f"[9:05] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(),
            "session": "905",
            "generated_at": now,
            "data_sources": data_sources,
            "correction_memo": {
                "actual_leader": "",
                "gap_from_morning": "",
                "tradable_now": "",
                "skip_or_not": "",
            },
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
    now = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")

    try:
        prompt = build_analysis_prompt_1535(
            today_str,
            pred_600,
            pred_905 or {},
            market,
            market["news"] + nhk + reuters,
        )
        raw = call_claude(prompt, max_tokens=1200)
        save_text("claude_raw_1535.txt", raw)
        parsed = parse_json(raw)

        review = parsed["review"]
        result = {
            "date": today.isoformat(),
            "session": "1535",
            "generated_at": now,
            "data_sources": data_sources,
            "review": review,
            "summary": parsed["summary"],
        }
    except Exception as e:
        print(f"[15:35] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(),
            "session": "1535",
            "generated_at": now,
            "data_sources": data_sources,
            "review": {
                "actually_strong": [],
                "morning_gap": "",
                "easy_pattern": "",
                "danger_pattern": "",
                "next_note": "",
            },
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_1535.txt", str(e))

    save("latest_1535.json", result)

    logs = load("review_log.json") or []
    review = result.get("review", {})
    logs.append({
        "date": today.isoformat(),
        "usefulness": result.get("summary", ""),
        "easy_pattern": review.get("easy_pattern", ""),
        "danger_pattern": review.get("danger_pattern", ""),
        "next_note": review.get("next_note", ""),
    })
    save("review_log.json", logs[-90:])
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