#!/usr/bin/env python3
"""
auto_fix_runner.py — called by GitHub Actions when job_monitor fails.

Steps:
  1. Read error_log.txt written by the workflow
  2. Call Claude API to generate a targeted fix
  3. Validate the fix (syntax check + basic sanity)
  4. Write the fixed job_monitor.py
  5. Write fix_applied=true to GITHUB_OUTPUT so the workflow commits it
  6. Send an email via SendGrid in all cases (fixed / could not fix)
"""

import ast
import json
import os
import ssl
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SENDGRID_API_KEY  = os.environ.get("SENDGRID_API_KEY", "")
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "")
FAILED_RUN_URL    = os.environ.get("FAILED_RUN_URL", "")
FAILED_RUN_NUMBER = os.environ.get("FAILED_RUN_NUMBER", "?")
GITHUB_OUTPUT     = os.environ.get("GITHUB_OUTPUT", "")

FROM_EMAIL = "jobmonitor@jobnotify.dev"
SCRIPT_FILE = "job_monitor.py"
LOG_FILE    = "error_log.txt"


# ── Helpers ───────────────────────────────────────────────────────────────────
def read_error_log() -> str:
    try:
        text = open(LOG_FILE).read()
        # Keep only the last 4 000 chars — enough for the traceback + context
        return text[-4000:].strip()
    except FileNotFoundError:
        return "(error_log.txt not found)"


def call_claude(error_log: str, script: str) -> tuple[str | None, str | None]:
    """Call Anthropic API; return (fixed_script, error_message)."""
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY secret not set — add it in repo Settings → Secrets"

    prompt = (
        "You are maintaining a Python job-monitoring script that runs on GitHub Actions.\n"
        "It just failed with the error log below. Fix ONLY the specific bug causing the failure.\n"
        "Return the COMPLETE corrected Python script and absolutely nothing else — "
        "no explanation, no markdown code fences, just the raw Python code.\n\n"
        f"ERROR LOG:\n{error_log}\n\n"
        f"CURRENT SCRIPT:\n{script}"
    )

    payload = json.dumps({
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":           ANTHROPIC_API_KEY,
            "anthropic-version":   "2023-06-01",
            "content-type":        "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=120, context=ctx) as r:
            resp = json.loads(r.read())
        fixed = resp["content"][0]["text"].strip()

        # Strip markdown fences if the model added them despite instructions
        if fixed.startswith("```"):
            lines = fixed.split("\n")
            end   = -1 if lines[-1].strip().startswith("```") else len(lines)
            fixed = "\n".join(lines[1:end])

        return fixed, None

    except HTTPError as e:
        return None, f"Anthropic API HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def validate_fix(code: str) -> str | None:
    """Return error string if fix is invalid, None if OK."""
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error: {e}"

    # Sanity: must still have the main entry point
    if "def run(" not in code and "asyncio.run(" not in code:
        return "Entry point missing — fix looks too aggressive"

    # Must still have the classify_jd function
    if "def classify_jd(" not in code:
        return "classify_jd() missing — fix looks too aggressive"

    return None


def send_email(subject: str, body: str):
    if not SENDGRID_API_KEY or not NOTIFY_EMAIL:
        print(f"  Email skipped (no credentials): {subject}")
        return
    payload = json.dumps({
        "personalizations": [{"to": [{"email": NOTIFY_EMAIL}]}],
        "from": {"email": FROM_EMAIL, "name": "Job Monitor"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }).encode()
    req = Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=30, context=ctx) as r:
            print(f"  Email sent ({r.status}): {subject}")
    except Exception as e:
        print(f"  Email failed: {e}")


def set_output(key: str, value: str):
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"  [output] {key}={value}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== Auto-fix runner — failed run #{FAILED_RUN_NUMBER} ===")

    error_log = read_error_log()
    print(f"\n--- Error log ({len(error_log)} chars) ---\n{error_log[-800:]}\n---\n")

    try:
        script = open(SCRIPT_FILE).read()
    except FileNotFoundError:
        print(f"ERROR: {SCRIPT_FILE} not found in working directory")
        sys.exit(1)

    print("Calling Claude API to generate fix...")
    fixed, api_err = call_claude(error_log, script)

    if fixed is None:
        msg = f"Auto-fix could not generate a patch: {api_err}"
        print(f"  {msg}")
        send_email(
            f"[Job Monitor] FAILED — run #{FAILED_RUN_NUMBER} (auto-fix unavailable)",
            f"{msg}\n\nFailed run: {FAILED_RUN_URL}\n\nFix it manually and push to restart the monitor.",
        )
        set_output("fix_applied", "false")
        return

    print("Validating generated fix...")
    invalid = validate_fix(fixed)
    if invalid:
        msg = f"Generated fix failed validation: {invalid}"
        print(f"  {msg}")
        send_email(
            f"[Job Monitor] FAILED — run #{FAILED_RUN_NUMBER} (auto-fix validation failed)",
            f"{msg}\n\nFailed run: {FAILED_RUN_URL}\n\nFix it manually and push to restart the monitor.",
        )
        set_output("fix_applied", "false")
        return

    # Write the fix
    with open(SCRIPT_FILE, "w") as f:
        f.write(fixed)

    print(f"  Fix written to {SCRIPT_FILE}")
    set_output("fix_applied", "true")
    # Success email is sent by the workflow after a successful commit+push


if __name__ == "__main__":
    main()
