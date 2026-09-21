"""Send an HTML email with inline (CID) images through the Gmail API.

Generic — any workflow in this repo can use it, not just the trend report.

MIME structure, which is what makes inline images actually render:

    multipart/related
      multipart/alternative
        text/plain          <- fallback for clients that refuse HTML
        text/html           <- references images as <img src="cid:name">
      image/png  (Content-ID: <name>)   <- one part per inline image
      ...

Inline CID parts beat hotlinked images here: most clients block remote images
until the reader clicks "display images", but CID parts are already in the
message and render immediately.

Two transports:
  api  (default) OAuth desktop flow, scope gmail.send only — cannot read your
       mail. First run opens a browser once and caches the grant in token.json.
  smtp           Gmail SMTP with an app password. No expiry, no consent screen,
                 so this is the one to use for scheduled runs.
--transport auto picks smtp when GMAIL_APP_PASSWORD is set, otherwise api.
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import pathlib
import re
import sys
from email.message import EmailMessage

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import PROJECT_ROOT, emit, fail, get_env, get_logger, read_json, tmp_path

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465


def send_via_smtp(message: EmailMessage, address: str, app_password: str) -> None:
    """Send through Gmail SMTP with an app password.

    Why this exists alongside the API transport: a Desktop OAuth app stuck in
    "Testing" has refresh tokens that expire every 7 days, and Google will not
    let you publish out of Testing without a homepage, privacy policy and terms
    of service on a verified domain — disproportionate for a personal script.
    An app password has no expiry, so this is the transport for unattended
    scheduled runs. The API transport stays the default for interactive use.
    """
    import smtplib

    del message["From"]  # assigning a present header appends a duplicate
    message["From"] = address
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.login(address, app_password)
        smtp.send_message(message)


def get_service(credentials_file: pathlib.Path, token_file: pathlib.Path,
                *, auth_timeout: int = 300):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except ValueError:
            log.warning("token.json is unreadable or has different scopes — re-authorizing.")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired Gmail token")
            try:
                creds.refresh(Request())
            except Exception as exc:
                log.warning("Refresh failed (%s) — re-authorizing.", str(exc)[:100])
                creds = None
        if not creds or not creds.valid:
            if not credentials_file.exists():
                fail(
                    f"OAuth client file not found: {credentials_file}",
                    hint=("Google Cloud Console -> APIs & Services -> Credentials -> "
                          "Create OAuth client ID -> Desktop app -> download JSON to "
                          f"{credentials_file}. Also enable the Gmail API for that project."),
                )
            log.info("Opening a browser for one-time Gmail authorization")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            try:
                # The default callback window is short and the browser does not
                # always open by itself, which strands the flow. Five minutes is
                # enough to click through the unverified-app interstitial.
                creds = flow.run_local_server(
                    port=0,
                    timeout_seconds=auth_timeout,
                    open_browser=True,
                    authorization_prompt_message=(
                        "\n" + "=" * 74
                        + "\nAUTHORIZE GMAIL — open this URL if a browser did not appear:\n\n{url}\n\n"
                        "On the 'Google hasn't verified this app' screen choose\n"
                        "  Advanced  ->  Go to <app name> (unsafe)\n"
                        "That warning only means the app has not been through Google review.\n"
                        + "=" * 74 + "\n"
                    ),
                    success_message="Authorized. You can close this tab and return to the terminal.",
                )
            except Exception as exc:
                if "Timed out" in str(exc) or "WSGITimeout" in type(exc).__name__:
                    fail(
                        f"Timed out after {auth_timeout}s waiting for Google authorization.",
                        hint=("Re-run and complete the browser consent. If the browser did not open, "
                              "copy the URL printed above into one. Raise the window with "
                              "--auth-timeout 600 if you need longer."),
                    )
                raise
            token_file.write_text(creds.to_json(), encoding="utf-8")
            log.info("Saved credentials to %s", token_file)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def html_to_text(html: str) -> str:
    """Rough plain-text alternative. Not pretty — it exists so the message is
    not flagged as HTML-only, and so text-only clients see something sane."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|h1|h2|h3|li|tr|div|table)>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "  - ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as html_mod
    text = html_mod.unescape(text)
    text = text.replace("\xa0", " ")  # layout spacers become stray bytes otherwise
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def build_message(*, sender: str | None, to: list[str], subject: str, html_body: str,
                  inline_images: dict[str, str], attachments: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if sender:
        message["From"] = sender

    # set_content + add_alternative gives multipart/alternative; adding the
    # images as related parts on the HTML payload gives multipart/related.
    message.set_content(html_to_text(html_body))
    message.add_alternative(html_body, subtype="html")
    html_part = message.get_payload()[-1]

    for cid, path_str in inline_images.items():
        path = pathlib.Path(path_str)
        if not path.exists():
            log.warning("Inline image missing, skipping: %s", path)
            continue
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "image/png").split("/", 1)
        html_part.add_related(path.read_bytes(), maintype=maintype, subtype=subtype,
                              cid=f"<{cid}>", filename=path.name)

    for path_str in attachments:
        path = pathlib.Path(path_str)
        if not path.exists():
            log.warning("Attachment missing, skipping: %s", path)
            continue
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=path.name)

    return message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-file", default=None, help="Default .tmp/report.html")
    parser.add_argument("--assets-file", default=None,
                        help="Manifest from render_report.py (default .tmp/report_assets.json)")
    parser.add_argument("--to", default=None, help="Comma-separated. Defaults to REPORT_RECIPIENT in .env")
    parser.add_argument("--subject", default=None, help="Overrides the subject in the assets manifest")
    parser.add_argument("--from-address", default=None, help="Defaults to the authorized account")
    parser.add_argument("--attach", action="append", default=[], help="Repeatable file attachment")
    parser.add_argument("--transport", choices=["auto", "api", "smtp"], default="auto",
                        help="auto = SMTP when GMAIL_APP_PASSWORD is set, else the OAuth API. "
                             "SMTP has no token expiry, so scheduled runs should use it.")
    parser.add_argument("--authorize-only", action="store_true",
                        help="Run the OAuth consent and cache token.json, without sending anything")
    parser.add_argument("--auth-timeout", type=int, default=300,
                        help="Seconds to wait for the browser consent (default 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write the .eml and report its structure, without sending")
    args = parser.parse_args()

    if args.authorize_only:
        credentials_file = PROJECT_ROOT / (get_env("GOOGLE_CREDENTIALS_FILE") or "credentials.json")
        token_file = PROJECT_ROOT / (get_env("GOOGLE_TOKEN_FILE") or "token.json")
        get_service(credentials_file, token_file, auth_timeout=args.auth_timeout)
        emit({"authorized": True, "token_file": str(token_file),
              "next": "python tools/send_gmail.py"})

    assets_path = pathlib.Path(args.assets_file) if args.assets_file else tmp_path("report_assets.json")
    assets = read_json(assets_path) if assets_path.exists() else {}

    html_path = pathlib.Path(args.html_file) if args.html_file else pathlib.Path(
        assets.get("html_file") or tmp_path("report.html"))
    if not html_path.exists():
        fail(f"HTML file not found: {html_path}", hint="Run: python tools/render_report.py")
    html_body = html_path.read_text(encoding="utf-8")

    recipients_raw = args.to or get_env("REPORT_RECIPIENT")
    if not recipients_raw:
        fail("No recipient.", hint="Pass --to you@example.com or set REPORT_RECIPIENT in .env")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    subject = args.subject or assets.get("subject") or "Tech Trend Report"
    inline_images = assets.get("inline_images", {})

    message = build_message(sender=args.from_address, to=recipients, subject=subject,
                            html_body=html_body, inline_images=inline_images,
                            attachments=args.attach)
    raw_bytes = message.as_bytes()

    if args.dry_run:
        eml_path = tmp_path("report_preview.eml")
        eml_path.write_bytes(raw_bytes)
        cids = re.findall(r'src="cid:([^"]+)"', html_body)
        missing = [c for c in cids if c not in inline_images]
        emit({
            "dry_run": True,
            "to": recipients,
            "subject": subject,
            "mime_structure": _structure(message),
            "inline_parts": len(inline_images),
            "cid_references": len(cids),
            "unresolved_cids": missing,
            "message_bytes": len(raw_bytes),
            "eml_file": str(eml_path),
        })

    if len(raw_bytes) > 25 * 1024 * 1024:
        fail(f"Message is {len(raw_bytes) / 1e6:.1f} MB, over Gmail's 25 MB limit.",
             hint="Drop some thumbnails or lower DPI in render_report.py.")

    app_password = get_env("GMAIL_APP_PASSWORD")
    transport = args.transport
    if transport == "auto":
        transport = "smtp" if app_password else "api"

    if transport == "smtp":
        address = args.from_address or get_env("GMAIL_ADDRESS") or get_env("REPORT_RECIPIENT")
        if not app_password:
            fail("GMAIL_APP_PASSWORD is not set.",
                 hint=("myaccount.google.com/apppasswords (requires 2-Step Verification), "
                       "then add GMAIL_APP_PASSWORD= to .env. Set GMAIL_ADDRESS= too if the "
                       "sending account differs from REPORT_RECIPIENT."))
        if not address:
            fail("No sending address for SMTP.", hint="Set GMAIL_ADDRESS= in .env or pass --from-address.")
        try:
            send_via_smtp(message, address, app_password.replace(" ", ""))
        except Exception as exc:
            fail(f"SMTP send failed: {type(exc).__name__}: {str(exc)[:200]}",
                 hint=("Check GMAIL_APP_PASSWORD (16 characters, spaces are ignored) and that "
                       "2-Step Verification is on for that account."))
        emit({
            "sent": True, "transport": "smtp", "to": recipients, "from": address,
            "subject": subject, "inline_images": len(inline_images),
            "message_bytes": len(raw_bytes),
        })

    credentials_file = PROJECT_ROOT / (get_env("GOOGLE_CREDENTIALS_FILE") or "credentials.json")
    token_file = PROJECT_ROOT / (get_env("GOOGLE_TOKEN_FILE") or "token.json")
    service = get_service(credentials_file, token_file, auth_timeout=args.auth_timeout)

    from googleapiclient.errors import HttpError
    try:
        sent = service.users().messages().send(
            userId="me", body={"raw": base64.urlsafe_b64encode(raw_bytes).decode()}
        ).execute()
    except HttpError as exc:
        fail(f"Gmail rejected the message: {exc}",
             hint="Confirm the Gmail API is enabled and that token.json carries the gmail.send scope.")

    emit({
        "sent": True,
        "transport": "api",
        "message_id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "to": recipients,
        "subject": subject,
        "inline_images": len(inline_images),
        "message_bytes": len(raw_bytes),
    })


def _structure(part, depth: int = 0) -> list[str]:
    """Flatten the MIME tree so a dry run can be eyeballed."""
    lines = [f"{'  ' * depth}{part.get_content_type()}"
             + (f"  cid={part.get('Content-ID')}" if part.get("Content-ID") else "")]
    if part.is_multipart():
        for sub in part.iter_parts():
            lines.extend(_structure(sub, depth + 1))
    return lines


if __name__ == "__main__":
    main()
