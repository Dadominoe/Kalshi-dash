"""Start-up for a HOSTED copy (Railway and similar): keep the history on the attached volume, then run the dashboard.

Set these variables on the host:   DASH_PASSWORD = the password people must type     READ_ONLY = 1 (no buy / arm / settings endpoints at all)
Attach a volume (Railway sets RAILWAY_VOLUME_MOUNT_PATH for you) so the history, streak and logs survive every restart and update.
"""
import os
import runpy
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

SEED_FILES = {"pvl_backtest.json", "cpi_history_stats.json", "line_behavior_stats.json", "market_open_stats.json", "cpi_cache.json"}    # shipped with the code; refreshed on every update

vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_VOLUME")
if vol:
    for name in ("data_cache", "bot_state"):
        target = os.path.join(vol, name)
        os.makedirs(target, exist_ok=True)
        local = os.path.join(HERE, name)
        if os.path.isdir(local) and not os.path.islink(local):
            for f in os.listdir(local):
                if name == "data_cache" and f in SEED_FILES:
                    shutil.copy2(os.path.join(local, f), os.path.join(target, f))          # the code's own statistics files always win
                elif not os.path.exists(os.path.join(target, f)):
                    shutil.copy2(os.path.join(local, f), os.path.join(target, f))
            shutil.rmtree(local)
        if not os.path.exists(local):
            try:
                os.symlink(target, local)
            except OSError as e:                                  # (Windows without rights) - fall back to a plain folder so the dashboard still starts
                print(f"[hosted] could not link {name} to the volume ({e}); using a normal folder")
                shutil.copytree(target, local)
    print(f"[hosted] history is kept on the volume at {vol}")
else:
    print("[hosted] WARNING: no volume attached - history will be lost every time the service restarts")
if not os.environ.get("DASH_PASSWORD"):
    print("[hosted] WARNING: DASH_PASSWORD is not set - anyone with the link can open this dashboard")

# ---- a snapshot of the owner's history (streak, records, pattern stats, price log): copied in whenever the STAMP changes, so the hosted copy matches the owner's
seed = os.path.join(HERE, "seed_data")
try:
    stamp = open(os.path.join(seed, "STAMP"), encoding="utf-8").read().strip()
    applied = os.path.join(HERE, "data_cache", ".seed_applied")
    done = open(applied, encoding="utf-8").read().strip() if os.path.exists(applied) else ""
    if stamp and stamp != done:
        for name in ("data_cache", "bot_state"):
            src_dir = os.path.join(seed, name)
            if os.path.isdir(src_dir):
                os.makedirs(os.path.join(HERE, name), exist_ok=True)
                for f in os.listdir(src_dir):
                    shutil.copy2(os.path.join(src_dir, f), os.path.join(HERE, name, f))
        open(applied, "w", encoding="utf-8").write(stamp)
        print(f"[hosted] history snapshot {stamp} copied in")
    else:
        print(f"[hosted] history snapshot {stamp} already applied")
except FileNotFoundError:
    pass

runpy.run_module("src.kalshi_web_dashboard", run_name="__main__")
