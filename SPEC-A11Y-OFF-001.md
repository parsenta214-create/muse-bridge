# Muse Bridge — 補助機能オフのショートカット Spec (Draft)

- Status: Draft（GPT レビュー CONS-092 は条件付き採用（P0=0、P1×1、P2×0）。v10 を作成し、再レビュー CONS-093 に回す。採用・凍結はサイガマンの決定による）
- Source: サイガマンの提案 2026-11-08 09:04 +07「Muse Bridgeをオフにしたらアクセシビリティをオフにする機能、またはMuse_Bridge単体のアクセシビリティをオフにするショートカットを別個で作るのは？」→ 2026-11-08 09:09 +07、サイガマンがショートカットの方を選択
- Version target: v2.9.3 / code 56
- Local file: `~/workspace/muse-bridge-repo/SPEC-A11Y-OFF-001.md`（正本。`~/workspace/muse-bridge/SPEC-A11Y-OFF-001.md` は同期コピー）

## 0. Goal

Muse Bridge のアクセシビリティサービスだけを、ホーム画面のショートカット一発でオフにできるようにする。使い終わったあとに補助機能を切っておきたいとき（銀行アプリの検出を避けたい、使っていない機能は切っておきたい）、設定画面を何段も開かずに済ませる。オンに戻すのは従来通り設定画面での手動操作（Android の制限）。

## 1. 判断（D1〜D6）

- D1：**ショートカット方式**とする。Bridge オフと連動する自動オフはやらない。サイガマンの選択（2026-11-08 09:09 +07）。連動方式は、次に使うたびに設定画面へ行く手間が増えるため見送る。将来、設定でオン・オフを選べる形にする場合は別仕様とする。
- D2：オフの仕組みは **`AccessibilityService.disableSelf()`** とする。サービスは自分自身をオフにできる（API 24+、minSdk 30 で利用可）。呼び出し元は `BridgeAccessibilityService.getInstance()`（`onServiceConnected` で設定され、`onDestroy` で null に戻る）。
- D3：**オフ専用**とする。オンに戻すショートカットは作らない。アプリ側から自分のアクセシビリティをオンにはできない（Android の制限）。オンに戻すのは設定 → ユーザー補助 → インストール済みアプリ → Muse Bridge での手動操作。
- D4：ショートカットは**サーバを止めない**。補助機能だけをオフにする。サーバは動き続けるが、画面操作は `ACCESSIBILITY_DISABLED` で失敗する（既存の挙動）。サーバを止めるのは従来の停止ボタン（アプリ・ウィジェット・タイル）で行う。
- D5：ショートカットの作成は**アプリ内のボタン**から行う。`ShortcutManager.requestPinShortcut()` を**ワーカースレッド**で呼び、システムのピン留め確認ダイアログでユーザが確認してからホーム画面に置く。作成の確認はシステムのダイアログに任せる。`PinRequestManager`（Application スコープの static シングルトン）は、4つの独立した状態を保持する。① UI 状態 `uiState`（IDLE／REQUESTING／WAITING_CONFIRMATION。ボタンの有効・無効は `uiState == IDLE` を映す。旧仕様の COMPLETED は廃止し IDLE に統合する）、② ワーカー状態 `workerRunning`（true／false）、③ 未解決要求状態 `unresolvedState`（NONE／REQUESTING_UNRESOLVED／CONFIRMATION_UNRESOLVED）、④ ライフサイクル記録 `pauseObserved`／`resumeObserved`（参考情報として記録するのみ）。すべての状態の読み書きと遷移処理はメインスレッドに直列化する。ワーカーで VM の致命的な Error が発生した場合は、ワーカー側の finally が Error 専用の復旧イベントをメインスレッドへ通知し、`PinRequestManager` がメインスレッドで `workerRunning = false` にし、`uiState` が `REQUESTING` なら `IDLE` に戻し、API 受理状況が不明なため `unresolvedState` を `CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマを開始する。Error は捕捉せず再スローする。プロセス終了時にはメモリ上の状態復旧を保証しない。`onPause → onResume` は復帰の参考情報として記録するのみとし、未解決要求の解除はタイムアウトで行う。Activity 再生成時は既存要求の状態を引き継ぐ。
- D6：**新規権限なし、新規 drawable なし、新規コンポーネントなし**。`ShortcutManager` に権限は不要。アイコンは既存の `R.drawable.ic_tile` を流用する。ショートカットの intent は MainActivity への明示 intent で、新しい exported コンポーネントは作らない。マニフェストの変更は、トークンのバックアップ除外のための `fullBackupContent` 属性と `dataExtractionRules` 属性の追加のみ認める（P2-02 の決定による。v5 で D6 を改定し、v6 で dataExtractionRules を追加）。
- D7：**無効化 intent はアプリ内トークンを運ぶ**。トークンは UUID を初回に一度だけ生成し、アプリ内の専用 SharedPreferences ファイル `a11y_off_token_prefs` に保存する（他アプリからは読めない）。ショートカット作成時に intent の extra として埋め込む。MainActivity は、アクション一致かつ extra のトークンが保存値と一致するときだけ無効化を実行する。保存値または extra が null・空文字の場合は一致とみなさず、無効化を実行しない。トークンを例外メッセージやデバッグログに出力しない。一致しない intent（他アプリからの送信を含む）は無言で無視する（トーストも出さない、無効化もしない）。アクション名だけでは認証にならないため、このトークンが外部起動の防波堤になる。トークンは作り直しでも変わらない（保存値を再利用する）ため、古いショートカットも動き続ける。保存は `commit()`（同期・成否が返る）で行い、保存の成功を確認してからショートカット作成要求を送る。保存に失敗した場合は作成しない。トークンは認証情報として扱い、ログに出力しない。これは呼び出し元アプリの身元を確認する仕組みではなく、秘密情報を持つ intent だけを受け付ける Bearer Token 方式である。トークンを取得した第三者による再送は防げない。ルート化端末やランチャー侵害など、アプリ外の信頼境界の侵害は対象外とする。デフォルトランチャーはトークンを含む intent を取り扱う信頼主体であり、信頼境界の内側に置く。信頼できないランチャーの利用は保証対象外とする。トークンは端末固有の認証情報とし、バックアップ・端末移行の復元対象から除外する（専用の SharedPreferences ファイル `a11y_off_token_prefs`（実ファイル `a11y_off_token_prefs.xml`）に保存し、バックアップ除外ルールで対象外にする）。除外は fullBackupContent（Android 11 以前）と dataExtractionRules（Android 12 以降）の両方で行い、dataExtractionRules では cloud-backup と device-transfer の両方から除外する。targetSdk 34 のため、dataExtractionRules が主の仕組みになる。復元後はトークンが存在しないため、古いショートカットは動かなくなる（アプリ内のボタンから作り直す）。実際のバックアップ挙動は Android のバージョンで異なるため、実装時にビルド設定と照合して確認する。トークン漏洩後の失効・再発行機構は現仕様には存在しない。
- D8：**ショートカット intent の flags を固定する**。`FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP | FLAG_ACTIVITY_SINGLE_TOP`。MainActivity に launchMode の指定はなく（標準）、flags なしでは毎回新しいインスタンスが作られるため。

## v1 → v2 の変更点（CONS-084 対応、2026-11-08）

- P1-01：MainActivity は `android:exported="true"`（ランチャー Activity、マニフェストで確認済み）のため、他アプリが明示 intent で同じアクションを送れる。対策として D7（アプリ内トークン）を追加する。アクション名だけでは認証にならないことを明記する。
- P1-02：`getInstance()` が null の場合を二分する。`MainActivity.isAccessibilityEnabled()`（既存の `ENABLED_ACCESSIBILITY_SERVICES` 読み取り）で確認できた無効は「すでにオフです」、確認できない場合は「サービスに接続されていません。状態を確認してください」とする。
- P1-03：MainActivity に launchMode の指定がない（標準）ことを確認済み。ショートカット intent の flags を明記する（D8）。`onNewIntent` で `setIntent(intent)` を呼ぶ。AX-03 を flags＋一度だけの処理の確認に改める。
- P2-01：`requestPinShortcut()` の true は「要求を送信できた」であって「設置できた」ではないことを明記する。コールバックは null のまま（新規コンポーネントを作らない）。例外時は try/catch でトーストを出す。
- P2-02：呼び出し自体はすぐ返る（待つのはシステムのダイアログ）。連打防止のため、要求中は作成ボタンを無効化する。フォアグラウンド Activity からの呼び出しを維持する。
- P2-03：操作中の無効化を追加する。サーバはクラッシュせず、操作中通知は残留せず、次の要求は 503 になることを確認する（AX-17、AX-L6）。
- テスト追加：AX-13（トークン）、AX-14（null 分離）、AX-15（Activity ライフサイクル）、AX-16（作成異常系）、AX-17（操作中の無効化・ソース）、AX-L6（操作中の無効化・実機）、AX-L7（連続実行）。

## v2 → v3 の変更点（CONS-085 対応、2026-11-08）

- P1-01：`requestPinShortcut()` の実行時間の認識を訂正する。公式リファレンスでは数秒かかる場合があり、ワーカースレッドからの呼び出しが推奨されている。v2 の「呼び出し自体はすぐ返る」は誤りだった。作成要求はワーカースレッドで実行し、結果を UI スレッドに返す。ボタンは押下時に無効化し、システムの確認ダイアログが終了して Activity に戻ったこと（onResume）を確認してから再有効化する。フォアグラウンド Activity からの呼び出しという要件は、ワーカースレッド経由でも満たされる。
- P1-02：§6 の古い記述（v1 の名残）が §3.2 と矛盾していたため修正する。状態判定を 3 状態にする（ENABLED／DISABLED／UNKNOWN）。`isAccessibilityEnabled()` の読み取り失敗（例外時）は UNKNOWN とし、「状態を確認できませんでした」と表示して無効化はしない。サーバ停止とアクセシビリティ無効化は別の状態として扱う。
- P2-01：トークンの永続性を保証する。`commit()`（同期・成否が返る）で保存し、保存の成功を確認してからショートカット作成要求を送る。保存に失敗した場合は作成しない。
- P2-02：D7 のセキュリティ境界を明記する。トークンは Bearer Token 方式であり、呼び出し元アプリの身元確認ではない。トークンは認証情報として扱い、ログに出力しない。漏洩後の再送は防げない。ルート化端末やランチャー侵害は対象外とする。
- P2-03：連続実行の抑制を追加する。`disableInProgress` フラグで同一 Activity 内の重複実行を抑え、トーストの重複も防ぐ。処理の冪等性は維持する。
- テスト追加：AX-18（UI スレッド非占有）、AX-19（読み取り失敗時に誤って無効化しない）、AX-20（トークン保存失敗時に作らない）、AX-21（トークン 3 パターン）、AX-22（連続タップ）、AX-23（ダイアログ終了後のボタン再有効化）。

## v3 → v4 の変更点（CONS-086 対応、2026-11-08）

- P1-01：onResume 単独の再有効化をやめ、作成要求の状態機械（IDLE／REQUESTING／WAITING_CONFIRMATION／COMPLETED）を追加する。早期再有効化と再有効化されない場合の両方を防ぐ。タイムアウト（30 秒）による復旧経路を設ける。世代カウンタで遅れた結果の取り違えを防ぐ。Activity 再生成時は状態が IDLE に戻る。
- P2-01：戻り値の意味を訂正する。true は「ランチャーがこの機能をサポートしている」、設置成功は未確定。false にはランチャー非対応のほか制限プロファイル・App Lock もある。isRequestPinShortcutSupported() による事前確認を追加する。
- P2-02：デフォルトランチャーはトークンを含む intent を取り扱う信頼主体であることを明記する。信頼できないランチャーは保証対象外。バックアップ・端末移行によるトークンの扱いは別途確認する。漏洩後の失効・再発行機構は現仕様に存在しない。
- P2-03：2 秒タイムアウト時は単純解除せず、Android 側の状態を再確認する。ENABLED ならフラグを維持して再確認を繰り返す（最大 3 回まで）。disableInProgress を static（プロセス内共有）にする。
- テスト追加：AX-24〜AX-31。

## v4 → v5 の変更点（CONS-087 対応、2026-11-08）

- P1-01：作成要求の排他管理を Activity の寿命から分離する。Application スコープの要求管理クラス（PinRequestManager、static シングルトン）を設け、要求状態と世代番号を Activity 間で共有する。Activity 再生成時は既存要求の状態を引き継ぐ（IDLE に戻さない）。ワーカーの完了結果は現在の要求 ID と照合する。古い Activity に UI 更新を行わない。実行中の要求がある場合は新しい要求を開始しない。
- P1-02：無効化完了イベントに世代管理を追加する。無効化処理ごとに操作世代番号を持つ。ブロードキャスト受信時は Android 側の状態を再確認し、DISABLED のときだけフラグを下ろす。ENABLED の場合は維持する。フラグの読み書きと世代の更新はメインスレッドに直列化する。
- P2-01：30 秒タイムアウトを「要求終了」と「UI 復旧」に分ける。タイムアウトは UI を復旧させるが、未解決要求として記録し、未解決の間は新しい要求の発行を制限する。未解決は次の onResume か、さらに 60 秒後のいずれかで解除する。REQUESTING のタイムアウトはワーカーのキャンセルを意味しない。
- P2-02：トークンのバックアップ方針を決定する。トークンは端末固有の認証情報とし、バックアップ・端末移行の復元対象から除外する（専用 SharedPreferences ファイル＋バックアップ除外ルール）。復元後は古いショートカットが動かなくなる（作り直す）。D6 を改定する（新規コンポーネント・新規権限なし。バックアップ除外のための fullBackupContent 属性の追加のみ認める）。
- P2-03：成功トーストを無効化の完了確認後に移す。disableSelf() 直後には成功を通知しない。ブロードキャスト受信後に DISABLED を確認してから「補助機能をオフにしました」を出す。6 秒以内に確認できない場合は「無効化の状態を確認できませんでした」と出す。
- テスト追加：AX-32〜AX-40。

## v5 → v6 の変更点（CONS-088 対応、2026-11-08）

- P1-01：onResume 単独では未解決要求を解除しない。要求開始後の onPause を確認し、その後の onResume が対応する復帰イベントである場合に限り解除する。無関係な onResume では解除しない。確認画面が Activity を pause しない場合は、ライフサイクルイベントだけでは終了を判定できないため、タイムアウト経路だけが解除する。
- P1-02：REQUESTING タイムアウト後は、ワーカー終了を確認するまで新規要求を許可しない。ワーカー終了と要求終了を別々に管理する（workerRunning フラグ）。世代番号による結果の無視と、API 呼び出しの排他制御を分離する。WAITING_CONFIRMATION のタイムアウトは REQUESTING と別の解除条件にする。
- P1-03：バックアップ除外を Android 12 以降の D2D 転送まで拡張する。fullBackupContent（Android 11 以前）に加え、dataExtractionRules（Android 12 以降）で cloud-backup と device-transfer の両方からトークンを除外する。targetSdk 34 のため dataExtractionRules が主の仕組みになる。実装時にビルド設定と照合する。
- P2-01：プロセス再生成を未対応ケースとして明記する。Activity 再生成とプロセス再生成は別の障害ケースである。プロセス再生成後は、以前の要求が完了したと断定せず、不確定状態として扱う。プロセスをまたぐ永続的な要求管理の要否は実機検証の結果で判断する。
- P2-02：バックアップ検証を経路別に分ける（AX-41〜AX-45）。
- テスト追加：AX-46〜AX-50。

## v6 → v7 の変更点（CONS-089 対応＋ Argon 解法、2026-11-08）

- P1-01：`PinRequestManager` の状態を4つの直交する軸に完全分離する（UI状態 `uiState`、ワーカー状態 `workerRunning`、未解決要求状態 `unresolvedState`、ライフサイクル記録 `pauseObserved`／`resumeObserved`）。UI状態と未解決要求状態を別変数にし、タイムアウトで UI を IDLE に戻しても未解決要求の管理情報は保持する。解除は `uiState` に依存しない60秒の専用タイマで行う。REQUESTING タイムアウト後の遅延ワーカー完了も処理する（`false`／例外なら即時解除、`true` なら `CONFIRMATION_UNRESOLVED` へ遷移して60秒タイマを開始）。世代番号の役割は古いタイマの無効化と破棄された Activity への直接 UI 更新の抑止に限定し、ワーカー結果の破棄には使わない。`COMPLETED` は廃止し `IDLE` に統合する。
- P1-02：GPT 推奨 A（安全性優先）を採用する。`onPause → onResume` を解除条件から完全に外し、未解決要求の解除はタイムアウトのみで行う。ライフサイクルは復帰の参考情報として記録するのみとする。代償は通常経路で最大30秒＋60秒＝90秒のクールダウンだが、REQUESTING タイムアウト後にワーカーが動作し続ける場合は 30秒＋ワーカー遅延＋60秒 となり90秒を超え得る。作成頻度が極めて低い操作のため許容する。
- P2-01：§6 の v5 残存記述（「次の onResume か、さらに 60 秒後」）を修正し、§3.1 手順 7・8 と一致させる。
- P2-02：プロセス再生成後の要求管理は未対応とし、AX-49 を削除する（AX-57 に統合）。プロセス再生成後は `IDLE`／`NONE` で初期化されるが、過去の未解決要求を識別できない既知の制限であることを明記する。同一ショートカット ID によるランチャー側の重複排除は保証されない。
- P2-03：状態変更のメインスレッド直列化を規定する（ワーカー結果は Handler 経由で post、`workerRunning` は volatile／AtomicBoolean）。Executor の拒否・開始直後の例外時も `workerRunning` と要求状態を必ず整合させる。専用 SharedPreferences の実ファイル名を `a11y_off_token_prefs.xml` と定め、バックアップ除外 XML のパスと一致させる（AX-58）。
- テスト追加：AX-51〜AX-58。
- 出典：Argon high の解法提案 `antigravity_projects/a11y-off-deadlock/SOLUTION-20261108.md`（2026-11-08、261行）。GPT レビュー6回（CONS-084〜089）の膠着に対するエスカレーション。

## v7 → v8 の変更点（CONS-090 対応、2026-11-08）

- P1-01：§6 と §3.1 手順 9 に残っていた旧世代管理の記述（「古いワーカーの結果は現在の要求 ID と照合され〜」）を修正する。遅延したワーカーの結果は破棄せず、`PinRequestManager` 側で結果に応じた状態遷移を行う旨に統一する。世代番号の役割は古いタイマの無効化と破棄された古い Activity への直接 UI 更新の抑止に限定する。
- P2-01：ボタンの有効・無効の説明を修正する。30秒タイムアウトで `uiState` が `IDLE` に戻ったあとはボタンは再び有効になるが、`unresolvedState != NONE` または `workerRunning == true` の間は押下ハンドラが新しい要求を拒否する。「ボタンが無効」と「ボタンは有効だが要求を拒否」を区別する。
- P2-02：「最大90秒」を通常経路に限定する。REQUESTING タイムアウト後にワーカーが動作し続ける場合は 30秒＋ワーカー遅延＋60秒 となり90秒を超え得ることを明記する。
- セキュリティレビュー対応（CONS-090）：ワーカーの例外処理を `catch (Exception)` にし、VM の致命的な `Error` は通常の API 例外として扱わず、finally で状態を戻したうえで再スローする。トークン比較時は null・空文字を一致とみなさず、トークンを例外メッセージやデバッグログに出力しない。
- テスト追加：AX-59〜AX-66。

## v8 → v9 の変更点（CONS-091 対応、2026-11-08）

- P2-01：致命的 Error 発生時の状態復旧とスレッド直列化の明確化。ワーカー側の finally は復旧イベントをメインスレッドへ通知し、`PinRequestManager` がメインスレッドで状態を戻す。ワーカー側の finally から直接 `workerRunning` を変更しない。Error は捕捉せず再スローする。プロセス終了時にはメモリ上の状態復旧を保証しないことを明文化する。
- テスト追加：AX-67〜AX-70。AX-55 と AX-64 を Executor 拒否と例外発生の独立した試験に分離する。

## v9 → v10 の変更点（CONS-092 対応、2026-11-08）

- P1-01：Error 専用の復旧イベントを定義する。ワーカー側の finally が Error 専用の復旧イベントをメインスレッドへ通知し、`PinRequestManager` がメインスレッドで `workerRunning = false` にし、`uiState` が `REQUESTING` なら `IDLE` に戻す。API 受理状況が不明なため `unresolvedState` を無条件で `NONE` にせず、`CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマを開始する。REQUESTING タイムアウトが既に発生している場合も同じ未解決状態に収束させる。復旧イベントは世代番号で識別し、`CONFIRMATION_UNRESOLVED` への移行は一度だけ行い、古いタイマによる二重遷移を防ぐ。
- テスト追加：AX-71〜AX-75。AX-68 を Error 復旧の全体（`uiState`・`unresolvedState` を含む）に拡張する。

## 2. 文言と識別子

- ショートカット ID：`a11y_off`（固定。作り直しても同じ ID）
- 短いラベル：`補助機能オフ`
- 長いラベル：`Muse Bridge の補助機能をオフにする`
- アイコン：`R.drawable.ic_tile`（流用）
- Intent アクション：`dev.muse.bridge.ACTION_DISABLE_A11Y`（明示 intent で MainActivity へ）
- Intent extra（トークン）：`dev.muse.bridge.EXTRA_DISABLE_TOKEN`（D7 の保存値と一致するときだけ実行する）
- Intent flags：`FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP | FLAG_ACTIVITY_SINGLE_TOP`（D8）
- 作成ボタン（MainActivity）：`補助機能オフのショートカットを作る`
- トースト（作成失敗）：`ショートカットを作れませんでした`
- トースト（オフ完了）：`補助機能をオフにしました`（無効化の完了確認後に表示する。`disableSelf()` 直後には出さない）
- トースト（すでにオフ）：`補助機能はすでにオフです`
- トースト（未接続）：`サービスに接続されていません。状態を確認してください`
- トースト（無効化の状態確認不能）：`無効化の状態を確認できませんでした`
- トースト（未解決要求あり）：`前の作成要求が未解決です。しばらく待ってからもう一度押してください`
- トースト（状態確認不能）：`状態を確認できませんでした`

## 3. 設計

### 3.1 ショートカットの作成

1. MainActivity のボタン「補助機能オフのショートカットを作る」を押す。要求の状態は `PinRequestManager`（Application スコープの static シングルトン）で保持する。`PinRequestManager` のすべての状態の読み書きと遷移処理はメインスレッドに直列化する。ボタンの有効・無効は `uiState == IDLE` を映す（IDLE のときだけ有効）。押下ハンドラでもマネージャの状態を再確認する（再生成直後の Activity が一時的に有効なボタンを表示している場合に備える）。押下時の分岐は次の通り。`uiState != IDLE` の場合は何もしない。`uiState == IDLE` だが `workerRunning == true` または `unresolvedState != NONE` の場合は、新しい要求を開始せず、トースト「前の作成要求が未解決です。しばらく待ってからもう一度押してください」を出して終わる。`uiState == IDLE && !workerRunning && unresolvedState == NONE` のときだけ新しい要求を開始する。まず `isRequestPinShortcutSupported()` で確認し、非対応の場合はトースト「ショートカットを作れませんでした」を出して終わる（要求は送らない。状態は `IDLE / false / NONE` のまま）。
2. まず D7 のトークンを用意する（保存済みのものがあれば再利用し、なければ UUID を生成して専用 SharedPreferences ファイル `a11y_off_token_prefs` に `commit()` で保存する。実ファイルは `a11y_off_token_prefs.xml` であり、`fullBackupContent` および `dataExtractionRules`（`cloud-backup`／`device-transfer`）の `<exclude domain="sharedpref" path="..."/>` の指定パスと完全に一致させる）。保存に失敗した場合はトースト「ショートカットを作れませんでした」を出し、状態は `uiState = IDLE`、`workerRunning = false`、`unresolvedState = NONE` のまま終了する。作成要求は送らない。
3. `ShortcutManager.requestPinShortcut()` を**ワーカースレッド**で呼び、`ShortcutInfo`（ID `a11y_off`、短いラベル・長いラベル・アイコン・MainActivity への明示 intent＋D7 のトークン extra＋D8 の flags）を渡す。コールバックは null（新規コンポーネントを作らないため、承認・拒否の結果は受け取らない。IntentSender コールバックは承認時には通知されるが拒否時には通知されないため、両方を処理できない）。ワーカー起動時にメインスレッドで `requestGeneration` を 1 増やし、`uiState = REQUESTING`、`workerRunning = true`、`unresolvedState = NONE`、`pauseObserved = false`、`resumeObserved = false` に設定し、30秒の `REQUESTING` タイムアウトタイマを開始する。ワーカーは `try / catch (Exception)` で `requestPinShortcut()` を呼び、結果（戻り値・例外）をメインスレッドの `Handler` 経由で `PinRequestManager` に返す。VM の致命的な `Error`（`OutOfMemoryError` 等）は捕捉せず再スローする。ワーカー側の finally は **Error 専用の復旧イベント**をメインスレッドの `Handler` へ通知する（通常の結果イベントとは別のイベントとし、世代番号で識別する）。`PinRequestManager` はメインスレッドで復旧イベントを処理する：`workerRunning = false` にし、`uiState` が `REQUESTING` なら `IDLE` に戻す。API が要求を受理したか不明なため、`unresolvedState` を無条件で `NONE` にせず、`CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマを開始する（ボタンは再び有効になるが、60秒間は押下時に未解決トーストを出して新しい要求を制限する）。30秒の REQUESTING タイマがまだ動作している場合は解除する。復旧イベントは現在の要求世代と一致するときだけ処理し、`CONFIRMATION_UNRESOLVED` への移行は一度だけ行う（60秒の解除タイマが既に動作している場合は新たに開始しない。古いタイマによる二重遷移を防ぐ）。ワーカー側の finally から直接 `workerRunning` を変更しない（メインスレッド直列化を守るため）。プロセス終了時にはメモリ上の状態復旧を保証しない（プロセス再生成後は `IDLE`／`NONE` で初期化される）。`workerRunning` は `volatile` または `AtomicBoolean` とする。**異常終了経路**：`Executor` がワーカーの実行を拒否した場合（`RejectedExecutionException` 等）や開始直後に例外が発生した場合は、即座に `workerRunning = false`、`uiState = IDLE`、`unresolvedState = NONE` に戻し、タイマを解除してトースト「ショートカットを作れませんでした」を出す。**ワーカー結果の扱い**：古い Activity インスタンスへの直接 UI 更新は行わないが、`PinRequestManager` 側では `REQUESTING` タイムアウト後であってもワーカーの結果（`true`／`false`／例外）を破棄せずに処理し、未解決要求状態を遷移させる（手順 8 の2）。世代番号の役割は、古いタイマコールバックの無効化と、破棄された古い Activity への直接 UI 更新の抑止に限定する。フォアグラウンド Activity が存在する状態での呼び出しであり、ワーカースレッド経由でもその要件は満たされる。
4. 戻り値の意味：false は「ショートカット作成要求を利用できなかった」（ランチャー非対応のほか、制限されたユーザープロファイルや App Lock なども含む）、true は「API が true を返した（ランチャーがこの機能をサポートしている）。ショートカットの設置成功は未確定」。例外は「API 呼び出しが正常終了しなかった」。**true は設置成功を保証しない**（ユーザがダイアログで拒否しても、コールバックなしでは分からない）。true のときは成功トーストを出さない（システムのダイアログとホーム画面の表示がフィードバックになる）。
5. **30秒以内**にワーカーが `false` を返したとき、または例外が発生したときは、`REQUESTING` タイマを解除し、`workerRunning = false`、`uiState = IDLE`、`unresolvedState = NONE` に戻してボタンを再有効化し、トースト「ショートカットを作れませんでした」を出す。
6. **30秒以内**にワーカーが `true` を返したときは、`REQUESTING` タイマを解除し、`workerRunning = false`、`uiState = WAITING_CONFIRMATION`、`unresolvedState = CONFIRMATION_UNRESOLVED` にし、`pauseObserved = false`、`resumeObserved = false` にリセットする。ボタンは無効のままにし、30秒の `WAITING_CONFIRMATION` タイマを開始する。
7. 確認待ち状態（`uiState == WAITING_CONFIRMATION` または `unresolvedState == CONFIRMATION_UNRESOLVED`）に入った後の `onPause` で `pauseObserved = true` を記録し、その後の `onResume` で `resumeObserved = true` を記録する。`REQUESTING` 中（API 呼び出し完了前）の `onPause`／`onResume` は記録しない。Android の Activity ライフサイクルでは、別アプリへの切り替え・通知・画面ロック等でも `onPause → onResume` が発生し、ピン留め確認画面終了の証明にはならないため、`pauseObserved`／`resumeObserved` は復帰の参考情報として記録するのみとし、`WAITING_CONFIRMATION` や未解決要求の解除条件には使用しない。`onResume` では、現在の Activity が `PinRequestManager` の `uiState` を読み直してボタンの有効・無効（`uiState == IDLE`）を反映する。
8. タイムアウトによる UI 復旧と未解決要求の解除は、次の2つの経路で行う。**（1）`WAITING_CONFIRMATION` のタイムアウト（30秒）と未解決要求の解除（+60秒）**：`WAITING_CONFIRMATION` が30秒続いた場合、UI を復旧させる（`uiState` を `IDLE` に戻してボタンを再有効化する）。ただし `unresolvedState = CONFIRMATION_UNRESOLVED` はそのまま保持し、未解決の間は新しい要求の発行を制限する（ボタンを押したときはトースト「前の作成要求が未解決です。しばらく待ってからもう一度押してください」を出す）。`CONFIRMATION_UNRESOLVED` は、UI が `IDLE` に復旧してから**さらに60秒後**のタイムアウトでのみ `NONE` に解除する（`onPause → onResume` では解除しない）。**（2）`REQUESTING` のタイムアウト（30秒）とワーカー遅延終了時の状態遷移**：`REQUESTING` が30秒続いた場合、UI を復旧させる（`uiState` を `IDLE` に戻してボタンを再有効化する）が、`workerRunning == true` のままであり、`unresolvedState` を `REQUESTING_UNRESOLVED` に設定して新しい要求の発行を制限する。その後、遅れてワーカーが終了したとき（`workerRunning = false`）：ワーカーが **`false` または例外**で終了した場合は、ランチャーに要求が渡っていないため、**`unresolvedState` を直ちに `NONE` に解除**し、トースト「ショートカットを作れませんでした」を出す。ワーカーが **`true`** を返した場合は、遅れてランチャーに要求が受理されたため、結果を捨てずに **`unresolvedState` を `REQUESTING_UNRESOLVED` から `CONFIRMATION_UNRESOLVED` に遷移**させ、`pauseObserved = false`、`resumeObserved = false` にリセットし、その時点から **60秒後**のタイムアウトで `unresolvedState = NONE` に解除する。
9. Activity 再生成時（画面回転等）：新しい Activity はマネージャの状態を引き継ぐ。要求が進行中の場合は状態を IDLE に戻さず、ボタンは `uiState == IDLE` のときだけ有効になる（`REQUESTING`／`WAITING_CONFIRMATION` の間は無効）。30秒タイムアウトで `uiState` が `IDLE` に戻ったあとはボタンは再び有効になるが、`unresolvedState != NONE` または `workerRunning == true` の間は押下ハンドラが新しい要求を拒否し、未解決トーストを出す（「ボタンが無効」と「ボタンは有効だが要求を拒否」は別の状態である）。表示中のダイアログがあればそれはシステムのものであり、アプリ側はブロックしない。
10. プロセス再生成時：Activity 再生成とプロセス再生成は別の障害ケースである。プロセスが終了した場合、メモリ上の `PinRequestManager` の状態は失われ、プロセス再生成後は `uiState = IDLE`、`workerRunning = false`、`unresolvedState = NONE` で初期化される。永続化を持たない v7 では、プロセス再生成後に過去の未解決要求を識別（不確定状態として検出）することはできず、プロセス再生成直後に再要求が行われた場合はランチャーに重複要求が送られる既知の制限がある（Android の仕様上、同一パッケージからの複数保留要求の扱いはランチャー実装に委ねられており、ランチャー側で重複排除される保証はない）。プロセスをまたぐ永続的な要求管理の要否は実機検証の結果で判断し、v7 では未対応ケースとして明記する。

### 3.2 ショートカットの実行

1. ホーム画面のショートカットをタップすると、MainActivity が `ACTION_DISABLE_A11Y` の intent で起動する（すでに起動中の場合は `onNewIntent` に届く）。
2. MainActivity は `onCreate` と `onNewIntent` の両方でこのアクションを処理する。`onNewIntent` では `setIntent(intent)` を呼んで intent を更新する。MainActivity に launchMode の指定はなく（標準）のため、flags（D8）なしでは毎回新しいインスタンスが作られる。flags 付きでは、既存インスタンスがある場合は `onNewIntent` に一度だけ届き、ない場合は `onCreate` で処理される。処理自体が冪等であること（無効化後の二度目の呼び出しは「すでにオフです」になる）が二重実行の防波堤になる。
3. 処理 `handleDisableA11y()`：
   - まず D7 のトークンを確認する。intent の extra が保存値と一致しないときは無言で無視する（トーストも出さない、無効化もしない）。
   - 次に `disableInProgress` フラグを確認する。すでに無効化の実行中の場合は無言で無視する（重複実行とトーストの重複を防ぐ）。実行中でない場合はフラグを立て、操作世代番号を 1 増やして今回の処理に割り当てる。
   - `BridgeAccessibilityService.getInstance()` を取る。
   - 非 null のとき：`disableSelf()` を呼ぶ。この時点では成功トーストを出さない。成功の通知は、サービスの終了が確認できてから行う（下記の完了確認）。
   - null のときは、Android 側の状態を 3 状態で確認する（`MainActivity.getAccessibilityState()`、既存の `ENABLED_ACCESSIBILITY_SERVICES` 読み取りを 3 状態化したもの。`getString()` が null を返す場合は DISABLED、例外時は UNKNOWN とする）。
     - DISABLED（設定に本サービスがない）と確認できたとき：トースト「補助機能はすでにオフです」を出し、終了する。
     - ENABLED（設定に本サービスがある）と確認できたとき（サービスは有効だがインスタンスが接続されていない＝プロセス再生成直後・接続中・再接続中）：「すでにオフです」とは表示せず、トースト「サービスに接続されていません。状態を確認してください」を出す。無効化はしない。
     - UNKNOWN（設定値の読み取り自体に失敗したとき）：トースト「状態を確認できませんでした」を出す。無効化はしない。
   - 完了確認：`disableSelf()` を呼んだあと、サービスの終了を示す `ACTION_A11Y_STATE_CHANGED` ブロードキャストを受け取ったときに Android 側の状態を `getAccessibilityState()` で再確認する。DISABLED ならフラグを下ろし、トースト「補助機能をオフにしました」を出す。ENABLED ならフラグを維持する（ブロードキャストが別の事象によるものである可能性があるため）。操作が進行中でないとき（フラグが下りているとき）に届いたブロードキャストは無視する。
   - `disableInProgress` フラグは static（プロセス内共有）とし、複数の Activity インスタンス間でも排他制御が働く。フラグの読み書きと操作世代番号の更新はメインスレッドに直列化する（ブロードキャスト受信もタイマもメインスレッドで処理する）。2 秒のタイムアウト時は単純に下ろさず、Android 側の状態を再確認する。DISABLED ならフラグを下ろし、トースト「補助機能をオフにしました」を出す。ENABLED ならフラグを維持し、さらに 2 秒後に再確認する（最大 3 回まで。再確認でも ENABLED のままならフラグを下ろし、トースト「無効化の状態を確認できませんでした」を出す。次の呼び出しは最初から判定し直す）。UNKNOWN ならフラグを下ろし、トースト「無効化の状態を確認できませんでした」を出す。6 秒以内に完了確認ができない場合は、成功と断定しない。処理の冪等性は維持する（無効化後の二度目の呼び出しは「すでにオフです」になる）。
4. `disableSelf()` 後、サービスは `onDestroy` で `instance = null` にし、既存の `ACTION_A11Y_STATE_CHANGED` ブロードキャストを送る。MainActivity の状態表示は既存のブロードキャスト受信／`onResume` で更新される（新しい更新経路は作らない）。
5. この処理は画面操作ではないため、操作中通知（「操作中」／Override）は出さない。

### 3.3 状態と範囲

- ショートカット実行後、サーバは動き続ける。`/health` は ok を返すが、`/state` 等の画面操作は `ACCESSIBILITY_DISABLED`（503）になる。ダッシュボードの 5 分更新も失敗する（アクセシビリティ無効のため）。これは仕様通りの状態であり、不具合ではない。ユーザが補助機能をオンに戻せば復旧する。
- `disableSelf()` はユーザ確認なしでサービスを無効化する（API の仕様）。誤タップ防止のため、ショートカットのラベルは「オフにする」ことが分かる文言にする。実行時の追加の確認ダイアログは出さない（ショートカット自体が明示的な操作であるため）。
- 他のアプリが同じアクションの intent を送る可能性について：MainActivity は `android:exported="true"`（ランチャー Activity、マニフェストで確認済み）のため、他アプリも明示 intent を送れる。ただし D7 のトークンがなければ無視される。トークンは ShortcutManager 経由でしか他アプリから読めない（他アプリは自分のショートカットしか取得できない）ため、第三者が推測することは実質的にできない。ただしトークンは呼び出し元アプリの身元を確認するものではなく、秘密情報を持つ intent だけを受け付ける Bearer Token 方式である。トークンを取得した第三者による再送は防げない。トークンは認証情報として扱い、ログに出力しない。残るリスク（ランチャー自体の侵害等）は本仕様の範囲外とする。
- 操作中の無効化について：画面操作の最中に `disableSelf()` が呼ばれても、サーバはクラッシュしない。進行中の操作は既存のアクセシビリティ無効エラー経路で失敗し（次の要求は 503 になる）、操作中通知は HttpBridgeServer 側の自動終了タイマで「操作終了」になるため残留しない。無効化の処理自体はサーバや操作状態に触れない。
- 作成要求の管理について：要求の状態は `PinRequestManager`（Application スコープの static シングルトン）で保持し、Activity の寿命から分離する。4つの独立した状態（`uiState`、`workerRunning`、`unresolvedState`、`pauseObserved`／`resumeObserved`）に分離し、すべての状態の読み書きと遷移処理はメインスレッドに直列化する。Activity 再生成時は既存要求の状態を引き継ぐ。実行中の要求がある場合は新しい要求を開始しない。遅延ワーカー完了時は古い Activity への直接 UI 更新は行わないが、`PinRequestManager` 側では結果に応じた状態遷移を行う。
- 未解決要求の解除について：`onPause → onResume` は別アプリ切り替え等でも発生し確認画面終了を証明できないため、復帰の参考情報として記録するのみとし、`CONFIRMATION_UNRESOLVED` の解除は60秒のタイムアウトで行う。`REQUESTING_UNRESOLVED` はワーカーが `false`／例外で終了したときに即時解除し、`true` で終了したときは `CONFIRMATION_UNRESOLVED` に移行して60秒のタイムアウトで解除する。
- ワーカー終了と要求終了の分離について：`workerRunning` フラグでワーカーの実行中を別に管理する。REQUESTING のタイムアウト後も、ワーカー終了が確認できるまで新しい要求は許可しない。世代番号による結果の無視と、API 呼び出しの排他制御は別の仕組みである。`Executor` の拒否・開始直後の例外時も `workerRunning` と要求状態を必ず整合させる。Error 専用の復旧イベントでは、`workerRunning` を `false` に戻すとともに、`uiState` が `REQUESTING` なら `IDLE` に戻し、`unresolvedState` を `CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマを開始する（API 受理状況が不明なため `NONE` にはしない）。復旧イベントは世代番号で識別し、現在の要求世代と一致するときだけ処理する。`CONFIRMATION_UNRESOLVED` への移行は一度だけ行い、60秒の解除タイマが既に動作している場合は新たに開始しない。
- プロセス再生成について：PinRequestManager はプロセス内の状態管理であり、プロセス終了で状態は失われる。プロセス再生成後は `IDLE`／`NONE` で初期化されるが、過去の未解決要求を識別できないため、重複要求が送られる可能性がある既知の制限がある。v7 では未対応ケースとして明記する。
- 成功通知のタイミングについて：`disableSelf()` 直後には成功を通知しない。ブロードキャスト受信後に DISABLED を確認してから「補助機能をオフにしました」を出す。6 秒以内に確認できない場合は成功と断定せず、「無効化の状態を確認できませんでした」を出す。
- デフォルトランチャーの信頼について：ショートカットの作成要求では、デフォルトランチャーがトークンを含む intent を取り扱う。デフォルトランチャーは信頼境界の内側の主体である。「一般の他アプリから SharedPreferences を直接読めない」ことと「トークンが端末内の他のソフトウェアに一切露出しない」ことは同じではない。信頼できないランチャーの利用は保証対象外とする。バックアップ・端末移行でトークンが別端末に移る場合の扱いは別途確認する。トークン漏洩後の失効・再発行機構は現仕様には存在しない。
- 状態判定の 3 状態について：`getAccessibilityState()` は ENABLED／DISABLED／UNKNOWN を返す。`getString()` が null を返す場合は DISABLED（有効なサービスが一つもない）とする。例外時は UNKNOWN とする。既存の `isAccessibilityEnabled()`（boolean）は状態表示用として残し、無効化の判定には使わない。

## 4. テスト・検証計画（Pythonソース構造検査／コードレビュー／実機）

- AX-01：ショートカット ID `a11y_off`、アクション `dev.muse.bridge.ACTION_DISABLE_A11Y`、MainActivity への明示 intent、D7 のトークン extra が実装されている。トークン不一致の intent は無視される。
- AX-02：作成に `requestPinShortcut()` を使う。ワーカースレッドで呼び出し、結果を UI スレッドに返す。戻り値の意味（false＝要求を利用できなかった、true＝API が true を返した。設置成功は未確定）が区別され、例外時は try/catch でトーストを出す。true のときに成功トーストは出さない。`isRequestPinShortcutSupported()` による事前確認がある。
- AX-03：MainActivity が `onCreate` と `onNewIntent` の両方でアクションを処理する。ショートカット intent の flags（D8）と `onNewIntent` での `setIntent(intent)` が実装されている。処理は冪等であり、`disableInProgress` フラグで同一 Activity 内の重複実行を抑える。
- AX-04：`getInstance()` が null のときに `getAccessibilityState()` で Android 側の状態を 3 状態で確認する。DISABLED は「すでにオフです」、ENABLED は「サービスに接続されていません。状態を確認してください」、UNKNOWN は「状態を確認できませんでした」と表示し、いずれも無効化はしない。誤って「すでにオフです」と表示しない。
- AX-05：非 null のときに `disableSelf()` を呼び、完了確認（ブロードキャスト受信後の DISABLED 確認）後に「オフにしました」のトーストを出す。`disableSelf()` 直後には成功トーストを出さない。トークン不一致のときは `disableSelf()` を呼ばない。6 秒以内に確認できない場合は「無効化の状態を確認できませんでした」を出す。
- AX-06：オフの仕組みが `disableSelf()` だけであること（`settings put`・adb・デバイス管理アプリの呼び出しがない）。
- AX-07：ショートカットの実行経路にサーバ停止（`server.stop()` 相当）の呼び出しがない。
- AX-08：ラベル（短い・長い）とアイコン（`ic_tile` 流用）が定義されている。新しい drawable ファイルは不要。
- AX-09：`AndroidManifest.xml` に新規権限の追加がない。
- AX-10：`AndroidManifest.xml` に新しい exported コンポーネント（activity／receiver）の追加がない。MainActivity が `exported=true` であることが確認済みであり、D7 のトークンが外部起動の防波堤になる。
- AX-11：無効化後の状態表示が既存のブロードキャスト／`onResume` で更新される（新しい更新経路がない）。
- AX-12：バージョン試験（v2.9.3 / code 56）。
- AX-13：トークンの生成（一度だけ、UUID）・保存（専用の SharedPreferences ファイル、アプリ内、`commit()` で同期保存し成否を確認する）・比較が実装されている。保存値を再利用するため、作り直しでも古いショートカットが動く。トークンはログに出力されない。バックアップ除外ルール（fullBackupContent と dataExtractionRules の両方）で復元対象外になっている。実機での外部アプリ試験は、第二のアプリがないためソース検査＋コードレビューとし、その旨を記録する。
- AX-14：サービス接続状態の不一致試験（ソース）：Android 設定では有効だが `getInstance()` が null の場合に「すでにオフ」と誤表示しない（AX-04 の分岐で確認する）。
- AX-15：Activity ライフサイクル試験（ソース）：起動中・バックグラウンド・破棄済み・プロセス再生成直後のいずれでも、ショートカット intent が正しく処理される（flags＋両ハンドラ＋冪等性）。
- AX-16：ショートカット作成異常系（ソース）：ピン留め確認でキャンセル（コールバックなしのため結果は受け取らない）、同一 ID で再作成、ランチャー非対応（false）、制限されたユーザープロファイル・App Lock 有効時（false）、API 例外。いずれもクラッシュしない。
- AX-17：操作中の無効化（ソース）：無効化の処理がサーバ停止や操作状態に触れない。進行中の操作は既存のアクセシビリティ無効エラー経路で失敗する。
- AX-18：`requestPinShortcut()` が UI スレッドを長時間占有しない（ワーカースレッドで呼び出す）。作成要求の状態機械が `uiState`（IDLE／REQUESTING／WAITING_CONFIRMATION）、`workerRunning`、`unresolvedState`（NONE／REQUESTING_UNRESOLVED／CONFIRMATION_UNRESOLVED）、`pauseObserved`／`resumeObserved` の分離管理として `PinRequestManager`（Application スコープ）で実装されている。
- AX-19：Android 側の状態読み取り失敗（UNKNOWN）時に誤って無効化しない。
- AX-20：トークンの保存に失敗したときにショートカット作成要求を送らない。
- AX-21：トークンの 3 パターン（なし・不正・正規）を確認する。なし・不正は無言で拒否され、正規だけが無効化を実行する。
- AX-22：連続タップ時の処理重複・トースト重複を確認する（実機）。`disableInProgress` が static（プロセス内共有）であり、操作世代番号で完了イベントを照合する。
- AX-23：ピン留め確認ダイアログ終了後のボタン再有効化を確認する（実機）。REQUESTING 中の onResume では再有効化されない。WAITING_CONFIRMATION のタイムアウト（30 秒）で UI は復旧するが、未解決要求として記録され、新しい要求は制限される。未解決（`CONFIRMATION_UNRESOLVED`）の解除は、UI 復旧からさらに 60 秒後のタイムアウトで行われる（`onPause → onResume` は参考情報として記録されるが解除はしない）。
- AX-24：確認画面が Activity を一時停止させない場合でも、状態機械が正しく動作する。
- AX-25：確認画面表示中の割り込み・復帰で、状態が壊れない。
- AX-26：ワーカースレッドの処理が遅延した場合でも、古い Activity の UI 更新は行わないが、`PinRequestManager` はワーカーの結果に応じて `REQUESTING_UNRESOLVED` を即時解除（`false`／例外時）または `CONFIRMATION_UNRESOLVED` へ遷移（`true` 時）させ、新要求と競合しない。
- AX-27：確認画面のキャンセル後にボタンが復旧する。
- AX-28：画面回転・Activity 再生成時の状態復旧（既存要求の状態を引き継ぎ、要求が終わるまでボタンは無効のままになる）。プロセス再生成とは別のケースである。
- AX-29：サービス終了が 2 秒以上遅延するケースで、フラグが維持され、再確認で DISABLED を検出してから下ろされる。
- AX-30：タイムアウト直後の連続タップで、重複実行されない。
- AX-31：複数 Activity インスタンスからの重複 intent で、static フラグにより重複実行されない。
- AX-32：REQUESTING 中の画面回転後、旧ワーカーが動作している状態で再要求しても二重要求にならない。ワーカー終了が確認できるまで新しい要求は許可されない。
- AX-33：Activity 再生成後に旧ワーカーの結果が返っても、新 Activity の状態を変更しない。
- AX-34：旧要求のタイムアウト後、遅延した API 呼び出しが完了しても新要求と競合しない。ワーカーが `true` を返した場合は `CONFIRMATION_UNRESOLVED` に遷移し、60秒のタイマが開始される。
- AX-35：要求 A の遅延ブロードキャストが要求 B の実行中に到着しても、要求 B の排他状態を解除しない。
- AX-36：古い完了イベントと新しい無効化要求が交錯しても、重複実行されない。
- AX-37：ブロードキャスト受信時に ENABLED が返った場合、排他フラグを維持する。
- AX-38：確認画面を 30 秒以上表示した状態での再要求が制限される。
- AX-39：無効化要求後、サービス終了が遅延した場合のトースト（成功は完了確認後に表示される）。
- AX-40：バックアップ・端末移行後のトークン整合性（トークンは復元対象外で、古いショートカットは動かない）。dataExtractionRules の device-transfer 除外を含む。
- AX-41：Android 11 相当のバックアップ・復元でトークンが復元されない。
- AX-42：Android 12 以降のクラウドバックアップ・復元でトークンが復元されない。
- AX-43：Android 12 以降の D2D 端末移行でトークンが復元されない（独立した試験）。
- AX-44：復元後の古いショートカットによる無効化が拒否される。
- AX-45：復元後に新しいトークンを生成してショートカットを再作成できる。
- AX-46：WAITING_CONFIRMATION 中および `CONFIRMATION_UNRESOLVED` 中の `onPause → onResume` で要求・未解決要求が解除されない。
- AX-47：REQUESTING タイムアウト後、旧ワーカー終了前に新要求が開始されない。
- AX-48：旧ワーカーの終了と新 Activity の状態更新が交錯しても排他状態が維持される。
- AX-50：D2D 転送でトークンが復元されない。
- AX-51：`WAITING_CONFIRMATION` が30秒タイムアウトして `uiState` が `IDLE` になった後、`CONFIRMATION_UNRESOLVED` が保持され、その後の `onPause → onResume` は参考記録のみ行い、60秒経過で解除される。
- AX-52：無関係なアプリへの切り替えや通知等による `onPause → onResume` で未解決要求が解除されない。
- AX-53：`REQUESTING` タイムアウト（30秒）後、ワーカーが `true` を返した場合、結果を捨てずに `unresolvedState` が `REQUESTING_UNRESOLVED` から `CONFIRMATION_UNRESOLVED` に遷移し、60秒の確認未解決タイマが開始される。
- AX-54：`REQUESTING` タイムアウト（30秒）後、ワーカーが `false` または例外で終了した場合、`workerRunning` が `false` になるとともに `REQUESTING_UNRESOLVED` が即座に `NONE` に解除される。
- AX-55：Executor がワーカーの実行を拒否した場合に、`workerRunning = false`、`uiState = IDLE`、`unresolvedState = NONE` に復旧する。
- AX-56：`workerRunning` および `PinRequestManager` の状態変更がメインスレッド直列化（および適切な同期）によりスレッド間で安全に可視・整合する。
- AX-57：プロセス再生成後の未対応状態（永続化なしによる IDLE 初期化）とランチャーへの重複要求リスクが仕様上明記されている。
- AX-58：専用 SharedPreferences の実ファイル名とバックアップ除外 XML（`fullBackupContent` および `dataExtractionRules` の `cloud-backup`／`device-transfer`）の `sharedpref` 除外パスが一致している。
- AX-59：遅延 true＋Activity 再生成（REQUESTING タイムアウト後にワーカーが true を返し、その間に Activity が再生成された場合でも、`PinRequestManager` は `CONFIRMATION_UNRESOLVED` に遷移し、新しい Activity のボタン状態が `uiState` と一致する）。
- AX-60：遅延 false＋Activity 再生成（REQUESTING タイムアウト後にワーカーが false／例外で終了し、その間に Activity が再生成された場合でも、`REQUESTING_UNRESOLVED` が即時解除され、新しい Activity で再作成可能になる）。
- AX-61：タイムアウトとワーカー完了の競合（30秒タイマ発火とワーカー完了が同時に起きた場合でも、状態遷移が単一メインスレッド上で原子的に処理され、`uiState`／`workerRunning`／`unresolvedState` が整合する）。
- AX-62：古いタイマの遅延実行（世代が変わった古いタイマコールバックが遅れて実行されても、無効化されて状態を変更しない）。
- AX-63：ワーカーが90秒以上終了しない（REQUESTING タイムアウト後、ワーカーが長時間終了しない場合でも、`unresolvedState` が `REQUESTING_UNRESOLVED` のまま新しい要求を制限し続け、ワーカー終了時に正しく遷移する）。
- AX-64：ワーカー開始直後に例外が発生した場合に、`workerRunning = false`、`uiState = IDLE`、`unresolvedState = NONE` に復旧する。
- AX-65：バックアップ除外のビルド成果物検査（ビルド済み APK／マニフェストで、`fullBackupContent` と `dataExtractionRules` の `sharedpref` 除外パスが実ファイル名 `a11y_off_token_prefs.xml` と一致している）。
- AX-66：不正 intent の実機検証（トークンなし・不一致・null・空文字の intent では無効化が実行されず、無言で無視される）。
- AX-67：ワーカー内で VM の致命的な Error が発生した場合、finally が実行されて復旧イベントがメインスレッドへ通知され、Error が再スローされる。
- AX-68：Error 時の復旧イベントがメインスレッドで処理され、`workerRunning` が `false` に戻るとともに、`uiState` が `REQUESTING` なら `IDLE` に戻り、`unresolvedState` が `CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマが開始される。プロセス終了時にはメモリ上の状態復旧を保証しないことが仕様上明記されている。
- AX-69：ワーカー完了と REQUESTING タイムアウトの競合時（両方が同時に起きた場合）に、二重の状態遷移や二重のトーストが発生しない。
- AX-70：トークン保存値・受信値の全組み合わせの境界値試験（保存値 null／空文字／UUID × 受信値 null／空文字／不一致／一致）。保存値または受信値が null・空文字の場合は一致とみなさず、無効化を実行しない。
- AX-71：Error 発生後、`workerRunning` だけでなく `uiState` も復旧する（`REQUESTING` なら `IDLE` に戻る）。
- AX-72：Error 発生後、未解決要求が適切なタイムアウトで解除される（`CONFIRMATION_UNRESOLVED` に移行し、60秒後に `NONE` になる）。
- AX-73：REQUESTING タイムアウト前後で Error が発生しても最終状態が整合する（30秒タイムアウトの前後で分けて検証する。特に重要。両方の場合で最終的に `CONFIRMATION_UNRESOLVED` に収束し、60秒後に解除される）。
- AX-74：Error 復旧イベントと古いタイムアウトイベントが競合しても二重遷移しない（世代番号で識別し、移行は一度だけ行う）。
- AX-75：Error 復旧後、未解決期間終了後に新しいショートカット作成要求が成功する。

### 責任分担

- Python テストはソース構造のみを証明する（ショートカットの定義、アクションの処理、null 分岐、`disableSelf()` の呼び出し、サーバ停止がないこと、マニフェストの不変性）。
- Java の実行時動作（ピン留めダイアログの表示、ショートカットからの起動、実際の無効化、トースト表示）は、Python テストでは証明できない。コードレビュー（実装時の目視確認）と実機確認（§5）で補う。Java 側にユニットテスト環境がないことは、本仕様の不採用理由ではない。

## 5. ライブ確認（実機、v2.9.3）

- AX-L1：アプリ内のボタン → システムのピン留め確認ダイアログ → 確認 → ホーム画面にショートカットが出る。ダイアログでキャンセルした場合はショートカットができず、クラッシュしない。30秒のタイムアウト後にボタンが再び有効になり、さらに60秒の未解決要求制限期間中は押下時に未解決トーストが出て、経過後に再作成可能になる。
- AX-L2：ショートカットをタップ → 補助機能がオフになる（設定のトグルがオフ、`/health` の accessibility が false）。
- AX-L3：オフの状態でもう一度ショートカットをタップ → 「すでにオフです」のトーストが出て、クラッシュしない。サービス有効・インスタンス null の場合は「サービスに接続されていません」と表示される（再現が難しい場合はコードレビューで確認する）。
- AX-L4：ショートカット実行後、サーバは動き続ける（`/health` は ok）が、`/state` は 503 になる（範囲の確認）。
- AX-L5：設定で補助機能をオンに戻す → 画面操作が再び動く（`/health` の accessibility が true）。
- AX-L6：操作中の無効化 — Bridge から画面操作を開始し、操作中にショートカットを実行する。操作が適切に終了または失敗し、サーバは動き続け、操作中通知は残留せず、次の要求は 503 になる。
- AX-L7：連続実行 — ショートカットを連続タップする。複数の Activity が生成されても異常な処理をせず、サーバは止まらず、最終的に無効になる。トーストが大量に表示されない。

## 6. Edge cases

- ショートカット作成時にランチャーがピン留めに対応していないとき：`requestPinShortcut()` が false を返し、トーストを出して終わる。クラッシュしない。
- サービスが接続されていない状態でショートカットをタップしたとき：`getInstance()` が null の場合は Android 側の状態を確認する。DISABLED なら「すでにオフです」、ENABLED なら「サービスに接続されていません。状態を確認してください」、UNKNOWN なら「状態を確認できませんでした」を出す。いずれも無効化はしない（§3.2 と同じ判定）。
- ショートカット実行中にサーバが停止したとき：サーバ停止とアクセシビリティ無効化は別の状態である。サーバが止まってもアクセシビリティの有効状態は変わらないため、「すでにオフです」とは表示しない。Android 側の状態を確認し、その結果に応じたトーストを出す。クラッシュしない。
- 状態確認の読み取りに失敗したとき（UNKNOWN）：「状態を確認できませんでした」を出し、無効化はしない。
- 端末を再起動したとき：ホーム画面のショートカットは残る（ランチャーの仕様）。補助機能のオン・オフ状態は再起動前の状態に従う（Android の仕様）。
- ショートカットをホーム画面から削除したとき：アプリ側の状態は変わらない。作り直すにはアプリ内のボタンをもう一度押す。
- トークン extra なしの intent が届いたとき：無言で無視する（トーストも出さない、無効化もしない）。
- 作成ボタンの連打：要求中はボタンを無効化するため、二重の要求は出ない。
- Activity 再生成中に作成要求が進行中のとき：新しい Activity は PinRequestManager の状態を引き継ぐ。ボタンは `uiState == IDLE` のときだけ有効になる（`REQUESTING`／`WAITING_CONFIRMATION` の間は無効）。30秒タイムアウトで `uiState` が `IDLE` に戻ったあとはボタンは再び有効になるが、`unresolvedState != NONE` または `workerRunning == true` の間は押下ハンドラが新しい要求を拒否する。遅延したワーカーの結果は破棄せず、`PinRequestManager` 側で結果に応じた状態遷移を行う（世代番号は古いタイマの無効化と破棄された古い Activity への直接 UI 更新の抑止にのみ使う）。
- 遅れたブロードキャストが届いたとき：操作が進行中でない場合は無視する。操作が進行中の場合は Android 側の状態を再確認し、DISABLED のときだけフラグを下ろす。ENABLED の場合は維持する。
- 30 秒のタイムアウトで UI が復旧したあと、確認画面がまだ表示されているとき：未解決要求として記録され、新しい要求は制限される（トーストを出す）。`onPause → onResume` は参考情報として記録するのみとし、さらに 60 秒後のタイムアウトで未解決が解除される。
- バックアップから復元したあと：トークンは復元対象外のため存在せず、古いショートカットは動かない。アプリ内のボタンから作り直す（新しいトークンが生成される）。
- 確認待ち中または未解決要求保持中に `onPause → onResume` が来たとき：別アプリへの切り替え等と区別できないため、復帰の参考情報として記録するのみとし、未解決要求は解除しない。解除はタイムアウト経路だけが行う。
- REQUESTING のタイムアウト後にワーカーがまだ動いているとき：未解決要求（`REQUESTING_UNRESOLVED`）として新しい要求は制限する。その後ワーカーが `false` または例外で終了したときは直ちに未解決を解除し、`true` を返したときは `CONFIRMATION_UNRESOLVED` に移行してさらに60秒間は新しい要求を制限する。なお、ワーカーの遅延が長い場合、REQUESTING タイムアウト（30秒）＋ワーカー遅延＋60秒の合計が90秒を超えることがある（通常経路の最大90秒は、ワーカーが30秒以内に完了する場合の上限である）。
- ワーカーで VM の致命的な Error が発生したとき：結果イベントは返らない。ワーカー側の finally が Error 専用の復旧イベントをメインスレッドへ通知し、`PinRequestManager` がメインスレッドで `workerRunning = false` にし、`uiState` が `REQUESTING` なら `IDLE` に戻し、`unresolvedState` を `CONFIRMATION_UNRESOLVED` に移行して60秒の解除タイマを開始する。REQUESTING タイムアウト（30秒）が既に発生している場合も同じ未解決状態に収束する。古いタイマによる二重遷移は世代番号で防ぐ。プロセス終了時にはメモリ上の状態復旧を保証しない。
- プロセスが終了したあと：PinRequestManager の状態は失われる。アプリ再起動後は IDLE で初期化されるが、以前の要求の成否は不確定である。重複した要求が送られる可能性がある（Android の公式仕様では同一パッケージからの複数保留要求の扱いはランチャーに委ねられており、以前の要求が無視される等の挙動はランチャー依存で保証されない）。v7 ではプロセスをまたぐ永続化は未対応ケースとして明記する。
- D2D 端末移行のあと：dataExtractionRules の device-transfer 除外により、トークンは新端末に復元されない。古いショートカットは動かない。アプリ内のボタンから作り直す。

## 7. 範囲外（やらないこと）

- Bridge オフと連動する自動オフ（D1 で見送り）。将来、設定でオン・オフを選べる形にする場合は別仕様とする。
- 補助機能をオンに戻すショートカット（Android の制限で不可能）。
- クイック設定タイルからのオフ操作（タイルは既存の開始・停止用。将来の拡張候補として残す）。
- 実行時の確認ダイアログ（ショートカット自体が明示的な操作のため出さない）。
- 透明アクティビティによる画面遷移なしの実行（v1 では MainActivity が一瞬開くことを許容する）。
