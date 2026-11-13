# ソース-テスト対応表（Source-Test Inventory）

SPEC-KOTLIN-MIGRATION-001 D6 で作る対応表。Python テストファイルと、
それが検査する Android ソース（Java / Kotlin）・build.sh・Manifest・
res の対応を記す。バッチごとに更新する。

最終確認: 2026-11-13、C-01-4 P0修正時。フルスイート: **532 passed、18 subtests**。
（test_android_app_client.py は 349 件）
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

## テストファイル一覧（`~/workspace/computer-use-preview/` 直下）

| ファイル | `def test_` 数 | 検査対象 |
|---|---|---|
| `test_android_app_client.py` | 349 | 下記の Android ソース検査（`_java_source` / `_kotlin_source`）＋ VM 側クライアントの振る舞い（モックサーバー） |
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
| `HttpBridgeServer.java` | 11 | P1-06 世代チェック、リクエストログ（KNOWN_PATHS ガード：C-01 で管理エンドポイント 5 つを追加）、サーバー設定 |
| `BridgeWidgetProvider.java` | 4 | ウィジェットボタン／PendingIntent |
| `MainActivity.java` | 3 | Drive 初期化フック、AppVersion 呼び出し（Kotlin パイロットで不変） |
| `BridgeWidgetActionReceiver.java` | 2 | ウィジェットアクション分岐 |
| `BridgeLog.kt`（バッチGで Java から移行） | 5 | ログ初期化・PII 除去（T-33/T-36は .kt 読取へ更新） |
| `BatchRegistry.kt`（新規・C-01） | 10 | 永続レジストリ。ソース検査は KNOWN_PATHS ガードとモックの batch 振る舞い（再送禁止・UNKNOWN）。JVM レベルの単一ロックは `javap -c` で確認（monitorenter が lock に集約）。CONS-152修正でソース検査 7 件を追加（P0-01 tombstone 再計算＋admit 順序、P0-02 CORRUPT ガード、P1-01 読み取り不能、P1-04 supersedes 確認必須、P2-01 512KB 上限）。CONS-153修正でソース検査 1 件を追加（P1-01 不存在の積極確認：Files.notExists／undeterminable は CORRUPT／NoSuchFileException 競合は null）。CONS-156修正でソース検査 2 件を追加（P2-1 IN_PROGRESS の payloadHash 照合、P2-3 confirm の 64KB 上限）。javap 再確認（c01-fix3-BatchRegistry.txt）：BatchMismatchException 参照 6 箇所（投げ 3 箇所）、monitorenter 8 箇所のまま |
| `BridgeAccessibilityService.java` | 3 | P1-01 collectNodes recycle。CONS-152修正で `batchRegistry` の volatile 化（P2-02）をソース検査＋javap で確認 |
| `UriPermissionRegistry.kt` | 1 | 権限レジストリ（バッチDで Java から移行） |
| `PingProber.kt`（バッチEで Java から移行） | 3 | ping 往復測定（※移行前はソース検査ゼロだった） |
| `BridgeNotificationListener.kt`（バッチFで Java から移行） | 3 | 通知イベント流（※移行前はソース検査ゼロだった） |
| `LogActivity.java` | 1 | Drive 初期化フック、AppVersion 呼び出し（不変） |
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
| `VERSION_NAME="2.9.19"` / `VERSION_CODE=72`（CONS-153修正） | `test_locale_tile_version_2_9_19`、`test_a11yoff002_version_2_9_19`、`test_kotlin_pilot_build_sh_two_compiler_pipeline`（バージョン断言のみ更新） |
| kotlinc ステップ（`-jvm-target 17`、256m、2 コンパイラ・パイプライン）、javac の `-cp` に `$KOTLIN_STDLIB`、d8 に stdlib を 1 個だけ | `test_kotlin_pilot_build_sh_two_compiler_pipeline` |

## 運用ルール（D6）

- 更新するのは、ソース形式の変更の影響を受けるテストだけ。実行するのは毎バッチ、全スイート。
- 既存の振る舞いアサーションとカバレッジは保つ。言語が変わったからといってテストを削除しない。
- Java ソースの `public static final` を探すテストは、同等の Kotlin 宣言と生成される JVM の振る舞いを認識するよう書き換える。JVM レベルの性質（volatile、synchronized、static メンバ、シグネチャ）は `javap -v` で実ビルド出力を確認する（T-M1）。ソース検査はその注釈が残っていることのガードである。
- この表はバッチごとに更新し、移行を見通せるようにする。
