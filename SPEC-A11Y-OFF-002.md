# Muse Bridge — 補助機能オフのウィジェット統合とクイック設定タイル (SPEC-A11Y-OFF-002)

- 状態: v6 ADOPTED AND FROZEN（採用・凍結 2026-11-09 07:48 +07）。v1〜v5 は草案（未凍結）。v6 は GPT CONS-099 の 3回目再レビュー（CONDITIONAL、9.4/10、P1=1、P2=1）の新規指摘 2 件を反映したもの。GPT はこの 2 件を直せば ADOPTABLE に移行できるとした。ユーザーは v6 ルート（採用して凍結）を 2026-11-09 07:47 +07 に選択した。
- 採用の根拠（v6）: GPT CONS-099 のレビュー履歴。v1 は CONDITIONAL（P0=0、P1=5、P2=4、8.7/10）。再レビュー1回目（v2）は ADOPTABLE（P0=0、9.3/10）。Argon（high）レビュー（v2）は CONDITIONAL（P0=0、P1=2、P2=3、8.9/10）。再レビュー2回目（v4）は CONDITIONAL（P0=0、P1=1、P2=2、9.2/10）。再レビュー3回目（v5）は CONDITIONAL（P0=0、P1=1、P2=1、9.4/10）。v6 は再レビュー3回目の新規指摘 2 件（P1-01 段階 C のフォールバック完了機構、P2-01 §3 の要約の整合）を反映し、GPT の示した ADOPTABLE への条件を満たす。Go プランは Luna 固定のため、GPT-6 レビューは Luna（軽量モデル）になり、最終確認としての価値は低いと判断し、v6 採用を選択した。
- 対象バージョン: v2.9.7 / code 60（L8 の規則による。最新のリリースは v2.9.6 / code 59（SPEC-LOCALE-TILE-001 v4）のため。仕様書の初稿では v2.9.4 / code 57 としていたが、LOCALE-TILE v2〜v4 で v2.9.4〜v2.9.6 が使われたため繰り上がった）
- 言語: 日本語
- 改訂履歴: v2 (2026-11-09) — CONS-099 (CONDITIONAL, P1=5, P2=4) の指摘を反映。v3 (2026-11-09) — CONS-099 再レビュー (ADOPTABLE) の新規指摘 3 件（P1-01、P2-01、P2-02）を反映。v4 (2026-11-09) — Argon レビュー (CONDITIONAL, P1=2, P2=3) の指摘を反映。v5 (2026-11-09) — CONS-099 2回目再レビュー (CONDITIONAL, P1=1, P2=2) の新規指摘 3 件（P1-01、P2-01、P2-02）を反映。v6 (2026-11-09) — CONS-099 3回目再レビュー (CONDITIONAL, P1=1, P2=1) の新規指摘 2 件（P1-01、P2-01）を反映。v1〜v5 は git に残る。
- 前提: SPEC-A11Y-OFF-001 v13 (凍結) の無効化の仕組みを流用する。001 のホーム画面ショートカット方式は本仕様で廃止する。
- レビュー: GPT CONS-099 (新規チャット。SPEC ごとに新規チャット、の規則による)

## 1. 背景と目的

SPEC-A11Y-OFF-001 v13 は、ホーム画面ショートカットから補助機能をオフにする仕組みを実装した。実機確認 AX-L1〜L5 (2026-11-08) で、無効化そのものは動くことが確認された (ショートカット作成、タップでオフ、サーバー継続、/state 503、設定からの再オン)。

しかし、サイガマンの判断 (2026-11-08 12:23 +07) により、ショートカット (アイコン) 方式は不要となった。理由は2つある。

- ショートカットを押すと MainActivity が開くため、連続タップが機能しているかをあの形では確かめられない。
- アイコン自体が不要。ウィジェットとクイック設定タイルに統合する方がよい。

本仕様は、001 の無効化の仕組み (トークン不要の部分を除く) を流用し、操作の入口をウィジェットとクイック設定タイルに置き換える。どちらの入口も MainActivity を開かない (ヘッドレス実行)。001 のショートカット作成経路は削除する。

## 2. 決定事項

- **D1 — 入口は2つ。** ウィジェットのボタンと、2つ目のクイック設定タイル。どちらも MainActivity を開かずに補助機能をオフにする。アプリの画面遷移は発生しない。
- **D2 — 無効化の流れは共通の実行器に切り出す。** MainActivity にある無効化の流れ (`handleDisableA11y`、`scheduleDisableCheck`、`getAccessibilityState`、トースト、`ACTION_A11Y_STATE_CHANGED` の受信) を、新しい `A11yOffExecutor` に切り出す。ウィジェットの受信器とタイルのサービスがこれを呼ぶ。001 の意味は変えない (D7)。実行器がサービスインスタンスをどう得るか、ガードと完了をどう原子的に扱うかは D11 で定める（`BridgeAccessibilityService.getInstance()` は `private static volatile` の静的インスタンスで、`onServiceConnected()` で設定され、`onDestroy()` で null に戻る）。
- **D3 — ショートカット経路を削除する。** 削除するもの: アプリ内の作成ボタン (`R.id.a11y_off_shortcut_button`)、`PinRequestManager.java`、`MainActivity` の無効化インテント受付 (`onCreate` / `onNewIntent` での `handleDisableIntent`、`handleDisableA11y`)、トークン (`a11y_off_token_prefs`、`getOrCreateA11yOffToken`、`isA11yOffTokenMatch`、`readA11yOffToken`)、`backup_rules.xml` / `data_extraction_rules.xml` (トークン除外のみのファイルのため、ファイルごと削除し、マニフェストの `fullBackupContent` / `dataExtractionRules` 属性も外す)。既存のホーム画面ショートカットは、押しても無効化しなくなる (MainActivity が開くだけ)。サイガマンがホーム画面からアイコンを削除する。削除の完全性: ファイルを消すだけでは不十分なため、リポジトリ全体で次の残存参照を検索し、すべて消すこと（§4 の否定テストで確認する）。`a11y_off_shortcut_button`、`PinRequestManager`、`a11y_off_token_prefs`、`getOrCreateA11yOffToken`、`isA11yOffTokenMatch`、`readA11yOffToken`、`handleDisableIntent`、`handleDisableA11y`、ショートカット専用 Intent extra / action、ショートカット専用文字列・リソース、`requestPinShortcut` などの作成 API。バックアップ規則ファイルは 2026-11-09 に内容を確認したところ、`a11y_off_token_prefs.xml` の除外だけを含んでおり、他のデータ保護規則はない。したがってファイルごと削除してよい。既存端末の SharedPreferences に残る古いトークンデータは、そのまま残す（移行時の消去処理は入れない）。トークンは削除後のコードからは二度と読まれず、アプリ専用の prefs で、バックアップも `android:allowBackup="false"` で無効のため、残しても漏れることはない。消去のためのコードを追加する利点はない。
- **D4 — ウィジェットとタイルにトークンは不要。** トークンは、外部からのインテント入口 (ランチャーからのショートカット) を守るためのものだった。その入口が D3 で消えるため、トークン自体が不要になる。ウィジェットの受信器 (非公開、同一 UID のみ) とタイルのサービス (システムが `BIND_QUICK_SETTINGS_TILE` で束縛) は、他のアプリから到達できない。したがって実行器はトークン検査をしない。セキュリティ境界は次のとおり。ウィジェットは非公開の受信器（`android:exported="false"`、intent-filter なし）＋明示的 PendingIntent（D5）。タイルはシステムによる権限付きバインド（`android:exported="true"` と `android:permission="android.permission.BIND_QUICK_SETTINGS_TILE"` の組み合わせ。D6）。実行器は外部から直接公開しない内部処理。トークン認証をなくしても、入口の権限設計は維持する。
- **D5 — ウィジェットに「補助機能オフ」ボタンを追加する。** `widget_bridge.xml` に3つ目のボタンを追加する (既存の「開始」「開く」と並べる。レイアウトは実装時に調整する。表示する4行 (状態、モード、補助機能、更新時刻) はそのまま残す)。ボタンの PendingIntent は、非公開の受信器 (`BridgeWidgetActionReceiver` に新しいアクション `ACTION_WIDGET_A11Y_OFF` を追加する。CONS-037 P1 と同じ考え方で、公開のプロバイダーに制御アクションを持たせない) を宛先とする。PendingIntent の生成条件は次のとおり。明示的 Intent（`new Intent(context, BridgeWidgetActionReceiver.class)` に `.setAction(ACTION_WIDGET_A11Y_OFF)` を付ける。開始・停止ボタンと同じ作り方）。`PendingIntent.getBroadcast()` を使う。`FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE` を指定する。トグル（`REQUEST_TOGGLE`）や開く（`REQUEST_OPEN`）と衝突しないよう、専用の request code（例: `REQUEST_A11Y_OFF`）を使う。受信器は `android:exported="false"` で intent-filter を持たない（マニフェストに既にあるとおり）。PendingIntent 自体が操作権限を委譲する仕組みであるため、非公開の受信器だからといって取り扱いが無条件に安全になるわけではない。ただし、この PendingIntent はユーザーがホーム画面に置いたウィジェットを操作するためのシステム仲介経路であり、旧ショートカットのような一般公開のインテント入口とは異なる。タップ後、ウィジェットを即時と約 600 ms 後に更新する (開始・停止と同じパターン)。W4 (SPEC-WIDGET) を改訂する: ウィジェットは補助機能をオフにできる。オンにはできない (Android の制限のまま)。
- **D6 — 2つ目のクイック設定タイル。** 新しい `A11yOffTileService` (TileService)。ラベルは「補助機能オフ」。タイルの状態は補助機能の状態を映す。オンなら `Tile.STATE_ACTIVE`、オフなら `Tile.STATE_INACTIVE`、状態不明（UNKNOWN）なら `Tile.STATE_UNAVAILABLE`（UNKNOWN を ACTIVE / INACTIVE のどちらかに誤分類しない）。`onStartListening` で Android 側の実状態を必ず再取得して更新する。`onClick` は実行器を呼ぶだけ。Activity を開かないため、クイック設定パネルは開いたままになる (タイルの状態変化がその場で見える)。パネルを開いたまま無効化した場合にタイルが確実に INACTIVE になる仕組みは次のとおり。`TileService.requestListeningState()` だけに頼らない。`onStartListening()` はタイルがリスニング状態に「移る」ときに呼ばれるもので、すでにリスニング中（パネルが開いている間、`mListening == true`）は再呼び出しされないため、`requestListeningState()` を呼んでも開いたままのタイルは更新されない。したがって、タイルは実状態を読んで `getQsTile().setState(...)` と `getQsTile().updateTile()` を直接実行するヘルパー（例: `refreshTile()`）を持ち、次の 3 か所から直接呼ぶ。`onStartListening()`、リスニング中の `ACTION_A11Y_STATE_CHANGED` 実行時受信器の `onReceive()`、`onClick()` で `A11yOffExecutor.execute(...)` を呼んだ直後（すでにオフ／UNKNOWN などの同期早期リターン時にもタイルの表示ずれを即時補正するため）。`getQsTile()` は `onStartListening` から `onStopListening` の間だけ有効で、`updateTile()` の重複呼び出しは冪等であり害はない。無効化完了時、実行器は `TileService.requestListeningState(context, ComponentName)`（パネルが閉じていてタイルがリスニングしていない場合の再評価要求）に加えて、パッケージ限定の `ACTION_A11Y_STATE_CHANGED`（`new Intent(ACTION_A11Y_STATE_CHANGED).setPackage(appContext.getPackageName())`）を明示的に送出する。これにより、開いたままのタイルの実行時受信器とウィジェットに完了タイミングが伝わる（サービスの `onDestroy()` によるブロードキャストが `Settings.Secure` の反映よりわずかに先に届き、その後ポーリングで完了した場合でも、完了時に確実に通知される）。タイルはリスニング中に `ACTION_A11Y_STATE_CHANGED` の実行時受信器を登録し（`RECEIVER_NOT_EXPORTED`。`onStartListening` で登録、`onStopListening` で解除）、受信したら `refreshTile()` を直接呼ぶ。受信器の登録は冪等にする。`onStartListening` が `onStopListening` を挟まずに複数回呼ばれても、二重登録しないよう、登録済みフラグ（例: `receiverRegistered`）でガードする。`onStopListening` では、登録済みの場合だけ解除し、フラグを戻す。これにより、設定から再有効化した場合も、パネルを開いたまま ACTIVE に戻る。オフの状態でタップした場合は「すでにオフです」のトーストを出し、何もしない。失敗時にアプリを開くフォールバックは設けない (開くことが本仕様の目的に反するため。実行器がトーストで結果を伝える)。マニフェストの宣言は `BridgeTileService` / `LocaleTileService` と同じ構成にする。`android:exported="true"` と `android:permission="android.permission.BIND_QUICK_SETTINGS_TILE"` を組み合わせ、QS_TILE の intent-filter を付け、`android:label="@string/a11y_off_tile_label"` と `android:icon="@drawable/ic_tile"`（既存のタイルと同じアイコン。区別できるアイコンは後日の磨き上げとする）を指定する。公開されていることと、任意のアプリが自由にサービスを操作できることは同義ではない。システム権限によってバインド元を制限する。
- **D7 — 実行器は 001 の意味を保つ。** 以下の点を 001 から引き継ぐ。状態とインスタンスの組み合わせは 6 通りあり、結果は状態で決まる（順序とガード解放の契約は D11(b) を参照）。
  - 状態が UNKNOWN（インスタンスの有無を問わず）→ 無効化せず「状態を確認できませんでした」のトースト。
  - 状態が DISABLED（インスタンスの有無を問わず）→ 無効化せず「補助機能はすでにオフです」のトースト。インスタンスが残っている場合（`Settings.Secure` が先に DISABLED になり `onDestroy()` がまだ走っていない競合時）も同じ。
  - 状態が ENABLED かつインスタンスが null → 無効化せず「サービスに接続されていません。状態を確認してください」のトースト。
  - 状態が ENABLED かつインスタンスがある → `disableSelf()` を呼び、2 秒 × 3 回 (6 秒) の完了確認を行う。DISABLED を確認しない限り成功とみなさない。確認できれば「補助機能をオフにしました」、できなければ「無効化の状態を確認できませんでした」のトースト。
  - `ACTION_A11Y_STATE_CHANGED` の受信で再確認し、DISABLED のときだけ完了とする (受信はアプリケーションのコンテキストに `Context.RECEIVER_NOT_EXPORTED` で登録し、完了時に解除する。二重解除は登録フラグまたは try-catch で防ぐ)。
  - サーバーは止めない。無効化は補助機能だけを対象とする。
  - 無効化の実行中に別の入口から呼ばれた場合は無視する (後述 D9)。トーストを大量に出さない。
  - 実行器のガードと完了の契約は D11 を参照。ウィジェットの BroadcastReceiver は処理終了後にプロセスが存続する保証がないため、6 秒間の完了確認がプロセス終了で中断した場合は通常の異常系として扱う（§6 のプロセス終了の項と整合する）。
- **D8 — 実行器はヘッドレス。** Activity の参照を持たない。アプリケーションのコンテキストでトーストを出し、ブロードキャスト受信器をアプリケーションのコンテキストに登録する。MainActivity の `disableInProgress` / `disableGeneration` のガードは、実行器の中に移す (プロセス内の単一の実行器が、すべての入口からの呼び出しを直列化する)。
- **D9 — 連続した呼び出し。** 無効化の実行中にウィジェットやタイルから再度呼ばれた場合、その呼び出しは無視し、トーストも出さない (トーストの大量表示を防ぐ)。実行が終わったあとに呼ばれた場合は、状態に応じたトーストを出す (オフなら「すでにオフです」)。001 の AX-L7 の懸念 (複数 Activity) は、本仕様では発生しない。入口が Activity を作らないため、複数 Activity が同時にできる経路がない。代わりに、実行器のガード (D8) が連続呼び出しをさばく。
- **D10 — ウィジェットとタイルは状態を表示し続ける。** ウィジェットの補助機能の行と、タイルの状態は、補助機能のオン・オフを映す。設定でオンに戻せば、どちらもオンの表示に戻る (既存の `ACTION_A11Y_STATE_CHANGED` によるウィジェット更新と、タイルの `onStartListening` による更新でまかなう)。
- **D11 — 実行器の契約（インスタンス所有と原子的ガード）。** (a) サービスインスタンス: 実行器は `BridgeAccessibilityService.getInstance()` で現在のインスタンスを得る。このインスタンスは `private static volatile` で、`onServiceConnected()` で設定され、`onDestroy()` で null に戻る（スレッドセーフ）。`disableSelf()` は、状態が ENABLED かつインスタンスが null でない場合にだけ、その参照に対して一度だけ呼ぶ（順序は D11(b)）。ここで保証するのは次の3点である。(1) 永続的な保持なし — 実行器はインスタンスを保持しない。呼び出しのたびに `getInstance()` で新たに取得する。(2) 検証済み参照での一度だけの呼び出し — `disableSelf()` は、null 判定を通過した直後の参照に対して一度だけ呼ぶ。(3) DISABLED 確認なしに成功を断定しない — 完了確認が DISABLED を確認しない限り、成功とはみなさない。なお、「切断済みのインスタンスを決して呼ばない」という絶対的な主張はしない。null 判定の直後に切断された場合、その参照に対する一度だけの呼び出しは起こりうるが、(3) により誤って成功とはならない。無効化中にインスタンスが消失した場合も同じく、完了確認が成功を確認できず「無効化の状態を確認できませんでした」のトーストになる。(b) 同期的な入口の順序とガード解放: `execute()` は `synchronized (LOCK)` の中で次の順序で進む。(1) 最初に `if (disableInProgress) return;` を判定する（ログに `ignored-during-execution` を出し、トーストなしで即座に戻る）。(2) 同じ同期ブロック内で、`service = BridgeAccessibilityService.getInstance()` と `state = getAccessibilityState(appContext)` を取得し、次の早期リターンを判定する。`state == UNKNOWN`（`service` の有無を問わず）→ ログ `UNKNOWN`、「状態を確認できませんでした」のトーストを出して戻る（`disableInProgress` は `false` のまま）。`state == DISABLED`（`service` の有無を問わず）→ ログ `already-off`、「補助機能はすでにオフです」のトーストを出して戻る（`disableInProgress` は `false` のまま）。`state == ENABLED && service == null` → ログ `not-connected`、「サービスに接続されていません。状態を確認してください」のトーストを出して戻る（`disableInProgress` は `false` のまま）。この順序が重要である。早期リターン判定を `if (disableInProgress) return;` より前に置くと、1 回目の無効化実行中（`disableSelf()` で切断された後の 6 秒確認中）に 2 回目のタップが来た際、「すでにオフです」等のトーストが出てしまい、D9（実行中の再呼び出しは無視しトーストも出さない）に違反する。逆に、`execute()` 冒頭で `disableInProgress = true` を設定したまま早期リターンで戻ると、オフ状態で 1 回タップした時点（W-L2 / T-L2）で `disableInProgress` が永久に `true` のまま残り、プロセス再起動まで実行器がデッドロックする。したがって、`disableInProgress = true` を設定するのは、早期リターンの判定をすべて通過したあと、すなわち `state == ENABLED && service != null` のときだけである。(3) `state == ENABLED && service != null` のときのみ、`disableInProgress = true` と `opGen = ++disableGeneration` を設定して非同期確認へ進む。ガードの取得と世代番号の捕捉は同じ同期ブロックで行う。(4) 開始時の例外は、発生した段階で区別する。受信器登録・`service.disableSelf()`・初回ポーリング登録を一つの try/catch で囲んで単純にロールバックしてはならない。段階は次のとおり。段階 A（`disableSelf()` の前、受信器登録時）に例外が起きた場合、無効化はまだ始まっていないため、ロールバックしてよい（登録済みなら受信器を解除し、`disableInProgress = false` に戻し、「無効化できませんでした」のトーストを出す）。段階 B（`disableSelf()` 自体）に例外が起きた場合、無効化は効いていないため、同じくロールバックしてよい。段階 C（`disableSelf()` が成功したあと、初回ポーリング登録時）に例外が起きた場合、無効化は進行中であるため、ロールバックしてはならない。ガードと受信器を残し、ブロードキャストの高速完了経路で完了を待つ。さらに、状態を一度だけ即座に再確認し、すでに DISABLED なら通常どおり完了させる。ブロードキャストも届かず、再確認でも DISABLED を確認できなければ、タイムアウトのトースト（「無効化の状態を確認できませんでした」）で完了させる。いったん `disableSelf()` を呼んだら、実行器は監視を放棄しない。段階 C では、失敗したポーリングとは独立したフォールバック完了機構を必ず用意する。ポーリング登録の失敗後にタイムアウトを実際に発火させる仕組みがなければ、ガードが永久に残る恐れがあるためである。具体的には、ポーリングとは別に 1 回限りの遅延再確認（フォールバックタイムアウトコールバック）を登録する。例えば、開始から 6 秒後に 1 回だけ状態を確認するコールバックを、ポーリングの登録とは独立した経路で登録する。このフォールバックコールバックは世代番号の確認を受け（古い世代なら破棄する）、発火時に最終的な状態確認を行い、DISABLED なら成功のトースト、そうでなければタイムアウトのトーストで完了させる。これにより、ブロードキャストが `Settings.Secure` への反映より先に届いて受信器が早すぎる時点で ENABLED を見た場合でも、フォールバックが発火して完了する。フォールバックの登録自体が例外で失敗した場合は、即座に最終的な状態確認を行って完了させる（DISABLED なら成功、そうでなければタイムアウトのトースト）。段階 C では、どのような場合でもガードが残り続けることはない。(c) 完了の原子性と受信器の所有権: 完了処理（`finishDisable`）は一度だけ実行する。ブロードキャストの高速完了経路と 2 秒間隔のポーリングの両方が、同じ完了処理を呼ぶ。完了の所有権は同期ブロック内で原子的に取得する。同期ブロックの中で `disableInProgress` が true かつ `opGen == disableGeneration` であることを確認し、確認できたらその場で `disableInProgress = false` にして完了を自分のものにする（所有権の取得）。確認できなければ何もしない（すでに完了済み、または古い世代の処理）。所有権を取得した呼び出し元だけが、ブロードキャスト受信器を解除し、トーストを出す。受信器の解除は一度、トーストも一度。古い世代に属するタイマーやブロードキャストは、この確認で破棄される。受信器は操作ごとに所有する。`finishDisable(opGen)` は、自分の世代で登録した受信器だけを解除する。ガードを解除したあとにロック外で受信器を解除すると、次の世代（N+1）の `execute()` が新しい受信器を登録したあとに、世代 N の後処理がそれを解除してしまう恐れがあるため、これを防ぐ。具体的には、受信器の参照を世代番号と一体として管理する（例: 世代番号と受信器参照を同じホルダーに格納する）。完了の同期ブロックの中で、所有権の取得と同時にその世代の受信器参照を取り出し（取り出したあとは共有フィールドを空にする）、取り出した参照だけを解除する。取り出した参照はその世代のものだけであり、後から登録された受信器に触れることはない。あるいは、受信器の解除を同じ同期ブロックの中で行う（`unregisterReceiver` はブロックしない局所操作であり、同期ブロック内での呼び出しは安全である）。どちらの方法でも、世代 N の完了処理が世代 N+1 の受信器を解除することはない。完了時に受信器を解除し、ガードを確実に解除する。`ACTION_A11Y_STATE_CHANGED` は高速完了経路として残すが、ポーリングとの二重完了は許さない。(d) ログ: 実行器はタグ `A11yOff` でログを出す。無効化要求、すでにオフ、UNKNOWN、実行中の無視、成功、タイムアウトを区別する。完了時は完了トリガーの種別（`broadcast` か `poll` か）も区別して出す。無効化のたびに大量のログを出さない。

## 3. 実装計画

### 新規ファイル

- `android-app/src/dev/muse/bridge/A11yOffExecutor.java` — 無効化の共通実行器 (D2、D7、D8、D9、D11)。
  - `execute(Context appContext)` 静的メソッド: 3値の状態判定、`disableSelf()`、2 秒 × 3 回の完了確認、トースト、`ACTION_A11Y_STATE_CHANGED` の受信と解除、実行中ガード。
  - MainActivity から移すもの: `handleDisableA11y` の本体 (トークン検査を除く)、`scheduleDisableCheck`、`finishDisable`、`getAccessibilityState`、`onA11yBroadcastForDisable` の相当処理、`disableInProgress` / `disableGeneration`。
  - インスタンスは `BridgeAccessibilityService.getInstance()` で取得し、状態が ENABLED かつ null でない場合にだけ `disableSelf()` を呼ぶ（D11a）。取得した参照はその回の呼び出しだけに使い、途中で再取得しない。
  - 入口の順序は D11(b) のとおり。`synchronized (LOCK)` の中で、まず `if (disableInProgress) return;` を判定し、次に `service` と `state` を取得して早期リターン（UNKNOWN / DISABLED / ENABLED かつ service null）を判定する。`disableInProgress = true` と世代番号の捕捉は、早期リターンをすべて通過したあと（`state == ENABLED && service != null` のときだけ）に行う。開始時の例外は D11(b) の段階別契約に従う。`disableSelf()` の前（段階 A）および `disableSelf()` 自体（段階 B）の例外ではロールバックする（登録済みなら受信器を解除し、`disableInProgress = false` に戻す）。`disableSelf()` 成功後（段階 C）の例外ではロールバックせず、ガードと受信器を保持して監視を続け、ポーリングとは独立したフォールバック完了機構（1 回限りの遅延再確認）で完了を保証する。フォールバックの登録自体が失敗した場合は、即座に最終的な状態確認を行って完了させる。
  - 完了処理は、同期ブロック内で所有権を原子的に取得して（一度だけ `disableInProgress = false` にして完了を自分のものにする）一度だけ実行し、受信器を解除してトーストを出す（D11c）。受信器の登録は `Context.RECEIVER_NOT_EXPORTED` を指定し（targetSdk 34 で必須）、解除時の二重解除（`IllegalArgumentException`）は登録フラグまたは try-catch で防ぐ。
  - 無効化完了時（成功・失敗を問わず）に `TileService.requestListeningState(appContext, new ComponentName(appContext, A11yOffTileService.class))` を呼び、さらにパッケージ限定の `ACTION_A11Y_STATE_CHANGED` を明示的に送出する（D6）。
  - ログのタグは `A11yOff`。無効化要求、すでにオフ、UNKNOWN、実行中の無視、成功、タイムアウトを区別し、完了時は完了トリガーの種別（`broadcast` か `poll` か）も出す（D11d）。
- `android-app/src/dev/muse/bridge/A11yOffTileService.java` — 2つ目のクイック設定タイル (D6)。
  - `onStartListening` で Android 側の実状態を必ず再取得して状態を更新する。`onClick` で `A11yOffExecutor.execute()` を呼ぶ。Activity を開かない。
  - タイルの状態: 補助機能がオンなら `Tile.STATE_ACTIVE`、オフなら `Tile.STATE_INACTIVE`、状態不明なら `Tile.STATE_UNAVAILABLE`。
  - 実状態を読んで `getQsTile().setState(...)` と `getQsTile().updateTile()` を直接実行するヘルパー（例: `refreshTile()`）を持つ。`onStartListening()`、リスニング中の `ACTION_A11Y_STATE_CHANGED` 実行時受信器の `onReceive()`、`onClick()` で実行器を呼んだ直後の 3 か所から直接呼ぶ（`requestListeningState()` による間接呼び出しに依存しない）。
  - リスニング中に `ACTION_A11Y_STATE_CHANGED` の実行時受信器を登録し（`RECEIVER_NOT_EXPORTED`。`onStartListening` で登録、`onStopListening` で解除）、受信したら `refreshTile()` を直接呼ぶ。登録は冪等にする（登録済みフラグでガードし、二重登録しない）。解除時の二重解除（`IllegalArgumentException`）は登録フラグまたは try-catch で防ぐ。

### 変更するファイル

- `android-app/src/dev/muse/bridge/BridgeWidgetActionReceiver.java` — 新しいアクション `ACTION_WIDGET_A11Y_OFF` を追加する (D5)。受けたら `A11yOffExecutor.execute()` を呼び、ウィジェットを即時と約 600 ms 後に更新する。開始・停止の経路は変えない。
- `android-app/src/dev/muse/bridge/BridgeWidgetProvider.java` — 「補助機能オフ」ボタンの PendingIntent を作る (D5)。明示的 Intent（`new Intent(context, BridgeWidgetActionReceiver.class)` に `.setAction(ACTION_WIDGET_A11Y_OFF)`）、`PendingIntent.getBroadcast()`、`FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE`、専用の request code（例: `REQUEST_A11Y_OFF`）。開始・停止ボタンと同じ作り方。
- `android-app/res/layout/widget_bridge.xml` — 「補助機能オフ」ボタンを追加する (D5)。ボタンの id は `widget_a11y_off_button` とする。
- `android-app/res/layout/activity_main.xml` — ショートカット作成ボタン (`a11y_off_shortcut_button`) を削除する (D3)。
- `android-app/res/values/strings.xml` — タイルのラベル (`a11y_off_tile_label` = 「補助機能オフ」) と、ウィジェットのボタンの文言 (`widget_a11y_off_button` = 「補助機能オフ」) を追加する。ショートカット専用文字列 (`btn_a11y_off_shortcut`、`a11y_off_short_label`、`a11y_off_long_label`) を削除する (D3)。トーストの文言は 001 のものを流用する (`toast_a11y_off_done` ほか)。
- `android-app/AndroidManifest.xml` — 新しいタイルのサービス (`A11yOffTileService`、`android:exported="true"`、`android:permission="android.permission.BIND_QUICK_SETTINGS_TILE"`、QS_TILE の intent-filter、`android:label="@string/a11y_off_tile_label"`、`android:icon="@drawable/ic_tile"`。`BridgeTileService` / `LocaleTileService` と同じ構成) を追加する。`backup_rules.xml` / `data_extraction_rules.xml` の属性を外す (D3)。`android:allowBackup="false"` は維持する。ウィジェットの受信器の宣言は変えない (`android:exported="false"`、intent-filter なし。新しいアクションは intent-filter を持たない非公開の受信器への明示インテントのため)。
- `android-app/src/dev/muse/bridge/MainActivity.java` — ショートカット作成ボタン (`pinButton`) とそのリスナー、無効化インテントの受付 (`handleDisableIntent`、`handleDisableA11y`)、トークン関連 (`getOrCreateA11yOffToken`、`isA11yOffTokenMatch`、`readA11yOffToken`、`A11Y_OFF_TOKEN_PREFS`、`KEY_A11Y_OFF_TOKEN`)、無効化の完了確認 (`scheduleDisableCheck`、`finishDisable`、`onA11yBroadcastForDisable`、`disableInProgress` / `disableGeneration`) を削除する。`getAccessibilityState` は実行器へ移す (MainActivity に残さない)。ほかの MainActivity の機能 (開始・停止、モード切替、トークン表示など) は変えない。
- `android-app/build.sh` — `VERSION_CODE` を 59 から 60 へ、`VERSION_NAME` を "2.9.6" から "2.9.7" へ上げる。

### 削除するファイル

- `android-app/src/dev/muse/bridge/PinRequestManager.java` (D3。ショートカット作成の状態機械は、作成経路とともに不要になる)。
- `android-app/res/xml/backup_rules.xml`、`android-app/res/xml/data_extraction_rules.xml` (D3。トークン除外のみのファイルのため)。

### ビルド

- 既存の `build.sh` (aapt2) が新しいレイアウトとタイルのリソースを拾う。ビルドスクリプトの変更は、バージョン番号以外は不要な見込み。

## 4. テスト

Python のスイートでは Java を実行できないため (001 と同じ制約)、ソース水準のガードテストを `test_android_app_client.py` に追加する (同じ正直な書き方)。

- マニフェストが新しいタイルのサービスを宣言している (`A11yOffTileService`、`BIND_QUICK_SETTINGS_TILE`、`QS_TILE`)。
- ウィジェットのレイアウトに「補助機能オフ」ボタンがある (`widget_a11y_off_button`)。
- タイルのサービスが MainActivity を開かない (ソースに `startActivity` / `MainActivity.class` がない)。
- 実行器が 3値の状態判定と 2 秒 × 3 回の完了確認を持ち、DISABLED を確認しない限り成功とみなさない (ソース水準)。
- 削除の確認 (否定的なテスト): ショートカット作成ボタンの id (`a11y_off_shortcut_button`) がレイアウトと MainActivity にない。`PinRequestManager.java` が存在しない。トークンの prefs (`a11y_off_token_prefs`、`getOrCreateA11yOffToken`、`isA11yOffTokenMatch`、`readA11yOffToken`) がソースにない。`backup_rules.xml` / `data_extraction_rules.xml` が存在しない。マニフェストに `fullBackupContent` / `dataExtractionRules` の属性がない。さらに次も確認する (CONS-099、Argon)。ショートカット専用 Intent extra / action がソースに残っていない。ショートカット専用文字列・リソースが残っていない。`requestPinShortcut` などの作成 API が残っていない。`handleDisableA11y` と `handleDisableIntent` が MainActivity に残っていない（二重実装の防止）。新しいウィジェット PendingIntent が `FLAG_IMMUTABLE` を指定し、明示的 Intent で `BridgeWidgetActionReceiver` を指定し、トグル／開くの PendingIntent と異なる request code を使う。`BridgeWidgetActionReceiver` が `android:exported="false"` で、intent-filter を持たない。`A11yOffTileService` が `android:exported="true"` かつ `android:permission="android.permission.BIND_QUICK_SETTINGS_TILE"` で、QS_TILE の intent-filter を持ち、`android:label` と `android:icon` を指定している。マニフェストの `android:allowBackup="false"` が維持されている。実行器が `getInstance()` の null 判定を持ち、ガードの取得と世代番号の捕捉を同期的に行い、完了処理が世代番号を確認する（ソース水準）。実行器とタイルの実行時受信器の登録が `Context.RECEIVER_NOT_EXPORTED` を指定している。タイルが実状態を読んで `getQsTile().updateTile()` を直接呼ぶヘルパー（`refreshTile()`）を持ち、`onStartListening`・実行時受信器の `onReceive`・`onClick` 直後の 3 か所から呼んでいる。受信器が操作ごとに所有されていること（`finishDisable(opGen)` が自分の世代の受信器だけを解除すること、または受信器の解除が完了の同期ブロックの中で行われていること）。開始時の例外処理が段階別であること（`disableSelf()` の前の例外ではロールバックし、`disableSelf()` 成功後の例外ではガードと受信器を残して監視を続けること）。段階 C で、ポーリングとは独立したフォールバック完了機構（1 回限りの遅延再確認）が存在し、フォールバックの登録失敗時には即座に最終的な状態確認で完了すること。
- 既存の 001 のテストのうち、ショートカット作成とトークンに関するもの (AX-01〜AX-100 の該当分) は、本仕様の実装時に削除または置き換える。削除するテストは、削除の理由とともにコミットメッセージに記す。
- フル・スイートが通ること。APK を再ビルドする (v2.9.7 / code 60)。

## 5. 実機確認 (実機、v2.9.7)

ウィジェット:

- W-L1: ウィジェットの「補助機能オフ」ボタンを押す → アプリが開かずに補助機能がオフになる。ウィジェットの補助機能の行が更新される。「補助機能をオフにしました」のトーストが出る。
- W-L2: オフの状態でもう一度押す → 「補助機能はすでにオフです」のトーストが出る。クラッシュしない。
- W-L3: 連続して 3 回押す (サイガマンの手で) → クラッシュしない。トーストが大量に出ない。最終的にオフになる。サーバーは止まらない。

タイル:

- T-L1: クイック設定にタイルを追加し、タップする → アプリが開かずに補助機能がオフになる。クイック設定パネルは開いたままになる。タイルが INACTIVE になる。「補助機能をオフにしました」のトーストが出る。
- T-L2: オフの状態でもう一度タップする → 「補助機能はすでにオフです」のトーストが出る。クラッシュしない。
- T-L3: 連続して 3 回タップする (サイガマンの手で) → クラッシュしない。トーストが大量に出ない。最終的にオフになる。サーバーは止まらない。
- T-L4: T-L5 でオンに戻したあと、パネルを開いたままもう一度タイルをタップして無効化する → パネルは開いたまま、タイルが INACTIVE に更新される（繰り返しの更新が働くこと）。「補助機能をオフにしました」のトーストが出る。
- T-L5: クイック設定を開いたまま、設定から補助機能をオンに戻す → タイルが ACTIVE に戻る。

交差呼び出し:

- X-L1: ウィジェットのボタンとタイルを交互に連続して操作する → 共通のガードが働き、トーストが重複せず、クラッシュしない。最終的にオフになる。サーバーは止まらない。

共通:

- L4: 無効化のあと、サーバーは動き続ける (`/health` は ok)。`/state` は 503 を返す。
- L5: 設定で補助機能をオンに戻す → ウィジェットとタイルがオンの表示に戻る。画面操作が再び動く (`/health` の accessibility が true)。
- L6: 操作中の無効化 — Bridge から画面操作を開始し、その最中にタイルまたはウィジェットのボタンで無効化する。操作は適切に終了または失敗し、サーバーは動き続け、操作中の通知は残らず、次のリクエストは 503 を返す。
- L7: ブロードキャストの高速完了経路と、ポーリングの通常完了経路をそれぞれ確認する。高速経路: 無効化が速く終わる場合にトーストが出ること。通常経路: ブロードキャストが届かなくても 2 秒 × 3 回の確認で完了すること。検証可能範囲は次のとおり。製品版 APK の実機タップだけではブロードキャスト不達を強制再現できない（`onDestroy()` が即座にブロードキャストを送り、`RECEIVER_NOT_EXPORTED` のため非特権の `adb shell am broadcast` でも注入できない）。したがって、ブロードキャストを抑止してポーリングだけで完了する動作の実行検証は、製品版 APK ではできない。§4 のテストは、ポーリング経路の実装構造の確認に限る（ポーリングのコードが存在すること、完了処理がブロードキャスト経路とポーリング経路で共有されていること、古い世代の完了が破棄されること）。実機では、W-L1〜L3 / T-L1〜T-L3 / X-L1 の実行時に `adb logcat -s A11yOff` で完了トリガーの種別（`broadcast` か `poll` か）の記録を確認し、完了が一度だけ記録されていることを確かめる。ログに `poll` の完了記録があれば、ポーリング経路が実際に使われた証拠になる。ただし、ブロードキャスト不達を強制してポーリング単独経路を実機で再現することはできないため、L7 の実機確認は「完了トリガーの種別と重複完了の有無の記録確認」であり、ポーリング単独経路の強制再現ではない。
- L8: 無効化完了の直前にブロードキャストとポーリングが競合しても、完了のトーストが重複しないこと。製品版 APK の実機タップだけでは完了直前の競合をミリ秒単位で強制再現できないため、W-L1〜L3 / T-L1〜T-L3 / X-L1 の実行時に `adb logcat -s A11yOff` で完了が一度だけ記録されていることを確認する。二重呼び出し排他の強制検証は §4 のテストで担保する。
- 旧ショートカット: 既存のホーム画面ショートカットを押す → MainActivity が開くだけで、無効化しない。サイガマンがアイコンをホーム画面から削除する。
- トーストの表示: トーストがウィジェットとタイルの両方で同じように表示されることを確認する（Application Context での表示。P2-02）。
- ウィジェットの表示: 3 ボタン化で表示 4 行が圧迫されていないか、Galaxy A55 のホーム画面で視認性を確認する（P2-03）。

## 6. 境界例

- サーバーが無効化の実行中に止まった場合: サーバーの停止と補助機能の無効化は別の状態。サーバーが止まっても補助機能のオン・オフは変わらないため、「すでにオフです」とは表示しない。Android 側の状態を確認し、結果に応じたトーストを出す。クラッシュしない (001 §6 と同じ)。
- 状態の確認に失敗した場合 (UNKNOWN): 「状態を確認できませんでした」のトーストを出し、無効化しない (001 §6 と同じ)。
- 端末を再起動した場合: ウィジェットとタイルは残る (Android の仕様)。補助機能のオン・オフは再起動前の状態に従う (Android の仕様)。
- ウィジェットをホーム画面から削除した場合: アプリ側の状態は変わらない。再度置くには、ホーム画面の長押しからウィジェットを選び直す。
- タイルをクイック設定から外した場合: アプリ側の状態は変わらない。再度追加するには、クイック設定の編集から選び直す。
- プロセスが終了した場合: 実行器のメモリ上の状態は失われる。アプリが再起動すれば初期状態に戻る。前回の要求が成功したかは不明。無効化そのものは、実行済みであれば Android 側の状態として残る (001 §6 と同じ)。ウィジェットの BroadcastReceiver は onReceive が終わったあとにプロセスが存続する保証がないため、6 秒間の完了確認がプロセス終了で中断することがある。これは通常の異常系として扱い、次回の呼び出し時に状態を新たに判定する (D7)。
- 無効化の実行中にプロセスが終了した場合: 完了確認のタイマーは失われる。補助機能の実際の状態は、設定画面と `/health` で確認する。実行器は、次回の呼び出し時に状態を新たに判定する。

## 7. 対象外 (やらないこと)

- ウィジェットやタイルから補助機能をオンに戻すこと (Android の制限のため不可能。001 §7 と同じ)。
- 実行時の確認ダイアログ (ウィジェットやタイルのタップ自体が明示的な操作のため、表示しない。001 §7 と同じ)。
- ホーム画面ショートカット (本仕様で廃止する。復活させない)。
- 透明な Activity による画面遷移なしの実行 (本仕様では、実行器が Activity を使わないため不要)。
- 実行器の多重起動のキューイング (実行中の呼び出しは無視する。キューにためない)。

## 8. 001 からの流用と削除の一覧

流用するもの (001 v13):

- 3値の状態判定 (`getAccessibilityState`)、2 秒 × 3 回の完了確認、トーストの文言、サーバーを止めない規則、境界例の考え方。

削除するもの (001 のショートカット経路):

- 作成ボタン、`PinRequestManager` (状態機械を含む)、トークンとその prefs、バックアップ除外の規則ファイル、MainActivity のインテント受付。
- 001 のテストのうち、作成とトークンに関するもの。
- バックアップ規則ファイルは 2026-11-09 に内容を確認済みで、トークン prefs の除外だけを含む（他のデータ保護規則なし）。既存端末の SharedPreferences に残る古いトークンデータはそのまま残す（移行時の消去処理は入れない。D3）。

残すもの (001 の成果として):

- 実機確認 AX-L1〜L5 の結果は、無効化の仕組みが動くことの証拠として残る。本仕様の実機確認 (W-L1〜L3、T-L1〜T-L5、X-L1、L4〜L8) は、入口が変わった分の確認であり、仕組み自体の再確認ではない。
