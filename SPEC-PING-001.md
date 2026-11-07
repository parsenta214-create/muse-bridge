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

## 7. 実機確認 — 2026-11-08 01:31–01:33 +07（v2.8.0、サイガマンがインストール後に Muse が確認）

- ヘルスチェック：up、全 4 層 ok、アプリ v2.8.0 (46)、a11y true。
- 確認済み（§5）：① フロー図の下に ping 行が表示される（スクリーンショットあり）。② ターゲット IP は 100.84.58.110 — VM の Tailscale IP であり、§2.1 の未確認の前提が正しいことが確認できた。127.0.0.1 ではなかった。③ RTT は 354ms（初回）→ 369ms（再計測）。ICMP（/system/bin/ping が動いた）、3/3 受信。VM 側の bridge レイテンシ（約 471ms）と同じオーダーで妥当。④ ping 行をタップすると再計測され、表示が更新された（at が 1794076313203 → 1794076382643、RTT 354 → 369ms）。⑤ /state の ping_to_client が画面の表示と一致する。⑥ 計測中に画面は固まらなかった（バックグラウンド動作）。
- スクリーンショット：`hidden_files/ping001-check-20261108-0131.jpg`。
- GPT レビュー CONS-20261107-052 は確認時点ではまだ実行中。採用の判断はサイガマンに委ねる。

## 8. 修正ラウンド — CONS-052 PING-01〜PING-11（v2.8.1）

- GPT レビュー CONS-20261107-052 の判定は条件付き採用（P0 なし）。最低限の採用条件 3 点を含む P1×4、P2×4、新規 3 件が出たため、本ラウンドで対応する。
- PING-01（ICMP プロセスの読み取り）：stderr を stdout に統合し（redirectErrorStream）、終了コードを確認し、waitFor にタイムアウト（10 秒、超過時は強制終了）を追加する。正規表現は実機確認で動作済み（354ms を正しく読めた）のため変更しない。
- PING-02（TCP 接続拒否の扱い）：接続拒否は失敗（error="refused"）とする。ただし測定した時間は rttMs に残し、表示の詳細行に「・応答 Xms」として出す。デバッグに必要な情報は失わない。
- PING-03 / PING-09（二重起動と古い結果の上書き）：probing フラグを追加し、計測中は新しい計測を開始しない。スロットリングは lastResult の有無にかかわらず lastProbeStartedMs で判定する。
- PING-04（クライアント以外の IP）：IP の記録をトークンチェックの後に移動する。認証されていないリクエストはターゲットを設定できない。
- PING-05（除外の不完全さ）：文字列の前方一致ではなく InetAddress の isLoopbackAddress / isLinkLocalAddress / isMulticastAddress を使う。169.254/16 や fe80::/10 も除外される。Tailscale IP（100.64/10）はリンクローカルではないので対象のままである。
- PING-06（計測中表示の不整合）：isProbing() を公開し、表示側で新しい計測が走っているときは詳細行に「・計測中」を追加する。表示中の数字は前回の結果であり、経過時間で古さが分かる。
- PING-07（経過時間の時計）：Result に measuredAtElapsedMs（SystemClock.elapsedRealtime）を追加し、経過時間の計算に使う。/state の at は従来通り wall-clock（epoch ms）のままである。
- PING-10（IP と結果の不整合）：setLastClientIp でターゲットが変わったときは lastResult を破棄する。古いターゲットの数字を表示し続けない。
- PING-08（テストが構造チェックのみ）は Python スイートの正直な制限として受け入れる（SPEC-UI-002 と同じ）。PING-11（ICMP 成功時でも TCP フォールバックが必要な場合）は将来の拡張事項として記録のみ。
- バージョン：v2.8.1（code 47）。

## 9. 実機確認 — 2026-11-08 01:40 +07（v2.8.1、サイガマンがインストール後に Muse が確認）

- ヘルスチェック：up、全 4 層 ok、アプリ v2.8.1 (47)、a11y true。
- ping 行は修正後も動いている：ターゲット 100.84.58.110（VM の Tailscale IP — 認証済みリクエストのみ記録する変更の後も正しく記録されている）、ICMP、RTT 287ms（3/3）、表示「Muse 側への ping： 287ms（3/3）・たった今」＋「宛先 100.84.58.110（ICMP）・タップで再計測」。/state の ping_to_client と一致する。ダイヤル、フロー図、カードも従来通り表示されている。
- スクリーンショット：`hidden_files/ping001-check-20261108-0140.jpg`。
- GPT 再レビュー CONS-20261107-053 は確認時点ではまだ実行中。採用の判断はサイガマンに委ねる。

## 10. 修正ラウンド — CONS-053 PING-01 / PING-10 / NEW-01〜NEW-03（v2.8.2）

- GPT 再レビュー CONS-20261107-053 の判定は条件付き採用（P0 なし、P1 1 件、P2 5 件）。PING-02 / PING-03 / PING-04 / PING-05 / PING-06 / PING-07 は解決と判定され、PING-09 は概ね解決。残った P1 1 件と P2 の新規 3 件に対応する。
- PING-01（ICMP タイムアウトが実質機能しない）：readLine() で出力を読む処理が先にあり、waitFor(10 秒) に到達できない。出力の読み取りを専用スレッド（ping-reader、daemon）に分離し、計測スレッド側で waitFor のタイムアウトを待つようにする。タイムアウト時はプロセスを強制終了する。読み取り完了は join(2000) で待つ。
- PING-10（ターゲット変更時の結果の復活）：計測スレッドが完了時に、計測開始時のターゲットが現在の lastClientIp と一致する場合にのみ結果を保存する。一致しない場合は結果を破棄し、新しいターゲットで改めて計測を開始する（probe(null) を呼ぶ）。保存の判定と probing フラグの解除は synchronized ブロックの中で行い、setLastClientIp との競合をなくす。
- NEW-01（ターゲット変更後のスロットリング）：setLastClientIp でターゲットが変わったときに lastProbeStartedMs を 0 にリセットする。新しいターゲットはすぐに計測できる。
- NEW-02（スロットリングの時計）：lastProbeStartedMs の比較を SystemClock.elapsedRealtime() に統一する（wall-clock からの変更）。
- NEW-03（認証順序テストの検証方法）：テストのアンカーをトークンの取得（tokens.currentToken()）から 401 拒否（"unauthorized"、ファイル内に 1 か所のみ）に変える。記録が認証拒否の後に置かれていることをより直接に確認する。
- PING-08（テストが構造チェックのみ）は受け入れたまま。PING-11（将来の拡張）は記録のみ。
- バージョン：v2.8.2（code 48）。
