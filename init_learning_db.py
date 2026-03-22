"""
init_learning_db.py
過去データからlearning_dbの初期値を生成するスクリプト

実行方法:
  python init_learning_db.py

生成物:
  data/learning_db.json  （既存ファイルがあればマージ）

取得するデータ:
  1. kabutanからテーマ一覧
  2. yfinanceで過去1年の相場環境データ（日経・SOX・ドル円・原油）
  3. 相場タグ別×テーマ別の「歴史的傾向」を初期値として設定
"""

import json, os, re, time, datetime
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
import yfinance as yf

JST = datetime.timezone(datetime.timedelta(hours=9))
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def save(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {path}")

def load(filename):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None

def safe_get(url, timeout=15):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; stock-analyzer/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  取得失敗: {url} - {e}")
        return None

# ══════════════════════════════════════════════
# 1. kabutanからテーマ一覧を取得
# ══════════════════════════════════════════════

def fetch_kabutan_themes():
    print("\n[1] kabutanテーマ一覧を取得中...")
    themes = []

    # テーマ別値上がりランキングからテーマを抽出
    urls = [
        "https://kabutan.jp/themes/",
        "https://kabutan.jp/warning/?mode=2_1",
    ]

    for url in urls:
        r = safe_get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if ("theme" in href or "themes" in href) and 2 <= len(text) <= 20:
                if text not in themes:
                    themes.append(text)
        time.sleep(1)

    # フォールバック: 主要テーマを手動定義
    fallback_themes = [
        "防衛・宇宙", "原子力・電力", "半導体", "電線・ケーブル",
        "AIインフラ", "重要鉱物・レアアース", "海運", "資源エネルギー",
        "決算好材料", "内需・サービス", "医療・ヘルスケア", "蓄電池",
        "自動車・EV", "銀行・金融", "建設・インフラ", "化学・素材",
        "食品・飲料", "小売・外食", "不動産", "通信・IT",
        "ゲーム・エンタメ", "観光・インバウンド", "農業・食料安保",
    ]
    for t in fallback_themes:
        if t not in themes:
            themes.append(t)

    print(f"  テーマ数: {len(themes)}件")
    return themes[:40]

# ══════════════════════════════════════════════
# 2. yfinanceで過去1年の相場環境を取得
# ══════════════════════════════════════════════

def fetch_historical_market():
    print("\n[2] 過去1年の相場環境データを取得中...")
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    symbols = {
        "nikkei":  "^N225",
        "dow":     "^DJI",
        "sox":     "^SOX",
        "usdjpy":  "JPY=X",
        "oil":     "CL=F",
        "vix":     "^VIX",
    }

    data = {}
    for key, symbol in symbols.items():
        try:
            df = yf.download(symbol, start=start_date, end=end_date,
                           progress=False, auto_adjust=True)
            if df.empty:
                print(f"  {key}: データなし")
                continue
            closes = df["Close"].dropna()
            # 日次変化率
            pct_changes = closes.pct_change().dropna()
            data[key] = {
                "dates":   [str(d.date()) for d in pct_changes.index],
                "pct":     [round(float(p)*100, 2) for p in pct_changes.values],
            }
            print(f"  {key}: {len(data[key]['dates'])}日分取得")
            time.sleep(0.5)
        except Exception as e:
            print(f"  {key}: エラー - {e}")

    return data

# ══════════════════════════════════════════════
# 3. 相場タグ付け（過去データから）
# ══════════════════════════════════════════════

def tag_historical_day(dow_pct, sox_pct, usdjpy_pct, oil_pct, vix_pct=None):
    """過去の1日に相場タグを付ける"""
    tags = []
    # NYダウ
    if dow_pct <= -1.5:   tags.append("NY急落")
    elif dow_pct <= -0.5: tags.append("NY下落")
    elif dow_pct >= 1.5:  tags.append("NY急騰")
    elif dow_pct >= 0.5:  tags.append("NY上昇")
    else:                 tags.append("NY横ばい")
    # SOX
    if sox_pct <= -2.0:   tags.append("SOX急落")
    elif sox_pct <= -0.5: tags.append("SOX弱い")
    elif sox_pct >= 2.0:  tags.append("SOX急騰")
    elif sox_pct >= 0.5:  tags.append("SOX強い")
    # 為替
    if usdjpy_pct >= 0.5:   tags.append("円安")
    elif usdjpy_pct <= -0.5: tags.append("円高")
    # 原油
    if oil_pct >= 2.0:    tags.append("原油高騰")
    elif oil_pct >= 0.5:  tags.append("原油高")
    elif oil_pct <= -2.0: tags.append("原油急落")
    elif oil_pct <= -0.5: tags.append("原油安")
    # VIX
    if vix_pct and vix_pct >= 10: tags.append("リスクオフ")
    return tags

# ══════════════════════════════════════════════
# 4. 相場タグ別の日本株テーマ傾向（知識ベース）
# ══════════════════════════════════════════════

# 過去の市場の知識から構築した傾向テーブル
# count: その条件で過去に観測された日数の推定値
# 数値は実際の統計ではなく経験則ベースの初期値
TAG_THEME_TENDENCY = {
    "NY急落": {
        "strong_themes": ["内需・サービス", "医療・ヘルスケア", "食品・飲料", "不動産"],
        "weak_themes":   ["半導体", "電線・ケーブル", "海運", "資源エネルギー"],
        "note": "リスクオフで内需ディフェンシブに資金逃避",
    },
    "NY上昇": {
        "strong_themes": ["半導体", "電線・ケーブル", "AIインフラ", "自動車・EV"],
        "weak_themes":   ["不動産", "食品・飲料"],
        "note": "リスクオンで輸出・ハイテクに資金集中",
    },
    "SOX急落": {
        "strong_themes": ["内需・サービス", "銀行・金融", "建設・インフラ"],
        "weak_themes":   ["半導体", "電線・ケーブル", "AIインフラ"],
        "note": "半導体テーマは全面回避",
    },
    "SOX強い": {
        "strong_themes": ["半導体", "電線・ケーブル", "AIインフラ"],
        "weak_themes":   [],
        "note": "半導体・電力インフラに追い風",
    },
    "円安": {
        "strong_themes": ["自動車・EV", "海運", "資源エネルギー", "観光・インバウンド"],
        "weak_themes":   ["小売・外食", "食品・飲料"],
        "note": "輸出関連・訪日外客関連に恩恵",
    },
    "円高": {
        "strong_themes": ["内需・サービス", "食品・飲料", "小売・外食"],
        "weak_themes":   ["自動車・EV", "海運"],
        "note": "内需に資金シフト",
    },
    "原油高騰": {
        "strong_themes": ["資源エネルギー", "海運", "原子力・電力"],
        "weak_themes":   ["航空", "化学・素材"],
        "note": "資源株・エネルギー輸送に資金集中",
    },
    "原油急落": {
        "strong_themes": ["航空", "化学・素材", "小売・外食"],
        "weak_themes":   ["資源エネルギー", "海運"],
        "note": "コスト恩恵セクターへ",
    },
    "リスクオフ": {
        "strong_themes": ["医療・ヘルスケア", "食品・飲料", "不動産", "内需・サービス"],
        "weak_themes":   ["半導体", "海運", "資源エネルギー", "防衛・宇宙"],
        "note": "全面的なリスク回避、ディフェンシブのみ",
    },
    "NY横ばい": {
        "strong_themes": ["決算好材料", "内需・サービス"],
        "weak_themes":   [],
        "note": "個別材料株が主役になりやすい",
    },
}

# ══════════════════════════════════════════════
# 5. learning_dbを構築
# ══════════════════════════════════════════════

def build_initial_learning_db(themes, historical):
    print("\n[3] learning_db初期値を構築中...")

    # 既存DBをロード（あればマージ）
    existing = load("learning_db.json") or {}

    db = {
        "version": 3,
        "records": existing.get("records", []),
        "pattern_stats": existing.get("pattern_stats", {}),
        "theme_stats": existing.get("theme_stats", {}),
        "us_theme_stats": existing.get("us_theme_stats", {}),
        "pts_reaction_stats": existing.get("pts_reaction_stats", {}),
        "total_days": existing.get("total_days", 0),
        "hit_days": existing.get("hit_days", 0),
        "initialized_at": datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "init_note": "kabutanテーマ一覧 + 過去1年相場データから初期化",
    }

    # theme_statsの初期値（kabutanテーマ一覧から）
    for theme in themes:
        if theme not in db["theme_stats"]:
            db["theme_stats"][theme] = {
                "total": 0, "hit": 0, "hit_rate": 0.0,
                "last_seen": "", "note": ""
            }

    # pattern_statsの初期値（相場タグから）
    if historical:
        # 過去1年分の日付を揃える
        dow_map  = {d: p for d, p in zip(historical.get("dow",{}).get("dates",[]),
                                          historical.get("dow",{}).get("pct",[]))}
        sox_map  = {d: p for d, p in zip(historical.get("sox",{}).get("dates",[]),
                                          historical.get("sox",{}).get("pct",[]))}
        fx_map   = {d: p for d, p in zip(historical.get("usdjpy",{}).get("dates",[]),
                                          historical.get("usdjpy",{}).get("pct",[]))}
        oil_map  = {d: p for d, p in zip(historical.get("oil",{}).get("dates",[]),
                                          historical.get("oil",{}).get("pct",[]))}
        vix_map  = {d: p for d, p in zip(historical.get("vix",{}).get("dates",[]),
                                          historical.get("vix",{}).get("pct",[]))}

        all_dates = sorted(set(dow_map) | set(sox_map) | set(fx_map) | set(oil_map))
        tag_counts = {}

        for d in all_dates:
            tags = tag_historical_day(
                dow_map.get(d, 0),
                sox_map.get(d, 0),
                fx_map.get(d, 0),
                oil_map.get(d, 0),
                vix_map.get(d, 0),
            )
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                if tag not in db["pattern_stats"]:
                    db["pattern_stats"][tag] = {
                        "count": 0, "hits": 0, "hit_rate": 0.0,
                        "strong_themes": TAG_THEME_TENDENCY.get(tag, {}).get("strong_themes", []),
                        "weak_themes":   TAG_THEME_TENDENCY.get(tag, {}).get("weak_themes",   []),
                        "note":          TAG_THEME_TENDENCY.get(tag, {}).get("note", ""),
                    }
                db["pattern_stats"][tag]["count"] += 1

        print(f"  タグ集計: {len(tag_counts)}種類")
        for tag, cnt in sorted(tag_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    {tag}: {cnt}日")

    # TAG_THEME_TENDENCYで未登録のタグも追加
    for tag, info in TAG_THEME_TENDENCY.items():
        if tag not in db["pattern_stats"]:
            db["pattern_stats"][tag] = {
                "count": 0, "hits": 0, "hit_rate": 0.0,
                "strong_themes": info["strong_themes"],
                "weak_themes":   info["weak_themes"],
                "note":          info["note"],
            }

    print(f"  pattern_stats: {len(db['pattern_stats'])}件")
    print(f"  theme_stats:   {len(db['theme_stats'])}件")
    return db

# ══════════════════════════════════════════════
# main
# ══════════════════════════════════════════════

def main():
    print("=== learning_db 初期化スクリプト ===")
    print(f"実行日時: {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}\n")

    themes     = fetch_kabutan_themes()
    historical = fetch_historical_market()
    db         = build_initial_learning_db(themes, historical)

    save("learning_db.json", db)

    print(f"""
=== 完了 ===
- pattern_stats: {len(db['pattern_stats'])}件（相場タグ別傾向）
- theme_stats:   {len(db['theme_stats'])}件（テーマ別統計）
- 既存records:   {len(db['records'])}件（日次記録）

次のステップ:
1. data/learning_db.json を GitHub の data/ フォルダにアップロード
2. 通常通り毎朝の自動実行を続ける
3. 30日後から実績ベースの学習が機能し始める
""")

if __name__ == "__main__":
    main()
