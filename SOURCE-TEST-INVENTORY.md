# ソース-テスト対応表（Source-Test Inventory）

SPEC-KOTLIN-MIGRATION-001 D6 で作る対応表。Python テストファイルと、
それが検査する Android ソース（Java / Kotlin）・build.sh・Manifest・
res の対応を記す。バッチごとに更新する。

最終確認: 2026-11-14、Stage 3 第4バッチ（最終、HttpBridgeServer 変換＋C-04 織り込み）時。フルスイート: **571 passed、18 subtests**。
（test_android_app_client.py は 405 collected／387 def test_）
内訳：C-01完了時点の実績は 502 passed＋3 failed（build.sh のバージョン断言が
2.9.16/69 のままだった更新漏れ — 「505 passed」の報告は誤り）→ CONS-152修正で断言を
2.9.18/71 へ更新（3件復帰）＋新規 15 件（test_batch_outbox.py 8 件＋
test_android_app_client.py の CONS-152 ソース検査 7 件）→ CONS-153修正で断言を
2.9.19/72 へ更新（3件リネーム、件数不変）＋新規 7 件（test_batch_outbox.py 6 件：
別プロセス同時 register・別インスタンス同時 register・失敗後のロック解放・
_locked 全適用・VM側 P1-01 判定 2 件、test_android_app_client.py の CONS-153
ソース検査 1 件）→ CONS-154修正で新規 1 件（test_batch_outbox.py のファイルロック
初期化固定テスト。コード変更なし・ビルドは v2.9.19／code 72 のまま）→ CONS-156修正で
断言を 2.9.20/73 へ更新（3件リネーム、件数不変）＋新規 3 件（test_android_app_client.py
の CONS-156 検査 3 件：P2-1 IN_PROGRESS payloadHash ソース検査・P2-2 失敗後の outbox
未キャッシュ振る舞い・P2-3 confirm 64KB ソース検査）→ CONS-157指摘対応で新規 1 件
（test_android_app_client.py の CONS-157 成功経路テスト：_batch_outbox() が
BatchOutbox インスタンスを返すこと。コード変更なし・ビルドは v2.9.20／code 73 のまま）→ C-01-4 P0修正で
断言を 2.9.21/74 へ更新（3件リネーム、件数不変）。新規テストなし — 修正は Kotlin 側のみで、JVM レベルの
動作テストは未整備（既知の gap、CONS-130 P2-02）。検証はコンパイル＋フルスイート＋実機 C-01-4 再テスト。
→ Stage 3 第1バッチ（MainActivity 変換）で断言を 2.9.22/75 へ更新（3件リネーム、件数不変）＋新規 2 件
（test_android_app_client.py の Stage 3 検査 2 件：is_kotlin・D4 interop）。MainActivity.java は削除し
MainActivity.kt が正本。ビルドは v2.9.22/code 75 で BUILD OK（918,591バイト）。CONS-161 P2 修正で
setPersistedMode を String? 化（Java null 契約の維持）＋open class 化し、回帰テスト 1 件を追加（535 passed）。CONS-162 P1-01 修正で
refreshAddress() のラベル判定を tokenRevealed → addressRevealed に訂正（Java 原文は addressRevealed。初回変換で兄弟関数から
写し間違えた D5 回帰。光月が原文の直接照合で発見）＋回帰テスト 1 件を追加（536 passed）。
→ Stage 3 第2バッチ（LogActivity 変換）で断言を 2.9.23/76 へ更新（3件リネーム、件数不変）＋新規 4 件
（test_android_app_client.py の Stage 3 検査 4 件：is_kotlin・D4 interop・nullable_views・in_keyword）。LogActivity.java は削除し
LogActivity.kt が正本。drive001 テスト群（95 件）は _drive_log() を Kotlin ソース読取へ切替え、Java 構文の断言を Kotlin 構文へ
更新（synchronized(SAVE_LOCK)、`catch (_: Exception)`、プロパティ形式の persistedUriPermissions／isFinishing 等）。
ビルドは v2.9.23/code 76 で BUILD OK（922,684バイト、SHA-256 40a0cecaf3388a0c0fb0e87da5cb7f07e181fff493807f88639c9af09142981a）。javap で open class＋static ensureDriveInit を確認。
CONS-163 P2-01（companion の公開 ABI）の対応：`private companion object` 化を試したが、kotlinc が MainActivity.kt:244 の
LogActivity.ensureDriveInit 呼び出しを「companion が private」で拒否し、ビルドが壊れた。Kotlin 呼び出し側は @JvmStatic を companion 経由で
解決するため、private 化は Kotlin 呼び出し側と両立しない。companion は public のままに戻し、P2-01 は「採用しない（accepted risk）」として
CONS-163 再レビューで琥珀に判断を仰ぎ、ADOPT 9.7/10 で同意を得た。琥珀の推奨（「WON'T FIX — Accepted ABI Surface」として
理由・影響・受容理由・再検討条件を整理する）に従い、コメントをその形に書き直した。Java から見える static API は
ensureDriveInit 1件のまま。「JVM 上の公開 ABI が1つだけ」という説明は正しくない（Companion の ABI が残る）点もコメントに明記。
なお、P2-01 対応の試行・復旧・コメント書き直しで APK の SHA-256 が変わった（ba62aa25…→ab508728…→fc583e68…。
コメント文の変更によるデバッグ情報の差で、動作は同一）。
CONS-164 P2-02（未使用 import 3件：UriPermission・OutputStream・Map）を除去。grep で未使用を確認してから削除。ビルド・フルスイートは再実行済み（540 passed）。
CONS-078 P2-02 のテスト（test_override_p202_drive_init_check_failure_fails_closed）は Kotlin の try 式形式に更新。
→ Stage 3 第3バッチ（BridgeAccessibilityService 変換）で断言を 2.9.24/77 へ更新（3件リネーム、件数不変）＋新規 4 件
（test_android_app_client.py の Stage 3 検査 4 件：is_kotlin・D4 interop・jvmfield・null_contracts）。BridgeAccessibilityService.java は削除し
BridgeAccessibilityService.kt が正本。P1-01・W3・CONS-152 P2-02 の3件は Java ソース読取から Kotlin ソース読取へ切替え。
CONS-165（琥珀 Tier 1、ADOPT 9.5/10、P0:0/P1:0/P2:3）後の P2-01 修正：公開ネストクラス 13 件を `open` 化（D4 継承契約の維持。CONS-161 P2-02 前例）。
D4 テストを拡張し 13 クラス全ての `open` を断言（件数不変）。フルスイート 544 passed のまま。再ビルド BUILD OK（v2.9.24/code 77、926,781バイト、
SHA-256 b3271bd1703f8ad41aeef76f43788cd6e7e995db7392c4dda44e9f37cc10b8df）、javap で 13 クラス全て non-final を確認。P2-02（ループ条件）・P2-03
（BatchRegistry 公開タイミング）は WON'T FIX として記録（理由は CONS-165 ログ参照）。
（collectNodes の `node.recycle()` 2件、broadcast の `.setPackage(packageName)`、`@Volatile private var batchRegistry`）。
ビルドは v2.9.24/code 77 で BUILD OK（926,781バイト、SHA-256 64bcbdd9f41ae8fe1a7c822ed655b0c094cdfea514caaecc557e84c07d886b72）。
javap で open class＋static getInstance／ACTION_A11Y_STATE_CHANGED＋BatchResult・State の全フィールドが実フィールド（@JvmField）＋BatchResult の2コンストラクタを確認。
変換中の kotlinc エラー2件：(1) TakeScreenshotCallback は interface のため `object : TakeScreenshotCallback()` は「constructor がない」と拒否される（括弧なし `object : TakeScreenshotCallback` が正解。GestureResultCallback は抽象クラスのため括弧ありのまま）。(2) 二次コンストラクタだけのクラスには無引数コンストラクタが生成されない（`constructor(other) : this()` でも解消せず。明示の空プライマリ `class BatchResult()` が必要）。
→ Stage 3 第4バッチ（最終、HttpBridgeServer 変換＋C-04 織り込み）で断言を 2.9.25/78 へ更新（3件リネーム、件数不変）＋新規テスト関数 18 件
（Stage 3／C-04 検査 4 件：is_kotlin・D4 interop・C-04 guard ソース検査・BatchRegistry.discard＋C-04 クライアント 13 件：build_operation_context・builders attach・submit hash 不変・click guard context・session 保持・session_mismatch クリア・foreground_mismatch 非再送・operation_end クリア・launch_app／open の session 参加・batch guard context・notification_action session・guard context の package 必須＋回帰 1 件：handler-chain 重複分岐）＋構造化 503 パラメータへ
/notification_action 1 件を追加。追加ケース合計 19 件（test_android_app_client.py は 378→397 collected、全スイート 544→563 passed）。HttpBridgeServer.java は削除し HttpBridgeServer.kt（4,006行）が正本。既存テスト 28 関数の本文を変更（Kotlin 構文への更新 22 件＋振る舞い更新 6 件：click/swipe/type_text/press_key/batch/notification_action は fresh /state＋operation_context 付きで送信。session_id は初回ブートストラップで省略、2回目以降は保持した mock-session-1 を付与）＋2 関数をリネーム（locale／a11yoff の _24→_25。pilot build の版数更新は本文変更 28 件に含む）。モックの変更：a11y-off 時に /notification_action を 503 へ（C-04 SPEC §2.2 で guarded になったため。旧モックの「a11y-off で利用可」は SPEC と矛盾する古い記述）、guarded 応答に session_id（mock-session-1）を追加、409 SPEC エラー用の fail_body フックを追加。
変換中の kotlinc エラー3件：(1) HttpBridgeServer.java を残したまま .kt と一緒にコンパイルすると class redeclaration（削除が先）。(2) `import java.util.List` 等の明示 import は Kotlin のコレクション型と衝突する（ArrayList が List へ代入不可になる。import を除き Kotlin 既定の List／ArrayList を使う）。(3) `svc!!.batchRegistry` は private フィールドのため `svc!!.getBatchRegistry()` を使う。/repair_termux ハンドラの else 分岐の重複（構文エラー）も修正。
ビルドは v2.9.25/code 78 で BUILD OK（971,836バイト、SHA-256 646e818794aab43c5ea308e217b91ff360eaba88d740f0b8da274297547dd56c）。javap で open class＋synchronized start/stop/boundTailscaleIps＋checkGuard／runGuardedOperation＋BatchRegistry.discard＋OperationGuardException を確認。
**自己発見 P0（レビュー送付前に修正、APK 再ビルド）：** 変換後の handle() 鎖に /click と /launch_app の2ハンドラが「空の else-if 分岐＋同一条件の else-if（実体）」の重複形で書かれていた。空の最初の分岐が常にマッチするため実体は到達不能で、POST /click・/launch_app は無応答になる。kotlinc は通る（構文として有効）。Python スイートはモック相手のため見えず、KNOWN_PATHS テストは集合比較のため重複が潰れる。修正は空の最初の分岐を除去（各2行＋空行）。回帰テスト `test_p1_03_handler_chain_has_no_duplicate_branch` を追加（全29エンドポイントの `"..." == path` 出現回数を断言。/clipboard のみ GET＋POST で2回。修正前形状で発火することを確認済み）。新規テスト関数は 18 件（上記の内訳。回帰 1 件を含む）、フルスイートは 562→563 passed。パラメータケース 1 件を含む追加ケース合計は 19 件。
C-04 のクライアント側の設計判断（レビューで検証してもらう前提）：(a) guarded 操作は毎回 fresh /state を取ってから送信する（a11y-off 時は /state の 503 で先に失敗する。従来の click 503 と同じ失敗クラス）。(b) session_mismatch を観測したら保持した session_id を捨てる（捨てないと次回以降も同じ stale id で 409 し続ける。サーバーは active でない id は null の場合も含め常に 409 する）。(c) operation_end() 成功後に保持 id を捨てる（サーバー側も無効化する）。(d) error_id の抽出を全エラー status へ拡張（従来は 503 のみ。batch の definitive 判定への影響なし）。
なお、/notification_action は Java 原文では a11y サービスを一切使わなかった（BridgeNotificationListener 直接呼び出し）が、C-04 SPEC §2.2 の表が guarded ordinary screen operations に明記しているため、Kotlin 変換で requireService＋runGuardedOperation を付けた。これは C-04 による意図的な振る舞い変更（a11y-off では 503 foreground_unverifiable）であり、変換ミスではない。
**CONS-167 P2-02 訂正（2026-11-14）：** レビューパケット §6 の「New tests (18)」に対し列挙が 17 件（サーバー 4＋クライアント 12＋回帰 1）に見えたのは、クライアント列挙の数え落とし。実数は新規テスト関数 18 件（サーバー 4＋クライアント 13＋回帰 1）＋新規パラメータケース 1 件（test_app_a11y_off_structured_503 へ POST /notification_action を追加）＝追加ケース 19 件で、test_android_app_client.py の collected 378→397、全スイート 544→563 passed と一致する。数え方は collected ID の差分（batch3 ファイル 378 件と batch4 ファイル 397 件の ID 比較）で直接確認済み。既存テストの変更は本文変更 28 関数（Kotlin 構文更新 22＋振る舞い更新 6）＋リネーム 2 関数。
**CONS-167 修正ラウンド（2026-11-14、琥珀 Tier 1 CONDITIONAL 7.2/10、P0:0/P1:2/P2:2 を受けて）：** P1-01＝noteOperation に expectedSessionId 引数を追加し、セッション検証とセッション開始・更新を同じ operationLock 臨界区で原子化（検証→inFlight 増加の間に auto-end が割り込めなくなった）。guarded ハンドラ（/click・/swipe・/type_text・/press_key・/notification_action open・/launch_app・/open）は検証済み id を渡す。/notification_action reply は noteOperation が無かったため対称化して追加。ノード操作は session 概念なし（SPEC §2.8）のため 2 引数のまま。P1-02＝/batch の noteOperation を runBatchInternal 内の最終ガード通過後に移動（onFinalGuardPassed コールバック。replay／unknown／guard 拒否の batch は通知なし）。P2-01＝discard の永続化失敗時はレコードをメモリ上で UNKNOWN 化（complete() の前例。re-send は "unknown"、再実行なし）＋runBatch 側で戻り値を検査してログ。markUnknownInMemoryLocked のログ文頭の "complete: " を外し、呼び出し元ごとの reason をそのまま出す形に変更（complete() 側のログ文言がわずかに変わる。テストはログ文言を断言していない）。既存テスト 3 件の断言を新形状へ更新（OV-07・OV-09・null_contracts）。新規テスト 5 件（P1-01×2・P1-02×1・P2-01×2）。フルスイート **568 passed、18 subtests**（test_android_app_client.py は 402 collected／384 def test_）。バージョンは据え置き（修正ラウンドの前例：第1・第3バッチと同じく v2.9.25/code 78）。ビルドは BUILD OK（975,932バイト、SHA-256 ec34f03a6401042c4159627bc557f309a5b8394bf1ed94713bab09a0e930afa1。レビュー済み 646e8187… から変わった）。javap で runBatch／runBatchInternal の onFinalGuardPassed 引数（Function0<Unit>）＋discard を確認。単体 kotlinc の R 未解決 168 件は生成 R.java 未投入による既知のもので、実ビルド（aapt2 が R を先に生成）では 0 件。HttpBridgeServer.kt は 4,036行。
**CONS-168 修正ラウンド（2026-11-14、光月最終 CONDITIONAL 8.5/10、P0:0/P1:1/P2:2 を受けて）：** P1-01＝/batch の operationInFlight 漏れ。CONS-167 P1-02 修正で noteOperation を runBatch 内のコールバックへ移した結果、runBatch が ServiceTimeoutException（20 秒メインスレッドタイムアウト）を投げると opGeneration へのコピーがスキップされ、handle() の finally が operationFinished を呼ばず inFlight が残り続け、10 秒 auto-end が恒久的に敗北した（修正前＝P1-02 修正前は worker 側で noteOperation を先に呼んでいたためこの漏れは存在しなかった。P1-02 修正が作った窓）。修正：generation を AtomicLong 化し、runBatch を try/finally で包んで finally で opGeneration へコピー。同時にフックを finally で武装解除（batchHookArmed＋batchHookLock。worker がタイムアウトした後にキューに残っていたメインスレッドタスクが handle() 終了後に noteOperation するのを防ぐ。check-and-note と disarm-and-copy は同一ロックで相互排他。ロック順序は service monitor → batchHookLock → operationLock）。P2-01＝/notification_action のコメントが reply の noteOperation 追加と矛盾していたため文言を更新。P2-02＝TokenProvider.currentToken() は非 null String 宣言のため `expected == null` チェックを除去（唯一の実装は MainActivity.getOrCreateToken で String を返す）。requireBatchRegistry の `svc!!` を `svc ?: throw ServiceUnavailable` に変更（呼び出し側 5 件は変更なし）。既存テスト 2 件の断言を新形状へ更新（OV-09・CONS-167 P1-02 テスト。旧断言は新コードで不成立になることを grep で確認済み）。新規テスト 3 件（P1-01・P2-01・P2-02）。フルスイート **571 passed、18 subtests**（test_android_app_client.py は 405 collected／387 def test_）。バージョンは据え置き（v2.9.25/code 78）。ビルドは BUILD OK（959,548バイト、SHA-256 8b486014f4d2a30d250be98bca9e03099d3400c052beb0c0c9359f43edfc82c4。レビュー済み ec34f03a… から変わった）。dex 内に batchHookArmed／AtomicLong を確認。HttpBridgeServer.kt は 4,072行。

## テストファイル一覧（`~/workspace/computer-use-preview/` 直下）

| ファイル | `def test_` 数 | 検査対象 |
|---|---|---|
| `test_android_app_client.py` | 387 | 下記の Android ソース検査（`_java_source` / `_kotlin_source`）＋ VM 側クライアントの振る舞い（モックサーバー）。Stage 3 第4バッチで 405 collected（パラメータ化分を含む） |
| `test_bridge_protocol.py` | 88 | VM 側プロトコル検証（`bridge_protocol.py`）。Android ソースは検査しない |
| `test_android_bridge.py` | 8 | VM 側クライアント（`android_bridge.py`）。Android ソースは検査しない |
| `test_batch_outbox.py` | 37 | C-01 新規：VM 側 outbox（`bridge_protocol.py` の `BatchOutbox`）の判定表・固定送信順・再利用禁止・永続失敗・容量 purge・UNKNOWN 形状・supersedes・確定/未確定分類。CONS-152修正で耐久ゲート 4 件・並行性 2 件・CLI 2 件を追加。CONS-153修正で 6 件を追加（別プロセス同時 register・別インスタンス同時 register・失敗後のロック解放・`_locked` 全適用・VM側 P1-01 判定 2 件）。CONS-154修正で 1 件を追加（ファイルロック初期化固定テスト） |
| `test_agent.py` | 11 | 上流 computer-use のテスト。Bridge 対象外 |
| `test_agent_pages.py` | 7 | 上流 computer-use のテスト。Bridge 対象外 |
| `test_page_manager.py` | 13 | 上流 computer-use のテスト。Bridge 対象外 |
| `test_main.py` | 2 | 上流 computer-use のテスト。Bridge 対象外 |

## `test_android_app_client.py` のソース検査対応

`_java_source(name)` は `android-app/src/dev/muse/bridge/<name>` を読む。
`_kotlin_source(name)` は同じディレクトリの `.kt` を読む（パイロットで新設）。
`_manifest_source()` / `_res_text(rel)` は Manifest / res を読む。

| Android ソース | 参照数 | 主なテスト（代表） |
|---|---|---|
| `HttpBridgeServer.kt`（Stage 3 第4バッチで Java から移行） | 14 | P1-06 世代チェック、リクエストログ（KNOWN_PATHS ガード：C-01 で管理エンドポイント 5 つを追加）、サーバー設定、Stage 3 検査 3 件（is_kotlin・D4 interop・C-04 guard）＋C-04 クライアント 13 件（build_operation_context 他）。`_override_server()` 経由の OV 検査も同ファイルを読む |
| `BridgeWidgetProvider.java` | 4 | ウィジェットボタン／PendingIntent |
| `MainActivity.kt`（Stage 3 第1バッチで Java から移行） | 14 | Drive 初期化フック、AppVersion 呼び出し、モード切替のウィジェット更新、PingProber／通知／VpnBindSelector の呼び出し側、Stage 3 検査 2 件（is_kotlin・D4 interop） |
| `BridgeWidgetActionReceiver.java` | 2 | ウィジェットアクション分岐 |
| `BridgeLog.kt`（バッチGで Java から移行） | 5 | ログ初期化・PII 除去（T-33/T-36は .kt 読取へ更新） |
| `BatchRegistry.kt`（新規・C-01） | 10 | 永続レジストリ。ソース検査は KNOWN_PATHS ガードとモックの batch 振る舞い（再送禁止・UNKNOWN）。JVM レベルの単一ロックは `javap -c` で確認（monitorenter が lock に集約）。CONS-152修正でソース検査 7 件を追加（P0-01 tombstone 再計算＋admit 順序、P0-02 CORRUPT ガード、P1-01 読み取り不能、P1-04 supersedes 確認必須、P2-01 512KB 上限）。CONS-153修正でソース検査 1 件を追加（P1-01 不存在の積極確認：Files.notExists／undeterminable は CORRUPT／NoSuchFileException 競合は null）。CONS-156修正でソース検査 2 件を追加（P2-1 IN_PROGRESS の payloadHash 照合、P2-3 confirm の 64KB 上限）。javap 再確認（c01-fix3-BatchRegistry.txt）：BatchMismatchException 参照 6 箇所（投げ 3 箇所）、monitorenter 8 箇所のまま |
| `BridgeAccessibilityService.kt`（Stage 3 第3バッチで Java から移行） | 7 | P1-01 collectNodes recycle（Kotlin 読取へ切替え）。W3 broadcast の setPackage（Kotlin 読取へ切替え）。CONS-152修正で `batchRegistry` の volatile 化（P2-02、Kotlin 読取へ切替え）＋javap で確認。Stage 3 検査 4 件（is_kotlin・D4 interop・jvmfield・null_contracts）。CONS-165 P2-01：公開ネストクラス 13 件を `open` 化（D4 テストで断言、javap で non-final 確認）。P2-02・P2-03 は WON'T FIX（CONS-165 ログ参照） |
| `UriPermissionRegistry.kt` | 1 | 権限レジストリ（バッチDで Java から移行） |
| `PingProber.kt`（バッチEで Java から移行） | 3 | ping 往復測定（※移行前はソース検査ゼロだった） |
| `BridgeNotificationListener.kt`（バッチFで Java から移行） | 3 | 通知イベント流（※移行前はソース検査ゼロだった） |
| `LogActivity.kt`（Stage 3 第2バッチで Java から移行） | 99 | Drive 初期化フック、AppVersion 呼び出し、drive001 検査 95 件＋Stage 3 検査 4 件（is_kotlin・D4 interop・nullable_views・in_keyword） |
| `LocaleTileService.kt`（バッチBで Java から移行） | 1 | ロケールタイル起動・帰属行 |
| `BridgeService.kt`（バッチBで Java から移行） | 1 | サービス起動 |
| `BootReceiver.kt`（バッチAで Java から移行） | 2 | Drive 初期化フック、Kotlin 化確認・帰属行 |
| `VpnBindSelector.kt`（新規・バッチA） | 5 | Kotlin 化確認・相互運用注釈（`@JvmStatic` / `@JvmField` / `const val`）、Java 呼び出し元不変、帰属行、P2対応（Regex定数化・null要素防御）（※移行前はソース検査ゼロだった） |
| `BridgeTileService.kt`（バッチBで Java から移行） | 1 | タイル状態・更新 |
| `A11yOffTileService.kt`（バッチCで Java から移行） | 1 | QS タイル状態・更新 |
| `A11yOffExecutor.kt`（バッチCで Java から移行） | 1 | 実行部ログ・完了トリガー種別 |
| `InteropProbeJava.java`（新規・パイロット） | 2 | 双方向フィクスチャ（Java→Kotlin）、帰属行 |
| `AppVersion.kt`（新規・パイロット） | 2 | Kotlin 化確認、スレッドセーフ注釈（`@Volatile` / `@Synchronized` / `@JvmStatic`） |
| `InteropProbe.kt`（新規・パイロット） | 1 | 双方向フィクスチャ（Kotlin→Java）、帰属行 |

`build.sh` の検査（バージョン・パイプライン）:

| 検査 | テスト |
|---|---|
| `VERSION_NAME="2.9.25"` / `VERSION_CODE=78`（Stage 3 第4バッチ） | `test_locale_tile_version_2_9_25`、`test_a11yoff002_version_2_9_25`、`test_kotlin_pilot_build_sh_two_compiler_pipeline`（バージョン断言のみ更新） |
| kotlinc ステップ（`-jvm-target 17`、256m、2 コンパイラ・パイプライン）、javac の `-cp` に `$KOTLIN_STDLIB`、d8 に stdlib を 1 個だけ | `test_kotlin_pilot_build_sh_two_compiler_pipeline` |

## 運用ルール（D6）

- 更新するのは、ソース形式の変更の影響を受けるテストだけ。実行するのは毎バッチ、全スイート。
- 既存の振る舞いアサーションとカバレッジは保つ。言語が変わったからといってテストを削除しない。
- Java ソースの `public static final` を探すテストは、同等の Kotlin 宣言と生成される JVM の振る舞いを認識するよう書き換える。JVM レベルの性質（volatile、synchronized、static メンバ、シグネチャ）は `javap -v` で実ビルド出力を確認する（T-M1）。ソース検査はその注釈が残っていることのガードである。
- この表はバッチごとに更新し、移行を見通せるようにする。
