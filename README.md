# Youzan Auto

Windows GUI automation tool for logging in to Youzan, reading the current user's shop list, and optionally filling follow-up records.

> This project is for authorized internal use only. Do not use it to access accounts or data without permission.

## Features

- Tkinter desktop GUI.
- Uses the project `.venv` virtual environment when launched from source.
- Uses Microsoft Edge through Playwright in headless mode by default.
- Real credential validation for the **Start Login** action.
- Optional local session reuse through the separate **Passwordless Login** action.
- Local password persistence protected with Windows user-scoped DPAPI.
- Account, password, UI options, cookies, session state, logs, screenshots and HTML dumps remain local and are excluded from Git.
- Follow-up writing is intentionally single-threaded because the service returned rate-limit warnings during concurrency tests.
- Detailed authentication responses are summarized and sensitive values are redacted.

## Requirements

- Windows 10/11.
- Python 3.12 or a compatible Python version supported by the dependencies.
- Microsoft Edge installed.
- Network access to the authorized Youzan account.
- Optional: a local proxy configured through the GUI or environment variables.

## First-time setup from source

Run PowerShell in the project directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\run.cmd
```

`setup.ps1` creates `.venv`, installs the runtime dependencies, and checks that the core modules can be imported.

To start explicitly with the project interpreter:

```powershell
.\.venv\Scripts\python.exe .\main.py
```

## Local configuration and sensitive data

The application creates local runtime files under `json/`, `LOG/`, `html/`, `txt/` and `png/`. These directories are intentionally ignored by Git.

Do not put real credentials in source files, README files, tests, screenshots, logs or issue reports. The application can read credentials from the local GUI configuration or from environment variables:

```powershell
$env:YOUZAN_USER = 'your-account'
$env:YOUZAN_PASSWORD = 'your-password'
$env:YOUZAN_PROXY_SERVER = 'http://your-proxy:port'
```

The example values above are placeholders only. Never commit real values.

The local configuration file stores the password in Windows DPAPI-protected form. If the file is missing or cannot be decrypted, enter the credentials again in the GUI. Local cookies and complete browser session state are also excluded from Git.

## Login behavior

- **Start Login** always validates the current account and password and does not use an existing local cookie to claim success.
- **Passwordless Login** may reuse the local browser session state.
- If SMS verification is required, enter the code in the GUI before it expires.
- The program only reports successful login after confirming that the workbench is actually available.
- Failed credential validation stops the task before shop processing begins.

## Follow-up behavior

The normal workflow is:

1. Confirm successful login to the workbench.
2. Open the user's shop list.
3. Read the shop count.
4. Ask whether to continue if today's records already exist.
5. Process shops in order using one worker.
6. Record server warnings and failures without hiding them.

The GUI exposes one worker because the service-side follow-up endpoint returned `operation too fast` warnings when multiple shop writes were attempted concurrently. The setting is deliberately conservative; it is not a claim that the service supports parallel writes.

## Tests

Run all offline tests with the project interpreter:

```powershell
Get-ChildItem .\tests -Filter *.py | ForEach-Object {
    .\.venv\Scripts\python.exe $_.FullName
}
.\.venv\Scripts\python.exe -m compileall -q .\main.py .\py .\tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

The tests do not contain real account credentials and do not perform a real login.

## Build a Windows EXE

Install the build dependency into `.venv` and run the build script:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt
.\build-exe.ps1 -UpxPath 'D:\TOOLS\upx.exe'
```

The script:

- builds a single-file GUI executable with PyInstaller;
- uses UPX for compressible native components and the final executable;
- copies `project_config.json` beside the executable;
- applies a locally generated Authenticode self-signature for test use;
- prints the SHA-256 hash and signature status.

Build output is written to `dist\`. Intermediate files are written to `build\`. Both directories are ignored by Git.

### Signature limitation

The local certificate is self-signed. It is useful for identifying the build on the machine where the certificate is trusted, but it is **not** a public CA certificate. Other computers may still show an unknown publisher or a SmartScreen warning. A publicly trusted signature requires a certificate or an eligible open-source signing service; it cannot be created solely by running a local script.

## GitHub synchronization

Use the included proxy-aware script:

```powershell
.\push-git.ps1 -ShowMode
.\push-git.ps1 push -u origin main
```

Before pushing, review the staged files and confirm that no local runtime files, credentials, cookies, logs, screenshots or build output are included:

```powershell
git status --short
git diff --cached --name-only
```

## Repository hygiene

Ignored local data may be removed when it is no longer needed:

- `html/`, `txt/`, `png/`, `LOG/`: generated diagnostics and reports;
- `build/`: PyInstaller intermediate files;
- `dist/`: local EXE package;
- `.venv/`: local Python environment;
- `__pycache__/`: Python cache files.

Do not delete `json/local_config.json`, `json/cookies.json` or `json/auth_state.json` unless you intentionally want to clear local login state and saved settings.
