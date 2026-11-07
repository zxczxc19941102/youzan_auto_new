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

At runtime, the project root is limited to these application outputs:

- `config.ini`: local account and menu settings; ignored by Git.
- `log/`: runtime logs and failure diagnostics; ignored by Git.

The application no longer creates or uses separate `json/`, `html/`, `txt/`, `png/` or proxy-setting output directories. Existing legacy directories from older versions are not read by the current runtime.

Do not put real credentials in source files, README files, tests, screenshots, logs or issue reports. After a successful login, the GUI writes all menu states as lowercase `true`/`false` values to `config.ini`.

The password is stored as `dpapi:<base64>` using Windows DPAPI, bound to the current Windows user profile. The plaintext password is never written to `config.ini`, and a ciphertext copied to another Windows user cannot be decrypted. If `config.ini` is missing or decryption fails, enter the credentials again in the GUI.

The application automatically loads `config.ini` on startup and fills the account/password controls after the controls are initialized, then performs a read-back equality check.

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
- uses UPX only for supported native components (not forced compression of the final onefile EXE);
- embeds non-sensitive runtime defaults; copying only the EXE works without an external config;
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

### Portable startup / compatibility build

Use Windows x64 with Microsoft Edge installed. No system Python or copied `.venv` is required by the EXE. Run as a normal user in a writable folder. Do not copy local credentials, cookies or `auth_state.json` between computers. DPAPI-protected passwords must be re-entered on the new computer.

Build a separately signed, non-UPX compatibility executable for comparison:

```powershell
.\build-exe.ps1 -SkipUpx -BuildName YouzanAuto-Compatibility
```

Startup-only diagnostic (hidden GUI and local Edge test, no login or submission):

```powershell
$p = Start-Process .\YouzanAuto.exe -ArgumentList "--startup-check", "--check-browser" -PassThru -Wait
$p.ExitCode
Get-Content .\LOG\startup_check.json
```

Uncaught Python startup errors show a native dialog and write `LOG/startup_error.log`. If the EXE folder is not writable, the error log falls back to `%LOCALAPPDATA%/YouzanAuto/LOG/`. Loader, security-policy and pre-Python extraction failures may occur before Python diagnostics can run; collect the original Windows dialog/security history in that case. The signature is self-signed and is not automatically trusted on another PC.
