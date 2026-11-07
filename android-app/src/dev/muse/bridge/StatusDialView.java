package dev.muse.bridge;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.util.AttributeSet;
import android.util.TypedValue;
import android.view.View;

/**
 * Status dial for the Muse Bridge main screen (SPEC-UI-002, v2.7.0).
 *
 * A ring plus the state word in the centre, with the effective mode
 * as a small sub-line. The ring is a status indicator, not a gauge:
 * it is always a full circle — green while the server is running,
 * red while it is stopped — and the state word is always shown as
 * text too, so colour never carries information alone (SPEC-UI-001
 * rule). The mode sub-line shows the effective mode in short form
 * (full access / minimal / banking safe).
 *
 * Colours come from colors.xml (status_ok / status_bad). Text comes
 * from strings.xml (status_running / status_stopped). No new
 * dependency: plain Canvas drawing, no library.
 */
public class StatusDialView extends View {

    private static final float RING_WIDTH_DP = 16f;
    private static final float STATE_TEXT_SP = 28f;
    private static final float MODE_TEXT_SP = 13f;
    private static final float DEFAULT_SIZE_DP = 200f;

    private final Paint ringPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint trackPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint statePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint modePaint = new Paint(Paint.ANTI_ALIAS_FLAG);

    private boolean running = false;
    private String modeLabel = "";

    public StatusDialView(Context context) {
        super(context);
        init();
    }

    public StatusDialView(Context context, AttributeSet attrs) {
        super(context, attrs);
        init();
    }

    private void init() {
        float ringWidth = dp(RING_WIDTH_DP);
        ringPaint.setStyle(Paint.Style.STROKE);
        ringPaint.setStrokeWidth(ringWidth);
        ringPaint.setStrokeCap(Paint.Cap.ROUND);
        trackPaint.setStyle(Paint.Style.STROKE);
        trackPaint.setStrokeWidth(ringWidth);
        trackPaint.setColor(0xFFE0E0E0);
        statePaint.setTextAlign(Paint.Align.CENTER);
        statePaint.setFakeBoldText(true);
        statePaint.setTextSize(sp(STATE_TEXT_SP));
        modePaint.setTextAlign(Paint.Align.CENTER);
        modePaint.setTextSize(sp(MODE_TEXT_SP));
        modePaint.setColor(0xFF757575);
    }

    /** Server state: true while BridgeService is running. */
    public void setRunning(boolean running) {
        if (this.running != running) {
            this.running = running;
            invalidate();
        }
    }

    /** Effective mode in short form (e.g. full access / minimal /
     * banking safe). Shown as the small sub-line under the state
     * word; empty means no sub-line. */
    public void setModeLabel(String modeLabel) {
        String label = modeLabel == null ? "" : modeLabel;
        if (!label.equals(this.modeLabel)) {
            this.modeLabel = label;
            invalidate();
        }
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int def = (int) dp(DEFAULT_SIZE_DP);
        int w = resolveSize(def, widthMeasureSpec);
        int h = resolveSize(def, heightMeasureSpec);
        int size = Math.min(w, h);
        setMeasuredDimension(size, size);
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float cx = getWidth() / 2f;
        float cy = getHeight() / 2f;
        float radius = Math.min(cx, cy) - ringPaint.getStrokeWidth() / 2f - dp(4f);

        int stateColor = getResources().getColor(
                running ? R.color.status_ok : R.color.status_bad, null);
        canvas.drawCircle(cx, cy, radius, trackPaint);
        ringPaint.setColor(stateColor);
        canvas.drawCircle(cx, cy, radius, ringPaint);

        String state = getContext().getString(
                running ? R.string.status_running : R.string.status_stopped);
        statePaint.setColor(stateColor);
        Paint.FontMetrics fm = statePaint.getFontMetrics();
        float stateY = cy - (fm.ascent + fm.descent) / 2f
                - (modeLabel.isEmpty() ? 0f : sp(MODE_TEXT_SP) * 0.7f);
        canvas.drawText(state, cx, stateY, statePaint);
        if (!modeLabel.isEmpty()) {
            Paint.FontMetrics mfm = modePaint.getFontMetrics();
            float modeY = stateY + (fm.descent - fm.ascent) * 0.5f
                    + (mfm.descent - mfm.ascent) * 0.5f + sp(6f);
            canvas.drawText(modeLabel, cx, modeY, modePaint);
        }
    }

    private float dp(float value) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP,
                value, getResources().getDisplayMetrics());
    }

    private float sp(float value) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP,
                value, getResources().getDisplayMetrics());
    }
}
