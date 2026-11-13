// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13
package dev.muse.bridge

import android.app.Activity
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.provider.DocumentsContract
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.view.View
import android.widget.Button
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

import java.io.File
import java.io.FileInputStream
import java.lang.ref.WeakReference
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import java.text.SimpleDateFormat
import java.util.Date
import java.util.HashMap
import java.util.Locale
import java.util.UUID

/**
 * On-phone debug log viewer. Works fully offline: it reads the
 * ring buffer directly, no network involved.
 *
 * SPEC-DRIVE-001 (v2.9.0): the .txt export can write straight
 * into a folder the user picked once (Storage Access Framework).
 * The folder is remembered (persistable URI permission) and the
 * export writes there directly, no picker. Without a remembered
 * folder, the export falls back to the per-export picker
 * (ACTION_CREATE_DOCUMENT, the v1.9.4 behaviour).
 *
 * No Drive API, OAuth, or Google Sign-In is used anywhere here.
 * The app only confirms the write to the provider; whether the
 * file reaches the Drive cloud is the Drive app's job.
 */
// The Java original was a non-final public class; keep it open so
// Java subclassing stays possible (D4 contract, CONS-161 P2-02).
// CONS-163 P2-01: WON'T FIX — Accepted ABI Surface.
// Reason: a private companion breaks the existing Kotlin callers
// (MainActivity.kt:244, BootReceiver.kt:37) — kotlinc resolves
// LogActivity.ensureDriveInit through the companion, and a private
// companion is inaccessible from them (verified by the build error,
// 2026-11-13 22:57). Impact: the public Companion field/type and
// the Companion-side ensureDriveInit instance method remain on the
// JVM. Accepted because: compatibility with the current call style
// takes priority; the Java-visible static API is still exactly one
// method, LogActivity.ensureDriveInit(Context). Reconsider when:
// the Kotlin call structure is changed. Same shape as MainActivity
// (accepted in CONS-161).
open class LogActivity : Activity() {

    companion object {
        private const val KIND_FOLDER_PICK = 1
        private const val KIND_FALLBACK_EXPORT = 2

        /** SPEC-DRIVE-001 DRIVE-00: explicit operation state. */
        private enum class OpState {
            IDLE, PICKER_PENDING, WRITING
        }

        private val SAVE_LOCK = Any()
        private var state = OpState.IDLE
        private var currentOpId = 0L
        private val requestCodeToOpId: MutableMap<Int, PickerRequest> =
            HashMap()
        private var currentPickerRequestCode = -1
        /** SPEC-DRIVE-001 DRIVE-03: picker timeout, recovery only. */
        private const val PICKER_TIMEOUT_MS = 10 * 60 * 1000L

        private const val FOLDER_URI_NAME = "log_folder_uri"
        private const val CLEAR_PENDING_NAME =
            "log_folder_clear_pending"
        private const val COUNTER_NAME = "request_code_counter"
        private const val INIT_MARKER_NAME = "drive_init_marker"
        private const val REQUEST_CODE_MIN = 1000
        private const val REQUEST_CODE_MAX = 65535
        /** SPEC-DRIVE-001 NEW-P1-01: random epoch range for the first
         * counter value (1000..60000). */
        private const val EPOCH_MAX = 60000

        /** SPEC-DRIVE-001 DRIVE-02: the currently resumed activity,
         * for completion notifications. Weak, so a finished activity
         * is never held alive by a background write. */
        private var resumedActivity: WeakReference<LogActivity>? = null

        private class PickerRequest(
            val opId: Long,
            val startedAtElapsed: Long,
            val kind: Int
        )

        private enum class FileState {
            EXISTS, NOT_EXISTS, UNKNOWN
        }

        /** SPEC-DRIVE-001 NEW-P1-02: how a small state file was read.
         * MISSING is a verified fact (ENOENT); UNREADABLE means the
         * read itself failed and must never be treated as missing. */
        private enum class ReadKind {
            MISSING, OK, CORRUPT, UNREADABLE
        }

        private class DecimalRead(
            val kind: ReadKind,
            val value: Long
        )

        private enum class FolderKind {
            OK, MISSING, ERROR
        }

        private class FolderRead(
            val kind: FolderKind,
            val uri: String?
        )

        // ------------------------------------------------------------------
        // SPEC-DRIVE-001 DRIVE-00: exclusive control (state + op id).
        // SAVE_LOCK guards only getting / checking / changing the
        // state; it is never held during async work — the state
        // variable itself keeps the exclusion while a background
        // write runs.
        // ------------------------------------------------------------------

        /** Start a new operation from the expected state. Returns the
         * new operation id, or -1 when the state does not match. */
        private fun beginOperation(expected: OpState, initial: OpState): Long {
            return synchronized(SAVE_LOCK) {
                if (state != expected) {
                    return@synchronized -1L
                }
                currentOpId++
                state = initial
                currentOpId
            }
        }

        /** Move an existing operation to its next state. Only the
         * operation that owns the id may move it. */
        private fun transition(expected: OpState, opId: Long,
                next: OpState): Boolean {
            return synchronized(SAVE_LOCK) {
                if (state != expected || currentOpId != opId) {
                    return@synchronized false
                }
                state = next
                true
            }
        }

        /** End an operation. Only the operation that owns the id may
         * release the state, so a stale callback can never clear a
         * newer operation. Always called in a finally block. */
        private fun endOperation(opId: Long) {
            synchronized(SAVE_LOCK) {
                if (currentOpId == opId) {
                    state = OpState.IDLE
                }
            }
        }

        private fun snapshotState(): OpState {
            return synchronized(SAVE_LOCK) {
                state
            }
        }

        private fun removePickerRequest(requestCode: Int) {
            synchronized(SAVE_LOCK) {
                requestCodeToOpId.remove(requestCode)
                if (currentPickerRequestCode == requestCode) {
                    currentPickerRequestCode = -1
                }
            }
        }

        // ------------------------------------------------------------------
        // SPEC-DRIVE-001 DRIVE-00: file helpers (stat distinguishes
        // ENOENT from EACCES/EIO; File.exists() alone never decides).
        // ------------------------------------------------------------------

        private fun statFile(f: File): FileState {
            try {
                Os.stat(f.absolutePath)
                return FileState.EXISTS
            } catch (e: ErrnoException) {
                return if (e.errno == OsConstants.ENOENT)
                    FileState.NOT_EXISTS else FileState.UNKNOWN
            } catch (_: Exception) {
                return FileState.UNKNOWN
            }
        }

        /** Read a small UTF-8 text file. Null on any read failure. */
        private fun readSmallFile(f: File): String? {
            // `in` is a Kotlin keyword; the stream is `input`.
            var input: FileInputStream? = null
            try {
                input = FileInputStream(f)
                val buf = ByteArray(f.length().toInt())
                var read = 0
                while (read < buf.size) {
                    val n = input.read(buf, read, buf.size - read)
                    if (n < 0) {
                        break
                    }
                    read += n
                }
                return String(buf, 0, read, StandardCharsets.UTF_8)
            } catch (_: Exception) {
                return null
            } finally {
                if (input != null) {
                    try {
                        input.close()
                    } catch (_: Exception) {
                    }
                }
            }
        }

        private fun readDecimalFile(ctx: Context, name: String): DecimalRead {
            val f = File(ctx.filesDir, name)
            val st = statFile(f)
            if (st == FileState.NOT_EXISTS) {
                return DecimalRead(ReadKind.MISSING, 0)
            }
            if (st == FileState.UNKNOWN) {
                return DecimalRead(ReadKind.UNREADABLE, 0)
            }
            val content = readSmallFile(f)
            if (content == null) {
                return DecimalRead(ReadKind.UNREADABLE, 0)
            }
            try {
                return DecimalRead(ReadKind.OK,
                    content.trim().toLong())
            } catch (_: NumberFormatException) {
                return DecimalRead(ReadKind.CORRUPT, 0)
            }
        }

        /** firstInstallTime / lastUpdateTime, or null when the
         * PackageManager cannot answer (verification impossible). */
        private fun firstInstallTimes(ctx: Context): LongArray? {
            try {
                val info: PackageInfo = ctx.packageManager.getPackageInfo(
                    ctx.packageName, 0)
                return longArrayOf(info.firstInstallTime, info.lastUpdateTime)
            } catch (_: PackageManager.NameNotFoundException) {
                return null
            }
        }

        // ------------------------------------------------------------------
        // SPEC-DRIVE-001 DRIVE-00 NEW-P1-01 / NEW-P1-02: first init.
        // Runs only from app-start paths (MainActivity, BootReceiver,
        // HttpBridgeServer constructor) and from LogActivity.onCreate
        // — never from a picker launch path. Creates the init marker
        // and the request-code counter only when all four conditions
        // hold; any other state is abnormal and the picker launch
        // refuses (no auto-reset to 1000).
        //
        // Failure model: app-private files can only disappear via
        // (i) a user data-clear / uninstall — which also destroys
        // the activity record, so no stale picker result can arrive;
        // (ii) filesystem corruption — out of scope (broken device);
        // (iii) an app bug — no defined flow deletes the marker or
        // the counter. The random epoch below is the second layer:
        // after a data loss + re-init, the new codes start from a
        // fresh random value, so a stale code collides only by
        // chance (at most N/59000 per new allocation, where N is the
        // number of codes issued from the old sequence — and the
        // stale result must also arrive inside the new operation's
        // pending window).
        //
        // NEW-P1-02: "verification impossible" (a read error, or a
        // PackageManager failure) is not "initialization
        // incomplete". Absence is a verified fact (ENOENT); a failed
        // read is recorded as an exception. The two can never be
        // confused as long as a failed read is never treated as
        // missing. There is no four-type classification model in
        // this file — the startup decision table below IS the
        // classification (file existence x readability x
        // firstInstallTime match).
        // ------------------------------------------------------------------

        /** App-start entry point for the DRIVE-001 first init. */
        @JvmStatic
        fun ensureDriveInit(ctx: Context) {
            val times = firstInstallTimes(ctx)
            if (times == null) {
                return // verification impossible; picker launch refuses
            }
            val marker = readDecimalFile(ctx, INIT_MARKER_NAME)
            val counter = readDecimalFile(ctx, COUNTER_NAME)
            if (marker.kind == ReadKind.OK && counter.kind == ReadKind.OK) {
                // Normal (or a foreign marker — the picker launch
                // re-checks firstInstallTime and refuses then).
                return
            }
            if (marker.kind == ReadKind.MISSING
                && counter.kind == ReadKind.OK) {
                // The counter is the authoritative history; recreate
                // the marker best-effort.
                BridgeLog.atomicWriteText(ctx.filesDir, INIT_MARKER_NAME,
                    times[0].toString())
                return
            }
            if (marker.kind == ReadKind.MISSING
                && counter.kind == ReadKind.MISSING) {
                // v2.9.1 fix (DRIVE-001 live defect, 2026-11-08): the
                // first init must also run on an UPDATE install. Two
                // v2.9.0 conditions blocked every update: (a) the
                // run-meta file — a LOG-001 artifact present on any
                // install that ran v2.8.6+ — is not DRIVE-001 state,
                // so it must not block init; (b) firstInstallTime ==
                // lastUpdateTime is false on any updated install, yet
                // init is safe there because no DRIVE-001 requestCode
                // was ever issued before v2.9.0, so no in-flight
                // picker result can collide with the fresh epoch.
                // What still guards init: no DRIVE-001 state at all
                // (no folder file, no clear marker, no registry,
                // no persisted URI permission).
                // CONS-078 P2-02: a failed persisted-permission check
                // is NOT "absent". If the check itself throws, refuse
                // init rather than proceeding on unknown state.
                val noPersistedPermissions = try {
                    ctx.contentResolver.persistedUriPermissions.isEmpty()
                } catch (_: Exception) {
                    false
                }
                val noOtherState =
                    statFile(File(ctx.filesDir, FOLDER_URI_NAME)) ==
                        FileState.NOT_EXISTS
                        && statFile(File(ctx.filesDir,
                            CLEAR_PENDING_NAME)) == FileState.NOT_EXISTS
                        && statFile(UriPermissionRegistry.registryFile(ctx)) ==
                            FileState.NOT_EXISTS
                        && noPersistedPermissions
                if (noOtherState) {
                    BridgeLog.atomicWriteText(ctx.filesDir,
                        INIT_MARKER_NAME, times[0].toString())
                    val start = REQUEST_CODE_MIN + SecureRandom()
                        .nextInt(EPOCH_MAX - REQUEST_CODE_MIN + 1)
                    BridgeLog.atomicWriteText(ctx.filesDir, COUNTER_NAME,
                        start.toString())
                }
                // Any failed condition is abnormal: do nothing; the
                // picker launch refuses and never auto-resets to 1000.
                return
            }
            // Every other combination (marker corrupt/unreadable, or
            // counter missing/corrupt/unreadable while the marker
            // exists) is abnormal: do nothing.
        }

        /** Picker-launch gate: true only when the marker and the
         * counter are both present, readable, and the marker's
         * firstInstallTime matches the current install. */
        private fun checkDriveInitOk(ctx: Context): Boolean {
            val times = firstInstallTimes(ctx)
            if (times == null) {
                return false
            }
            val marker = readDecimalFile(ctx, INIT_MARKER_NAME)
            val counter = readDecimalFile(ctx, COUNTER_NAME)
            return marker.kind == ReadKind.OK && counter.kind == ReadKind.OK
                && marker.value == times[0]
        }

        /**
         * Allocate a requestCode from the persistent counter. The
         * reservation is persisted FIRST, then registered in the map
         * — a code is never reused, even when the picker launch
         * fails afterwards. Returns -1 on refusal (counter missing,
         * corrupt, unreadable, out of range, or the reservation
         * could not be persisted).
         */
        private fun allocateRequestCode(ctx: Context, opId: Long, kind: Int): Int {
            return synchronized(SAVE_LOCK) {
                val c = readDecimalFile(ctx, COUNTER_NAME)
                if (c.kind != ReadKind.OK) {
                    return@synchronized -1
                }
                val next = c.value
                if (next < REQUEST_CODE_MIN || next > REQUEST_CODE_MAX) {
                    return@synchronized -1
                }
                if (!BridgeLog.atomicWriteText(ctx.filesDir, COUNTER_NAME,
                        (next + 1).toString())) {
                    return@synchronized -1
                }
                val code = next.toInt()
                requestCodeToOpId.put(code, PickerRequest(opId,
                    SystemClock.elapsedRealtime(), kind))
                currentPickerRequestCode = code
                code
            }
        }

        private fun fallbackFileName(): String {
            val ts = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .format(Date())
            return "bridge-log-" + ts + ".txt"
        }

        private fun autoSaveFileName(): String {
            val ts = SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.US)
                .format(Date())
            val uuid = UUID.randomUUID().toString().replace("-", "")
                .substring(0, 6)
            return "bridge-log-" + ts + "-" + uuid + ".txt"
        }

        private fun deleteCreatedBestEffort(resolver: ContentResolver,
                created: Uri?) {
            if (created == null) {
                return
            }
            try {
                DocumentsContract.deleteDocument(resolver, created)
            } catch (_: Exception) {
                // Must not hide the original error.
            }
        }
    }

    // ------------------------------------------------------------------
    // Instance state: the views. Nullable (not a must-be-initialised
    // field) because the Java fields start null and
    // setButtonsEnabled / refreshFolderDisplay guard against null
    // — the Kotlin form keeps those guards meaningful instead of
    // turning them into dead checks.
    // ------------------------------------------------------------------

    private var logText: TextView? = null
    private var scrollView: ScrollView? = null
    private var refreshButton: Button? = null
    private var exportButton: Button? = null
    private var folderButton: Button? = null
    private var folderClearButton: Button? = null
    private var folderText: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_log)

        logText = findViewById(R.id.log_text)
        scrollView = findViewById(R.id.log_scroll)
        folderText = findViewById(R.id.log_folder_text)

        refreshButton = findViewById(R.id.log_refresh_button)
        refreshButton?.setOnClickListener { refresh() }

        exportButton = findViewById(R.id.log_export_button)
        exportButton?.setOnClickListener { exportLog() }

        folderButton = findViewById(R.id.log_folder_button)
        folderButton?.setOnClickListener { pickFolder() }

        folderClearButton = findViewById(R.id.log_folder_clear_button)
        folderClearButton?.setOnClickListener { clearFolder() }

        ensureDriveInit(this)
        startupConsistencyCheck()
        refresh()
        syncUiFromState()
    }

    override fun onResume() {
        super.onResume()
        resumedActivity = WeakReference(this)
        checkPickerTimeout()
        refresh()
        syncUiFromState()
    }

    override fun onPause() {
        val ra = resumedActivity
        if (ra != null && ra.get() == this) {
            ra.clear()
        }
        super.onPause()
    }

    private fun refresh() {
        val text = BridgeLog.renderText(
            AppVersion.name(this), AppVersion.code(this))
        logText?.setText(text)
        // Show the newest lines (the end of the log).
        scrollView?.post { scrollView?.fullScroll(View.FOCUS_DOWN) }
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02 / DRIVE-03: export (auto-save or
    // fallback picker).
    // ------------------------------------------------------------------

    private fun exportLog() {
        val opId = beginOperation(OpState.IDLE, OpState.WRITING)
        if (opId < 0) {
            return // busy (double-tap guard)
        }
        setButtonsEnabled(false)
        // Snapshot the log at save start, so a background update
        // cannot change the output mid-write.
        val snapshot = BridgeLog.renderText(
            AppVersion.name(this), AppVersion.code(this))
        val folder = readFolderUri()
        if (folder.kind == FolderKind.ERROR) {
            endOperation(opId)
            setButtonsEnabled(true)
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                Toast.LENGTH_LONG).show()
            return
        }
        if (folder.kind == FolderKind.MISSING) {
            // Fallback: end this operation and start a picker one.
            endOperation(opId)
            val pickOp = beginOperation(OpState.IDLE,
                OpState.PICKER_PENDING)
            if (pickOp < 0) {
                setButtonsEnabled(true)
                return
            }
            val code = allocateRequestCode(this, pickOp,
                KIND_FALLBACK_EXPORT)
            if (code < 0) {
                endOperation(pickOp)
                setButtonsEnabled(true)
                return
            }
            val intent = Intent(Intent.ACTION_CREATE_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("text/plain")
            intent.putExtra(Intent.EXTRA_TITLE, fallbackFileName())
            try {
                startActivityForResult(intent, code)
            } catch (_: Exception) {
                removePickerRequest(code)
                endOperation(pickOp)
                setButtonsEnabled(true)
            }
            return
        }
        // kind == OK implies folder.uri is non-null; Uri.parse
        // needs a non-null String, so assert it (the Java code
        // would throw NullPointerException on a null here too).
        writeLogToUriInBackground(Uri.parse(folder.uri!!), snapshot, opId,
            true)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int,
            data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        // The whole check sequence runs in one SAVE_LOCK section,
        // so the timeout path and the result callback cannot race
        // each other into a double release or a wrong transition.
        // The returns below are non-local: synchronized is inline,
        // so they exit onActivityResult, exactly like the Java.
        // The block yields the request; every other exit returns
        // from the function.
        val req: PickerRequest = synchronized(SAVE_LOCK) {
            val r = requestCodeToOpId[requestCode]
                ?: return // unknown or already invalidated
            if (r.opId != currentOpId) {
                // Stale result: drop only this entry, keep the
                // current operation untouched.
                requestCodeToOpId.remove(requestCode)
                if (currentPickerRequestCode == requestCode) {
                    currentPickerRequestCode = -1
                }
                return
            }
            if (state != OpState.PICKER_PENDING) {
                requestCodeToOpId.remove(requestCode)
                if (currentPickerRequestCode == requestCode) {
                    currentPickerRequestCode = -1
                }
                return
            }
            if (SystemClock.elapsedRealtime() - r.startedAtElapsed
                >= PICKER_TIMEOUT_MS) {
                requestCodeToOpId.remove(requestCode)
                currentPickerRequestCode = -1
                endOperation(r.opId)
                Toast.makeText(this, R.string.toast_picker_timeout,
                    Toast.LENGTH_LONG).show()
                syncUiFromState()
                return
            }
            requestCodeToOpId.remove(requestCode)
            currentPickerRequestCode = -1
            if (!transition(OpState.PICKER_PENDING, r.opId,
                    OpState.WRITING)) {
                return
            }
            r
        }
        if (resultCode != RESULT_OK || data == null
            || data.data == null) {
            // User cancelled; the existing folder is unchanged.
            endOperation(req.opId)
            syncUiFromState()
            return
        }
        if (req.kind == KIND_FOLDER_PICK) {
            handleFolderPicked(data, req.opId)
        } else {
            val snapshot = BridgeLog.renderText(
                AppVersion.name(this), AppVersion.code(this))
            // data.data is non-null: checked above.
            writeLogToUriInBackground(data.data!!, snapshot, req.opId,
                false)
        }
    }

    /** SPEC-DRIVE-001 DRIVE-03: the timeout is recovery from an
     * abnormal end, not a completion of the open picker. A late
     * result is ignored because its requestCode is already gone
     * from the map, and the user is asked to pick again. */
    private fun checkPickerTimeout() {
        // Non-local returns inside synchronized exit this function,
        // exactly like the Java.
        synchronized(SAVE_LOCK) {
            if (state != OpState.PICKER_PENDING) {
                return
            }
            val code = currentPickerRequestCode
            if (code < 0) {
                return
            }
            val req = requestCodeToOpId[code] ?: return
            if (SystemClock.elapsedRealtime() - req.startedAtElapsed
                < PICKER_TIMEOUT_MS) {
                return
            }
            requestCodeToOpId.remove(code)
            currentPickerRequestCode = -1
            endOperation(req.opId)
        }
        Toast.makeText(this, R.string.toast_picker_timeout,
            Toast.LENGTH_LONG).show()
        syncUiFromState()
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-01 / DRIVE-05: pick, change, clear.
    // ------------------------------------------------------------------

    private fun pickFolder() {
        if (!checkDriveInitOk(this)) {
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                Toast.LENGTH_LONG).show()
            return
        }
        val opId = beginOperation(OpState.IDLE, OpState.PICKER_PENDING)
        if (opId < 0) {
            return
        }
        setButtonsEnabled(false)
        val code = allocateRequestCode(this, opId, KIND_FOLDER_PICK)
        if (code < 0) {
            endOperation(opId)
            setButtonsEnabled(true)
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                Toast.LENGTH_LONG).show()
            return
        }
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
            or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        try {
            startActivityForResult(intent, code)
        } catch (_: Exception) {
            removePickerRequest(code)
            endOperation(opId)
            setButtonsEnabled(true)
        }
    }

    private fun handleFolderPicked(data: Intent, opId: Long) {
        // data.data is non-null: onActivityResult checked it.
        val treeUri = data.data!!
        val granted = data.flags
        val readOk = (granted
            and Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0
        val writeOk = (granted
            and Intent.FLAG_GRANT_WRITE_URI_PERMISSION) != 0
        val persistOk = (granted
            and Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION) != 0
        if (!readOk || !writeOk || !persistOk) {
            endOperation(opId)
            syncUiFromState()
            Toast.makeText(this, R.string.toast_log_folder_no_write,
                Toast.LENGTH_LONG).show()
            return
        }
        if (!isCreatableDirectory(treeUri)) {
            endOperation(opId)
            syncUiFromState()
            Toast.makeText(this, R.string.toast_log_folder_not_creatable,
                Toast.LENGTH_LONG).show()
            return
        }
        try {
            contentResolver.takePersistableUriPermission(treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                    or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        } catch (_: Exception) {
            endOperation(opId)
            syncUiFromState()
            Toast.makeText(this, R.string.toast_log_folder_no_write,
                Toast.LENGTH_LONG).show()
            return
        }
        val uriString = treeUri.toString()
        if (!UriPermissionRegistry.register(this,
                UriPermissionRegistry.FEATURE_LOG_FOLDER, uriString)) {
            // Registration failed: the feature is not enabled for
            // this URI; the permission is kept.
            endOperation(opId)
            syncUiFromState()
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                Toast.LENGTH_LONG).show()
            return
        }
        val old = readFolderUri()
        val oldUri = if (old.kind == FolderKind.OK) old.uri else null
        if (!BridgeLog.atomicWriteText(filesDir, FOLDER_URI_NAME,
                uriString)) {
            handleFolderWriteFailed(uriString, oldUri, opId)
            return
        }
        // Success: release the old permission only when it is a
        // different folder (re-picking the same folder keeps it).
        if (oldUri != null && oldUri != uriString) {
            UriPermissionRegistry.releaseIfUnused(this,
                UriPermissionRegistry.FEATURE_LOG_FOLDER, oldUri)
        }
        endOperation(opId)
        syncUiFromState()
    }

    /** SPEC-DRIVE-001 DRIVE-05 step 4 (NEW-P2-01): the settings
     * write failed after the new URI was registered. Remove the
     * new registration FIRST; only when that succeeds may the
     * new permission be released. A failed registry removal
     * means the usage is uncertain — the permission is kept. */
    private fun handleFolderWriteFailed(newUri: String, oldUri: String?,
            opId: Long) {
        val stored = readFolderUriString()
        if (stored != null && oldUri != null && stored == oldUri) {
            // fsync/rename failed: the old file is intact.
            if (newUri != oldUri) {
                if (UriPermissionRegistry.unregister(this,
                        UriPermissionRegistry.FEATURE_LOG_FOLDER, newUri)) {
                    releasePermissionBestEffort(newUri)
                }
                // else: keep the new permission (uncertain).
            }
        }
        // Otherwise the rename took effect but the directory sync
        // failed (or the state is unreadable): keep BOTH
        // permissions until the state is certain.
        endOperation(opId)
        syncUiFromState()
        Toast.makeText(this, R.string.toast_log_folder_change_failed,
            Toast.LENGTH_LONG).show()
    }

    private fun releasePermissionBestEffort(uriString: String) {
        try {
            contentResolver.releasePersistableUriPermission(
                Uri.parse(uriString),
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                    or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        } catch (_: Exception) {
        }
    }

    private fun isCreatableDirectory(treeUri: Uri): Boolean {
        try {
            val docId = DocumentsContract.getTreeDocumentId(treeUri)
            val docUri = DocumentsContract.buildDocumentUriUsingTree(
                treeUri, docId)
            val c = contentResolver.query(docUri,
                arrayOf(
                    DocumentsContract.Document.COLUMN_MIME_TYPE,
                    DocumentsContract.Document.COLUMN_FLAGS),
                null, null, null)
            if (c == null) {
                return false
            }
            try {
                if (!c.moveToFirst()) {
                    return false
                }
                val mime = c.getString(0)
                val flags = c.getInt(1)
                return DocumentsContract.Document.MIME_TYPE_DIR == mime
                    && flags and DocumentsContract.Document
                        .FLAG_DIR_SUPPORTS_CREATE != 0
            } finally {
                c.close()
            }
        } catch (_: Exception) {
            return false
        }
    }

    private fun clearFolder() {
        val opId = beginOperation(OpState.IDLE, OpState.WRITING)
        if (opId < 0) {
            return
        }
        setButtonsEnabled(false)
        try {
            doClearFolder()
        } finally {
            endOperation(opId)
            syncUiFromState()
        }
    }

    /**
     * SPEC-DRIVE-001 DRIVE-05 (NEW-P2-02): the marker is written
     * first, so a crash mid-clear leaves recovery info. The
     * deletion result is then determined by re-reading, and the
     * classification (A/B/C) is the FINAL determination — a
     * directory-sync failure alone never decides the marker:
     * - A: the file is present and the old URI reads back — the
     *   clear did not happen; permission kept, marker MAY be
     *   deleted (only when both the file's presence and the old
     *   URI's readability are confirmed).
     * - B: the state cannot be confirmed (EACCES/EIO) — keep
     *   the permission and the marker.
     * - C: ENOENT confirmed — proceed to release; the marker
     *   stays until the release succeeds.
     */
    private fun doClearFolder() {
        val folder = readFolderUri()
        if (folder.kind != FolderKind.OK) {
            return // nothing saved; nothing to clear
        }
        // kind == OK implies folder.uri is non-null.
        val uriString = folder.uri!!
        if (!BridgeLog.atomicWriteText(filesDir, CLEAR_PENDING_NAME,
                uriString)) {
            Toast.makeText(this, R.string.toast_log_folder_clear_failed,
                Toast.LENGTH_LONG).show()
            return
        }
        val settings = File(filesDir, FOLDER_URI_NAME)
        settings.delete()
        BridgeLog.fsyncDirectory(filesDir)
        val st = statFile(settings)
        if (st == FileState.EXISTS) {
            val stored = readSmallFile(settings)
            if (stored != null && stored.trim() == uriString) {
                deleteClearMarker() // kind A
            }
            // kind A (content differs) and kind B both keep the
            // permission and the marker.
            return
        }
        if (st == FileState.UNKNOWN) {
            return // kind B: keep the permission and the marker
        }
        // kind C: proceed to release.
        val released = UriPermissionRegistry.releaseIfUnused(this,
            UriPermissionRegistry.FEATURE_LOG_FOLDER, uriString)
        if (released) {
            deleteClearMarker()
        }
        // else: keep the marker for next-launch reconciliation.
    }

    private fun deleteClearMarker() {
        val m = File(filesDir, CLEAR_PENDING_NAME)
        m.delete()
        BridgeLog.fsyncDirectory(filesDir)
    }

    /**
     * SPEC-DRIVE-001 DRIVE-05: next-launch consistency check.
     * Runs in onCreate: reconcile the registry, resolve a
     * leftover clear marker, and warn when a saved folder lost
     * its permission (treated as unselected, file left alone).
     */
    private fun startupConsistencyCheck() {
        UriPermissionRegistry.reconcile(this)
        val marker = File(filesDir, CLEAR_PENDING_NAME)
        if (statFile(marker) == FileState.EXISTS) {
            val markerUri = readSmallFile(marker)
            val settings = File(filesDir, FOLDER_URI_NAME)
            val st = statFile(settings)
            if (st == FileState.EXISTS) {
                // The clear never took effect. If the folder was
                // re-picked after the crashed clear, the marker
                // URI is orphaned — release it when safe.
                val stored = readSmallFile(settings)
                if (stored != null && markerUri != null
                    && stored.trim() != markerUri.trim()) {
                    UriPermissionRegistry.releaseIfUnused(this,
                        UriPermissionRegistry.FEATURE_LOG_FOLDER,
                        markerUri.trim())
                }
                deleteClearMarker()
            } else if (st == FileState.NOT_EXISTS) {
                // ENOENT: the clear was effective on disk.
                if (markerUri != null && markerUri.trim().isNotEmpty()) {
                    val released = UriPermissionRegistry
                        .releaseIfUnused(this,
                            UriPermissionRegistry.FEATURE_LOG_FOLDER,
                            markerUri.trim())
                    if (released) {
                        deleteClearMarker()
                    }
                } else {
                    deleteClearMarker()
                }
            }
            // UNKNOWN: cannot decide; keep the marker.
        }
        val folder = readFolderUri()
        if (folder.kind == FolderKind.OK
            && !isWritePermissionHeld(folder.uri)) {
            Toast.makeText(this, R.string.toast_log_folder_no_permission,
                Toast.LENGTH_LONG).show()
        }
    }

    private fun isWritePermissionHeld(uriString: String?): Boolean {
        // CONS-078 P2-02: a failed check means "not confirmed
        // held" — never a crash, never treated as held.
        // uriString is nullable: String.equals(null) is false, so
        // a null URI simply never matches (same as the Java).
        try {
            for (p in contentResolver
                .persistedUriPermissions) {
                if (p.uri.toString() == uriString
                    && p.isWritePermission) {
                    return true
                }
            }
        } catch (_: Exception) {
            return false
        }
        return false
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02: background write + completion.
    // ------------------------------------------------------------------

    private fun writeLogToUriInBackground(target: Uri,
            text: String, opId: Long, autoSave: Boolean) {
        val appCtx = applicationContext
        Thread {
            var ok = false
            var clearFolder = false
            var created: Uri? = null
            try {
                val resolver = appCtx.contentResolver
                var writeTarget = target
                if (autoSave) {
                    val docId = DocumentsContract
                        .getTreeDocumentId(target)
                    val parent = DocumentsContract
                        .buildDocumentUriUsingTree(target, docId)
                    val newDoc = DocumentsContract.createDocument(resolver,
                        parent, "text/plain", autoSaveFileName())
                        ?: throw IllegalStateException(
                            "createDocument returned null")
                    created = newDoc
                    writeTarget = newDoc
                }
                val out = resolver.openOutputStream(writeTarget)
                    ?: throw IllegalStateException(
                        "openOutputStream returned null")
                try {
                    out.write(text.toByteArray(StandardCharsets.UTF_8))
                } finally {
                    try {
                        out.close()
                    } catch (_: Exception) {
                    }
                }
                ok = true
            } catch (_: Exception) {
                if (autoSave) {
                    if (created != null) {
                        deleteCreatedBestEffort(
                            appCtx.contentResolver, created)
                    }
                    clearFolder = classifyAutoSaveFailure(target)
                }
            } finally {
                if (clearFolder) {
                    clearSavedFolderAfterFailure()
                }
                endOperation(opId)
                notifySaveFinished(ok)
            }
        }.start()
    }

    /**
     * SPEC-DRIVE-001 DRIVE-06: classify an auto-save failure.
     * The saved folder is cleared ONLY when verified:
     * - the persisted WRITE permission is actually gone, or
     * - the URI is clearly malformed, or
     * - the parent folder is confirmed absent (two independent
     *   queries, both completing normally with zero rows).
     * A SecurityException alone, a FileNotFoundException alone,
     * an IOException, a null createDocument, low storage, or no
     * provider response all KEEP the folder.
     */
    private fun classifyAutoSaveFailure(treeUri: Uri): Boolean {
        val resolver = applicationContext
            .contentResolver
        var writeHeld = false
        // CONS-078 P2-02: a failed check must NOT clear the
        // folder — failure is not "permission absent". Keep it.
        try {
            for (p in resolver.persistedUriPermissions) {
                if (p.uri.toString() == treeUri.toString()
                    && p.isWritePermission) {
                    writeHeld = true
                    break
                }
            }
        } catch (_: Exception) {
            return false
        }
        if (!writeHeld) {
            return true
        }
        val docId: String?
        try {
            docId = DocumentsContract.getTreeDocumentId(treeUri)
        } catch (_: Exception) {
            return true
        }
        if (docId == null) {
            return true
        }
        val parent = DocumentsContract.buildDocumentUriUsingTree(
            treeUri, docId)
        var emptyCount = 0
        for (i in 0 until 2) {
            if (i > 0) {
                try {
                    Thread.sleep(500)
                } catch (_: InterruptedException) {
                }
            }
            var c: Cursor? = null
            try {
                c = resolver.query(parent,
                    arrayOf(
                        DocumentsContract.Document.COLUMN_DOCUMENT_ID),
                    null, null, null)
                if (c == null) {
                    return false // keep: no answer
                }
                if (!c.moveToFirst()) {
                    emptyCount++
                } else {
                    return false // parent exists: keep
                }
            } catch (_: Exception) {
                return false // keep on any error
            } finally {
                c?.close()
            }
        }
        return emptyCount == 2
    }

    /** Remove a verified-dead saved folder (file + registry).
     * Runs on the background thread; the UI resyncs via the
     * completion notification. */
    private fun clearSavedFolderAfterFailure() {
        val uri = readFolderUriString()
        if (uri != null) {
            UriPermissionRegistry.releaseIfUnused(
                applicationContext,
                UriPermissionRegistry.FEATURE_LOG_FOLDER, uri)
        }
        val f = File(filesDir, FOLDER_URI_NAME)
        f.delete()
        BridgeLog.fsyncDirectory(filesDir)
    }

    /** Notify the currently resumed activity (if any) that the
     * save finished. The notified side re-reads the static state
     * instead of trusting the notification alone. When no
     * activity is resumed, nothing is sent — the next onResume
     * resyncs the UI from the state. */
    private fun notifySaveFinished(ok: Boolean) {
        val activity = resumedActivity?.get()
        if (activity == null) {
            return
        }
        val target = activity
        target.runOnUiThread {
            if (target.isFinishing || target.isDestroyed) {
                return@runOnUiThread
            }
            target.syncUiFromState()
            Toast.makeText(target,
                if (ok) R.string.toast_log_saved
                else R.string.toast_log_save_failed,
                Toast.LENGTH_LONG).show()
        }
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02 / DRIVE-04: UI sync + folder display.
    // ------------------------------------------------------------------

    private fun syncUiFromState() {
        val busy = snapshotState() != OpState.IDLE
        setButtonsEnabled(!busy)
        refreshFolderDisplay()
    }

    private fun setButtonsEnabled(enabled: Boolean) {
        refreshButton?.isEnabled = enabled
        exportButton?.isEnabled = enabled
        folderButton?.isEnabled = enabled
        folderClearButton?.isEnabled = enabled
    }

    private fun readFolderUri(): FolderRead {
        val f = File(filesDir, FOLDER_URI_NAME)
        val st = statFile(f)
        if (st == FileState.NOT_EXISTS) {
            return FolderRead(FolderKind.MISSING, null)
        }
        if (st == FileState.UNKNOWN) {
            return FolderRead(FolderKind.ERROR, null)
        }
        val content = readSmallFile(f)
        if (content == null) {
            return FolderRead(FolderKind.ERROR, null)
        }
        val uri = content.trim()
        if (uri.isEmpty()) {
            return FolderRead(FolderKind.ERROR, null)
        }
        try {
            DocumentsContract.getTreeDocumentId(Uri.parse(uri))
        } catch (_: Exception) {
            return FolderRead(FolderKind.ERROR, null)
        }
        return FolderRead(FolderKind.OK, uri)
    }

    private fun readFolderUriString(): String? {
        val folder = readFolderUri()
        return if (folder.kind == FolderKind.OK) folder.uri else null
    }

    private fun refreshFolderDisplay() {
        if (folderText == null) {
            return
        }
        val folder = readFolderUri()
        if (folder.kind != FolderKind.OK
            || !isWritePermissionHeld(folder.uri)) {
            folderText?.setText(R.string.log_folder_unselected)
            return
        }
        // kind == OK implies folder.uri is non-null.
        val name = resolveFolderDisplayName(Uri.parse(folder.uri!!))
        folderText?.setText(name
            ?: getString(R.string.log_folder_name_unknown))
    }

    /** Display name via COLUMN_DISPLAY_NAME; the raw URI is never
     * shown on screen. Null when it cannot be read. */
    private fun resolveFolderDisplayName(treeUri: Uri): String? {
        try {
            val docId = DocumentsContract.getTreeDocumentId(treeUri)
            val docUri = DocumentsContract.buildDocumentUriUsingTree(
                treeUri, docId)
            val c = contentResolver.query(docUri,
                arrayOf(
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME),
                null, null, null)
            if (c == null) {
                return null
            }
            try {
                if (!c.moveToFirst()) {
                    return null
                }
                val idx = c.getColumnIndex(
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                if (idx < 0) {
                    return null
                }
                return c.getString(idx)
            } finally {
                c.close()
            }
        } catch (_: Exception) {
            return null
        }
    }
}
