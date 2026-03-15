# 株式AI自動分析ダッシュボード

スマホで毎朝URLを開くだけ。PCなし・インストールなし。

## セットアップ（10分）

### 1. GitHubリポジトリ作成
https://github.com/new → 名前: `stock-analyzer` → **Public** → Create

※ GitHub Pages 無料利用のため Public にする

### 2. このファイルを全部アップロード
```
index.html
main.py
requirements.txt
.github/workflows/analyze.yml
data/（フォルダごと）
```
GitHub の「Add file → Upload files」でドラッグ&ドロップでOK

### 3. GitHub Secrets 登録
Settings → Secrets and variables → Actions → New repository secret

| 名前 | 値 |
|---|---|
| `ANTHROPIC_API_KEY` | AnthropicのAPIキー |
| `LINE_NOTIFY_TOKEN` | LINE Notifyのトークン（任意） |

### 4. GitHub Pages を有効化
Settings → Pages → Source: **Deploy from a branch** → Branch: **gh-pages** → Save

### 5. 初回テスト実行
Actions → 株式AI自動分析 → Run workflow → session: `730` → Run

### 6. スマホのホーム画面に追加
ブラウザで `https://あなたのGitHubユーザー名.github.io/stock-analyzer/` を開く
→ 共有ボタン → 「ホーム画面に追加」

### 7. ダッシュボードの設定
「設定」タブでGitHubユーザー名とリポジトリ名を入力 → 保存して接続

---

## 毎日の流れ

```
7:30 自動  世界ニュース・先物を分析 → ダッシュボード更新 + LINE通知
8:30 自動  Yahoo気配を確認 → 予測評価 → 学習DB更新 + LINE通知
毎朝      スマホでURLを開くだけで最新の分析が表示される
```

## URL
```
https://あなたのユーザー名.github.io/stock-analyzer/
```
