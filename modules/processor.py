"""Scan an export directory, pair EML + metadata, check dedup, return new emails.

Proton exports produce two files per message:
  {messageID}.eml
  {messageID}.metadata.json

Metadata fields of interest (Proton-specific):
  Subject, SenderAddress, SenderName, ToList, CCList, BCCList,
  Time (Unix timestamp), Unread, LabelIDs, ExternalID, NumAttachments

These are mapped to standard RFC 5322 headers when they are missing from the
raw EML (Proton sometimes omits headers in the raw export).
"""

import email
import email.policy
import json
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from .database import filter_new
from .logger import get_logger

log = get_logger(__name__)


@dataclass
class RichEmail:
    """An enriched email ready for delivery."""
    message_id: str
    message: EmailMessage          # the (possibly augmented) email object
    raw_bytes: bytes               # final RFC 2822 bytes
    metadata: dict = field(default_factory=dict)


class ProcessorError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_and_filter(export_dir: Path) -> list[RichEmail]:
    """Scan *export_dir*, pair EML+metadata, filter already-seen IDs.

    Returns a list of RichEmail objects for new messages only.
    """
    pairs = _find_pairs(export_dir)
    log.info("Found %d exported message(s) in %s", len(pairs), export_dir)

    ids = list(pairs.keys())
    new_ids = filter_new(ids)
    log.info("%d new message(s) after deduplication.", len(new_ids))

    results: list[RichEmail] = []
    for mid in new_ids:
        eml_path, meta_path = pairs[mid]
        try:
            rich = _build_rich_email(mid, eml_path, meta_path)
            results.append(rich)
        except Exception as exc:
            log.warning("Skipping %s — could not process: %s", mid, exc)

    return results


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _find_pairs(export_dir: Path) -> dict[str, tuple[Path, Optional[Path]]]:
    """Return {messageID: (eml_path, meta_path_or_None)} for every .eml found."""
    pairs: dict[str, tuple[Path, Optional[Path]]] = {}

    for eml_path in export_dir.glob("*.eml"):
        mid = eml_path.stem
        meta_path = eml_path.with_suffix(".metadata.json")
        if not meta_path.exists():
            meta_path = None
            log.debug("No metadata file for %s", mid)
        pairs[mid] = (eml_path, meta_path)

    return pairs


def _build_rich_email(
    message_id: str,
    eml_path: Path,
    meta_path: Optional[Path],
) -> RichEmail:
    raw = eml_path.read_bytes()
    msg: EmailMessage = email.message_from_bytes(
        raw, policy=email.policy.default
    )  # type: ignore[assignment]

    metadata: dict = {}
    if meta_path:
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not parse metadata for %s: %s", message_id, exc)

    # Augment missing headers from metadata
    _merge_metadata(msg, metadata)

    final_bytes = msg.as_bytes(policy=email.policy.SMTP)
    return RichEmail(
        message_id=message_id,
        message=msg,
        raw_bytes=final_bytes,
        metadata=metadata,
    )


def _merge_metadata(msg: EmailMessage, meta: dict) -> None:
    """Back-fill standard headers from Proton metadata where missing."""
    if not meta:
        return

    # Subject
    if not msg.get("Subject") and meta.get("Subject"):
        msg["Subject"] = meta["Subject"]

    # From
    if not msg.get("From"):
        sender_addr = meta.get("SenderAddress", "")
        sender_name = meta.get("SenderName", "")
        if sender_addr:
            from_value = (
                f'"{sender_name}" <{sender_addr}>'
                if sender_name
                else sender_addr
            )
            msg["From"] = from_value

    # To
    if not msg.get("To"):
        to_list = meta.get("ToList", [])
        if to_list:
            msg["To"] = _format_address_list(to_list)

    # CC
    if not msg.get("Cc"):
        cc_list = meta.get("CCList", [])
        if cc_list:
            msg["Cc"] = _format_address_list(cc_list)

    # BCC
    if not msg.get("Bcc"):
        bcc_list = meta.get("BCCList", [])
        if bcc_list:
            msg["Bcc"] = _format_address_list(bcc_list)

    # Date — Proton uses Unix timestamp in "Time"
    if not msg.get("Date") and meta.get("Time"):
        import email.utils
        msg["Date"] = email.utils.formatdate(meta["Time"], localtime=False)

    # Message-ID — prefer ExternalID if the EML header is missing
    if not msg.get("Message-ID") and meta.get("ExternalID"):
        msg["Message-ID"] = f"<{meta['ExternalID']}>"

    # X-Proton-* passthrough headers for labels and read status
    if meta.get("LabelIDs"):
        msg["X-Proton-LabelIDs"] = ",".join(str(l) for l in meta["LabelIDs"])
    if "Unread" in meta:
        msg["X-Proton-Unread"] = str(meta["Unread"])


def _format_address_list(entries: list) -> str:
    """Convert Proton address list entries to RFC 5322 address string."""
    parts = []
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("Name", "")
            addr = entry.get("Address", "")
            if addr:
                parts.append(f'"{name}" <{addr}>' if name else addr)
        elif isinstance(entry, str):
            parts.append(entry)
    return ", ".join(parts)
