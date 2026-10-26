# Local setup notes

This file contains local setup guidance only. It must not contain real account numbers, passwords, cookies, access tokens or screenshots with sensitive data.

## Setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\run.cmd
```

The setup script creates `.venv` and installs the dependencies from `requirements.txt`.

## Credentials

Enter credentials in the GUI or provide placeholder-free values through environment variables in the current PowerShell session:

```powershell
$env:YOUZAN_USER = 'your-account'
$env:YOUZAN_PASSWORD = 'your-password'
```

Do not paste real values into source files, README files or Git commits. The application stores local password data using Windows DPAPI and ignores local runtime files through `.gitignore`.

## Build

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt
.\build-exe.ps1 -UpxPath 'D:\TOOLS\upx.exe'
```

The generated EXE is local build output and is not committed to GitHub.
