#!/usr/bin/env python3
"""Mirror FMX attachments into a local folder tree.

For each module you configure this lists records, finds their attachments, and
downloads any it does not already hold into:

    <output>/<module>/<record-id>/<filename>

Point <output> at a Google Drive, OneDrive or Dropbox folder, or an external
drive, and the sync client does the uploading.

Standard library only. No "pip install". Python 3.9 or newer.

What a run costs
----------------
API calls, rather than bytes or CPU, are the thing worth economising on:

    Listing records (all modules, projected) ... ~20-30 calls
    An attachment already held ................. 0 calls
    A newly discovered attachment .............. 1 call
    Transferring the file ...................... 0 calls, it comes from storage

So a run with nothing new to fetch costs only the record listing, and a new
attachment costs a single call.

Worth knowing before changing any of this
-----------------------------------------
* Attachments are nested inside customFields[] entries as an attachmentIDs
  array, under whatever field names a site has configured. We look for that key
  anywhere in a record rather than matching on a name.
* Attachments added to a response or comment belong to the record's actions,
  which the "fields" projection has to ask for explicitly.
* The download endpoint replies with a redirect to storage. That URL is fetched
  with none of our own headers, and it carries the filename in its query
  string, so there is no separate metadata call.
* Only query parameters known to be supported are sent, and the projection we
  asked for is checked against what came back.

This is a one-way mirror: it adds and overwrites, and never deletes.
"""


import argparse
import dataclasses
import datetime
import email.message
import getpass
import json
import os
import random
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

if sys.version_info < (3, 9):
    sys.stderr.write(
        "This script needs Python 3.9 or newer; you are running %d.%d.\n"
        % sys.version_info[:2]
    )
    raise SystemExit(2)

# A Windows console cannot represent every character a filename may contain, and
# printing one would otherwise end the run. Substitute rather than fail.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


#region Constants

STATE_FILENAME = ".fmx_sync_state.json"
STATE_SCHEMA_VERSION = 1
PART_SUFFIX = ".fmxpart"

# Only parameters we know are supported ever get sent.
ALLOWED_QUERY_PARAMS = frozenset({"limit", "offset", "fields", "fromDate", "toDate"})

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

# Storage answers one of these when the URL is no longer usable; a fresh one is
# the only fix.
BLOB_REAUTH_STATUS = frozenset({403, 404, 409})

MAX_PAGES = 10000

# Outcomes that add a state entry, and so are the only ones worth saving for.
STATE_CHANGING = frozenset({"downloaded", "copied", "verified"})

# Below this many records, a missing projected collection is not evidence of a typo.
PROJECTION_GUARD_MIN_RECORDS = 10
CONSECUTIVE_FAILURE_LIMIT = 25
COPY_CHUNK = 1024 * 1024

# Windows stops at 260 characters unless long paths are enabled.
MAX_COMPONENT_BYTES = 200
MAX_FULL_PATH_CHARS = 240

WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)
ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/|?*\x5c\x00-\x1f\x7f]')

# Probed by --discover, since module paths vary between sites.
CANDIDATE_PATHS = [
    ("maintenance-requests", "/maintenance-requests"),
    ("technology-requests", "/technology-requests"),
    ("transportation-requests", "/transportation-requests"),
    ("planning-requests", "/planning-requests"),
    ("schedule-requests", "/scheduling/requests"),
    ("schedule-occurrences", "/scheduling/occurrences"),
    ("planned-maintenance-tasks", "/planned-maintenance/tasks"),
    ("planned-maintenance-occurrences", "/planned-maintenance/occurrences"),
    ("purchase-orders", "/purchase-orders"),
    ("invoices", "/invoices"),
    ("equipment", "/equipment"),
    ("buildings", "/buildings"),
    ("users", "/users"),
]

CANDIDATE_COLLECTIONS = [
    "actions", "occurrences", "tasks", "logs", "lineItems",
    "items", "inventory", "transactions", "responses", "notes",
]

BASE_FIELDS = "id,customFields(attachmentIDs)"
ACTION_FIELDS = "actions(id,createdTimeUtc,isPrivate,customFields(attachmentIDs))"

# A handful of records is enough for --discover to tell whether a module is
# reachable, has any records, and which collections it will expand. Asking for each
# collection by id only keeps the reply small.
DISCOVER_SAMPLE_RECORDS = 3
DISCOVER_FIELDS = BASE_FIELDS + "," + ",".join(
    "%s(id)" % collection for collection in CANDIDATE_COLLECTIONS)

#endregion

#region Errors

class FmxError(Exception):
    """Fatal: abandon the run."""


class TransientError(Exception):
    """Worth retrying."""


class ExpiredUrlError(TransientError):
    """Storage would not accept the download URL; the caller needs a fresh one."""


class ModuleNotEnabled(Exception):
    """This module is not available on the site."""
#endregion

#region Value types

@dataclasses.dataclass(frozen=True)
class Module:
    name: str        # folder name, and what you pass to --modules
    api_path: str    # what the API actually wants, e.g. /scheduling/requests
    fields: str      # projection; see Surprise 2 in the module docstring
    date_window: Optional[Dict[str, str]] = None


@dataclasses.dataclass(frozen=True)
class Found:
    """One attachment reference discovered on one record."""
    attachment_id: int
    container: str   # "record" or "action 9001"; shown in the log


@dataclasses.dataclass(frozen=True)
class CachedFile:
    """Something we already fetched during this run."""
    path: Path
    raw_name: Optional[str]
    content_type: Optional[str]


@dataclasses.dataclass
class Session:
    base_url: str
    auth_header: str
    timeout: float
    retries: int
    throttle: float
    user_email: str
    fmx_opener: urllib.request.OpenerDirector
    blob_opener: urllib.request.OpenerDirector
    fmx_calls: int = 0


@dataclasses.dataclass
class Stats:
    records: int = 0
    found: int = 0
    downloaded: int = 0
    copied: int = 0        # served from the within-run cache, no transfer
    verified: int = 0      # already on disk, size confirmed by a free blob HEAD
    skipped: int = 0       # already in state, cost nothing at all
    failed: int = 0
    bytes_downloaded: int = 0   # actually crossed the network
    bytes_copied: int = 0       # duplicated locally from an earlier fetch
    modules_missing: List[str] = dataclasses.field(default_factory=list)
    modules_empty: List[str] = dataclasses.field(default_factory=list)
    failures: List[str] = dataclasses.field(default_factory=list)
    projection_warnings: List[str] = dataclasses.field(default_factory=list)
#endregion

#region Console output

_QUIET = False
_VERBOSE = False


def log(message: str = "") -> None:
    if not _QUIET:
        print(message, flush=True)


def detail(message: str) -> None:
    """Only shown with -v. Use for URLs, headers, retries."""
    if _VERBOSE:
        print("      . %s" % message, flush=True)


def warn(message: str) -> None:
    print("  !! %s" % message, file=sys.stderr, flush=True)


def format_bytes(count: int) -> str:
    """1024-based, whole bytes but one decimal above: 512 B, 216.0 KB, 3.7 MB."""
    size = float(count)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return "%d B" % size if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


def format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm %02ds" % (total // 60, total % 60)
    return "%dh %02dm" % (total // 3600, (total % 3600) // 60)


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

#endregion

#region HTTP layer

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects so the storage URL can be fetched on its own terms.

    The download endpoint redirects to storage, and that URL must be requested
    without our own headers, so we handle the hop rather than letting urllib
    follow it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_openers() -> Tuple[urllib.request.OpenerDirector, urllib.request.OpenerDirector]:
    """Return (fmx_opener, blob_opener); separate so credentials cannot reach storage."""
    return urllib.request.build_opener(NoRedirect), urllib.request.build_opener()


def basic_auth_header(email_address: str, password: str) -> str:
    import base64
    raw = ("%s:%s" % (email_address, password)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def classify_http_error(err: urllib.error.HTTPError, what: str) -> Exception:
    """Turn an HTTPError into either a retryable or a fatal error."""
    if err.code == 401:
        # Never retried: repeated bad logins can lock the account out.
        return FmxError(
            "Authentication failed (HTTP 401). Check FMX_EMAIL / FMX_PASSWORD."
        )
    if err.code in RETRYABLE_STATUS:
        return TransientError("%s: HTTP %d" % (what, err.code))
    return FmxError("%s: HTTP %d" % (what, err.code))


def retry(operation, what: str, attempts: int):
    """Run operation(), retrying transient failures with jittered backoff."""
    delay = 1.0
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (FmxError, ExpiredUrlError):
            # ExpiredUrlError is a TransientError, but retrying the same signed URL
            # cannot help, so it goes straight to the caller.
            raise
        except TransientError as exc:
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # URLError wraps socket-level problems; OSError covers the rest.
            last = TransientError("%s: %s" % (what, exc))
        if attempt < attempts:
            pause = min(delay, 30.0) * (1 + random.uniform(-0.25, 0.25))
            detail("retry %d/%d in %.1fs (%s)" % (attempt, attempts - 1, pause, last))
            time.sleep(pause)
            delay *= 2
    raise FmxError("%s: giving up after %d attempts (%s)" % (what, attempts, last))


def fmx_get(session: Session, path: str, params: Optional[Dict[str, Any]] = None,
            expect_redirect: bool = False) -> Any:
    """Make one authenticated GET against the FMX API, with retries.

    Returns the decoded JSON body, or the Location header when expect_redirect is
    set. Parameters are checked against ALLOWED_QUERY_PARAMS before being sent.
    """
    params = params or {}
    unknown = set(params) - ALLOWED_QUERY_PARAMS
    if unknown:
        raise FmxError("unsupported query parameter(s): %s"
                       % ", ".join(sorted(unknown)))

    url = session.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": session.auth_header}
    if not expect_redirect:
        headers["Accept"] = "application/json"

    def once():
        if session.throttle:
            time.sleep(session.throttle)
        session.fmx_calls += 1
        detail("GET %s%s" % (url, " (expecting a 302)" if expect_redirect else ""))
        try:
            with session.fmx_opener.open(
                    urllib.request.Request(url, headers=headers),
                    timeout=session.timeout) as response:
                if expect_redirect:
                    raise FmxError("%s: expected a 302 redirect but got HTTP %s"
                                   % (path, getattr(response, "status", "?")))
                body = response.read()
        except urllib.error.HTTPError as err:
            # With NoRedirect installed a 3xx arrives here rather than being
            # followed, and HTTPError carries the headers we need.
            if expect_redirect and err.code in REDIRECT_STATUS:
                location = err.headers.get("Location")
                err.close()
                if not location:
                    raise TransientError("%s: redirect carried no Location" % path)
                return location
            if err.code == 404 and not expect_redirect:
                raise ModuleNotEnabled(path)
            raise classify_http_error(err, "GET %s" % path)
        try:
            return json.loads(body or b"null")
        except ValueError as exc:
            raise TransientError("GET %s: response was not JSON (%s)" % (path, exc))

    return retry(once, "GET %s" % path, session.retries)


def api_iter_records(session: Session, module: Module, page_size: int,
                     max_records: Optional[int]) -> Iterator[dict]:
    """Yield every record in a module, always paging explicitly with limit/offset."""
    offset = 0
    produced = 0
    for _ in range(MAX_PAGES):
        # Checked before fetching, so --max-records never costs an extra request.
        if max_records is not None and produced >= max_records:
            return
        params: Dict[str, Any] = {"limit": page_size, "offset": offset,
                                  "fields": module.fields}
        if module.date_window:
            # Opt-in: filters on event date, so records outside it are not seen.
            params.update(module.date_window)

        page = fmx_get(session, module.api_path, params)
        if not isinstance(page, list):
            raise FmxError("%s: expected a JSON array, got %s"
                           % (module.api_path, type(page).__name__))
        if not page:
            return

        # More rows than we asked for means the whole collection arrived at once,
        # so use it and stop rather than paging again.
        oversized = len(page) > page_size
        if oversized:
            warn("%s returned %d records for limit=%d; treating that as the whole "
                 "collection." % (module.api_path, len(page), page_size))

        for record in page:
            if max_records is not None and produced >= max_records:
                return
            produced += 1
            yield record

        if oversized or len(page) < page_size:
            return
        offset += page_size

    warn("%s: stopped after %d pages as a safety limit." % (module.api_path, MAX_PAGES))


def api_get_record(session: Session, module: Module, record_id: int) -> Optional[dict]:
    """Fetch one record by id (used by --record-ids)."""
    try:
        record = fmx_get(session, "%s/%d" % (module.api_path, record_id),
                              {"fields": module.fields})
    except ModuleNotEnabled:
        return None
    return record if isinstance(record, dict) else None


def api_resolve_blob_url(session: Session, attachment_id: int) -> str:
    """Turn an attachment id into a temporary storage URL, carrying its filename."""
    return fmx_get(session, "/attachments/%d/download" % attachment_id,
                   expect_redirect=True)


def filename_from_content_disposition(value: Optional[str]) -> Optional[str]:
    """Parse a filename out of a Content-Disposition value.

    Handles the quoted and unquoted forms plus RFC 2231 encoding. (cgi.parse_header
    was removed in Python 3.13, so the email module is the stdlib route now.)
    """
    if not value:
        return None
    message = email.message.EmailMessage()
    message["Content-Disposition"] = value
    try:
        parsed = message.get_filename()
    except Exception:
        parsed = None

    # A strict parser stops at the first space in an unquoted value, which would
    # truncate the name. Recover the whole thing for a bare filename= only.
    match = re.search(r'(?:^|;)\s*filename=\s*(?!")([^;]+)$', value)
    if match:
        candidate = match.group(1).strip()
        if parsed is None or (len(candidate) > len(parsed)
                              and candidate.startswith(parsed)):
            return candidate
    return parsed


def filename_from_blob_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the filename and content type out of the storage URL itself.

    They travel as the rscd and rsct query parameters, which is why resolving the
    redirect is enough and no metadata call is needed.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    disposition = query.get("rscd", [None])[0]
    content_type = query.get("rsct", [None])[0]
    return filename_from_content_disposition(disposition), content_type


def blob_head(session: Session, url: str) -> Optional[int]:
    """Return the file's size without transferring it, or None if unavailable.

    Goes to storage rather than the API, so it confirms a file already on disk
    without costing a call or a download.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with session.blob_opener.open(request, timeout=session.timeout) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length is not None else None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None      # not worth reporting; we simply download instead


def blob_download(session: Session, url: str, destination: Path) -> Tuple[int, Optional[str]]:
    """Stream the file to destination via a temporary .fmxpart, headers omitted.

    Returns (bytes_written, content_type). See NoRedirect for why no headers.
    """
    part = destination.with_name(destination.name + PART_SUFFIX)
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(url)   # deliberately header-free
    if _VERBOSE:
        detail("blob GET headers sent: %s"
               % (sorted(k for k in request.headers) or "none"))
    try:
        with session.blob_opener.open(request, timeout=session.timeout) as response:
            expected = response.headers.get("Content-Length")
            content_type = response.headers.get("Content-Type")
            written = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(COPY_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
        if expected is not None and written != int(expected):
            part.unlink(missing_ok=True)
            raise TransientError(
                "short read: got %d bytes, expected %s" % (written, expected)
            )
    except urllib.error.HTTPError as err:
        part.unlink(missing_ok=True)
        body = b""
        try:
            body = err.read()[:400]
        except Exception:
            pass
        if err.code == 403 and b"AuthenticationFailed" in body:
            # On a freshly issued URL this points at headers being sent that
            # should not be; see NoRedirect.
            raise TransientError("storage declined the request (403)")
        if err.code in BLOB_REAUTH_STATUS:
            raise ExpiredUrlError("storage returned HTTP %d" % err.code)
        raise FmxError("blob download failed: HTTP %d" % err.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise TransientError("blob download failed: %s" % exc)

    # Atomic on Windows and POSIX, so no reader ever sees a half-written file.
    os.replace(str(part), str(destination))
    return written, content_type
#endregion


#region Finding attachments inside a record

def find_attachments(node: Any, skip_private: bool = False,
                     container: str = "record") -> List[Found]:
    """Collect every attachment reference in a record, at any depth.

    Looks for the attachmentIDs key anywhere rather than reaching into
    customFields[], since sites name those fields freely and attachments also hang
    off actions. Only finds what the module's "fields" projection asked for.
    """
    found: List[Found] = []

    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "attachmentIDs" and isinstance(child, list):
                    for item in child:
                        if isinstance(item, int):
                            found.append(Found(item, where))
                    continue
                if key == "actions" and isinstance(child, list):
                    for action in child:
                        if not isinstance(action, dict):
                            continue
                        if skip_private and action.get("isPrivate") is True:
                            continue
                        label = "action %s" % action.get("id", "?")
                        walk(action, label)
                    continue
                walk(child, where)
        elif isinstance(value, list):
            for item in value:
                walk(item, where)

    walk(node, container)

    # Sorted so the same attachment always wins a filename collision.
    unique: Dict[int, Found] = {}
    for item in found:
        unique.setdefault(item.attachment_id, item)
    return [unique[key] for key in sorted(unique)]


def projection_top_level_keys(fields: str) -> List[str]:
    """Top-level names in a projection string, used to check what came back.

    "id,customFields(attachmentIDs),actions(id,...)" -> ["id", "customFields", "actions"]
    """
    keys: List[str] = []
    depth = 0
    current = ""
    for char in fields:
        if char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            continue
        elif char == "," and depth == 0:
            if current.strip():
                keys.append(current.strip())
            current = ""
            continue
        if depth == 0:
            current += char
    if current.strip():
        keys.append(current.strip())
    return keys
#endregion


#region Filenames

def split_extension(name: str) -> Tuple[str, str]:
    """Split "report.final.pdf" into ("report.final", ".pdf"); no dot means no suffix."""
    stem, dot, extension = name.rpartition(".")
    return (stem, "." + extension) if dot else (name, "")


def sanitize_filename(raw: Optional[str], attachment_id: int) -> str:
    """Turn an uploaded filename into one safe path component.

    Treated as untrusted input: it may carry separators, traversal, or characters
    that are illegal on Windows.
    """
    fallback = "attachment-%d" % attachment_id
    if not raw:
        return fallback

    # Last path segment only, which defeats traversal and drive prefixes.
    candidate = raw.replace("\\", "/").split("/")[-1]
    candidate = re.sub(r"^[A-Za-z]:", "", candidate)
    candidate = candidate.strip()
    if candidate in ("", ".", ".."):
        return fallback

    # Replaced rather than deleted, so distinct names stay distinct. Spaces and
    # parentheses are deliberately kept: real filenames use them.
    candidate = ILLEGAL_FILENAME_CHARS.sub("_", candidate)

    # Windows drops trailing dots and spaces, which breaks the next run's
    # "does this already exist?" check.
    candidate = candidate.rstrip(". ")
    if not candidate:
        return fallback

    stem, suffix = split_extension(candidate)
    # NUL.pdf and friends cannot be opened on Windows whatever the extension.
    if (stem if suffix else candidate).upper() in WINDOWS_RESERVED:
        candidate = "_" + candidate

    return candidate or fallback


def truncate_component(name: str, attachment_id: int, budget_chars: int) -> str:
    """Shorten a filename to fit byte and path-length budgets, keeping the extension."""
    stem, suffix = split_extension(name)
    suffix = suffix[:11]          # a dot plus at most ten characters

    def too_long(value: str) -> bool:
        return (len(value.encode("utf-8")) > MAX_COMPONENT_BYTES
                or len(value) > max(budget_chars, 8))

    trimmed = stem
    while trimmed and too_long(trimmed + suffix):
        trimmed = trimmed[:-1]
    if not trimmed:
        return "attachment-%d%s" % (attachment_id, suffix)
    return trimmed + suffix


def fit_to_filesystem(name: str, destination_dir: Path, attachment_id: int) -> str:
    """Ensure destination_dir/name is short enough to actually create."""
    budget = MAX_FULL_PATH_CHARS - len(str(destination_dir)) - 1 - len(PART_SUFFIX)
    if (len(name.encode("utf-8")) <= MAX_COMPONENT_BYTES) and (len(name) <= budget):
        return name
    return truncate_component(name, attachment_id, budget)


def choose_filename(raw: Optional[str], attachment_id: int, destination_dir: Path,
                    taken: Dict[str, int]) -> str:
    """Pick the final filename for one attachment inside one record folder.

    "taken" maps lowercased filename -> attachment id, lowercased so a clash is
    treated the same way on case-insensitive and case-sensitive filesystems.
    """
    name = fit_to_filesystem(sanitize_filename(raw, attachment_id),
                             destination_dir, attachment_id)
    key = name.lower()
    owner = taken.get(key)
    if owner is None or owner == attachment_id:
        taken[key] = attachment_id
        return name

    # Disambiguate with the attachment id, not a counter, so the suffix is stable.
    stem, suffix = split_extension(name)
    candidate = fit_to_filesystem(
        "%s (attachment %d)%s" % (stem, attachment_id, suffix),
        destination_dir, attachment_id,
    )
    taken[candidate.lower()] = attachment_id
    return candidate
#endregion


#region State
# A local index of what has already been written, keyed on
# (module, recordID, attachmentID) so an attachment shared by several records gets
# a copy in each record folder.

def state_key(module_name: str, record_id: int, attachment_id: int) -> str:
    return "%s/%d/%d" % (module_name, record_id, attachment_id)


def default_state(tenant: str) -> dict:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "_comment": (
            "lastRun* values are diagnostic only. This API has no modified-since "
            "filter, so they are NOT a watermark and must not be used to skip "
            "records."
        ),
        "tenantSubdomain": tenant,
        "lastRunStartedUtc": None,
        "lastRunFinishedUtc": None,
        "downloads": {},
    }


def load_state(path: Path, tenant: str) -> dict:
    if not path.exists():
        return default_state(tenant)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (ValueError, OSError) as exc:
        warn("could not read %s (%s); starting a fresh state file." % (path, exc))
        return default_state(tenant)

    if not isinstance(state, dict) or "downloads" not in state:
        warn("%s is not a recognised state file; starting fresh." % path)
        return default_state(tenant)
    if state.get("tenantSubdomain") not in (None, tenant):
        warn("%s was written for tenant %r but you are syncing %r; starting fresh."
             % (path, state.get("tenantSubdomain"), tenant))
        return default_state(tenant)
    state.setdefault("schemaVersion", STATE_SCHEMA_VERSION)
    state["tenantSubdomain"] = tenant
    return state


def save_state(path: Path, state: dict) -> None:
    """Write atomically, so an interrupted run never corrupts the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(str(temporary), str(path))


def entry_path(entry: dict, output_root: Path) -> Optional[Path]:
    """Where a state entry says its file lives; forward slashes keep it portable."""
    relative = entry.get("relativePath")
    return output_root.joinpath(*relative.split("/")) if relative else None


def entry_status(entry: dict, output_root: Path) -> str:
    """Compare one state entry against the disk: "ok", "missing" or "wrong-size".

    Used by both the sync and --verify so they agree on what counts as intact.
    """
    local = entry_path(entry, output_root)
    if local is None:
        return "missing"
    try:
        if not local.is_file():
            return "missing"
        return "ok" if local.stat().st_size == int(entry["byteCount"]) else "wrong-size"
    except (OSError, KeyError, TypeError, ValueError):
        return "wrong-size"


def remember_download(state: dict, key: str, attachment_id: int, output_root: Path,
                      path: Path, size: int, content_type: Optional[str]) -> None:
    relative = path.relative_to(output_root).as_posix()
    state["downloads"][key] = {
        "attachmentId": attachment_id,
        "relativePath": relative,
        "byteCount": size,
        "contentType": content_type,
        "downloadedUtc": utc_now_iso(),
    }


def sweep_partials(root: Path) -> int:
    """Delete leftover .fmxpart files from a killed run."""
    if not root.exists():
        return 0
    removed = 0
    for stale in root.rglob("*" + PART_SUFFIX):
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed
#endregion


#region Configuration and credentials

def load_config(path: Path) -> dict:
    if not path.exists():
        raise FmxError(
            "no config file at %s. Copy config.example.json to config.json and edit "
            "it, or pass --config with a path." % path
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except ValueError as exc:
        raise FmxError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(config, dict):
        raise FmxError("%s must contain a JSON object." % path)
    if not config.get("subdomain"):
        raise FmxError('%s is missing "subdomain".' % path)
    return config


def read_env_file(path: Path) -> Dict[str, str]:
    """Read simple KEY=VALUE lines. Not a full dotenv implementation."""
    values: Dict[str, str] = {}
    if not path.exists():
        raise FmxError("no env file at %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_credentials(args) -> Tuple[str, str]:
    """Find credentials: environment, then --env-file, then a prompt.

    There is deliberately no --password flag; it would land in shell history and
    the process list.
    """
    email_address = os.environ.get("FMX_EMAIL") or os.environ.get("FMX_USERNAME")
    password = os.environ.get("FMX_PASSWORD")

    if (not email_address or not password) and args.env_file:
        values = read_env_file(Path(args.env_file))
        email_address = email_address or values.get("FMX_EMAIL") or \
            values.get("FMX_USERNAME") or values.get("username") or values.get("email")
        password = password or values.get("FMX_PASSWORD") or values.get("password")

    if not email_address:
        email_address = input("FMX API user email: ").strip()
    if not password:
        password = getpass.getpass("FMX API user password: ")

    if not email_address or not password:
        raise FmxError("no credentials supplied.")
    return email_address, password


def resolve_modules(config: dict, only: Optional[str]) -> List[Module]:
    entries = config.get("modules")
    if not isinstance(entries, list) or not entries:
        raise FmxError('config has no "modules" list. See config.example.json.')

    wanted = None
    if only:
        wanted = [name.strip() for name in only.split(",") if name.strip()]

    modules: List[Module] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        api_path = entry.get("apiPath")
        if not name or not api_path:
            warn("skipping a module entry missing name or apiPath: %r" % entry)
            continue
        if wanted is None:
            if not entry.get("enabled", True):
                continue
        elif name not in wanted:
            continue
        modules.append(Module(
            name=name,
            api_path=api_path,
            fields=entry.get("fields") or BASE_FIELDS,
            date_window=entry.get("dateWindow") or None,
        ))

    if wanted:
        missing = set(wanted) - {module.name for module in modules}
        if missing:
            raise FmxError("no such module(s) in config: %s" % ", ".join(sorted(missing)))
    if not modules:
        raise FmxError("no enabled modules to sync.")
    return modules
#endregion


#region Syncing

def download_with_fresh_url(session: Session, attachment_id: int, blob_url: str,
                            destination: Path) -> Tuple[int, Optional[str]]:
    """Transfer the bytes, asking for a new URL if storage will not accept this one.

    Download URLs are time limited, so the recovery is a fresh one rather than
    another attempt. Ordinary network trouble is handled by retry() underneath.
    """
    for remaining in (2, 1, 0):
        try:
            return retry(lambda: blob_download(session, blob_url, destination),
                         "download attachment %d" % attachment_id, session.retries)
        except ExpiredUrlError as exc:
            if not remaining:
                raise FmxError("attachment %d: %s, and a fresh download URL did not "
                               "help either" % (attachment_id, exc))
            detail("download URL refused (%s); requesting a fresh one" % exc)
            blob_url = api_resolve_blob_url(session, attachment_id)
    raise FmxError("attachment %d: could not be downloaded" % attachment_id)


def sync_attachment(session: Session, module: Module, record_id: int, found: Found,
                    output_root: Path, state: dict, args, taken: Dict[str, int],
                    cache: Dict[int, "CachedFile"], stats: Stats) -> str:
    """Bring one attachment reference into line with the site.

    Returns "skipped", "verified", "copied" or "downloaded". The steps below run
    cheapest first and stop as early as they can, which is the whole cost model.
    One call per attachment is deliberate; the plural form is not usable here.
    """
    attachment_id = found.attachment_id
    key = state_key(module.name, record_id, attachment_id)
    record_dir = output_root / module.name / str(record_id)

    # Step 1, free: already held and still intact.
    if not args.full:
        entry = state["downloads"].get(key)
        if entry and entry_status(entry, output_root) == "ok":
            taken[Path(entry["relativePath"]).name.lower()] = attachment_id
            stats.skipped += 1
            return "skipped"

    # Step 2, also free: another record needed these bytes earlier in this run, so
    # copy locally. Caching the filename too is what avoids a second call.
    cached = cache.get(attachment_id)
    if cached is not None and not args.dry_run and cached.path.is_file():
        filename = choose_filename(cached.raw_name, attachment_id, record_dir, taken)
        destination = record_dir / filename
        if destination != cached.path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(cached.path), str(destination))
            size = destination.stat().st_size
            remember_download(state, key, attachment_id, output_root, destination,
                              size, cached.content_type)
            stats.copied += 1
            stats.bytes_copied += size
            log("         + %-48s %10s   copied (fetched once this run)"
                % (filename[:48], format_bytes(size)))
            return "copied"

    # Step 3, one call: a temporary URL plus the filename, and no bytes moved yet.
    blob_url = api_resolve_blob_url(session, attachment_id)
    raw_name, url_content_type = filename_from_blob_url(blob_url)
    filename = choose_filename(raw_name, attachment_id, record_dir, taken)
    destination = record_dir / filename

    if args.dry_run:
        log("         ? %-48s would download -> %s"
            % (filename[:48], destination.parent))
        return "skipped"

    # Step 4, free: already on disk, so confirm the size and skip the transfer.
    if not args.full and destination.is_file():
        remote_size = blob_head(session, blob_url)
        if remote_size is not None and destination.stat().st_size == remote_size:
            remember_download(state, key, attachment_id, output_root, destination,
                              remote_size, url_content_type)
            cache[attachment_id] = CachedFile(destination, raw_name,
                                              url_content_type)
            stats.verified += 1
            log("         = %-48s %10s   already on disk"
                % (filename[:48], format_bytes(remote_size)))
            return "verified"

    # Step 5: actually transfer the bytes.
    size, response_content_type = download_with_fresh_url(
        session, attachment_id, blob_url, destination)

    content_type = url_content_type or response_content_type
    remember_download(state, key, attachment_id, output_root, destination, size,
                      content_type)
    cache[attachment_id] = CachedFile(destination, raw_name, content_type)
    stats.downloaded += 1
    stats.bytes_downloaded += size
    suffix = "" if found.container == "record" else "   [%s]" % found.container
    log("         + %-48s %10s   downloaded%s"
        % (filename[:48], format_bytes(size), suffix))
    return "downloaded"


def iter_module_records(session: Session, module: Module, args) -> Iterator[dict]:
    """Records for one module: an explicit id list, or the full paged scan."""
    if args.record_ids:
        for raw in args.record_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            record = api_get_record(session, module, int(raw))
            if record is None:
                warn("record %s not found in %s" % (raw, module.name))
                continue
            yield record
        return

    # Every record is scanned each run; the projection is what keeps that cheap.
    yield from api_iter_records(session, module, args.page_size, args.max_records)


def sync_module(session: Session, module: Module, output_root: Path, state: dict,
                state_path: Path, args, stats: Stats) -> None:
    label = "[%s]" % module.name
    log("%-34s %s" % (label, "-" * 6))

    expected_keys = [key for key in projection_top_level_keys(module.fields)
                     if key != "id"]
    seen_keys = set()
    record_count = 0
    module_found = 0
    consecutive_failures = 0

    try:
        for record in iter_module_records(session, module, args):
            if not isinstance(record, dict):
                continue
            record_id = record.get("id")
            if not isinstance(record_id, int):
                continue
            record_count += 1
            stats.records += 1
            seen_keys.update(key for key in expected_keys if key in record)

            references = find_attachments(record, skip_private=args.skip_private)
            if not references:
                continue
            module_found += len(references)
            stats.found += len(references)

            taken: Dict[str, int] = {}
            for found in references:
                status = None
                try:
                    status = sync_attachment(session, module, record_id, found,
                                             output_root, state, args, taken,
                                             _CONTENT_CACHE, stats)
                    consecutive_failures = 0
                except FmxError as exc:
                    message = str(exc)
                    if "Authentication failed" in message:
                        raise
                    stats.failed += 1
                    consecutive_failures += 1
                    stats.failures.append(
                        "%s/%d attachment %d: %s"
                        % (module.name, record_id, found.attachment_id, message)
                    )
                    log("         ! attachment %-36d %s"
                        % (found.attachment_id, message[:60]))
                    if args.fail_fast:
                        raise FmxError("stopping on first failure (--fail-fast)")
                    if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                        raise FmxError(
                            "%d consecutive failures; stopping. Progress is saved."
                            % consecutive_failures
                        )
                # Only when something was recorded; an unchanged run writes nothing.
                if status in STATE_CHANGING and not args.dry_run:
                    save_state(state_path, state)

    except ModuleNotEnabled:
        log("%-34s not enabled on this tenant (404) - skipped" % label)
        stats.modules_missing.append(module.name)
        return

    # Check the projection took effect. Only meaningful on a reasonably sized scan:
    # a few records may genuinely have no actions, and --record-ids is hand-picked.
    missing = [key for key in expected_keys if key not in seen_keys]
    guard_applies = record_count >= PROJECTION_GUARD_MIN_RECORDS and not args.record_ids
    if missing and guard_applies:
        message = (
            "%s: \"fields\" asked for %s but no record returned %s. Check the "
            "spelling, or drop it from \"fields\" if this module has no such "
            "collection."
            % (module.name, ", ".join(missing),
               "it" if len(missing) == 1 else "them"))
        warn(message)
        stats.projection_warnings.append(message)

    if not record_count:
        stats.modules_empty.append(module.name)
    log("%-34s %d records, %d attachments" % (label, record_count, module_found))


# Fetched-this-run cache, so an attachment on several records is fetched once.
_CONTENT_CACHE: Dict[int, "CachedFile"] = {}


def run_sync(session: Session, modules: List[Module], output_root: Path,
             state_path: Path, args) -> Stats:
    stats = Stats()
    state = load_state(state_path, args.subdomain)
    state["lastRunStartedUtc"] = utc_now_iso()

    removed = sweep_partials(output_root)
    if removed:
        log("Cleaned up %d unfinished file(s) from a previous run." % removed)

    try:
        for module in modules:
            sync_module(session, module, output_root, state, state_path, args, stats)
    finally:
        # Saved on every exit path, including Ctrl-C, so a long run is never wasted.
        state["lastRunFinishedUtc"] = utc_now_iso()
        if not args.dry_run:
            save_state(state_path, state)
    return stats


def run_verify(session: Session, output_root: Path, state_path: Path, args) -> int:
    """Check every recorded file still matches its recorded size. Uses no API calls."""
    state = load_state(state_path, args.subdomain)
    entries = state.get("downloads") or {}
    if not entries:
        log("Nothing recorded in %s yet." % state_path)
        return 0

    missing = 0
    mismatched = 0
    for key in sorted(entries):
        entry = entries[key]
        relative = entry.get("relativePath") or "?"
        status = entry_status(entry, output_root)
        if status == "missing":
            log("  missing     %s" % relative)
            missing += 1
        elif status == "wrong-size":
            log("  wrong size  %s (recorded %s bytes)"
                % (relative, entry.get("byteCount")))
            mismatched += 1

    log("")
    log("Checked %d recorded file(s): %d missing, %d wrong size."
        % (len(entries), missing, mismatched))
    log("FMX API calls used: 0")
    return 1 if (missing or mismatched) else 0


# Modules whose path does not follow /<name>-requests; already listed above.
NON_REQUEST_MODULES = frozenset({
    "schedule", "planned maintenance", "purchase order", "invoice",
})


def derive_request_path(module_name: str) -> Optional[str]:
    """Work out a work-request module's path from its display name.

    Sites name these freely ("Grounds", "Custodial") and they follow
    /<kebab-name>-requests; other module families do not.
    """
    cleaned = module_name.strip().lower()
    if not cleaned or cleaned in NON_REQUEST_MODULES:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return ("/%s-requests" % slug) if slug else None


def candidate_paths(reported_modules: List[str]) -> List[Tuple[str, str]]:
    """Paths worth probing: the built-in list plus any custom modules this site reports."""
    candidates = list(CANDIDATE_PATHS)
    seen = {api_path for _, api_path in candidates}
    for module_name in reported_modules:
        api_path = derive_request_path(module_name)
        if api_path and api_path not in seen:
            seen.add(api_path)
            candidates.append((api_path.lstrip("/"), api_path))
    return candidates


def fmx_get_or_none(session: Session, path: str,
                    params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """Like fmx_get but returns None instead of raising; for --discover probing."""
    try:
        return fmx_get(session, path, params)
    except (FmxError, ModuleNotEnabled):
        return None


# How the read permission for each module is spelled in a user type record. "Any"
# sees everything, "Own" only what that account created, "None" nothing at all.
READ_ACCESS_SOURCES = [
    ("workRequestSettings", "readAccessPermission", None),
    ("workTaskSettings", "readAccessPermission", "/planned-maintenance/tasks"),
    ("scheduleRequestSettings", "readApprovedAcceptedAndUndeletedAccessPermission",
     "/scheduling/requests"),
    ("transportationRequestSettings", "readApprovedAccessPermission",
     "/transportation-requests"),
    ("invoiceSettings", "readAccessPermission", "/invoices"),
    ("purchaseOrderSettings", "readAccessPermission", "/purchase-orders"),
    ("equipmentSettings", "readAccessPermission", "/equipment"),
    ("buildingSettings", "readAccessPermission", "/buildings"),
]


def read_access_by_path(session: Session, email_address: str) -> Dict[str, str]:
    """Read permission per module path for the account we are signing in as.

    Returns {api path: "Any" | "Own" | "None"}, or an empty mapping if it cannot be
    worked out. Used only to explain why a module came back empty: a module set to
    "Own" shows this account nothing unless it created the records itself.
    """
    users = fmx_get_or_none(session, "/users", {"limit": 500})
    if not isinstance(users, list):
        return {}
    wanted = (email_address or "").strip().lower()
    mine = next((u for u in users if isinstance(u, dict)
                 and (u.get("email") or "").lower() == wanted), None)
    if mine is None:
        return {}       # a large site may not list this account on the first page

    types = fmx_get_or_none(session, "/user-types", {"limit": 500})
    if not isinstance(types, list):
        return {}
    user_type = next((t for t in types if isinstance(t, dict)
                      and t.get("id") == mine.get("userTypeID")), None)
    if user_type is None:
        return {}

    access: Dict[str, str] = {}
    for key, permission_field, fixed_path in READ_ACCESS_SOURCES:
        section = user_type.get(key)
        entries = section if isinstance(section, list) else [section]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            permission = entry.get(permission_field)
            if not permission:
                continue
            # Work request and work task modules are named, so their path comes from
            # the name; the rest sit at a fixed path.
            path = fixed_path or derive_request_path(entry.get("moduleName") or "")
            if path:
                access[path] = permission
    return access

def discovered_fields(api_path: str, collections: List[str]) -> str:
    """The projection to recommend for a module.

    Work request modules always get the actions expansion. A small sample can
    easily contain no comments, and leaving it out would quietly miss any
    attachments on them. Occurrences stay unexpanded, since they inherit the
    parent's attachments and would multiply copies on a recurring schedule.
    """
    wants_actions = api_path.endswith("-requests") or "actions" in collections
    return BASE_FIELDS + ("," + ACTION_FIELDS if wants_actions else "")


def run_discover(session: Session, args, config: dict) -> int:
    """Probe the site, report what it has, and offer to write the config.

    Both the module paths and their expandable collections vary by site, so both
    are probed rather than assumed.
    """
    def row(path: str, status: str, note: str) -> None:
        log("%-34s %-12s %s" % (path, status, note))

    log("Modules this site reports (from /request-types):")
    request_types = fmx_get_or_none(session, "/request-types", {"limit": 500})
    reported: List[str] = []
    if isinstance(request_types, list):
        reported = sorted({entry.get("module") for entry in request_types
                           if isinstance(entry, dict) and entry.get("module")})
        log("  " + ", ".join(reported))
    else:
        warn("could not read /request-types, so only the built-in paths are probed.")
    log("")
    log("Paths vary by module family, so each candidate is probed below.")
    log("")

    # Used to explain empty modules; costs two calls and is worth it, because an
    # empty module usually means read access rather than an empty site.
    access = read_access_by_path(session, session.user_email)

    row("PATH", "STATUS", "EXPANDABLE COLLECTIONS")
    discovered: List[dict] = []
    unprobed: List[str] = []
    restricted: List[Tuple[str, str]] = []
    for name, api_path in candidate_paths(reported):
        page = fmx_get_or_none(session, api_path,
                               {"limit": DISCOVER_SAMPLE_RECORDS,
                                "fields": DISCOVER_FIELDS})
        if page is None:
            row(api_path, "404", "not enabled on this site")
            continue

        # A collection missing from every sampled record is one this module does
        # not expand. With no records at all there is nothing to go on.
        records = [rec for rec in page if isinstance(rec, dict)] if isinstance(
            page, list) else []
        collections = sorted({name_ for rec in records
                              for name_ in CANDIDATE_COLLECTIONS if name_ in rec})
        probeable = bool(records)
        permission = access.get(api_path)
        if probeable:
            note = ", ".join(collections) or "none"
        elif permission in ("Own", "None"):
            note = "no records visible - read access is %r" % permission
            restricted.append((name, permission))
        else:
            note = "no records yet - cannot probe"
        row(api_path, "200 ok", note)

        fields = discovered_fields(api_path, collections)
        if not probeable and ACTION_FIELDS in fields and permission not in ("Own", "None"):
            unprobed.append(name)
        discovered.append({
            "name": name,
            "apiPath": api_path,
            "enabled": name not in ("users", "schedule-occurrences",
                                    "planned-maintenance-occurrences"),
            "fields": fields,
        })

    log("")
    log("Modules found on this site:")
    log("")
    log(format_modules_block(discovered))
    log("")
    if unprobed:
        log("Note: %s had no records to sample, so the actions(...) expansion above "
            "is assumed from the path." % ", ".join(unprobed))
        log("Re-run --discover once they have data to confirm.")
        log("")
    if restricted:
        log("This account cannot see everything in these modules:")
        for name, permission in restricted:
            log("    %-34s read access is %r" % (name, permission))
        log("")
        log("\"Own\" shows only records this account created, and \"None\" shows")
        log("nothing, so these will sync as empty however many records the site has.")
        log("An administrator can change this under Admin Settings > User Types.")
        log("")
    log("FMX API calls used by this probe: %d" % session.fmx_calls)
    log("")

    offer_config_update(Path(args.config), config, discovered, args)
    return 0


def format_modules_block(modules: List[dict]) -> str:
    """Render module entries the way they look in config.json."""
    blocks = []
    for entry in modules:
        blocks.append(
            '    { "name": "%s", "apiPath": "%s", "enabled": %s,\n'
            '      "fields": "%s" }'
            % (entry["name"], entry["apiPath"],
               "true" if entry.get("enabled", True) else "false", entry["fields"])
        )
    return '  "modules": [\n' + ",\n".join(blocks) + "\n  ]"


def merge_modules(existing: List[Any], discovered: List[dict]) -> Tuple[List[dict], dict]:
    """Fold discovered modules into the existing list without losing settings.

    Discovery supplies apiPath and fields; any "enabled" or "dateWindow" already in
    the config wins, and modules the probe did not find are kept rather than dropped.
    """
    by_name = {}
    for entry in existing:
        if isinstance(entry, dict) and entry.get("name"):
            by_name[entry["name"]] = entry

    merged: List[dict] = []
    summary: Dict[str, List[str]] = {"added": [], "updated": [], "unchanged": [],
                                     "kept": []}

    for found in discovered:
        previous = by_name.pop(found["name"], None)
        entry = {"name": found["name"], "apiPath": found["apiPath"],
                 "enabled": found["enabled"], "fields": found["fields"]}
        if previous is None:
            summary["added"].append(found["name"])
        else:
            if "enabled" in previous:
                entry["enabled"] = previous["enabled"]
            for carried in ("dateWindow", "_comment"):
                if previous.get(carried):
                    entry[carried] = previous[carried]
            changed = (previous.get("apiPath") != entry["apiPath"]
                       or previous.get("fields") != entry["fields"])
            summary["updated" if changed else "unchanged"].append(found["name"])
        merged.append(entry)

    # Left over means in the config but not found; kept, since an absent module
    # only costs a skipped probe.
    for name, entry in by_name.items():
        merged.append(entry)
        summary["kept"].append(name)

    return merged, summary


def confirm(question: str, args) -> bool:
    """Yes/no prompt that behaves sensibly when there is nobody to ask."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        log("Not an interactive terminal, so leaving the config alone. Pass --yes "
            "to write it automatically.")
        return False
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def offer_config_update(config_path: Path, config: dict, discovered: List[dict],
                        args) -> None:
    """Offer to write the discovered modules into the config file."""
    merged, summary = merge_modules(config.get("modules") or [], discovered)

    if not (summary["added"] or summary["updated"]):
        log("Your %s already matches this site. Nothing to change." % config_path.name)
        return

    log("Changes this would make to %s:" % config_path)
    for name in summary["added"]:
        log("    add      %s" % name)
    for name in summary["updated"]:
        log("    update   %s  (apiPath / fields)" % name)
    if summary["unchanged"]:
        log("    %d module(s) already correct" % len(summary["unchanged"]))
    if summary["kept"]:
        log("    keep     %s  (in your config, not found on this site)"
            % ", ".join(summary["kept"]))
    log("")
    log("Your own enabled/disabled choices are preserved.")
    log("")

    if not confirm("Update %s now? [y/N] " % config_path.name, args):
        log("Left %s unchanged - the block above can be pasted in by hand."
            % config_path.name)
        return

    backup = config_path.with_name(config_path.name + ".bak")
    try:
        shutil.copyfile(str(config_path), str(backup))
    except OSError as exc:
        warn("could not write a backup (%s), so leaving the config alone." % exc)
        return

    updated = dict(config)
    updated["modules"] = merged
    temporary = config_path.with_name(config_path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
        handle.write("\n")
    os.replace(str(temporary), str(config_path))

    log("Updated %s. Previous version saved as %s." % (config_path, backup.name))
    log("Next: python %s --dry-run" % Path(sys.argv[0]).name)
#endregion

#region Built-in tests for the fiddly pure functions (--self-test, no network needed)

def self_test() -> int:
    """Check the fiddly pure functions. No network, credentials or config needed.

    Table driven, so adding a case is one line.
    """
    failures: List[str] = []

    def check(label: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            failures.append("%s\n     expected: %r\n     actual:   %r"
                            % (label, expected, actual))

    # An uploaded filename is untrusted input. Names must survive intact where they
    # are legal, and be defanged where they are not.
    filename_cases = [
        # (uploaded name, attachment id, expected name on disk)
        ("Waiver.pdf", 1, "Waiver.pdf"),
        ("FMX Questions 08272026 (1).pdf", 1, "FMX Questions 08272026 (1).pdf"),
        ("../../etc/passwd", 7, "passwd"),
        (r"C:\evil\x.pdf", 7, "x.pdf"),
        ("NUL.pdf", 7, "_NUL.pdf"),
        ("con", 7, "_con"),
        ("report. ", 7, "report"),
        ('a<b>c:d"e|f?g*h.pdf', 7, "a_b_c_d_e_f_g_h.pdf"),
        ("", 42, "attachment-42"),
        (None, 42, "attachment-42"),
        ("..", 42, "attachment-42"),
    ]
    for raw, attachment_id, expected in filename_cases:
        check("sanitize_filename(%r)" % raw,
              sanitize_filename(raw, attachment_id), expected)

    # Every character Windows rejects must be caught. chr(92) is a backslash,
    # spelled that way so it survives any amount of quoting.
    for illegal in ["<", ">", ":", chr(34), "/", chr(92), "|", "?", "*",
                    chr(0), chr(31), chr(127)]:
        check("illegal char %r is caught" % illegal,
              bool(ILLEGAL_FILENAME_CHARS.search(illegal)), True)
    check("a legal character is left alone",
          bool(ILLEGAL_FILENAME_CHARS.search("(")), False)

    check("extension split", split_extension("report.final.pdf"),
          ("report.final", ".pdf"))
    check("extension split without a dot", split_extension("README"), ("README", ""))

    long_name = ("x" * 400) + ".pdf"
    fitted = fit_to_filesystem(sanitize_filename(long_name, 9), Path("/tmp/out"), 9)
    check("a very long name is truncated",
          len(fitted.encode("utf-8")) <= MAX_COMPONENT_BYTES, True)
    check("truncation keeps the extension", fitted.endswith(".pdf"), True)

    # Content-Disposition, including both forms FMX really emits.
    disposition_cases = [
        ("attachment; filename=Waiver.pdf", "Waiver.pdf"),
        ('attachment; filename="FMX Questions 08272026 (1).pdf"',
         "FMX Questions 08272026 (1).pdf"),
        ("attachment; filename=My Report.pdf", "My Report.pdf"),
        ("attachment; filename*=UTF-8''caf%C3%A9.pdf", "café.pdf"),
        (None, None),
    ]
    for value, expected in disposition_cases:
        check("filename from %r" % value,
              filename_from_content_disposition(value), expected)

    # The filename has to be recoverable from the signed URL alone: that is what
    # lets us skip the metadata call entirely.
    sas_cases = [
        ("https://example.blob.core.windows.net/fs/abc?sv=2026-02-06"
         "&se=2026-09-15T14%3A37%3A10Z&sr=b&sp=r"
         "&rscd=attachment%3B+filename%3DWaiver.pdf"
         "&rsct=application%2Fpdf&sig=abc%3D",
         "Waiver.pdf", "application/pdf"),
        ("https://example.blob.core.windows.net/fs/abc?sp=r"
         "&rscd=attachment%3B+filename%3D%22FMX+Questions+%281%29.pdf%22",
         "FMX Questions (1).pdf", None),
    ]
    for url, expected_name, expected_type in sas_cases:
        name, content_type = filename_from_blob_url(url)
        check("filename from signed url", name, expected_name)
        check("content type from signed url", content_type, expected_type)

    # Attachment discovery: arbitrary custom-field names, and the actions case that
    # is invisible unless the projection expands it.
    record = {
        "id": 1,
        "customFields": [
            {"name": "Attachments", "attachmentIDs": [200]},
            {"name": "Before Photos", "attachmentIDs": [100, 300]},
            {"name": "Description", "value": "no attachments here"},
        ],
        "actions": [
            {"id": 55, "isPrivate": False,
             "customFields": [{"name": "Attachments", "attachmentIDs": [400]}]},
            {"id": 66, "isPrivate": True,
             "customFields": [{"name": "Attachments", "attachmentIDs": [500]}]},
        ],
    }
    found = find_attachments(record)
    containers = {item.attachment_id: item.container for item in found}
    check("ids found, deduped and sorted",
          [item.attachment_id for item in found], [100, 200, 300, 400, 500])
    check("a record attachment is labelled", containers[100], "record")
    check("an action attachment is labelled", containers[400], "action 55")
    check("private actions are included by default", 500 in containers, True)
    check("skip_private drops them",
          [i.attachment_id for i in find_attachments(record, skip_private=True)],
          [100, 200, 300, 400])
    check("attachments nested at any depth are found",
          [i.attachment_id for i in
           find_attachments({"a": {"b": [{"c": {"attachmentIDs": [7]}}]}})], [7])
    check("a record with none yields none",
          find_attachments({"id": 1, "customFields": []}), [])

    check("projection top-level keys",
          projection_top_level_keys(
              "id,customFields(attachmentIDs),"
              "actions(id,createdTimeUtc,customFields(attachmentIDs))"),
          ["id", "customFields", "actions"])
    check("projection with a single key", projection_top_level_keys("id"), ["id"])

    # The state key includes the record, because one attachment can be referenced by
    # several records and each folder needs its own copy.
    check("state key", state_key("maintenance-requests", 1001, 5001),
          "maintenance-requests/1001/5001")
    # A skipped attachment records nothing, so it must not trigger a state write.
    for outcome, changes in (("skipped", False), ("downloaded", True),
                             ("copied", True), ("verified", True)):
        check("%r changes state" % outcome, outcome in STATE_CHANGING, changes)

    # Filename collisions inside one record folder. Stateful, so it stays a sequence
    # rather than a table.
    taken: Dict[str, int] = {}
    out = Path("/tmp/out")
    check("first claimant keeps the name",
          choose_filename("Waiver.pdf", 5001, out, taken), "Waiver.pdf")
    check("a second attachment gets an id suffix",
          choose_filename("Waiver.pdf", 5004, out, taken),
          "Waiver (attachment 5004).pdf")
    check("the same attachment is idempotent",
          choose_filename("Waiver.pdf", 5001, out, taken), "Waiver.pdf")
    check("clashes are case-insensitive",
          choose_filename("waiver.PDF", 999, out, taken),
          "waiver (attachment 999).PDF")

    # Merging discovery into an existing config must never undo the operator's own
    # decisions, nor drop modules the probe did not happen to find.
    existing = [
        {"name": "equipment", "apiPath": "/equipment", "enabled": False,
         "fields": BASE_FIELDS},
        {"name": "maintenance-requests", "apiPath": "/maintenance-requests",
         "enabled": True, "fields": BASE_FIELDS,
         "dateWindow": {"fromDate": "2025-01-01"}},
        {"name": "retired-module", "apiPath": "/retired", "enabled": True,
         "fields": "id"},
    ]
    probed = [
        {"name": "equipment", "apiPath": "/equipment", "enabled": True,
         "fields": BASE_FIELDS},
        {"name": "maintenance-requests", "apiPath": "/maintenance-requests",
         "enabled": True, "fields": BASE_FIELDS + "," + ACTION_FIELDS},
        {"name": "invoices", "apiPath": "/invoices", "enabled": True,
         "fields": BASE_FIELDS},
    ]
    merged, summary = merge_modules(existing, probed)
    by_name = {entry["name"]: entry for entry in merged}
    check("a disabled module stays disabled", by_name["equipment"]["enabled"], False)
    check("dateWindow is carried over",
          by_name["maintenance-requests"].get("dateWindow"),
          {"fromDate": "2025-01-01"})
    check("stale fields are corrected",
          "actions(" in by_name["maintenance-requests"]["fields"], True)
    check("an undiscovered module is kept", "retired-module" in by_name, True)
    check("nothing is lost", len(merged), 4)
    for bucket, expected in (("added", ["invoices"]),
                             ("updated", ["maintenance-requests"]),
                             ("unchanged", ["equipment"]),
                             ("kept", ["retired-module"])):
        check("summary[%r]" % bucket, summary[bucket], expected)

    # A site can name its work request modules anything, so those paths are derived
    # from what the site reports rather than hardcoded.
    for module_name, expected in (("Grounds", "/grounds-requests"),
                                  ("Building Services", "/building-services-requests"),
                                  ("IT & Networks", "/it-networks-requests"),
                                  ("Schedule", None),
                                  ("Planned Maintenance", None),
                                  ("   ", None)):
        check("derive_request_path(%r)" % module_name,
              derive_request_path(module_name), expected)
    derived = [path for _, path in candidate_paths(["Maintenance", "Grounds",
                                                    "Schedule"])]
    check("a known path is not duplicated", derived.count("/maintenance-requests"), 1)
    check("a custom module is added", "/grounds-requests" in derived, True)
    check("schedule is not added twice", derived.count("/scheduling/requests"), 1)

    # A work request module must always be given the actions expansion, whatever a
    # small sample happened to show.
    check("work request path always expands actions",
          ACTION_FIELDS in discovered_fields("/grounds-requests", []), True)
    check("actions kept when detected",
          ACTION_FIELDS in discovered_fields("/anything", ["actions"]), True)
    check("occurrences are not expanded",
          discovered_fields("/scheduling/occurrences", ["occurrences"]), BASE_FIELDS)
    check("plain module gets the base projection",
          discovered_fields("/equipment", []), BASE_FIELDS)

    for count, expected in ((512, "512 B"), (221142, "216.0 KB"),
                            (3_900_000, "3.7 MB")):
        check("format_bytes(%d)" % count, format_bytes(count), expected)

    if failures:
        print("SELF-TEST FAILED (%d)" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1
    print("Self-test passed. No network or credentials were used.")
    return 0
#endregion


#region CLI

def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        prog="fmx_attachment_sync.py",
        description="Mirror FMX attachments into <output>/<module>/<record-id>/.",
        epilog="Credentials come from FMX_EMAIL and FMX_PASSWORD, an --env-file, or "
               "a prompt. There is deliberately no --password flag.",
    )
    parser.add_argument("--config", default="config.json", help="default: config.json")
    parser.add_argument("--output", help="override outputDirectory from the config")
    parser.add_argument("--modules", help="comma-separated module names to sync")
    parser.add_argument("--record-ids", help="only these record ids (needs one --modules)")
    parser.add_argument("--full", action="store_true",
                        help="ignore the state file and re-download everything")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be downloaded, transfer nothing")
    parser.add_argument("--discover", action="store_true",
                        help="probe this tenant for modules, then exit")
    parser.add_argument("--verify", action="store_true",
                        help="check recorded files against their sizes, then exit")
    parser.add_argument("--page-size", type=int, default=None, help="default: 200")
    parser.add_argument("--max-records", type=int, default=None,
                        help="stop after N records per module (for testing)")
    parser.add_argument("--state", help="default: <output>/" + STATE_FILENAME)
    parser.add_argument("--env-file", help="read credentials from a KEY=VALUE file")
    parser.add_argument("--timeout", type=float, default=None, help="default: 60")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--slow", action="store_true",
                        help="pause 0.25s between API calls")
    parser.add_argument("--skip-private", action="store_true",
                        help="ignore attachments on private responses")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop at the first failed attachment")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="answer yes to prompts (lets --discover write the config)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="run built-in checks; no network, no credentials")
    args = parser.parse_args(argv)

    if args.record_ids and len(
            [name for name in (args.modules or "").split(",") if name.strip()]) != 1:
        parser.error("--record-ids needs exactly one module in --modules")
    return args


def print_summary(stats: Stats, session: Session, elapsed: float,
                  state_path: Path) -> None:
    log("")
    log("Finished in %s" % format_duration(elapsed))
    log("  Records scanned .......  %6d" % stats.records)
    log("  Attachments found .....  %6d" % stats.found)
    log("  Downloaded ............  %6d   (%s)"
        % (stats.downloaded, format_bytes(stats.bytes_downloaded)))
    if stats.copied:
        log("  Copied locally ........  %6d   (%s, shared by several records)"
            % (stats.copied, format_bytes(stats.bytes_copied)))
    if stats.verified:
        log("  Confirmed on disk .....  %6d   (no transfer needed)" % stats.verified)
    log("  Already had ...........  %6d" % stats.skipped)
    log("  Failed ................  %6d" % stats.failed)
    log("  FMX API calls used ....  %6d" % session.fmx_calls)

    if stats.failed:
        log("")
        log("  %d attachment(s) could not be downloaded. Re-run the same command to "
            "retry them." % stats.failed)
        for item in stats.failures[:10]:
            log("    - %s" % item)
        if len(stats.failures) > 10:
            log("    ... and %d more" % (len(stats.failures) - 10))
    if stats.modules_missing:
        log("")
        log("  Not available on this site: %s" % ", ".join(stats.modules_missing))
    if stats.modules_empty:
        log("")
        log("  Returned no records: %s" % ", ".join(stats.modules_empty))
        log("  If you expect data there, run --discover to check this account's read")
        log("  access for those modules.")
    if stats.projection_warnings:
        log("")
        log("  Check your \"fields\" settings: %d module(s) did not return a "
            "projected collection." % len(stats.projection_warnings))
    log("")
    log("  Progress saved to %s" % state_path)


def main(argv: Optional[List[str]] = None) -> int:
    global _QUIET, _VERBOSE
    args = parse_args(argv)
    _QUIET = args.quiet
    _VERBOSE = args.verbose

    if args.self_test:
        return self_test()

    started = time.time()
    try:
        config = load_config(Path(args.config))
        args.subdomain = config["subdomain"]
        args.page_size = args.page_size or int(config.get("pageSize", 200))
        args.timeout = args.timeout or float(config.get("timeoutSeconds", 60))
        if args.page_size < 1:
            raise FmxError("--page-size must be at least 1")

        output_root = Path(args.output or config.get("outputDirectory", "./fmx-attachments")).expanduser()
        state_path = Path(args.state) if args.state else output_root / STATE_FILENAME

        email_address, password = resolve_credentials(args)
        fmx_opener, blob_opener = build_openers()
        session = Session(
            base_url="https://%s.gofmx.com/api/v1" % args.subdomain,
            auth_header=basic_auth_header(email_address, password),
            timeout=args.timeout,
            retries=max(1, args.retries),
            throttle=0.25 if args.slow else float(config.get("throttleSeconds", 0.0)),
            user_email=email_address,
            fmx_opener=fmx_opener,
            blob_opener=blob_opener,
        )
        del password

        if args.verbose:
            proxies = urllib.request.getproxies()
            if proxies:
                detail("proxy in effect: %s" % ", ".join(sorted(proxies)))

        log("FMX Attachment Sync")
        log("  Tenant:  %s.gofmx.com" % args.subdomain)
        log("  Output:  %s" % output_root.resolve())
        if args.discover:
            log("  Mode:    discover (read-only, nothing will be downloaded)")
            log("")
            return run_discover(session, args, config)
        if args.verify:
            log("  Mode:    verify (no FMX calls, no downloads)")
            log("")
            return run_verify(session, output_root, state_path, args)

        mode = "full re-download" if args.full else "incremental"
        if args.dry_run:
            mode += ", dry run"
        log("  Mode:    %s%s" % (mode, "" if args.full else
                                 "  (add --full to re-download everything)"))
        log("")

        modules = resolve_modules(config, args.modules)
        output_root.mkdir(parents=True, exist_ok=True)
        stats = run_sync(session, modules, output_root, state_path, args)
        print_summary(stats, session, time.time() - started, state_path)
        return 1 if stats.failed else 0

    except KeyboardInterrupt:
        log("")
        log("Interrupted. Progress saved - re-run the same command to continue.")
        return 130
    except FmxError as exc:
        print("", file=sys.stderr)
        print("Error: %s" % exc, file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 2
    except Exception as exc:  # noqa: BLE001 - operators should not see a traceback
        print("", file=sys.stderr)
        print("Unexpected error: %s" % exc, file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        else:
            print("Re-run with -v to see the full traceback.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
#endregion