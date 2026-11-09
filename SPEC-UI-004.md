# Muse Bridge — タイル長押しで MainActivity を開く (SPEC-UI-004)

- 状態: v4 FROZEN（凍結。採用済み。2026-11-10 01:18 +07 にユーザー決定、OP-02 受け入れ。CONS-113 焦点再レビュー ADOPTABLE 10.0/10、P0/P1/P2=0）
- 対象バージョン: v2.9.10 / code 63（SPEC-UI-003 v2 と同梱。2026-11-10 00:29 +07 にユーザー決定）
- 言語: 日本語
- 前提: SPEC-UI-003 v2（凍結）、SPEC-A11Y-OFF-002 v6（凍結）、SPEC-LOCALE-TILE-001 v4（凍結）。マニフェストのみの変更。Java / Kotlin のコード変更はない。
- 出典: ユーザーの要望 2026-11-09 03:35 +07「Muse Bridge のメインのタイルを長押ししたら MainActivity を開く」。長押しで現在はアプリ情報が開くことは、ユーザーが 2026-11-09 04:12 +07 に実機で確認済み。

### v1 → v2 変更点（CONS-113 の指摘への対応）

- P1-01（CATEGORY_DEFAULT の欠落）: D1 の intent-filter に `android.intent.category.DEFAULT` を追加。「カテゴリは付けない」を削除し、公式「Intents and intent filters」の2規則を根拠として明記。
- P1-02（Back スタックの断定）: L-T5 と §6 を書き換え。「戻るボタンで元のインスタンスに戻る」は実機で確認する期待結果とし、タスク配置と intent のフラグが未確認であることを明記。
- P2-01（公開面の説明）: 「新たな公開面は増えない」を「既存の exported な Activity に新しい暗黙的 Intent の解決経路を追加するが、新しい Activity や権限は追加しない」に修正。
- P2-02（実機記録の条件）: §5 に記録規則を追加（端末・OS・One UI ビルド・アプリ版・タイル配置・日時・長押し結果・パネル状態）。
- P2-03（L-T8 のプロセス終了確認）: 履歴からの削除による推測をやめ、Termux の `pidof` でプロセス不在を確認してから試験する手順に変更。
- P2-04（テストの厳密化）: §4 に一意性の assert を追加（MainActivity に1つだけ、他に同アクションなし、MAIN/LAUNCHER と分離、APK 最終 Manifest の確認）。検証層を3層（ソーステスト・APK 検査・実機確認）として明記。
- L-T9（ロック中の長押し）・L-T10（編集モードでの長押し）を独立した実機確認 ID として追加（§5・§6）。

### v2 → v3 変更点（CONS-113 再レビューの指摘への対応）

- P2-05（Termux の pidof の信頼性）: L-T8 から Termux の `pidof` によるプロセス不在の確認手順を削除。Android 10 以降、/proc は hidepid=2 で制限され、アプリは自分のプロセスしか見えないため、Termux（通常のアプリ UID）の `pidof dev.muse.bridge` は Bridge のプロセスが生きていても何も返らず、空振りチェックになる（端末は Android 15）。代わりに、(1)「設定 → アプリ → Muse Bridge → 強制停止」を主たる終了操作とし、(2) 終了の確認は「設定 → 開発者向けオプション → 実行中のサービス」で dev.muse.bridge が無いことを見る、(3) ADB が使える場合は `adb shell pidof dev.muse.bridge` を代替手段とする。GPT の提案した「Termux の PID 確認は補助的に」は採用しない（空振りチェックは誤判定を招くため）。

### v3 → v4 変更点（CONS-113 焦点再レビューの指摘への対応）

- P2-06（Running services の表示だけではプロセス不存在を証明できない）: L-T8 の終了確認手順を厳密化。(1) ADB が使える場合は `adb shell pidof dev.muse.bridge` を優先的な確認方法とする（v3 では代替手段だったものを優先へ格上げ）。(2) Running services を使う場合は、実行中のサービスの表示に加えてキャッシュプロセスの表示も確認対象に含める（Activity を保持したままのキャッシュプロセスは別表示になるため）。(3) Running services で見つからない場合の判定は「表示上、確認できない」と記録し、プロセス不存在の確証とは区別する。プロセス終了の根拠は強制停止そのものであり、変わらない。

## 1. 背景と目的

現在、クイック設定タイルを長押しすると、アプリ情報（App Info）が開く。ユーザーが 2026-11-09 04:12 +07 に実機で確認した挙動である。タイルの長押しは、アプリの本体画面を開くための入口として使われることが多い。Muse Bridge では、メイン画面（MainActivity）にすぐ戻れることが便利である。

本仕様は、タイルの長押しで MainActivity を開くようにする。方法はマニフェストへの intent-filter 追加だけであり、コードの変更は不要である（§3 で検証方法を示す）。

## 2. 決定事項

- **D1 — MainActivity に `QS_TILE_PREFERENCES` の intent-filter を追加する。** `AndroidManifest.xml` の `MainActivity` の宣言に、次の intent-filter を1つ追加する（既存の MAIN / LAUNCHER の intent-filter は触らない）。

  ```xml
  <!-- SPEC-UI-004 (v2.9.10): タイル長押しで MainActivity を開く。 -->
  <intent-filter>
      <action android:name="android.service.quicksettings.action.QS_TILE_PREFERENCES" />
      <category android:name="android.intent.category.DEFAULT" />
  </intent-filter>
  ```

  - 根拠（Android 公式 API リファレンス、TileService）。`ACTION_QS_TILE_PREFERENCES` は「TileService の設定を調整するユーザーインターフェイスを提供するアクティビティ」であり、TileService を実装するアプリには任意だが推奨される。定数値は `android.service.quicksettings.action.QS_TILE_PREFERENCES`。出典：`~/workspace/archives/android-api-reference-2026-11-10.md` の TileService 節、および公式ページ https://developer.android.com/reference/android/service/quicksettings/TileService（2026-11-10 読了）。
  - カテゴリは `android.intent.category.DEFAULT` を付ける。理由は公式ガイド「Intents and intent filters」（https://developer.android.com/guide/components/intents-filters、2026-11-10 読了）の2つの規則による。(1) `startActivity()` に渡される暗黙的 intent には、システムが自動的に `CATEGORY_DEFAULT` を付ける。暗黙的 intent を受け取るには、intent-filter に `android.intent.category.DEFAULT` が必要である。システムが通常の暗黙的起動でこの intent を解決するなら、カテゴリのないフィルタでは MainActivity は解決対象にならない。(2) カテゴリのテストは「intent のすべてのカテゴリがフィルタのカテゴリと一致しなければならない。逆は不要であり、カテゴリのない intent は、フィルタに何が宣言されていても必ずこのテストを通る」。したがって `DEFAULT` を追加しても、カテゴリを付けない intent（QS 固有の解決経路）には影響しない。`DEFAULT` の追加は、通常の暗黙的起動の経路と QS 固有の経路の両方で解決されるためのものである。出典：`~/workspace/archives/android-api-reference-2026-11-10.md` の「Intents and intent filters」節。
  - `MainActivity` の `android:exported="true"` は変えない。`BIND_QUICK_SETTINGS_TILE` 権限を `MainActivity` に付けることもしない。公式リファレンスには、設定アクティビティをその権限で保護できることが書いてあるが、`MainActivity` はホーム画面からの起動口でもあるため、その権限を要求するとランチャーから開けなくなる。`MainActivity` はもともと公開されており、どのアプリからでも明示的に起動できる。ただし、この変更は既存の exported な Activity に新しい暗黙的 Intent の解決経路を追加するものであり、新しい Activity や権限は追加しない。外部アプリからの起動可能性という意味では、intent の受け口が増える。
- **D2 — 3つのタイルの長押しは、すべて MainActivity を開く。** intent-filter はアクティビティに宣言するものであり、タイルごと（TileService ごと）には宣言できない。そのため、システムはどのタイルの長押しでも同じ `MainActivity` に解決する。

  | タイル | 長押し | 備考 |
  |---|---|---|
  | Muse Bridge | MainActivity を開く | 本仕様の目的 |
  | 補助機能オフ | MainActivity を開く | 副作用。タップの挙動は変わらない |
  | 言語設定 | MainActivity を開く | 副作用。タップの挙動は変わらない |

  - この副作用は不具合ではなく、受け入れた設計である。ユーザーは 2026-11-09 03:35 +07 の時点でこの仕組みを把握しており、採用時に改めて受け入れを確認する（§9 OP-02）。
  - システムはこの intent に `Intent.EXTRA_COMPONENT_NAME` を付けて、どのタイル（ComponentName）が長押しの原因かを示すことがある（公式リファレンス）。`MainActivity` は起動 intent の内容を読まないため（`getIntent` / `getStringExtra` / `onNewIntent` の使用なし。ソースで確認済み）、この値は無視され、どのタイルからでも同じメイン画面が開く。
  - 長押しでアプリ情報が開く挙動はなくなる。アプリ情報自体は、設定 → アプリ → Muse Bridge から引き続き開ける。
- **D3 — 変えないもの。** 次は本仕様では触らない。
  - 3つのタイルのタップ時の挙動。Muse Bridge タイルの開始／停止、`A11yOffTileService` の無効化、`LocaleTileService` の言語設定起動は、すべて従来どおり（SPEC-UI-003 D1・D2、SPEC-A11Y-OFF-002 D7、SPEC-LOCALE-TILE-001 L2）。
  - `A11yOffExecutor`、3つの `TileService` のコード、ウィジェット、`MainActivity` のコード、文字列リソース、アイコン、タイルのラベルと状態の意味（SPEC-UI-003 D2 の表）。
  - `MainActivity` の `launchMode`。マニフェストに指定はなく、既定値の `standard` のままにする（§6 の境界例を参照）。`singleTop` 化は別途検討する（§9 OP-03）。
- **D4 — バージョンは変えない。** 対象は v2.9.10 / code 63（SPEC-UI-003 v2 と同梱）。`build.sh` は SPEC-UI-003 v2 の実装ですでに 63 / "2.9.10" になっているため、本仕様ではバージョン番号を触らない。本仕様を入れたビルドも同じ v2.9.10 になる。SPEC-UI-003 v2 のみを入れた中間ビルド（ローカルのみ、未配布）とは区別し、配布するのは本仕様を入れた後の v2.9.10 とする。

## 3. 実装計画

### 変更するファイル

- `android-app/AndroidManifest.xml` — `MainActivity` の宣言に、D1 の intent-filter を1つ追加する。ほかの宣言は変えない。追加する位置は、`MainActivity` の既存の intent-filter（MAIN / LAUNCHER）の直後とする。
  - 変更前の `MainActivity` の宣言（現行。2026-11-10 にソースで確認）。

    ```xml
    <activity
        android:name=".MainActivity"
        android:exported="true">
        <intent-filter>
            <action android:name="android.intent.action.MAIN" />
            <category android:name="android.intent.category.LAUNCHER" />
        </intent-filter>
    </activity>
    ```

  - 3つの `TileService` の宣言は変えない。いずれも `exported="true"`、`BIND_QUICK_SETTINGS_TILE`、`QS_TILE` の intent-filter のみである。

### コード変更が不要であることの検証

- `QS_TILE_PREFERENCES` の出現は、マニフェストとソース全体で 0 件である（2026-11-10 に `grep -rn` で確認）。追加するものはマニフェストの宣言だけであり、Java / Kotlin の新規・変更ファイルは不要である。
- `MainActivity.java` は起動 intent の内容を読まない（`getIntent` / `getStringExtra` / `EXTRA` / `onNewIntent` の使用なし。2026-11-10 にソースで確認）。したがって、`EXTRA_COMPONENT_NAME` の有無や内容が挙動に影響することはなく、MainActivity のコード変更は不要である。
- システムが intent-filter を解決してアクティビティを起動する仕組み自体は、公式リファレンスの `ACTION_QS_TILE_PREFERENCES` の定義による。

### 変更しないファイル

- Java / Kotlin のソースはすべて変更しない。`strings.xml`、レイアウト XML、ドローアブル、`build.sh`（バージョン変更なし。D4）も変更しない。

### ビルド

- 既存の `build.sh`（kotlinc + javac の 2 コンパイラ経路。SPEC-KOTLIN-MIGRATION-001 v2.2）でビルドする。ビルドスクリプトの変更は不要である。
- 本仕様の変更は `MainActivity.java`（Java のまま）にも `A11yOffTileService` にも触らないため、Kotlin 移行の段取り（stage 2 は `A11yOffExecutor` の次）とは干渉しない。

## 4. テスト

Python のスイートでは Java を実行できないため（SPEC-UI-003 §4 と同じ制約）、ソース水準のガードテストを `test_android_app_client.py` に追加する。既存のマニフェスト用ヘルパー `_manifest_source()` を使う（SPEC-UI-003 §4 と同じ）。

- **追加するテスト（長押しの宣言）**: `test_ui004_longpress_manifest_mainactivity` を新設し、次を assert する。`MainActivity` の宣言内に `android.service.quicksettings.action.QS_TILE_PREFERENCES` を含む intent-filter が**ちょうど1つだけ**存在すること。その intent-filter が `android.intent.category.DEFAULT` を含むこと（D1）。`MainActivity` の既存の MAIN / LAUNCHER の intent-filter が、QS_TILE_PREFERENCES の intent-filter とは**別の intent-filter として**残っていること。`MainActivity` が引き続き `exported="true"` であること。`MainActivity` の宣言に `BIND_QUICK_SETTINGS_TILE` 権限が付いていないこと（D1）。
- **追加するテスト（マニフェストのみの変更）**: `test_ui004_longpress_manifest_only` を新設し、次を assert する。`QS_TILE_PREFERENCES` を含むファイルが `AndroidManifest.xml` だけであること（Java / Kotlin ソースに同文字列がないこと）。`MainActivity` 以外の Activity と3つの `TileService` のいずれも `QS_TILE_PREFERENCES` を含まないこと。3つの `TileService` の宣言が変わっていないこと（`QS_TILE` の intent-filter のみであること）。
- **検証層（3層）**: ソース水準のテストは、マニフェストの記述だけを検証するものであり、APK にパッケージされた最終 Manifest や One UI の動作を保証しない。検証は次の3層に分ける。(1) ソーステスト（上記の2件。マニフェストの記述と一意性）。(2) APK 検査（ビルドした APK の最終 Manifest を `aapt2 dump xmltree` などで確認し、MainActivity に QS_TILE_PREFERENCES の intent-filter が1つだけ含まれ、他に同アクションの宣言がないことを確かめる）。(3) 実機確認（§5 の L-T1〜L-T10。One UI 7 での実際の長押し動作）。
- **変えないテスト**: バージョン固定のテスト群（`test_locale_tile_version_2_9_10`、`test_a11yoff002_version_2_9_10` など。本仕様ではバージョンを変えないため、そのまま通る）。タイルのマニフェストのテスト群（`test_a11yoff002_d6_tile_manifest`、`test_locale_tile_manifest_declares_service_once`。3つの TileService の宣言は変わらない）。SPEC-UI-003 のテスト群（`test_ui003_*`。タイルの状態の意味は変わらない）。ウィジェットのテスト群（変わらない）。
- フル・スイートが通ること。APK を再ビルドし、(2) の APK 検査を行う（バージョンは v2.9.10 / code 63 のまま）。

## 5. 実機確認（実機。L-T は本仕様の確認番号）

前提: パネルを開いたままの確認が多いため、実機確認はサイガマンの手で行う。長押しの挙動は Samsung One UI 7 で未確認であるため、L-T6 が本仕様の中核の確認である。

実機確認の記録規則: すべての実機確認（L-T1〜L-T10）の結果には、次を添えて記録する。端末モデル、Android バージョン、One UI バージョンとビルド番号、アプリバージョン、タイルの配置状態（3つのタイルがパネルに追加されているか、順序）、実施日時、長押しの結果、長押し後の QS パネルの開閉状態。OS 更新や System UI の変更で挙動が変わった場合に、後から比較できるようにするためである。

- L-T1（メインのタイルの長押し）: クイック設定を開き、Muse Bridge タイルを長押しする → MainActivity が開く。アプリ情報は開かない。タップ（短押し）の挙動は変わらない（サーバーの開始／停止）。
- L-T2（言語設定タイルの長押し。副作用）: 言語設定タイルを長押しする → MainActivity が開く（D2 の副作用）。タップの挙動は変わらない（言語設定が開く）。
- L-T3（補助機能オフタイルの長押し。副作用）: 「補助機能オフ」タイルを長押しする → MainActivity が開く（D2 の副作用）。タップの挙動は変わらない（補助機能の無効化）。
- L-T4（長押し後のパネルの挙動。観察項目）: 長押しで MainActivity が開いたあと、クイック設定パネルが開いたままか、閉じるかを記録する。公式リファレンスはこの点について述べていないため、仕様として断定しない。観察結果を記録する。
- L-T5（MainActivity が既に前面にある場合）: MainActivity を開いた状態で、ホームに戻らずにパネルを開き、タイルを長押しする → 新しい MainActivity のインスタンスが開くことが期待される（`launchMode` は既定値の `standard` であり、新しい intent ごとに、対象タスクに新しいインスタンスが作られる。公式の `<activity>` リファレンスによる）。戻るボタンを押すと元の MainActivity のインスタンスに戻るかどうかは、実機で確認する期待結果であり、仕様として断定しない。System UI からの起動がどのタスクに配置されるか、起動 intent にどのフラグが付くかは未確認であるためである。確認では、長押し後の Activity のインスタンス数、タスク、Back 操作の結果を記録する。
- L-T6（One UI 7 での確認）: Samsung One UI 7 のクイック設定パネルで、L-T1〜L-T3 がすべて同じ結果になることを確認する。One UI の長押し挙動は公式リファレンスに載っておらず、未確認である。
- L-T7（再起動と開き直し）: 端末を再起動し、パネルを開く → タイルの長押しで MainActivity が開く。タイルをクイック設定から外して再度追加しても、長押しで MainActivity が開く。intent-filter はアクティビティ側の宣言であり、タイルの付け外しに影響されない。
- L-T8（プロセスが死んでいる場合）: まずプロセスが死んでいることを確認してから試験する。手順: (1) 「設定 → アプリ → Muse Bridge → 強制停止」でプロセスを止める（主たる終了操作。プロセス終了の根拠はこの強制停止そのものである）。(2) 終了の確認は、ADB が使える場合は `adb shell pidof dev.muse.bridge` を優先的な確認方法とする（何も返らなければプロセス不在。ADB の shell UID は全プロセスが見える）。ADB が使えない場合は「設定 → 開発者向けオプション → 実行中のサービス」で確認するが、実行中のサービスの表示に加えてキャッシュプロセスの表示も確認対象に含める（Activity を保持したままのキャッシュプロセスは別表示になるため）。dev.muse.bridge が実行中のサービスの表示にもキャッシュプロセスの表示にも無ければ、「表示上、確認できない」と記録する。これはプロセス不存在の確証ではない。Termux の `pidof` は使わない。Android 10 以降、/proc は hidepid=2 で制限され、アプリは自分のプロセスしか見えないため、Termux（通常のアプリ UID）の `pidof dev.muse.bridge` は Bridge のプロセスが生きていても何も返らず、プロセス不在の証明にならない（端末は Android 15）。履歴からアプリを消すだけでは、プロセスが残っている場合があり、プロセス終了の証明にならない。そのうえでタイルを長押しする → MainActivity が開く（システムがプロセスを起動する）ことが期待される。タイル自体が表示されることと、長押しが MainActivity を開くことは、プロセスの生死に依存しない。
- L-T9（ロック中の長押し）: 端末をロックし、ロック画面からクイック設定パネルを開いてタイルを長押しする → 結果を記録する。MainActivity が開くか、ロック解除を求められるか、反応しないかは、仕様として断定しない。ロック中の挙動は未確認であるため、観察結果を記録する。
- L-T10（編集モードでの長押し）: クイック設定の編集モード（タイルの追加・移動の画面）でタイルを長押しする → 結果を記録する。編集モードでは長押しはタイルの移動操作になることが想定されるが、MainActivity が開くか、移動操作になるかは、仕様として断定しない。観察結果を記録する。

既知の実機の癖: 2026-11-09 08:49–08:59 に、QS タイルの移動・編集が反応しない現象があった。マニフェストは正しく、端末の再起動で直った（System UI の不具合）。実機確認中にタイルの編集が反応しなくても、アプリの不具合と決めつけず、まず端末の再起動を試すこと（SPEC-UI-003 §5 と同じ）。

## 6. 境界例

- MainActivity が既に前面にある場合: `launchMode` は既定値の `standard` であるため、長押しごとに、対象タスクに新しいインスタンスが作られる（公式の `<activity>` リファレンス。「`standard` のアクティビティは、新しい intent ごとに新しいインスタンスが作られる」）。戻るボタンで元のインスタンスに戻るかどうかは、タスク配置と intent のフラグが未確認であるため断定せず、L-T5 で実機確認する。インスタンスが重なること自体は、プラットフォームの既定の挙動であり、本仕様の不具合ではない。
- アプリのプロセスが死んでいる場合: システムがプロセスを起動して MainActivity を開く。長押しの解決はマニフェストの宣言によるものであり、プロセスが生きていることを前提としない（L-T8）。
- タイルをクイック設定から外した場合: intent-filter は `MainActivity` 側の宣言であり、タイルの有無に影響されない。再度追加すれば、長押しは再び MainActivity を開く（L-T7）。
- 端末がロックされている場合: ロック中の長押しの挙動は未確認である。L-T9 で実機確認し、結果を記録する。仕様として断定しない。
- `EXTRA_COMPONENT_NAME` が付いている場合: システムがどのタイルからの長押しかを示す値を付けることがあるが、`MainActivity` は起動 intent の内容を読まないため、挙動に影響しない（D2、§3）。
- QS 編集モードでの長押しの場合: 編集モードでは長押しはタイルの移動操作になることが想定される。MainActivity が開くかどうかは、L-T10 で実機確認し、結果を記録する。仕様として断定しない。

## 7. 対象外（やらないこと）

- タップ時の挙動の変更（3つのタイルのタップはすべて従来どおり）。
- タイルごとの長押し先の振り分け。本仕様の仕組み（マニフェストのみ、コード変更なし）ではできない。intent-filter は1つのアクティビティに宣言するものであり、システムはどのタイルの長押しでも同じアクティビティに解決する。タイルごとに開く先を変えるには、アクティビティが `EXTRA_COMPONENT_NAME` を読んで分岐するコード変更か、タイルごとの別アクティビティが必要であり、別の設計になる。本仕様では行わない。
- `launchMode` の変更（`singleTop` 化など）。長押しで新しいインスタンスが重なる挙動を変えるには `launchMode` の変更が必要だが、それはランチャーやウィジェットからの起動を含む MainActivity 全体の挙動を変えるため、本仕様の範囲外とする（§9 OP-03）。
- 専用の設定画面の追加。長押しの開き先は MainActivity そのものであり、タイル専用の設定画面は作らない。
- アイコンやラベルの変更。タイルの表示は SPEC-UI-003 v2 のとおりであり、本仕様では変えない。

## 8. 置き換えと維持（SPEC-UI-003 v2・SPEC-A11Y-OFF-002 v6・SPEC-LOCALE-TILE-001 v4 との関係）

- **置き換えるもの**: なし。新規の仕様である。既存の仕様の決定事項を変更しない。
- **SPEC-UI-003 v2 との関係**: SPEC-UI-003 v2 §7 は、長押し機能を「別途計画中の機能」として対象外にしていた。本仕様が、その計画中の機能の本体である。SPEC-UI-003 v2 の OP-01（対象バージョン v2.9.10、長押しと同梱）は、本仕様の対象バージョンと一致する。タイルの状態の意味（SPEC-UI-003 D2 の表）は、本仕様では変わらない。
- **維持するもの（SPEC-A11Y-OFF-002 v6）**: D1（入口は2つ）、D2（共通の実行器）、D7（実行器の意味）。「補助機能オフ」タイルのタップは引き続き無効化だけを行い、長押しで MainActivity が開くことは、タップの挙動に影響しない。
- **維持するもの（SPEC-LOCALE-TILE-001 v4）**: L2（タイルは起動専用、常時 INACTIVE）、L6（サブタイトルに現在の言語）。言語設定タイルのタップは引き続き言語設定を開き、長押しで MainActivity が開くことは、タップの挙動に影響しない。なお、長押し機能は 2026-11-09 03:37–03:38 のユーザー決定により、ロケール・タイルの修正とは分けて進める。
- **実機確認の実績の扱い**: 長押しでアプリ情報が開くことは、2026-11-09 04:12 +07 にユーザーが実機で確認した現行挙動として残る。本仕様の実機確認（L-T1〜L-T10）は、その挙動が MainActivity を開くことに変わったことの確認である。

## 9. 未決事項（オープン・ポイント）

- **OP-01 — 対象バージョン（解決済み。2026-11-10 00:29 +07 にユーザー決定）。** v2.9.10 / code 63、SPEC-UI-003 v2 と同梱する。本仕様ではバージョン変更をしない（D4）。
- **OP-02 — 3つのタイルの長押しがすべて MainActivity を開く副作用の受け入れ（解決済み。2026-11-10 01:18 +07 にユーザー決定）。** ユーザーは 2026-11-09 03:35 +07 の時点でこの仕組みを把握しており、採用時に L-T2・L-T3 の副作用を認めたうえで採用した。
- **OP-03 — `launchMode` の見直し（`singleTop` 化）。** 長押しで MainActivity の新しいインスタンスが重なる挙動（§6）を変えるには、`launchMode` を `singleTop` にする必要がある。ただし、それはランチャーやウィジェットからの起動を含む MainActivity 全体の挙動を変えるため、本仕様では決めない。別途検討する。
- **OP-04 — One UI 7 の未確認事項。** L-T6（One UI 7 での長押し）と L-T4（長押し後のパネルの開閉）、L-T9（ロック中の長押し）、L-T10（編集モードでの長押し）の挙動は、実機確認で初めて分かる。実機確認の結果が仕様の前提と食い違う場合は、仕様の改訂（v5）として扱う。
