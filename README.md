# FMX Attachment Sync

Download every attachment in your FMX site to a local folder tree, and keep it up
to date on a schedule.

```
fmx-attachments/
  maintenance-requests/
    1001/
      Waiver.pdf
      FMX Questions 08272026 (1).pdf
    1003/
      Waiver (1).pdf
  equipment/
    2001/
      Warranty.pdf
```

Point the output folder at your **Google Drive** (or OneDrive / Dropbox) sync
folder and the desktop sync client uploads everything for you. The script
deliberately has no Drive integration: that keeps it dependency-free and means
there is no OAuth or service account to configure.

* One file. **Python 3.9+, standard library only.** Nothing to `pip install`.
* Safe to re-run. It only downloads what it does not already have.
* Runs the whole site in well under a minute for a typical tenant.

---

## Quick start

**1. Create an API user in FMX.**

In FMX, go to *Admin Settings → User Types*, create a user type with API access,
then create a user assigned to that type. Use a dedicated account with a long
password rather than a person's login, so that rotating somebody's password does
not silently break your nightly sync.

The account needs to be able to *read* the modules you want to mirror.

**2. Set up the config.**

```bash
cp config.example.json config.json
```

Edit `config.json` and set `subdomain` to your FMX hostname. If you sign in at
`https://acme.gofmx.com`, your subdomain is `acme`.

**3. Tell it your credentials.** They never go in the config file.

```bash
export FMX_EMAIL='api@acme.gofmx.com'
export FMX_PASSWORD='...'
```

On Windows PowerShell:

```powershell
$env:FMX_EMAIL='api@acme.gofmx.com'
$env:FMX_PASSWORD='...'
```

If you skip this, the script prompts you.

**4. Check the script works before touching the network.**

```bash
python fmx_attachment_sync.py --self-test
```

**5. Let it configure itself for your site.**

```bash
python fmx_attachment_sync.py --discover
```

This probes your site, shows you what it found, and offers to update
`config.json` for you:

```
Changes this would make to config.json:
    add      schedule-requests
    update   maintenance-requests  (apiPath / fields)
    9 module(s) already correct

Your own enabled/disabled choices are preserved.

Update config.json now? [y/N]
```

Answer `y` and it writes the file, saving your previous version as
`config.json.bak`. Answer anything else and nothing is touched — the block it
printed can still be pasted in by hand.

This step matters because module paths are not guessable (see
[Why the config looks like that](#why-the-config-looks-like-that)). It is safe to
re-run any time, for example after your FMX site has a new module enabled: it only
corrects `apiPath` and `fields`, and never overrides which modules you chose to
enable or disable.

For an unattended run, `--discover --yes` skips the prompt. Without a terminal to
prompt at (a cron job, say) it leaves the config alone rather than guessing.

**6. Do a practice run that downloads nothing.**

```bash
python fmx_attachment_sync.py --dry-run
```

**7. Run it.**

```bash
python fmx_attachment_sync.py
```

```
FMX Attachment Sync
  Tenant:  acme.gofmx.com
  Output:  C:\FMX-Attachments
  Mode:    incremental  (add --full to re-download everything)

[maintenance-requests]             ------
         + Waiver.pdf                                         216.0 KB   downloaded
         + FMX Questions 08272026 (1).pdf                     866.9 KB   downloaded
         + Waiver (1).pdf                                     216.0 KB   downloaded   [action 9001]
[maintenance-requests]             412 records, 3 attachments
[technology-requests]              not enabled on this tenant (404) - skipped
[schedule-requests]                2780 records, 0 attachments

Finished in 15s
  Records scanned .......    3192
  Attachments found .....       3
  Downloaded ............       3   (1.3 MB)
  Already had ...........       0
  Failed ................       0
  FMX API calls used ....      27

  Progress saved to C:\FMX-Attachments\.fmx_sync_state.json
```

---

## Running it on a schedule

The script is safe to run repeatedly: a run with nothing new to fetch does almost
no work.

**Windows (Task Scheduler)** — daily at 2am:

```
schtasks /create /tn "FMX Attachment Sync" /tr "python C:\path\to\fmx_attachment_sync.py --config C:\path\to\config.json --quiet" /sc daily /st 02:00
```

**macOS / Linux (cron)**:

```
0 2 * * * cd /path/to/repo && /usr/bin/python3 fmx_attachment_sync.py --quiet >> sync.log 2>&1
```

Set `FMX_EMAIL` and `FMX_PASSWORD` where the scheduled job can see them, or use
`--env-file /secure/path/fmx.env` with a file containing:

```
FMX_EMAIL=api@acme.gofmx.com
FMX_PASSWORD=...
```

Keep that file outside the repository, or at least make sure it is gitignored.

Exit codes: `0` clean, `1` finished but some attachments failed, `2` configuration
or authentication problem, `130` interrupted.

---

## What it costs to run

API calls, not disk or CPU, are the thing worth minimising here. A run spends
them like this:

| Work | FMX API calls |
|---|---|
| Listing records across all modules | ~20-30 total |
| An attachment you already have | **0** |
| An attachment you have not seen before | **1** |
| Confirming a file already on disk | 0 |
| Transferring the bytes | 0 (that traffic goes to storage, not FMX) |

Measured on a real site with 2,800 records across 10 modules: **27 calls** on the
first run, **23** on every run after that. A brand new attachment costs exactly
one call, and an attachment referenced by several records is fetched once no matter
how many records point at it.

Two design choices produce that:

* **The download URL tells us the filename**, so there is no separate metadata
  lookup. See Surprise 3.
* **A small state file** (`.fmx_sync_state.json`, in your output folder) records
  what has already been written, so unchanged attachments cost nothing at all.

If you delete the state file, nothing is lost. The next run re-checks against what
is on disk and re-downloads only what is genuinely missing — calls, but no wasted
transfer.

---

## Why the config looks like that

Two fields in each module entry are doing unobvious work.

### `apiPath` is separate from `name`

Module API paths are not derivable from module names. The Schedule module lives at
`/scheduling/requests`; `/schedule-requests` returns 404. Planned maintenance is
`/planned-maintenance/tasks`. So `name` is the folder we create and `apiPath` is
whatever the API actually wants. Run `--discover` and let it write the config for you
rather than guessing.

`--discover` also handles custom module names. If your site has a work request
module called "Grounds", it will find it at `/grounds-requests` by deriving the
path from what your site reports, so it is not limited to the stock module names.

Modules your site does not have simply return 404, and are reported as
`not enabled on this tenant` and skipped. That is not an error.

### `fields` is required, not an optimisation

```json
"fields": "id,customFields(attachmentIDs),actions(id,createdTimeUtc,isPrivate,customFields(attachmentIDs))"
```

This is the single most important line in the config, for two reasons.

**It is the difference between finding all your attachments and silently missing
some.** An attachment added in a *response or comment* belongs to the record's
"actions", and the default API response includes only action **ID numbers**, not
the actions themselves. Without the `actions(...)` part above, those attachments
are invisible and the run still reports success.

**It also makes the run far cheaper.** One 2,780-record module measured **12.4 MB**
unprojected and **297 KB** projected — 40x smaller, because we ask only for the
few fields we need.

> **Careful:** this API silently ignores field names it does not recognise and
> still answers `200`. Writing `action(...)` instead of `actions(...)` does not
> raise an error, it just quietly returns nothing. The script warns you if a
> collection you asked for never comes back on a reasonably sized scan, but the
> safest move is to use what `--discover` gives you.

### Occurrences are switched off by default

`schedule-occurrences` and `planned-maintenance-occurrences` ship disabled. An
occurrence inherits the attachments of its parent series, and each record gets its
own folder — so a single 20 MB PDF on a daily recurring schedule would be written
hundreds of times. Turn them on only if you truly want a copy filed under every
occurrence.

`users` is disabled for a different reason: user attachments tend to be personal
documents (certifications, contracts), so mirroring them into a shared Drive folder
should be a deliberate decision.

---

## How the attachment API actually behaves

This section exists because several things here are genuinely surprising, and
knowing them will save you a lot of debugging if you write your own client.

### 1. Attachments are not a field on the record

They are nested inside custom field values:

```json
{
  "id": 1001,
  "customFields": [
    { "customFieldID": 4001, "name": "Attachments", "attachmentIDs": [5001, 5002] }
  ]
}
```

A site can have several attachment custom fields with any names it likes
("Photos", "Before Pictures", "Signed Waiver"). This script therefore looks for
the `attachmentIDs` **key** anywhere in the record rather than matching on the
field name — matching on `"Attachments"` would miss the others.

### 2. Attachments on comments live somewhere else again

Covered above under `fields`, and worth repeating because it is the easiest way to
build something that looks like it works. A record with an attachment on a comment
returns this by default:

```json
{ "id": 1003, "actionIDs": [9001, 9002] }
```

No attachment in sight. Only with `fields=...actions(...)` do you see:

```json
{ "actions": [ { "id": 9001, "isPrivate": false,
                 "customFields": [ { "attachmentIDs": [5004] } ] } ] }
```

### 3. The download URL contains the filename

`GET /api/v1/attachments/{id}/download` answers `302` with a `Location` pointing at
Azure Blob Storage, and that URL carries the filename in its query string:

```
...&rscd=attachment%3B+filename%3DWaiver.pdf&rsct=application%2Fpdf&sig=...
```

`rscd` and `rsct` are Azure's response-content-disposition and content-type
overrides. Reading them means one call gives you the filename *and* the download
location without transferring a byte — which is why this script never calls
`GET /attachments/{id}` for metadata during a sync.

### 4. Do not let your HTTP library follow that redirect

This one costs people an afternoon. The Azure URL is already signed. If your HTTP
client follows the redirect and helpfully re-sends your `Authorization: Basic`
header, Azure sees two competing credentials and rejects the request:

```
403 AuthenticationFailed
Server failed to authenticate the request. Make sure the value of Authorization
header is formed correctly including the signature.
```

The fix is to stop at the redirect and fetch the storage URL with **no headers at
all**. In `requests` that means `allow_redirects=False` followed by a bare
`requests.get(location)` — with no session that might re-attach auth. This script
uses two separate URL openers so that sending FMX credentials to storage is
structurally impossible.

### 5. `GET /attachments/{id1},{id2}` does not do what it looks like

The plural form appears to be a bulk metadata lookup, and returns a tidy JSON
array. But it filters on *who uploaded the file*, with no permission check
involved — so it returns only attachments **the authenticated account uploaded
itself**. For a dedicated API user, which uploads nothing, it returns:

```json
[]
```

An empty array with a `200`, not an error. So it looks like it worked. If you have
seen it return real data, you were almost certainly signed in as the person who
uploaded those files.

The single-ID form `GET /attachments/{id}` is unaffected — it applies proper
read-access checks and works correctly for anything you can see in the UI. (The
two forms hit different handlers; the plural route only matches when you pass two
or more IDs.)

**This script never calls the plural form.** Please do not "optimise" it back in:
a partial result that reports success is worse than an error.

### 6. Nothing validates your query string

Unrecognised query parameters are ignored and answered `200`. A filter you
misspelled does not fail, it returns unfiltered data. The script keeps an
allow-list of parameters it is willing to send, purely to protect against its own
typos.

### 7. There is no "modified since" filter

This is the one that shapes the whole design, and it is why the script scans every
record on every run.

* `fromDate` and `toDate` exist, but they filter the **event / due date**, not the
  modified time. Two records edited today but due last October are excluded by
  `fromDate=2026-01-01`.
* The edited-timestamp field is inconsistent, and often absent entirely:

  | Entity | Edited timestamp |
  |---|---|
  | Work requests | `editedTimeUtc` |
  | Equipment, Users | `editedTimestampUtc` (different name) |
  | Schedule requests, planned maintenance, buildings | *none at all* |

* There is no edit-time sort key either, so you cannot sort newest-first and stop
  early.

So "only fetch what changed since last night" is not something this API can
answer. Scanning every record is unavoidable — the `fields` projection is what
makes it cheap, and the state file is what stops the *attachments* being refetched.

If a module is too large even so, you can set an opt-in date window per module:

```json
{ "name": "schedule-requests", "apiPath": "/scheduling/requests", "enabled": true,
  "fields": "id,customFields(attachmentIDs)",
  "dateWindow": { "fromDate": "2025-01-01" } }
```

**This is lossy.** It filters on event date, so attachments on records outside the
window will never be downloaded. Use it only if you understand that trade-off.

---

## Limitations you should know about

**It never deletes. This is a one-way mirror.** It adds files and overwrites
files; it does not remove them. If an attachment is deleted or unlinked in FMX,
your local copy stays where it is. Do not treat the output folder as an accurate
picture of what is *currently* in FMX — treat it as an accumulating archive of
everything that has ever been attached.

Pruning is deliberately not implemented. An attachment can be referenced by
several records, and there is no reverse index to ask "is this still referenced
anywhere?", so a delete feature would risk removing files that are still live.
Removing things you no longer want is a manual decision.

**Attachments never attached to anything are invisible.** A file uploaded but not
attached to any record can only be seen by the account that uploaded it. This
script finds attachments by walking records, so orphans are out of reach.

**Private comments are included by default.** If a comment is marked private, its
attachments are still downloaded. Pass `--skip-private` to leave them out — worth
considering if the output folder is shared with people who cannot see private
responses in FMX.

**Same-named files in one record are kept separate.** Two different attachments
both called `Waiver.pdf` on the same record become `Waiver.pdf` and
`Waiver (attachment 5004).pdf`. The suffix is the attachment ID, so it stays
stable across runs.

---

## Command reference

| Flag | Meaning |
|---|---|
| `--config PATH` | Config file. Default `config.json`. |
| `--output DIR` | Override the output folder from the config. |
| `--modules a,b` | Only these modules (by `name`). |
| `--record-ids 1,2` | Only these records. Needs exactly one `--modules`. |
| `--full` | Ignore the state file and re-download everything. |
| `--dry-run` | Report what would be downloaded. Transfers nothing. |
| `--discover` | Probe the site for modules, print a config block, exit. |
| `--verify` | Check recorded files against their sizes. Uses no API calls. |
| `--page-size N` | Records per request. Default 200. |
| `--max-records N` | Stop after N records per module. Useful for testing. |
| `--state PATH` | State file location. Default `<output>/.fmx_sync_state.json`. |
| `--env-file PATH` | Read `FMX_EMAIL` / `FMX_PASSWORD` from a `KEY=VALUE` file. |
| `--timeout S` | Per-request timeout. Default 60. |
| `--retries N` | Attempts per request. Default 4. |
| `--slow` | Pause 0.25s between API calls. |
| `--skip-private` | Ignore attachments on private comments. |
| `--fail-fast` | Stop at the first failed attachment. |
| `-y`, `--yes` | Answer yes to prompts, so `--discover` writes the config unattended. |
| `-v`, `--verbose` | Show URLs, retries, and headers. |
| `-q`, `--quiet` | Summary only. |
| `--self-test` | Run built-in checks. No network, no credentials. |

There is intentionally **no `--password` flag**: it would end up in your shell
history and be visible to anyone who can list processes.

---

## Troubleshooting

**`403 AuthenticationFailed` from `blob.core.windows.net`**
Your HTTP client is following the download redirect and re-sending the
`Authorization` header. See Surprise 4. If you see this from *this* script on a
freshly resolved URL, something is re-attaching auth — a corporate proxy can also
do it. Run with `-v`, which reports whether a proxy is in effect.

**`Authentication failed (HTTP 401)`**
Check `FMX_EMAIL` and `FMX_PASSWORD`. The script stops immediately rather than
retrying, deliberately: repeatedly retrying bad credentials can lock the account
out of a system your facilities team depends on.

**A module reports `not enabled on this tenant (404)`**
Normal if your site does not use that module. Set `"enabled": false` for it in the
config to stop probing it.

**A warning that `"fields"` asked for something that never came back**
Either a typo (this API drops unknown field names silently) or that module has no
such collection. Compare against `--discover` output; if the collection genuinely
does not exist there, remove it from that module's `fields`.

**`0 attachments` everywhere but you know there are some**
Almost always a missing `actions(...)` in `fields`. See Surprise 2. Confirm with:

```bash
python fmx_attachment_sync.py --modules maintenance-requests --max-records 5 --dry-run -v
```

**Paths too long on Windows**
Filenames are truncated to keep paths under 260 characters, but a deeply nested
output folder eats that budget. Use a short output root such as `C:\FMX`, or
enable long path support in Windows 10+.

**It downloaded the same file into several folders**
Working as intended: one attachment can be linked to several records, and each
record folder gets its own copy. It is only fetched from the network once.

---

## How incremental syncing works

Because the API cannot tell us what changed (Surprise 7), the state file does that
job. It lives in your output folder and looks like this:

```json
{
  "downloads": {
    "maintenance-requests/1001/5001": {
      "attachmentId": 5001,
      "relativePath": "maintenance-requests/1001/Waiver.pdf",
      "byteCount": 221142,
      "downloadedUtc": "2026-09-08T14:03:11Z"
    }
  }
}
```

The key is `module/record/attachment` rather than just the attachment ID, because
one attachment shared across three records needs three copies on disk.

An attachment is downloaded when any of these is true:

* it is not in the state file;
* the file it points at is missing;
* that file is on disk at the wrong size;
* you passed `--full`.

Because disk is always re-checked, the state file can never lie to you about what
you actually have. Delete files and they come back; delete the state file and the
next run rebuilds it by checking sizes, without re-downloading anything intact.

The `lastRunStartedUtc` / `lastRunFinishedUtc` values in that file are for your
information only. They are **not** a watermark and are never used to skip records
— that would silently miss attachments, for all the reasons in Surprise 7.
