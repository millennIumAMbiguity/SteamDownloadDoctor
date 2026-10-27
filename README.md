# Steam Download Doctor

> Updating/Validating/Patching infinite loop fixer

Finds game files Steam keeps failing to update with **"Device Corrupt"**, shows how
long it has been stuck, and lets you delete the broken file(s) so Steam downloads a
fresh copy.

Nothing is deleted unless you pick the file(s) and type `DELETE`.

## Run it

| System  | How                               |
| ------- | --------------------------------- |
| Windows | Double-click `run.bat`            |
| Linux   | `./run.sh`                        |
| Python  | `python steam_download_doctor.py` |

You can pass a path to skip the log picker. It accepts `content_log.txt`, the `logs`
folder, or the Steam folder:

```
run.bat S:\Steam
./run.sh ~/.local/share/Steam
```

Needs Python 3.8+ to run.

## What it shows

```
  Scanning log  ██████████████████████████████ 100.0%  708 corruption entries
  Checking disk ██████████████████████████████ 100.0%  2 files

  Latest corruption
  ──────────────────────────────────────────────────────────────
  Game            Star Birds  (AppID 2719750)
  File            cutscene-videos-local_assets_all_…bundle
  Last failed     just now  (2026-10-27 04:27)
  Retry loops     35 loops  (stuck for 47 min)
  Unreadable      242/506 chunks (48%)
  On disk         504.9 MB

  Other corrupt files (1 of 1)
  ──────────────────────────────────────────────────────────────
  [2]  Road to Vostok   RTV.pck   2d 4h ago   320 loops  missing
```

- **Scanning log bar**: red blocks mark corruption in log.
- **Checking disk bar**: checks files on disk. A sleeping hard drive can take 10–20 s to wake up.
- **Retry loops**: how many times in a row Steam failed on the same file, and for how long.
- **Other corrupt files**: up to 10 older ones. `missing` means the file is already gone.

## Fixing

The Fix section lists each deletable file with its game name. Type a number, several
(`1 2`), or `all`, then confirm by entering `DELETE`.\
If Steam currently is trying to update the application, it should automatically notice the file missing and re-download it.\
Alternatively restart the update or run **Properties → Installed Files → Verify integrity of game files** in Steam.

Only the exact file from Steam's error is deleted. It refuses if the real path differs
from the logged one (e.g. a link).

> A "Device Corrupt" error that keeps coming back after a fix can mean the drive is
> failing. Check its health (e.g. CrystalDiskInfo on Windows, `smartctl` on Linux).

## Where it looks for the log

- **Windows**: Steam registry entries, Program Files, and on every fixed drive:
  `\Steam`, `\Program Files (x86)\Steam`, `\Program Files\Steam`, `\Games\Steam`
- **Linux**: `~/.steam/steam`, `~/.local/share/Steam`, Flatpak and Snap installs
- **macOS**: `~/Library/Application Support/Steam`

If several are found, you can pick one from a displayed list. If not found, you can provide the path.

## Report

Each run appends a summary and what was deleted to `steam_download_doctor.log` next to the script.

## License

MIT. See [LICENSE](LICENSE).
