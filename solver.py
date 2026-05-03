"""
WinterHub Solver
Install: pip install aiohttp rich
Run:     python solver.py [--once] [--workers N]
"""

import asyncio
import aiohttp
import json
import os
import sys
import time
import random
import argparse
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich import box

# ── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR      = Path("/sdcard/Download/Solver")
CONFIG_PATH   = BASE_DIR / "config.json"
ACCOUNTS_PATH = BASE_DIR / "accounts.txt"
FAILED_PATH   = BASE_DIR / "failed.txt"
DEAD_PATH     = BASE_DIR / "dead.txt"
SESSION_PATH  = BASE_DIR / "session.json"
LOGS_DIR      = BASE_DIR / "logs"

GITHUB_RAW = "https://raw.githubusercontent.com/lucivaantarez/slv/main/solver.py"
VERSION    = "1.0.0"

# ── API ───────────────────────────────────────────────────────────────────────

WH_BASE          = "https://solver.wintercode.dev/api"
CAPTCHA_ENDPOINT = f"{WH_BASE}/captcha/solve"
POW_ENDPOINT     = f"{WH_BASE}/pow/solve"
BALANCE_ENDPOINT = f"{WH_BASE}/captcha/balance"
ROBLOX_AUTH_URL  = "https://users.roblox.com/v1/users/authenticated"

SUCCESS_STATUSES = {"CAPTCHA_SUCCESS", "NO_CAPTCHA", "POW_SUCCESS", "POS_SUCCESS", "NO_CHALLENGE"}
REFUND_STATUSES  = {"INVALID_COOKIES", "CAPTCHA_FAILED", "SOLVER_ERROR",
                    "INVALID_SOLVER_KEY", "INSUFFICIENT_BALANCE", "NO_SOLVER_KEY",
                    "UNSUPPORTED_CHALLENGE"}
BUSY_STATUS      = "SERVER_BUSY"

STATUS_ICON = {
    "CAPTCHA_SUCCESS": ("✦ SOLVED",  "cyan"),
    "NO_CAPTCHA":      ("◎ SKIP",    "bright_black"),
    "POW_SUCCESS":     ("⚡ POW",    "yellow"),
    "POS_SUCCESS":     ("◈ POS",     "green"),
    "NO_CHALLENGE":    ("◎ SKIP",    "bright_black"),
    "CAPTCHA_FAILED":  ("✗ FAIL",    "red"),
    "INVALID_COOKIES": ("✗ COOKIE",  "red"),
    "SOLVER_ERROR":    ("✗ ERR",     "red"),
    "SERVER_BUSY":     ("⧗ BUSY",    "yellow"),
    "GATEWAY_FAIL":    ("✗ DEAD",    "red"),
    "PENDING":         ("… WAIT",    "cyan"),
    "IDLE":            ("· IDLE",    "bright_black"),
}

# ── CONFIG ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "loop":                  True,
    "delay_minutes":         10,
    "max_workers":           3,
    "max_gateway_workers":   10,
    "gateway_cache_minutes": 30,
    "gateway_timeout":       5,
    "delay_per_cookie":      1,
    "solver_retries":        3,
    "solver_retry_delay":    5,
    "place_id":              0,
    "winter_api_key":        "",
    "discord_webhook":       "",
    "min_balance_warning":   1000,
    "min_battery":           20,
    "max_consecutive_fails": 3,
}

REQUIRED_FIELDS = {
    "place_id":       (int,),
    "winter_api_key": (str,),
    "max_workers":    (int,),
    "delay_minutes":  (int, float),
    "solver_retries": (int,),
}

def load_config() -> dict:
    raw = json.loads(CONFIG_PATH.read_text())
    return {**DEFAULT_CONFIG, **raw}

def validate_config(cfg: dict) -> list[str]:
    errors = []
    for field, types in REQUIRED_FIELDS.items():
        if field not in cfg:
            errors.append(f"missing field: {field}")
            continue
        if not isinstance(cfg[field], types):
            errors.append(f"{field} must be {types[0].__name__}")
    if not cfg.get("winter_api_key"):
        errors.append("winter_api_key is empty")
    if not cfg.get("place_id"):
        errors.append("place_id is 0 or missing")
    return errors

# ── ACCOUNTS ──────────────────────────────────────────────────────────────────

def load_accounts(cfg: dict) -> list[dict]:
    dead  = load_dead_names()
    lines = ACCOUNTS_PATH.read_text().splitlines()
    result = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 3:
            continue
        username, cookie = parts[0], parts[2]
        if username in dead:
            continue
        result.append({
            "username":           username,
            "cookie":             cookie,
            "status":             "IDLE",
            "rounds":             None,
            "solves":             0,
            "errors":             0,
            "consecutive_fails":  0,
            "last_solve_time":    None,
            "gateway_checked":    False,
            "gateway_ok":         False,
            "gateway_ts":         0.0,
        })
    return result

def load_dead_names() -> set[str]:
    if not DEAD_PATH.exists():
        return set()
    return {l.strip().split(":")[0] for l in DEAD_PATH.read_text().splitlines() if l.strip()}

def is_valid_cookie(cookie: str) -> bool:
    return cookie.startswith("_|WARNING:")

def trunc(name: str, n: int) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"

# ── SESSION ───────────────────────────────────────────────────────────────────

def save_session(data: dict) -> None:
    SESSION_PATH.write_text(json.dumps(data, indent=2))

def load_session() -> Optional[dict]:
    if not SESSION_PATH.exists():
        return None
    try:
        return json.loads(SESSION_PATH.read_text())
    except Exception:
        return None

def clear_session() -> None:
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()

# ── LOGGING ───────────────────────────────────────────────────────────────────

_log_lines: list[tuple[str, str]] = []
_log_file  = None

def open_log(cycle: int) -> None:
    global _log_file
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date    = datetime.now().strftime("%Y-%m-%d")
    _log_file = open(LOGS_DIR / f"{date}_cycle{cycle}.txt", "a")

def close_log() -> None:
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None

def log(msg: str, style: str = "white") -> None:
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"{ts}  {msg}"
    _log_lines.append((style, line))
    if len(_log_lines) > 300:
        _log_lines.pop(0)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()

# ── BATTERY & NOTIFY ─────────────────────────────────────────────────────────

def get_battery() -> Optional[int]:
    try:
        out = subprocess.check_output(["termux-battery-status"], timeout=3).decode()
        return json.loads(out).get("percentage")
    except Exception:
        return None

def notify(title: str, content: str) -> None:
    try:
        subprocess.Popen(
            ["termux-notification", "--title", title, "--content", content],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

# ── DISCORD ───────────────────────────────────────────────────────────────────

async def send_discord(http: aiohttp.ClientSession, webhook: str, content: str) -> None:
    if not webhook:
        return
    try:
        await http.post(webhook, json={"content": content},
                        timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass

def build_discord_msg(stats: dict, cycle: int) -> str:
    m, s = divmod(int(stats.get("elapsed", 0)), 60)
    total  = max(stats["total"], 1)
    solved = stats["solved"] + stats["pow"]
    ratio  = solved / total * 100
    return (
        f"**WinterHub Solver — Cycle {cycle} complete**\n```\n"
        f"✦ solved   : {solved}\n"
        f"◎ skip     : {stats['skip']}\n"
        f"✗ dead     : {stats['dead']}\n"
        f"✗ failed   : {stats['failed']}\n"
        f"ratio      : {ratio:.1f}%\n"
        f"time       : {m}m {s}s\n"
        f"cost       : Rp {stats['cost']:,}\n"
        f"balance    : Rp {stats['balance']:,}\n"
        f"```"
    )

# ── API CALLS ─────────────────────────────────────────────────────────────────

async def fetch_balance(http: aiohttp.ClientSession, api_key: str) -> dict:
    try:
        async with http.get(
            BALANCE_ENDPOINT,
            headers={"X-API-Key": api_key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return await r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

async def check_gateway(http: aiohttp.ClientSession, cookie: str, timeout: int) -> bool:
    try:
        async with http.get(
            ROBLOX_AUTH_URL,
            headers={"Cookie": f".ROBLOSECURITY={cookie}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            data = await r.json()
            return "id" in data
    except Exception:
        return False

async def do_solve(
    http:         aiohttp.ClientSession,
    api_key:      str,
    cookie:       str,
    place_id:     int,
    retries:      int,
    retry_delay:  int,
) -> dict:
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    payload = {"cookie": cookie, "placeId": place_id}
    last    = {}

    for attempt in range(retries + 1):
        try:
            async with http.post(
                CAPTCHA_ENDPOINT, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                result = await r.json()
                status = result.get("status", "")

                if status == BUSY_STATUS:
                    await asyncio.sleep(retry_delay * (2 ** attempt))
                    last = result
                    continue

                if status in {"CAPTCHA_FAILED", "SOLVER_ERROR"} and attempt < retries:
                    async with http.post(
                        POW_ENDPOINT, headers=headers, json=payload,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as pr:
                        pow_res = await pr.json()
                        if pow_res.get("success"):
                            return pow_res

                return result

        except Exception as e:
            last = {"success": False, "status": "SOLVER_ERROR", "error": str(e)}
            await asyncio.sleep(retry_delay * (2 ** attempt))

    return last

# ── STATE ─────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.is_paused       = False
        self.should_quit     = False
        self.balance         = {}
        self.cycle           = 1
        self.progress        = 0
        self.total           = 0
        self.speed           = 0.0
        self.eta_str         = "─"
        self.workers         = 3
        self.busy_streak     = 0
        self.cycle_start     = 0.0
        self.cycle_stats     = self._empty_stats()
        self.cycle_history:  list[dict] = []
        self.active_accs:    list[dict] = []
        self.recent_accs:    list[dict] = []

    @staticmethod
    def _empty_stats() -> dict:
        return {"solved": 0, "skip": 0, "pow": 0, "dead": 0,
                "failed": 0, "total": 0, "cost": 0, "rounds": []}

    def wh_balance(self) -> int:
        return self.balance.get("data", {}).get("winterhub", {}).get("balance", 0)

    def yc_pts(self) -> str:
        yc = self.balance.get("data", {}).get("yescaptcha") or {}
        b  = yc.get("balance", 0)
        return f"{b:,}" if b else "─"

    def cost_per_solve(self) -> int:
        return self.balance.get("data", {}).get("winterhub", {}).get("costPerSolve", 7)

    def cost_per_pow(self) -> int:
        return self.balance.get("data", {}).get("winterhub", {}).get("costPerPow", 5)

    def success_ratio(self) -> float:
        cs     = self.cycle_stats
        solved = cs["solved"] + cs["skip"] + cs["pow"]
        return solved / max(cs["total"], 1) * 100

    def elapsed_str(self) -> str:
        if not self.cycle_start:
            return "─"
        e = int(time.time() - self.cycle_start)
        return f"{e // 60}m {e % 60}s"

# ── UI ────────────────────────────────────────────────────────────────────────

def make_acc_table(accs: list[dict], start_idx: int, name_w: int) -> Table:
    tbl = Table(
        box=box.SIMPLE_HEAD, show_header=True,
        header_style="bright_black", padding=(0, 1), expand=True,
    )
    for col, w, j in [
        ("#", 3, "right"), ("User", name_w, "left"), ("Status", 12, "left"),
        ("Rounds", 6, "right"), ("Solves", 6, "right"),
        ("Errors", 6, "right"), ("Time", 6, "right"),
    ]:
        tbl.add_column(col, width=w, justify=j, no_wrap=True)

    for i, acc in enumerate(accs):
        icon, col = STATUS_ICON.get(acc["status"], ("? ???", "white"))
        dim  = acc["status"] in {"NO_CAPTCHA", "NO_CHALLENGE", "IDLE"}
        s    = "bright_black" if dim else col
        nc   = "bright_black" if dim else "white"
        rnd  = f"{acc['rounds']}r" if acc.get("rounds") else "─"
        tim  = acc.get("last_solve_time") or "─"
        err  = str(acc["errors"]) if acc["errors"] else "0"
        tbl.add_row(
            Text(str(start_idx + i), style="bright_black"),
            Text(trunc(acc["username"], name_w), style=nc + " bold"),
            Text(icon, style=s),
            Text(rnd, style="bright_black" if dim else "cyan"),
            Text(str(acc["solves"]), style="bright_black" if dim else "green"),
            Text(err, style="red" if acc["errors"] else "bright_black"),
            Text(tim, style="bright_black"),
        )
    return tbl

def build_ui(state: State, cfg: dict) -> Layout:
    console  = Console()
    term_w   = console.width or 222
    narrow   = term_w < 100
    name_w   = 8 if narrow else 16

    layout = Layout()
    layout.split_column(
        Layout(name="top",    size=2),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── TOP BAR ───────────────────────────────────────────────────────────────
    pct      = int(state.progress / max(state.total, 1) * 100)
    bar_w    = max(30, term_w // 3)
    filled   = int(pct / 100 * bar_w)
    sc       = "green" if not state.is_paused else "yellow"
    st_txt   = "running" if not state.is_paused else "PAUSED"

    top = Text()
    top.append("● ", style="green bold")
    top.append(f"Cycle #{state.cycle}", style="white bold")
    top.append("  |  ", style="bright_black")
    top.append(f"{state.total:,} accounts", style="white")
    top.append("  |  ", style="bright_black")
    top.append("status: ", style="bright_black")
    top.append(st_txt, style=f"{sc} bold")
    if state.is_paused:
        top.append("  [PAUSED]", style="yellow bold")
    top.append("\n")
    top.append("Progress  ", style="bright_black")
    top.append("━" * filled, style="cyan bold")
    top.append("░" * (bar_w - filled), style="bright_black")
    top.append(f"  {state.progress}/{state.total}  {pct}%", style="white")

    layout["top"].update(top)

    # ── LEFT ──────────────────────────────────────────────────────────────────
    cs      = state.cycle_stats
    rnds    = cs["rounds"]
    avg_r   = int(sum(rnds) / len(rnds)) if rnds else 0
    min_r   = min(rnds) if rnds else 0
    max_r   = max(rnds) if rnds else 0
    ratio   = state.success_ratio()
    bar_s   = 18
    fill_s  = int(ratio / 100 * bar_s)
    cps     = state.cost_per_solve()
    cpp     = state.cost_per_pow()

    # breakdown text
    bd = Text()
    bd.append("Challenges    ", style="bright_black")
    bd.append("PoW  ", style="white bold"); bd.append(f"{cs['pow']}    ·    ", style="bright_black")
    bd.append("PoS  ", style="white bold"); bd.append("0    ·    ", style="bright_black")
    bd.append("Captcha  ", style="white bold"); bd.append(f"{cs['solved']}    ·    ", style="cyan bold")
    bd.append("No Challenge  ", style="white bold"); bd.append(f"{cs['skip']}\n", style="bright_black")
    bd.append("Captcha Rounds    ", style="bright_black")
    bd.append("Average  ", style="white bold"); bd.append(f"{avg_r}r    ·    ", style="cyan bold")
    bd.append("Best  ", style="white bold");    bd.append(f"{min_r}r    ·    ", style="green bold")
    bd.append("Worst  ", style="white bold");   bd.append(f"{max_r}r\n", style="yellow bold")
    bd.append("Errors    ", style="bright_black")
    bd.append("Dead  ", style="white bold");   bd.append(f"{cs['dead']}    ·    ", style="red bold")
    bd.append("Failed  ", style="white bold"); bd.append(f"{cs['failed']}    ·    ", style="red bold")
    bd.append("Cookie  ", style="white bold"); bd.append("─    ·    ", style="bright_black")
    bd.append("Busy  ", style="white bold");   bd.append("─", style="yellow bold")

    # summary text
    sm = Text()
    sm.append("█" * fill_s,         style="green bold")
    sm.append("░" * (bar_s-fill_s), style="bright_black")
    sm.append(f"  {ratio:.0f}% success  ", style="green bold")
    sm.append("|  ",                 style="bright_black")
    sm.append("✓ ",                  style="green bold")
    sm.append(f"{cs['solved']+cs['pow']}  ", style="white bold")
    sm.append("✗ ",                  style="red bold")
    sm.append(f"{cs['dead']+cs['failed']}  ", style="white bold")
    sm.append("|  elapsed  ",        style="bright_black")
    sm.append(f"{state.elapsed_str()}  ", style="white bold")
    sm.append("|  cost  ",           style="bright_black")
    sm.append(f"Rp {cs['cost']:,}\n", style="cyan bold")
    sm.append("Captcha  ", style="bright_black")
    sm.append(f"{cs['solved']}  ", style="white bold")
    sm.append(f"× Rp {cps} =  ", style="bright_black")
    sm.append(f"Rp {cs['solved']*cps:,}     ", style="cyan bold")
    sm.append("POW  ", style="bright_black")
    sm.append(f"{cs['pow']}  ", style="white bold")
    sm.append(f"× Rp {cpp} =  ", style="bright_black")
    sm.append(f"Rp {cs['pow']*cpp:,}     ", style="cyan bold")
    sm.append("POS  0  × Rp 5 =  Rp 0", style="bright_black")

    # stats line
    st = Text()
    st.append("WinterHub Balance    ", style="bright_black")
    st.append(f"Rp {state.wh_balance():,}", style="cyan bold")
    st.append("          ·          ", style="bright_black")
    st.append("YesCaptcha Points    ", style="bright_black")
    st.append(state.yc_pts(), style="white bold")
    st.append("          ·          ", style="bright_black")
    st.append("Success Rate    ", style="bright_black")
    st.append(f"{ratio:.0f}%", style="green bold")

    layout["left"].update(Panel(Group(
        Panel(make_acc_table(state.active_accs, 1, name_w),
              title="[blue]◈[/blue]  [white bold]ACTIVE[/white bold]  "
                    f"[bright_black]({len(state.active_accs)} workers running)[/bright_black]",
              border_style="bright_black", padding=(0,0)),
        Panel(make_acc_table(state.recent_accs[-10:], len(state.active_accs)+1, name_w),
              title="[blue]◈[/blue]  [white bold]RECENTLY COMPLETED[/white bold]  "
                    f"[bright_black]({len(state.recent_accs)} accounts)[/bright_black]",
              border_style="bright_black", padding=(0,0)),
        Panel(bd, border_style="bright_black", padding=(0,1)),
        Panel(sm, title="[green]Cycle Summary[/green]",
              border_style="green", padding=(0,1)),
        Panel(st, border_style="bright_black", padding=(0,1)),
    ), border_style="bright_black", padding=(0,0)))

    # ── RIGHT ─────────────────────────────────────────────────────────────────

    # right stats bar
    rs = Text()
    rs.append("Cycle Progress    ", style="bright_black")
    rs.append(f"{state.progress}", style="cyan bold")
    rs.append(f" / {state.total:,}", style="bright_black")
    rs.append("    ·    ", style="bright_black")
    rs.append("Speed    ", style="bright_black")
    rs.append(f"{state.speed:.1f} / min", style="white bold")
    rs.append("    ·    ", style="bright_black")
    rs.append("ETA    ", style="bright_black")
    rs.append(state.eta_str, style="yellow bold")
    rs.append("    ·    ", style="bright_black")
    rs.append("Workers    ", style="bright_black")
    rs.append(f"{state.workers}", style="white bold")
    rs.append(f" / {cfg['max_workers']}", style="bright_black")

    # live feed table
    feed_tbl = Table(box=None, show_header=False, padding=(0,1), expand=True)
    feed_tbl.add_column("ts",  style="bright_black", width=10, no_wrap=True)
    feed_tbl.add_column("msg", no_wrap=True)
    feed = _log_lines[-20:]
    for style, line in feed:
        parts = line.split("  ", 1)
        ts_p  = parts[0] if parts else ""
        msg_p = parts[1] if len(parts) > 1 else line
        feed_tbl.add_row(ts_p, Text(msg_p, style=style, no_wrap=True))
    for _ in range(20 - len(feed)):
        feed_tbl.add_row("", "")

    # cycle history table
    ht = Table(box=box.SIMPLE_HEAD, show_header=True,
               header_style="bright_black", padding=(0,1), expand=True)
    for col, w, j in [
        ("Cycle",6,"left"),("Progress",14,"right"),("Ratio",6,"right"),
        ("Time",9,"left"),("Cost",12,"left"),("Dead/Fail",12,"left"),("Status",11,"left"),
    ]:
        ht.add_column(col, width=w, justify=j, no_wrap=True)

    for c in state.cycle_history:
        run = c["running"]
        pc  = "cyan" if run else "green"
        ht.add_row(
            Text(f"Cycle {c['n']}", style="white bold"),
            Text(f"{c['done']:,} / {c['total']:,}", style=pc),
            Text(f"{c['ratio']}%", style=pc),
            Text(c["time"], style="yellow" if run else "bright_black"),
            Text(f"Rp {c['cost']:,}", style="cyan"),
            Text(f"{c['dead']}d / {c['failed']}f", style="red"),
            Text("● run" if run else "✓ done", style=pc),
        )

    # per-cycle mini summaries
    mini_panels = []
    for c in state.cycle_history:
        pct_c = int(c["solved"] / max(c["done"], 1) * 100)
        f_c   = int(pct_c / 100 * 16)
        run   = c["running"]
        col   = "cyan" if run else "green"
        t     = Text()
        t.append(f"Cycle {c['n']}  ", style="white bold")
        t.append("● running" if run else "✓ complete", style=col)
        t.append("     ")
        t.append("█" * f_c,      style="green bold")
        t.append("░" * (16-f_c), style="bright_black")
        t.append(f"  {pct_c}%  ", style=f"{col} bold")
        t.append("|  ✓ ",         style="bright_black")
        t.append(f"{c['solved']}  ", style="white bold")
        t.append("✗ ",             style="red bold")
        t.append(f"{c['done']-c['solved']}  ", style="white bold")
        t.append("|  cost  ",      style="bright_black")
        t.append(f"Rp {c['cost']:,}  ", style="cyan bold")
        t.append("|  ",            style="bright_black")
        t.append(c["time"],        style="yellow" if run else "bright_black")
        mini_panels.append(Panel(t, border_style=col, padding=(0,1)))

    layout["right"].update(Panel(Group(
        Panel(rs, border_style="bright_black", padding=(0,1)),
        Panel(feed_tbl, title="[blue]◈[/blue]  [white bold]LIVE FEED[/white bold]",
              border_style="bright_black", padding=(0,0)),
        Panel(ht, title="[blue]◈[/blue]  [white bold]CYCLE HISTORY[/white bold]",
              border_style="bright_black", padding=(0,0)),
        Panel(Group(*mini_panels) if mini_panels else Text(""),
              title="[blue]◈[/blue]  [white bold]CYCLE SUMMARIES[/white bold]",
              border_style="bright_black", padding=(0,0)),
    ), border_style="bright_black", padding=(0,0)))

    # ── FOOTER ────────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%H:%M:%S")
    ft = Text()
    ft.append("[", style="bright_black"); ft.append("Q", style="white bold")
    ft.append("]  quit", style="bright_black")
    ft.append("          ")
    ft.append("[", style="bright_black"); ft.append("P", style="white bold")
    ft.append("]  pause / resume", style="bright_black")
    ft.append("          ")
    ft.append("[", style="bright_black"); ft.append("R", style="white bold")
    ft.append("]  refresh balance", style="bright_black")
    ft.append(f"          {ts}", style="bright_black")
    layout["footer"].update(ft)

    return layout

# ── STARTUP ───────────────────────────────────────────────────────────────────

def print_check(label: str, result: str, note: str = "") -> None:
    w    = 32
    dots = "." * (w - len(label))
    c    = "\033[92m" if result == "OK" else "\033[91m" if result == "FAIL" else "\033[93m"
    line = f"  {label}{dots} {c}{result}\033[0m"
    if note:
        line += f"  ({note})"
    print(line)

async def run_startup_checks(cfg: dict, http: aiohttp.ClientSession) -> bool:
    print("\n  checking environment...\n")

    errors = validate_config(cfg)
    if errors:
        print_check("config.json fields", "FAIL")
        for e in errors:
            print(f"  \033[91m✗\033[0m {e}")
        return False
    print_check("config.json fields", "OK")

    if not ACCOUNTS_PATH.exists():
        print_check("accounts.txt", "FAIL")
        print(f"  \033[91m✗\033[0m not found at {ACCOUNTS_PATH}")
        return False
    accounts = load_accounts(cfg)
    if not accounts:
        print_check("accounts.txt", "FAIL")
        print("  \033[91m✗\033[0m no valid accounts found")
        return False
    print_check("accounts.txt", "OK", f"{len(accounts):,} accounts")

    bat = get_battery()
    if bat is not None and bat < cfg.get("min_battery", 20):
        print_check("battery", "FAIL", f"{bat}% < {cfg['min_battery']}%")
        return False
    print_check("battery", "OK", f"{bat}%" if bat else "unknown")

    bal = await fetch_balance(http, cfg["winter_api_key"])
    if not bal.get("success"):
        print_check("WinterHub API", "FAIL")
        print(f"  \033[91m✗\033[0m {bal.get('error') or bal.get('status', '?')}")
        return False
    wh_bal = bal["data"]["winterhub"]["balance"]
    print_check("WinterHub API", "OK")

    if wh_bal < cfg.get("min_balance_warning", 1000):
        print_check("WH balance", "WARN", f"Rp {wh_bal:,} below threshold")
    else:
        print_check("WH balance", "OK", f"Rp {wh_bal:,}")

    print("\n  \033[92mall checks passed. starting solver...\033[0m\n")
    return True

async def check_update(http: aiohttp.ClientSession) -> bool:
    try:
        async with http.get(GITHUB_RAW, timeout=aiohttp.ClientTimeout(total=10)) as r:
            remote = await r.text()
            if f'VERSION    = "{VERSION}"' not in remote:
                Path(__file__).resolve().write_text(remote)
                return True
    except Exception:
        pass
    return False

# ── SOLVE ACCOUNT ─────────────────────────────────────────────────────────────

async def process_account(
    acc:      dict,
    cfg:      dict,
    http:     aiohttp.ClientSession,
    state:    State,
    cap_sem:  asyncio.Semaphore,
    gate_sem: asyncio.Semaphore,
) -> None:
    state.active_accs.append(acc)
    try:
        # format check
        if not is_valid_cookie(acc["cookie"]):
            acc["status"] = "INVALID_COOKIES"
            acc["errors"] += 1
            acc["consecutive_fails"] += 1
            log(f"{acc['username']}  →  ✗ invalid cookie format", "red")
            _append_failed(acc)
            return

        # gateway check
        now        = time.time()
        cache_secs = cfg.get("gateway_cache_minutes", 30) * 60
        if (not acc["gateway_checked"]) or (now - acc["gateway_ts"] > cache_secs):
            acc["status"] = "PENDING"
            async with gate_sem:
                alive = await check_gateway(http, acc["cookie"], cfg.get("gateway_timeout", 5))
            acc["gateway_checked"] = True
            acc["gateway_ts"]      = now
            acc["gateway_ok"]      = alive
            if not alive:
                acc["status"] = "GATEWAY_FAIL"
                acc["errors"] += 1
                acc["consecutive_fails"] += 1
                state.cycle_stats["dead"] += 1
                log(f"{acc['username']}  →  ✗ DEAD  (gateway rejected)", "red")
                _append_dead(acc)
                return

        await asyncio.sleep(cfg["delay_per_cookie"])

        acc["status"] = "PENDING"
        t0 = time.time()
        async with cap_sem:
            result = await do_solve(
                http, cfg["winter_api_key"], acc["cookie"],
                cfg["place_id"], cfg["solver_retries"], cfg["solver_retry_delay"],
            )
        acc["last_solve_time"] = f"{time.time()-t0:.1f}s"

        status = result.get("status", "SOLVER_ERROR")
        acc["status"] = status
        acc["rounds"] = result.get("rounds")

        # auto-scale workers
        if status == BUSY_STATUS:
            state.busy_streak += 1
            if state.busy_streak >= 3 and state.workers > 1:
                state.workers -= 1
                log(f"SERVER_BUSY streak  ·  reducing workers to {state.workers}", "yellow")
        else:
            if state.busy_streak > 0:
                state.busy_streak = max(0, state.busy_streak - 1)
                if state.workers < cfg["max_workers"]:
                    state.workers += 1

        # tally stats
        icon, _ = STATUS_ICON.get(status, ("?", "white"))
        if status in SUCCESS_STATUSES:
            acc["solves"] += 1
            acc["consecutive_fails"] = 0
            if status == "CAPTCHA_SUCCESS":
                state.cycle_stats["solved"] += 1
                if acc["rounds"]:
                    state.cycle_stats["rounds"].append(acc["rounds"])
                state.cycle_stats["cost"] += state.cost_per_solve()
            elif status in {"POW_SUCCESS", "POS_SUCCESS"}:
                state.cycle_stats["pow"] += 1
                state.cycle_stats["cost"] += state.cost_per_pow()
            else:
                state.cycle_stats["skip"] += 1
            rnd = f"  ·  {acc['rounds']} rounds" if acc.get("rounds") else ""
            log(f"{acc['username']}  →  {icon}{rnd}  ·  {acc['last_solve_time']}", "green")
        else:
            acc["errors"] += 1
            acc["consecutive_fails"] += 1
            state.cycle_stats["failed"] += 1
            log(f"{acc['username']}  →  {icon}  ({status})", "red")
            if status == "INVALID_COOKIES":
                _append_failed(acc)

        state.cycle_stats["total"] += 1

        if acc["consecutive_fails"] >= cfg.get("max_consecutive_fails", 3):
            _append_dead(acc)

    finally:
        if acc in state.active_accs:
            state.active_accs.remove(acc)
        state.recent_accs.append(acc)
        if len(state.recent_accs) > 20:
            state.recent_accs.pop(0)
        state.progress += 1

def _append_failed(acc: dict) -> None:
    with open(FAILED_PATH, "a") as f:
        f.write(f"{acc['username']}:{acc['cookie']}\n")

def _append_dead(acc: dict) -> None:
    date = datetime.now().strftime("%Y-%m-%d")
    with open(DEAD_PATH, "a") as f:
        f.write(f"{acc['username']}:{date}\n")
    log(f"{acc['username']}  →  marked dead", "yellow")

# ── RUN CYCLE ─────────────────────────────────────────────────────────────────

async def run_cycle(
    accounts: list[dict],
    cfg:      dict,
    state:    State,
    http:     aiohttp.ClientSession,
) -> dict:
    state.progress    = 0
    state.total       = len(accounts)
    state.cycle_start = time.time()
    state.cycle_stats = State._empty_stats()

    random.shuffle(accounts)
    open_log(state.cycle)
    log(f"Cycle {state.cycle} started  ·  {len(accounts):,} accounts  ·  {state.workers} workers  ·  shuffled", "white")

    cap_sem  = asyncio.Semaphore(state.workers)
    gate_sem = asyncio.Semaphore(cfg.get("max_gateway_workers", 10))
    t_start  = time.time()
    tasks: list[asyncio.Task] = []

    for acc in accounts:
        if state.should_quit:
            break
        while state.is_paused:
            await asyncio.sleep(0.5)

        tasks.append(asyncio.create_task(
            process_account(acc, cfg, http, state, cap_sem, gate_sem)
        ))

        if len(tasks) % 5 == 0 and state.progress > 0:
            elapsed       = time.time() - t_start
            state.speed   = state.progress / (elapsed / 60)
            remaining     = len(accounts) - state.progress
            eta           = remaining / max(state.speed / 60, 0.001)
            m, s          = divmod(int(eta), 60)
            state.eta_str = f"{m}m {s}s"

    await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.time() - t_start
    cs      = state.cycle_stats
    log(
        f"Cycle {state.cycle} complete  ·  "
        f"✓{cs['solved']+cs['pow']}  ✗{cs['dead']+cs['failed']}  ·  "
        f"{int(elapsed//60)}m {int(elapsed%60)}s  ·  Rp {cs['cost']:,}",
        "green",
    )
    close_log()
    return {**cs, "elapsed": elapsed}

# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",    action="store_true")
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        print(f"\033[91mERROR:\033[0m config.json not found at {CONFIG_PATH}")
        sys.exit(1)

    try:
        cfg = load_config()
    except json.JSONDecodeError as e:
        print(f"\033[91mERROR:\033[0m config.json invalid JSON: {e}")
        sys.exit(1)

    if args.workers:
        cfg["max_workers"] = args.workers

    async with aiohttp.ClientSession() as http:
        if not await run_startup_checks(cfg, http):
            sys.exit(1)

        if await check_update(http):
            print("\033[93m[!] script updated. please rerun.\033[0m")
            sys.exit(0)

        state         = State()
        state.workers = cfg["max_workers"]
        state.balance = await fetch_balance(http, cfg["winter_api_key"])

        prev = load_session()
        if prev:
            ans = input(
                f"\n  previous session found (cycle {prev.get('cycle',1)})  resume? [Y/n]: "
            ).strip().lower()
            if ans in ("", "y"):
                state.cycle = prev.get("cycle", 1)

        accounts = load_accounts(cfg)

        # key input thread
        def read_keys() -> None:
            try:
                import termios, tty
                fd  = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    while not state.should_quit:
                        ch = sys.stdin.read(1).lower()
                        if ch == "q":
                            state.should_quit = True
                        elif ch == "p":
                            state.is_paused = not state.is_paused
                            log("paused" if state.is_paused else "resumed", "yellow")
                        elif ch == "r":
                            asyncio.run_coroutine_threadsafe(
                                _refresh(http, cfg, state),
                                asyncio.get_event_loop(),
                            )
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

        threading.Thread(target=read_keys, daemon=True).start()

        with Live(build_ui(state, cfg), refresh_per_second=2, screen=True) as live:
            while not state.should_quit:
                try:
                    cfg = load_config()
                    if args.workers:
                        cfg["max_workers"] = args.workers
                except Exception:
                    pass

                bat = get_battery()
                if bat is not None and bat < cfg.get("min_battery", 20):
                    log(f"battery low ({bat}%)  ·  pausing", "yellow")
                    notify("WinterHub Solver", f"Battery low ({bat}%) — paused")
                    state.is_paused = True

                state.balance = await fetch_balance(http, cfg["winter_api_key"])
                wh_bal = state.wh_balance()
                if wh_bal < cfg.get("min_balance_warning", 1000):
                    log(f"balance low: Rp {wh_bal:,}  ·  pausing", "yellow")
                    notify("WinterHub Solver", f"Balance low: Rp {wh_bal:,}")
                    state.is_paused = True

                save_session({"cycle": state.cycle, "total": len(accounts)})

                cycle_task = asyncio.create_task(
                    run_cycle(accounts, cfg, state, http)
                )
                while not cycle_task.done():
                    live.update(build_ui(state, cfg))
                    await asyncio.sleep(0.5)

                stats = cycle_task.result()
                state.balance = await fetch_balance(http, cfg["winter_api_key"])

                state.cycle_history.append({
                    "n":       state.cycle,
                    "done":    state.progress,
                    "total":   state.total,
                    "solved":  state.cycle_stats["solved"] + state.cycle_stats["pow"],
                    "failed":  state.cycle_stats["failed"],
                    "dead":    state.cycle_stats["dead"],
                    "ratio":   int(state.success_ratio()),
                    "time":    state.elapsed_str(),
                    "cost":    state.cycle_stats["cost"],
                    "running": False,
                })

                live.update(build_ui(state, cfg))

                if cfg.get("discord_webhook"):
                    await send_discord(
                        http, cfg["discord_webhook"],
                        build_discord_msg({**stats, "balance": state.wh_balance()}, state.cycle),
                    )

                notify(
                    "WinterHub Solver",
                    f"Cycle {state.cycle} done  ·  "
                    f"{state.cycle_stats['solved']+state.cycle_stats['pow']} solved  ·  "
                    f"{state.success_ratio():.0f}%",
                )

                state.cycle += 1
                clear_session()

                if args.once or not cfg.get("loop", True) or state.should_quit:
                    break

                delay = int(cfg["delay_minutes"] * 60)
                log(f"next cycle in {cfg['delay_minutes']} minutes", "white")
                for remaining in range(delay, 0, -1):
                    if state.should_quit:
                        break
                    m, s          = divmod(remaining, 60)
                    state.eta_str = f"{m}m {s}s"
                    live.update(build_ui(state, cfg))
                    await asyncio.sleep(1)

        print("\n  [!] solver stopped.\n")

async def _refresh(http: aiohttp.ClientSession, cfg: dict, state: State) -> None:
    state.balance = await fetch_balance(http, cfg["winter_api_key"])
    log("balance refreshed", "white")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  [!] force quit.\n")
