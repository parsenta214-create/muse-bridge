# SPEC-LOG-001 — ロガー改良

- Status: Draft
- Version target: v2.8.6 (code 52)
- Date: 2026-11-08
- Requested by: サイガマン (2026-11-08 04:09 +07) — まずロガーの改良。2026-11-08 の接続障害（03:32–04:07、VM からブリッジに届かなくなった）の原因特定が、ログの読みにくさで遅れたため。
- Revision: v5 (2026-11-08) — GPT レビュー CONS-20261108-062（条件付き採用、P0×2、P1×6、P2×3）に対応。対応表は付録 A を参照。

## 1. 目的

- 接続障害のとき、端末側のログだけで「サーバーがどのアドレスで listen しているか」「いつ・なぜ再起動したか」「VM から届いているか」を切り分けられるようにする。
- ログの量を減らし、サーバー関連の行が通知行に埋もれないようにする。

## 2. 現状の問題（証拠つき）

- LOG-01 時刻順でない：`BridgeLog.renderText()` は現行の行を先に、前回分を「--- previous run ---」の後に置く。前回分は現行より古いため、エクスポート全体は時刻順にならない。証拠：`bridge-log-20261108-040306_10_94ud.txt` は 03:42 から始まり 02:18 で終わる。
- LOG-02 listen 行にインターフェース名がない：`HttpBridgeServer.java:451` は `BridgeLog.log("listen", addr + ":" + port)` のみ。`sanitize()` が IP を 2 オクテットにマスクするため、「listen 100.68.*.*:8080」では Tailscale なのかキャリア CGNAT なのか分からない。今回の障害（03:44:11 に 100.68.*.* だけに bind し、Tailscale IP 100.111.74.42 には bind しなかった）の特定が遅れた。
- LOG-03 STOPPED に理由がない：`HttpBridgeServer.java:520` は `BridgeLog.log("server", "STOPPED")` のみ。03:44 の再起動の理由がログから分からない。
- LOG-04 通知の大量行：`BridgeNotificationListener.java:111,117` は通知のたびに `BridgeLog.log("notification", ...)` を呼ぶ（スロットルなし）。306 行のうち大半が通知行で、サーバー行が埋もれる。
- LOG-05 VM からの到達がログに残らない：VM の CONNECT はプロキシ／TCP 層で失敗するためアプリには届かず、ログにも残らない。アプリが受けた接続の記録がないため、「VM から届いているか」がログから分からない。
- BIND-01（根本原因・ログ以外）：`bindAddresses()`（`HttpBridgeServer.java:527`）は `MainActivity.isTailscaleIp()`（100.64.0.0/10）で判定するが、100.64/10 は Tailscale とキャリア CGNAT の両方に使われるため区別できない。03:44:11 には 100.68.*.*（CGNAT と推定）に bind し、Tailscale IP には bind しなかった。ログ改良だけでは再発を防げないため、bind の選択も本 SPEC に含める。切り離す場合は別 SPEC にする。

## 3. 変更内容

### 3.1 LOG-01 エクスポートを時刻順にする

- `BridgeLog.renderText()` で、前回分の tail を先に、現行の行を後に並べる（全体が古い→新しい順になる）。区切り「--- previous run ---」は残すが、先頭側に移す。ファイル・リングの保持量（200 行／64 KB）は変えない。
- 各セクションに実行開始時刻を付ける（例：`--- previous run (started 2026-11-08 01:51:00) ---`）。実行順と壁時計の時刻順を混同しないため、セクションの順序は実行順（前回→現行）で固定し、時刻は参考表示とする。
- エクスポートの時刻順保証は、端末時計が正常に進んでいる場合に限る（時計が戻ると順序が崩れることを明記する）。
- 実行開始時刻の永続化（P1-H）：開始時刻をログ本文とは別の永続化メタデータ（`run-meta` ファイル）として保存し、ログのリングバッファと開始時刻の保存領域を分離する。起動時手順を定義する：(1) `run-meta` から前回の currentStartTime を読み、previousStartTime として確定する、(2) 新しい currentStartTime を取得する、(3) 両方を保存する、(4) 保存完了後にログ記録を開始する。破損に強い保存方法として、一時ファイルへの書き込み＋原子的な置換（rename）を使う。`renderText()` はこのメタデータから前回セクションの開始時刻を読み、メタデータがない場合（旧バージョンのファイル）は "unknown" と表示する。

### 3.2 LOG-02 listen 行にインターフェース名を付ける

- `start()` の bind ループで、アドレスとともにインターフェース名を記録し、listen 行を `listen <ifname> <masked-ip>:<port>` の形にする（例：`listen tailscale0 100.111.*.*:8080`）。インターフェース名は IP ではないため `sanitize()` のマスク対象外で、そのまま残る。
- インターフェース一覧と bind 結果は分離して記録する（P1-B）：
  - インターフェース一覧の取得元：`NetworkInterface.getNetworkInterfaces()`（java.net。非推奨ではない）で全インターフェースを列挙する。VPN ネットワークのインターフェース名とアドレスは、§3.6 の NetworkCallback 経由で取得した `LinkProperties` から取る。両者の役割を混同しない。`NetworkInterface` と `LinkProperties` の結果を同一情報として扱わない。両者が一致しなくても、VPN 判定には NetworkCallback 側（`LinkProperties`）の情報を使う。
  - 両者でアドレスが不一致の場合は、ログにその旨を表示する（例：`ifaces-mismatch NetworkInterface=100.111.*.* LinkProperties=100.68.*.* (using LinkProperties)`）。不一致があっても判定は NetworkCallback 側で行う。
  - `LinkProperties.getInterfaceName()` が null を返す場合は、インターフェース名を "unknown" として記録する（例：`listen unknown 100.111.*.*:8080`）。
  - 起動時に、列挙したインターフェース一覧を 1 行で記録する（名前＋マスク済み IP。例：`ifaces wlan0:192.168.*.*, rmnet_data0:100.68.*.*, tailscale0:100.111.*.*`）。一覧には §7 のマスク規則を適用する。
  - bind 結果の要約行を 1 行追加する。件数は bound／excluded／failed に分離して記録する（例：`bind-summary bound=2 excluded=1 failed=0`）。bound にはループバックを含むか明記する（本 SPEC では含める。`bound=2 (tailscale0:100.111.*.*, lo:127.0.0.1)` の形で内訳も示す）。bind しなかったアドレスは理由付きで記録し（例：`excluded=1 (rmnet_data0:100.68.*.* not-vpn-iface)`）、bind を試みて失敗したアドレスも IP をマスクしたうえで理由を記録する。
- マスク方針は変えない（IP は §7 の規則に従う）。トークン・要求ボディ・ノードテキストは引き続きログに出さない。

### 3.3 LOG-03 server STOPPED に理由を付ける

- `stop()` に理由を渡せるようにし、`BridgeLog.log("server", "STOPPED: <reason>")` の形で記録する。理由は列挙型とする：MANUAL（手動停止）／SERVICE_DESTROY（サービス破棄）／WIDGET（ウィジェット操作）／RESTART（再起動のため）／ERROR（エラーによる停止）／UNKNOWN（不明）。
- ERROR の場合は安全な範囲でエラー分類を付加するが、例外メッセージはそのまま出さない（§7）。
- 停止理由の競合処理（P1-C）：最初に確定した停止理由を保持し、後からの `stop()` 呼び出しで理由が上書きされないようにする。複数スレッドからの同時 `stop()` に備え、`AtomicReference<StopReason>`＋`compareAndSet()` で理由の確定を原子化する。停止理由の確定と STOPPED の記録回数は別問題のため、停止完了処理の実行権も `AtomicBoolean` 等で原子的に確保し、STOPPED は 1 回だけ記録する。記録に失敗した場合は 1 回だけ再試行し、それでも失敗した場合は状態遷移を優先する（サーバーは停止する）。その場合は次回起動時に `previous STOPPED record failed` を記録する。

### 3.4 LOG-04 通知行をスロットルする

- `BridgeNotificationListener.java:111,117` の `BridgeLog.log("notification", ...)` を `BridgeLog.logThrottled()` に変える（キー "notification"）。動作定義：
  - 最初の通知は即時記録する。
  - その後 10 秒間は抑制し、間引いた件数を数える。
  - 次の記録時に抑制件数を "(+N suppressed)" で表示する。
  - 時間判定には単調増加時計（`SystemClock.elapsedRealtime()`、既存の `logThrottled()` と同じ）を使用する。
  - スレッドセーフにする（既存の `logThrottled()` は synchronized のため、そのまま使う）。
- 停止・保存処理の順序を固定する（P1-D）：(1) 新規通知の受付を停止する、(2) 実行中の通知処理の終了を待つ、(3) 残った抑制件数を flush する、(4) サーバーを停止する、(5) STOPPED を記録する、(6) ログを確定・保存する。
- 実行中の通知処理の終了待機には上限時間（5 秒）を設ける。超過した場合は、未完了の存在を記録しつつ、flush・サーバー停止・ログ保存を続行する（停止処理が無限に待たないようにする）。
- flush の出力形式を明記する（例：`notification (+12 suppressed, final flush)`）。
- 「listener connected / disconnected」（`:97,103`）は稀で重要なためスロットルしない。

### 3.5 LOG-05 新規リモートからの初回接続を記録する

- `HttpBridgeServer` の接続処理で、リモート IP ごとに初回だけ `BridgeLog.log("client-first", <masked-ip>)` を記録する。定義：
  - 重複判定は実際の IP をメモリ内で使い、ログに出す際だけマスクする（マスク済み IP をキーにすると 100.111.10.10 と 100.111.20.20 が同一視されるため）。
  - ポート番号は記録しない。
  - 実行ごとに判定情報を初期化する。保持数の上限は 64 件とし、上限を超えたら最も古いものから FIFO で破棄する。FIFO は純粋な挿入順とする（アクセス順ではない）。
  - IPv4-mapped IPv6（例：`::ffff:100.111.74.42`）は IPv4 に正規化してから判定する。
  - 記録タイミングは TCP 接続の accept 成功時と明記し、HTTP 成功とは区別する。
  - ログ名は「client-first 100.111.*.*」に統一する。
  - 同時接続時のスレッドセーフ性を確保する（判定用の集合へのアクセスを同期する）。
  - 上限を超えて破棄された IP が再接続した場合は、再び client-first が記録されることを仕様上許容する。
- 毎リクエストの記録はしない（既存の方針「never every request」を維持）。

### 3.6 BIND-01 bind は VPN ネットワークの状態で Tailscale を選ぶ（根本原因の修正）

- 問題：`bindAddresses()` は `MainActivity.isTailscaleIp()`（100.64.0.0/10）だけで判定するが、100.64/10 は Tailscale とキャリア CGNAT の両方に使われる。インターフェース名（"tailscale0"）での判定も採用できない。Android の Tailscale は VpnService.Builder で TUN インターフェースを確立し、名前は "tailscale0" とは限らず "tun0" などになる可能性があるため。
- 運用前提：Tailscale を端末の唯一の VPN とする。Android は同一ユーザー／プロファイルで原則 1 つの VPN しか同時利用できないため、複数の VPN ネットワークが存在する場合は想定外とし、fail-closed とする。
- 採用する判定：Android の `ConnectivityManager` で VPN ネットワークを特定する。Tailscale は他アプリ向けの公開 API を提供していないため、Android 自身の VPN ネットワーク状態を情報源とする。`getAllNetworks()` は API 31 で非推奨のため使わない。ネットワーク選択とアドレス選択を分離した 8 段階の処理順序とする（P0-01）：
  1. `NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_VPN).removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN).build()` で VPN 向けの要求を作る。`NetworkRequest.Builder` はデフォルトで `NET_CAPABILITY_NOT_VPN` を要求するため、TRANSPORT_VPN を追加しただけでは VPN ネットワークを取得できない。`clearCapabilities()` ではなく NOT_VPN だけを除去する方が意図が明確なため、こちらを使う。
  2. `ConnectivityManager.registerNetworkCallback(request, callback)` でコールバックを登録する。登録成功フラグを持ち、登録前に例外が発生した場合はコールバックなしで DEGRADED とする（安全処理）。
  3. コールバックの `onAvailable(network)`／`onLinkPropertiesChanged(network, linkProperties)`／`onLost(network)` で、VPN ネットワークの情報を Network ごとに収集する。`onLost(network)` が呼ばれた場合は、その Network の情報を収集領域から削除する。判定確定前は削除を反映し、確定後は無視する。削除の反映とスナップショットの確定は同期する。
  4. 最大 2000 ms まで待つ。2000 ms は取得保証時間ではなく、アプリ側の待機上限として定義する。計測には単調増加時計（`SystemClock.elapsedRealtime()`）を使う。待機をメインスレッドで実行しない。
  5. 2000 ms 経過時点で、取得済みの情報を一貫した状態で確定する（スナップショット）。受付終了とスナップショット確定は原子的に行う。確定後に遅れて届いたコールバックは無視する。`unregisterNetworkCallback()` は例外時も含め必ず実行する（finally で解除する）。解除後に残るコールバックがあっても、確定済みの判定を書き換えない。
  6. VPN ネットワークの総数を確認する。0 件の場合は DEGRADED（tailscale-interface-unavailable）。2 件以上の場合は DEGRADED（tailscale-network-ambiguous）。ちょうど 1 件のときだけ次に進む。
  7. その 1 件の `LinkProperties` を評価する。IPv4 アドレスのうち 100.64/10 のものがちょうど 1 つなら、そのアドレスを bind 対象にする。0 件なら DEGRADED（tailscale-interface-unavailable）。複数なら DEGRADED（tailscale-network-ambiguous）。
  8. bind を実行する。bind 直前に共有キャンセル状態を確認し、停止要求が確定している場合は bind を進めない（§3.7）。
- 100.64/10 は複数の VPN の中から選ぶための補助条件であり、単独の判定根拠にはしない。Tailscale 以外の VPN が 100.64/10 を持つ場合、公開 API だけでは識別できないことを仕様上の限界として明記する（T-20 も参照）。
- 識別の限界を明記する：本方式は管理上の前提（Tailscale が唯一の VPN）に依存する。公開 Connectivity API だけでは VPN の提供元アプリを確実に識別できない。他 VPN が存在する可能性を排除できない環境では、Tailscale としての識別を保証しない。前提が崩れた場合は運用上の異常として扱い、接続確認を要求する。
- 必要な権限：`ACCESS_NETWORK_STATE`（通常権限。インストール時に付与される）。
- 100.64/10 だけを根拠にした判定は行わない（CONS-059 P0-01）。従来の `isTailscaleIp()` による判定は削除する。フォールバックとして 100.64/10 判定を残さない（CGNAT への誤 bind が再発するため）。
- 却下した代替案：0.0.0.0（全インターフェース）への bind。モバイル回線や公共 Wi-Fi のインターフェースにもサーバーが露出するため、選択的 bind というセキュリティ方針を維持する。

### 3.7 BIND-02 bind 失敗時の状態を明確にする（P0-02）

- 127.0.0.1 のみで起動した場合を正常起動として扱わない。サーバー状態を区別する。状態遷移表：

| 現在の状態 | 条件 | 次の状態 | ログ |
|------------|------|----------|------|
| STARTING | Tailscale への bind 成功 | LISTENING | `server LISTENING` |
| STARTING | Tailscale 不在、loopback 成功 | DEGRADED | `server DEGRADED: tailscale-interface-unavailable` |
| STARTING | Tailscale への bind 失敗、loopback 成功 | DEGRADED | `server DEGRADED: tailscale-bind-failed` |
| STARTING | 候補が複数あり一意に特定できない | DEGRADED | `server DEGRADED: tailscale-network-ambiguous` |
| STARTING | 全 bind 失敗（loopback を含む） | FAILED | `server FAILED: no listener bound`（既存） |
| STARTING | 停止要求（stop() が呼ばれた） | STOPPING | 起動をキャンセルし、Callback を解除する。bind を進めない |
| LISTENING／DEGRADED | 停止処理の開始 | STOPPING | （状態遷移のみ。ログ行は STOPPED で記録する） |
| STOPPING | 停止処理の完了 | STOPPED | `server STOPPED: <reason>`（§3.3） |
| FAILED | （終端状態） | FAILED のまま | STOPPED は記録しない（サーバーは一度も起動していないため） |

- 起動中の停止要求：STARTING 中に `stop()` が呼ばれた場合、STARTING→STOPPING に遷移し、起動をキャンセルして Callback を解除する。STOPPING→STOPPED で STOPPED を 1 回記録する。起動処理が完了した時点で停止要求がなければ、通常の判定を行う。停止要求が確定した後に bind を進めないこと（共有キャンセル状態を持ち、bind 直前に確認する）。
- FAILED でも NetworkCallback の解除・リソース解放・後始末は必須とする。再起動時は新しい実行として状態を初期化する（前回の FAILED を引き継がない）。
- 起動時に確定した DEGRADED は、外部接続可能な状態として扱わない（UI・通知にも反映する）。
- DEGRADED の理由 `tailscale-interface-unavailable` の範囲を定義する：VPN ネットワークが 0 件、2000 ms 以内の取得失敗、`LinkProperties` が null、100.64/10 候補なし、IPv4 なし。
- DEGRADED の場合は、通知・UI にも外部接続不能状態を反映する（ダイヤル表示は SPEC-UI-002 の色分けに従い、赤系で示す方向とする）。
- 状態遷移は STARTING／LISTENING／DEGRADED／STOPPING／STOPPED／FAILED で統一する。

## 4. 変更するファイル

- `android-app/src/dev/muse/bridge/BridgeLog.java`（LOG-01、LOG-04 の呼び出し側は別ファイル）
- `android-app/src/dev/muse/bridge/HttpBridgeServer.java`（LOG-02、LOG-03、LOG-05、BIND-01、BIND-02）
- `android-app/src/dev/muse/bridge/BridgeNotificationListener.java`（LOG-04）
- `android-app/src/dev/muse/bridge/BridgeService.java`（LOG-03：stop 呼び出しに理由を付ける）
- `android-app/src/dev/muse/bridge/MainActivity.java`（BIND-01：`isTailscaleIp()` の削除。ただし PingProber など他の呼び出し元があれば、その用途に応じて残すか置き換えるかを判断する）
- バージョン：v2.8.6（code 52）に上げる（`build.sh` の VERSION_NAME／VERSION_CODE）。
- bind 対象アドレスの決定処理は、Android API 依存部分と純粋な判定ロジックに分離し、Android 実機なしで VPN 判定の境界条件を検証できる構成にする（§5.1）。

## 5. テスト

### 5.1 第 1 段階：判定ロジックの単体テスト（実機確認の前に行う）

- 判定ロジック（VPN ネットワークの特定、アドレスの選択、DEGRADED への遷移）を、Android 依存のない純粋な関数として分離し、実機を使わずにテストできる形にする。テストケースは §5.4 の表のうち実機不要のものを用いる。

### 5.2 第 2 段階：Android 実機テスト（Galaxy A55）

- Wi-Fi／モバイル切替、Tailscale 接続・切断・再接続、端末再起動後、他の VPN 有効時、の各条件で、bind 結果とログを確認する。追加条件：VPN 接続中の Wi-Fi 切断、モバイル切替、起動タイミング（Tailscale 起動前／起動中／起動後）、VPN 情報取得時の null。

### 5.3 第 3 段階：VM からの疎通試験

- TCP 到達／HTTP 到達／処理失敗／/health 正常応答の 4 段階を区別して確認する。接続先 IP を明示する（Tailscale IP には到達すること、CGNAT 側 IP には到達しないこと、loopback には VM から到達しないこと）。

### 5.4 テストケース表

| # | 条件 | 期待する結果 |
|---|------|-------------|
| T-01 | CGNAT のみ存在（Tailscale 停止中） | 127.0.0.1 のみに bind し、DEGRADED（tailscale-interface-unavailable）を記録する。CGNAT には bind しない |
| T-02 | Tailscale IP あり | Tailscale インターフェースのアドレスに bind し、LISTENING を記録する |
| T-03 | Tailscale 停止（起動後に切断） | 次回起動時に DEGRADED になる。実行中の切断への対応は本 SPEC の対象外（将来の拡張） |
| T-04 | インターフェース名が "tun0" など "tailscale0" 以外 | 名前に依存せず、VPN ネットワークの状態で正しく bind する |
| T-05 | 複数の 100.64/10 アドレスが存在 | VPN ネットワークの総数を先に確認する。VPN が複数ある場合は、100.64/10 候補が 1 つでも DEGRADED（tailscale-network-ambiguous）になる。VPN が 1 件で、その中の 100.64/10 が 1 つなら bind する |
| T-06 | bind 例外（ポート使用中など） | Tailscale への bind 失敗・loopback 成功の場合は DEGRADED（tailscale-bind-failed）。全 bind 失敗の場合は FAILED |
| T-07 | 同時通知が多数発生 | 通知行がスロットルされ "(+N suppressed)" が付く |
| T-08 | 同一 IP から連続接続 | client-first が 1 回だけ記録される |
| T-09 | 異なる IP から接続 | それぞれ初回に client-first が記録される |
| T-10 | ログの再起動（アプリ再起動） | エクスポート全体が時刻順になる。前回セクションの開始時刻が表示される。200 行を超えるログを生成した状態でも、開始時刻が復元できること |
| T-11 | STOPPED の重複呼び出し | STOPPED が 1 回だけ記録される。最初に確定した理由が保持される。同時 stop() でも STOPPED は 1 回だけ記録される |
| T-12 | IPv6 アドレスのみ存在 | IPv6 は bind 対象外とし、DEGRADED になる。IPv6 は §7 のマスク規則で記録する |
| T-13 | VPN 情報取得時に null が返る | DEGRADED（tailscale-interface-unavailable）になる。クラッシュしない |
| T-14 | 複数候補で判定不能 | DEGRADED（tailscale-network-ambiguous）になる。bind しない |
| T-15 | Tailscale bind 失敗・loopback 成功 | DEGRADED（tailscale-bind-failed）になる |
| T-17 | 機密情報のログ出力防止 | 要求ボディ・ノードテキスト・通知のタイトル／本文・接続元ポート・トークンがログに出ないこと。IPv4／IPv6 のマスクが §7 通りであること。既知のトークン形式（32 文字以上の 16 進数列以外も含む）が出ないこと。`BridgeLog.log()` と `logThrottled()` で同一のサニタイズが適用されること |
| T-18 | NetworkRequest の VPN 条件 | `removeCapability(NET_CAPABILITY_NOT_VPN)` により VPN ネットワークが取得できること。除去しない場合は VPN ネットワークが取得できないこと |
| T-19 | VPN 情報取得のタイムアウト | 2000 ms 以内に届かない場合は DEGRADED になる。遅延コールバックで判定が変わらないこと。コールバックが解除されること。2000 ms の計測に単調増加時計が使われていること |
| T-20 | 別 VPN の誤認防止 | Tailscale 以外の VPN が 1 つだけの場合、100.64/10 の有無で区別する。100.64/10 がなければ DEGRADED になる。他 VPN が 100.64/10 を持つ場合は、公開 API だけでは識別できないことを仕様上の限界として記録する |
| T-21 | 同一 VPN 内の複数 IPv4 アドレス | 100.64/10 が 1 つだけなら選択する。複数なら DEGRADED、なしも DEGRADED |
| T-22 | NetworkCallback の後始末 | 起動を繰り返しても重複登録されないこと。正常・タイムアウト・例外のいずれでも解除されること。登録前に例外が発生した場合は安全に DEGRADED になること |
| T-23 | onLost による情報削除 | 対象 Network が onLost された場合、その情報が収集領域から削除されること。判定確定前は反映され、確定後は無視されること |
| T-24 | 複数 VPN で候補 1 つでも DEGRADED | VPN ネットワークが複数あり、そのうち 100.64/10 候補が 1 つだけでも、DEGRADED（tailscale-network-ambiguous）になる |
| T-25 | コールバック順序不同で同一結果 | onAvailable／onLinkPropertiesChanged の到着順が変わっても、同じ判定結果になること |
| T-26 | STARTING 中の stop() で bind 不実行 | STARTING 中に stop() が呼ばれた場合、bind が実行されず、STOPPED が 1 回記録されること |
| T-27 | 同時 stop() で STOPPED 1 回 | 複数スレッドから同時に stop() が呼ばれても、STOPPED が 1 回だけ記録されること |
| T-28 | 通知待機上限超過時も保存継続 | 実行中の通知処理が 5 秒以内に終了しない場合、未完了の存在を記録しつつ、flush・サーバー停止・ログ保存が続行されること |
| T-29 | client-first 64 件上限・FIFO 破棄 | 64 件を超える接続があった場合、最も古いものから破棄され、破棄された IP の再接続で再び client-first が記録されること |
| T-30 | run-meta 更新中のプロセス終了時の復旧 | run-meta の更新中にプロセスが終了した場合、次回起動時に前回の開始時刻が復元できること（破損しないこと） |

（注：T-16 は T-12 と重複するため削除し、枠を T-22 に使った。T-23〜T-30 を追加した。）

### 5.5 実機確認（サイガマンがインストール後に Muse が確認）

- ログをエクスポートし、全体が時刻順になっていること（LOG-01）。
- listen 行にインターフェース名が付き、Tailscale IP に bind していること（LOG-02、BIND-01）。VM からの /health が届くこと。
- ブリッジを停止し、STOPPED 行に理由が付いていること（LOG-03）。
- 通知を多数発生させ、通知行がスロットルされ "(+N suppressed)" が付くこと（LOG-04）。
- VM から接続し、client-first 行が 1 回だけ出ること（LOG-05）。
- Tailscale を切断した状態で起動し、DEGRADED になること（BIND-02）。

## 6. 対象外・将来の拡張候補

- マスク方針の変更なし（§7 の規則に従う）。
- ログの保持量（リング 200 行／ファイル 64 KB）の変更なし。
- 毎リクエストの記録はしない。
- /logs エンドポイントの変更なし。
- 実行中の Tailscale 切断への対応（再 bind や状態遷移）は将来の拡張とする（T-03 は起動時のみ対象）。
- P2-01：ログに実行識別子（例：run=03）を付ける。
- P2-02：起動時のネットワーク状態の記録は、LOG-02 のインターフェース一覧と bind-summary で一部まかなう。残り（VPN ネットワーク数、VPN 内 IPv4 アドレス数、100.64/10 該当数、除外アドレス数など）は将来の拡張とする。
- P2-03：UI の状態表示。DEGRADED の赤系表示は適切だが、状態と理由を UI から確認できるとさらに良い（将来の拡張）。
- P2-04：ログの行数制限を考慮し、インターフェース一覧が長い場合の切り詰め方と省略件数を定義する（将来の拡張）。
- BIND-01 を切り離す場合は、LOG-02 の要約行は「bind したアドレス一覧」のみとする。

## 7. プライバシー規則（P1-F）

- IPv4 アドレスは先頭 2 オクテットのみ記録する（例：100.111.*.*）。
- IPv6 アドレスは先頭 2 グループのみ記録する（例：2001:db8:*.*）。IPv6 は bind 対象外とする。
- IPv4-mapped IPv6（例：`::ffff:100.111.74.42`）は IPv4 に正規化してからマスクする。接続元ポートを含む IPv6 表記（例：`[2001:db8::1]:8080`）も、ポート部分を除いてからマスクする。
- トークン（32 文字以上の 16 進数列）は常に [token] に置換する。トークンは 32 文字以上の 16 進数列以外の形式もあり得るため、既知のトークン形式がログに出ないことをテストする（T-17）。置換前後の区切り文字・付加文字列が残っても、トークン本体が復元できないことを確認する。
- HTTP 要求ボディ、ノードテキスト、通知のタイトル／本文、接続元ポートは記録禁止とする。
- 例外メッセージは無条件にログ出力しない（エラー分類のみ記録する）。例外オブジェクトの文字列化（`toString()`）による情報漏洩を防ぐため、例外はログに渡さない。全ログ経路でこの方針を徹底する。
- LOG-02 のインターフェース一覧にも同じマスク処理を適用する（複数アドレスがある場合もすべてマスクする）。
- サニタイズは共通のログ入口（`BridgeLog.log`／`BridgeLog.logThrottled`）で適用する。呼び出し元が個別にマスクすることを前提としない。`BridgeLog.log()` と `logThrottled()` の両方で同一のサニタイズが適用されることをテストする（T-17）。
- テストで確認すること（T-17）：IPv4／IPv6 のマスク、トークンの置換、要求ボディ・通知・接続元ポートが出力されないこと。

## 付録 A：レビュー指摘への対応表

### CONS-20261108-059（P0×2、P1×7、P2×4）

| 指摘 | v2 での対応 | v3 での対応 | v4 での対応 | v5 での対応 |
|------|-------------|-------------|-------------|-------------|
| P0-01（BIND-01 再設計） | §3.6：インターフェース名で判定 | §3.6：NetworkCallback で VPN ネットワークを特定 | §3.6：NOT_VPN 除去、コールバック管理規則、識別の限界を追加 | §3.6：8 段階の処理順序、onLost、ネットワーク単位とアドレス単位の分離 |
| P0-02（bind 失敗時の状態） | §3.7：DEGRADED を新設 | §3.7：状態遷移表を追加 | §3.7：完全な状態遷移表、FAILED は終端、理由の範囲を定義 | §3.7：STARTING 中の停止遷移、FAILED 時の後始末、再起動時の初期化 |
| P1-01（LOG-01 時刻順の定義） | §3.1：前回分を先に置く | §3.1：実行開始時刻、時計の条件、永続化方法を追加 | §3.1：開始時刻を別メタデータとして保存（P1-H） | §3.1：起動時手順、原子的な置換を追加 |
| P1-02（LOG-03 理由の列挙型） | §3.3：列挙型を定義 | §3.3：競合処理を追加 | §3.3：AtomicReference＋compareAndSet で原子化 | §3.3：AtomicBoolean で実行権を確保。再試行方針を追加 |
| P1-03（LOG-04 スロットル定義） | §3.4：動作定義を追加 | §3.4：flush の順序を固定 | §3.4：6 段階の停止順序、flush の出力形式を追加 | §3.4：待機上限 5 秒、超過時は記録しつつ続行 |
| P1-04（LOG-05 重複判定） | §3.5：実 IP で判定、accept 時 | §3.5：上限 64 件、FIFO、正規化を追加 | §3.5：スレッドセーフ、破棄後の再記録を許容 | §3.5：FIFO は純粋な挿入順。64 件超のテストを追加 |
| P1-05（LOG-02 分離） | §3.2：一覧と結果を分離 | §3.2：取得元の区別、null 時の形式、件数の分離を追加 | §3.2：NetworkInterface と LinkProperties の優先順位を明記 | §3.2：不一致時のログ表示方法を追加 |
| P1-06（テスト計画の拡充） | §5：3 段階＋表 T-01〜T-12 | §5：T-13〜T-17 を追加 | §5：T-18〜T-22 を追加。T-16 を削除 | §5：T-23〜T-30 を追加。T-05・T-20 を修正 |
| P1-07（プライバシー明文化） | §7：規則を定義 | §7：共通入口での適用とテストを追加 | §7：マスクの追加確認事項（P1-F）を追加 | §7：トークン形式、同一サニタイズのテストを追加 |
| P2-01〜P2-04 | §6 に記録 | §6 に記録 | §6 に記録 | §6 に記録（P2-03 を追加。v4 までの §6 には P2-03 が欠けていた） |

### CONS-20261108-060（P0×2、P1-A〜G、P2×4）

| 指摘 | v3 での対応 | v4 での対応 | v5 での対応 |
|------|-------------|-------------|-------------|
| P0-01（VPN と Tailscale の識別） | §3.6：唯一の VPN という運用前提を明記 | §3.6：識別の限界を明記。ネットワーク単位とアドレス単位の区別を追加 | §3.6：8 段階の処理順序、onLost、T-20 の修正 |
| P0-02（DEGRADED と FAILED の境界） | §3.7：状態遷移表を追加 | §3.7：完全な状態遷移表。FAILED は終端 | §3.7：STARTING 中の停止遷移、FAILED 時の後始末 |
| P1-A（getAllNetworks 非推奨） | §3.6：NetworkCallback を基本とする | §3.6：NOT_VPN 除去を追加（P0-01） | §3.6：コールバック管理規則（onLost、単調増加時計、原子性、登録フラグ、InterruptedException） |
| P1-B（LOG-02 取得元） | §3.2：役割を区別。null 時は "unknown" | §3.2：優先順位（NetworkCallback 側）を明記 | §3.2：不一致時のログ表示方法 |
| P1-C（LOG-03 競合処理） | §3.3：最初に確定した理由を保持 | §3.3：AtomicReference＋compareAndSet | §3.3：AtomicBoolean で実行権を確保。再試行方針 |
| P1-D（LOG-04 flush 順序） | §3.4：停止・保存処理の順序を固定 | §3.4：6 段階の順序。flush の出力形式 | §3.4：待機上限 5 秒。超過時は記録しつつ続行 |
| P1-E（LOG-05 上限） | §3.5：上限 64 件、FIFO、正規化 | §3.5：スレッドセーフ、破棄後の再記録を許容 | §3.5：FIFO は純粋な挿入順。64 件超のテスト |
| P1-F（プライバシー） | §7：共通入口での適用 | §7：追加確認事項を明記 | §7：トークン形式、同一サニタイズのテスト |
| P1-G（テスト不足） | §5：T-13〜T-17 を追加 | §5：T-18〜T-22 を追加。T-16 を削除 | §5：T-23〜T-30 を追加。T-05・T-20 を修正 |
| P2-01〜P2-04 | §6 に記録 | §6 に記録 | §6 に記録（P2-03 を追加） |

### CONS-20261108-061（P0×1、P1×8、P2×4）

| 指摘 | v4 での対応 | v5 での対応 |
|------|-------------|-------------|
| P0-01（NetworkRequest のデフォルト条件） | §3.6：`removeCapability(NET_CAPABILITY_NOT_VPN)` を追加 | §3.6：8 段階の処理順序。ネットワーク総数を先に確認 |
| P0-01 補足（コールバック取得手順） | §3.6：到着順に依存しない、Network ごとに管理、メインスレッドで待機しない、遅延コールバックは無視、finally で解除、2000 ms は待機上限 | §3.6：onLost、単調増加時計、原子性、登録フラグ、InterruptedException を追加 |
| P0-01 補足（Tailscale と他 VPN の識別） | §3.6：識別の限界を明記 | §3.6：T-20 を修正。仕様上の限界として明記 |
| P0-02（DEGRADED と FAILED の境界） | §3.7：完全な状態遷移表。FAILED は終端 | §3.7：STARTING 中の停止遷移、FAILED 時の後始末、再起動時の初期化 |
| P1-A（NetworkCallback） | §3.6：対応済み。NOT_VPN 除去を追加 | §3.6：onLost、単調増加時計、原子性、登録フラグ、InterruptedException |
| P1-B（LOG-02 取得元） | §3.2：優先順位を明記 | §3.2：不一致時のログ表示方法 |
| P1-C（LOG-03 競合処理） | §3.3：AtomicReference＋compareAndSet | §3.3：AtomicBoolean で実行権を確保。再試行方針 |
| P1-D（LOG-04 flush） | §3.4：6 段階の順序。flush の出力形式 | §3.4：待機上限 5 秒。超過時は記録しつつ続行 |
| P1-E（LOG-05 上限） | §3.5：スレッドセーフ、破棄後の再記録を許容 | §3.5：FIFO は純粋な挿入順。64 件超のテスト |
| P1-F（プライバシー） | §7：追加確認事項を明記 | §7：トークン形式、同一サニタイズのテスト |
| P1-G（テスト不足） | §5：T-18〜T-22 を追加。T-16 を削除 | §5：T-23〜T-30 を追加。T-05・T-20 を修正 |
| P1-H（LOG-01 開始時刻の永続化） | §3.1：開始時刻を別メタデータとして保存 | §3.1：起動時手順、原子的な置換 |
| P2-01〜P2-04 | §6 に記録 | §6 に記録（P2-03 を追加） |

### CONS-20261108-062（P0×2、P1×6、P2×3）

| 指摘 | v5 での対応 |
|------|-------------|
| P0-01（複数 VPN 時の fail-closed が不完全） | §3.6：ネットワーク選択とアドレス選択を分離。VPN 総数を先に確認する 8 段階の処理順序。onLost を定義。T-20 を修正 |
| P0-02（起動中の停止要求の遷移が欠けている） | §3.7：STARTING→STOPPING→STOPPED を追加。停止要求確定後は bind を進めない。FAILED 時の後始末と再起動時の初期化を明記 |
| P1-A（NetworkCallback） | §3.6：onLost、単調増加時計、原子性、登録フラグ、InterruptedException |
| P1-B（LOG-02 取得元分離） | §3.2：不一致時のログ表示方法 |
| P1-C（STOPPED 競合） | §3.3：AtomicBoolean で実行権を確保。再試行方針 |
| P1-D（通知 flush 順序） | §3.4：待機上限 5 秒。超過時は記録しつつ続行 |
| P1-E（client-first 重複判定） | §3.5：FIFO は純粋な挿入順。64 件超のテスト |
| P1-F（プライバシー） | §7：トークン形式、同一サニタイズのテスト |
| P1-G（テスト計画） | §5：T-23〜T-30 を追加。T-05・T-20 を修正。T-10・T-11・T-17・T-19・T-22 を補強 |
| P1-H（LOG-01 開始時刻の永続化） | §3.1：起動時手順、原子的な置換 |
| P2-01〜P2-03 | §6 に記録（P2-03 を追加。v4 までの §6 には P2-03 が欠けていた） |

## 採用 — 2026-11-08 04:44 +07（サイガマン決定）

- v5 を採用する。サイガマンの決定「いったんv5を実装して様子見よう」（2026-11-08 04:44 +07）。
- GPT レビュー：CONS-20261108-063 は条件付き採用（P0×2、P1×8、P2×5）。v5 の設計方向は妥当とされ、全体を作り直す必要はないとされた。
- CONS-063 の P0-01／P0-02 の補強内容（onLost 後の再登録禁止、共通ライフサイクルロック、InterruptedException の本文定義、T-31〜T-38 の競合試験）は、実装時の条件として引き継ぐ。単体テストと実機確認で検証する。
- Status は Draft のまま。採用であり、仕様の凍結ではない。

## 実装 — v2.8.6（2026-11-08、MUSE-Koyuki が直接実装）

- AntiGravity のワークスペースは antigravity_projects に限定されており、muse-bridge リポジトリには届かないため、Muse が直接実装した。
- 変更ファイル：BridgeLog.java（LOG-01、run-meta、maskIp、log() の真偽値返却）、HttpBridgeServer.java（LOG-02、LOG-03、LOG-05、BIND-01、BIND-02、DEGRADED 状態、/health）、BridgeNotificationListener.java（LOG-04、停止時の quiesce）、BridgeService.java（停止理由）、MainActivity.java（isTailscaleIp の委譲、停止理由 MANUAL）、BridgeWidgetActionReceiver.java（停止理由 WIDGET）、BridgeTileService.java（停止理由 WIDGET）。新規：VpnBindSelector.java（純粋な VPN 判定ロジック、Android 非依存）。
- ビルド：成功。APK `Muse_Bridge_v2.8.6.apk`（code 52）。
- Python テスト：299 passed、18 subtests passed（2026-11-08 05:01 +07）。v2.8.6 の変更とは無関係の既存テスト不具合 3 件を修正した（テストツリーが古かったため同期、ウィジェット受信機の `action` 変数化、テストの過剰な部分一致と古い終了マーカーの修正）。
- 既知の実装上の判断（仕様文からの意図的な逸脱）：
  - STARTING→STOPPING の遷移（§3.7）：start()/stop() は synchronized メソッドであり、モニターが共通ライフサイクルロックとして機能する。stop() は start() の実行中には走れないため、確認と bind の間に競合窓は存在しない。停止要求が起動中に来た場合、stop() は最大 2000 ms（VPN 探索の待機上限）+ bind 時間だけ待ってから実行される。bind 直前のキャンセル確認（§3.6 step 8）は明示的に実装してある。ウェイクシグナルによる即時キャンセルは未実装（将来の改善候補）。
  - 起動時に VPN 探索のため最大 2000 ms 待つ（§3.6）。2 つ目の VPN の onAvailable が遅れて届く可能性があり、fail-closed のためには完全な件数が必要なため、早期復帰はしない。起動が最大 2 秒遅くなる。
  - 「previous STOPPED record failed」の永続化（P1-C）：BridgeLog.log() が真偽値を返すようにし、STOPPED 記録の失敗時に SharedPreferences（MainActivity.PREFS）にフラグを保存し、次回起動時に報告する。
  - T-31〜T-38 の競合試験は、リポジトリに Java 単体テスト基盤がないため、実機確認とライブ検証で代替する。VpnBindSelector は Android 非依存の純粋関数として分離済みで、将来の単体テストが可能。
- ライブ確認（§5.5）はインストール後に実施し、結果をここに記録する。

## ライブ確認 — 2026-11-08 05:05–05:06 +07（v2.8.6、サイガマンがインストール）

- ヘルスチェック：status up、app 2.8.6/52、a11y true。全レイヤー ok（proxy 3 ms、tailscale 451 ms、bridge 637 ms）。/health は HEALTHY（http HEALTHY、tailscale HEALTHY、a11y ENABLED、notification_access GRANTED）。
- ログエクスポート（サイガマンが送付した `bridge-log-20261108-050501_2_k2h8.txt`、exported 05:05:05）：
  - LOG-01：時刻順になっている（前回 04:47–05:04 → 現行 05:04–05:05）。前回セクションの開始時刻は "unknown"（v2.8.5 には run-meta がないため、想定通りのフォールバック）。現行セクションの開始時刻は "2026-11-08 05:04:41"。
  - LOG-02：`ifaces rmnet0:100.72.*.*, lo:127.0.*.*, tun0:100.111.*.*`、`listen tun0 100.111.*.*:8080`、`listen lo 127.0.*.*:8080`、`bind-summary bound=2 excluded=1 failed=0 (tailscale bound) (loopback bound)`。インターフェース名付き。
  - BIND-01：CGNAT（rmnet0、100.72.*.*）は excluded=1 で除外され、Tailscale（tun0、100.111.*.*）に bind された。インターフェース名は "tun0" であり、"tailscale0" ではないことが実機で確認された（SPEC の前提通り）。これが 2026-11-08 の障害（CGNAT 100.68.*.* への誤 bind）の修正である。
  - BIND-02：`server RUNNING`（Tailscale に bind されたため DEGRADED ではない）。
  - LOG-04：現行のログは通知行がまばら（posted 05:04:43、removed 05:06:01 のみ）。前回の flood（約 10–20 秒間隔の updated）が現行では出ていない。スロットルが機能している。
  - LOG-05：VM からの接続で `[11-08 05:05:46] client-first 100.84.*.*` が 1 回だけ記録された（VM の Tailscale IP 100.84.58.110、マスク済み）。
- ping_to_client：/state では null。PingProber.probe() は MainActivity の UI から呼ばれるもので、VM からの接続では起動しない。v2.8.5 で確認済み（364 ms）のコードは v2.8.6 で変更していないため、回帰ではない。アプリを開けば表示される。
- 未確認だった 2 件（LOG-03、BIND-02）は、2026-11-08 06:55 +07 のログ画面読取で確認済みとなった（下の「ライブ確認（追記）」を参照）。

## ライブ確認（追記）— 2026-11-08 06:55 +07（v2.9.0、ログ画面を VM から読取）

- 2026-11-08 06:17 のトンネル障害と Tailscale 入れ直し（06:54）のあと、ログ画面（exported 2026-11-08 06:55:32、app 2.9.0/53）を VM から読み取った。LOG-001 のコードは v2.9.0／v2.9.1 で変更していないため、以下の確認は v2.8.6 実装の検証として有効。
- LOG-03：確認済み。`[11-08 06:46:41] server STOPPED: MANUAL`、`[11-08 06:46:55] server STOPPED: MANUAL`、`[11-08 06:54:11] server STOPPED: MANUAL` — STOPPED 行に理由（MANUAL）が付いている。
- BIND-02：確認済み。06:17:23 の BOOT_COMPLETED 起動時、Tailscale が未接続で `ifaces rmnet0:100.69.*.*, lo:127.0.*.*`（tun0 なし）、`bind-summary bound=1 excluded=1 failed=0 (loopback bound)`、`server DEGRADED: tailscale-interface-unavailable`。06:54:15 の Tailscale 復旧後の再起動では `listen tun0 100.111.*.*:8080`、`bind-summary bound=2 excluded=1 failed=0 (tailscale bound) (loopback bound)`、`server RUNNING`。
- これで §5.5 の未確認 2 件はすべて確認済みとなった。
