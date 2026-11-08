# Muse Bridge — 操作中のライブ通知「Override - x/y」Spec (Draft)

- Status: Draft（GPT レビュー CONS-079 は条件付き採用。v2 を作成し、再レビュー CONS-080 に回す。採用・凍結はサイガマンの決定による）
- Source: サイガマンの提案 2026-11-08 07:46 +07「Museが端末を操作中にステータスバーに「Override - x/y」みたいなライブ通知追加できない？　君が操作中に間違って操作するのが申し訳ない」
- Version target: v2.9.2 / code 55
- Local file: `~/workspace/muse-bridge-repo/SPEC-OVERRIDE-001.md`（正本。`~/workspace/muse-bridge/SPEC-OVERRIDE-001.md` は同期コピー）
- 前提となる既存仕様：SPEC-OPERATION-NOTIFY.md（v2.1、操作中／操作終了の通知）

## v1 → v2 の変更点（CONS-079 対応、2026-11-08）

- P1-01：スロットルでスキップした最新ラベルを保留し、最後の投稿から 500 ms 経過した時点で遅延投稿する（§3.3）。セッション終了後は投稿しない。新規操作が入ったら保留ラベルを更新する。
- P1-05：D6 の改訂を書き直す（§1 D3）。座標を「操作メタデータ」として分類し、画面配置を知る第三者が操作対象を推測できる可能性と、ロック画面への表示を、許容する情報開示として明記する。
- P1-02：テスト OV-11〜OV-15 を追加（§4）。スロットル境界・セッション境界・競合を検証する。
- P1-03：ライブ確認 OV-L7 を追加（§5）。通知権限が無効でも操作が継続すること。
- P1-04：テスト計画に責任分担を明記（§4 末尾）。Python テストはソース構造のみを証明し、Java の実行時動作はコードレビューと実機確認で補う。
- P2-01：「検証済みの整数」の表現を修正（§2、§6）。`getInt()` は整数型であることのみを保証し、画面範囲内であることは保証しない。
- P2-02：OV-10 の対象を広げる（`/type_node`・`/scroll_node`・`/click_node`・`/notification_action`・`/press_key`）。
- P2-03：初回投稿の判定を `operationLastNotifyMs = 0` から専用フラグ `hasPostedInSession` に変える（§3.3）。
- P2-04：Now Bar／Live Updates の説明を修正（§1 D5）。「許可リスト入りの Google アプリだけ」→「Samsung が認めた対応アプリ・システム機能」。「永久に対象外」→「本端末の運用期間中は対象外」。Live Updates と ProgressStyle を同一視しない。
- P2-05：OV-L5 に小サイズ表示とシェードでの視認性の確認を加える（§5）。

## 0. Goal

VM 側が端末の画面を操作している間、Bridge アプリの「操作中」通知の文面を、操作のたびに「Override - タップ 540/2196」のようにライブ更新する。サイガマンがステータスバーのアイコンと通知シェードの文面で「今こっちが操作中だよ、ここを触ってるよ」と一目で分かり、誤って端末を触ることを防ぐ。通知はあくまで観察者であり、サイガマンのタッチを止める手段ではない（止める手段は Android に存在しない）。

## 1. Locked decisions

| # | Decision |
|---|---|
| D1 | 土台は v2.1 の操作通知をそのまま使う。チャンネル `muse-bridge-operation`、ID 3＝「操作中」（ongoing）、ID 4＝「操作終了」（one-shot）、自動終了 10 秒、タップで Bridge アプリの MainActivity を開く、という挙動は変えない。 |
| D2 | 文面の書式は `Override - <操作> <座標>` とする。タイトルは「操作中」のまま（タイトル＝セッションの状態、文面＝今の操作、という v2.1 の役割分担を維持する）。 |
| D3 | **SPEC-OPERATION-NOTIFY の D6（通知に座標を載せない）を改訂する。** 座標は画面内容・ユーザー入力ではなく**操作メタデータ**として分類し、整数の座標に限り通知への掲載を許可する。画面テキスト、入力文字、URI、クリップボードは引き続き禁止する。**ただし、座標が情報を一切含まないわけではない。** 画面解像度や UI 配置を知る第三者は、座標から操作対象を推測できる可能性がある（例：銀行アプリの振込確定ボタン、認証画面の特定位置、パスワード入力欄）。また通知はロック画面にも表示され得る。これらは本仕様が**許容する情報開示**として明記し、サイガマンの採用判断の材料とする。 |
| D4 | 座標を載せるのは、リクエストに整数の座標が含まれる操作だけ（`/click`、`/swipe`）。それ以外は既存ラベルに `Override - ` を付けるだけとし、内容は変えない（入力文字・URI は載せない）。 |
| D5 | **One UI 7.0 対応のみ。** この端末は赤ロム（中古・キャリア制限端末を管理者権限で突破したもの）で、One UI 8 への更新はできない（更新すると突破が壊れるおそれがあり、現状が最終状態）。そのため Android 16 の Live Updates は**本端末の運用期間中は対象外**とする（Live Updates と `Notification.ProgressStyle` は同一ではないが、いずれも対象外であることに変わりはない）。Now Bar についても対象外。One UI 7.0 では、Now Bar に表示できるライブ通知は Samsung が認めた対応アプリ・システム機能に制限され、一般のサードパーティアプリ向けの公開 API は提供されていない。この仕様で実現するのは、ステータスバーのアイコン＋通知シェードの文面である。 |
| D6 | 通知は観察者であり、ゲートではない（v2.1 D5 を維持）。通知の投稿に失敗しても操作は続行し、失敗はログに残すだけとする。 |
| D7 | 通知のタップは Bridge アプリの MainActivity のみを開く（v2.1 D7 を維持）。他アプリの起動には使わない。 |
| D8 | 文面の更新はスロットルする。連続する投稿の間隔が 500 ms 未満のときは即投稿をスキップし、最新ラベルを保留する。保留したラベルは、最後の投稿から 500 ms 経過した時点で遅延投稿する（§3.3）。ライブ表示であり、ログではないため、途中のラベルが飛ぶことは許容する。ただし、**最後の操作のラベルは必ず最終的に表示される**（P1-01 の保証）。 |

## 2. ラベル書式（文面。タイトルは常に「操作中」）

| Endpoint | 新しいラベル |
|---|---|
| `/click` | `Override - タップ 540/2196` — リクエストの x/y をそのまま使う（整数であることのみ保証される。`getInt()` は整数型への変換であり、画面範囲内の座標であることまでは保証しない。画面外の値もそのまま出す — §6 参照） |
| `/swipe` | `Override - スワイプ 100/800 → 100/300` — 開始座標 → 終了座標 |
| `/type_text` | `Override - 文字を入力中`（入力文字は載せない） |
| `/press_key` | `Override - キー操作中（BACK）` — キー名は既存の固定 enum 検証の後 |
| `/click_node` | `Override - 操作を実行中（click）` — 座標はリクエストにないため付けない |
| `/type_node` | `Override - 文字を入力中`（入力文字は載せない） |
| `/scroll_node` | `Override - スクロール中` |
| `/batch` | `Override - 複数の操作を実行中`（ステップごとの要約はしない。バッチ内のタップ座標も載せない — v1 の範囲外。§8 参照） |
| `/launch_app` | `Override - アプリを起動中：K PLUS` — アプリラベルは既存通り |
| `/open` | `Override - リンクを開いています`（URI は載せない） |
| `/notification_action` open | `Override - 通知を開いています` |

終了側は変更なし。タイトル `操作終了`、文面 `Muse の操作が終わりました`。

## 3. Java changes — `android-app/src/dev/muse/bridge/HttpBridgeServer.java`（主）＋ drawable 1 件

### 3.1 ラベル生成の変更（各 `handle()` 分岐の `noteOperation(...)` 呼び出し）

- `/click`：`"タップ中"` → `"Override - タップ " + x + "/" + y`。
- `/swipe`：`"スワイプ中"` → `"Override - スワイプ " + x1 + "/" + y1 + " → " + x2 + "/" + y2`。
- その他の分岐：既存ラベルの先頭に `"Override - "` を付ける（`"文字を入力中"` → `"Override - 文字を入力中"` 等）。`/press_key` の未検証キー 400 返却はフックの前に置いたままにする（ラベルに未検証の値が入らない原則を維持）。
- `noteOperation()` 自体のシグネチャとセッション管理（generation、in-flight 計数、自動終了タイマー）は変えない。

### 3.2 専用アイコン（新規 drawable）

- `res/drawable/ic_override.xml` を新規追加する。白シルエットのベクター（24dp ビューポート、背景は透明、白い単色シルエット、ステータスバー用）。指でタップする意匠とする。正確なパスデータは実装時に決める。小さく表示してもタップ動作と認識できる形状にする。
- `postOperationRunning()` の `.setSmallIcon(android.R.drawable.ic_dialog_info)` を `R.drawable.ic_override` に置き換える（「操作中」通知のみ。`postOperationEnded()` と更新通知は変更しない）。
- 既存の `ic_dialog_info` を他で使っている箇所があれば、そのまま残す（削除しない）。

### 3.3 スロットルと遅延投稿（`postOperationRunning()` 内。P1-01・P2-03 対応）

新規フィールド（`operationLock` の保護下で扱う。`operationScheduler` は自動終了タイマーと共用する）：

- `operationLastNotifyMs`（`elapsedRealtime`、初期値 0）。
- `hasPostedInSession`（boolean、初期値 false。セッション開始時に false に戻す。`operationLastNotifyMs = 0` による初回判定は使わない）。
- `pendingLabel`（String、null＝保留なし）。
- `pendingGeneration`（long、保留ラベルのセッション世代）。
- `pendingPostFuture`（`ScheduledFuture`、null＝遅延投稿の予約なし）。

動作：

1. `postOperationRunning(label)` は、セッション開始直後（`!hasPostedInSession`）なら即投稿し、`hasPostedInSession = true`、`operationLastNotifyMs = now` とする。
2. 2 回目以降は、`now - operationLastNotifyMs >= 500` なら即投稿し、`operationLastNotifyMs = now` とする。同時に、保留中の遅延投稿があれば取り消す（`pendingPostFuture.cancel(false)`、`pendingLabel = null`）。
3. `now - operationLastNotifyMs < 500` なら即投稿せず、`pendingLabel = label`、`pendingGeneration = operationGeneration` とし、遅延投稿を予約する（予約済みなら予約はそのままにし、ラベルだけ更新する）。予約時刻は `operationLastNotifyMs + 500`。スキップは失敗ではないため、呼び出し側に false を返さず、`logThrottled` も出さない。
4. 遅延投稿タスクは発火時に `operationLock` を取り、次を確認してから投稿する：セッションがまだ有効（`operationActive` が true）で、世代が `pendingGeneration` と一致し、`pendingLabel` が null でないこと。すべて満たせば `pendingLabel` を投稿し（スロットルを迂回する強制投稿。投稿後に `operationLastNotifyMs = now`、`pendingLabel = null`、`pendingPostFuture = null` とする）。満たさなければ何もせず、保留を破棄する。
5. セッション終了時（`endOperationLocked()`、明示終了と自動終了の両方）は、`pendingLabel = null` とし、予約中の遅延投稿があれば取り消す。終了後の遅延投稿は行わない。
6. 通知投稿の失敗（`areNotificationsEnabled()` が false、`nm == null`）は従来通り操作を妨げない。遅延投稿の失敗も同じ扱いとする。

競合の整理：遅延投稿タスクと通常投稿は `operationLock` で直列化する。新しい操作が入ると保留ラベルは新しいものに置き換わるため、古いラベルが新しいラベルを上書きすることはない。自動終了タイマーと遅延投稿タイマーが同じタイミングで発火した場合、終了処理が先に保留を破棄すれば遅延投稿は何もしない（上記 4 の確認による）。

### 3.4 変更しないもの

- チャンネル（`muse-bridge-operation`、`IMPORTANCE_DEFAULT`）、ID（3／4）、`setOngoing(true)`、`setOnlyAlertOnce(true)`、自動終了 10 秒、`/operation_end` の挙動、`operationTapIntent()`。
- `BridgeAccessibilityService.java`、Python クライアント（`android_bridge.py`、`bridge_protocol.py`）は変更しない。`/batch` の内部ステップも変更しない。
- `targetSdk 34`・`minSdk 30` は変更しない。API 36 のクラス・権限は使わない（D5）。VM の SDK（android-34 のみ）でビルドできる。

## 4. テスト（Python、ソースレベル）

`test_android_app_client.py` に `test_override_*` を追加する。既存の `test_operation_*` とは別セクションにする。

- OV-01：`/click` のラベルが `Override - タップ 540/2196` になる（リクエストの x/y と一致する）。
- OV-02：`/swipe` のラベルが `Override - スワイプ 100/800 → 100/300` になる（開始・終了の両座標を含む）。
- OV-03：`/type_text` のラベルが `Override - 文字を入力中` であり、入力文字を含まない。
- OV-04：`/press_key` のラベルが `Override - キー操作中（BACK）` になる。未知のキーは従来通りフックの前に 400 を返す（通知は出ない）。
- OV-05：`/batch` のラベルが `Override - 複数の操作を実行中` であり、ステップの要約・座標を含まない。
- OV-06：スロットル — 500 ms 未満の連続投稿では 2 回目の即投稿が行われず、保留ラベルが遅延投稿される。500 ms 以上あければ即投稿される。新規セッションの初回投稿はスロットル対象外（`hasPostedInSession` フラグによる）。
- OV-07：通知 ID・チャンネル・`setOngoing`・`setOnlyAlertOnce` が v2.1 と同じである。「操作終了」の投稿フローは変わらない。
- OV-08：`ic_override.xml` が存在し、`postOperationRunning()` がそれを参照している（`ic_dialog_info` ではない）。
- OV-09：バージョン試験 — `VERSION_NAME 2.9.2`、`VERSION_CODE 55`。
- OV-10：D6 改訂の回帰防止 — 文面に画面テキスト・入力文字・URI・クリップボード内容が含まれないこと（座標と固定ラベルとアプリラベル以外は出ないこと）を、`/open`・`/type_text`・`/type_node`・`/scroll_node`・`/click_node`・`/notification_action`・`/launch_app`・`/press_key`（許可キーと未知のキー）のラベルで確認する。
- OV-11：スキップされた最後の操作が、遅延投稿によって最終的に通知に反映される（P1-01）。
- OV-12：新規セッション開始時は、直前の投稿時刻に関係なく即時投稿される（`hasPostedInSession` が false に戻る）。
- OV-13：セッション終了後に保留中の遅延投稿が実行されない（終了時に保留が破棄される）。
- OV-14：同時操作時に古いラベルが新しいラベルを上書きしない（保留ラベルは新しい操作で置き換わる。遅延投稿は世代を確認する）。
- OV-15：500 ms 境界の確認 — 499 ms・500 ms・501 ms での即投稿／保留の切り替わり。

### 責任分担（P1-04）

- Python テストはソース構造のみを証明する（ラベルの書式、スロットルの分岐、保留・遅延投稿の存在、世代確認の有無、ID・チャンネルの不変性）。
- Java の実行時動作（`NotificationManager` による実際の更新、Android 側の通知表示、スロットルと遅延投稿の競合、セッション終了と遅延投稿の競合、アイコンの描画）は、Python テストでは証明できない。コードレビュー（実装時の目視確認）と実機確認（§5）で補う。Java 側にユニットテスト環境がないことは、本仕様の不採用理由ではない。

## 5. ライブ確認（実機、v2.9.2）

- OV-L1：端末でタップする → 「操作中」通知が出て、文面が `Override - タップ x/y` で、タップした座標と一致する。
- OV-L2：スワイプする → 文面が `Override - スワイプ x1/y1 → x2/y2` で、開始・終了座標と一致する。
- OV-L3：連続タップ → 文面が最新の操作に更新される（途中のラベルがスロットルで飛ぶことがあるのは許容。ただし最後の操作は遅延投稿で必ず表示される）。
- OV-L4：セッション終了（10 秒無操作または `/operation_end`）→ 「操作中」が消え、「操作終了」が出る。
- OV-L5：操作中にステータスバーに新しいアイコン（ic_override）が見える。ステータスバーの小サイズ表示と通知シェードでの視認性を両方確認する。
- OV-L6：文字入力 → 文面が `Override - 文字を入力中` で、入力文字が見えない。
- OV-L7：通知権限を無効にする → 操作を実行する → 操作自体は正常に継続する。通知投稿の失敗がログに記録される。スロットルによるスキップは失敗ログに記録されない。

## 6. Edge cases

- 通知が無効（`areNotificationsEnabled()` が false）のとき：従来通り操作は続行し、`logThrottled` に残すだけ。スロットルによるスキップは失敗とみなさない（§3.3）。
- 座標が画面外（負数・画面サイズ超え）のとき：サーバ側は座標の範囲検証をしていない（既存の挙動）。文面にはリクエストの値をそのまま出す。範囲外のタップ自体の成否はサービスの返却に従う。なお `getInt()` が保証するのは整数型であることのみであり、画面範囲内であることではない（P2-01）。
- セッション中に端末を再起動したとき：通知は消える（OS の挙動）。再起動後の初回操作で新しいセッションの通知が出る。特別な復旧処理はしない。保留中の遅延投稿も消える（プロセスごと消えるため）。
- `ic_override.xml` がビルドに含まれなかったとき：ビルドが失敗する（リソース参照エラー）ため、出荷前に気づく。フォールバック（`ic_dialog_info` への退避）はしない。
- 遅延投稿と自動終了が競合したとき：§3.3 の手順 4・5 により、終了済みなら遅延投稿は何もしない。遅延投稿が先に走った場合は、その直後に自動終了が走り「操作終了」が出る（表示の順序は保証されないが、最終状態は「操作終了」になる）。

## 7. 範囲外（やらないこと）

- `/batch` 内のステップごとの座標表示。バッチ実行はサーバが `runBatch()` に丸投げしており、ステップごとの再通知には in-flight 計数の変更が必要になるため、v2 ではやらない。将来の拡張候補として残す。
- `/click_node` の座標表示。ノードの bounds 中心をサービス側から返す配管が必要になるため、v2 ではやらない。
- Now Bar のピル表示、Android 16 Live Updates。D5 の通り、この端末では対象外。
- 通知アイコンのアニメーション、通知の常駐表示（ロック画面での優先表示など）。
- CONS-078 P2-02 の堅牢化（`getPersistedUriPermissions()` の try/catch）は本仕様の範囲外。v2.9.2 の同梱候補として残し、実装時に判断する。
