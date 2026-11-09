# SPEC-KOTLIN-MIGRATION-001: Android アプリの Java → Kotlin 移行

- **版**: v2.2 ADOPTED AND FROZEN（採用・凍結 2026-11-09 18:25 +07）
**対象**: `android-app/`（Muse Bridge Android アプリ）
**前提**: GitHub Actions ビルド証明済み（2026-11-09 02:14、run 42513881471）。Java 側の残り作業は完了（LOCALE-TILE v2.9.4、A11Y-OFF-002 v2.9.7）。L6 再試行と L7/L8 は Kotlin 化のあとに延期済み（2026-11-09 09:38、サイガマン決定）。
**方針出典**: repo AGENTS.md「Kotlin 移行の方針（2026-11-08 決定）」— Android 側（`android-app/`）だけ Kotlin にする。VM 側 Python と上流の Python 樹は対象外。時期は v2.9.0 の後で、GitHub Actions のビルド証明が先。移行の細則（試すファイル、巨大小ファイルの扱い、Kotlin 標準ライブラリの依存ゼロ例外）は移行 SPEC で決める。
**レビュー履歴**: CONS-111（GPT、2026-11-09 10:00、新チャット https://chatgpt.com/c/6af136f5-90e0-83ec-a070-ca22bf2da461 ）— v1 は **CONDITIONAL、7.2/10、P0=1、P1=9、P2=4**。v2 は全指摘を反映。v2 再レビュー（同チャット、2026-11-09 10:04）は **CONDITIONAL、8.6/10、P0=1（新規）、P1=0、P2=3**（v2 の D2 に新たな技術的誤り—kotlinc は Java をコンパイルしない）。v2.1 はその誤りを正す狭い範囲の改訂。v2.1 焦点再レビュー（同チャット、2026-11-09 10:07）は **ADOPTABLE、9.5/10、P0=0、P1=0、P2=2**。本 v2.2 は残る P2 2 件を反映した最終版。変更点の対応表は末尾の付録を参照。

## §1 現状（2026-11-09 10:00 時点）

- Java ソース 18 ファイル、計 10,251 行（生成 R.java 209 行を除く）。Kotlin ファイルは 0。
- 内訳（行数昇順）: BootReceiver 42、AppVersion 56、BridgeTileService 78、A11yOffTileService 117、LocaleTileService 118、VpnBindSelector 124、BridgeService 126、BridgeWidgetActionReceiver 138、StatusDialView 152、BridgeWidgetProvider 161、UriPermissionRegistry 275、PingProber 297、A11yOffExecutor 398、BridgeNotificationListener 424、BridgeLog 461、MainActivity 705、LogActivity 1263、BridgeAccessibilityService 1753、HttpBridgeServer 3563。
- ビルド: 手動 aapt2 / javac / d8 パイプライン（build.sh）。Gradle なし（VM 空き RAM 約 458 MB で Gradle デーモンが入らない）。JDK 17、Android SDK 34、targetSdk 34、minSdk 30。javac -J-Xmx256m、d8 -JXmx384m。APK v2.9.7/code 60、約 124 KB。
- Python テスト: 448 passed、18 subtests。ソースのパターンを検査するテストが多数あり、Java ソースを前提としている。
- 試行済み: AppVersion.kt の試作ドラフトあり（`~/workspace/research/kotlin-trial/AppVersion.kt`、Qwen3-Coder-Next 80B 生成、2026-11-08）。**未採用・未コンパイル検証**。指摘事項: `volatile` が落ちている（Kotlin では `@Volatile` が必要）。`load()` が `@Synchronized` のため影響は小さい。
- kotlinc は未導入（PATH になし）。Kotlin 標準ライブラリも未導入。
- Kotlin バージョン確認済み（2026-11-09、GitHub API）: 最新リリースは **v2.4.21**（2026-10-08 公開）。v1 で「LTS」と記した 2.0.21 は 2024-10-10 の旧版であり、LTS の指定は誤りだった（CONS-111 P1-02）。

## §2 設計決定

### D1 範囲
- 対象は `android-app/` の Java ソース 18 ファイルのみ。`res/`、AndroidManifest.xml、build.sh（Kotlin 対応のための変更を除く）、Python テスト基盤、VM 側 Python、上流 Python 樹は対象外。
- 依存ゼロ方針の例外: **Kotlin 標準ライブラリ（kotlin-stdlib）のみ**を新たな依存として認める。Kotlin 標準ライブラリ以外のサードパーティライブラリは追加しない（従来のゼロ依存方針を維持）。kotlin-reflect は含めない（D2 参照）。

### D2 ツールチェーン
- **コンパイラ**: kotlinc スタンドアロン・コンパイラ（JetBrains 公式リリース）。**バージョン 2.4.21**（最新安定版、2026-10-08 公開、GitHub API で確認済み）。「LTS」の指定はしない（2.0.21 を LTS とした v1 の記述は誤りだった）。コンパイラと標準ライブラリは同じバージョン（2.4.21）で固定する。**アップグレード方針**: 新しい Kotlin リリースは四半期ごとに評価し、上げる場合は SPEC の改訂＋パイロットの再検証（再ビルド＋フルテスト）を行う。無断で上げない。
- **コンパイラとランタイムの分離**: kotlinc はビルド時依存（VM と GitHub Actions のビルド環境）。kotlin-stdlib は実行時依存（Android APK に同梱）。kotlin-reflect は不要であり、APK に含めない。コンパイラ配布物そのものを APK に入れない。
- **JVM バイトコード・ターゲット**: kotlinc は `-jvm-target 17` を明示する（デフォルトの 1.8 にはしない）。現行の javac は JDK 17 のデフォルトでクラスファイル 61（Java 17）を出しており、d8 が処理できている（実績あり）。両者を 17 で統一し、コンパイラのデフォルト任せにしない。
- **混合コンパイル手順（P0-01 対応、v2.1 で訂正）**: kotlinc は Kotlin ソースを JVM クラスファイルにコンパイルする。Java ソースを一緒に入力として渡すと、Kotlin コードが Java の宣言を参照するための**シンボル解決**には使われるが、**Java のクラスファイルは生成しない**。Java ソースは javac で別途コンパイルする必要がある（v2 の「kotlinc の 1 回の呼び出しで両方をコンパイルする」という記述は技術的に誤りだった。CONS-111 v2 再レビューで指摘された）。正しい手順は 2 コンパイラ・パイプラインである。
  1. aapt2 compile/link（従来通り）。生成される `R.java` は `build/gen` に出る。
  2. **ステップ 1 — Kotlin コンパイル（kotlinc）**: 入力は全 `.kt` ソース、シンボル解決に必要な Java ソース、Android SDK の android.jar、必要な生成 `R.java`。出力は Kotlin の `.class` ファイル（`$BUILD/classes`）。コマンド: `kotlinc -jvm-target 17 -cp "$ANDROID_JAR" -d "$BUILD/classes" $(find "$HERE/src" "$BUILD/gen" -name '*.kt') $(find "$HERE/src" "$BUILD/gen" -name '*.java')`。ここで `.java` はシンボル解決のためだけに渡す。Kotlin のクラスだけが生成される。kotlin-stdlib は kotlinc がコンパイル用クラスパスに自動で載せる（`-include-runtime` は使わない。stdlib を出力に束ねないため）。
  3. **ステップ 2 — Java コンパイル（javac）**: 入力は全 `.java` ソース（生成 `R.java` を含む）、Android SDK の android.jar、Kotlin 生成物の出力（`$BUILD/classes` を `-cp` に入れる）、Kotlin 標準ライブラリ（`$KOTLIN_STDLIB` を `-cp` に入れる。P2-01 対応。Kotlin の生成クラスは stdlib に属する型・アノテーションを参照する場合があるため、javac のクラスパスに stdlib を明示する）、必要なコンパイル時依存。出力は Java の `.class` ファイル（同じ `$BUILD/classes` ディレクトリ）。コマンド: `"$JAVA_HOME/bin/javac" -J-Xmx256m -encoding UTF-8 -source 17 -target 17 -cp "$ANDROID_JAR:$BUILD/classes:$KOTLIN_STDLIB" -d "$BUILD/classes" $(find "$HERE/src" "$BUILD/gen" -name '*.java')`。`$KOTLIN_STDLIB` は kotlinc 配布物の `lib/kotlin-stdlib.jar`（例: `~/workspace/tools/kotlin/kotlinc/lib/kotlin-stdlib.jar`、バージョン 2.4.21）。
  4. **ステップ 3 — パッケージング**: `jar` で `$BUILD/classes`（Kotlin＋Java の両方のクラス）を `$BUILD/classes.jar` にまとめる。d8 には `$BUILD/classes.jar` と `kotlin-stdlib.jar` の両方を渡す（D2 標準ライブラリ項を参照）。
  5. **ステップ 4 — APK**: 既存の zipalign と apksigner の手順は変更なし。
  6. この構成は循環するソースレベル参照を支える。Kotlin は kotlinc 時に Java の宣言を参照する（Java ソースをシンボル解決に使う）。Java は javac 時に生成された Kotlin クラスを参照する（Kotlin 出力を `-cp` に入れる）。
  7. **検証**: パイロット（D3）の双方向相互運用フィクスチャが、**両方のクラスファイルが実際に生成されたこと**を確認する。コンパイラが参照を受け入れただけでは不十分。出力ディレクトリに Kotlin と Java の両方の `.class` が存在することを確認する（例: `InteropProbe.class` と `InteropProbeJava.class` の存在確認。T-M1 の javap 検査も参照）。
- **メモリ（P1-01 対応）**: kotlinc のヒープは **-J-Xmx256m** から始める。`-J-Xmx` は Java ヒープの上限であり、プロセス全体のメモリではない（metaspace、JIT、スレッドスタック、ネイティブ割当が別に要る）。そのため **ピーク RSS（実メモリ）を測定して記録する**（`/usr/bin/time -v` または同等）。スワップ使用量とシステムのメモリ逼迫も記録する。**384m への自動エスカレーションはしない**。256m で OOM するか、RSS が危険域に入る場合、**GitHub Actions を正式なビルド環境とする**（明確な受入規則: ローカルビルドはベストエフォートであり、Actions のビルドが通らなければバッチは採用しない）。パイロットで実測値を記録し、結果を SPEC に追記する。
- **標準ライブラリのパッケージング（P1-08 対応）**: 同梱する kotlin-stdlib.jar は、kotlinc 配布物の `lib/kotlin-stdlib.jar`（バージョン 2.4.21、コンパイラと同一）。kotlinc がコンパイル時に自動で解決する。APK への組込みは、d8 の入力として `$BUILD/classes.jar` と `kotlin-stdlib.jar` の両方を渡すことで行う。**stdlib は 1 個だけ**（重複を入れない）。kotlin-reflect は含めない。コンパイラ配布物そのものは入れない。APK サイズの増加見込み（約 1 MB）は仮説として扱い、T-M5 で実測する。
- **GitHub Actions の再現性（P1-09 対応）**: ワークフローに kotlinc 取得ステップを追加する。**バージョン 2.4.21 を固定**。**ダウンロード URL を明示**（GitHub Releases の kotlin-compiler-2.4.21.zip）。**SHA-256 チェックサム検証**を行い、不一致ならビルドを失敗させる。**期待する SHA-256 はバージョン管理された値として固定する（P2-02 対応）**: `7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd`。この値は公式リリースの `kotlin-compiler-2.4.21.zip.sha256` サイドカーファイル（GitHub Releases、2026-11-09 確認）から取得したもので、SPEC とワークフロー（および `~/workspace/tools/kotlin/SHA256`）に記録する。ダウンロードした zip と未検証のチェックサムを同じ取得元から取って照合するだけでは、独立した検証にならない。そのため、期待値はリポジトリ側でバージョン管理し、取得元のサイドカーファイルと突き合わせてから固定する。JDK は 17 で統一する。キャッシュはコンパイラバージョンをキーにする（actions/cache）。ダウンロードしたバージョンが期待値と違う場合は失敗させる。ローカル VM と GitHub Actions が無言で違う Kotlin バージョンを使うことがないようにし、両環境で `kotlinc -version` の出力をビルドログに記録する。

### D3 変換順序（依存関係を優先し、ファイルサイズは副次）
- **第 0 段階（パイロット）: AppVersion.java（56 行）＋双方向相互運用フィクスチャ**。AppVersion はツールチェーンの基本（コンパイラ取得、ビルド統合、Kotlin クラス生成、stdlib 同梱、d8、署名、インストール）を検証する。加えて、**双方向相互運用フィクスチャ**を設ける。フィクスチャは小さな 2 ファイル（`InteropProbe.kt` と `InteropProbe.java`）で、互いを呼び合う（Kotlin→Java、Java→Kotlin）。検証項目: Java から Kotlin クラスを呼ぶこと。Kotlin から既存 Java クラスを呼ぶこと。Android フレームワーク型を使うこと。`@Volatile`。`@Synchronized`。Java から見える static メンバ。フィクスチャはアプリの実行時コードからは呼ばれない（ inert ）。コンパイル時の相互運用リグレッション検査としてリポジトリに残す（小さく、APK への影響は無視できる）。パイロットは「APK が生成・インストールできた」だけでは合格としない。フィクスチャが両方向を実証して初めて合格とする。
- **第 1 段階（小・独立）**: BootReceiver、BridgeTileService、LocaleTileService、VpnBindSelector、BridgeService、BridgeWidgetActionReceiver、StatusDialView、BridgeWidgetProvider。依存の少ないものから順に。LocaleTileService は独立している（実行部への依存なし）。
- **第 2 段階（中・実行部を先に）**: **A11yOffExecutor を先に**変換し、そのあと **A11yOffTileService**（実行部に依存するため）。続いて UriPermissionRegistry、PingProber、BridgeNotificationListener、BridgeLog。（v1 の矛盾—第 1 段階にタイル、第 2 段階に実行部—を解消。依存関係をファイルサイズより優先する。）
- **第 3 段階（大）**: MainActivity、LogActivity、BridgeAccessibilityService、HttpBridgeServer。HttpBridgeServer（3,563 行）は最大のファイルで、1 ファイルとしての変換が重い場合は、変換前に分割しない（分割は別 SPEC の対象）。1 ファイルのまま変換する。変換が難しい部分は、Kotlin 化を諦めて Java のまま残す選択肢も認める（D5）。
- **バッチサイズの指針（P2-03 対応）**: 1 バッチは **1〜3 ファイル**（密接に関連するもの）。可能な限り 1 つの機能サブシステムを 1 バッチにする。段階の全ファイルを 1 バッチで変換する必要はない。指針であり、厳格な上限ではない。
- 順序は目安であり、依存関係により前後してよい。ただしパイロット（AppVersion＋フィクスチャ）が最初であることは固定。

### D4 相互運用（混在期間のルール）
- 移行期間中は Java と Kotlin が混在する。Java から Kotlin を呼ぶ場合のアノテーションは、目的別に使い分ける。
  - `@JvmStatic`: companion/object の関数を Java の static メソッドとして出す。
  - `@JvmField`: プロパティを Java のフィールドとして出す（通常は getter/setter になるため、フィールドとして出す場合にのみ付ける）。
  - `@JvmOverloads`: Kotlin のデフォルト引数に対するオーバーロードを生成する。
  - `@Throws`: Java から見えるチェック例外を宣言する。
  - `@file:JvmName`: トップレベル宣言の Java クラス名を制御する。
  - `@Volatile`: Java の volatile フィールドの意味を保つ。**可視性（visibility）の保証であり、原子性（atomicity）ではない**。インクリメントのような複合操作をスレッドセーフにはしない。
  - `@Synchronized`: Java の synchronized メソッドに対応する。
  - 通常の Kotlin インスタンスメソッドに `@JvmStatic` は要らない。Kotlin のプロパティは通常 getter/setter になる。
- **守ること**: 公開 Java メソッドのシグネチャを保つ。static とインスタンスの区別を保つ。フィールドの可視性と可変性を保つ。nullable と non-null（Kotlin の `?` 型）を明確にし、Java 側の `@Nullable` / `@NonNull` と対応させる。Java 呼び出し側が依存するチェック例外の契約を保つ。internal、coroutine、その他 Kotlin 固有の実装詳細を Java 向け API から出さない。生成された Kotlin クラスに対して Java のコンパイルを通して確認する。
- **スレッドセーフ保持チェックリスト（変換時の必須確認）**: volatile → `@Volatile`。synchronized メソッド → `@Synchronized`。synchronized (lock) → `synchronized(lock)`。static final 定数 → 用途に応じて `const val` または `@JvmField`。AtomicInteger → 既存の Java 実装を保つ。Thread / Handler → 既存の実行モデルを保つ。Kotlin に代替構文があるからといって同期機構を置き換えない。
- `@Volatile` の規則は D5 の例外条項から本チェックリストに移した（必須）。

### D5 変換できない場合の扱い
- 1 ファイルの変換が著しく困難な場合（Kotlin の制約で等価な書き方がない、変換後の動作が検証できない等）、そのファイルは **Java のまま残す**ことを認める。残す場合は理由を SPEC に記録する。全部を Kotlin にすることが目的ではなく、無理のない範囲で Kotlin 化することが目的。
- `@Volatile` の扱いは D4 のチェックリストに移した（必須）。ここでは繰り返さない。

### D6 テスト
- **更新するのは、ソース形式の変更の影響を受けるテストだけ**（バッチごと）。448 件すべてを毎バッチ書き換えるのではない。**実行するのは、毎バッチ、全 448 件＋新規テスト**（フルスイートをグリーンに保つ）。既存の振る舞いアサーションとカバレッジは保つ。言語が変わったからといってテストを削除しない。たとえば Java ソースの `public static final` を探すテストは、同等の Kotlin 宣言と生成される JVM の振る舞いを認識するよう書き換える。必要に応じて Kotlin 固有のソース検査と相互運用検査を追加する。
- **ソース-テスト対応表**を作る。Python テストファイルと、それが検査する Java クラス／ソースパターンの対応を記した一覧。バッチごとに更新し、移行を見通せるようにする。
- 各バッチの完了条件: フルテストがグリーン（448 件＋追加分）であること。ビルド（build.sh）が成功し、APK が署名付きで生成されること。

### D7 バージョニングとロールアウト
- バッチごとに新しいバージョンを付ける。パイロット（AppVersion）が v2.9.8/code 61。以後のバッチは code を **1 ずつ単調に増やす**（62、63、…）。code の再利用はしない。versionName は v2.9.9、v2.9.10、…と進める。
- 各バッチの APK は従来通り Google Drive（書斎/設備管理）で配布し、ユーザーがインストールしてスモークテストする（アプリ起動、/health、ウィジェット/タイルの基本動作）。フルライブテストは最終バッチのあとにまとめて行う。
- **ロールバック（P1-07 対応）**: Android は versionCode の低い APK へのダウングレードを通常拒否するため、前の APK をそのまま再インストールするだけでは失敗する。ロールバック手順は次の通り。直前のソースリビジョンをチェックアウトし、**versionCode を（問題のあるバッチの code ＋1）に上げて再ビルド**し、配布する。versionName はそのままでよい（code が判断基準）。署名鍵は同一、パッケージ ID は同一とする。リリースした APK はすべて、対応するソースコミットと一緒に保管する。ロールバック手順は、採用前に 1 回テストする（手順が動くことを確認する）。
  - **ロールバックは versionCode を消費する（P2-01 対応）**: ロールバックで使った code は、その後のバッチが再利用しない。以後のバッチは新しいグローバル・カウンタから続ける。例: パイロット 61、バッチ A 62、A のロールバック 63、バッチ B 64。こうしないと、計画上の連番がロールバックビルドと衝突する。
- ロールバック時: 変換したファイルに問題が見つかった場合、そのバッチの APK を差し戻す（上記の手順で）。Git のコミットはバッチごとに分ける。

## §3 変更ファイル（予定）
- 新規: `SPEC-KOTLIN-MIGRATION-001.md`（本 SPEC）。`~/workspace/tools/kotlin/`（kotlinc 設置）。`InteropProbe.kt` / `InteropProbe.java`（パイロットの相互運用フィクスチャ）。
- 変更: `build.sh`（kotlinc ステップ追加、バージョン更新、-jvm-target 17、d8 への stdlib 追加）。`.github/workflows/build.yml`（kotlinc 取得ステップ追加、SHA-256 検証、JDK 17）。`src/dev/muse/bridge/*.java` → `*.kt`（段階的に）。`tests/*.py`（各バッチで影響を受ける箇所だけ更新）。ソース-テスト対応表（新規ドキュメント）。
- 削除: 変換済みの `.java` ファイル（`.kt` に置き換え）。

## §4 テスト計画
- **T-M1**: パイロット（AppVersion＋フィクスチャ）で、kotlinc 取得から APK 署名・インストールまで端から端まで通ること。/health と /state でバージョンが正しく報告されること。フィクスチャが両方向の相互運用を実証すること。`@Volatile` / `@Synchronized` / static メンバが正しく変換されること。**生成された JVM プロパティの検査（P2-03 対応）**: コンパイルが成功しただけでは JVM の意味が正しいことの証明にならない。生成されたクラスファイルを `javap -v`（または同等の自動検査）で確認し、次を検証する。volatile フィールド修飾子。synchronized メソッド修飾子。期待される Java から見える static メンバ。期待されるメソッドシグネチャ。両方向のクラスファイルが実際に生成されたこと（出力ディレクトリの `.class` 存在確認）も含む。
- **T-M2**: 各バッチで、フル Python テストがグリーンであること（448 件＋新規分）。
- **T-M3**: 各バッチで、APK がビルドでき、ユーザーのスモークテスト（起動、/health、ウィジェット/タイル）が通ること。
- **T-M4**: 最終バッチのあと、既存のライブテスト（W/T/X/L4/L5/L6/L7/L8 のうち実施済みのもの）を再確認すること。L6 再試行と L7/L8 はこのタイミングで実施する（2026-11-09 09:38 の決定）。
- **T-M5**: APK サイズの実測（各バッチで、署名済み APK の絶対サイズと、124 KB 基準に対する増加率の両方を記録する）。stdlib による増加が想定内であることを確認する。
- **T-M6**: ビルド時間の計測（新規）。Java ベースラインのビルド時間、Kotlin ビルド時間、CI 全体のビルド時間、コンパイラ実行時間を記録する。許容できる増加と、病的なコンパイラ設定を区別するため。目安: Kotlin ビルドが Java ベースラインの 2 倍を超える場合は原因を調べる。
- **T-M7**: リグレッション保護（新規）。移行でテストのアサーションを弱めたり、既存のカバレッジを除去したりしない。大きなファイル（特に HttpBridgeServer）は、変換前後で外部から観測できる振る舞いを比較する。

## §5 リスク
- **R1 メモリ**: kotlinc が VM の空き RAM（約 458 MB）で OOM する可能性。対策: D2 の 256m 開始＋ピーク RSS 測定。384m への自動エスカレーションはしない。ローカルで安全にコンパイルできない場合は GitHub Actions を正式なビルド環境とする（明確な受入規則）。
- **R2 APK サイズ**: kotlin-stdlib で約 1 MB 増加（仮説）。124 KB 基準に対する増加率は約 8 倍であり、絶対値だけでなく割合も記録する（T-M5）。サイドロード用デバッグ APK として許容する。R8 は導入しない（複雑化を避ける）。
- **R3 ビルド時間**: kotlinc は javac より遅い。ローカルビルド時間が延びる。対策: T-M6 で計測し、目安（Java ベースラインの 2 倍）を超える場合は原因を調べる。
- **R4 変換ミス**: 大ファイル（HttpBridgeServer 3,563 行、BridgeAccessibilityService 1,753 行）での変換ミス。対策: D5（困難な場合は Java のまま残す）、バッチごとのテスト・スモークテスト、T-M7 のリグレッション保護。
- **R5 テストの陳腐化**: Java 前提のテストが、変換に追従できずに残る可能性。対策: D6（影響を受けるテストだけ更新、フルスイート実行、ソース-テスト対応表）。
- **R6 混合コンパイル失敗（新規）**: kotlinc の 1 回呼び出しで Java/Kotlin の混合コンパイルが通らない可能性。対策: D2 の手順定義とパイロットのフィクスチャによる実証。通らない場合はコンパイル手順を調査・修正し、解決できなければ D5 に従って該当ファイルを Java のまま残す。（v2.1 では javac 併用はフォールバックではなく通常の必須工程であるため、「D2 のフォールバック（javac 併用）」という記述は使わない。P2-02 対応。）
- **R7 JVM API・振る舞いの非互換（新規）**: Kotlin 変換で JVM レベルの API や振る舞い（null 許容、例外、同期）が変わる可能性。対策: D4 の相互運用チェックリストとスレッドセーフ保持チェックリスト、フルテスト。
- **R8 ロールバック失敗（新規）**: Android の versionCode 制約でロールバックができない可能性。対策: D7 の再ビルド＋code 引き上げ手順。採用前に 1 回テストする。
- **R9 コンパイラ・依存の再現性（新規）**: ローカルと CI で違う Kotlin バージョンを使う可能性。対策: D2 の固定＋SHA-256 検証＋バージョン記録。
- **R10 標準ライブラリのパッケージングと実行時影響（新規）**: stdlib の重複、reflect の混入、実行時の予期せぬ影響。対策: D2 のパッケージング契約（1 個だけ、reflect 除外）、T-M5 の実測。

## §6 対象外
- VM 側 Python、上流 Python 樹の Kotlin 化（対象外）。
- Gradle の導入（しない。メモリ制約のため）。
- R8/ProGuard の導入（しない）。
- coroutines への書き換え（しない。D4）。
- HttpBridgeServer の分割（しない。D3）。
- ログ用キャプチャー動画機能（別候補。Kotlin 化のあとの検討対象）。
- **コンパイラとランタイムの分離**: kotlinc はビルド時依存、kotlin-stdlib は実行時依存、kotlin-reflect は除外。コンパイラ配布物を APK に入れない（D2）。

## 付録: CONS-111 指摘 → v2 対応表
- P0-01（混合コンパイルが未定義）→ D2 に正確な手順を追加（kotlinc 1 回呼び出し、クラスパス、ソース、出力、引数）。パイロットのフィクスチャで実証する。
- P1-01（メモリが楽観的）→ D2 を改訂（256m 開始、ピーク RSS 測定、384m 自動エスカレーションなし、Actions を正式環境とする受入規則）。
- P1-02（2.0.21 の LTS 指定が誤り）→ 2.4.21 に変更（最新、2026-10-08、GitHub API 確認済み）。LTS 指定を削除。コンパイラ＋stdlib を同バージョンで固定。アップグレード方針を追加。
- P1-03（パイロットが不十分）→ D3 に双方向相互運用フィクスチャを追加。検証項目を明示。
- P1-04（変換順序の矛盾）→ D3 を改訂（実行部をタイルより先に。依存関係をファイルサイズより優先）。
- P1-05（相互運用ルールが不完全）→ D4 を拡充（@JvmOverloads、@Throws、@file:JvmName、nullable、例外、static/instance、フィールド可視性）。@Volatile を主チェックリストに移し、原子性ではないことを明記。スレッドセーフ保持チェックリストを追加。
- P1-06（テスト移行が非現実的）→ D6 を改訂（影響を受けるテストだけ更新、全 448 件を実行、カバレッジ保持、ソース-テスト対応表）。
- P1-07（ロールバックが不完全）→ D7 を改訂（再ビルド＋code 引き上げ手順、単調 code、同一署名・パッケージ、APK 保管、採用前のテスト）。
- P1-08（stdlib パッケージングが不明確）→ D2 に契約を追加（どの jar か、d8 への渡し方、重複防止、reflect 除外、実測）。
- P1-09（Actions の再現性）→ D2 に追加（固定、URL、SHA-256、JDK 17、キャッシュ、バージョン不一致で失敗）。
- P2-01（ビルド時間の閾値なし）→ T-M6 を追加（計測と目安 2 倍）。
- P2-02（APK サイズの相対評価）→ T-M5 を改訂（絶対＋割合の両方を記録）。
- P2-03（バッチサイズ指針）→ D3 に指針を追加（1〜3 ファイル）。
- P2-04（リグレッション保護）→ T-M7 を追加（アサーション弱体化の禁止、大ファイルの振る舞い比較）。
- 追加要求 A（JVM ターゲット）→ D2 に `-jvm-target 17` を明示。
- 追加要求 B（スレッドセーフ）→ D4 にチェックリストを追加。
- 追加要求 C（コンパイラとランタイムの分離）→ D2 と §6 に明記。
- リスク R6〜R10 → §5 に追加。

## 付録 2: CONS-111 v2 再レビュー指摘 → v2.1 対応表
- v2 再レビュー（2026-11-09 10:04、同チャット）: **CONDITIONAL、8.6/10、P0=1（新規）、P1=0、P2=3**。v1 の指摘はすべて解決済みとされた。残るのは v2 の D2 混合コンパイル手順の新たな技術的誤りのみ。
- P0-01 新規（D2）: kotlinc は Java ソースをコンパイルしない（シンボル解決にのみ使う）。v2 の「1 回の呼び出しで両方をコンパイルする」は誤り。→ D2 を 2 コンパイラ・パイプラインに訂正（ステップ 1: kotlinc で Kotlin をコンパイル、Java はシンボル解決のみ。ステップ 2: javac で全 Java をコンパイル、Kotlin 出力を `-cp` に入れる。ステップ 3: jar で統合。ステップ 4: d8）。パイロットで両方の `.class` が実際に生成されたことを確認する。
- P2-01（D7）: ロールバックは versionCode を消費する。以後のバッチは新しいグローバル・カウンタから続ける（例: 61、62、ロールバック 63、次バッチ 64）。→ D7 に明記。
- P2-02（D2）: 期待する SHA-256 をバージョン管理された値として固定する。→ 期待値 `7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd` を SPEC・ワークフロー・`~/workspace/tools/kotlin/SHA256` に記録。取得元は公式リリースの `.sha256` サイドカーファイル（2026-11-09 確認）。
- P2-03（T-M1）: 生成された JVM プロパティを `javap -v` で検証する。→ T-M1 に追加（volatile、synchronized、static メンバ、シグネチャ、両 `.class` の存在確認）。

## 付録 3: CONS-111 v2.1 焦点再レビュー指摘 → v2.2 対応表
- v2.1 焦点再レビュー（2026-11-09 10:07、同チャット）: **ADOPTABLE、9.5/10、P0=0、P1=0、P2=2**。移行を阻む重大な設計上の欠陥なし。残る 2 件は軽微であり、採用を遅らせる理由にならない（GPT）。
- P2-01（D2）: javac のクラスパスに kotlin-stdlib が明示されていない。→ ステップ 2 の javac コマンドを `-cp "$ANDROID_JAR:$BUILD/classes:$KOTLIN_STDLIB"` に変更。`$KOTLIN_STDLIB` を kotlinc 配布物の `lib/kotlin-stdlib.jar`（バージョン 2.4.21）と定義。
- P2-02（§5 R6）: R6 のフォールバック記述が v2.1 の設計と不整合（javac 併用はフォールバックではなく通常の必須工程）。→ R6 の対策を「コンパイル手順を調査・修正し、解決できなければ D5 に従って該当ファイルを Java のまま残す」に変更。
- GPT の採用条件: 最初の AppVersion パイロットで、Kotlin・Java の相互参照、stdlib を含む javac のコンパイル、javap -v の検査、APK 内のクラス・ライブラリ収録を一連で確認すること（T-M1 に含まれる）。
