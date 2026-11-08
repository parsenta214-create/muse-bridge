package dev.muse.bridge;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.UriPermission;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.provider.DocumentsContract;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.view.View;
import android.widget.Button;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

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
public class LogActivity extends Activity {

    private static final int KIND_FOLDER_PICK = 1;
    private static final int KIND_FALLBACK_EXPORT = 2;

    /** SPEC-DRIVE-001 DRIVE-00: explicit operation state. */
    private enum OpState {
        IDLE, PICKER_PENDING, WRITING
    }

    private static final Object SAVE_LOCK = new Object();
    private static OpState state = OpState.IDLE;
    private static long currentOpId = 0;
    private static final Map<Integer, PickerRequest> requestCodeToOpId =
            new HashMap<>();
    private static int currentPickerRequestCode = -1;
    /** SPEC-DRIVE-001 DRIVE-03: picker timeout, recovery only. */
    private static final long PICKER_TIMEOUT_MS = 10 * 60 * 1000;

    private static final String FOLDER_URI_NAME = "log_folder_uri";
    private static final String CLEAR_PENDING_NAME =
            "log_folder_clear_pending";
    private static final String COUNTER_NAME = "request_code_counter";
    private static final String INIT_MARKER_NAME = "drive_init_marker";
    private static final int REQUEST_CODE_MIN = 1000;
    private static final int REQUEST_CODE_MAX = 65535;
    /** SPEC-DRIVE-001 NEW-P1-01: random epoch range for the first
     * counter value (1000..60000). */
    private static final int EPOCH_MAX = 60000;

    /** SPEC-DRIVE-001 DRIVE-02: the currently resumed activity,
     * for completion notifications. Weak, so a finished activity
     * is never held alive by a background write. */
    private static WeakReference<LogActivity> resumedActivity;

    private static class PickerRequest {
        final long opId;
        final long startedAtElapsed;
        final int kind;

        PickerRequest(long opId, long startedAtElapsed, int kind) {
            this.opId = opId;
            this.startedAtElapsed = startedAtElapsed;
            this.kind = kind;
        }
    }

    private enum FileState {
        EXISTS, NOT_EXISTS, UNKNOWN
    }

    /** SPEC-DRIVE-001 NEW-P1-02: how a small state file was read.
     * MISSING is a verified fact (ENOENT); UNREADABLE means the
     * read itself failed and must never be treated as missing. */
    private enum ReadKind {
        MISSING, OK, CORRUPT, UNREADABLE
    }

    private static class DecimalRead {
        final ReadKind kind;
        final long value;

        DecimalRead(ReadKind kind, long value) {
            this.kind = kind;
            this.value = value;
        }
    }

    private enum FolderKind {
        OK, MISSING, ERROR
    }

    private static class FolderRead {
        final FolderKind kind;
        final String uri;

        FolderRead(FolderKind kind, String uri) {
            this.kind = kind;
            this.uri = uri;
        }
    }

    private TextView logText;
    private ScrollView scrollView;
    private Button refreshButton;
    private Button exportButton;
    private Button folderButton;
    private Button folderClearButton;
    private TextView folderText;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_log);

        logText = findViewById(R.id.log_text);
        scrollView = findViewById(R.id.log_scroll);
        folderText = findViewById(R.id.log_folder_text);

        refreshButton = findViewById(R.id.log_refresh_button);
        refreshButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                refresh();
            }
        });

        exportButton = findViewById(R.id.log_export_button);
        exportButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                exportLog();
            }
        });

        folderButton = findViewById(R.id.log_folder_button);
        folderButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                pickFolder();
            }
        });

        folderClearButton = findViewById(R.id.log_folder_clear_button);
        folderClearButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                clearFolder();
            }
        });

        ensureDriveInit(this);
        startupConsistencyCheck();
        refresh();
        syncUiFromState();
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumedActivity = new WeakReference<>(this);
        checkPickerTimeout();
        refresh();
        syncUiFromState();
    }

    @Override
    protected void onPause() {
        if (resumedActivity != null
                && resumedActivity.get() == this) {
            resumedActivity.clear();
        }
        super.onPause();
    }

    private void refresh() {
        String text = BridgeLog.renderText(
                AppVersion.name(this), AppVersion.code(this));
        logText.setText(text);
        // Show the newest lines (the end of the log).
        scrollView.post(new Runnable() {
            @Override
            public void run() {
                scrollView.fullScroll(View.FOCUS_DOWN);
            }
        });
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-00: exclusive control (state + op id).
    // SAVE_LOCK guards only getting / checking / changing the
    // state; it is never held during async work — the state
    // variable itself keeps the exclusion while a background
    // write runs.
    // ------------------------------------------------------------------

    /** Start a new operation from the expected state. Returns the
     * new operation id, or -1 when the state does not match. */
    private static long beginOperation(OpState expected, OpState initial) {
        synchronized (SAVE_LOCK) {
            if (state != expected) {
                return -1;
            }
            currentOpId++;
            state = initial;
            return currentOpId;
        }
    }

    /** Move an existing operation to its next state. Only the
     * operation that owns the id may move it. */
    private static boolean transition(OpState expected, long opId,
            OpState next) {
        synchronized (SAVE_LOCK) {
            if (state != expected || currentOpId != opId) {
                return false;
            }
            state = next;
            return true;
        }
    }

    /** End an operation. Only the operation that owns the id may
     * release the state, so a stale callback can never clear a
     * newer operation. Always called in a finally block. */
    private static void endOperation(long opId) {
        synchronized (SAVE_LOCK) {
            if (currentOpId == opId) {
                state = OpState.IDLE;
            }
        }
    }

    private static OpState snapshotState() {
        synchronized (SAVE_LOCK) {
            return state;
        }
    }

    private static void removePickerRequest(int requestCode) {
        synchronized (SAVE_LOCK) {
            requestCodeToOpId.remove(requestCode);
            if (currentPickerRequestCode == requestCode) {
                currentPickerRequestCode = -1;
            }
        }
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-00: file helpers (stat distinguishes
    // ENOENT from EACCES/EIO; File.exists() alone never decides).
    // ------------------------------------------------------------------

    private static FileState statFile(File f) {
        try {
            Os.stat(f.getAbsolutePath());
            return FileState.EXISTS;
        } catch (ErrnoException e) {
            return e.errno == OsConstants.ENOENT
                    ? FileState.NOT_EXISTS : FileState.UNKNOWN;
        } catch (Exception ignored) {
            return FileState.UNKNOWN;
        }
    }

    /** Read a small UTF-8 text file. Null on any read failure. */
    private static String readSmallFile(File f) {
        FileInputStream in = null;
        try {
            in = new FileInputStream(f);
            byte[] buf = new byte[(int) f.length()];
            int read = 0;
            while (read < buf.length) {
                int n = in.read(buf, read, buf.length - read);
                if (n < 0) {
                    break;
                }
                read += n;
            }
            return new String(buf, 0, read, StandardCharsets.UTF_8);
        } catch (Exception ignored) {
            return null;
        } finally {
            if (in != null) {
                try {
                    in.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private static DecimalRead readDecimalFile(Context ctx, String name) {
        File f = new File(ctx.getFilesDir(), name);
        FileState st = statFile(f);
        if (st == FileState.NOT_EXISTS) {
            return new DecimalRead(ReadKind.MISSING, 0);
        }
        if (st == FileState.UNKNOWN) {
            return new DecimalRead(ReadKind.UNREADABLE, 0);
        }
        String content = readSmallFile(f);
        if (content == null) {
            return new DecimalRead(ReadKind.UNREADABLE, 0);
        }
        try {
            return new DecimalRead(ReadKind.OK,
                    Long.parseLong(content.trim()));
        } catch (NumberFormatException ignored) {
            return new DecimalRead(ReadKind.CORRUPT, 0);
        }
    }

    /** firstInstallTime / lastUpdateTime, or null when the
     * PackageManager cannot answer (verification impossible). */
    private static long[] firstInstallTimes(Context ctx) {
        try {
            PackageInfo info = ctx.getPackageManager().getPackageInfo(
                    ctx.getPackageName(), 0);
            return new long[]{info.firstInstallTime, info.lastUpdateTime};
        } catch (PackageManager.NameNotFoundException ignored) {
            return null;
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
    public static void ensureDriveInit(Context ctx) {
        long[] times = firstInstallTimes(ctx);
        if (times == null) {
            return; // verification impossible; picker launch refuses
        }
        DecimalRead marker = readDecimalFile(ctx, INIT_MARKER_NAME);
        DecimalRead counter = readDecimalFile(ctx, COUNTER_NAME);
        if (marker.kind == ReadKind.OK && counter.kind == ReadKind.OK) {
            // Normal (or a foreign marker — the picker launch
            // re-checks firstInstallTime and refuses then).
            return;
        }
        if (marker.kind == ReadKind.MISSING
                && counter.kind == ReadKind.OK) {
            // The counter is the authoritative history; recreate
            // the marker best-effort.
            BridgeLog.atomicWriteText(ctx.getFilesDir(), INIT_MARKER_NAME,
                    String.valueOf(times[0]));
            return;
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
            boolean noPersistedPermissions;
            try {
                noPersistedPermissions = ctx.getContentResolver()
                        .getPersistedUriPermissions().isEmpty();
            } catch (Exception e) {
                noPersistedPermissions = false;
            }
            boolean noOtherState =
                    statFile(new File(ctx.getFilesDir(), FOLDER_URI_NAME))
                            == FileState.NOT_EXISTS
                    && statFile(new File(ctx.getFilesDir(),
                            CLEAR_PENDING_NAME)) == FileState.NOT_EXISTS
                    && statFile(UriPermissionRegistry.registryFile(ctx))
                            == FileState.NOT_EXISTS
                    && noPersistedPermissions;
            if (noOtherState) {
                BridgeLog.atomicWriteText(ctx.getFilesDir(),
                        INIT_MARKER_NAME, String.valueOf(times[0]));
                int start = REQUEST_CODE_MIN + new SecureRandom()
                        .nextInt(EPOCH_MAX - REQUEST_CODE_MIN + 1);
                BridgeLog.atomicWriteText(ctx.getFilesDir(), COUNTER_NAME,
                        String.valueOf(start));
            }
            // Any failed condition is abnormal: do nothing; the
            // picker launch refuses and never auto-resets to 1000.
            return;
        }
        // Every other combination (marker corrupt/unreadable, or
        // counter missing/corrupt/unreadable while the marker
        // exists) is abnormal: do nothing.
    }

    /** Picker-launch gate: true only when the marker and the
     * counter are both present, readable, and the marker's
     * firstInstallTime matches the current install. */
    private static boolean checkDriveInitOk(Context ctx) {
        long[] times = firstInstallTimes(ctx);
        if (times == null) {
            return false;
        }
        DecimalRead marker = readDecimalFile(ctx, INIT_MARKER_NAME);
        DecimalRead counter = readDecimalFile(ctx, COUNTER_NAME);
        return marker.kind == ReadKind.OK && counter.kind == ReadKind.OK
                && marker.value == times[0];
    }

    /**
     * Allocate a requestCode from the persistent counter. The
     * reservation is persisted FIRST, then registered in the map
     * — a code is never reused, even when the picker launch
     * fails afterwards. Returns -1 on refusal (counter missing,
     * corrupt, unreadable, out of range, or the reservation
     * could not be persisted).
     */
    private static int allocateRequestCode(Context ctx, long opId, int kind) {
        synchronized (SAVE_LOCK) {
            DecimalRead c = readDecimalFile(ctx, COUNTER_NAME);
            if (c.kind != ReadKind.OK) {
                return -1;
            }
            long next = c.value;
            if (next < REQUEST_CODE_MIN || next > REQUEST_CODE_MAX) {
                return -1;
            }
            if (!BridgeLog.atomicWriteText(ctx.getFilesDir(), COUNTER_NAME,
                    String.valueOf(next + 1))) {
                return -1;
            }
            int code = (int) next;
            requestCodeToOpId.put(code, new PickerRequest(opId,
                    SystemClock.elapsedRealtime(), kind));
            currentPickerRequestCode = code;
            return code;
        }
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02 / DRIVE-03: export (auto-save or
    // fallback picker).
    // ------------------------------------------------------------------

    private void exportLog() {
        long opId = beginOperation(OpState.IDLE, OpState.WRITING);
        if (opId < 0) {
            return; // busy (double-tap guard)
        }
        setButtonsEnabled(false);
        // Snapshot the log at save start, so a background update
        // cannot change the output mid-write.
        String snapshot = BridgeLog.renderText(
                AppVersion.name(this), AppVersion.code(this));
        FolderRead folder = readFolderUri();
        if (folder.kind == FolderKind.ERROR) {
            endOperation(opId);
            setButtonsEnabled(true);
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                    Toast.LENGTH_LONG).show();
            return;
        }
        if (folder.kind == FolderKind.MISSING) {
            // Fallback: end this operation and start a picker one.
            endOperation(opId);
            long pickOp = beginOperation(OpState.IDLE,
                    OpState.PICKER_PENDING);
            if (pickOp < 0) {
                setButtonsEnabled(true);
                return;
            }
            int code = allocateRequestCode(this, pickOp,
                    KIND_FALLBACK_EXPORT);
            if (code < 0) {
                endOperation(pickOp);
                setButtonsEnabled(true);
                return;
            }
            Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("text/plain");
            intent.putExtra(Intent.EXTRA_TITLE, fallbackFileName());
            try {
                startActivityForResult(intent, code);
            } catch (Exception ignored) {
                removePickerRequest(code);
                endOperation(pickOp);
                setButtonsEnabled(true);
            }
            return;
        }
        writeLogToUriInBackground(Uri.parse(folder.uri), snapshot, opId,
                true);
    }

    private static String fallbackFileName() {
        String ts = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .format(new Date());
        return "bridge-log-" + ts + ".txt";
    }

    private static String autoSaveFileName() {
        String ts = new SimpleDateFormat("yyyyMMdd-HHmmss-SSS", Locale.US)
                .format(new Date());
        String uuid = UUID.randomUUID().toString().replace("-", "")
                .substring(0, 6);
        return "bridge-log-" + ts + "-" + uuid + ".txt";
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode,
            Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        PickerRequest req;
        // The whole check sequence runs in one SAVE_LOCK section,
        // so the timeout path and the result callback cannot race
        // each other into a double release or a wrong transition.
        synchronized (SAVE_LOCK) {
            req = requestCodeToOpId.get(requestCode);
            if (req == null) {
                return; // unknown or already invalidated
            }
            if (req.opId != currentOpId) {
                // Stale result: drop only this entry, keep the
                // current operation untouched.
                requestCodeToOpId.remove(requestCode);
                if (currentPickerRequestCode == requestCode) {
                    currentPickerRequestCode = -1;
                }
                return;
            }
            if (state != OpState.PICKER_PENDING) {
                requestCodeToOpId.remove(requestCode);
                if (currentPickerRequestCode == requestCode) {
                    currentPickerRequestCode = -1;
                }
                return;
            }
            if (SystemClock.elapsedRealtime() - req.startedAtElapsed
                    >= PICKER_TIMEOUT_MS) {
                requestCodeToOpId.remove(requestCode);
                currentPickerRequestCode = -1;
                endOperation(req.opId);
                Toast.makeText(this, R.string.toast_picker_timeout,
                        Toast.LENGTH_LONG).show();
                syncUiFromState();
                return;
            }
            requestCodeToOpId.remove(requestCode);
            currentPickerRequestCode = -1;
            if (!transition(OpState.PICKER_PENDING, req.opId,
                    OpState.WRITING)) {
                return;
            }
        }
        if (resultCode != RESULT_OK || data == null
                || data.getData() == null) {
            // User cancelled; the existing folder is unchanged.
            endOperation(req.opId);
            syncUiFromState();
            return;
        }
        if (req.kind == KIND_FOLDER_PICK) {
            handleFolderPicked(data, req.opId);
        } else {
            String snapshot = BridgeLog.renderText(
                    AppVersion.name(this), AppVersion.code(this));
            writeLogToUriInBackground(data.getData(), snapshot, req.opId,
                    false);
        }
    }

    /** SPEC-DRIVE-001 DRIVE-03: the timeout is recovery from an
     * abnormal end, not a completion of the open picker. A late
     * result is ignored because its requestCode is already gone
     * from the map, and the user is asked to pick again. */
    private void checkPickerTimeout() {
        PickerRequest req;
        synchronized (SAVE_LOCK) {
            if (state != OpState.PICKER_PENDING) {
                return;
            }
            int code = currentPickerRequestCode;
            req = code < 0 ? null : requestCodeToOpId.get(code);
            if (req == null) {
                return;
            }
            if (SystemClock.elapsedRealtime() - req.startedAtElapsed
                    < PICKER_TIMEOUT_MS) {
                return;
            }
            requestCodeToOpId.remove(code);
            currentPickerRequestCode = -1;
            endOperation(req.opId);
        }
        Toast.makeText(this, R.string.toast_picker_timeout,
                Toast.LENGTH_LONG).show();
        syncUiFromState();
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-01 / DRIVE-05: pick, change, clear.
    // ------------------------------------------------------------------

    private void pickFolder() {
        if (!checkDriveInitOk(this)) {
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                    Toast.LENGTH_LONG).show();
            return;
        }
        long opId = beginOperation(OpState.IDLE, OpState.PICKER_PENDING);
        if (opId < 0) {
            return;
        }
        setButtonsEnabled(false);
        int code = allocateRequestCode(this, opId, KIND_FOLDER_PICK);
        if (code < 0) {
            endOperation(opId);
            setButtonsEnabled(true);
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                    Toast.LENGTH_LONG).show();
            return;
        }
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        try {
            startActivityForResult(intent, code);
        } catch (Exception ignored) {
            removePickerRequest(code);
            endOperation(opId);
            setButtonsEnabled(true);
        }
    }

    private void handleFolderPicked(Intent data, long opId) {
        Uri treeUri = data.getData();
        int granted = data.getFlags();
        boolean readOk = (granted
                & Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0;
        boolean writeOk = (granted
                & Intent.FLAG_GRANT_WRITE_URI_PERMISSION) != 0;
        boolean persistOk = (granted
                & Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION) != 0;
        if (!readOk || !writeOk || !persistOk) {
            endOperation(opId);
            syncUiFromState();
            Toast.makeText(this, R.string.toast_log_folder_no_write,
                    Toast.LENGTH_LONG).show();
            return;
        }
        if (!isCreatableDirectory(treeUri)) {
            endOperation(opId);
            syncUiFromState();
            Toast.makeText(this, R.string.toast_log_folder_not_creatable,
                    Toast.LENGTH_LONG).show();
            return;
        }
        try {
            getContentResolver().takePersistableUriPermission(treeUri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        } catch (Exception ignored) {
            endOperation(opId);
            syncUiFromState();
            Toast.makeText(this, R.string.toast_log_folder_no_write,
                    Toast.LENGTH_LONG).show();
            return;
        }
        String uriString = treeUri.toString();
        if (!UriPermissionRegistry.register(this,
                UriPermissionRegistry.FEATURE_LOG_FOLDER, uriString)) {
            // Registration failed: the feature is not enabled for
            // this URI; the permission is kept.
            endOperation(opId);
            syncUiFromState();
            Toast.makeText(this, R.string.toast_log_folder_unreadable,
                    Toast.LENGTH_LONG).show();
            return;
        }
        FolderRead old = readFolderUri();
        String oldUri = old.kind == FolderKind.OK ? old.uri : null;
        if (!BridgeLog.atomicWriteText(getFilesDir(), FOLDER_URI_NAME,
                uriString)) {
            handleFolderWriteFailed(uriString, oldUri, opId);
            return;
        }
        // Success: release the old permission only when it is a
        // different folder (re-picking the same folder keeps it).
        if (oldUri != null && !oldUri.equals(uriString)) {
            UriPermissionRegistry.releaseIfUnused(this,
                    UriPermissionRegistry.FEATURE_LOG_FOLDER, oldUri);
        }
        endOperation(opId);
        syncUiFromState();
    }

    /** SPEC-DRIVE-001 DRIVE-05 step 4 (NEW-P2-01): the settings
     * write failed after the new URI was registered. Remove the
     * new registration FIRST; only when that succeeds may the
     * new permission be released. A failed registry removal
     * means the usage is uncertain — the permission is kept. */
    private void handleFolderWriteFailed(String newUri, String oldUri,
            long opId) {
        String stored = readFolderUriString();
        if (stored != null && oldUri != null && stored.equals(oldUri)) {
            // fsync/rename failed: the old file is intact.
            if (!newUri.equals(oldUri)) {
                if (UriPermissionRegistry.unregister(this,
                        UriPermissionRegistry.FEATURE_LOG_FOLDER, newUri)) {
                    releasePermissionBestEffort(newUri);
                }
                // else: keep the new permission (uncertain).
            }
        }
        // Otherwise the rename took effect but the directory sync
        // failed (or the state is unreadable): keep BOTH
        // permissions until the state is certain.
        endOperation(opId);
        syncUiFromState();
        Toast.makeText(this, R.string.toast_log_folder_change_failed,
                Toast.LENGTH_LONG).show();
    }

    private void releasePermissionBestEffort(String uriString) {
        try {
            getContentResolver().releasePersistableUriPermission(
                    Uri.parse(uriString),
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        } catch (Exception ignored) {
        }
    }

    private boolean isCreatableDirectory(Uri treeUri) {
        try {
            String docId = DocumentsContract.getTreeDocumentId(treeUri);
            Uri docUri = DocumentsContract.buildDocumentUriUsingTree(
                    treeUri, docId);
            Cursor c = getContentResolver().query(docUri,
                    new String[]{
                            DocumentsContract.Document.COLUMN_MIME_TYPE,
                            DocumentsContract.Document.COLUMN_FLAGS},
                    null, null, null);
            if (c == null) {
                return false;
            }
            try {
                if (!c.moveToFirst()) {
                    return false;
                }
                String mime = c.getString(0);
                int flags = c.getInt(1);
                return DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)
                        && (flags & DocumentsContract.Document
                                .FLAG_DIR_SUPPORTS_CREATE) != 0;
            } finally {
                c.close();
            }
        } catch (Exception ignored) {
            return false;
        }
    }

    private void clearFolder() {
        long opId = beginOperation(OpState.IDLE, OpState.WRITING);
        if (opId < 0) {
            return;
        }
        setButtonsEnabled(false);
        try {
            doClearFolder();
        } finally {
            endOperation(opId);
            syncUiFromState();
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
    private void doClearFolder() {
        FolderRead folder = readFolderUri();
        if (folder.kind != FolderKind.OK) {
            return; // nothing saved; nothing to clear
        }
        String uriString = folder.uri;
        if (!BridgeLog.atomicWriteText(getFilesDir(), CLEAR_PENDING_NAME,
                uriString)) {
            Toast.makeText(this, R.string.toast_log_folder_clear_failed,
                    Toast.LENGTH_LONG).show();
            return;
        }
        File settings = new File(getFilesDir(), FOLDER_URI_NAME);
        settings.delete();
        BridgeLog.fsyncDirectory(getFilesDir());
        FileState st = statFile(settings);
        if (st == FileState.EXISTS) {
            String stored = readSmallFile(settings);
            if (stored != null && stored.trim().equals(uriString)) {
                deleteClearMarker(); // kind A
            }
            // kind A (content differs) and kind B both keep the
            // permission and the marker.
            return;
        }
        if (st == FileState.UNKNOWN) {
            return; // kind B: keep the permission and the marker
        }
        // kind C: proceed to release.
        boolean released = UriPermissionRegistry.releaseIfUnused(this,
                UriPermissionRegistry.FEATURE_LOG_FOLDER, uriString);
        if (released) {
            deleteClearMarker();
        }
        // else: keep the marker for next-launch reconciliation.
    }

    private void deleteClearMarker() {
        File m = new File(getFilesDir(), CLEAR_PENDING_NAME);
        m.delete();
        BridgeLog.fsyncDirectory(getFilesDir());
    }

    /**
     * SPEC-DRIVE-001 DRIVE-05: next-launch consistency check.
     * Runs in onCreate: reconcile the registry, resolve a
     * leftover clear marker, and warn when a saved folder lost
     * its permission (treated as unselected, file left alone).
     */
    private void startupConsistencyCheck() {
        UriPermissionRegistry.reconcile(this);
        File marker = new File(getFilesDir(), CLEAR_PENDING_NAME);
        if (statFile(marker) == FileState.EXISTS) {
            String markerUri = readSmallFile(marker);
            File settings = new File(getFilesDir(), FOLDER_URI_NAME);
            FileState st = statFile(settings);
            if (st == FileState.EXISTS) {
                // The clear never took effect. If the folder was
                // re-picked after the crashed clear, the marker
                // URI is orphaned — release it when safe.
                String stored = readSmallFile(settings);
                if (stored != null && markerUri != null
                        && !stored.trim().equals(markerUri.trim())) {
                    UriPermissionRegistry.releaseIfUnused(this,
                            UriPermissionRegistry.FEATURE_LOG_FOLDER,
                            markerUri.trim());
                }
                deleteClearMarker();
            } else if (st == FileState.NOT_EXISTS) {
                // ENOENT: the clear was effective on disk.
                if (markerUri != null && !markerUri.trim().isEmpty()) {
                    boolean released = UriPermissionRegistry
                            .releaseIfUnused(this,
                                    UriPermissionRegistry.FEATURE_LOG_FOLDER,
                                    markerUri.trim());
                    if (released) {
                        deleteClearMarker();
                    }
                } else {
                    deleteClearMarker();
                }
            }
            // UNKNOWN: cannot decide; keep the marker.
        }
        FolderRead folder = readFolderUri();
        if (folder.kind == FolderKind.OK
                && !isWritePermissionHeld(folder.uri)) {
            Toast.makeText(this, R.string.toast_log_folder_no_permission,
                    Toast.LENGTH_LONG).show();
        }
    }

    private boolean isWritePermissionHeld(String uriString) {
        // CONS-078 P2-02: a failed check means "not confirmed
        // held" — never a crash, never treated as held.
        try {
            for (UriPermission p : getContentResolver()
                    .getPersistedUriPermissions()) {
                if (p.getUri().toString().equals(uriString)
                        && p.isWritePermission()) {
                    return true;
                }
            }
        } catch (Exception ignored) {
            return false;
        }
        return false;
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02: background write + completion.
    // ------------------------------------------------------------------

    private void writeLogToUriInBackground(final Uri target,
            final String text, final long opId, final boolean autoSave) {
        final Context appCtx = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                boolean ok = false;
                boolean clearFolder = false;
                Uri created = null;
                try {
                    ContentResolver resolver =
                            appCtx.getContentResolver();
                    Uri writeTarget = target;
                    if (autoSave) {
                        String docId = DocumentsContract
                                .getTreeDocumentId(target);
                        Uri parent = DocumentsContract
                                .buildDocumentUriUsingTree(target, docId);
                        created = DocumentsContract.createDocument(resolver,
                                parent, "text/plain", autoSaveFileName());
                        if (created == null) {
                            throw new IllegalStateException(
                                    "createDocument returned null");
                        }
                        writeTarget = created;
                    }
                    OutputStream out =
                            resolver.openOutputStream(writeTarget);
                    if (out == null) {
                        throw new IllegalStateException(
                                "openOutputStream returned null");
                    }
                    try {
                        out.write(text.getBytes(StandardCharsets.UTF_8));
                    } finally {
                        try {
                            out.close();
                        } catch (Exception ignored) {
                        }
                    }
                    ok = true;
                } catch (Exception ignored) {
                    if (autoSave) {
                        if (created != null) {
                            deleteCreatedBestEffort(
                                    appCtx.getContentResolver(), created);
                        }
                        clearFolder = classifyAutoSaveFailure(target);
                    }
                } finally {
                    if (clearFolder) {
                        clearSavedFolderAfterFailure();
                    }
                    endOperation(opId);
                    notifySaveFinished(ok);
                }
            }
        }).start();
    }

    private static void deleteCreatedBestEffort(ContentResolver resolver,
            Uri created) {
        if (created == null) {
            return;
        }
        try {
            DocumentsContract.deleteDocument(resolver, created);
        } catch (Exception ignored) {
            // Must not hide the original error.
        }
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
    private boolean classifyAutoSaveFailure(Uri treeUri) {
        ContentResolver resolver = getApplicationContext()
                .getContentResolver();
        boolean writeHeld = false;
        // CONS-078 P2-02: a failed check must NOT clear the
        // folder — failure is not "permission absent". Keep it.
        try {
            for (UriPermission p : resolver.getPersistedUriPermissions()) {
                if (p.getUri().toString().equals(treeUri.toString())
                        && p.isWritePermission()) {
                    writeHeld = true;
                    break;
                }
            }
        } catch (Exception ignored) {
            return false;
        }
        if (!writeHeld) {
            return true;
        }
        String docId;
        try {
            docId = DocumentsContract.getTreeDocumentId(treeUri);
        } catch (Exception ignored) {
            return true;
        }
        if (docId == null) {
            return true;
        }
        Uri parent = DocumentsContract.buildDocumentUriUsingTree(
                treeUri, docId);
        int emptyCount = 0;
        for (int i = 0; i < 2; i++) {
            if (i > 0) {
                try {
                    Thread.sleep(500);
                } catch (InterruptedException ignored) {
                }
            }
            Cursor c = null;
            try {
                c = resolver.query(parent,
                        new String[]{
                                DocumentsContract.Document.COLUMN_DOCUMENT_ID},
                        null, null, null);
                if (c == null) {
                    return false; // keep: no answer
                }
                if (!c.moveToFirst()) {
                    emptyCount++;
                } else {
                    return false; // parent exists: keep
                }
            } catch (Exception ignored) {
                return false; // keep on any error
            } finally {
                if (c != null) {
                    c.close();
                }
            }
        }
        return emptyCount == 2;
    }

    /** Remove a verified-dead saved folder (file + registry).
     * Runs on the background thread; the UI resyncs via the
     * completion notification. */
    private void clearSavedFolderAfterFailure() {
        String uri = readFolderUriString();
        if (uri != null) {
            UriPermissionRegistry.releaseIfUnused(
                    getApplicationContext(),
                    UriPermissionRegistry.FEATURE_LOG_FOLDER, uri);
        }
        File f = new File(getFilesDir(), FOLDER_URI_NAME);
        f.delete();
        BridgeLog.fsyncDirectory(getFilesDir());
    }

    /** Notify the currently resumed activity (if any) that the
     * save finished. The notified side re-reads the static state
     * instead of trusting the notification alone. When no
     * activity is resumed, nothing is sent — the next onResume
     * resyncs the UI from the state. */
    private void notifySaveFinished(final boolean ok) {
        LogActivity activity = resumedActivity == null
                ? null : resumedActivity.get();
        if (activity == null) {
            return;
        }
        final LogActivity target = activity;
        target.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (target.isFinishing() || target.isDestroyed()) {
                    return;
                }
                target.syncUiFromState();
                Toast.makeText(target,
                        ok ? R.string.toast_log_saved
                                : R.string.toast_log_save_failed,
                        Toast.LENGTH_LONG).show();
            }
        });
    }

    // ------------------------------------------------------------------
    // SPEC-DRIVE-001 DRIVE-02 / DRIVE-04: UI sync + folder display.
    // ------------------------------------------------------------------

    private void syncUiFromState() {
        boolean busy = snapshotState() != OpState.IDLE;
        setButtonsEnabled(!busy);
        refreshFolderDisplay();
    }

    private void setButtonsEnabled(boolean enabled) {
        if (refreshButton != null) {
            refreshButton.setEnabled(enabled);
        }
        if (exportButton != null) {
            exportButton.setEnabled(enabled);
        }
        if (folderButton != null) {
            folderButton.setEnabled(enabled);
        }
        if (folderClearButton != null) {
            folderClearButton.setEnabled(enabled);
        }
    }

    private FolderRead readFolderUri() {
        File f = new File(getFilesDir(), FOLDER_URI_NAME);
        FileState st = statFile(f);
        if (st == FileState.NOT_EXISTS) {
            return new FolderRead(FolderKind.MISSING, null);
        }
        if (st == FileState.UNKNOWN) {
            return new FolderRead(FolderKind.ERROR, null);
        }
        String content = readSmallFile(f);
        if (content == null) {
            return new FolderRead(FolderKind.ERROR, null);
        }
        String uri = content.trim();
        if (uri.isEmpty()) {
            return new FolderRead(FolderKind.ERROR, null);
        }
        try {
            DocumentsContract.getTreeDocumentId(Uri.parse(uri));
        } catch (Exception ignored) {
            return new FolderRead(FolderKind.ERROR, null);
        }
        return new FolderRead(FolderKind.OK, uri);
    }

    private String readFolderUriString() {
        FolderRead folder = readFolderUri();
        return folder.kind == FolderKind.OK ? folder.uri : null;
    }

    private void refreshFolderDisplay() {
        if (folderText == null) {
            return;
        }
        FolderRead folder = readFolderUri();
        if (folder.kind != FolderKind.OK
                || !isWritePermissionHeld(folder.uri)) {
            folderText.setText(R.string.log_folder_unselected);
            return;
        }
        String name = resolveFolderDisplayName(Uri.parse(folder.uri));
        folderText.setText(name != null ? name
                : getString(R.string.log_folder_name_unknown));
    }

    /** Display name via COLUMN_DISPLAY_NAME; the raw URI is never
     * shown on screen. Null when it cannot be read. */
    private String resolveFolderDisplayName(Uri treeUri) {
        try {
            String docId = DocumentsContract.getTreeDocumentId(treeUri);
            Uri docUri = DocumentsContract.buildDocumentUriUsingTree(
                    treeUri, docId);
            Cursor c = getContentResolver().query(docUri,
                    new String[]{
                            DocumentsContract.Document.COLUMN_DISPLAY_NAME},
                    null, null, null);
            if (c == null) {
                return null;
            }
            try {
                if (!c.moveToFirst()) {
                    return null;
                }
                int idx = c.getColumnIndex(
                        DocumentsContract.Document.COLUMN_DISPLAY_NAME);
                if (idx < 0) {
                    return null;
                }
                return c.getString(idx);
            } finally {
                c.close();
            }
        } catch (Exception ignored) {
            return null;
        }
    }
}
