# MailRelay

A self-hosted email forwarding tool that monitors a Proton Mail account on a set interval, exports new emails using the official Proton Mail Export CLI, and forwards them to an iCloud Mail account.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) |
| `wget` | Pre-installed on most Linux distros; on macOS: `brew install wget` |
| Linux x86\_64 **or** macOS (for testing) | The Proton Export CLI download is the Linux x86\_64 build |
| iCloud app-specific password | Generate at [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords |
| Proton Mail TOTP secret | The **base32 secret key** shown when you first enabled 2FA in Proton Mail settings (not a one-time code) |

---

## Project layout

```
mailrelay/
├── main.py               Entry point — CLI flags, scheduler startup
├── config.py             Encrypted config read/write (age + TOML)
├── exporter.py           pexpect automation of proton-mail-export-cli
├── processor.py          EML + metadata merging, dedup check
├── forwarder.py          IMAP push to iCloud (Mode 1)
├── packager.py           MBOX generation and local download server (Mode 2)
├── scheduler.py          APScheduler interval logic
├── database.py           SQLite dedup tracking
├── otp.py                pyotp TOTP generation
├── logger.py             Rotating log setup
├── tools.py              Proton Export CLI download + install manager
├── requirements.txt      Python dependencies
├── setup.sh              One-shot venv + dependency installer
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

### 2. Create the virtual environment and install dependencies

Run the included setup script:

```bash
bash setup.sh
```

This will:
- Verify Python 3.11+ is available
- Create a `.venv` virtual environment in the project directory
- Upgrade pip
- Install all required packages from `requirements.txt`

To activate the environment manually in future sessions:

```bash
source .venv/bin/activate
```

### 3. Run first-time setup

```bash
python main.py --setup
```

The setup wizard will:

1. Check for the **Proton Mail Export CLI** in `tools/proton-export/`. If it is not present, you will be prompted to download it automatically via `wget`. The Linux x86\_64 build is downloaded, extracted, and made executable — no manual steps required.

2. Prompt for your credentials and preferences:

   | Prompt | What to enter |
   |---|---|
   | Proton Mail email | Your full `@proton.me` address |
   | Proton Mail password | Your Proton account password |
   | Proton Mail mailbox password | Your mailbox password if set, or leave blank |
   | TOTP secret key | The base32 secret from your Proton 2FA setup |
   | iCloud email | Your full `@icloud.com` address |
   | iCloud app-specific password | The password generated at appleid.apple.com |
   | Delivery mode | `1` = automatic IMAP push (default), `2` = manual MBOX download |
   | Polling interval | Choose a preset or enter a custom number of minutes (minimum 15) |
   | Master password | A password you choose to encrypt the config file |

All credentials are stored in `data/config.age`, encrypted with [age](https://age-encryption.org/) using your master password. The plaintext is never written to disk.

### 4. Start MailRelay

```bash
python main.py
```

You will be prompted for your master password once. MailRelay then runs in the foreground, syncing Proton Mail on your chosen interval.

> **Headless / server use:** Pass the master password via environment variable to avoid the interactive prompt:
> ```bash
> MAILRELAY_MASTER_PASSWORD="your-master-password" python main.py
> ```

---

## CLI reference

| Flag | Description |
|---|---|
| `--setup` | Run the first-time setup wizard |
| `--run-now` | Trigger an immediate sync, then continue running on schedule |
| `--status` | Print a summary of the last run, next scheduled run, and any pending MBOX downloads |
| `--logs` | Print the last 100 lines of the log file |
| `--config` | Change a single config value without redoing full setup |

### Examples

```bash
# First-time setup
python main.py --setup

# Start the service with an immediate sync
python main.py --run-now

# Check status while the service is running in another terminal
python main.py --status

# Tail the log
python main.py --logs

# Change the delivery mode
python main.py --config
# > Setting to change: preferences.delivery_mode
# > New value: mbox
```

---

## Delivery modes

### Mode 1 — Automatic IMAP push (default)

MailRelay connects to iCloud Mail over IMAP (port 993, SSL) and appends each new email directly to your inbox using your app-specific password. No mail client interaction needed.

If the IMAP push fails for any reason, MailRelay automatically falls back to Mode 2 for that sync cycle and logs the reason.

### Mode 2 — Manual MBOX download

New emails are bundled into a timestamped `.mbox` file and served by a local HTTP server at:

```
http://127.0.0.1:8765/download/<filename>.mbox
```

The download URL is shown in `--status` output and written to the log. Import the `.mbox` into iCloud Mail via **File → Import Mailboxes** in the macOS Mail app.

Once the file is downloaded, MailRelay marks the messages as delivered and deletes the file. If the file is not downloaded before the next sync cycle, MailRelay deletes it and reprocesses the messages on the next run.

---

## How a sync cycle works

1. APScheduler fires at the configured interval (or `--run-now` is called)
2. Any stale un-downloaded MBOX files from the previous cycle are cleaned up; their message IDs are cleared from the database so they will be re-processed
3. A fresh TOTP code is generated from the stored secret
4. `proton-mail-export-cli` is launched via pexpect; credentials and the TOTP code are injected automatically
5. Exported `.eml` and `.metadata.json` pairs are scanned; each message ID is checked against SQLite
6. New messages only: Proton metadata fields are merged into the EML headers
7. Delivery proceeds in the configured mode (IMAP push or MBOX bundle)
8. Delivered message IDs are recorded in SQLite; the export directory is wiped

---

## iCloud app-specific password

A standard Apple ID password will not work for IMAP access. Generate an app-specific password:

1. Go to [appleid.apple.com](https://appleid.apple.com)
2. Sign In and Security → App-Specific Passwords
3. Click **+** and give it a label (e.g. `MailRelay`)
4. Copy the generated password — it will only be shown once

---

## Proton TOTP secret

MailRelay needs the **base32 secret key** that backs your Proton 2FA, not a one-time code. You set this up when you first enabled two-factor authentication in Proton Mail:

- **If you saved the secret at setup time:** use that string directly
- **If you did not save it:** disable and re-enable 2FA in Proton Mail settings — the QR code setup screen displays the raw secret as text

---

## Logs

Logs are written to `data/mailrelay.log` (rotating, max 5 MB, 3 backups). View the tail with:

```bash
python main.py --logs
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

All stdlib modules used (`imaplib`, `sqlite3`, `email`, `mailbox`, `logging`, `tarfile`) require no installation.
