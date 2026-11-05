"""Local test setup (Phase 1/2).

The VM's NO_PROXY list contains bracketed IPv6 entries ([::1], ...),
which httpx fails to parse when the google-genai SDK builds its client
(httpx.InvalidURL: Invalid port: ':1]'). This trims NO_PROXY for the
test process only, the same workaround run_trial.py applies at run time.
It does not change any test or any upstream code.
"""
import os

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
