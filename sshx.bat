@echo off
rem sshx —— CLI 包装（cmd / PowerShell）。解释器可用 SSHX_PY 覆盖。
if defined SSHX_PY (
    "%SSHX_PY%" "%~dp0sshx.py" %*
) else (
    python "%~dp0sshx.py" %*
)
