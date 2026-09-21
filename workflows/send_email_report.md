# Send an HTML Email Report

**Objective**
Deliver a rendered HTML report to an inbox via the Gmail API, with charts and
images embedded inline so they display without the reader clicking
"display images".

**When to use this**
The final step of any workflow whose deliverable is an email. Not specific to
the trend report — `tools/send_gmail.py` takes any HTML file plus an image
manifest.

---

## Inputs

| Input | Required | Description |
|---|---|---|
| `--html-file` | no | Defaults to the `html_file` in the assets manifest |
| `--assets-file` | no | `.tmp/report_assets.json` from `render_report.py` |
| `--to` | no | Comma-separated; falls back to `REPORT_RECIPIENT` in `.env` |
| `--subject` | no | Falls back to the manifest's subject |
| `--attach` | no | Repeatable file attachment |

## Choosing a transport

| | `api` (OAuth) | `smtp` (app password) |
|---|---|---|
| Setup | Cloud project, consent screen, browser consent | One app password |
| Expiry | **Refresh token dies every 7 days** while the app is in Testing | None |
| Best for | Interactive runs | **Scheduled / unattended runs** |

`--transport auto` (the default) uses SMTP when `GMAIL_APP_PASSWORD` is set and
falls back to the OAuth API otherwise.

**For anything scheduled, SMTP is not a preference — it is the only option that
works.** OAuth needs a browser for the initial grant and a live refresh token
afterwards; neither exists on a runner. Verify the SMTP path without sending
anything:

    python tools/preflight.py --only email

That connects to `smtp.gmail.com:465`, authenticates, and disconnects.

**Publishing the OAuth app out of Testing is not a practical option here**
(confirmed 2026-09-21): the *Publish app* button stays disabled until the
Branding page has an application homepage, privacy policy and terms-of-service
URL on a verified domain. That is disproportionate for a personal script, so
scheduled runs should use SMTP rather than fighting the consent screen.

## Credentials — one-time setup

1. Google Cloud Console → same project as the YouTube key is fine.
2. **Enable the Gmail API** for that project.
3. OAuth consent screen → External → add your own address as a **test user**.
   (A personal-account app stays in "testing" forever, which is fine. Refresh
   tokens for a testing app expire after 7 days — if sending starts failing
   with `invalid_grant`, delete `token.json` and re-authorize. Publishing the
   app removes that expiry.)
4. Credentials → Create credentials → OAuth client ID → **Desktop app** →
   download the JSON to the project root as `credentials.json`.
5. First run opens a browser once. Approve, and the grant is cached in
   `token.json`. Both files are gitignored.

Scope requested is `gmail.send` only — it can send as you, and cannot read your
mail.

**For the SMTP transport instead:** enable 2-Step Verification on the account,
create an app password at `myaccount.google.com/apppasswords`, and put it in
`.env` as `GMAIL_APP_PASSWORD`. No Cloud project or consent screen involved.
Treat it like any other secret — it grants send access to the account.

## Steps

0. **First time on a machine** — `python tools/send_gmail.py --authorize-only`
   - Does the browser consent and caches `token.json`, without building or
     sending a message. Isolating it means an auth problem cannot half-send.
   - Skip once `token.json` exists and is valid.
1. **Dry run first** — `python tools/send_gmail.py --dry-run`
   - Produces `.tmp/report_preview.eml`
   - Check: `unresolved_cids` is `[]`, and the MIME tree reads
     `multipart/alternative → text/plain + multipart/related → text/html + images`
   - Open the `.eml` in a mail client to see it exactly as the recipient will.
2. **Send** — `python tools/send_gmail.py`
   - Confirm with the user first. Sending is not reversible.
   - Check: `sent: true` and a `message_id` comes back.
3. **Verify in the client** — open Gmail on web *and* phone. Inline images and
   the 600px layout both need to survive.

## Output

A message in the recipient's inbox. The tool returns the Gmail `message_id` and
`thread_id`.

## Edge cases

| Situation | What to do |
|---|---|
| `credentials.json` missing | Stop and walk the user through the setup above. Do not fall back to SMTP without asking. |
| `Error 403: access_denied`, "has not completed the Google verification process" | The consent screen is in Testing and the sender is not on the allow list. Google Auth Platform → **Audience** → Test users → add the address. Publishing the app removes both this and the 7-day token expiry. |
| `WSGITimeoutError: Timed out waiting for response` | The local callback server gave up before consent finished — the browser often does not auto-open. Use `--authorize-only`, click the printed URL, and raise `--auth-timeout` if needed. |
| `invalid_grant` / refresh fails | Testing-mode refresh tokens expire after 7 days. Delete `token.json` and re-authorize. |
| Images do not display | Check `unresolved_cids` in the dry run. A `cid:` in the HTML with no matching part renders as a broken image. |
| Message over 25 MB | Gmail rejects it. Render with `--no-thumbnails` or lower `DPI`. |
| Recipient sees raw HTML | Their client took the `text/plain` alternative. Expected, and why the plain-text part is generated. |

## Lessons learned

- **2026-09-21** — Inline CID parts beat hotlinked images: most clients block
  remote images until the reader opts in, so a hotlinked chart shows as a hole.
  CID parts ship inside the message and render immediately.
- **2026-09-21** — `EmailMessage.set_content()` + `add_alternative(subtype="html")`
  + `add_related()` on the HTML payload produces the correct nesting for free.
  Hand-building `MIMEMultipart` trees for this is unnecessary.
- **2026-09-21** — Email clients do not reliably honour `prefers-color-scheme`,
  so the report is light-surface only. Charts are baked PNGs and cannot adapt
  anyway.
- **2026-09-21** — The OAuth consent screen defaults to **Testing**, which
  rejects even the project owner's own address with `Error 403: access_denied`
  until it is added under Audience → Test users. Testing mode also expires
  refresh tokens every 7 days, so **any scheduled/recurring send should publish
  the app** rather than living on the test-user list.
- **2026-09-21** — `run_local_server()` does not reliably open a browser, and
  its default callback window is short, so the first attempt died with
  `WSGITimeoutError` after the URL sat unopened in the terminal. Now it runs
  with a 300s window, prints the URL prominently, and is reachable on its own
  via `--authorize-only` so authorization never races a send.
- **2026-09-21** — SMTP transport verified end to end: identical 305 KB message,
  both inline CID charts intact, sent via `smtp.gmail.com:465` with an app
  password. Since `--transport auto` prefers SMTP whenever `GMAIL_APP_PASSWORD`
  is present, the default path no longer touches OAuth at all — the 7-day
  Testing-mode token expiry stops being a concern. Force the other path with
  `--transport api` if ever needed.
