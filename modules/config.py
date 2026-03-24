"""Encrypted config management using age (via pyrage) and TOML.

The config file is stored as an age-encrypted TOML blob at data/config.age.
The encryption key is a scrypt-derived passphrase key from the master password.

Config schema (as TOML):
    [proton]
    email           = "user@proton.me"
    password        = "..."
    mailbox_password = "..."   # optional, "" if not set
    totp_secret     = "..."    # base32 TOTP secret

    [icloud]
    email    = "user@icloud.com"
    password = "..."           # app-specific password

    [preferences]
    delivery_mode     = "imap"   # "imap" | "mbox"
    poll_interval_min = 60       # integer minutes
"""

import io
from pathlib import Path
from typing import Any

import pyrage
import toml

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.age"

# Keys we expose to the rest of the app
REQUIRED_KEYS = {
    "proton": ["email", "password", "totp_secret"],
    "icloud": ["email", "password"],
    "preferences": ["delivery_mode", "poll_interval_min"],
}

DELIVERY_MODES = ("imap", "mbox")
MIN_INTERVAL_MIN = 15


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Low-level encrypt / decrypt helpers
# ---------------------------------------------------------------------------

def _encrypt(plaintext: str, passphrase: str) -> bytes:
    return pyrage.passphrase.encrypt(plaintext.encode(), passphrase)


def _decrypt(ciphertext: bytes, passphrase: str) -> str:
    return pyrage.passphrase.decrypt(ciphertext, passphrase).decode()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def config_exists() -> bool:
    return CONFIG_PATH.exists()


def save_config(data: dict[str, Any], passphrase: str) -> None:
    """Serialise *data* to TOML, encrypt with *passphrase*, write to disk."""
    _validate(data)
    plaintext = toml.dumps(data)
    ciphertext = _encrypt(plaintext, passphrase)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_bytes(ciphertext)


def load_config(passphrase: str) -> dict[str, Any]:
    """Decrypt the config file and return it as a dict."""
    if not CONFIG_PATH.exists():
        raise ConfigError("No config file found. Run --setup first.")
    try:
        ciphertext = CONFIG_PATH.read_bytes()
        plaintext = _decrypt(ciphertext, passphrase)
    except Exception as exc:
        raise ConfigError(f"Failed to decrypt config (wrong master password?): {exc}") from exc
    data = toml.loads(plaintext)
    _validate(data)
    return data


def update_config(passphrase: str, section: str, key: str, value: Any) -> None:
    """Load config, change one value, and re-save."""
    data = load_config(passphrase)
    if section not in data:
        data[section] = {}
    data[section][key] = value
    save_config(data, passphrase)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(data: dict[str, Any]) -> None:
    for section, keys in REQUIRED_KEYS.items():
        if section not in data:
            raise ConfigError(f"Config missing section [{section}]")
        for key in keys:
            if key not in data[section]:
                raise ConfigError(f"Config missing [{section}].{key}")

    mode = data["preferences"]["delivery_mode"]
    if mode not in DELIVERY_MODES:
        raise ConfigError(
            f"Invalid delivery_mode '{mode}'. Must be one of {DELIVERY_MODES}"
        )

    interval = data["preferences"]["poll_interval_min"]
    if not isinstance(interval, int) or interval < MIN_INTERVAL_MIN:
        raise ConfigError(
            f"poll_interval_min must be an integer >= {MIN_INTERVAL_MIN}"
        )


# ---------------------------------------------------------------------------
# Interactive setup wizard helpers
# ---------------------------------------------------------------------------

INTERVAL_PRESETS = {
    "1": 15,
    "2": 30,
    "3": 60,
    "4": 360,
    "5": 1440,
}


def build_config_interactively() -> tuple[dict[str, Any], str]:
    """Prompt the user for all settings and a master password.

    Returns (config_dict, master_password).
    """
    import getpass

    print("\n=== MailRelay First-Time Setup ===\n")

    proton_email = input("Proton Mail email address: ").strip()
    proton_password = getpass.getpass("Proton Mail password: ")
    proton_mailbox_pw = getpass.getpass(
        "Proton Mail mailbox password (leave blank if none): "
    )
    totp_secret = input("TOTP secret key (base32, from your 2FA setup): ").strip()

    icloud_email = input("\niCloud email address: ").strip()
    icloud_password = getpass.getpass("iCloud app-specific password: ")

    print("\nDelivery mode:")
    print("  1) Automatic IMAP push to iCloud (default)")
    print("  2) Manual MBOX download")
    mode_choice = input("Choose [1/2, default 1]: ").strip() or "1"
    delivery_mode = "imap" if mode_choice != "2" else "mbox"

    print("\nPolling interval:")
    print("  1) 15 minutes")
    print("  2) 30 minutes")
    print("  3) 1 hour (default)")
    print("  4) 6 hours")
    print("  5) 24 hours")
    print("  6) Custom")
    interval_choice = input("Choose [1-6, default 3]: ").strip() or "3"
    if interval_choice in INTERVAL_PRESETS:
        poll_interval = INTERVAL_PRESETS[interval_choice]
    else:
        while True:
            raw = input(f"Enter interval in minutes (min {MIN_INTERVAL_MIN}): ").strip()
            if raw.isdigit() and int(raw) >= MIN_INTERVAL_MIN:
                poll_interval = int(raw)
                break
            print(f"Please enter a whole number >= {MIN_INTERVAL_MIN}.")

    print("\nSet a master password to encrypt your config.")
    while True:
        master_pw = getpass.getpass("Master password: ")
        confirm = getpass.getpass("Confirm master password: ")
        if master_pw == confirm:
            break
        print("Passwords do not match. Try again.")

    config = {
        "proton": {
            "email": proton_email,
            "password": proton_password,
            "mailbox_password": proton_mailbox_pw,
            "totp_secret": totp_secret,
        },
        "icloud": {
            "email": icloud_email,
            "password": icloud_password,
        },
        "preferences": {
            "delivery_mode": delivery_mode,
            "poll_interval_min": poll_interval,
        },
    }

    return config, master_pw
