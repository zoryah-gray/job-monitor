#!/usr/bin/env python3
"""
auto_fix_runner.py — called by GitHub Actions when job_monitor fails.

Steps:
  1. Read error_log.txt written by the workflow
  2. Call Claude API for a targeted fix as exact search/replace edits
  3. Apply the edits, then validate (syntax check + basic sanity)
  4. Write the fixed job_monitor.py
  5. Write fix_applied=true to GITHUB_OUTPUT so the workflow opens a PR for review
  6. Send an email via Gmail SMTP in all cases (fixed / could not fix)
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
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "")
FAILED_RUN_URL    = os.environ.get("FAILED_RUN_URL", "")
FAILED_RUN_NUMBER = os.environ.get("FAILED_RUN_NUMBER", "?")
GITHUB_OUTPUT     = os.environ.get("GITHUB_OUTPUT", "")

SCRIPT_FILE = "job_monitor.py"
LOG_FILE    = "error_log.txt"
SUMMARY_FILE = "fix_summary.md"   # read by the workflow for the PR body


# ── Helpers ───────────────────────────────────────────────────────────────────
def read_error_log() -> str:
    try:
        text = open(LOG_FILE).read()
        # Keep only the last 4 000 chars — enough for the traceback + context
        return text[-4000:].strip()
    except FileNotFoundError:
        return "(error_log.txt not found)"


# Structured output: a short summary plus exact search/replace edits. Returning
# edits instead of the whole ~1,500-line script keeps the reply to a few hundred
# tokens (the full script would not fit in max_tokens and would be cut off).
EDITS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["old", "new"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "edits"],
    "additionalProperties": False,
}
MAX_EDITS = 10


def call_claude(error_log: str, script: str) -> tuple[dict | None, str | None]:
    """Call Anthropic API; return ({summary, edits}, error_message)."""
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY secret not set — add it in repo Settings → Secrets"

    prompt = (
        "You are maintaining a Python job-monitoring script that runs on GitHub Actions.\n"
        "It just failed with the error log below. Fix ONLY the specific bug causing the failure.\n\n"
        "Return the fix as search/replace edits:\n"
        "- `old` must be copied character-for-character from the current script (including "
        "indentation) and must appear exactly once — include surrounding lines if needed to make it unique.\n"
        "- `new` is the replacement text.\n"
        f"- Use as few edits as possible (at most {MAX_EDITS}). Do not refactor or reformat.\n"
        "- `summary`: 2-4 sentences on the root cause and what the edits change.\n\n"
        f"ERROR LOG:\n{error_log}\n\n"
        f"CURRENT SCRIPT:\n{script}"
    )

    payload = json.dumps({
        "model": "claude-haiku-5-5",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": EDITS_SCHEMA}},
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
        with urlopen(req, timeout=180, context=ctx) as r:
            resp = json.loads(r.read())
        stop = resp.get("stop_reason")
        if stop == "refusal":
            return None, "Claude declined to generate a fix (stop_reason=refusal)"
        if stop == "max_tokens":
            return None, "Claude's edits were cut off at max_tokens"
        # Current models think by default: content[0] is usually a thinking block,
        # so take the first text block.
        text = next((b.get("text", "") for b in resp.get("content", [])
                     if b.get("type") == "text"), "").strip()
        if not text:
            return None, "Claude returned no text block"
        usage = resp.get("usage") or {}
        print(f"  Tokens: in {usage.get('input_tokens')} · out {usage.get('output_tokens')}")
        return json.loads(text), None

    except HTTPError as e:
        return None, f"Anthropic API HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def apply_edits(script: str, edits: list[dict]) -> tuple[str | None, str | None]:
    """Apply exact search/replace edits. Every `old` must match exactly once."""
    if not edits:
        return None, "Claude proposed no edits"
    if len(edits) > MAX_EDITS:
        return None, f"Claude proposed {len(edits)} edits (max {MAX_EDITS}) — fix looks too aggressive"
    for i, e in enumerate(edits, 1):
        old, new = e.get("old", ""), e.get("new", "")
        if not old:
            return None, f"Edit {i} has an empty `old` string"
        n = script.count(old)
        if n != 1:
            return None, f"Edit {i}: `old` text found {n} times (must be exactly 1)"
        script = script.replace(old, new, 1)
    return script, None


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

    if "async def score_jobs_parallel(" not in code:
        return "score_jobs_parallel() missing — fix looks too aggressive"

    return None


def send_email(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASS or not NOTIFY_EMAIL:
        print(f"  Email skipped (SMTP credentials not set): {subject}")
        return
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"]    = f"Job Monitor <{SMTP_USER}>"
        msg["To"]      = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"  Email sent: {subject}")
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
    result, api_err = call_claude(error_log, script)

    fixed = None
    if result is not None:
        fixed, edit_err = apply_edits(script, result.get("edits", []))
        if fixed is None:
            api_err = f"Could not apply Claude's edits: {edit_err}"

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
    with open(SUMMARY_FILE, "w") as f:
        f.write(f"{result.get('summary', '').strip()}\n\n"
                f"_{len(result.get('edits', []))} edit(s) applied to `job_monitor.py`._\n")

    print(f"  Fix written to {SCRIPT_FILE}")
    set_output("fix_applied", "true")
    # Success email is sent by the workflow after the PR is opened


if __name__ == "__main__":
    main()
