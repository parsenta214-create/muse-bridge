# Computer Use Preview

## Quick Start

This section will guide you through setting up and running Gemini Computer Use, using either the Gemini Developer API or Vertex AI. Follow these steps to get started.

### 1. Installation

**Clone the Repository**

```bash
git clone https://github.com/google-gemini/computer-use-preview.git
cd computer-use-preview
```

**Set up Python Virtual Environment and Install Dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Install Playwright and Browser Dependencies**

```bash
# Install system dependencies required by Playwright for Chrome
playwright install-deps chrome

# Install the Chrome browser for Playwright
playwright install chrome
```

### 2. Configuration
You can get started using either the Gemini Developer API or Vertex AI.

#### A. If using the Gemini Developer API:

You need a Gemini API key to use the agent:

```bash
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
```

Or to add this to your virtual environment:

```bash
echo 'export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"' >> .venv/bin/activate
# After editing, you'll need to deactivate and reactivate your virtual
# environment if it's already active:
deactivate
source .venv/bin/activate
```

Replace `YOUR_GEMINI_API_KEY` with your actual key.

#### B. If using the Vertex AI Client:

You need to explicitly use Vertex AI, then provide project and location to use the agent:

```bash
export USE_VERTEXAI=true
export VERTEXAI_PROJECT="YOUR_PROJECT_ID"
export VERTEXAI_LOCATION="YOUR_LOCATION"
```

Or to add this to your virtual environment:

```bash
echo 'export USE_VERTEXAI=true' >> .venv/bin/activate
echo 'export VERTEXAI_PROJECT="your-project-id"' >> .venv/bin/activate
echo 'export VERTEXAI_LOCATION="your-location"' >> .venv/bin/activate
# After editing, you'll need to deactivate and reactivate your virtual
# environment if it's already active:
deactivate
source .venv/bin/activate
```

Replace `YOUR_PROJECT_ID` and `YOUR_LOCATION` with your actual project and location.

#### C. Using a `.env` file (Recommended for local development):

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Then open `.env` and replace the placeholder values with your actual API keys. The project will automatically load this file on startup — no need to manually export environment variables each session.

### 3. Running the Tool

The primary way to use the tool is via the `main.py` script.

**General Command Structure:**

```bash
python main.py --query "Go to Google and type 'Hello World' into the search bar"
```

**Available Environments:**

You can specify a particular environment with the ```--env <environment>``` flag.  Available options:

- `playwright`: Runs the browser locally using Playwright.
- `browserbase`: Connects to a Browserbase instance.

**Local Playwright**

Runs the agent using a Chrome browser instance controlled locally by Playwright.

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="playwright"
```

You can also specify an initial URL for the Playwright environment:

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="playwright" --initial_url="https://www.google.com/search?q=latest+AI+news"
```

**Browserbase**

Runs the agent using Browserbase as the browser backend. Ensure the proper Browserbase environment variables are set:`BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`.

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="browserbase"
```

**Available Models:**

You can choose the model to use by specifying the ```--model <model name>``` flag. Available options on Gemini Developer API and Vertex AI Client:

- `gemini-3.6-flash`: This is the default model.
- `gemini-3.5-flash-lite`: A low-latency, cost-efficient model with computer use support.
- `gemini-3.5-flash`: An earlier Gemini 3.5 Flash model with computer use support.
- `gemini-2.5-computer-use-preview-10-2025`: An earlier computer use preview model.
- `gemini-3-flash-preview`: The preview version of Gemini 3 Flash.

## Agent CLI

The `main.py` script is the command-line interface (CLI) for running the browser agent.

### Command-Line Arguments

| Argument | Description | Required | Default | Supported Environment(s) |
|-|-|-|-|-|
| `--query` | The natural language query for the browser agent to execute. | Yes | N/A | All |
| `--env` | The computer use environment to use. Must be one of the following: `playwright`, or `browserbase` | No | N/A | All |
| `--initial_url` | The initial URL to load when the browser starts. | No | https://www.google.com | All |
| `--highlight_mouse` | If specified, the agent will attempt to highlight the mouse cursor's position in the screenshots. This is useful for visual debugging. | No | False (not highlighted) | `playwright` |
| `--model` | The model to use. See the "Available Models" section for more information. | No | `gemini-3.6-flash` | All |

### Environment Variables

| Variable | Description | Required |
|-|-|-|
| GEMINI_API_KEY | Your API key for the Gemini model. | Yes |
| BROWSERBASE_API_KEY | Your API key for Browserbase. | Yes (when using the browserbase environment) |
| BROWSERBASE_PROJECT_ID | Your Project ID for Browserbase. | Yes (when using the browserbase environment) |

## Known Issues

### Playwright Dropdown Menu

On certain operating systems, the Playwright browser is unable to capture `<select>` elements because they are rendered by the operating system. As a result, the agent is unable to send the correct screenshot to the model.

There are several ways to mitigate this.

1. Use the Browserbase option instead of Playwright.
2. Inject a script like [proxy-select](https://github.com/amitamb/proxy-select) to render a custom `<select>` element. You must inject `proxy-select.css` and `proxy-select.js` into each page that has a non-custom `<select>` element. You can do this in the [`Playwright.__enter__`](https://github.com/google-gemini/computer-use-preview/blob/main/computers/playwright/playwright.py#L100) method by adding a few lines of code, like the following (replacing `PROXY_SELECT_JS` and `PROXY_SELECT_CSS` with the appropriate variables):

```python
self._page.add_init_script(PROXY_SELECT_JS)
def inject_style(page):
    try:
        page.add_style_tag(content=PROXY_SELECT_CSS)
    except Exception as e:
        print(f"Error injecting style: {e}")

self._page.on('domcontentloaded', inject_style)
```

Note, option 2 does not work 100% of the time, but is a temporary workaround for certain websites. The better option is to use Browserbase.

---

## Local trial setup (MUSE Phase 1/2, 2026-11-05)

Local additions on top of the upstream reference implementation, for
running one bounded trial on a low-memory Linux VM. Upstream files are
otherwise unchanged except where noted.

### Layout

- `bootstrap.sh` — idempotent first-time setup (venv, pinned
  requirements, Chromium, `.env`). Safe to re-run.
- `run.sh [query] [model]` — start the relay, run one trial, stop the
  relay, write a timestamped log under `logs/`. If the primary run
  fails, one fresh run with `GEMINI_MODEL_FALLBACK` (default
  `gemini-3.8-flash`) is tried; conversation state is NOT carried over.
- `stop.sh` — stop the relay if it is running.
- `run_trial.py` — fetches the Gemini credential as a surrogate from the
  registered `custom.gemini` connection and runs `main.py` headless.
- `connect_relay.py` — local forward proxy on 127.0.0.1:3129 (see below).
- `retry.py` — exponential backoff with full jitter for API calls
  (Phase 2). Retries only 429 and 500/502/503/504; 400/401/403/404 fail
  fast. Honors Retry-After when present.
- `computers/page_manager.py` — Page Manager (Phase 3): multiple tabs
  in one BrowserContext, each with a stable ID (`page_001`, ...).
  Popups are registered and become active; the cap is 3 tabs
  (`MAX_PAGES`); the registry is included in every action result.
- `diagnostics/` — scratch scripts from the proxy investigation
  (`proxy_diag.py`, `mitm_diag.py`, `mitm_diag2.py`) and the scripted
  live Page Manager test (`test_page_manager_live.py`, no model).
- `test_page_manager.py`, `test_agent_pages.py` — unit tests for the
  Page Manager and for the agent-side page functions.
- `logs/` — per-run logs (`run-<timestamp>-primary.log`, plus
  `-fallback.log` when used). Ignored by git.
- `.env` — non-secret settings only (model, fallback model, initial
  URL, relay port, default query, page cap). The API key is NEVER stored here.

### Phase 3 notes

- `computers/playwright/playwright.py` now routes every action through
  the Page Manager's active page (`_active_page` property). The old
  `_handle_new_page` (overwrite the current page on a popup) is gone:
  a popup is registered as a new page and becomes active, and the
  previous page stays open.
- The model gets two custom functions, declared only when the computer
  supports pages (`supports_pages` on the `Computer` base class, True
  for Playwright): `switch_page(page_id)` and `open_new_page(url)`.
  Both return the new active page's state, so the model sees that
  page's screenshot next. The Computer Use tool itself has no custom
  page action; these are plain function declarations handled by the
  agent, which is what the reference implementation's custom-function
  mechanism is for.
- Every action result includes the tab registry (`pages`: `[{id, url,
  active}]` plus `active_page_id`), and the first user message gains a
  short page-management instruction when pages are supported, so the
  model learns the page IDs without the caller having to explain them.
- `EnvState` gained optional `active_page_id` / `pages` fields (None
  for environments without page management). Screenshots of
  `switch_page` / `open_new_page` turns are pruned like the predefined
  Computer Use turns.

### Spec decisions (frozen 2026-11-05)

Saardeim delegated spec decisions up to spec freeze to Muse for this
Phase 1/2 work. The decisions below are the frozen spec for this
project; change them only with a new explicit decision.

### Local patches (two)

1. `computers/playwright/playwright.py`: Chromium is launched with
   `--proxy-server=http://127.0.0.1:3129` and
   `--ignore-certificate-errors`, because Chromium in this VM cannot
   CONNECT directly to the egress proxy (`ERR_TUNNEL_CONNECTION_FAILED`)
   while a plain-socket CONNECT succeeds. The relay forwards each
   CONNECT unchanged to the egress proxy.
2. `run_trial.py`: trims `NO_PROXY`/`no_proxy` to `localhost,127.0.0.1`
   because httpx (used by the google-genai SDK) fails to parse the
   bracketed IPv6 entries in this VM's `NO_PROXY` list.

### How to run a trial

```bash
./bootstrap.sh   # first time, or any time to verify setup
./run.sh         # default task: Google -> search bar -> type 'Hello World'
./run.sh "Go to example.com and tell me the headline" gemini-3.8-flash
./stop.sh        # stop the relay manually if a run was interrupted
```

### Phase 2 notes

- `agent.py` `get_model_response()` now uses `retry.py` (max 5
  attempts, jittered delays 1-2s, 2-4s, 4-8s, 8-16s, 16-32s).
  A failed run ends the loop and `main.py` exits non-zero
  (`agent.last_error`), so `run.sh` can tell success from failure.
- Screenshot refresh on retry is deliberately NOT implemented: in this
  reference flow a model-call failure happens before any browser
  action in that iteration, and the previous iteration's actions
  already returned their screenshots in the conversation, so a retry
  re-sends the same state and cannot double-execute an action.

### Spec decisions (frozen 2026-11-05)

Saardeim delegated spec decisions up to spec freeze to Muse for this
Phase 1/2 work. The decisions below are the frozen spec for this
project; change them only with a new explicit decision.

1. Retry schedule (`retry.py`): delay before retry n (1-based) is
   uniform in [2**(n-1), 2**n] seconds (1-2s, 2-4s, 4-8s, 8-16s,
   16-32s), max 5 attempts. A Retry-After value from the API wins over
   the computed delay. Retryable: HTTP 429 and 500/502/503/504.
   Fail fast: 400/401/403/404. Exceptions without a readable status
   are retried only when they look like a transient network failure
   (connect/timeout); anything else fails fast.
2. Screenshot refresh on retry: deliberately NOT implemented. In this
   reference flow a model-call failure happens before any browser
   action in that iteration, and the previous iteration's actions
   already returned their screenshots in the conversation, so a retry
   re-sends the same state and cannot double-execute an action.
3. Fallback model: `GEMINI_MODEL_FALLBACK` (default
   `gemini-3.8-flash`). If the primary run exits non-zero, `run.sh`
   tries ONE fresh run with the fallback model. Conversation state is
   NOT carried over. Implemented in `run.sh`, not in the agent loop.
4. Failure reporting: `agent.last_error` is set when the model call
   exhausts its retries, and `main.py` exits 1 in that case (it
   previously always exited 0, and never called `sys.exit`).
5. Relay lifecycle: `run.sh` starts the relay and stops it on exit
   (including on failure). If something already listens on the relay
   port, `run.sh` aborts and tells you to run `./stop.sh` first.
   No resident process is left behind after a run.
6. Test workaround (`conftest.py`): the test process trims
   `NO_PROXY`/`no_proxy` to `localhost,127.0.0.1`, the same httpx
   parsing workaround `run_trial.py` applies at run time. No test or
   upstream file was changed for this.
7. Log timestamps use the VM system time (UTC), not Asia/Bangkok.
   Each run log records model, query, initial URL, free memory,
   exit code, and the count of `503` occurrences.
8. The Gemini API key is never stored in any file. `.env` holds
   non-secret settings only; the key is fetched at run time as a
   surrogate from the registered `custom.gemini` connection.
9. Phase 3 page cap: at most `MAX_PAGES` tabs (default 3, in `.env`),
   in a single BrowserContext (shared cookies/login). A new tab past
   the cap is REFUSED (closed immediately) rather than evicting an
   existing tab, so the agent never silently loses a tab it is working
   on. The refusal is printed and the registry in the next action
   result shows the tab was not opened.
10. Popups (target=_blank, window.open) are registered and become the
    active tab; the previous tab stays open and can be switched back
    to by ID. This replaces the upstream behaviour of overwriting the
    current page with the popup's URL.
11. Closed tabs are deregistered. If the active tab closes, the most
    recently registered live tab becomes active. The registry is also
    pruned on every read (`is_closed()`), because the sync API may
    deliver "page"/"close" events lazily; registration is idempotent
    by Page object identity.
12. Page switching for the model is via plain custom function
    declarations (`switch_page`, `open_new_page`), not via the
    Computer Use tool, which has no page action. They are declared
    only when the computer's `supports_pages` is True. A failed
    switch/open (unknown ID, cap reached) returns an error dict with
    the current page list attached, not an exception, so the agent
    loop keeps running and the model can correct itself.
13. The tab registry (`pages` + `active_page_id`) is included in every
    action result, and a short page-management instruction is appended
    to the first user message when pages are supported.
14. `switch_page` / `open_new_page` turns carry screenshots and are
    pruned like predefined Computer Use turns. `EnvState` carries the
    optional `active_page_id` / `pages` fields (None for environments
    without page management).
15. Verification (2026-11-05): 33 unit tests pass (incl. 12 Page
    Manager + 7 agent page-function tests); the scripted live test
    (`diagnostics/test_page_manager_live.py`) passes all 10 checks
    (popup registration, third tab, cap refusal, switching, close
    fallback); one model run via `run.sh` (gemini-3.5-flash-lite,
    exit 0, ~41s, 0 503s) called `open_new_page` then `switch_page`
    and reported both tab URLs correctly.

### Phase 4 notes (Android ADB bridge, 2026-11-05, first step)

- `android_bridge.py` — the Android adapter with the common execution
  interface (`capture_screen`, `get_current_state`, `click`, `swipe`,
  `type_text`, `press_key`) shared by the Playwright and Android
  adapters, following GPT's 3-layer split (judgement Gemini /
  execution ADB / permission management Muse).
- `android_readonly_pass.py` — read-only verification loop: screenshot,
  Gemini text read of the screen (gemini-3.5-flash-lite, vision, text
  only), second screenshot, byte comparison. No input is performed.
- `adb_tunnel.py` — local TCP forwarder for the ADB transport:
  127.0.0.1:5555 through the Tailscale tunnel proxy (port 3130) to the
  phone's wireless-debugging port. Start it only for an ADB session.
- `android_bridge.py _proxy` — hidden ProxyCommand helper for ssh: it
  opens a CONNECT tunnel through the Tailscale proxy and relays stdio.
  The same relay pattern as `connect_relay.py`, for the phone instead
  of the egress proxy.
- Transports, in priority order:
  - `termux-rish` (primary): SSH into Termux over the Tailscale tunnel
    proxy, then `rish -c ...` (Shizuku shell) for `screencap` and
    `input`. No adb binary is needed on this VM, and the SSH path is
    the one whose reachability was already proven (2026-11-01). Needs
    on the phone: Tailscale connected, Termux sshd running, Shizuku
    started, rish installed in Termux.
  - `adb` (secondary): local adb binary + `adb_tunnel.py` +
    `adb connect 127.0.0.1:5555`. Needs on the phone: wireless
    debugging enabled and paired. Needs on this VM: `apt install adb`
    (package `adb` 1:34.0.4-1build3 is available; NOT installed yet —
    install only after the phone side is ready).
- Route comparison (Termux/rish vs ADB): both routes need the same
  phone-side prerequisite (wireless debugging, because Shizuku is
  started through it and ADB over TCP needs it). The Termux/rish route
  fits this setup better: it reuses the already-proven SSH path, needs
  no VM package install, and ssh supports a ProxyCommand while adb does
  not natively (adb needs the local forwarder). The ADB route stays as
  the alternative for cases where Termux/rish is unavailable. Neither
  route replaces the existing Galaxy screen-operation track
  (`workspace/goals/galaxy-a55-screen-operation-via-termux-and-tailscale/`);
  Phase 4 builds on it.
- The phone is reached ONLY over the private Tailscale network
  (the phone's Tailscale IP, set in `.env` as `ANDROID_PHONE_IP`).
  The ADB port is never exposed to the internet.
  Ordinary networking does not reach the tailnet; all access goes
  through the tunnel proxy on port 3130.

### Phase 4b notes (bridge app, 2026-11-05)

The Shizuku/ADB route is dead on this phone (wireless debugging and USB
both unavailable), so the alternative agreed with the user is a bridge
app (`android-app/`) built on this VM. It needs no ADB, USB, or wireless
debugging: an AccessibilityService provides screenshots
(`takeScreenshot`, API 30+), taps/swipes (`dispatchGesture`), text entry
(`ACTION_SET_TEXT`), and global keys, served over a small HTTP server on
the phone (raw `ServerSocket`, zero dependencies, pure Java).

- `android-app/Muse_Bridge_v1.5.apk` — debug-signed, sideloading only
  (37,431 bytes, version 1.5). Package `dev.muse.bridge`, minSdk 30,
  targetSdk 34. Built with the manual aapt2 / javac / d8 pipeline
  (`android-app/build.sh`); no Gradle (the VM has ~400 MiB free RAM).
- The server binds to the phone's Tailscale IPv4 (100.x) plus 127.0.0.1,
  port 8080. Auth is a token generated at runtime on first launch,
  shown in the app UI; every request needs it as `X-Bridge-Token`.
  No credential is in the source; the VM client reads the token from
  `ANDROID_APP_TOKEN` at run time only.
- `android_bridge.py` gained the `app` transport (same common
  interface, same action gate). `android_readonly_pass.py` accepts
  `--transport app`. The token is never stored in `.env` (see
  `.env.example`: `ANDROID_APP_URL` only).
- The SDK was installed by hand because Java networking fails in this
  VM: `HttpURLConnection` throws `NoSuchElementException` in
  `doTunneling0` even though curl and raw sockets CONNECT fine through
  the same egress proxy (a Java raw socket to localhost even gets the
  runtime's "Other TCP connections is turned off" reply). So
  `sdkmanager` cannot fetch packages here; the official zips were
  downloaded with curl and unpacked into `~/workspace/tools/android-sdk`
  (build-tools 34.0.0, platform android-34, platform-tools 37.0.2;
  sizes checked against repository2-1.xml). The JDK is Temurin 17 at
  `~/workspace/tools/jdk17`. d8 needs its input as a jar (a bare class
  directory is rejected as "Unsupported source file type"), aapt2 2.19
  has no `add` (classes.dex is added with `jar uf`), and javac needs
  `-encoding UTF-8` in this VM's C locale. All of this is scripted in
  `android-app/build.sh`; see `android-app/README.md` for the full
  story and the phone-side setup steps.
- Verification (2026-11-05): build succeeds, `apksigner verify` passes
  (1 signer), `aapt2 dump badging` shows package `dev.muse.bridge`,
  minSdk 30, targetSdk 34, both services in the manifest, all classes
  in classes.dex. Full test suite passes (44 tests, incl. 6 new app
  client tests against a local mock server). The app has NOT been
  tested against a real phone yet — it is not installed on the phone.

### Spec decisions (frozen 2026-11-05, Phase 4 added)

16. Read-only is the default. Actions (`click`, `swipe`, `type_text`,
    `press_key`) raise unless the bridge was started with
    `--allow-actions` AND an explicit action on the command line. The
    read-only pass never enables actions. Irreversible actions
    (purchase, send, delete, permission changes) additionally need the
    user's approval of the exact action first; the bridge does not
    invent or approve actions on its own.
17. Transport priority is termux-rish first, adb second (see the route
    comparison above). The ADB transport is implemented but untested
    until adb is installed and the phone's wireless debugging is on.
18. The Gemini API key is never stored in any file. The read-only pass
    fetches it at run time as a surrogate from the registered
    `custom.gemini` connection, same as `run_trial.py`.
19. Screenshot comparison is by PNG bytes (SHA-256) plus sizes. A
    mismatch does not by itself prove an action happened: the clock
    and status-bar changes alone alter bytes, so a mismatch must be
    confirmed visually against the two saved screenshots.
20. The full mobile Computer Use function-calling loop is NOT part of
    this first step. The read-only pass uses Gemini vision
    (generateContent with the screenshot) for the screen read. Wiring
    the mobile Computer Use environment is a later step, after the
    screenshot path is proven.
21. Connectivity status (2026-11-05): the phone is currently NOT
    reachable through the tunnel. `android_bridge.py check` reports
    ssh exit 255 ("tunnel proxy closed the connection (empty reply)")
    and adb not installed. No approval is pending, so the likely cause
    is the phone side (Tailscale not connected and/or Termux sshd not
    running). The read-only verification pass could not run yet.
22. Bridge app (Phase 4b, frozen 2026-11-05): package
    `dev.muse.bridge`, minSdk 30 (needed for
    `AccessibilityService.takeScreenshot`), targetSdk 34, pure Java,
    zero dependencies. The server binds the phone's Tailscale IPv4
    (100.x) plus 127.0.0.1 on port 8080; auth is a runtime-generated
    token shown in the app UI, sent as `X-Bridge-Token`, never stored
    in source, `.env`, or logs. The VM client reads it from
    `ANDROID_APP_TOKEN` at run time only.
23. Bridge-app build: manual aapt2 / javac / d8 pipeline
    (`android-app/build.sh`), no Gradle (VM RAM). The SDK is installed
    by hand from official zips fetched with curl (sdkmanager cannot
    tunnel through this VM's proxy from Java). d8 takes a jar, not a
    bare class directory; classes.dex is added with `jar uf` (aapt2
    2.19 has no `add`); javac needs `-encoding UTF-8`.
24. Bridge-app endpoints use screen pixels; the VM client converts the
    model's 0-999 coordinates using width/height from `/state`.
    `type_text` sets text on the focused editable field and returns
    422 when there is none (tap the field first). `takeScreenshot`
    returns nothing for OS-protected secure windows; the endpoint then
    returns 500.
25. The bridge app runs as a foreground service with a notification
    while started and stops cleanly from the app. The VM-side action
    gate applies unchanged to the `app` transport (read-only by
    default; actions only with `--allow-actions` and an explicit
    user-approved action).
26. The debug signing key (`android-app/debug.keystore`) is local-only
    (git-ignored); the debug APK is for sideloading, never for any
    store.
27. The `app` transport is the third transport in
    `android_bridge.py`, after termux-rish and adb. It does not
    replace them: termux-rish stays first when Shizuku/rish work, adb
    stays the alternative when wireless debugging works, and the app
    is the route when neither is available.
28. Verification (2026-11-05): VM build succeeds (debug APK, 25,143
    bytes, 1 signer); full test suite passes (44 tests, incl. 6 new
    app-client tests against a local mock server and the CLI `check`
    against the mock). NOT yet verified against a real phone: the app
    is not installed on the phone, so screenshot/tap/type over the
    app transport are untested on device.
29. Amendment (2026-11-05, spec-freeze authority): the bridge token
    MAY be stored in `.env` (`ANDROID_APP_TOKEN`, file chmod 600,
    git-ignored), superseding the "never in `.env`" part of decision
    22. Reason: the secure connector store rejects this credential
    because its host is a private Tailscale IP, not a public DNS
    hostname, and there is no website for a login card — so no
    vault-backed storage fits. The token is scoped to the private
    Tailscale network, is never written to chat, logs, or source,
    and is rotated by regenerating it in the app. Transfer: the user
    writes it to a file in Termux on the phone and Muse reads it
    over the existing authenticated SSH channel, then writes it to
    `.env`.
30. Fixes found during the first real-phone verification (2026-11-05):
    the app transport used plain `urllib.request.urlopen`, which
    follows the env proxy (port 3128, internet egress) and cannot
    reach the tailnet; it now routes through the tunnel proxy (3130)
    via a dedicated opener, with a direct route for localhost (the
    unit-test mock server). `android_readonly_pass.py` did not trim
    `NO_PROXY` (bracketed IPv6 entries break httpx) and did not
    retry Gemini 503s; both fixed (same trim as `run_trial.py`;
    `call_with_retry` from `retry.py`). `.env`'s `DEFAULT_QUERY` was
    unquoted, breaking `source .env` in bash; quoted in `.env` and
    `.env.example`.
31. Verification (2026-11-05 23:38 +07, log
    `logs/android-20261105-163846-readonly.log`): bridge-app check ok
    (app reachable, accessibility service enabled); the read-only
    pass captured two real screenshots and Gemini read shot1
    accurately (a YouTube video screen). No bridge actions were
    performed (`actions_performed: []`). The two screenshots differ
    because the user navigated the phone himself between the two
    captures (YouTube -> a GitHub permissions screen), so the
    before/after byte comparison cannot prove "unchanged" for this
    run — the pass still proves screenshot capture and the model
    screen read work over the app transport on a real phone.
32. First real action (2026-11-05 23:42 +07): `press_key HOME` once
    over the app transport with `--allow-actions`, user-approved in
    chat ("じゃあHOMEキーおすよ？"). Bridge returned `{"ok": true}`.
    Before screenshot showed the Muse chat; after screenshot
    (`logs/android-20261105-164218-home-after.png`) shows the home
    screen — first proven bridge action on the real phone. Next
    candidate: tap the Termux icon (needs the icon's screen
    coordinates from `/state` first, and user approval).
33. Tap failure (2026-11-05 23:44 +07): `tap` on the utility folder
    failed twice with HTTP 500 `{"ok":false,"error":"gesture timed
    out"}` (raw body confirmed via a direct request — the VM client
    discarded the body before this fix). Root cause: the service
    config lacked `android:canPerformGestures="true"`, so
    `dispatchGesture` returned false and `runGesture` ignored the
    return value, waiting the full 5s latch timeout. Latent second
    bug: the tap path was moveTo-only (degenerate). Fixes in v1.1
    (versionCode 2, APK 25,144 bytes): `canPerformGestures="true"`,
    tap adds a 1px lineTo, `runGesture` throws immediately when
    `dispatchGesture` returns false, and the VM client now includes
    the 500 body in `TransportError`. Install over the existing app
    (same debug key; SharedPreferences token survives — do NOT
    uninstall first). If taps still fail after the update, toggle
    the accessibility service off and on once. User put Termux in a
    "MUSE用" folder on the home screen (2026-11-05 23:46 +07); the
    tap target is inside that folder.
34. Tap test SUCCEEDED with v1.1 (2026-11-05 23:54–23:55 +07, logs
    `android-20261105-165446-muse-folder-open.png`,
    `android-20261105-165503-termux-open.png`): HOME, tap on the
    MUSE用 folder at (539, 859) -> `{"ok": true}`, folder opened
    (Termux, Shizuku, F-Droid, Tailscale, Muse Bridge); tap on the
    Termux icon at (201, 1074) -> `{"ok": true}`; Termux opened
    ("Welcome to Termux!", `$` prompt, on-screen keyboard visible),
    confirmed by screenshot and `/state` package `com.termux`.
    First proven tap actions on the real phone; the v1.1
    `canPerformGestures` fix is confirmed working. Coordinates were
    found with visual grounding on the fresh screenshots (folder
    499/367, Termux 186/459 in 0–1000 space, screen 1080x2340).
35. Swipe test SUCCEEDED (2026-11-05 23:55–23:56 +07, logs
    `android-20261105-165553-swipe-before.png` /
    `-swipe-after.png`): swipe left (900, 1200) -> (200, 1200),
    300 ms, `{"ok": true}`; home screen moved from the main page
    (Proton Mail, MUSE用 folder, utility, banking, ThaiD, Chrome,
    Brave) to the second page (llm folder, Muse, Jota+, game folder,
    YouTube, YT Music, X, シュリンピア). Pointer-location overlay
    confirmed the gesture (dX: -700.0). Next candidate: type_text
    test (Jota+ text editor, no sending).
36. Latency (measured 2026-11-06 00:03 +07): one small request
    (health/state) costs ~0.75 s round-trip through the tunnel
    proxy + Tailscale; a persistent connection does NOT help
    (0.72–0.74 s per request either way — the cost is the path,
    not the CONNECT handshake). A screenshot cost ~2.8 s, mostly
    a 2.3 MB full-res PNG of a photo wallpaper. Python startup
    0.14 s; the server already uses a 4-thread pool — neither is
    the bottleneck. Fixes in v1.2 (versionCode 3, APK 29,240
    bytes): `/capture_screen` takes `?scale=&format=&quality=`
    (server clamps scale 0.1–1.0, quality 10–100; JPEG via
    Bitmap.compress, PNG default) and answers `X-Capture-Ms`;
    the VM client defaults routine shots to half-scale JPEG
    (~540x1170, ~0.3 MB) and accepts PNG-or-JPEG bytes; the
    read-only pass keeps full PNG so its byte comparison stays
    valid. CLI `screenshot` gains `--scale/--format/--quality`.
    46 unit tests pass (3 new: default JPEG params, full-PNG
    params, non-image rejection). Install over v1.1 (same debug
    key; token survives — do NOT uninstall). Workflow rule
    adopted at the same time: fewer screenshots per action
    (after-only when state is known; /state for cheap checks).
    Not fixable from the VM: the ~0.75 s floor (phone on 4G+,
    likely DERP-relayed; a Wi-Fi direct path might be faster,
    unconfirmed).
37. File naming rule (2026-11-06, user instruction): file names
    use underscores, never spaces, and the extension always
    matches the content. The APK is now versioned:
    `Muse_Bridge_v1.2.apk` (build.sh: VERSION_CODE/VERSION_NAME
    drive both the manifest and the output name; same 29,240
    bytes as the v1.2 build before the rename — the build is
    byte-reproducible). Screenshot files follow the same rule:
    `android_bridge.py` rewrites the output extension from the
    format (`_shot_out_path`; JPEG -> .jpg, PNG -> .png), so a
    v1.2 JPEG shot is never saved as .png. 47 unit tests pass.
    The launcher label stays "Muse Bridge" (spaces are normal
    for a display name); only file names follow the rule.
38. APK push (2026-11-06 00:13 +07, user-approved): `push_apk.sh`
    copies the current `Muse_Bridge_v*.apk` to the phone's
    Download folder over the Termux SSH channel (same
    ProxyCommand as the bridge; scp needs -P for the port, ssh
    -p — a first version used -p for scp and failed), verifies
    with sha256, falls back to Termux home if
    `storage/downloads` is unavailable (needs
    `termux-setup-storage`). Install still needs one manual tap
    (Android requires it for sideloaded APKs). Push blocked at
    setup time: Termux SSH was down (tunnel proxy empty reply)
    — the user must open Termux and run `sshd` first. User then
    proposed update notification via the APK itself (v1.3
    candidate): VM POSTs /notify to the bridge after a push, the
    app posts a notification, tap opens Downloads; needs
    POST_NOTIFICATIONS granted once. No polling, no new server,
    no new dependency.
39. Bridge app v1.3 BUILT and PUSHED (2026-11-06 00:17 +07,
    user-approved): POST /notify posts an update notification
    (channel "muse-bridge-updates", tap opens Downloads via
    DownloadManager.ACTION_VIEW_DOWNLOADS; 422 when notifications
    are not permitted); MainActivity requests POST_NOTIFICATIONS
    on Android 13+ at launch; VM `notify()` (ungated — it only
    shows a notification) + CLI `notify --title/--text`;
    push_apk.sh calls notify after a successful push, non-fatal
    on failure, and now sources .env for the token. versionCode 4
    / versionName 1.3, APK 29,239 bytes, apksigner OK, 49 unit
    tests pass. v1.2 was never installed — v1.3 supersedes it
    (all v1.2 changes + notify). First push (00:18 +07): SSH was
    back up after the user ran `sshd`; `storage/downloads`
    does not exist (termux-setup-storage never run), so the APK
    landed in Termux home `~/Muse_Bridge_v1.3.apk`, sha256
    verified; `termux-open ~/Muse_Bridge_v1.3.apk` then opened
    the package installer on the phone (Termux:API present).
    Notify failed on this first push as expected (installed app
    is v1.1, no /notify; bridge also not answering on :8080).
    After install: open Muse Bridge, grant notifications when
    asked, tap Start bridge. For future pushes to land in
    Downloads, run `termux-setup-storage` once in Termux.
40. Bridge app v1.4 BUILT and PUSHED — Phase 1 State Engine
    (2026-11-06 00:46 +07, user-approved, GPT design
    CONS-20261106-009): /state now carries the node tree —
    per-node id (valid only within its revision), parent, class,
    text, content_description, bounds in screen pixels,
    clickable/focusable/focused/editable/scrollable/enabled/
    selected, password flag, input_type, resource_id, and
    actions (click/long_click/focus/set_text/scroll_*/select);
    plus focused node id (input focus first, accessibility
    focus fallback), ime_visible (TYPE_INPUT_METHOD window
    present), and a monotonic revision incremented per
    snapshot; tree capped at 500 nodes / depth 25 with a
    truncated flag. Password nodes report "[REDACTED]" and
    never expose their value. Focus is read during the walk,
    never matched by bounds afterwards. versionCode 5 /
    versionName 1.4, APK 29,241 bytes, apksigner OK, 50 unit
    tests pass (mock /state now returns a sample tree incl. a
    password node). First push (00:47 +07): the first scp
    failed on a transient tunnel-proxy empty reply, the
    immediate retry succeeded — APK in Termux home
    ~/Muse_Bridge_v1.4.apk, sha256 verified; termux-open
    opened the installer. Notify failed this time because the
    bridge was not running (:8080 unreachable); the bridge
    was reachable at 00:21 +07, so it stopped sometime after.
    After install: open Muse Bridge, tap Start bridge, then
    Muse verifies /state on the real phone (node count,
    latency, bounds). VERIFIED 2026-11-06 00:48 +07 on the
    real phone (Muse chat open): /state in 2.1 s, revision 1,
    125 nodes, focused n69, ime_visible false, truncated
    false. Bounds are real: n69 EditText (message input,
    169,1958–781,2101, editable, set_text available, focused),
    n68 Button "添付ファイルを追加", n73 "音声入力", n75
    "生成を停止", n120 "Open MUSE-Koyuki's profile". 13
    clickable, 1 editable, 39 with text. Note: 2.1 s is slower
    than the old bare /state (~1 s) — the tree walk costs
    time — but it replaces screenshot (1.8 s) + grounding, so
    it is a net win. TYPE TEST SUCCEEDED 2026-11-06 00:49 +07
    (user-approved, in the focused Muse chat input n69):
    type_text "MUSEテスト" returned ok:true; /state revision 2
    showed the EditText holding "MUSEテスト" and ime_visible
    true (keyboard up); cleared with type_text "" (ok:true);
    /state revision 3 showed the field empty and no node
    holding the test string. Nothing was sent — no enter/send
    was pressed. Conclusion: the bridge's type_text works on a
    standard editable field; the Jota+ failure is that app's
    custom editor exposing no editable node, not a bridge bug.
41. Bridge app v1.5 BUILT and PUSHED — Phase 2 Action Engine
    (2026-11-06 00:52 +07, user-approved "どんどんやろう",
    GPT design CONS-20261106-009): /click_node and /type_node
    (node id + tree_hash; 409 when the screen moved on, 422
    when the node is gone/not clickable/not editable; a
    non-clickable node falls back to its first clickable
    ancestor); /batch (batch_id, tree_hash, actions[]:
    click_node/tap/type_node/type/key/swipe/wait, each with
    action_id and optional expect_change). One batch runs
    against one snapshot: the first failure stops it, and an
    expect_change step waits up to 2 s for the tree hash to
    move, then stops the batch (later node ids are invalid
    after a screen change — the client refetches /state).
    Re-sent batch_id returns the cached result without
    re-executing (idempotency, max 50). The service keeps the
    live nodes from the last walk (recycled on the next
    snapshot) and refreshes each node right before acting;
    staleness is judged by tree_hash, not the snapshot
    counter. VM: click_node/type_node/batch + CLI commands.
    versionCode 6 / versionName 1.5, APK 37,431 bytes,
    apksigner OK, 54 unit tests pass. Push (00:52 +07):
    APK in Termux home ~/Muse_Bridge_v1.5.apk, sha256
    verified; /notify posted OK this time (bridge was
    running) — but its text said "ダウンロードフォルダ" while
    the file was in Termux home, so push_apk.sh now names the
    real location in the notification and runs termux-open
    automatically on the Termux-home fallback; the installer
    was opened on the phone. After install: open Muse Bridge,
    tap Start bridge, then Muse verifies a real batch on the
    phone (type + clear in one request, then the same
    batch_id re-sent to prove idempotency).

    VERIFIED on the real phone 2026-11-06 01:03-01:05 +07:
    the user installed v1.5 manually (App info showed
    バージョン1.5; the 01:00 cron run was stopped as
    redundant). The bridge came back with tree_hash (v1.5
    live; it auto-restarted after the update, no Start tap
    needed this time). Batch verify-v15-001 (click_node n60,
    type_node "MUSEテスト", type_node "") executed in one
    request; the same batch_id re-sent returned replayed:true
    with no re-execution. Batch verify-v15-002 (type_node
    only) proved via /state (revision 4) that the field held
    "MUSEテスト". Batch verify-v15-003 failed with "node n55
    went stale" because the chat UI was updating mid-test
    (generation finishing; node ids shifted n55→n60) — the
    staleness guard refused to act on a moved node, as
    designed. Batch verify-v15-004 with the fresh state
    cleared the field; final /state revision 7 shows the
    EditText empty and no node holding the test string;
    nothing was sent. Note: per-action hash_before/hash_after
    in a batch result are the cached lastTreeHash, not fresh
    values (only expect_change polls a fresh snapshot) —
    intermediate state must be checked with /state, not from
    those numbers. Lesson: termux-open fires silently but
    Android blocks the background activity start when Termux
    has been backgrounded a while — bring Termux to the
    foreground first, or install from the chat attachment.

42. Bridge app v1.6 BUILT and PUSHED — Phase 3 Event Engine
    (2026-11-06 01:07 +07, user-approved "p3へ", GPT design
    CONS-20261106-009): onAccessibilityEvent now records a
    bounded, coalesced history — whitelist of 7 types
    (WINDOW_STATE_CHANGED, WINDOW_CONTENT_CHANGED,
    VIEW_CLICKED, VIEW_FOCUSED, VIEW_TEXT_CHANGED,
    VIEW_SCROLLED, VIEW_SELECTED); identical repeats within
    300 ms merge into the previous record (coalesced count);
    ring buffer cap 200 (oldest evicted); monotonic seq
    (AtomicLong); password sources always "[REDACTED]" (text,
    before-text, and description never leave the phone);
    event time is uptimeMillis, never compared with VM wall
    clocks. GET /events?since=&limit= (default 100, max 200)
    returns events after `since` plus latest_seq, dropped
    (true when the buffer evicted events the caller never
    saw — refetch /state), tree_hash, revision. /state also
    carries event_seq. Config needed no change (typeAllMask
    + notificationTimeout 100 already set). VM: events()
    (read-only, no --allow-actions gate) + CLI `events
    --since/--limit`. versionCode 7 / versionName 1.6, APK
    37,434 bytes, apksigner OK, 59 unit tests pass (5 new).
    Push (01:07 +07): APK in Termux home, sha256 verified;
    termux-open again fired silently with no installer (same
    background-start block); the user installed from the chat
    attachment. VERIFIED on the real phone 2026-11-06
    01:15 +07: /state showed event_seq 2018 (v1.6 live);
    /events returned the buffered history with coalesced
    counts (12, 6, 3, 8 on identical CONTENT_CHANGED repeats);
    seq advanced live 2018 → 2095 → 2134 → 2173 as the user
    used the phone (Chrome scrolling: VIEW_SCROLLED +
    WINDOW_CONTENT_CHANGED, incl. input-method events);
    dropped=true on since=0 after evictions (correct — the
    client must refetch /state), dropped=false on a fresh
    since within the buffer; /state event_seq matches the
    events stream. Redaction is unit-tested only (no password
    field was touched live). Next: Phase 4 long-polling
    /events, Phase 5 SSE validation, Phase 6 benchmarks.

43. Phone address removed from the source (2026-11-06 01:25
    +07, user-approved "それは変えとこう" — the public repo
    must not carry the phone's Tailscale IP): android_bridge.py,
    adb_tunnel.py, push_apk.sh no longer default to the old
    100.111.74.42 / u0_a388; ANDROID_PHONE_IP,
    ANDROID_TERMUX_USER, ANDROID_APP_URL must come from the
    environment, with a clear error when unset (guards in
    ssh_base_cmd and _app_request). The live values moved to
    .env (chmod 600, git-ignored). Tests use 192.0.2.1
    (TEST-NET-1) instead. READMEs use <phone-tailscale-ip>.
    Repo gained .env.example (placeholders only). 59 tests
    pass; live /state still works from .env. Pushed to
    github.com/parsenta214-create/muse-bridge as commit
    f59ea0d4.

44. Quick Settings tile + notification Stop (v1.7, 2026-11-06
    01:32 +07, user asked for a QS-tile / notification on-off
    switch): new BridgeTileService (tap toggles BridgeService,
    state follows isRunning(), refreshed on panel open and
    600 ms after a tap; falls back to opening the app if the
    platform refuses the foreground-service start);
    MainActivity "クイック設定に追加" button uses
    StatusBarManager.requestAddTileService on API 33+ (system
    one-tap dialog), manual add on older Android; the running
    notification carries a Stop action (PendingIntent to the
    service with ACTION_STOP, handled in onStartCommand).
    The tile toggles the server only — it does NOT enable the
    accessibility service (Android requires that by hand), so
    a running server without a11y still cannot gesture or
    screenshot. versionCode 8 / 1.7, APK 41,598 bytes,
    apksigner OK, tile declared in the manifest (verified
    with aapt2 dump), 59 tests pass. Phone verification
    COMPLETE 2026-11-06 01:45–01:46 +07: v1.7 installed (tile
    visible in the QS panel); STOP via tile verified (server
    unreachable, tile grey); START via tile verified (server
    reachable again, /state answers, package systemui).
    Both directions work. Next: Phase 4 long-polling
    /events, Phase 5 SSE validation, Phase 6 benchmarks.
45. Version reporting (v1.8, 2026-11-06 02:56 +07, promised
    2026-11-06 01:44 after the v1.7 tile verification exposed the
    blind spot: /state had no app version and Termux cannot run
    dumpsys): /state and /health carry "app_version" (e.g. "1.8")
    and "app_version_code" (e.g. 9), read at run time from
    PackageManager (new AppVersion helper, cached, "unknown"/-1
    fallback) — the single source of truth is the
    versionCode/versionName build.sh passes to aapt2 link, nothing
    is hardcoded in Java. MainActivity shows "v1.8 (9)" at the
    top. The old /health "version": 1 protocol field is
    untouched. versionCode 9 / 1.8, APK 41,597 bytes, apksigner
    OK, 60 tests pass (new test_app_state_app_version_v18 asserts
    both endpoints through the mock). Phone verification PENDING:
    user installs v1.8, Muse checks /state app_version.
46. Phase B approved (2026-11-06 03:10 +07, user chose "推奨通りで
    Phase Bへ進む"): implement the Codex Phase A design
    (CONS-20261106-011) in order A-1 HTTP input validation → A-2
    Executor separation → A-3 Accessibility execution model → A-4
    batch idempotency → A-5 integration check. The three human
    decisions are settled as recommended: (1) NodeInfo execution
    context = service main thread as baseline (HandlerThread only
    as a verified alternative); (2) batch cache TTL = 10 min +
    tombstone for expired IDs; (3) batch result persistence =
    memory only (no exactly-once across process restarts — a
    stated design constraint). New frozen decisions are needed
    for: node_id/snapshot validity, batch TTL, post-restart
    resend, 503 responses, Accessibility serialization.
    A-1 implemented in v1.9 (2026-11-06 03:14, 65 tests pass):
    HttpBridgeServer validates the request line, rejects bad/
    duplicate headers, checks the token BEFORE reading the body,
    validates Content-Length (negative -> 400, over 256 KiB ->
    413), caps headers at 8 KiB (-> 431), per-phase timeouts
    5 s header / 10 s body / 45 s total (-> 408), malformed
    JSON -> 400; the VM client classifies 400/408/413/431 as
    input-validation errors instead of blaming the token.
    Verified live 2026-11-06 03:19 after the user installed v1.9:
    /state 1.9/10, /health matches, bad token -> 401, oversized
    Content-Length -> 413 "body too large". The bridge was briefly
    unreachable right after the update (service restarting);
    /state + /health both answer again after ~1 min.
47. A-2 adopted limits (2026-11-06 03:16, Codex Phase A initial
    values, now frozen): HTTP workers 4, worker queue 8, max
    connections 16 (Semaphore, acquired at accept, fail -> close
    without reading). Accept threads are dedicated (one per
    listener socket) and never run HTTP handling; the worker pool
    is a bounded ThreadPoolExecutor (AbortPolicy). Lifecycle
    NEW -> STARTING -> RUNNING / FAILED -> STOPPING -> STOPPED;
    start() returns true only when >= 1 socket is bound, and
    BridgeService reports running only then (fixes the old
    "running = true unconditionally" bug). Deviation from the
    Codex design, recorded: a queue-full 503 is sent WITHOUT
    reading the request (Codex said "after minimal header
    parse") — reading the request would spend the work the
    shedding is meant to save. Same deviation for the
    connection-cap close.
48. A-3 adopted model (2026-11-06 03:25, Codex Phase A baseline):
    single execution context = service main thread; HTTP workers
    post accessibility work to it via runOnMainThread (20 s timeout
    -> 503 ServiceTimeoutException) and run directly when already on
    the main thread (no self-deadlock in waitForChange). Every node
    action takes a FRESH tree walk just before acting (resolveFresh)
    and checks tree_hash match (else 409), node exists, and visible;
    click also requires a clickable ancestor that is enabled;
    type requires editable + enabled. Password fields accept typed
    text (entering a password); their value never leaves the phone
    (redacted in /state and /events). /state gains snapshot_id
    (= revision), snapshot_valid, snapshot_stable (false when an
    event arrived mid-walk — the VM refetches /state before
    important ops); action responses gain snapshot_id. A failed
    walk recycles its partial nodes (walk-level finally +
    collectNodes guard for a node that failed before reaching the
    live map) instead of leaking them. Client rule: an unstable
    last snapshot is refetched before a node action; a moved
    tree_hash raises instead of acting. Batch entry check and
    409/422 contract unchanged; batch carries the entry
    snapshot_id.
49. A-4 batch idempotency (2026-11-06 03:31, Codex Phase A):
    batch_id is the lookup key; payload_hash = SHA-256 of a
    canonical payload (fixed field order, defaults filled by the
    HTTP layer, strings length-prefixed; batch_id NOT in the hash;
    protocol_version defaults to 1, any other version is 400, and
    unknown top-level or per-action fields are 400). States:
    ACCEPTED -> RUNNING -> COMPLETED/PARTIAL/FAILED. Same id + same
    hash -> cached result (replayed: true), never re-executed;
    re-send while running -> status "running", replayed: true.
    Same id + different hash -> 409; an id whose result expired
    (10 min) -> 410 until its tombstone lapses (another 10 min).
    0 successes = failed; >= 1 success then stop = partial.
    Response gains status, payload_hash, completed_action_ids,
    failed_action_id. "completed" means Android confirmed the
    action ran, NOT that the target app finished its business
    operation. Memory only: no exactly-once across a process
    restart (a stated design constraint). Client sends
    protocol_version: 1 and re-sends on 410 only with a NEW
    batch_id (never re-use the expired one). The AntiGravity PoC
    (SPEC-POC-001, antigravity_projects/muse-bridge-poc/) is a
    separate independent protocol implementation; no merge into
    muse-bridge was done — that remains a separate approval.
50. A-5 integration check (2026-11-06 03:33, VM-side part):
    backward compat — /state, /click_node, /type_node, /batch only
    gain fields (snapshot_id, status, payload_hash,
    completed_action_ids, failed_action_id); protocol_version is
    optional (default 1) so old batch requests still work. Error
    codes: 400 bad JSON / unknown field / unsupported version /
    too many actions, 401 bad token, 408 timeout, 409 stale
    snapshot or batch_id reuse with different content, 410
    expired batch_id, 413 body too large, 422 node not found /
    not clickable / not editable, 431 header too large, 503
    service unavailable / main-thread timeout / overloaded.
    Deadlines: header 5 s, body 10 s, request total 45 s (A-1);
    main-thread work 20 s (A-3, inside the 45 s total); batch
    max 20 actions (A-5) so even all-expect_change batches stay
    inside 45 s. Client resend rules: unknown outcome -> re-send
    the SAME batch_id + payload (server replays from cache);
    409 -> new content needs a new batch_id; 410 -> expired,
    always a new batch_id; 409 stale snapshot -> refetch /state
    and re-plan. AntiGravity PoC build_batch emits only
    server-allowed fields and no protocol_version (defaults to 1),
    so its payloads pass the strict validation; its
    parse_batch_result reads required keys only, so the added
    response fields do not break it. Phone verification (A-1
    errors, A-2 flood/503, A-3 snapshot fields, A-4 replay/409)
    is the remaining A-5 step and needs the final v1.9 APK
    installed on the phone.
51. A-5 live bug found and fixed (2026-11-06 03:45, v1.9.1,
    versionCode 11): the A-2 accept threads were born dead. start()
    spawns them while state is STARTING, but acceptLoop only ran
    while state == RUNNING, so each thread exited at once — the
    socket stayed bound (start() reported running, the app showed
    "Bridge: running") but no thread ever accepted, and every
    request timed out. The A-2 build had never run on a real phone
    before this (it was an intermediate build), so the bug only
    surfaced at the A-5 phone verification. Fix: the loop runs in
    STARTING or RUNNING (until STOPPING/STOPPED). Verified by the
    symptom (bound but deaf, /health timeout, raw tunnel to
    :8080 silent) and the code read. Note: the app's MainActivity
    status can say "Accessibility service: NOT enabled" while the
    system settings list shows Muse Bridge ON — the status text is
    read at activity open and can be stale; /state is the live
    check (200 = connected, 503 = not). If /state 503s after the
    fix, toggle the accessibility service off and on in settings.
### What the user must do on the phone (bridge app route, current)

The bridge app is the current route (wireless debugging and USB are
both unavailable, so Shizuku/ADB cannot start). Steps:

1. Copy `android-app/Muse_Bridge_v1.8.apk` to the phone and install
   it (allow "install unknown apps" for the app used to open the APK).
2. Open the **Muse Bridge** app.
3. Tap **Open accessibility settings**, find **Muse Bridge**, and turn
   it on. Confirm the dialog. (Android requires this by hand.)
4. Back in the app, tap **Start bridge**. Status should read
   "Bridge: running" and "Accessibility service: enabled".
5. Note the bridge address (e.g. `http://<phone-tailscale-ip>:8080`) and the
   token shown in the app. Tell Muse the address and the token via
   the secure entry, never in plain chat.
6. Muse then runs `android_bridge.py check` and the read-only pass
   with `--transport app`.

### What the user must do on the phone (Termux/rish route, before the verification pass)

1. Open Tailscale on the Galaxy A55 5G and check it shows Connected.
2. Open Termux and start sshd if it is not running (`sshd`; check with
   `pgrep -f sshd`). Termux sshd does not survive a reboot.
3. For the Termux/rish route: open Shizuku and start it via wireless
   debugging (Settings > Developer options > Wireless debugging must be
   on; the start must be repeated after each reboot). In Termux, `rish`
   must be installed.
4. For the ADB route (alternative): Settings > Developer options >
   Wireless debugging ON, and pair the device if asked.
5. Tell Muse when the phone is ready, so the tunnel can be re-probed
   (`python3 android_bridge.py check`) and the read-only pass can run.
