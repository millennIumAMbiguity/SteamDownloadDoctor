"""Steam Download Doctor.

Reads Steam's content_log.txt, finds files Steam keeps failing to update with
"Device Corrupt", counts how many times it has looped on each, and offers to
delete the chosen files so Steam can re-download them. Nothing is deleted
without typing DELETE.

Works on Windows, Linux and macOS. Python 3.8+, standard library only.

Usage:
    python steam_corruption_review.py [content_log.txt | logs folder | Steam folder]
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

OTHER_FILES_MAX = 10   # max rows in the "other corrupt files" table
BAR_WIDTH = 30
SLOW_DISK_SECS = 2     # show a "waiting for drive" hint after this long

FAIL_RE = re.compile(
    r'AppID\s+(?P<app>\d+)\s+update canceled\s*:\s*Failed updating depot\s+(?P<depot>\d+)'
    r'\s+while reading chunk\s+"(?P<chunk>[^"]+)"\s+\(Device Corrupt\)\s+\(Corrupt game files\)'
    r'\s+"(?P<path>[^"]+)"'
)
VALIDATION_RE = re.compile(
    r'Validation:\s+(?P<bad>\d+)\s+chunks failed to read of\s+(?P<total>\d+)(?:\s+total)?'
    r'\s+in file\s+"(?P<path>[^"]+)"\s+\(Device Corrupt\)'
)
QUEUE_RE = re.compile(r'AppID\s+(?P<app>\d+)\s+(?:state changed\s*:.*Update Queued|scheduler update\s*:)')
TS_RE = re.compile(r'^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
ACF_NAME_RE = re.compile(r'"name"\s+"(?P<name>[^"]+)"')


# ── Console styling ─────────────────────────────────────────────────────────

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    CLEAR_EOL = "\033[K"


def setup_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        os.system("") # enables ANSI escape codes in the Windows console
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        for name in vars(C):
            if not name.startswith("_"):
                setattr(C, name, "")


def color(text: str, *styles: str) -> str:
    return "".join(styles) + text + C.RESET if styles else text


def progress(label: str, done: int, total: int, marks: set[int] | None = None,
             suffix: str = "", final: bool = False) -> None:
    """Draw a progress bar. Cells listed in `marks` are drawn red."""
    frac = min(done / total, 1.0) if total else 1.0
    filled = int(frac * BAR_WIDTH)
    marks = marks or set()
    cells = []
    for i in range(BAR_WIDTH):
        if i < filled or final:
            cells.append(color("█", C.RED) if i in marks else color("█", C.CYAN))
        else:
            cells.append(color("░", C.DIM))
    pct = 100.0 if final else frac * 100
    sys.stdout.write(f"\r  {label:<13}  {''.join(cells)} {pct:5.1f}%  {suffix}{C.CLEAR_EOL}")
    if final:
        sys.stdout.write("\n")
    sys.stdout.flush()


def row(label: str, value: str) -> None:
    print(f"  {color(f'{label:<15}', C.DIM)} {value}")


def header(title: str, style: str = "") -> None:
    style = style or C.CYAN
    print()
    print(color(f"  {title}", C.BOLD, style))
    print(color("  " + "─" * 62, C.DIM))


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# ── Data ─────────────────────────────────────────────────────────

@dataclass
class Failure:
    line_no: int
    ts: datetime | None
    app: str
    depot: str
    chunk: str
    path: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.app, self.depot, self.chunk, self.path)


@dataclass
class Validation:
    line_no: int
    ts: datetime | None
    bad: int
    total: int
    path: str # relative to the game folder


@dataclass
class LogData:
    failures: list[Failure]
    validations: list[Validation]
    queues: list[tuple[int, str]] # (line index, appid)


@dataclass
class Entry:
    """One corrupt file, with everything we know about it."""
    num: int
    latest: Failure
    loops: list[Failure]      # current retry loop, newest first
    total_hits: int           # all failures for this file in the whole log
    boundary: str
    name: str = ""
    size: int | None = None   # None if missing or unreadable
    status: str = "?"         # "ok", "missing" or an error message

    @property
    def path(self) -> Path:
        return Path(self.latest.path)


def parse_ts(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ── Finding the log ─────────────────────────────────────────────────────────

LOG_REL = Path("logs") / "content_log.txt"

# Steam install folders relative to a drive root (Windows).
WIN_DRIVE_DIRS = (
    "Steam",
    "Program Files (x86)/Steam",
    "Program Files/Steam",
    "Games/Steam",
)

# Steam install folders relative to the home directory (Linux / macOS).
UNIX_HOME_DIRS = (
    ".steam/steam",
    ".steam/root",
    ".local/share/Steam",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",   # Flatpak
    "snap/steam/common/.local/share/Steam",                  # Snap
    "Library/Application Support/Steam",                     # macOS
)


def _windows_fixed_drives() -> list[str]:
    """Drive roots like 'C:\\' for fixed/network disks (skips CD and card readers)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        drives = []
        for i in range(26):
            if mask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if k32.GetDriveTypeW(root) in (3, 4): # DRIVE_FIXED, DRIVE_REMOTE
                    drives.append(root)
        return drives
    except Exception:
        return [f"{d}:\\" for d in "CDEFGS" if os.path.exists(f"{d}:\\")]


def _windows_registry_steam() -> list[Path]:
    paths = []
    try:
        import winreg
    except ImportError:
        return paths
    for hive, key_path, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                paths.append(Path(winreg.QueryValueEx(key, value)[0]))
        except OSError:
            pass
    return paths


def find_logs() -> list[Path]:
    """All content_log.txt files found in common places, newest first."""
    steam_dirs: list[Path] = []
    if os.name == "nt":
        steam_dirs += _windows_registry_steam()
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                steam_dirs.append(Path(os.environ[env]) / "Steam")
        for drive in _windows_fixed_drives():
            steam_dirs += [Path(drive) / d for d in WIN_DRIVE_DIRS]
    else:
        home = Path.home()
        steam_dirs += [home / d for d in UNIX_HOME_DIRS]

    found: dict[str, Path] = {}
    for d in steam_dirs:
        log = d / LOG_REL
        try:
            if log.is_file():
                real = log.resolve() # dedupe symlinks like ~/.steam/steam
                found.setdefault(os.path.normcase(str(real)), real)
        except OSError:
            pass
    return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_log_input(text: str) -> Path | None:
    """Accept a content_log.txt path, a logs folder, or a Steam folder."""
    p = Path(os.path.expandvars(os.path.expanduser(text.strip().strip('"').strip("'"))))
    for candidate in (p, p / "content_log.txt", p / LOG_REL):
        if candidate.is_file():
            return candidate
    return None


def choose_log() -> Path | None:
    logs = find_logs()
    header("Steam log")
    if logs:
        for i, p in enumerate(logs, 1):
            st = p.stat()
            when = ago(datetime.fromtimestamp(st.st_mtime))
            tag = color(" (newest)", C.GREEN) if i == 1 and len(logs) > 1 else ""
            num = f"[{i}] " if len(logs) > 1 else ""
            print(f"  {color(num, C.BOLD, C.CYAN)}{p}{tag}")
            print(color(f"  {' ' * len(num)}updated {when}, {fmt_size(st.st_size)}", C.DIM))
        print()
        if len(logs) == 1:
            prompt = "  Press Enter to continue, paste another path, or q to quit: "
        else:
            prompt = f"  Pick 1-{len(logs)} (Enter = 1), paste a path, or q to quit: "
    else:
        print(color("  No Steam log found in common locations.", C.YELLOW))
        print(color("  Paste the Steam folder, its logs folder, or content_log.txt.", C.DIM))
        prompt = "  Path (or q to quit): "

    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return logs[0] if logs else None
        if answer.lower() in ("q", "quit", "exit"):
            return None
        if not answer and logs:
            return logs[0]
        if answer.isdigit() and logs:
            idx = int(answer)
            if 1 <= idx <= len(logs):
                return logs[idx - 1]
            print(color(f"  Pick a number between 1 and {len(logs)}.", C.YELLOW))
            continue
        if answer:
            if found := resolve_log_input(answer):
                return found
            print(color(f"  No content_log.txt found at: {answer}", C.YELLOW))


# ── Parsing ─────────────────────────────────────────────────────────

def read_log(path: Path) -> LogData:
    """Single pass over the log. The progress bar marks where corruption appears."""
    total = max(path.stat().st_size, 1)
    failures: list[Failure] = []
    validations: list[Validation] = []
    queues: list[tuple[int, str]] = []
    marks: set[int] = set()
    done = 0

    with open(path, "rb") as fh:
        for i, raw in enumerate(fh):
            done += len(raw)
            line = raw.decode("utf-8", errors="replace")
            if "Device Corrupt" in line:
                hit = False
                if m := FAIL_RE.search(line):
                    failures.append(Failure(i + 1, parse_ts(line), m["app"], m["depot"], m["chunk"], m["path"]))
                    hit = True
                elif m := VALIDATION_RE.search(line):
                    validations.append(Validation(i + 1, parse_ts(line), int(m["bad"]), int(m["total"]), m["path"]))
                    hit = True
                if hit:
                    marks.add(min(done * BAR_WIDTH // total, BAR_WIDTH - 1))
                    continue
            if "AppID" in line and (m := QUEUE_RE.search(line)):
                queues.append((i, m["app"]))
            if i % 1000 == 0:
                progress("Scanning log", done, total, marks)

    found = len(failures) + len(validations)
    summary = color(f"{found} corruption entries", C.RED) if found else color("clean", C.GREEN)
    progress("Scanning log", total, total, marks, summary, final=True)
    return LogData(failures, validations, queues)


def count_loops(data: LogData, target: Failure) -> tuple[list[Failure], str]:
    """Walk backward from `target` and collect identical failures (newest first).

    Stops at a different corrupt file for the same app, or at another app being
    queued after the target app's last queue event.
    """
    last_target_queue = max((i for i, app in data.queues if app == target.app), default=-1)
    boundary_queues = [(i, app) for i, app in data.queues
                       if app != target.app and last_target_queue < i < target.line_no - 1]
    stop_at = max(boundary_queues, default=None)

    streak: list[Failure] = []
    for f in reversed(data.failures):
        if f.line_no > target.line_no:
            continue
        if stop_at and f.line_no - 1 < stop_at[0]:
            return streak, f"Another game (AppID {stop_at[1]}) was queued before this loop"
        if f.key == target.key:
            streak.append(f)
        elif f.app == target.app:
            return streak, f"Different corrupt file for this game (line {f.line_no})"
    return streak, "Beginning of log"


def build_entries(data: LogData) -> list[Entry]:
    """One entry per distinct corrupt file, newest first."""
    latest: dict[tuple, Failure] = {}
    totals: dict[tuple, int] = {}
    for f in data.failures:
        latest[f.key] = f
        totals[f.key] = totals.get(f.key, 0) + 1
    ordered = sorted(latest.values(), key=lambda f: f.line_no, reverse=True)
    entries = []
    for n, f in enumerate(ordered, 1):
        loops, boundary = count_loops(data, f)
        entries.append(Entry(n, f, loops, totals[f.key], boundary))
    return entries


def check_disk(entries: list[Entry]) -> None:
    """Look up game names and file sizes in a worker thread with a live progress bar.

    Sleeping hard drives can take many seconds to answer the first request.
    """
    state = {"i": 0, "since": time.monotonic()}

    def work() -> None:
        for i, e in enumerate(entries):
            state["i"], state["since"] = i, time.monotonic()
            e.name = game_name(e.latest)
            try:
                e.size = e.path.stat().st_size
                e.status = "ok"
            except FileNotFoundError:
                e.status = "missing"
            except OSError as err:
                e.status = f"unreadable ({err.strerror or err})"
        state["i"] = len(entries)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    tick = 0
    while worker.is_alive():
        waited = time.monotonic() - state["since"]
        hint = color(f"waiting for drive to respond… {int(waited)}s", C.YELLOW) if waited > SLOW_DISK_SECS else ""
        progress("Checking disk", state["i"], len(entries),
                 suffix=f"{spinner[tick % len(spinner)]} {state['i']}/{len(entries)} files  {hint}")
        tick += 1
        worker.join(0.1)
    progress("Checking disk", 1, 1, suffix=f"{len(entries)} files", final=True)


# ── Helpers ─────────────────────────────────────────────────────────

def ago(ts: datetime | None) -> str:
    if ts is None:
        return "unknown"
    secs = int((datetime.now() - ts).total_seconds())
    return f"{human_duration(secs)} ago" if secs >= 60 else "just now"


def human_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def split_steam_path(file_path: str) -> tuple[Path | None, str | None]:
    """Return (steamapps folder, game folder name) from a full game file path."""
    parts = Path(file_path).parts
    lower = [p.lower() for p in parts]
    for i in range(len(lower) - 2):
        if lower[i] == "steamapps" and lower[i + 1] == "common":
            return Path(*parts[: i + 1]), parts[i + 2]
    return None, None


_name_cache: dict[str, str] = {}


def game_name(f: Failure) -> str:
    if f.app in _name_cache:
        return _name_cache[f.app]
    steamapps, folder = split_steam_path(f.path)
    name = None
    if steamapps:
        acf = steamapps / f"appmanifest_{f.app}.acf"
        try:
            if m := ACF_NAME_RE.search(acf.read_text(encoding="utf-8", errors="replace")):
                name = m["name"]
        except OSError:
            pass
    name = name or folder or f"AppID {f.app}"
    _name_cache[f.app] = name
    return name


def latest_validation(data: LogData, f: Failure) -> Validation | None:
    full = f.path.replace("\\", "/").lower()
    hits = [v for v in data.validations if full.endswith("/" + v.path.replace("\\", "/").lower())]
    return hits[-1] if hits else None


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def disk_text(e: Entry) -> str:
    if e.status == "ok" and e.size is not None:
        return fmt_size(e.size)
    if e.status == "missing":
        return color("missing (already removed?)", C.YELLOW)
    return color(e.status, C.RED)


# ── Output ─────────────────────────────────────────────────────────

def show_latest(data: LogData, e: Entry) -> None:
    f = e.latest
    header("Latest corruption", C.RED)
    row("Game", color(e.name, C.BOLD) + color(f"  (AppID {f.app})", C.DIM))
    row("File", color(e.path.name, C.BOLD))
    row("Folder", shorten(str(e.path.parent), 60))
    row("Last failed", ago(f.ts) + color(f"  ({f.ts:%Y-%m-%d %H:%M})" if f.ts else "", C.DIM))

    n = len(e.loops)
    stuck = ""
    first = e.loops[-1]
    if n > 1 and first.ts and f.ts:
        stuck = color(f"  (stuck for {human_duration(int((f.ts - first.ts).total_seconds()))})", C.DIM)
    row("Retry loops", color(plural(n, "loop"), C.BOLD, C.RED if n >= 3 else C.YELLOW) + stuck)
    if e.total_hits > n:
        row("Total in log", plural(e.total_hits, "failure"))

    if v := latest_validation(data, f):
        pct = v.bad / v.total * 100 if v.total else 0
        row("Unreadable", f"{v.bad}/{v.total} chunks ({pct:.0f}%)")
    row("On disk", disk_text(e))
    row("Depot / chunk", color(f"{f.depot} / {f.chunk[:12]}…", C.DIM))


def show_others(others: list[Entry]) -> None:
    shown = others[:OTHER_FILES_MAX]
    header(f"Other corrupt files ({len(shown)} of {len(others)})", C.RED)
    for e in shown:
        num = color(f"[{e.num}]", C.BOLD, C.CYAN)
        loops = color(f"{plural(len(e.loops), 'loop'):>9}", C.DIM)
        state = "" if e.status == "ok" else "  " + color("missing" if e.status == "missing" else "unreadable", C.YELLOW)
        print(f"  {num:<4} {shorten(e.name, 18):<18}  {shorten(e.path.name, 26):<26}  {ago(e.latest.ts):>12}  {loops}{state}")


# ── Fixing ─────────────────────────────────────────────────────────

def parse_selection(text: str, valid: dict[int, Entry]) -> list[Entry] | None:
    if text.lower() == "all":
        return list(valid.values())
    picks = []
    for token in re.split(r"[,\s]+", text):
        if not token:
            continue
        if not token.isdigit() or int(token) not in valid:
            return None
        if valid[int(token)] not in picks:
            picks.append(valid[int(token)])
    return picks


def delete_file(e: Entry) -> str:
    path = e.path
    resolved = path.resolve()
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        print(color(f"  ✖ {e.name}: real path differs from the logged path (link?). Skipped.", C.RED))
        print(color(f"      Logged:   {path}\n      Resolved: {resolved}", C.DIM))
        return f"REFUSED (path differs) {path}"
    try:
        resolved.unlink()
    except OSError as err:
        print(color(f"  ✖ {e.name}: could not delete — {err}", C.RED))
        return f"FAILED ({err}) {path}"
    print(color(f"  ✔ {e.name}: deleted {path.name}", C.GREEN))
    return f"DELETED {resolved}"


def offer_fix(entries: list[Entry], shown: list[Entry]) -> list[str]:
    deletable = {e.num: e for e in shown if e.status == "ok"}
    header("Fix")
    print("  Deleting a corrupt file makes Steam download a fresh copy on the next update.")
    print(color("  Note: 'Device Corrupt' that keeps coming back can mean a failing drive.", C.YELLOW))
    print()

    if not deletable:
        print(color("  None of the listed files exist on disk any more — nothing to delete.", C.GREEN))
        return ["nothing to delete"]

    for e in deletable.values():
        print(f"  {color(f'[{e.num}]', C.BOLD, C.CYAN)} {color(e.name, C.BOLD)}: {shorten(e.path.name, 44)}")
    print()
    if len(deletable) > 1:
        example = f"e.g. {' '.join(str(n) for n in list(deletable)[:2])}"
        prompt = f"  Number(s) to fix ({example}, or all). Enter to skip: "
    else:
        only = next(iter(deletable))
        prompt = f"  Type {only} to fix it, or press Enter to skip: "

    while True:
        answer = ask(prompt)
        if not answer:
            print(color("  Skipped. No files were deleted.", C.GREEN))
            return ["user skipped; nothing deleted"]
        picks = parse_selection(answer, deletable)
        if picks:
            break
        print(color(f"  Pick from: {', '.join(map(str, deletable))}", C.YELLOW))

    print()
    print(color(f"  About to delete {plural(len(picks), 'file')}:", C.BOLD))
    for e in picks:
        print(f"    {color(e.name, C.BOLD)}  {color(f'({fmt_size(e.size or 0)})', C.DIM)}")
        print(color(f"      {e.path}", C.DIM))
    print()
    while True:
        answer = ask(f"  Type {color('DELETE', C.BOLD, C.RED)} to confirm, or press Enter to cancel: ")
        if answer == "DELETE":
            break
        if not answer:
            print(color("  Cancelled. No files were deleted.", C.GREEN))
            return ["user cancelled; nothing deleted"]
        print(color(f"  '{answer}' is not DELETE (it's case-sensitive). Try again.", C.YELLOW))

    results = [delete_file(e) for e in picks]
    if any(r.startswith("DELETED") for r in results):
        print()
        print("  Next: in Steam, right-click the game → Properties → Installed Files →")
        print("  'Verify integrity of game files', or just restart the update.")
    return results


def write_report(log_path: Path, entries: list[Entry], actions: list[str]) -> Path:
    report = Path(__file__).with_suffix(".log")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[{now}] Steam Download Doctor", f"  Source log: {log_path}"]
    for e in entries[: OTHER_FILES_MAX + 1]:
        lines.append(
            f"  [{e.num}] {e.name} (AppID {e.latest.app}, depot {e.latest.depot}) | "
            f"{len(e.loops)} loops, {e.total_hits} total | last {e.latest.ts} | {e.status} | {e.latest.path}"
        )
    lines += [f"  Action: {a}" for a in actions]
    with open(report, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")
    return report


# ── Main ─────────────────────────────────────────────────────────

def main() -> int:
    setup_console()
    print()
    print(color("  ╭──────────────────────────────────────╮", C.CYAN))
    print(color("  │        Steam Download Doctor         │", C.CYAN))
    print(color("  ╰──────────────────────────────────────╯", C.CYAN))

    if len(sys.argv) > 1:
        log_path = resolve_log_input(sys.argv[1])
        if not log_path:
            print(color(f"\n  No content_log.txt found at: {sys.argv[1]}", C.RED))
            return 1
    else:
        log_path = choose_log()
        if not log_path:
            print("\n  No log selected.")
            return 1

    print()
    data = read_log(log_path)

    if not data.failures:
        print()
        if data.validations:
            v = data.validations[-1]
            print(color("  Validation errors found, but no 'Failed updating depot' line with a full path.", C.YELLOW))
            row("Latest", f"{shorten(v.path, 50)}  ({ago(v.ts)})")
        else:
            print(color("  ✔ No Device Corrupt errors in this log.", C.GREEN))
        return 0

    entries = build_entries(data)
    shown = entries[: OTHER_FILES_MAX + 1]
    check_disk(shown)

    show_latest(data, entries[0])
    if len(entries) > 1:
        show_others(entries[1:])

    actions = offer_fix(entries, shown)
    report = write_report(log_path, entries, actions)
    print()
    print(color(f"  Report appended to {report}", C.DIM))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n  Cancelled.")
    except Exception as e: # keep the window open on unexpected errors
        print(color(f"\n  ERROR: {e}", C.RED))
    if sys.stdin.isatty() and len(sys.argv) <= 1:
        print()
        while True:
            try:
                if not input("  Press Enter to quit: ").strip():
                    break
            except EOFError:
                break
    sys.exit(code)
