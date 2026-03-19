"""
株式AI自動分析 v2 - 学習システム搭載版
SESSION=600 / 905 / 1200 / 1535

【学習の仕組み】
1. 600: 朝の分析 + 過去の類似パターンをプロンプトに注入
2. 1535: 予測vs実際を詳細に記録、パターンタグ付け、的中率計算
3. 蓄積データ: learning_db.json に全履歴を保存
4. 毎朝: 類似パターンの過去結果を参照して分析精度を上げる
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
import yfinance as yf

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
    body = (
        body.replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
    )
    body = re.sub(r",\s*}", "}", body)
    body = re.sub(r",\s*]", "]", body)
    return json.loads(body)


def call_claude(prompt, max_tokens=2400):
    res = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in res.content if block.type == "text")


def arr(v):
    return v if isinstance(v, list) else []


# ═══════════════════════════════════════════════
# 指標取得（yfinance）
# ═══════════════════════════════════════════════

def yahoo_quote(symbol, label):
    try:
        ticker = yf.Ticker(symbol)
        fast = ticker.fast_info
        price = fast.last_price
        prev = fast.previous_close
        if price is None or prev is None:
            return None
        price = float(price)
        prev = float(prev)
        change = round(price - prev, 2)
        sign = "+" if change >= 0 else ""
        pct = round((change / prev) * 100, 2) if prev != 0 else 0
        pct_sign = "+" if pct >= 0 else ""
        try:
            last_time = getattr(fast, "regularMarketTime", None)
            jst_time = (
                datetime.fromtimestamp(int(last_time), JST).strftime("%H:%M")
                if last_time else "N/A"
            )
        except Exception:
            jst_time = "N/A"
        return {
            "name": label,
            "value": f"{price:,.2f}",
            "value_raw": price,
            "change": f"{sign}{change:,.2f}",
            "change_raw": change,
            "percent": f"{pct_sign}{pct:.2f}%",
            "percent_raw": pct,
            "time": jst_time,
            "source": f"yfinance:{symbol}",
        }
    except Exception as e:
        print(f"yfinance error {symbol}: {e}")
        return None


def fetch_strict_market_quotes():
    quotes = {
        "indices": [], "world_indices": [], "forex": [],
        "futures": [], "sox": [], "oil": [], "search_results": [],
    }
    targets = [
        ("indices",       "^N225",  "日経平均"),
        ("futures",       "NIY=F",  "日経先物"),
        ("forex",         "JPY=X",  "ドル円"),
        ("world_indices", "^DJI",   "NYダウ"),
        ("sox",           "^SOX",   "SOX"),
        ("oil",           "CL=F",   "WTI原油"),
    ]
    ok = 0
    for bucket, symbol, label in targets:
        item = yahoo_quote(symbol, label)
        if item:
            quotes[bucket].append(item)
            quotes["search_results"].append(f"yfinance:{label} 取得成功")
            ok += 1
        else:
            quotes["search_results"].append(f"yfinance:{label} 未取得")
    print(f"strict quotes success: {ok}/{len(targets)}")
    return quotes


# ═══════════════════════════════════════════════
# ニュース・テーマ取得
# ═══════════════════════════════════════════════

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
    ng = [
        r"すでに会員の方はログイン", r"プレミアム会員限定", r"ログイン",
        r"銘柄検索", r"メニュー", r"PC版を表示", r"人気テーマ", r"人気株",
        r"ベスト30を見る", r"お知らせ", r"会員限定", r"^\d+$",
        r"^(TOP|決算|開示|人気|コラム)$", r"日経平均", r"ドル円", r"NYダウ",
        r"上海総合", r"日経先物", r"日経225先物", r"^\s*PR\s*$",
    ]
    for p in ng:
        if re.search(p, text):
            return False
    return True


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
        item = {"code": code, "name": name}
        if mode in ("gainers", "losers"):
            item["price"] = row[idx + 2] if idx + 2 < len(row) else ""
            item["change"] = row[-1] if row else ""
        else:
            item["volume"] = row[-2] if len(row) >= 2 else ""
            item["change"] = row[-1] if row else ""
        parsed.append(item)
    return parsed[:15]


def fetch_kabutan_theme_news():
    result = {"themes": [], "news": [], "top_gainers": [], "top_losers": [], "volume_surge": []}
    try:
        r = safe_get("https://kabutan.jp/")
        soup = BeautifulSoup(r.text, "html.parser")
        theme_candidates = []
        for a in soup.select("a[href*='theme'], a[href*='/themes/']"):
            t = clean_text(a.get_text(" ", strip=True))
            if 2 <= len(t) <= 30:
                theme_candidates.append(t)
        result["themes"] = list(dict.fromkeys(theme_candidates))[:12]
        news_candidates = []
        for a in soup.select("a[href]"):
            t = clean_text(a.get_text(" ", strip=True))
            if is_valid_news_text(t):
                news_candidates.append(t)
        result["news"] = list(dict.fromkeys(news_candidates))[:14]
    except Exception as e:
        print(f"kabutan home error: {e}")

    pages = [
        ("gainers", "https://kabutan.jp/warning/?mode=2_1"),
        ("losers",  "https://kabutan.jp/warning/?mode=2_2"),
        ("volume",  "https://kabutan.jp/warning/?mode=25_1"),
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


def classify_news(news_items):
    classified = {"macro": [], "industry": [], "stock_specific": [], "supply_demand": [], "noise": []}
    for text in news_items:
        t = clean_text(text)
        lower = t.lower()
        if any(k in t for k in ["米国", "金利", "FOMC", "雇用", "CPI", "為替", "原油", "地政学", "中東", "関税"]):
            classified["macro"].append(t)
        elif any(k in t for k in ["半導体", "AI", "電力", "防衛", "資源", "原油", "量子", "データセンター"]):
            classified["industry"].append(t)
        elif any(k in t for k in ["上方修正", "受注", "提携", "承認", "決算", "受賞", "新製品"]):
            classified["stock_specific"].append(t)
        elif any(k in lower for k in ["自社株", "増担", "売り禁", "大量保有", "需給", "ストップ高", "出来高"]):
            classified["supply_demand"].append(t)
        else:
            classified["noise"].append(t)
    for k in classified:
        classified[k] = list(dict.fromkeys(classified[k]))[:6]
    return classified


def fetch_all_market_data():
    strict = fetch_strict_market_quotes()
    kabu = fetch_kabutan_theme_news()
    combined_news = kabu["news"]
    result = {
        "indices":         strict["indices"],
        "world_indices":   strict["world_indices"],
        "forex":           strict["forex"],
        "futures":         strict["futures"],
        "sox":             strict["sox"],
        "oil":             strict["oil"],
        "sector":          [],
        "top_gainers":     kabu["top_gainers"],
        "top_losers":      kabu["top_losers"],
        "volume_surge":    kabu["volume_surge"],
        "themes":          kabu["themes"],
        "news":            combined_news,
        "classified_news": classify_news(combined_news),
        "search_results":  strict["search_results"],
        "source":          "yfinance + kabutan.jp",
    }
    print("=== market summary ===")
    for key in ["indices", "world_indices", "forex", "futures", "sox", "oil",
                "top_gainers", "top_losers", "volume_surge", "themes", "news"]:
        print(f"{key}: {len(result[key])}")

    live_items = []
    for bucket in ["indices", "futures", "forex", "world_indices", "sox", "oil"]:
        live_items.extend(result.get(bucket, []))
    save("market_live.json", {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "items": live_items,
    })
    return result


def build_data_sources_summary(market, nhk, reuters):
    sources = []
    if market.get("indices"):       sources.append(f"日経平均{len(market['indices'])}件")
    if market.get("futures"):       sources.append(f"日経先物{len(market['futures'])}件")
    if market.get("world_indices"): sources.append(f"NYダウ{len(market['world_indices'])}件")
    if market.get("forex"):         sources.append(f"ドル円{len(market['forex'])}件")
    if market.get("sox"):           sources.append(f"SOX{len(market['sox'])}件")
    if market.get("oil"):           sources.append(f"原油{len(market['oil'])}件")
    if market.get("top_gainers"):   sources.append(f"値上がり{len(market['top_gainers'])}件")
    if market.get("top_losers"):    sources.append(f"値下がり{len(market['top_losers'])}件")
    if market.get("volume_surge"):  sources.append(f"出来高急増{len(market['volume_surge'])}件")
    if market.get("themes"):        sources.append(f"テーマ{len(market['themes'])}件")
    if market.get("news"):          sources.append(f"かぶたんニュース{len(market['news'])}件")
    if nhk:                         sources.append(f"NHKニュース{len(nhk)}件")
    if reuters:                     sources.append(f"ロイター{len(reuters)}件")
    return sources


# ═══════════════════════════════════════════════
# 学習データベース
# ═══════════════════════════════════════════════

def load_learning_db():
    db = load("learning_db.json")
    if not db:
        db = {
            "version": 2,
            "records": [],
            "pattern_stats": {},
            "theme_stats": {},
            "total_days": 0,
            "hit_days": 0,
        }
    return db


def save_learning_db(db):
    save("learning_db.json", db)


def tag_market_condition(market):
    tags = []
    dji = next((x for x in market.get("world_indices", []) if x.get("name") == "NYダウ"), None)
    if dji:
        pct = dji.get("percent_raw", 0) or 0
        if pct <= -2:    tags.append("NY大幅下落")
        elif pct <= -1:  tags.append("NY下落")
        elif pct >= 2:   tags.append("NY大幅上昇")
        elif pct >= 1:   tags.append("NY上昇")
        else:            tags.append("NY横ばい")
    sox = next((x for x in market.get("sox", []) if x.get("name") == "SOX"), None)
    if sox:
        pct = sox.get("percent_raw", 0) or 0
        if pct <= -1.5:  tags.append("SOX弱い")
        elif pct >= 1.5: tags.append("SOX強い")
        else:            tags.append("SOX横ばい")
    fx = next((x for x in market.get("forex", []) if x.get("name") == "ドル円"), None)
    if fx:
        chg = fx.get("change_raw", 0) or 0
        if chg >= 0.5:   tags.append("円安")
        elif chg <= -0.5:tags.append("円高")
        else:            tags.append("為替横ばい")
    oil = next((x for x in market.get("oil", []) if x.get("name") == "WTI原油"), None)
    if oil:
        pct = oil.get("percent_raw", 0) or 0
        if pct >= 2:     tags.append("原油高騰")
        elif pct <= -2:  tags.append("原油急落")
    return tags


def find_similar_patterns(db, current_tags, limit=5):
    if not db["records"]:
        return []
    scored = []
    for rec in db["records"]:
        past_tags = set(rec.get("market_tags", []))
        current_set = set(current_tags)
        if not past_tags:
            continue
        overlap = len(past_tags & current_set)
        score = overlap / max(len(past_tags | current_set), 1)
        if score > 0:
            scored.append((score, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def build_learning_context(market):
    db = load_learning_db()
    current_tags = tag_market_condition(market)

    if not db["records"]:
        return "初回実行（学習データなし）", current_tags

    similar = find_similar_patterns(db, current_tags, limit=5)
    theme_stats = db.get("theme_stats", {})
    top_themes = sorted(theme_stats.items(), key=lambda x: x[1].get("hit_rate", 0), reverse=True)[:5]
    bad_themes  = sorted(theme_stats.items(), key=lambda x: x[1].get("hit_rate", 0))[:3]
    recent = db["records"][-10:]
    ban_rules  = [r.get("next_ban_rule", "") for r in recent if r.get("next_ban_rule")][-3:]
    next_focus = [r.get("next_focus", "") for r in recent if r.get("next_focus")][-3:]

    ctx_parts = []
    ctx_parts.append(f"今日の相場タグ: {' / '.join(current_tags) if current_tags else 'なし'}")

    total = db.get("total_days", 0)
    hit   = db.get("hit_days", 0)
    if total > 0:
        ctx_parts.append(f"累計成績: {total}日中{hit}日的中 ({round(hit/total*100,1)}%)")

    if similar:
        ctx_parts.append("【類似パターンの過去結果】")
        for rec in similar[:3]:
            date        = rec.get("date", "")
            tags        = " ".join(rec.get("market_tags", []))
            pred_theme  = rec.get("predicted_top_theme", "不明")
            actual_theme= rec.get("actual_top_theme", "不明")
            hit_str     = "的中" if rec.get("theme_hit") else "外れ"
            lesson      = rec.get("key_lesson", "")
            ctx_parts.append(
                f"  {date}({tags}): 予測={pred_theme} 実際={actual_theme} [{hit_str}]"
                + (f" 教訓={lesson}" if lesson else "")
            )

    if top_themes:
        ctx_parts.append("【過去に強かったテーマ（的中率順）】")
        for name, stat in top_themes[:3]:
            n  = stat.get("count", 0)
            hr = stat.get("hit_rate", 0)
            ctx_parts.append(f"  {name}: {n}回中{round(hr*n)}回的中({round(hr*100)}%)")

    if bad_themes:
        ctx_parts.append("【外れやすいテーマ（注意）】")
        for name, stat in bad_themes[:2]:
            n  = stat.get("count", 0)
            hr = stat.get("hit_rate", 0)
            ctx_parts.append(f"  {name}: 的中率{round(hr*100)}%({n}回)")

    if ban_rules:
        ctx_parts.append(f"直近の禁止ルール: {' / '.join(ban_rules)}")
    if next_focus:
        ctx_parts.append(f"直近の注目点: {' / '.join(next_focus)}")

    return "\n".join(ctx_parts), current_tags


# ═══════════════════════════════════════════════
# バリデーション
# ═══════════════════════════════════════════════

def validate_600_analysis_json(obj):
    required = ["strategy", "priority_themes", "avoid_themes", "watchlist",
                "entry_conditions", "skip_conditions", "danger_patterns"]
    for k in required:
        if k not in obj:
            raise ValueError(f"必須キー不足: {k}")
    s = obj.get("strategy")
    if not isinstance(s, dict):
        raise ValueError("strategyがdictではありません")
    for k in ["market_regime", "market_regime_label", "entry_style", "entry_style_label", "danger_level", "conclusion"]:
        if k not in s:
            raise ValueError(f"strategy 必須キー不足: {k}")
    if s["market_regime"] not in ["attack", "selective", "avoid"]:
        raise ValueError("market_regimeが規定値外")
    if s["market_regime_label"] not in ["攻めやすい", "選別相場", "見送り寄り"]:
        raise ValueError("market_regime_labelが規定値外")
    if s["entry_style"] not in ["breakout", "pullback", "rebound", "skip"]:
        raise ValueError("entry_styleが規定値外")
    if s["entry_style_label"] not in ["初動ブレイク狙い", "寄り後押し目待ち", "リバ狙い", "見送り"]:
        raise ValueError("entry_style_labelが規定値外")
    if s["danger_level"] not in ["低", "中", "高"]:
        raise ValueError("danger_levelが規定値外")
    return True


# ═══════════════════════════════════════════════
# プロンプト構築
# ═══════════════════════════════════════════════

def build_analysis_prompt_600(today_str, learning_ctx, market, key_news):
    compact_market = {
        "us_index":       market["world_indices"][:1],
        "sox":            market["sox"][:1],
        "nikkei_futures": market["futures"][:1],
        "dollar_yen":     market["forex"][:1],
        "oil":            market["oil"][:1],
        "top_gainers":    market["top_gainers"][:8],
        "top_losers":     market["top_losers"][:8],
        "volume_surge":   market["volume_surge"][:6],
        "recent_themes":  market["themes"][:10],
        "news":           key_news[:10],
        "classified_news":market["classified_news"],
    }
    schema = {
        "strategy": {
            "market_regime": "selective",
            "market_regime_label": "選別相場",
            "entry_style": "pullback",
            "entry_style_label": "寄り後押し目待ち",
            "danger_level": "中",
            "conclusion": "120字以内"
        },
        "priority_themes": [{"name": "半導体", "reason": "60字以内", "priority": "A"}],
        "avoid_themes":    [{"name": "材料小型", "reason": "60字以内"}],
        "watchlist": [{
            "bucket": "先導株", "name": "銘柄名", "code": "1234",
            "reason": "50字以内", "trigger": "50字以内",
            "invalidation": "50字以内", "time_window": "9:00-9:20"
        }],
        "entry_conditions": ["60字以内"],
        "skip_conditions":  ["60字以内"],
        "danger_patterns":  ["60字以内"],
        "summary": "160字以内"
    }
    return f"""
You are a Japanese short-term stock trader's personal AI strategy assistant with a learning system.
Today is {today_str}.

Your goal: maximize prediction accuracy through continuous learning.
Use past performance data actively to avoid repeating mistakes.

=== LEARNING CONTEXT (past performance) ===
{learning_ctx}
===========================================

Market data:
{json.dumps(compact_market, ensure_ascii=False)}

Rules:
- Return ONLY valid JSON, no markdown
- Use learning context to AVOID past mistakes
- If similar past pattern exists, reference it in conclusion
- priority_themes max 3, avoid_themes max 3, watchlist max 5
- entry/skip/danger max 3 each
- bucket: 先導株/連想1軍/連想2軍/危険株
- market_regime: attack/selective/avoid
- market_regime_label: 攻めやすい/選別相場/見送り寄り
- entry_style: breakout/pullback/rebound/skip
- entry_style_label: 初動ブレイク狙い/寄り後押し目待ち/リバ狙い/見送り
- danger_level: 低/中/高

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_905(today_str, morning_data, market, news):
    compact = {
        "morning_strategy":        morning_data.get("strategy", {}),
        "morning_priority_themes": morning_data.get("priority_themes", [])[:3],
        "morning_watchlist":       morning_data.get("watchlist", [])[:5],
        "top_gainers":             market["top_gainers"][:10],
        "volume_surge":            market["volume_surge"][:8],
        "recent_themes":           market["themes"][:10],
        "news":                    news[:10],
    }
    schema = {
        "action_judgement": {
            "opening_state":      "予測通りで継続監視",
            "best_current_theme": "テーマ名",
            "best_current_stock": "銘柄名",
            "action_now":         "80字以内",
            "do_not_chase":       "80字以内",
            "morning_accuracy":   "朝予測との比較一言"
        },
        "theme_status": [{"name": "テーマ名", "type": "継続上昇型", "comment": "60字以内"}],
        "summary": "120字以内"
    }
    return f"""
You are a Japanese short-term stock trader's opening action assistant.
Today is {today_str} 9:10.

Compare morning prediction vs actual opening. Note what was right/wrong.

Data:
{json.dumps(compact, ensure_ascii=False)}

Rules:
- Return ONLY valid JSON
- theme_status max 4
- opening_state: 予測通りで継続監視/予測より強く押し目待ち/予測より弱く見送り/予測外テーマ浮上/主役交代中
- theme_status.type: 継続上昇型/寄り天警戒型/一発材料型/指数連動型

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_1200(today_str, morning_data, open_data, market, news):
    compact = {
        "morning_strategy":        morning_data.get("strategy", {}),
        "morning_priority_themes": morning_data.get("priority_themes", [])[:3],
        "morning_watchlist":       morning_data.get("watchlist", [])[:5],
        "opening_action":          open_data.get("action_judgement", {}) if open_data else {},
        "opening_theme_status":    open_data.get("theme_status", [])[:4] if open_data else [],
        "top_gainers":             market["top_gainers"][:10],
        "top_losers":              market["top_losers"][:8],
        "volume_surge":            market["volume_surge"][:8],
        "recent_themes":           market["themes"][:10],
        "news":                    news[:10],
    }
    schema = {
        "afternoon_plan": {
            "status": "修正", "status_label": "朝仮説を修正",
            "pm_regime": "selective", "pm_regime_label": "後場は選別相場",
            "summary": "120字以内"
        },
        "strong_themes_am": ["テーマ名"],
        "pm_core_themes":   ["テーマ名"],
        "drop_themes":      ["テーマ名"],
        "new_watchlist": [{
            "name": "銘柄名", "code": "1234",
            "reason": "50字以内", "trigger": "50字以内",
            "invalidation": "50字以内", "time_window": "12:30-14:00"
        }],
        "do_not_do_pm":        ["60字以内"],
        "entry_conditions_pm": ["60字以内"],
        "skip_conditions_pm":  ["60字以内"],
        "summary":             "120字以内"
    }
    return f"""
You are a Japanese short-term stock trader's afternoon strategy assistant.
Today is {today_str} 12:00.

Rebuild afternoon plan based on actual morning performance.

Data:
{json.dumps(compact, ensure_ascii=False)}

Rules:
- Return ONLY valid JSON
- status: 維持/修正/破棄
- status_label: 朝仮説を維持/朝仮説を修正/朝仮説を破棄
- pm_regime: attack/selective/avoid
- pm_regime_label: 後場は攻めやすい/後場は選別相場/後場は見送り寄り

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_1535(today_str, pred_600, pred_905, pred_1200, market, news):
    morning_themes   = [t.get("name", "") for t in arr(pred_600.get("priority_themes", []))]
    morning_watchlist= [w.get("name", "") for w in arr(pred_600.get("watchlist", []))]
    morning_avoid    = [t.get("name", "") for t in arr(pred_600.get("avoid_themes", []))]
    actual_gainers   = [f"{g.get('name','')}({g.get('change','')})" for g in market["top_gainers"][:10]]
    actual_losers    = [f"{l.get('name','')}({l.get('change','')})" for l in market["top_losers"][:5]]

    compact = {
        "morning_prediction": {
            "regime":       pred_600.get("strategy", {}).get("market_regime_label", ""),
            "entry_style":  pred_600.get("strategy", {}).get("entry_style_label", ""),
            "top_themes":   morning_themes,
            "avoid_themes": morning_avoid,
            "watchlist":    morning_watchlist,
            "danger_level": pred_600.get("strategy", {}).get("danger_level", ""),
        },
        "opening_judgement": pred_905.get("action_judgement", {}) if pred_905 else {},
        "afternoon_plan":    pred_1200.get("afternoon_plan", {}) if pred_1200 else {},
        "actual_results": {
            "top_gainers":  actual_gainers,
            "top_losers":   actual_losers,
            "active_themes":market["themes"][:8],
            "volume_surge": [f"{v.get('name','')}({v.get('volume','')})" for v in market["volume_surge"][:6]],
        },
        "news": news[:8],
    }

    schema = {
        "review": {
            "theme_hit":               True,
            "predicted_top_theme":     "朝に予測した本命テーマ名",
            "actual_top_theme":        "実際に動いたテーマ名",
            "theme_match_reason":      "的中/外れた理由（80字）",
            "watchlist_performance":   "監視銘柄の実際の動き（80字）",
            "best_call":               "今日一番良かった判断（50字）",
            "worst_call":              "今日一番悪かった判断（50字）",
            "actually_best_theme":     "実際に一番動いたテーマ",
            "danger_theme_in_morning": "朝に危険だったテーマ",
            "best_leader_stock":       "本日の主役銘柄",
            "wrong_hypothesis":        "外れた仮説とその理由（100字）",
            "next_ban_rule":           "次回の禁止ルール（80字）",
            "next_focus":              "次回の注目点（80字）",
            "key_lesson":              "今日の最重要学習（60字）",
            "pattern_tags":            ["相場タグ（例:NY下落）"],
            "theme_score":     0,
            "execution_score": 0,
            "skip_score":      0,
            "overall_score":   0,
        },
        "summary": "150字以内"
    }

    return f"""
You are a Japanese short-term stock trader's AI learning assistant.
Today is {today_str} 15:35.

Your job: create a detailed learning record to improve future predictions.
Compare EVERY morning prediction against actual results. Be brutally honest.

Data:
{json.dumps(compact, ensure_ascii=False)}

Critical tasks:
1. Check if predicted themes actually moved (theme_hit: true/false)
2. Identify WHY predictions were right or wrong
3. Extract ONE specific key_lesson usable tomorrow
4. Rate performance honestly (0-100)

Rules:
- Return ONLY valid JSON
- key_lesson must be specific and actionable
- pattern_tags describe today's market (e.g. "NY下落", "円安", "原油高")
- scores: 0-100 integers

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


# ═══════════════════════════════════════════════
# 学習DBの更新
# ═══════════════════════════════════════════════

def update_learning_db(today, review, market_tags, market):
    db = load_learning_db()

    record = {
        "date":               today.isoformat(),
        "market_tags":        market_tags,
        "predicted_top_theme":review.get("predicted_top_theme", ""),
        "actual_top_theme":   review.get("actually_best_theme", review.get("actual_top_theme", "")),
        "theme_hit":          review.get("theme_hit", False),
        "theme_match_reason": review.get("theme_match_reason", ""),
        "best_call":          review.get("best_call", ""),
        "worst_call":         review.get("worst_call", ""),
        "wrong_hypothesis":   review.get("wrong_hypothesis", ""),
        "next_ban_rule":      review.get("next_ban_rule", ""),
        "next_focus":         review.get("next_focus", ""),
        "key_lesson":         review.get("key_lesson", ""),
        "theme_score":        review.get("theme_score", 0),
        "execution_score":    review.get("execution_score", 0),
        "skip_score":         review.get("skip_score", 0),
        "overall_score":      review.get("overall_score", 0),
        "market_snapshot": {
            "nikkei_change": next((x.get("percent","") for x in market.get("indices",[]) if x.get("name")=="日経平均"), ""),
            "dji_change":    next((x.get("percent","") for x in market.get("world_indices",[]) if x.get("name")=="NYダウ"), ""),
            "sox_change":    next((x.get("percent","") for x in market.get("sox",[]) if x.get("name")=="SOX"), ""),
            "fx_change":     next((x.get("percent","") for x in market.get("forex",[]) if x.get("name")=="ドル円"), ""),
        },
    }

    db["records"] = [r for r in db["records"] if r.get("date") != today.isoformat()]
    db["records"].append(record)
    db["total_days"] = len(db["records"])
    db["hit_days"]   = sum(1 for r in db["records"] if r.get("theme_hit"))

    if not db.get("theme_stats"):
        db["theme_stats"] = {}
    pred_theme = review.get("predicted_top_theme", "")
    if pred_theme:
        if pred_theme not in db["theme_stats"]:
            db["theme_stats"][pred_theme] = {"count": 0, "hits": 0, "hit_rate": 0.0}
        db["theme_stats"][pred_theme]["count"] += 1
        if review.get("theme_hit"):
            db["theme_stats"][pred_theme]["hits"] += 1
        stat = db["theme_stats"][pred_theme]
        stat["hit_rate"] = stat["hits"] / stat["count"]

    if not db.get("pattern_stats"):
        db["pattern_stats"] = {}
    for tag in market_tags:
        if tag not in db["pattern_stats"]:
            db["pattern_stats"][tag] = {"count": 0, "hits": 0, "hit_rate": 0.0}
        db["pattern_stats"][tag]["count"] += 1
        if review.get("theme_hit"):
            db["pattern_stats"][tag]["hits"] += 1
        stat = db["pattern_stats"][tag]
        stat["hit_rate"] = stat["hits"] / stat["count"]

    save_learning_db(db)
    total = db["total_days"]
    hit   = db["hit_days"]
    rate  = round(hit / total * 100, 1) if total > 0 else 0
    print(f"学習DB更新: {total}日分 / テーマ的中率 {rate}% ({hit}/{total})")
    return db


# ═══════════════════════════════════════════════
# セッション実行
# ═══════════════════════════════════════════════

def run_600(today):
    now = datetime.now(JST).strftime("%H:%M")
    print("[6:00] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    key_news = (market["news"] + nhk + reuters)[:12]
    learning_ctx, market_tags = build_learning_context(market)
    print(f"[6:00] 相場タグ: {market_tags}")
    today_str = today.strftime("%Y年%m月%d日")

    try:
        print("[6:00] Claude分析中（学習データ参照）...")
        prompt = build_analysis_prompt_600(today_str, learning_ctx, market, key_news)
        raw    = call_claude(prompt, max_tokens=2400)
        save_text("claude_raw_600.txt", raw)
        parsed = parse_json(raw)
        validate_600_analysis_json(parsed)
        result = {
            "date":          today.isoformat(),
            "session":       "600",
            "generated_at":  now,
            "data_sources":  data_sources,
            "market_tags":   market_tags,
            "market_data": {
                "indices":        market["indices"][:6],
                "world_indices":  market["world_indices"][:6],
                "forex":          market["forex"][:4],
                "futures":        market["futures"][:4],
                "sox":            market["sox"][:2],
                "oil":            market["oil"][:2],
                "search_results": market["search_results"][:10],
                "key_news":       key_news[:10],
                "classified_news":market["classified_news"],
            },
            "strategy":         parsed["strategy"],
            "priority_themes":  parsed["priority_themes"][:3],
            "avoid_themes":     parsed["avoid_themes"][:3],
            "watchlist":        parsed["watchlist"][:5],
            "entry_conditions": parsed["entry_conditions"][:3],
            "skip_conditions":  parsed["skip_conditions"][:3],
            "danger_patterns":  parsed["danger_patterns"][:3],
            "summary":          parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[6:00] Claude分析失敗: {e}")
        save_text("analysis_error_600.txt", str(e))
        result = {
            "date":          today.isoformat(),
            "session":       "600",
            "generated_at":  now,
            "data_sources":  data_sources,
            "market_tags":   market_tags,
            "market_data": {
                "indices": market["indices"][:6], "world_indices": market["world_indices"][:6],
                "forex": market["forex"][:4], "futures": market["futures"][:4],
                "sox": market["sox"][:2], "oil": market["oil"][:2],
                "search_results": market["search_results"][:10],
                "key_news": key_news[:10], "classified_news": market["classified_news"],
            },
            "strategy": {
                "market_regime": "avoid", "market_regime_label": "見送り寄り",
                "entry_style": "skip", "entry_style_label": "見送り",
                "danger_level": "高", "conclusion": "AI分析失敗。今日は確認優先。"
            },
            "priority_themes": [], "avoid_themes": [], "watchlist": [],
            "entry_conditions": ["分析失敗日は無理に入らない"],
            "skip_conditions": ["AI分析失敗時は見送り寄り"],
            "danger_patterns": [f"AI分析失敗: {e}"],
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
    save("latest_600.json", result)
    return result


def run_905(today):
    morning = load("latest_600.json")
    if not morning:
        raise FileNotFoundError("latest_600.json なし")
    print("[9:10] データ収集中...")
    market = fetch_all_market_data()
    nhk    = fetch_nhk_news()
    data_sources = build_data_sources_summary(market, nhk, [])
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    try:
        prompt = build_analysis_prompt_905(today_str, morning, market, market["news"] + nhk)
        raw    = call_claude(prompt, max_tokens=1600)
        save_text("claude_raw_905.txt", raw)
        parsed = parse_json(raw)
        result = {
            "date":             today.isoformat(),
            "session":          "905",
            "generated_at":     now,
            "data_sources":     data_sources,
            "action_judgement": parsed["action_judgement"],
            "theme_status":     parsed["theme_status"][:4],
            "summary":          parsed["summary"],
        }
    except Exception as e:
        print(f"[9:10] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "905", "generated_at": now,
            "data_sources": data_sources,
            "action_judgement": {
                "opening_state": "予測より弱く見送り", "best_current_theme": "",
                "best_current_stock": "", "action_now": "", "do_not_chase": "",
                "morning_accuracy": "",
            },
            "theme_status": [], "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_905.txt", str(e))
    save("latest_905.json", result)
    return result


def run_1200(today):
    morning = load("latest_600.json")
    opening = load("latest_905.json") or {}
    if not morning:
        raise FileNotFoundError("latest_600.json なし")
    print("[12:00] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    try:
        prompt = build_analysis_prompt_1200(today_str, morning, opening, market,
                                            market["news"] + nhk + reuters)
        raw    = call_claude(prompt, max_tokens=1800)
        save_text("claude_raw_1200.txt", raw)
        parsed = parse_json(raw)
        result = {
            "date": today.isoformat(), "session": "1200", "generated_at": now,
            "data_sources": data_sources,
            "afternoon_plan":      parsed["afternoon_plan"],
            "strong_themes_am":    parsed["strong_themes_am"][:3],
            "pm_core_themes":      parsed["pm_core_themes"][:3],
            "drop_themes":         parsed["drop_themes"][:3],
            "new_watchlist":       parsed["new_watchlist"][:3],
            "do_not_do_pm":        parsed["do_not_do_pm"][:3],
            "entry_conditions_pm": parsed["entry_conditions_pm"][:3],
            "skip_conditions_pm":  parsed["skip_conditions_pm"][:3],
            "summary":             parsed["summary"],
        }
    except Exception as e:
        print(f"[12:00] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "1200", "generated_at": now,
            "data_sources": data_sources,
            "afternoon_plan": {
                "status": "修正", "status_label": "朝仮説を修正",
                "pm_regime": "avoid", "pm_regime_label": "後場は見送り寄り",
                "summary": f"AI分析に失敗しました: {e}",
            },
            "strong_themes_am": [], "pm_core_themes": [], "drop_themes": [],
            "new_watchlist": [], "do_not_do_pm": ["分析失敗時は後場で無理に増やさない"],
            "entry_conditions_pm": [], "skip_conditions_pm": ["後場は見送り寄り"],
            "summary": f"AI分析に失敗しました: {e}", "analysis_status": "failed",
        }
        save_text("analysis_error_1200.txt", str(e))
    save("latest_1200.json", result)
    return result


def run_1535(today):
    pred_600  = load("latest_600.json")
    pred_905  = load("latest_905.json")
    pred_1200 = load("latest_1200.json")
    if not pred_600:
        raise FileNotFoundError("latest_600.json なし")
    print("[15:35] データ収集中...")
    market  = fetch_all_market_data()
    nhk     = fetch_nhk_news()
    reuters = fetch_reuters_news()
    data_sources = build_data_sources_summary(market, nhk, reuters)
    now       = datetime.now(JST).strftime("%H:%M")
    today_str = today.strftime("%Y年%m月%d日")
    market_tags = pred_600.get("market_tags", tag_market_condition(market))

    try:
        prompt = build_analysis_prompt_1535(today_str, pred_600, pred_905, pred_1200,
                                            market, market["news"] + nhk + reuters)
        raw    = call_claude(prompt, max_tokens=2400)
        save_text("claude_raw_1535.txt", raw)
        parsed = parse_json(raw)
        review = parsed["review"]
        result = {
            "date": today.isoformat(), "session": "1535", "generated_at": now,
            "data_sources": data_sources,
            "review":  review,
            "summary": parsed["summary"],
        }
        # 学習DB更新（最重要）
        update_learning_db(today, review, market_tags, market)

    except Exception as e:
        print(f"[15:35] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "1535", "generated_at": now,
            "data_sources": data_sources,
            "review": {}, "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
        }
        save_text("analysis_error_1535.txt", str(e))
    save("latest_1535.json", result)
    return result


# ═══════════════════════════════════════════════
# メイン
# ═══════════════════════════════════════════════

def main():
    session = os.environ.get("SESSION", "")
    now_jst = datetime.now(JST)
    today   = now_jst.date()

    if not session:
        h = now_jst.hour
        if h < 7:    session = "600"
        elif h < 10: session = "905"
        elif h < 13: session = "1200"
        else:        session = "1535"

    print(f"=== SESSION={session} / {today} ===")

    if not is_trading_day(today):
        print("本日は取引日ではありません。終了します。")
        sys.exit(0)

    if session == "600":
        run_600(today)
    elif session == "905":
        run_905(today)
    elif session == "1200":
        run_1200(today)
    elif session == "1535":
        run_1535(today)
    else:
        print(f"不明なSESSION: {session}")
        sys.exit(1)


if __name__ == "__main__":
    main()
