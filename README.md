# MailRelay

A self-hosted email forwarding service for iCloud, Gmail, Outlook, and Proton Mail. Primarily for Proton Mail's paid forwarding restriction, the tool provides a free and open source alternative for forwarding Proton Mail messages to any supported mailbox, while also being able to forward mail from other supported providers.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) |
| Linux x86\_64, macOS, or Windows x86\_64 | Required for the Proton Export CLI (only needed when Proton is a source or destination) |
| iCloud app-specific password | Generate at [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords |
| Gmail app-specific password | Requires 2FA enabled. Generate at [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App passwords |
| Outlook app-specific password | Requires 2FA enabled. Generate at [account.microsoft.com](https://account.microsoft.com) → Security → Advanced security options → App passwords |
| Proton Mail TOTP secret | The **base32 secret key** shown when you first enabled 2FA in Proton Mail settings (not a one-time code). You can also find this in your password manager or TOTP app |

> [!NOTE]
> If your password/TOTP manager stores your TOTP secret as a URL, look for `secret=` in the URL.
>
> **Example:**
> `otpauth://totp/entry%20name:youremail%40proton.me?...&secret=your_secret_here&...`
>
> In this case, your TOTP secret would be `your_secret_here`.

---

## Project layout

```
MailRelay/
├── mailrelay.py          Entry point — CLI flags, venv bootstrap, scheduler startup
├── requirements.txt      Python dependencies
├── setup.sh              Optional shell alias for `python mailrelay.py --setup`
├── modules/
│   ├── __init__.py
│   ├── config.py         Encrypted config read/write (age + TOML)
│   ├── database.py       SQLite dedup tracking
│   ├── exporter.py       Proton CLI automation + IMAP fetch for other providers
│   ├── forwarder.py      IMAP APPEND delivery
│   ├── logger.py         Rotating log setup
│   ├── otp.py            pyotp TOTP generation
│   ├── packager.py       MBOX generation and local download server (Mode 2)
│   ├── processor.py      EML + metadata merging, dedup check
│   ├── scheduler.py      APScheduler interval logic
│   └── tools.py          Proton Export CLI download + install manager
├── tools/
│   └── proton-export/
│       └── proton-mail-export-cli   (downloaded on first run, Proton only)
└── data/
    ├── config.age        Encrypted config (created on first run)
    ├── mailrelay.db      SQLite database (created on first run)
    ├── mailrelay.log     Rotating log file (created on first run)
    ├── exports/          Temporary working directory for CLI exports
    └── downloads/        Temporary directory for generated MBOX files
```

---

## Setup

### 1. Clone or download the project

```bash
git clone <repo-url> mailrelay
cd mailrelay
```

### 2. Run first-time setup

```bash
python mailrelay.py --setup
```

That's it. The setup wizard handles everything in one step:

1. **Checks Python 3.11+** is available
2. **Creates a `.venv`** virtual environment (if one doesn't already exist)
3. **Installs all dependencies** from `requirements.txt`
4. **Restarts itself** inside the venv automatically
5. **Prompts for your credentials and preferences** (see table below)
6. **Downloads the Proton Export CLI** only if Proton is your source or destination — you will be shown the download URL and asked to confirm your OS

| Prompt | What to enter |
|---|---|
| Source service | The account you want to forward **from** |
| Destination service | The account you want to forward **to** |
| Credentials | Email and password (or app-specific password) for each selected service |
| Proton TOTP secret | The base32 secret from your Proton 2FA setup (Proton accounts only) |
| Delivery mode | `1` = automatic IMAP push (default), `2` = manual MBOX download |
| Polling interval | Choose a preset or enter a custom number of minutes (minimum 15) |
| Master password | A password you choose to encrypt the config file |

All credentials are stored in `data/config.age`, encrypted with [age](https://age-encryption.org/) using your master password. The plaintext is never written to disk.

### 3. Start MailRelay

```bash
python mailrelay.py
```

You will be prompted for your master password once. MailRelay then runs in the foreground, syncing on your chosen interval.

> **Headless / server use:** Pass the master password via environment variable to avoid the interactive prompt:
> ```bash
> MAILRELAY_MASTER_PASSWORD="your-master-password" python mailrelay.py
> ```

> **Note:** After setup, activate the venv manually for future sessions if needed:
> ```bash
> source .venv/bin/activate        # bash/zsh
> source .venv/bin/activate.fish   # fish
> .venv\Scripts\activate           # Windows
> ```

---

## CLI reference

| Flag | Description |
|---|---|
| `--setup` | Run the first-time setup wizard (creates venv, installs deps, configures) |
| `--run-now` | Trigger an immediate sync, then continue running on schedule |
| `--status` | Print a summary of the last run, next scheduled run, and any pending MBOX downloads |
| `--logs` | Print the last 100 lines of the log file |
| `--config` | Change a single config value without redoing full setup |
| `--debug` | Show all debug output on the console |

### Examples

```bash
# First-time setup (installs deps + configures)
python mailrelay.py --setup

# Start the service with an immediate sync
python mailrelay.py --run-now

# Trigger a sync while the service is running (type in the service terminal)
now

# Check status while the service is running in another terminal
python mailrelay.py --status

# Tail the log
python mailrelay.py --logs

# Change the delivery mode
python mailrelay.py --config
# > Setting to change: preferences.delivery_mode
# > New value: mbox
```

---

## Delivery modes

### Mode 1 — Automatic IMAP push (default)

MailRelay connects to the destination mailbox over IMAP (port 993, SSL) and appends each new email directly to the inbox using your app-specific password. No mail client interaction needed. Supported for iCloud, Gmail, and Outlook.

If the IMAP push fails for any reason, MailRelay automatically falls back to Mode 2 for that sync cycle and logs the reason.

### Mode 2 — Manual MBOX download

New emails are bundled into a timestamped `.mbox` file and served by a local HTTP server at:

```
http://127.0.0.1:8765/download/<filename>.mbox
```

The download URL is shown in `--status` output and written to the log. Import the `.mbox` into macOS Mail via **File → Import Mailboxes**.

Once the file is downloaded, MailRelay marks the messages as delivered and deletes the file. If the file is not downloaded before the next sync cycle, MailRelay deletes it and reprocesses the messages on the next run.

### Mode 3 — Proton to Proton (automatic)

When both source and destination are Proton accounts, MailRelay uses the Proton Export CLI's restore operation to import emails directly into the destination account. No IMAP or MBOX involved.

---

## How a sync cycle works

1. APScheduler fires at the configured interval (or `--run-now` is called, or `now` is typed)
2. Any stale un-downloaded MBOX files from the previous cycle are cleaned up
3. Emails are fetched from the source (Proton CLI export, or IMAP fetch for Gmail/Outlook/iCloud)
4. Each message ID is checked against SQLite — already-delivered messages are skipped
5. New messages are delivered in the configured mode
6. Delivered message IDs are recorded in SQLite; the export directory is wiped

---

## Proton TOTP secret

MailRelay needs the **base32 secret key** that backs your Proton 2FA, not a one-time code. You set this up when you first enabled two-factor authentication in Proton Mail:

- **If you saved the secret at setup time:** use that string directly
- **If you did not save it:** disable and re-enable 2FA in Proton Mail settings — the QR code setup screen displays the raw secret as text

---

## Logs

Logs are written to `data/mailrelay.log` (rotating, max 5 MB, 3 backups). View the tail with:

```bash
python mailrelay.py --logs
```

Or follow live:

```bash
tail -f data/mailrelay.log
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `typer` | CLI flags and interface |
| `pyrage` | age encryption for the config file |
| `toml` | Config file format |
| `pyotp` | TOTP code generation |
| `pexpect` | Driving the Proton Export CLI interactively |
| `APScheduler` | Background polling interval |
| `fastapi` + `uvicorn` | Local MBOX download server |

All stdlib modules used (`imaplib`, `sqlite3`, `email`, `mailbox`, `logging`, `tarfile`, `zipfile`, `urllib`) require no installation.

> [!NOTE]
> All product and company names are trademarks™ or registered® trademarks of their respective holders. Use of them does not imply any affiliation with or endorsement by them.
