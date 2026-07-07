# Archive Printer

A small Dockerized IPP printer server that archives printed PDF jobs to disk.

The server accepts IPP `Print-Job`, `Create-Job` plus `Send-Document`, and common printer discovery/validation requests. It stores each PDF under:

```text
archive-root/
  User Name/
    YYYY-MM-DD/
      Optional Timetable Folder/
        Document Name-YYYYMMDDTHHMMSSffffff.pdf
        Document Name-YYYYMMDDTHHMMSSffffff.json
```

The JSON sidecar records the IPP metadata that was available, such as `requesting-user-name`, `job-originating-user-name`, `job-name`, `document-name`, document format, client address, and an optional HTTP Basic username. Passwords are never written.

## Run

```powershell
New-Item -ItemType Directory -Force config
Copy-Item config.example.json config\config.json
docker compose up --build
```

The IPP endpoint is:

```text
ipp://localhost:8631/ipp/print
```

The container writes archived files to `./archive` by default.

By default the server also advertises itself with mDNS/DNS-SD as an IPP printer
using `_ipp._tcp.local`. This is what operating systems use for automatic
printer discovery. In WSL/container setups, multicast discovery may still be
blocked by the host network layer even when the IPP port is reachable; in that
case add the printer manually with:

```text
http://127.0.0.1:8631/ipp/print
```

## Run with WSLC

If you are using Microsoft's `wslc.exe`, publish the IPP port explicitly and bind the archive folder:

```powershell
& "C:\Program Files\WSL\wslc.exe" run -d `
  -p 8631:8631 `
  -v "C:\Users\mark\Documents\ArchivePrinter\archive:/archive" `
  -e MDNS_HOST=archive-printer `
  -e MDNS_ADDRESS=127.0.0.1 `
  --name ArchivePrinter `
  archive_printer
```

Check the running container and logs:

```powershell
& "C:\Program Files\WSL\wslc.exe" ps
& "C:\Program Files\WSL\wslc.exe" logs ArchivePrinter
```

## Configure

Edit `config/config.json`:

```json
{
  "archive_root": "/archive",
  "timezone": "Europe/London",
  "printer_name": "Archive Printer",
  "port": 8631,
  "enable_mdns": true,
  "mdns_host": "archive-printer",
  "mdns_address": "127.0.0.1",
  "require_basic_auth": false,
  "timetable": [
    {
      "users": ["John Smith"],
      "days": ["monday"],
      "start": "10:00",
      "end": "11:00",
      "folder": "Maths"
    }
  ]
}
```

Timetable rules are checked in order. `users` can contain exact names from the print job or `"*"`. `days` supports individual day names, `weekday`, and `weekend`. Times use `HH:MM` in the configured timezone.

## PDF input

The printer advertises `application/pdf` and archives only real PDF payloads. Configure clients or CUPS queues to render to PDF before sending to this printer.

If you need conversion inside the container, set `pdf_converter_command` in the config to a command that accepts `{input}` and `{output}` placeholders and produces a PDF. The base image does not install conversion tools.

## Authentication & Web UI Roles

To enable authentication for printing and the Web UI dashboard, set `"require_basic_auth": true` in `config/config.json`.

By default, if the `"users"` section is omitted from the configuration:
- The username `"admin"` with password `"admin"` acts as the default **administrator** account.
- Any other username/password pair is accepted and assigned the **student** role (to avoid disrupting print pipelines).

To enforce strict user validation and assign specific permissions, add the `"users"` dictionary inside `config/config.json`:

```json
  "users": {
    "admin": {
      "password": "strongpassword",
      "role": "administrator"
    },
    "teacher1": {
      "password": "staffpassword",
      "role": "staff"
    },
    "student1": {
      "password": "studentpassword",
      "role": "student"
    }
  }
```

### Roles and Permissions

* **Administrator** (`administrator`): Can view all print jobs in the queue and delete any print job.
* **Staff** (`staff`): Can view student prints and their own print jobs. Can delete only their own print jobs.
* **Student** (`student`): Can view only their own print jobs. Cannot delete any print jobs.

### Unique Job URLs
Each print job has a unique detail URL under `/jobs/<job_id>` (e.g. `/jobs/123`), which shows metadata attributes and download links. These URLs enforce the role permissions list and return `404 Not Found` if accessed by unauthorized users.

### Web UI Domain Alias
You can restrict the Web UI dashboard to a specific domain alias (e.g. `admin-printer.local`) by configuring:
```json
"web_ui_domain": "admin-printer.local"
```
If configured, the server verifies the `Host` header on all Web UI browser paths (`/` and `/jobs/*`). If accessed using another name or the raw printer IP address, it returns `404 Not Found` to prevent access. This makes it easy to block the dashboard in specific subnetworks at the DNS level.

## Development

Run tests:

```powershell
python -m unittest
```
