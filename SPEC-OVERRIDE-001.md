# Muse Bridge — 操作中のライブ通知「Override - x/y」Spec (Draft)

- Status: Draft（GPT レビュー未実施。採用・凍結はサイガマンの決定による）
- Source: サイガマンの提案 2026-11-08 07:46 +07「Museが端末を操作中にステータスバーに「Override - x/y」みたいなライブ通知追加できない？　君が操作中に間違って操作するのが申し訳ない」
- Version target: v2.9.2 / code 55
- Local file: `~/workspace/muse-bridge-repo/SPEC-OVERRIDE-001.md`（正本。`~/workspace/muse-bridge/SPEC-OVERRIDE-001.md` は同期コピー）
- 前提となる既存仕様：SPEC-OPERATION-NOTIFY.md（v2.1、操作中／操作終了の通知）

## 0. Goal

VM 側が端末の画面を操作している間、Bridge アプリの「操作中」通知の文面を、操作のたびに「Override - タップ 540/2196」のようにライブ更新する。サイガマンがステータスバーのアイコンと通知シェードの文面で「今こっちが操作中だよ、ここを触ってるよ」と一目で分かり、誤って端末を触ることを防ぐ。通知はあくまで観察者であり、サイガマンのタッチを止める手段ではない（止める手段は Android に存在しない）。

## 1. Locked decisions

| # | Decision |
|---|---|
| D1 | 土台は v2.1 の操作通知をそのまま使う。チャンネル `muse-bridge-operation`、ID 3＝「操作中」（ongoing）、ID 4＝「操作終了」（one-shot）、自動終了 10 秒、タップで Bridge アプリの MainActivity を開く、という挙動は変えない。 |
| D2 | 文面の書式は `Override - <操作> <座標>` とする。タイトルは「操作中」のまま（タイトル＝セッションの状態、文面＝今の操作、という v2.1 の役割分担を維持する）。 |
| D3 | **SPEC-OPERATION-NOTIFY の D6（通知に座標を載せない）を改訂する。** 改訂の理由：座標は Muse 自身の操作パラメータであり、画面の文字・入力内容・URI・クリップボードといったユーザーデータではない。`/press_key` のキー名（固定 enum）が既に許されているのと同じ原則で、検証済みの整数座標は文面に入れてよい。画面の内容が漏れることはない。 |
| D4 | 座標を載せるのは、リクエストに検証済みの座標が含まれる操作だけ（`/click`、`/swipe`）。それ以外は既存ラベルに `Override - ` を付けるだけとし、内容は変えない（入力文字・URI は載せない）。 |
| D5 | **One UI 7.0 対応のみ。** この端末は赤ロム（中古・キャリア制限端末を管理者権限で突破したもの）で、One UI 8 への更新はできない（更新すると突破が壊れるおそれがあり、現状が最終状態）。そのため Android 16 の Live Updates（`Notification.ProgressStyle`、`POST_PROMOTED_NOTIFICATIONS`）はこの端末では永久に対象外とする。Now Bar のピル表示も対象外（One UI 7.0 にはサードパーティ製アプリ向けの公式 API がない。許可リスト入りの Google アプリだけが出せる）。この仕様で実現するのは、ステータスバーのアイコン＋通知シェードの文面である。 |
| D6 | 通知は観察者であり、ゲートではない（v2.1 D5 を維持）。通知の投稿に失敗しても操作は続行し、失敗はログに残すだけとする。 |
| D7 | 通知のタップは Bridge アプリの MainActivity のみを開く（v2.1 D7 を維持）。他アプリの起動には使わない。 |
| D8 | 文面の更新はスロットルする。連続する投稿の間隔が 500 ms 未満のときは投稿をスキップし、最新のラベルを次の投稿に回す（ライブ表示であり、ログではないため、途中のラベルが飛ぶことは許容する）。 |

## 2. ラベル書式（文面。タイトルは常に「操作中」）

| Endpoint | 新しいラベル |
|---|---|
| `/click` | `Override - タップ 540/2196` — リクエストの x/y をそのまま使う（検証済みの整数。`getInt` の後でフックするため、未検証の値が入ることはない） |
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

- `res/drawable/ic_override.xml` を新規追加する。白シルエットのベクター（24dp ビューポート、ステータスバー用）。指でタップする意匠とする。正確なパスデータは実装時に決める。
- `postOperationRunning()` の `.setSmallIcon(android.R.drawable.ic_dialog_info)` を `R.drawable.ic_override` に置き換える（「操作中」通知のみ。`postOperationEnded()` と更新通知は変更しない）。
- 既存の `ic_dialog_info` を他で使っている箇所があれば、そのまま残す（削除しない）。

### 3.3 スロットル（`postOperationRunning()` 内）

- フィールド `operationLastNotifyMs`（`elapsedRealtime`、初期値 0）を追加する。
- 投稿時に `now - operationLastNotifyMs < 500` なら投稿をスキップし、false ではなく true を返す（スキップは失敗ではないため、呼び出し側の `logThrottled` を出さない）。500 ms 以上なら投稿し、`operationLastNotifyMs = now` とする。
- セッション終了時（`endOperationLocked()` での `nm.cancel(OPERATION_NOTIFICATION_ID)`）はスロットルと無関係に行う。`operationLastNotifyMs` のリセットはしない（次セッションの初回投稿が 500 ms 以内でも、セッション開始の表示が遅れるより、即表示を優先する — 初回投稿はスロットル対象外とする。実装：`noteOperation()` で新規セッション開始時（`!operationActive` → `true` になる分岐）は `operationLastNotifyMs = 0` にしてから `postOperationRunning()` を呼ぶ）。

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
- OV-06：スロットル — 500 ms 未満の連続投稿では 2 回目の `nm.notify` が呼ばれない。500 ms 以上あければ呼ばれる。新規セッションの初回投稿はスロットル対象外。
- OV-07：通知 ID・チャンネル・`setOngoing`・`setOnlyAlertOnce` が v2.1 と同じである。「操作終了」の投稿フローは変わらない。
- OV-08：`ic_override.xml` が存在し、`postOperationRunning()` がそれを参照している（`ic_dialog_info` ではない）。
- OV-09：バージョン試験 — `VERSION_NAME 2.9.2`、`VERSION_CODE 55`。
- OV-10：D6 改訂の回帰防止 — 文面に画面テキスト・入力文字・URI・クリップボード内容が含まれないこと（座標と固定ラベルとアプリラベル以外は出ないこと）を、`/open`・`/type_text`・`/launch_app` のラベルで確認する。

## 5. ライブ確認（実機、v2.9.2）

- OV-L1：端末でタップする → 「操作中」通知が出て、文面が `Override - タップ x/y` で、タップした座標と一致する。
- OV-L2：スワイプする → 文面が `Override - スワイプ x1/y1 → x2/y2` で、開始・終了座標と一致する。
- OV-L3：連続タップ → 文面が最新の操作に更新される（途中のラベルがスロットルで飛ぶことがあるのは許容）。
- OV-L4：セッション終了（10 秒無操作または `/operation_end`）→ 「操作中」が消え、「操作終了」が出る。
- OV-L5：操作中にステータスバーに新しいアイコン（ic_override）が見える。
- OV-L6：文字入力 → 文面が `Override - 文字を入力中` で、入力文字が見えない。

## 6. Edge cases

- 通知が無効（`areNotificationsEnabled()` が false）のとき：従来通り操作は続行し、`logThrottled` に残すだけ。スロットルは失敗とみなさない（§3.3）。
- 座標が画面外（負数・画面サイズ超え）のとき：サーバ側は座標の範囲検証をしていない（既存の挙動）。文面にはリクエストの値をそのまま出す。範囲外のタップ自体の成否はサービスの返却に従う。
- セッション中に端末を再起動したとき：通知は消える（OS の挙動）。再起動後の初回操作で新しいセッションの通知が出る。特別な復旧処理はしない。
- `ic_override.xml` がビルドに含まれなかったとき：ビルドが失敗する（リソース参照エラー）ため、出荷前に気づく。フォールバック（`ic_dialog_info` への退避）はしない。

## 7. 範囲外（やらないこと）

- `/batch` 内のステップごとの座標表示。バッチ実行はサーバが `runBatch()` に丸投げしており、ステップごとの再通知には in-flight 計数の変更が必要になるため、v1 ではやらない。将来の拡張候補として残す。
- `/click_node` の座標表示。ノードの bounds 中心をサービス側から返す配管が必要になるため、v1 ではやらない。
- Now Bar のピル表示、Android 16 Live Updates。D5 の通り、この端末では対象外。
- 通知アイコンのアニメーション、通知の常駐表示（ロック画面での優先表示など）。
- CONS-078 P2-02 の堅牢化（`getPersistedUriPermissions()` の try/catch）は本仕様の範囲外。v2.9.2 の同梱候補として残し、実装時に判断する。
