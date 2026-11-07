# AGENTS.md — Muse Bridge

Muse（AI）が Android スマホ（Galaxy A55 5G）を遠隔操作するための橋渡しアプリと、その VM 側クライアントのリポジトリ。最終決定者は人間のユーザー（Saardeim / サイガマン）。

## このリポジトリは何か

- Android アプリ（`android-app/`）：AccessibilityService で画面取得・タップ・入力を行い、Tailscale 経由の HTTP サーバー（ポート 8080）で VM からの指示を受ける。パッケージ `dev.muse.bridge`、pure Java、依存ライブラリゼロ。
- VM 側 Python クライアント（ルートの `android_bridge.py` など）：同じ HTTP API を叩く。テスト付き。
- 元は Google の computer-use-preview のコピーで、Phase 1〜3（Gemini Computer Use）の成果物も含む。README の先頭は上流の説明のまま。Muse Bridge の本体は README の途中（`Local trial setup` 以降）と `android-app/` にある。

## 地図

- `README.md`：上流の説明＋ Muse の作業記録。末尾近くに番号付きの凍結決定（1〜97）がある。決定の追加は末尾の作法を守ること（下記）。
- `MUSE_BRIDGE_OVERVIEW.md`：日本語の概要。「スマホに手足を生やすアプリ」という説明。
- `ROADMAP.md`：Phase D-1 完了、D-2/D-3 未着手。
- `SPEC-*.md`：凍結仕様書（P0 / P1 / CONS-014 P1 / 操作通知 / ウィジェット / PING / LOG / DRIVE など）。凍結済みのものは直接編集しない。
- `android-app/`：アプリ本体。`build.sh`（手動 aapt2 / javac / d8 パイプライン、Gradle なし）、`src/dev/muse/bridge/`（Java 15 ファイル）、`res/`、`AndroidManifest.xml`、`README.md`。
- ルートの Python：`android_bridge.py`（クライアント）、`bridge_protocol.py`（プロトコル・検証）、`retry.py`、`conftest.py`、`test_*.py`（テスト）。
- `termux/`：電話側の Termux 起動スクリプト。`termux_exec.sh` / `push_apk.sh`：VM から電話へ SSH・APK 転送する道具。
- `.github/workflows/build.yml`：GitHub Actions（APK ビルド＋テスト）。手動実行のみ。
- `LICENSE`：Apache-2.0。

## ビルドとテスト（VM 側）

作業コピーは `~/workspace/computer-use-preview/`。このリポジトリの staging は `~/workspace/muse-bridge/`（push 用の写し）。

```bash
cd ~/workspace/computer-use-preview
.venv/bin/python -m pytest -q          # テスト（最終確認 299 passed、2026-11-08、v2.8.6 時点）
cd ~/workspace/computer-use-preview/android-app
./build.sh                              # APK ビルド。VERSION_CODE / VERSION_NAME は build.sh の 19〜20 行目
```

- ビルドはバイト単位で再現しない（同じソースでも SHA が変わる）。SHA は毎回読み直すこと。
- `.env`（トークン等）は chmod 600、git 管理外。トークンをチャット・ログ・ソースに書かない。

## 同期と push の手順

1. 変更は `~/workspace/computer-use-preview/` で行い、変わったファイルを `~/workspace/muse-bridge/`（staging）に写す。`android-app/build.sh` は毎回写す。`.env.example` は写さない。
2. 新しいファイルは `~/workspace/muse-bridge.paths`（manifest）にも追加する。追加し忘れると push から漏れる。manifest は NUL 区切りで、末尾の NUL が必須。
3. push は `github push-files`（owner `parsenta214-create`、repo `muse-bridge`、branch `main`）。出力の本物の sha を読んでから同期記録 `~/workspace/muse-bridge.muse-github-snapshot-sync.json` に追記する。
4. staging と GitHub HEAD が食い違うことがある（別チャットの作業が未 push のまま staging にある）。全体 push の前は必ず差分を確認する。疑わしいときは、対象ファイルだけの別 paths ファイルで単独 push する。
5. `.github/workflows/` 配下のファイルは push-files でも gh でも置けない（`workflow` 権限がないため 403 になる）。GitHub Web UI からブラウザ経由で作る。

## 凍結済みのもの（変えるときは新規バージョンを作る）

- P0（v2.0 起動仕様、v2.2.0）：HTTP サーバーと AccessibilityService の分離、モード（MINIMAL / FULL_ACCESS / BANKING_SAFE）、モジュール別 Health、P0-4（`/launch_app` の正直な報告＋銀行アプリの拒否リスト）。
- P1（CONS-022 の 5 項目、v2.3.0）：UI と Health の一致、tailscale_ips の再取得、`install_apk_permitted`、Health の相互検証。
- CONS-014 P1（6 項目、v2.5.0）：ログの PII 除去、`/scroll_node` の direction 必須、`/node_action` の引数検証など。
- 操作通知（v2.1.0）、ウィジェット（v2.6.0、修正ラウンドは停止中）、step 3 P2 丸め（凍結判断待ち）。

## 触ってはいけないもの・禁則

- `debug.keystore` を GitHub に置かない。署名鍵が変わると更新インストールが壊れる（アンインストール→トークン消失）。取り出せるコピーは Drive の 書斎/設備管理/`muse-bridge-debug.keystore`。GitHub Secret（`DEBUG_KEYSTORE_B64`）は Actions が使うだけで、中身は取り出せない。
- トークン・鍵・パスワードをチャット・ログ・ソース・ファイル名に出さない。
- main ブランチは保護あり：force-push と削除は禁止。直 push はできる。PR・レビューは不要。
- README の番号付き決定を追加するときは、直前の決定の本文の末尾（または次の `###` 見出し）を起点にする。見出し行を起点にすると本文が置き去りになる（過去に 2 回やった）。
- ファイルの移動・名前変更はしない。manifest・同期記録・凍結仕様の参照が壊れる。
- 公開・連絡・支出・削除・設定変更は、ユーザーの exact な承認があるまで止まる。

## 電話を操作するとき

- VM 側から画面を変える操作（タップ・スワイプ・アプリ起動など）の前は、ユーザーに先に知らせる。読み取りだけの確認（`/health`、`/state`、`/logs`、通知）は知らせなくてよい。
- 銀行アプリ（K PLUS、Bualuang mBanking、SCB EASY、KMA）は `/launch_app` の拒否リスト入りで 403 になる。Krungthai NEXT も対象だが、コードには未追加。
- Termux の sshd が落ちていると VM からの SSH は通らない。`/repair_termux` で直せる（`allow-external-apps=true` が `~/.termux/termux.properties` に必要）。

## 現在の状態（2026-11-08）

- GitHub HEAD は v2.8.5/code 51。電話は v2.8.6/code 52（メインの部屋の作業、push 待ち）。
- GitHub Actions は登録済みだが未実行（手動のみ）。走らせるのはユーザーの合図があったときだけ。
- ライセンス Apache-2.0。トピック：`android`、`accessibility-service`、`tailscale`、`termux`。
