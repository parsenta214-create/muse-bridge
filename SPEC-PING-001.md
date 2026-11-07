# SPEC-PING-001 — 双方向レイテンシ確認（ping）

- Status: Draft
- Version target: v2.8.0 (code 46)
- Date: 2026-11-08
- Requested by: サイガマン (2026-11-08 01:22 +07) — 接続の流れあたりに通信速度（ping）を表示してほしい。Muse Bridge Android 側から信号を送り、得られる情報を表示する。Muse クライアント（Linux）と Muse Bridge（Android）が双方向から調査できる仕組みを作り、デバッグを円滑化したい。

## 1. 目的

- 接続が遅い・切れるといった不具合のとき、どちらの方向・どの層が悪いのかを切り分けられるようにする。
- 双方向の計測は次の 2 本でそろえる。
  - Muse クライアント → Bridge：すでに存在する（ヘルスチェックが proxy / tailscale / termux_ssh / bridge のレイテンシを計測している）。
  - Bridge → Muse クライアント：本仕様で新設する（Android 側から ping を送り、結果を画面と /state に出す）。
- 画面に表示するのは Bridge 側の計測結果。クライアント側の計測結果はヘルスチェックとダッシュボードにすでにあるため、本仕様ではアプリへの取り込み（/report_latency のような push 用エンドポイント）は対象外とする。将来の拡張候補として §6 に記録する。

## 2. 設計

### 2.1 計測対象（ターゲット）

- Bridge の HTTP サーバーが最後に受けたクライアント接続のリモート IP をターゲットにする。`HttpBridgeServer.handle(Socket)` で `conn.getInetAddress().getHostAddress()` を取り、volatile フィールド `lastClientIp` に保存する。
- ループバック（127.0.0.1）やリンクローカルは対象外とする（ローカルのダッシュボードや Termux からの接続は 127.0.0.1 になるため）。VM は Tailscale 経由でつながるので、想定するターゲットは VM の Tailscale IP（100.x.x.x）である。
- 未確認の前提：リモートアドレスが実際に VM の Tailscale IP になること。実機確認（§5）でターゲット IP を画面で確認する。もし 127.0.0.1 しか見えない場合は、ターゲットの決め方を設計し直す。

### 2.2 計測方法（PingProber、新規クラス）

- まず ICMP ping を試す：`Runtime.exec("/system/bin/ping -c 3 -W 2 <ip>")`。出力の `rtt min/avg/max/mdev = ...` から平均 RTT を読む。送信 3、受信数を記録する。
- ping バイナリがない・失敗した場合はフォールバックとして TCP 接続時間を測る：`<ip>:22` への Socket 接続にかかった時間（接続成功でも拒否でも、そのまでの時間をおおよその RTT とする）。計測方法は結果に記録する（"icmp" / "tcp-connect"）。
- 計測は必ずバックグラウンドスレッドで行う（UI スレッドを止めない）。結果は不変の値オブジェクト（target, method, rttMs, sent, received, measuredAtMs）として保持し、MainActivity と /state の両方から読めるようにする。
- 実行間隔は最低 30 秒あける（スロットリング）。トリガーは次の 2 つ：アクティビティが表示されたとき（onResume → refresh）、ping 行をタップしたとき（手動で再計測）。

### 2.3 表示（MainActivity、フロー図の下）

- フロー図のノードの下に ping 行を追加する（タップ可能）。
- 表示の形：
  - クライアント接続がまだないとき：「まだ Muse 側からの接続がありません」
  - 計測中：「計測中…」
  - 成功：「Muse 側への ping： 12ms（3/3）・たった今」＋ ターゲット IP を小さなグレー文字で併記
  - 失敗：「ping 失敗（<理由>）」＋ ターゲット IP を併記
- 時刻は「たった今」「N 秒前」「N 分前」の形で表示する。色だけでは伝えない（SPEC-UI-001 のルール通り、文字で状態を示す）。

### 2.4 /state への追加

- /state の JSON に `"ping_to_client"` を追加する。計測結果がないときは null。形：`{"target": "100.x.x.x", "method": "icmp", "rtt_ms": 12, "sent": 3, "received": 3, "at": 173...}`（at は epoch ms）。
- これにより VM 側のツール（ヘルスチェック、ダッシュボード）からも Bridge 側の計測結果を読める。双方向の数字が両側から見えるようになる。

## 3. 変更するファイル

- 新規：`android-app/src/dev/muse/bridge/PingProber.java`（計測ロジック、バックグラウンド実行、結果保持）。
- 変更：`HttpBridgeServer.java`（`handle(Socket)` で lastClientIp を記録、/state に ping_to_client を追加）。
- 変更：`MainActivity.java`（ping 行の表示・更新、onResume とタップで再計測を呼ぶ、refresh() から結果を読む）。
- 変更：`res/layout/activity_main.xml`（フロー図の下に ping 行を追加）。
- 変更：`res/values/strings.xml`（ping 行の文言）。
- 変更：`android-app/build.sh`（v2.8.0 / code 46）。
- テスト：`test_android_app_client.py` にガードを追加する（§4）。

## 4. テスト（ソースレベルのガード）

- PingProber が存在し、`/system/bin/ping` を使い、TCP 接続のフォールバックを持ち、バックグラウンドスレッドで動き、30 秒のスロットリングがあること。
- HttpBridgeServer が `getInetAddress()` から lastClientIp を記録し、/state に `ping_to_client` を含むこと。
- レイアウトに ping 行があり、MainActivity が結果を表示し、タップで再計測を呼ぶこと。
- 正直な制限：Python スイートは Java の描画・実行を動かせない（SPEC-UI-002 と同じ）。実際の ping 動作は §5 の実機確認で確かめる。aapt2 のリンク時にレイアウト XML は検証される。

## 5. 実機確認

1. インストール後、フロー図の下に ping 行が表示されること。
2. ping 行のターゲット IP が VM の Tailscale IP（100.x.x.x）であること。127.0.0.1 の場合は設計し直し（§2.1）。
3. RTT が表示されること。値が妥当な範囲であること（Tailscale 経由で数十〜数百 ms を想定。極端に大きい・小さい値は要確認）。
4. ping 行をタップすると再計測され、時刻表示が更新されること。
5. /state に `ping_to_client` が含まれ、画面の表示と一致すること。
6. 画面の表示が固まらないこと（計測がバックグラウンドで動いていること）。

## 6. 対象外・将来の拡張候補

- クライアント側の計測結果をアプリに push するエンドポイント（/report_latency）：本仕様では対象外。クライアント側の数字はヘルスチェックとダッシュボードにある。アプリ画面で両方向を並べて見せたくなったら別仕様で検討する。
- 定期的な自動 ping（常駐の計測）：本仕様では画面表示時とタップ時のみ。常駐化は電池・通信量の観点から別途判断する。
- ping の履歴・グラフ：本仕様では最新 1 件のみ。時系列データは端末内にないため、履歴表示は対象外（SPEC-UI-002 と同じ理由）。
