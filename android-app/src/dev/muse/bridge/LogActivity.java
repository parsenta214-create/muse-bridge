package dev.muse.bridge;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * On-phone debug log viewer (v1.9.4). Works fully offline: it reads
 * the ring buffer directly, no network involved. The .txt export
 * uses the system file picker (Storage Access Framework), so no
 * storage permission is needed — the user picks where the file
 * goes (e.g. Downloads) and can open or share it from there.
 */
public class LogActivity extends Activity {

    private static final int REQUEST_EXPORT = 1;

    private TextView logText;
    private ScrollView scrollView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_log);

        logText = findViewById(R.id.log_text);
        scrollView = findViewById(R.id.log_scroll);

        Button refresh = findViewById(R.id.log_refresh_button);
        refresh.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                refresh();
            }
        });

        Button export = findViewById(R.id.log_export_button);
        export.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                exportLog();
            }
        });

        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
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

    private void exportLog() {
        String ts = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .format(new Date());
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("text/plain");
        intent.putExtra(Intent.EXTRA_TITLE, "bridge-log-" + ts + ".txt");
        startActivityForResult(intent, REQUEST_EXPORT);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode,
            Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_EXPORT || resultCode != RESULT_OK
                || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        try {
            OutputStream out = getContentResolver().openOutputStream(uri);
            if (out == null) {
                throw new IllegalStateException("cannot open " + uri);
            }
            out.write(BridgeLog.renderText(
                    AppVersion.name(this), AppVersion.code(this))
                    .getBytes("UTF-8"));
            out.close();
            Toast.makeText(this, R.string.toast_log_saved,
                    Toast.LENGTH_LONG).show();
        } catch (Exception e) {
            Toast.makeText(this, R.string.toast_log_save_failed,
                    Toast.LENGTH_LONG).show();
        }
    }
}
