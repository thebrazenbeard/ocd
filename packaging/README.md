# Optional observe-only startup (developer preview)

OCD does **not** automatically install a background service. These opt-in examples let an already-installed CLI run `ocd watch` at login on Windows or Linux. Watch mode **only scans, plans, and reports**; it does not apply moves, remove duplicates, upload models, or reorganize cloud storage. A service cannot make currently unimplemented automatic organization work.

Register your source/destination folders with the CLI and inspect a successful foreground `ocd --config PATH watch --interval 2` run **before** installing an automatic startup entry. Use an account that has read access only to folders that you intend to observe.

## Linux: systemd user service

1. Install OCD in a Python environment using `python3 -m pip install -e .` (or another deliberate installation method), configure its explicit roots, and find that Python interpreter's full path with `python3 -c 'import sys; print(sys.executable)'` from the same environment.
2. Copy [`linux/ocd-watch.service.template`](linux/ocd-watch.service.template) to `~/.config/systemd/user/ocd-watch.service`. Edit both `REPLACE_WITH_ABSOLUTE_*` placeholders to your chosen interpreter and config file paths. **Do not enable the unit while placeholders remain.** If your paths contain spaces or systemd-special characters, use systemd's documented command-line escaping; test with `systemd-analyze --user verify` where supported.
3. Review the completed unit and opt in with:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now ocd-watch.service
   systemctl --user status ocd-watch.service
   journalctl --user -u ocd-watch.service -n 30
   ```

To stop/uninstall the startup service, use `systemctl --user disable --now ocd-watch.service`, remove only your installed `~/.config/systemd/user/ocd-watch.service` file, then `systemctl --user daemon-reload`. This does not delete the OCD config or any files in registered roots. The user service runs while that user's service manager is active; no system service or lingering login session is enabled by OCD.

## Windows: per-user Scheduled Task

1. Install OCD into a Python interpreter on Windows; configure and inspect its registered roots. Check the correct Python launcher with `py -m ocd --help` or specify a known absolute interpreter path.
2. Review [`windows/Manage-OCDWatchTask.ps1`](windows/Manage-OCDWatchTask.ps1) first. Then deliberately install a **user-level, limited** task:

   ```powershell
   .\packaging\windows\Manage-OCDWatchTask.ps1 -Action Install `
     -ConfigPath 'C:\Users\YOUR_NAME\.config\ocd\config.json' -IntervalSeconds 2
   ```

   Use your real existing absolute configuration path. `-PythonExecutable 'C:\...\python.exe'` selects a specific interpreter. No administrative privileges, Task Scheduler password, or automatic elevation are requested. The task triggers only at interactive user logon, runs `-m ocd --config ... watch`, and does **not** call `apply`.
3. Inspect it using `Get-ScheduledTask -TaskName OCD-Watch-ObserveOnly` and Task Scheduler's history.

To remove the startup entry, invoke `.\packaging\windows\Manage-OCDWatchTask.ps1 -Action Remove`. Removal refuses to touch a task with the same name unless it has OCD's exact description marker. The config, watched data, and OCD installation remain untouched.

## Limits

- These are opt-in **startup helpers**, not packaged installers or tested production daemons.
- Neither platform enables filesystem-event listening, background move execution, cloud accounts, or synchronization.
- A host must separately qualify Python, permissions, paths, launcher behavior, log rotation, and sleep/resume handling before unattended use.
- Unregister a task/service before changing the Python environment or moving the config to a new path.
