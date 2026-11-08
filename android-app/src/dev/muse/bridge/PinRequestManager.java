// Implemented by claude-sonnet-5-5 (medium) — SPEC-A11Y-OFF-001, 2026-11-08
package dev.muse.bridge;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.drawable.Icon;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * SPEC-A11Y-OFF-001 (v2.9.3) section 3.1: Application-scoped static
 * singleton that owns the shortcut-creation request state, so it
 * outlives any Activity instance.
 *
 * Four independent states: uiState, workerRunning, unresolvedState,
 * pauseObserved / resumeObserved. Every read, write and transition
 * happens on the main thread (worker results arrive via the main
 * Handler). The only exception, spelled out in the spec, is the
 * worker's finally setting workerRunning = false directly when the
 * Error-specific recovery event itself cannot be posted.
 */
public final class PinRequestManager {

    public enum UiState { IDLE, REQUESTING, WAITING_CONFIRMATION }

    public enum UnresolvedState { NONE, REQUESTING_UNRESOLVED, CONFIRMATION_UNRESOLVED }

    /** Known outcome carried by the worker's events. */
    enum Outcome { KNOWN_TRUE, KNOWN_FALSE, KNOWN_EXCEPTION, UNKNOWN }

    /** Notified on the main thread when the state changed. */
    public interface Listener {
        void onPinStateChanged();
    }

    static final String SHORTCUT_ID = "a11y_off";
    static final long REQUESTING_TIMEOUT_MS = 30_000L;
    static final long WAITING_TIMEOUT_MS = 30_000L;
    static final long RELEASE_TIMEOUT_MS = 60_000L;

    private static final Handler HANDLER = new Handler(Looper.getMainLooper());
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    // ---- state (main thread only, except workerRunning) ----
    private static UiState uiState = UiState.IDLE;
    private static final AtomicBoolean workerRunning = new AtomicBoolean(false);
    private static UnresolvedState unresolvedState = UnresolvedState.NONE;
    private static boolean pauseObserved = false;
    private static boolean resumeObserved = false;

    private static int requestGeneration = 0;
    /** Generation whose result / recovery event was already processed. */
    private static int processedGeneration = 0;
    private static boolean releaseTimerRunning = false;
    private static Runnable requestingTimer;
    private static Runnable waitingTimer;
    private static Runnable releaseTimer;
    private static Listener listener;
    private static Context appContext;

    private PinRequestManager() {
    }

    // ---- accessors for the UI (main thread) ----

    public static UiState getUiState() {
        return uiState;
    }

    public static boolean isWorkerRunning() {
        return workerRunning.get();
    }

    public static UnresolvedState getUnresolvedState() {
        return unresolvedState;
    }

    public static boolean isPauseObserved() {
        return pauseObserved;
    }

    public static boolean isResumeObserved() {
        return resumeObserved;
    }

    /** The button is enabled only when uiState == IDLE. */
    public static boolean isButtonEnabled() {
        return uiState == UiState.IDLE;
    }

    public static void setListener(Listener l) {
        listener = l;
    }

    public static void clearListener(Listener l) {
        if (listener == l) {
            listener = null;
        }
    }

    // ---- lifecycle record (reference only, never a release condition) ----

    public static void onActivityPaused() {
        if (isConfirmationWaiting()) {
            pauseObserved = true;
        }
    }

    public static void onActivityResumed() {
        if (isConfirmationWaiting() && pauseObserved) {
            resumeObserved = true;
        }
    }

    private static boolean isConfirmationWaiting() {
        return uiState == UiState.WAITING_CONFIRMATION
                || unresolvedState == UnresolvedState.CONFIRMATION_UNRESOLVED;
    }

    // ---- press handler (main thread) ----

    /** Handles a press of the in-app create button (section 3.1 steps 1-3). */
    public static void requestPin(Context ctx) {
        Context app = ctx.getApplicationContext();
        appContext = app;
        if (uiState != UiState.IDLE) {
            return;
        }
        if (workerRunning.get() || unresolvedState != UnresolvedState.NONE) {
            toast(R.string.toast_pin_unresolved);
            return;
        }
        ShortcutManager sm = null;
        ShortcutInfo info = null;
        try {
            sm = app.getSystemService(ShortcutManager.class);
            if (sm == null || !sm.isRequestPinShortcutSupported()) {
                toast(R.string.toast_pin_failed);
                return;
            }
            String token = MainActivity.getOrCreateA11yOffToken(app);
            if (token == null) {
                toast(R.string.toast_pin_failed);
                return;
            }
            info = buildShortcutInfo(app, token);
        } catch (Exception e) {
            toast(R.string.toast_pin_failed);
            return;
        }

        requestGeneration++;
        final int gen = requestGeneration;
        uiState = UiState.REQUESTING;
        workerRunning.set(true);
        unresolvedState = UnresolvedState.NONE;
        pauseObserved = false;
        resumeObserved = false;
        cancelWaitingTimer();
        cancelReleaseTimer();
        armRequestingTimer(gen);
        notifyListener();
        try {
            EXECUTOR.execute(new PinWorker(gen, sm, info));
        } catch (Exception e) {
            // Executor rejection or an exception right after startup.
            cancelRequestingTimer();
            cancelWaitingTimer();
            cancelReleaseTimer();
            workerRunning.set(false);
            uiState = UiState.IDLE;
            unresolvedState = UnresolvedState.NONE;
            notifyListener();
            toast(R.string.toast_pin_failed);
        }
    }

    private static ShortcutInfo buildShortcutInfo(Context app, String token) {
        Intent intent = new Intent(MainActivity.ACTION_DISABLE_A11Y);
        intent.setClass(app, MainActivity.class);
        intent.putExtra(MainActivity.EXTRA_DISABLE_TOKEN, token);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_CLEAR_TOP
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return new ShortcutInfo.Builder(app, SHORTCUT_ID)
                .setShortLabel(app.getString(R.string.a11y_off_short_label))
                .setLongLabel(app.getString(R.string.a11y_off_long_label))
                .setIcon(Icon.createWithResource(app, R.drawable.ic_tile))
                .setIntent(intent)
                .build();
    }

    // ---- worker ----

    private static final class PinWorker implements Runnable {
        private final int gen;
        private final ShortcutManager sm;
        private final ShortcutInfo info;

        PinWorker(int gen, ShortcutManager sm, ShortcutInfo info) {
            this.gen = gen;
            this.sm = sm;
            this.info = info;
        }

        @Override
        public void run() {
            boolean resultEventPosted = false;
            Outcome known = Outcome.UNKNOWN;
            try {
                // The known outcome is recorded when the API returns or
                // the exception is caught, BEFORE any post is attempted.
                try {
                    boolean ok = sm.requestPinShortcut(info, null);
                    known = ok ? Outcome.KNOWN_TRUE : Outcome.KNOWN_FALSE;
                } catch (Exception e) {
                    known = Outcome.KNOWN_EXCEPTION;
                }
                // A fatal VM Error is not caught: it skips this post and
                // is rethrown after finally.
                final Outcome carried = known;
                try {
                    resultEventPosted = HANDLER.post(new Runnable() {
                        @Override
                        public void run() {
                            onResultEvent(carried);
                        }
                    });
                } catch (Exception ignored) {
                    resultEventPosted = false;
                }
            } finally {
                if (!resultEventPosted) {
                    final Outcome carried = known;
                    boolean posted = false;
                    try {
                        posted = HANDLER.post(new Runnable() {
                            @Override
                            public void run() {
                                onRecoveryEvent(gen, carried);
                            }
                        });
                    } catch (Exception ignored) {
                        posted = false;
                    } finally {
                        if (!posted) {
                            // Exception to main-thread serialization (spec).
                            workerRunning.set(false);
                        }
                    }
                }
            }
        }
    }

    // ---- events (main thread) ----

    /** Normal result event: late results are NOT discarded. */
    private static void onResultEvent(Outcome outcome) {
        processedGeneration = requestGeneration;
        applyOutcome(outcome);
    }

    /** Error-specific recovery event: generation-checked, applied once. */
    private static void onRecoveryEvent(int gen, Outcome outcome) {
        if (gen != requestGeneration || processedGeneration == gen) {
            return;
        }
        processedGeneration = gen;
        applyOutcome(outcome);
    }

    private static void applyOutcome(Outcome outcome) {
        final int gen = requestGeneration;
        cancelRequestingTimer();
        switch (outcome) {
            case KNOWN_FALSE:
            case KNOWN_EXCEPTION:
                workerRunning.set(false);
                uiState = UiState.IDLE;
                unresolvedState = UnresolvedState.NONE;
                cancelWaitingTimer();
                cancelReleaseTimer();
                notifyListener();
                toast(R.string.toast_pin_failed);
                break;
            case KNOWN_TRUE:
                workerRunning.set(false);
                pauseObserved = false;
                resumeObserved = false;
                unresolvedState = UnresolvedState.CONFIRMATION_UNRESOLVED;
                if (uiState == UiState.REQUESTING) {
                    uiState = UiState.WAITING_CONFIRMATION;
                    armWaitingTimer(gen);
                } else {
                    // Late true after the REQUESTING timeout: the UI is
                    // already IDLE; only the 60s release timer runs.
                    startReleaseTimerOnce(gen);
                }
                notifyListener();
                break;
            case UNKNOWN:
            default:
                workerRunning.set(false);
                if (uiState == UiState.REQUESTING) {
                    uiState = UiState.IDLE;
                }
                unresolvedState = UnresolvedState.CONFIRMATION_UNRESOLVED;
                startReleaseTimerOnce(gen);
                notifyListener();
                break;
        }
    }

    // ---- timers (main thread, generation-checked) ----

    private static void armRequestingTimer(final int gen) {
        cancelRequestingTimer();
        requestingTimer = new Runnable() {
            @Override
            public void run() {
                onRequestingTimeout(gen);
            }
        };
        HANDLER.postDelayed(requestingTimer, REQUESTING_TIMEOUT_MS);
    }

    private static void cancelRequestingTimer() {
        if (requestingTimer != null) {
            HANDLER.removeCallbacks(requestingTimer);
            requestingTimer = null;
        }
    }

    private static void onRequestingTimeout(int gen) {
        if (gen != requestGeneration || processedGeneration == gen) {
            return;
        }
        if (workerRunning.get()) {
            // Normal timeout transition; the guard re-arms every 30s
            // while the worker is still running.
            if (uiState == UiState.REQUESTING) {
                uiState = UiState.IDLE;
            }
            unresolvedState = UnresolvedState.REQUESTING_UNRESOLVED;
            armRequestingTimer(gen);
            notifyListener();
        } else {
            // workerRunning == false and no event processed: only the
            // emergency direct fallback can cause this. Converge to the
            // Error-recovery terminal state and stop re-arming.
            requestingTimer = null;
            uiState = UiState.IDLE;
            unresolvedState = UnresolvedState.CONFIRMATION_UNRESOLVED;
            startReleaseTimerOnce(gen);
            notifyListener();
        }
    }

    private static void armWaitingTimer(final int gen) {
        cancelWaitingTimer();
        waitingTimer = new Runnable() {
            @Override
            public void run() {
                waitingTimer = null;
                if (gen != requestGeneration || uiState != UiState.WAITING_CONFIRMATION) {
                    return;
                }
                // Restore the UI but keep CONFIRMATION_UNRESOLVED; release
                // it a further 60s later.
                uiState = UiState.IDLE;
                startReleaseTimerOnce(gen);
                notifyListener();
            }
        };
        HANDLER.postDelayed(waitingTimer, WAITING_TIMEOUT_MS);
    }

    private static void cancelWaitingTimer() {
        if (waitingTimer != null) {
            HANDLER.removeCallbacks(waitingTimer);
            waitingTimer = null;
        }
    }

    /** Starts the 60s release timer only if it is not already running. */
    private static void startReleaseTimerOnce(final int gen) {
        if (releaseTimerRunning) {
            return;
        }
        releaseTimerRunning = true;
        releaseTimer = new Runnable() {
            @Override
            public void run() {
                if (gen != requestGeneration) {
                    return;
                }
                releaseTimerRunning = false;
                releaseTimer = null;
                unresolvedState = UnresolvedState.NONE;
                notifyListener();
            }
        };
        HANDLER.postDelayed(releaseTimer, RELEASE_TIMEOUT_MS);
    }

    private static void cancelReleaseTimer() {
        if (releaseTimer != null) {
            HANDLER.removeCallbacks(releaseTimer);
            releaseTimer = null;
        }
        releaseTimerRunning = false;
    }

    // ---- helpers ----

    private static void notifyListener() {
        Listener l = listener;
        if (l != null) {
            l.onPinStateChanged();
        }
    }

    private static void toast(int resId) {
        if (appContext != null) {
            Toast.makeText(appContext, resId, Toast.LENGTH_LONG).show();
        }
    }
}
