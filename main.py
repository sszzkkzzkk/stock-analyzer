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
            tomorrow_h  = rec.get("tomorrow_hint", "")
            sign_v      = rec.get("sign_visible_at_600", "")
            sign_h      = rec.get("sign_hidden_at_600", "")
            ctx_parts.append(
                f"  {date}({tags}): 予測={pred_theme} 実際={actual_theme} [{hit_str}]"
                + (f" 教訓={lesson}" if lesson else "")
            )
            if sign_v:
                ctx_parts.append(f"    朝に見えたサイン: {sign_v}")
            if sign_h:
                ctx_parts.append(f"    朝に見えなかったサイン: {sign_h}")
            if tomorrow_h:
                ctx_parts.append(f"    翌朝ヒント: {tomorrow_h}")

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
    """新スキーマ: themes / watchlist / strategy / avoid_themes / skip_rule / summary"""
    required = ["strategy", "themes", "watchlist", "avoid_themes", "skip_rule", "summary"]
    for k in required:
        if k not in obj:
            raise ValueError(f"必須キー不足: {k}")
    s = obj.get("strategy")
    if not isinstance(s, dict):
        raise ValueError("strategyがdictではありません")
    for k in ["market_regime", "market_regime_label", "entry_style", "entry_style_label", "danger_level"]:
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
    """
    6:00 本番画面用プロンプト
    目的: 「今日、何に資金が集まるか」を即断できる情報を生成する
    テーマをA本命/B対抗/C監視の3段階 + 資金集中スコアで評価
    """
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
            "battlefield": "今日の主戦場テーマ名（1つ）",
            "one_line": "今日の相場を一言で（40字以内）"
        },
        "themes": [
            {
                "rank": "A",
                "rank_label": "本命",
                "name": "テーマ名",
                "score": 85,
                "reason": "資金が集まる根拠（60字以内）",
                "continuity": "継続/新規/終息",
                "leader_strength": "強/中/弱",
                "ripple": "広/中/狭",
                "leader_stock": "先導株名",
                "entry_hint": "仕掛けヒント（40字以内）"
            }
        ],
        "watchlist": [
            {
                "bucket": "先導株",
                "name": "銘柄名",
                "code": "1234",
                "theme": "所属テーマ",
                "reason": "テーマ本物度確認の根拠（40字以内）",
                "trigger": "仕掛け条件（40字以内）",
                "invalidation": "失効条件（30字以内）",
                "time_window": "9:00-9:20"
            }
        ],
        "avoid_themes": [{"name": "テーマ名", "reason": "避ける理由（40字以内）"}],
        "skip_rule": "今日絶対やらないこと（60字以内）",
        "summary": "今日の資金集中先まとめ（100字以内）"
    }
    return f"""
You are a Japanese short-term stock trader's AI. Your ONLY job at 6:00 AM:
Identify WHERE capital will concentrate today.

Today is {today_str}.

=== LEARNING CONTEXT (past performance - use this to improve accuracy) ===
{learning_ctx}
=========================================================================

Market data:
{json.dumps(compact_market, ensure_ascii=False)}

PRIMARY MISSION:
Answer "Where will capital concentrate today?" with maximum precision.

Theme evaluation criteria:
- score (0-100): capital concentration probability
  90+: near-certain theme day  80+: strong  70+: possible  below 60: weak
- continuity: 継続(was strong yesterday too) / 新規(new catalyst today) / 終息(fading)
- leader_strength: strength of the leading stock in that theme
- ripple: how many related stocks will follow (広=many, 中=some, 狭=few)

Rank themes:
- A本命: THE theme of the day. Capital will concentrate here. Score 75+.
- B対抗: Strong but secondary. Score 55-74.
- C監視: Possible but uncertain. Score 40-54.
- Max 1 A-rank, max 2 B-rank, max 2 C-rank (total max 5 themes)

Watchlist purpose: NOT just to predict. Use them to VERIFY theme is real.
- 先導株: confirms theme is moving
- 連想1軍: confirms theme is spreading
- 連想2軍: confirms theme has depth
- Max 5 stocks total

Rules:
- Return ONLY valid JSON, no markdown
- Use learning context to AVOID repeating past mistakes
- market_regime: attack/selective/avoid
- market_regime_label: 攻めやすい/選別相場/見送り寄り
- entry_style: breakout/pullback/rebound/skip
- entry_style_label: 初動ブレイク狙い/寄り後押し目待ち/リバ狙い/見送り
- danger_level: 低/中/高
- continuity: 継続/新規/終息
- leader_strength: 強/中/弱
- ripple: 広/中/狭

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_905(today_str, morning_data, market, news):
    """
    9:10 学習ログ用プロンプト
    目的: 朝6:00の仮説が実際にどうだったかを記録する
    売買判断ではなく「仮説検証」が主目的
    """
    morning_themes = [t.get("name","") for t in arr(morning_data.get("themes", morning_data.get("priority_themes", [])))]
    morning_battlefield = morning_data.get("strategy", {}).get("battlefield", "")
    compact = {
        "morning_battlefield": morning_battlefield,
        "morning_themes":      morning_themes,
        "morning_watchlist":   [w.get("name","") for w in arr(morning_data.get("watchlist", []))[:5]],
        "actual_top_gainers":  market["top_gainers"][:10],
        "actual_volume_surge": market["volume_surge"][:8],
        "actual_themes":       market["themes"][:10],
        "news":                news[:8],
    }
    schema = {
        "log": {
            "battlefield_correct": True,
            "predicted_battlefield": "朝に予測した主戦場",
            "actual_battlefield":    "実際の主戦場",
            "theme_accuracy": "的中/部分的中/外れ",
            "what_moved_as_predicted":  "予測通りだったこと（60字）",
            "what_was_different":       "予測と違ったこと（60字）",
            "capital_flow_note":        "資金の実際の流れ（80字）",
            "hint_for_afternoon":       "後場に活かすヒント（60字）",
            "hint_for_tomorrow":        "明日の朝に活かすヒント（60字）"
        },
        "summary": "80字以内"
    }
    return f"""
You are a learning log generator for a Japanese stock trader.
Today is {today_str} 9:10.

Your job: Record how accurate the 6:00 AM prediction was. This is NOT a trading decision.
This log will be used to improve tomorrow's 6:00 prediction.

Data:
{json.dumps(compact, ensure_ascii=False)}

Focus on:
1. Was the predicted battlefield (主戦場) correct?
2. Where did capital actually flow?
3. What hint does this give for tomorrow morning?

Rules:
- Return ONLY valid JSON
- Be specific and factual, not vague
- theme_accuracy: 的中/部分的中/外れ

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_1200(today_str, morning_data, open_data, market, news):
    """
    12:00 学習ログ用プロンプト
    目的: 前場の答え合わせ + 翌朝6:00に活かすヒントの整理
    後場の売買判断ではなく「学習材料の構造化」が主目的
    """
    morning_themes    = [t.get("name","") for t in arr(morning_data.get("themes", morning_data.get("priority_themes",[])))]
    opening_log       = open_data.get("log", {}) if open_data else {}
    compact = {
        "morning_battlefield":  morning_data.get("strategy",{}).get("battlefield",""),
        "morning_themes":       morning_themes,
        "opening_log_summary":  opening_log.get("capital_flow_note",""),
        "actual_top_gainers":   market["top_gainers"][:10],
        "actual_top_losers":    market["top_losers"][:6],
        "actual_volume_surge":  market["volume_surge"][:8],
        "actual_themes":        market["themes"][:10],
        "news":                 news[:8],
    }
    schema = {
        "log": {
            "am_winner_theme":         "前場で本当に強かったテーマ",
            "am_loser_theme":          "前場で弱かったテーマ",
            "new_theme_emerged":       "朝には見えていなかった新規テーマ（なければ空文字）",
            "hypothesis_correction":   "朝の仮説のどこを修正すべきか（80字）",
            "capital_flow_am":         "前場の資金フローの特徴（80字）",
            "sign_that_was_visible":   "朝の時点で見抜けたはずのサイン（60字）",
            "sign_that_was_hidden":    "朝の段階では見えなかったサイン（60字）",
            "hint_for_tomorrow_600":   "明日の朝6:00分析に活かすべき最重要ヒント（80字）"
        },
        "summary": "80字以内"
    }
    return f"""
You are a learning log generator for a Japanese stock trader.
Today is {today_str} 12:00.

Your job: Analyze the morning session and extract lessons for tomorrow's 6:00 prediction.
This is NOT about afternoon trading. Focus on what you can learn for tomorrow.

Data:
{json.dumps(compact, ensure_ascii=False)}

Focus on:
1. What theme actually dominated the morning?
2. What correction is needed to the morning hypothesis?
3. What ONE hint should be used in tomorrow's 6:00 analysis?

Rules:
- Return ONLY valid JSON
- hint_for_tomorrow_600 is the most important field - make it specific and actionable

Required JSON schema:
{json.dumps(schema, ensure_ascii=False)}

Return ONLY JSON.
""".strip()


def build_analysis_prompt_1535(today_str, pred_600, pred_905, pred_1200, market, news):
    """
    15:35 翌朝6:00への教師データ生成プロンプト
    目的: 一日の完全な答え合わせ + 翌朝6:00が使える構造化データを作る
    感想ではなく、明日の精度向上に直結する情報を残す
    """
    morning_themes    = [t.get("name","") for t in arr(pred_600.get("themes", pred_600.get("priority_themes",[])))]
    morning_watchlist = [w.get("name","") for w in arr(pred_600.get("watchlist",[]))]
    log_905  = pred_905.get("log",{})  if pred_905  else {}
    log_1200 = pred_1200.get("log",{}) if pred_1200 else {}
    actual_gainers = [f"{g.get('name','')}({g.get('change','')})" for g in market["top_gainers"][:12]]
    actual_losers  = [f"{l.get('name','')}({l.get('change','')})" for l in market["top_losers"][:6]]

    compact = {
        "morning_prediction": {
            "battlefield":  pred_600.get("strategy",{}).get("battlefield",""),
            "one_line":     pred_600.get("strategy",{}).get("one_line",""),
            "themes":       morning_themes,
            "watchlist":    morning_watchlist,
            "danger_level": pred_600.get("strategy",{}).get("danger_level",""),
        },
        "log_905":  log_905,
        "log_1200": log_1200,
        "actual_results": {
            "top_gainers":    actual_gainers,
            "top_losers":     actual_losers,
            "active_themes":  market["themes"][:10],
            "volume_leaders": [f"{v.get('name','')}({v.get('volume','')})" for v in market["volume_surge"][:8]],
        },
        "news": news[:8],
    }

    schema = {
        "review": {
            # 予測精度の検証
            "theme_hit":              True,
            "predicted_top_theme":    "朝に予測した本命テーマ名",
            "actual_top_theme":       "実際に一番資金が集まったテーマ",
            "theme_match_reason":     "的中/外れの理由（80字）",

            # 朝に見抜けたか/見抜けなかったか
            "sign_visible_at_600":    "朝6:00の時点で見抜けたはずのサイン（60字）",
            "sign_hidden_at_600":     "朝6:00では見えなかったサイン（60字）",
            "fake_theme":             "ダマシだったテーマ（なければ空文字）",
            "fake_reason":            "ダマシの理由（40字）",

            # 先導株の特徴
            "leader_stock_name":      "本日の真の先導株",
            "leader_characteristics": "先導株の特徴（50字）",

            # 次回の6:00に活かすデータ
            "next_ban_rule":          "次回の禁止ルール（具体的に60字）",
            "next_focus":             "明日の朝に注目すべき視点（60字）",
            "key_lesson":             "今日の最重要学習（一文で50字以内）",
            "pattern_tags":           ["相場タグ例:NY下落","円安","原油高"],

            # スコア
            "theme_score":    0,
            "execution_score":0,
            "skip_score":     0,
            "overall_score":  0,
        },
        "tomorrow_hint": "明日の朝6:00分析で最優先で考慮すべきこと（100字以内）",
        "summary": "120字以内"
    }

    return f"""
You are generating structured teaching data for tomorrow's 6:00 AM prediction.
Today is {today_str} 15:35.

Your job: Create a complete answer sheet for today that maximizes tomorrow's prediction accuracy.
This is NOT a summary for humans. This is training data for an AI prediction system.

Data:
{json.dumps(compact, ensure_ascii=False)}

Critical tasks:
1. Was the morning battlefield prediction correct? (theme_hit: true/false)
2. What signs WERE visible at 6:00 that pointed to the correct theme?
3. What signs were NOT visible at 6:00 that would have helped?
4. What was the fake/trap theme today?
5. What is the ONE most important thing to check tomorrow morning? (tomorrow_hint)

Rules:
- Return ONLY valid JSON
- Be brutally honest - wrong predictions help learning more than vague answers
- key_lesson must be usable TOMORROW, not general advice
- tomorrow_hint is the single most important output of this analysis
- pattern_tags: short tags describing today's market condition
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
        "date":                  today.isoformat(),
        "market_tags":           market_tags,
        # 予測精度
        "predicted_top_theme":   review.get("predicted_top_theme", ""),
        "actual_top_theme":      review.get("actual_top_theme", ""),
        "theme_hit":             review.get("theme_hit", False),
        "theme_match_reason":    review.get("theme_match_reason", ""),
        # 朝に見えた/見えなかったサイン
        "sign_visible_at_600":   review.get("sign_visible_at_600", ""),
        "sign_hidden_at_600":    review.get("sign_hidden_at_600", ""),
        "fake_theme":            review.get("fake_theme", ""),
        "fake_reason":           review.get("fake_reason", ""),
        # 先導株
        "leader_stock_name":     review.get("leader_stock_name", ""),
        "leader_characteristics":review.get("leader_characteristics", ""),
        # 次回への学習
        "next_ban_rule":         review.get("next_ban_rule", ""),
        "next_focus":            review.get("next_focus", ""),
        "key_lesson":            review.get("key_lesson", ""),
        "tomorrow_hint":         review.get("tomorrow_hint", ""),
        # スコア
        "theme_score":           review.get("theme_score", 0),
        "execution_score":       review.get("execution_score", 0),
        "skip_score":            review.get("skip_score", 0),
        "overall_score":         review.get("overall_score", 0),
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
            "strategy":      parsed["strategy"],
            "themes":         parsed.get("themes", [])[:5],
            "watchlist":      parsed.get("watchlist", [])[:5],
            "avoid_themes":   parsed.get("avoid_themes", [])[:3],
            "skip_rule":      parsed.get("skip_rule", ""),
            "summary":        parsed.get("summary", ""),
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
                "danger_level": "高",
                "battlefield": "分析失敗", "one_line": "AI分析に失敗。今日は見送り優先。"
            },
            "themes": [], "watchlist": [], "avoid_themes": [],
            "skip_rule": "AI分析失敗日は無理に入らない",
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
            "log":     parsed.get("log", {}),
            "summary": parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[9:10] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "905", "generated_at": now,
            "data_sources": data_sources,
            "log": {
                "battlefield_correct": False, "predicted_battlefield": "",
                "actual_battlefield": "", "theme_accuracy": "外れ",
                "what_moved_as_predicted": "", "what_was_different": "",
                "capital_flow_note": "", "hint_for_afternoon": "",
                "hint_for_tomorrow": f"AI分析失敗のため不明: {e}"
            },
            "summary": f"AI分析に失敗しました: {e}",
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
            "log":     parsed.get("log", {}),
            "summary": parsed.get("summary", ""),
        }
    except Exception as e:
        print(f"[12:00] Claude分析失敗: {e}")
        result = {
            "date": today.isoformat(), "session": "1200", "generated_at": now,
            "data_sources": data_sources,
            "log": {
                "am_winner_theme": "", "am_loser_theme": "", "new_theme_emerged": "",
                "hypothesis_correction": f"AI分析失敗: {e}",
                "capital_flow_am": "", "sign_that_was_visible": "",
                "sign_that_was_hidden": "", "hint_for_tomorrow_600": ""
            },
            "summary": f"AI分析に失敗しました: {e}",
            "analysis_status": "failed",
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
            "review":        review,
            "tomorrow_hint": parsed.get("tomorrow_hint", ""),
            "summary":       parsed.get("summary", ""),
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
