# Project rules

## Scope

The only place this project ever touches is this folder (`C:\OPENCODE`).
Everything the app needs or creates — source file, JSON state files, and
SQLite database files — lives inside this folder and is created and modified
here.

## Hard rules

1. Work **only** inside `C:\OPENCODE`. Never read, search, modify, create,
   rename, or delete anything anywhere else on the computer: no other folders,
   no other drives, no home directory, no `%LOCALAPPDATA%`, no system files.
2. Files in scope:
   - `C:\OPENCODE\ChartSageAlgoOPENcode.py`
   - `C:\OPENCODE\ChartSageBhavcopy.py`
   - `C:\OPENCODE\bhavcopy_history.db`
   - `C:\OPENCODE\nse_bhavcopy_archive\` and its contents
   - `C:\OPENCODE\bhavcopy_snapshots\` and its contents
   - The bhavcopy feeder's JSON state and log files inside `C:\OPENCODE`
   - The app's JSON state files and SQLite database files that the app needs or
     creates — created and modified inside this folder only.
3. Restrict all file search (glob/grep) and shell commands to `C:\OPENCODE`.
4. If a task requires data, files, or actions outside `C:\OPENCODE`, stop and
   ask the user. Do not touch anything outside the folder.
5. Ephemeral scratch files may be written only to the OS temp directory and
   must never be persisted elsewhere.
6. Live-data testing: the user may explicitly grant permission to exercise the
   app's live state files inside `C:\OPENCODE` (for example, running the app
   against the real data folder to verify a feature end-to-end). Such work is
   still confined to `C:\OPENCODE`; it never extends to anything outside the
   folder, and a safety snapshot of the affected state files should be taken
   first.

## Distribution builds

1. Do **not** create or rebuild a PyInstaller/onedir distribution after routine
   source-code edits, bug fixes, GUI adjustments, or tests.
2. After ordinary changes, validate the source with appropriate compile checks
   and focused tests only.
3. Build or rebuild an onedir distribution only when the user explicitly asks
   to build, package, or refresh the distribution. Never infer a packaging
   request merely because code was modified.
4. Do not overwrite or delete an existing distribution unless that action is
   part of the user's explicit packaging request.
5. A requested final distribution must be a genuinely self-contained,
   portable Windows bundle that can be extracted or installed in any writable
   folder on another PC and run without Python or this development folder.
6. Never hard-code `C:\OPENCODE`, the build machine's user profile, drive
   letter, or another absolute development path into the packaged runtime.
   Resolve application resources from the executable/bundle location and keep
   writable state in documented portable locations within the bundle.
7. Include every runtime dependency and native component required by all suite
   programs, including the correct PySide6, Shiboken and Qt DLLs/plugins. Also
   include all required application modules, databases, historical membership
   and benchmark data, Bhavcopy data, archives/snapshots, scanner templates,
   watchlists, recipes, seed data, calendars, icons and other runtime assets.
8. Include required non-secret configuration/token support files. Never expose
   the owner's private credentials in a bundle for another person; credentials
   or API tokens that must be unique per user must have a portable first-run
   setup or documented configuration path.
9. Packaging is not complete merely because PyInstaller succeeds. Validate the
   staged bundle for required files, reject references to development-only
   absolute paths, verify bundled SQLite databases, and perform representative
   clean-bundle launch/import smoke tests for every packaged program. In
   particular, verify PySide6/Shiboken imports so the known missing-DLL failure
   cannot silently recur.
10. Create the final ZIP only after those checks pass. Keep a manifest or other
    auditable inventory of the files included in the final distribution.

## Backup completeness

1. `create_important_backup.py` is the restorable project-backup mechanism.
   Whenever persistent files or directories are added to the application,
   verify that this script includes them.
2. New persistent root-level source, state, configuration and database files
   must remain covered automatically. Every newly introduced persistent data
   directory must be added to `IMPORTANT_DIRECTORIES` explicitly.
3. Backups must include all source code, build specifications/scripts, JSON
   state, SQLite databases, Bhavcopy data, historical membership and benchmark
   data, templates, watchlists, recipes, seed data and the consolidated
   packaging runtime needed for restoration.
4. Continue to omit generated build/distribution folders, previous backups,
   bytecode/cache folders, temporary files, exported ZIP/XLSX reports and
   SQLite WAL/SHM sidecars.
5. Copy SQLite databases transactionally, verify their integrity, and create a
   checksum manifest. A backup change is incomplete until required-file
   coverage and database verification have been checked.
6. Before declaring any feature, refactor, new program, or data-storage change
   complete, perform a backup-impact review. If the work creates, renames,
   relocates, or removes any program, supporting module, persistent file,
   database, configuration, runtime asset, or data directory, update
   `create_important_backup.py` in the same change when necessary.
7. A newly added program must have all of its source modules, build
   specifications/scripts, persistent state, databases, templates and runtime
   assets covered by the backup. Do not assume that backing up its main `.py`
   file alone makes the program restorable.
8. After changing the backup inventory or application storage layout, compare
   the backup selection against the actual project inventory and the paths
   referenced by the applications. Confirm that every active persistent item
   is included and that every omission is deliberately transient, generated,
   superseded, private-to-the-OS, or otherwise documented.
9. Keep `create_important_backup.py` itself in every backup so the restored
   project retains the same backup capability. Future project improvements are
   not complete if their required files can run locally but cannot be restored
   from a newly created important backup.

## When in doubt

Re-read these rules before any read or write operation. The boundary is
absolute: nothing outside this folder, ever.
