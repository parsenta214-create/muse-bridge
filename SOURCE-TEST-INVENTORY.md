# ソース-テスト対応表（Source-Test Inventory）

SPEC-KOTLIN-MIGRATION-001 D6 で作る対応表。Python テストファイルと、
それが検査する Android ソース（Java / Kotlin）・build.sh・Manifest・
res の対応を記す。バッチごとに更新する。

最終確認: 2026-11-09、バッチA P2対応（Argonレビュー）
完了時。フルスイート: **460 passed、18 subtests**
（バッチA時の 458 件 ＋ P2対応新規 2 件）。

## テストファイル一覧（`~/workspace/computer-use-preview/` 直下）

| ファイル | `def test_` 数 | 検査対象 |
|---|---|---|
| `test_android_app_client.py` | 308 | 下記の Android ソース検査（`_java_source` / `_kotlin_source`）＋ VM 側クライアントの振る舞い（モックサーバー） |
| `test_bridge_protocol.py` | 88 | VM 側プロトコル検証（`bridge_protocol.py`）。Android ソースは検査しない |
| `test_android_bridge.py` | 8 | VM 側クライアント（`android_bridge.py`）。Android ソースは検査しない |
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
| `HttpBridgeServer.java` | 7 | P1-06 世代チェック、リクエストログ、サーバー設定 |
| `BridgeWidgetProvider.java` | 4 | ウィジェットボタン／PendingIntent |
| `MainActivity.java` | 3 | Drive 初期化フック、AppVersion 呼び出し（Kotlin パイロットで不変） |
| `BridgeWidgetActionReceiver.java` | 2 | ウィジェットアクション分岐 |
| `BridgeLog.java` | 2 | ログ初期化・PII 除去 |
| `BridgeAccessibilityService.java` | 2 | P1-01 collectNodes recycle |
| `UriPermissionRegistry.java` | 1 | 権限レジストリ |
| `LogActivity.java` | 1 | Drive 初期化フック、AppVersion 呼び出し（不変） |
| `LocaleTileService.java` | 1 | ロケールタイル起動・帰属行 |
| `BridgeService.java` | 1 | サービス起動 |
| `BootReceiver.kt`（バッチAで Java から移行） | 2 | Drive 初期化フック、Kotlin 化確認・帰属行 |
| `VpnBindSelector.kt`（新規・バッチA） | 5 | Kotlin 化確認・相互運用注釈（`@JvmStatic` / `@JvmField` / `const val`）、Java 呼び出し元不変、帰属行、P2対応（Regex定数化・null要素防御）（※移行前はソース検査ゼロだった） |
| `A11yOffTileService.java` | 1 | QS タイル状態・更新 |
| `A11yOffExecutor.java` | 1 | 実行部ログ・完了トリガー種別 |
| `InteropProbeJava.java`（新規・パイロット） | 2 | 双方向フィクスチャ（Java→Kotlin）、帰属行 |
| `AppVersion.kt`（新規・パイロット） | 2 | Kotlin 化確認、スレッドセーフ注釈（`@Volatile` / `@Synchronized` / `@JvmStatic`） |
| `InteropProbe.kt`（新規・パイロット） | 1 | 双方向フィクスチャ（Kotlin→Java）、帰属行 |

`build.sh` の検査（バージョン・パイプライン）:

| 検査 | テスト |
|---|---|
| `VERSION_NAME="2.9.9"` / `VERSION_CODE=62`（バッチA） | `test_locale_tile_version_2_9_9`、`test_a11yoff002_version_2_9_9`（パイロット時代の同名テストから改名・更新）、`test_kotlin_pilot_build_sh_two_compiler_pipeline`（バージョン断言のみ更新） |
| kotlinc ステップ（`-jvm-target 17`、256m、2 コンパイラ・パイプライン）、javac の `-cp` に `$KOTLIN_STDLIB`、d8 に stdlib を 1 個だけ | `test_kotlin_pilot_build_sh_two_compiler_pipeline` |

## 運用ルール（D6）

- 更新するのは、ソース形式の変更の影響を受けるテストだけ。実行するのは毎バッチ、全スイート。
- 既存の振る舞いアサーションとカバレッジは保つ。言語が変わったからといってテストを削除しない。
- Java ソースの `public static final` を探すテストは、同等の Kotlin 宣言と生成される JVM の振る舞いを認識するよう書き換える。JVM レベルの性質（volatile、synchronized、static メンバ、シグネチャ）は `javap -v` で実ビルド出力を確認する（T-M1）。ソース検査はその注釈が残っていることのガードである。
- この表はバッチごとに更新し、移行を見通せるようにする。
