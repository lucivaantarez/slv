"""
WinterHub Solver — Web Dashboard Edition
Install: pip install aiohttp
Run:     python solver.py [--once] [--workers N] [--port 8080]
Open:    http://localhost:8080 in Chrome
"""

import asyncio
import aiohttp
from aiohttp import web
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

# ── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR      = Path("/sdcard/Download/Solver")
CONFIG_PATH   = BASE_DIR / "config.json"
ACCOUNTS_PATH = BASE_DIR / "accounts.txt"
FAILED_PATH   = BASE_DIR / "failed.txt"
DEAD_PATH     = BASE_DIR / "dead.txt"
SESSION_PATH  = BASE_DIR / "session.json"
LOGS_DIR      = BASE_DIR / "logs"

GITHUB_RAW = "https://raw.githubusercontent.com/lucivaantarez/slv/main/solver.py"
VERSION    = "2.0.0"

# ── API ───────────────────────────────────────────────────────────────────────

WH_BASE          = "https://solver.wintercode.dev/api"
CAPTCHA_ENDPOINT = f"{WH_BASE}/captcha/solve"
POW_ENDPOINT     = f"{WH_BASE}/pow/solve"
BALANCE_ENDPOINT = f"{WH_BASE}/captcha/balance"
ROBLOX_AUTH_URL  = "https://users.roblox.com/v1/users/authenticated"

SUCCESS_STATUSES = {"CAPTCHA_SUCCESS", "NO_CAPTCHA", "POW_SUCCESS", "POS_SUCCESS", "NO_CHALLENGE"}
BUSY_STATUS      = "SERVER_BUSY"

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
    "min_balance_warning":   0,
    "min_battery":           15,
    "max_consecutive_fails": 3,
    "performance_mode":      False,
    "web_port":              8080,
}

REQUIRED_FIELDS = {
    "place_id":       (int,),
    "winter_api_key": (str,),
    "max_workers":    (int,),
    "delay_minutes":  (int, float),
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

def parse_upc_line(line: str) -> Optional[tuple[str, str]]:
    """Parse UPC format: username:password:cookie (cookie may contain colons)"""
    parts = line.strip().split(":")
    if len(parts) < 3:
        return None
    username = parts[0]
    # cookie is everything from index 2 onward rejoined with ':'
    cookie   = ":".join(parts[2:])
    if not cookie:
        return None
    return username, cookie

def is_valid_cookie(cookie: str) -> bool:
    """Cookie must start with _|WARNING: (with colon)"""
    return cookie.startswith("_|WARNING:")

def load_accounts(cfg: dict) -> list[dict]:
    dead   = load_dead_names()
    result = []
    for line in ACCOUNTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_upc_line(line)
        if not parsed:
            continue
        username, cookie = parsed
        if username in dead:
            continue
        result.append({
            "username":          username,
            "cookie":            cookie,
            "status":            "IDLE",
            "rounds":            None,
            "solves":            0,
            "errors":            0,
            "consecutive_fails": 0,
            "last_solve_time":   None,
            "gateway_checked":   False,
            "gateway_ok":        False,
            "gateway_ts":        0.0,
        })
    return result

def load_dead_names() -> set[str]:
    if not DEAD_PATH.exists():
        return set()
    return {l.strip().split(":")[0] for l in DEAD_PATH.read_text().splitlines() if l.strip()}

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

_log_lines: list[dict] = []
_log_lock  = threading.Lock()
_log_file  = None

def open_log(cycle: int) -> None:
    global _log_file
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date      = datetime.now().strftime("%Y-%m-%d")
    _log_file = open(LOGS_DIR / f"{date}_cycle{cycle}.txt", "a")

def close_log() -> None:
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None

def log(msg: str, level: str = "info") -> None:
    ts   = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    with _log_lock:
        _log_lines.append(entry)
        if len(_log_lines) > 500:
            _log_lines.pop(0)
    if _log_file:
        _log_file.write(f"[{ts}] {msg}\n")
        _log_file.flush()
    print(f"[{ts}] {msg}")

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

def build_discord_msg(state: "State") -> str:
    cs     = state.cycle_stats
    solved = cs["solved"] + cs["pow"]
    ratio  = solved / max(cs["total"], 1) * 100
    e      = int(time.time() - state.cycle_start)
    return (
        f"**WinterHub Solver — Cycle {state.cycle} complete**\n```\n"
        f"✦ solved   : {solved}\n"
        f"◎ skip     : {cs['skip']}\n"
        f"✗ dead     : {cs['dead']}\n"
        f"✗ failed   : {cs['failed']}\n"
        f"ratio      : {ratio:.1f}%\n"
        f"time       : {e//60}m {e%60}s\n"
        f"cost       : Rp {cs['cost']:,}\n"
        f"balance    : Rp {state.wh_balance():,}\n"
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
    http:        aiohttp.ClientSession,
    api_key:     str,
    cookie:      str,
    place_id:    int,
    retries:     int,
    retry_delay: int,
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
        self.is_paused      = False
        self.should_quit    = False
        self.balance        = {}
        self.cycle          = 1
        self.progress       = 0
        self.total          = 0
        self.speed          = 0.0
        self.eta_str        = "─"
        self.workers        = 3
        self.busy_streak    = 0
        self.cycle_start    = 0.0
        self.cycle_stats    = self._empty_stats()
        self.cycle_history: list[dict] = []
        self.active_accs:   list[dict] = []
        self.recent_accs:   list[dict] = []
        self.next_cycle_in: int = 0
        self.perf_mode:     bool = False
        self.max_workers:   int  = 3

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "solved": 0, "skip": 0, "pow": 0,
            "dead": 0, "failed": 0, "total": 0,
            "cost": 0, "rounds": [],
        }

    def wh_balance(self) -> int:
        return self.balance.get("data", {}).get("winterhub", {}).get("balance", 0)

    def yc_pts(self) -> str:
        yc = self.balance.get("data", {}).get("yescaptcha") or {}
        b  = yc.get("balance", 0)
        return f"{int(b):,}" if b else "─"

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

    def to_json(self) -> dict:
        cs   = self.cycle_stats
        rnds = cs["rounds"]
        return {
            "cycle":        self.cycle,
            "is_paused":    self.is_paused,
            "progress":     self.progress,
            "total":        self.total,
            "speed":        round(self.speed, 1),
            "eta":          self.eta_str,
            "workers":      self.workers,
            "max_workers":  self.max_workers,
            "perf_mode":    self.perf_mode,
            "elapsed":      self.elapsed_str(),
            "next_cycle_in":self.next_cycle_in,
            "balance": {
                "wh":    self.wh_balance(),
                "yc":    self.yc_pts(),
                "ratio": round(self.success_ratio(), 1),
            },
            "stats": {
                "solved":  cs["solved"],
                "pow":     cs["pow"],
                "skip":    cs["skip"],
                "dead":    cs["dead"],
                "failed":  cs["failed"],
                "total":   cs["total"],
                "cost":    cs["cost"],
                "avg_r":   int(sum(rnds) / len(rnds)) if rnds else 0,
                "best_r":  min(rnds) if rnds else 0,
                "worst_r": max(rnds) if rnds else 0,
                "cost_captcha": cs["solved"] * self.cost_per_solve(),
                "cost_pow":     cs["pow"]    * self.cost_per_pow(),
            },
            "active":  [self._acc_json(a) for a in self.active_accs],
            "recent":  [self._acc_json(a) for a in self.recent_accs[-20:]],
            "history": self.cycle_history[-10:],
            "logs":    list(_log_lines[-50:]),
        }

    def _acc_json(self, acc: dict) -> dict:
        return {
            "username":        acc["username"],
            "status":          acc["status"],
            "rounds":          acc.get("rounds"),
            "solves":          acc["solves"],
            "errors":          acc["errors"],
            "last_solve_time": acc.get("last_solve_time"),
        }

# ── SSE BROADCAST ─────────────────────────────────────────────────────────────

_sse_clients: list[web.StreamResponse] = []
_sse_lock = threading.Lock()

async def broadcast_state(state: State) -> None:
    data = "data: " + json.dumps(state.to_json()) + "\n\n"
    dead = []
    with _sse_lock:
        clients = list(_sse_clients)
    for client in clients:
        try:
            await client.write(data.encode())
        except Exception:
            dead.append(client)
    if dead:
        with _sse_lock:
            for d in dead:
                if d in _sse_clients:
                    _sse_clients.remove(d)

# ── WEB DASHBOARD HTML ────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>WinterHub Solver</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #080c10;
  --bg2:      #0c1118;
  --bg3:      #0f1520;
  --border:   #1a2535;
  --border2:  #243040;
  --cyan:     #00d4ff;
  --cyan2:    #0099bb;
  --green:    #00ff88;
  --green2:   #00bb66;
  --yellow:   #ffcc00;
  --red:      #ff3355;
  --orange:   #ff8800;
  --magenta:  #dd44ff;
  --white:    #e8f0f8;
  --dim:      #4a6080;
  --dim2:     #2a3a50;
  --mono:     'JetBrains Mono', monospace;
  --head:     'Syne', sans-serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--white);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  min-height: 100vh;
  overflow-x: hidden;
}

/* scanlines */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px,
    rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px
  );
  pointer-events: none;
  z-index: 9999;
}

.layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto 1fr auto;
  gap: 0;
  min-height: 100vh;
}

/* ── HEADER ── */
.header {
  grid-column: 1 / -1;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 10px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.header-row1 {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 8px var(--green);
  flex-shrink: 0;
  animation: pulse 2s ease-in-out infinite;
}
.status-dot.paused { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }

@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

.cycle-label {
  font-family: var(--head);
  font-size: 15px;
  font-weight: 700;
  color: var(--white);
}

.header-sep { color: var(--dim2); }

.header-meta { color: var(--dim); font-size: 11px; }

.status-badge {
  margin-left: auto;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.15em;
  padding: 2px 8px;
  border-radius: 2px;
  background: rgba(0,255,136,0.1);
  color: var(--green);
  border: 1px solid rgba(0,255,136,0.2);
}
.status-badge.paused {
  background: rgba(255,204,0,0.1);
  color: var(--yellow);
  border-color: rgba(255,204,0,0.2);
}
.status-badge.perf {
  background: rgba(221,68,255,0.1);
  color: var(--magenta);
  border-color: rgba(221,68,255,0.2);
  margin-left: 6px;
}

.prog-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.prog-label { color: var(--dim); font-size: 11px; width: 60px; }

.prog-track {
  flex: 1;
  height: 4px;
  background: var(--bg3);
  border-radius: 2px;
  overflow: hidden;
  max-width: 400px;
}

.prog-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--cyan2), var(--cyan));
  border-radius: 2px;
  transition: width 0.8s ease;
  box-shadow: 0 0 6px var(--cyan2);
}

.prog-text { color: var(--cyan); font-size: 11px; font-weight: 700; min-width: 90px; }
.prog-pct  { color: var(--dim); font-size: 11px; }

/* ── STAT BAR ── */
.stat-bar {
  display: flex;
  align-items: center;
  gap: 0;
  flex-wrap: wrap;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 0 16px 0 0;
  border-right: 1px solid var(--border);
  margin-right: 16px;
}
.stat-item:last-child { border-right: none; }

.stat-label { font-size: 9px; letter-spacing: 0.12em; color: var(--dim); text-transform: uppercase; }
.stat-value { font-size: 14px; font-weight: 700; font-family: var(--head); }
.stat-value.cyan    { color: var(--cyan); }
.stat-value.green   { color: var(--green); }
.stat-value.yellow  { color: var(--yellow); }
.stat-value.white   { color: var(--white); }
.stat-value.magenta { color: var(--magenta); }

/* ── PANELS ── */
.panel {
  border-right: 1px solid var(--border);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.panel:last-of-type { border-right: none; }

.panel-head {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 7px 14px;
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 10px;
  letter-spacing: 0.15em;
  color: var(--dim);
  flex-shrink: 0;
}

.panel-icon { color: var(--cyan2); }
.panel-title { color: var(--white); font-weight: 700; }
.panel-note  { color: var(--dim); font-size: 10px; }

.panel-body { padding: 0; flex: 1; overflow: auto; }

/* ── TABLES ── */
.acc-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.acc-table th {
  background: var(--bg3);
  color: var(--dim);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 5px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
}

.acc-table td {
  padding: 5px 10px;
  border-bottom: 1px solid rgba(26,37,53,0.5);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.acc-table tr:hover td { background: rgba(255,255,255,0.02); }
.acc-table tr.dim-row td { opacity: 0.4; }

.c-num  { width: 36px; text-align: right; }
.c-user { width: 22%; }
.c-stat { width: 24%; }
.c-rnd  { width: 10%; text-align: right; }
.c-sol  { width: 10%; text-align: right; }
.c-err  { width: 10%; text-align: right; }
.c-time { width: 10%; text-align: right; }

.num-cell { color: var(--dim2); font-size: 10px; }
.user-cell { color: var(--white); font-weight: 500; }
.user-cell.dim { color: var(--dim); font-weight: 400; }

.status-cell {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 500;
}

.s-dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.s-solved  { color: var(--cyan);    } .s-dot.s-solved  { background: var(--cyan);    box-shadow: 0 0 4px var(--cyan);    animation: pulse 2s infinite; }
.s-pow     { color: var(--yellow);  } .s-dot.s-pow     { background: var(--yellow);  box-shadow: 0 0 4px var(--yellow);  }
.s-skip    { color: var(--dim);     } .s-dot.s-skip    { background: var(--dim2);    }
.s-fail    { color: var(--red);     } .s-dot.s-fail    { background: var(--red);     box-shadow: 0 0 4px var(--red);     }
.s-dead    { color: var(--red);     } .s-dot.s-dead    { background: var(--red);     }
.s-cookie  { color: var(--red);     } .s-dot.s-cookie  { background: var(--red);     }
.s-busy    { color: var(--orange);  } .s-dot.s-busy    { background: var(--orange);  box-shadow: 0 0 4px var(--orange);  animation: pulse 0.8s infinite; }
.s-wait    { color: var(--cyan2);   } .s-dot.s-wait    { background: var(--cyan2);   animation: pulse 1.5s infinite; }
.s-gateway { color: var(--magenta); } .s-dot.s-gateway { background: var(--magenta); animation: pulse 1s infinite; }
.s-idle    { color: var(--dim2);    } .s-dot.s-idle    { background: var(--bg3);     }

.rnd-cell  { color: var(--cyan);   font-size: 11px; font-weight: 700; }
.sol-cell  { color: var(--green);  font-size: 11px; }
.err-cell  { color: var(--red);    font-size: 11px; }
.err-zero  { color: var(--dim2);   font-size: 11px; }
.time-cell { color: var(--dim);    font-size: 10px; }

/* ── LOG ── */
.log-wrap { padding: 6px 0; }

.log-line {
  display: flex;
  gap: 10px;
  padding: 2px 14px;
  font-size: 11px;
  line-height: 1.5;
  transition: background 0.1s;
}
.log-line:hover { background: rgba(255,255,255,0.02); }

.log-ts   { color: var(--dim2); flex-shrink: 0; width: 58px; font-size: 10px; }
.log-msg  { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.log-msg.info    { color: var(--dim); }
.log-msg.ok      { color: var(--green); }
.log-msg.warn    { color: var(--yellow); }
.log-msg.error   { color: var(--red); }
.log-msg.success { color: var(--cyan); }

/* ── BREAKDOWN ── */
.breakdown {
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-bottom: 1px solid var(--border);
}

.bd-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  flex-wrap: wrap;
}

.bd-label { color: var(--dim); width: 110px; flex-shrink: 0; font-size: 10px; letter-spacing: 0.08em; }
.bd-sep   { color: var(--dim2); }

.bd-key   { color: var(--white); font-weight: 600; }
.bd-val   { font-weight: 700; }
.bd-val.cyan    { color: var(--cyan); }
.bd-val.green   { color: var(--green); }
.bd-val.yellow  { color: var(--yellow); }
.bd-val.red     { color: var(--red); }
.bd-val.dim     { color: var(--dim); }
.bd-val.magenta { color: var(--magenta); }

/* ── SUMMARY BOX ── */
.summary-box {
  margin: 10px 14px;
  border: 1px solid var(--green2);
  border-radius: 3px;
  overflow: hidden;
}

.summary-head {
  background: rgba(0,255,136,0.05);
  border-bottom: 1px solid var(--green2);
  padding: 5px 12px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.15em;
  color: var(--green);
}

.summary-body { padding: 8px 12px; display: flex; flex-direction: column; gap: 5px; }

.summary-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  flex-wrap: wrap;
}

.mini-bar-wrap { display: flex; gap: 2px; }
.mini-seg { width: 10px; height: 8px; border-radius: 1px; }
.mini-seg.ok  { background: var(--green2); }
.mini-seg.bad { background: var(--bg3); border: 1px solid var(--border); }

.summary-sep { color: var(--dim2); }

/* ── CYCLE HISTORY ── */
.history-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.history-table th {
  background: var(--bg3);
  color: var(--dim);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 5px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
}

.history-table td {
  padding: 5px 10px;
  border-bottom: 1px solid rgba(26,37,53,0.5);
  white-space: nowrap;
}

.hc-n      { width: 14%; }
.hc-prog   { width: 20%; }
.hc-ratio  { width: 10%; }
.hc-time   { width: 14%; }
.hc-cost   { width: 16%; }
.hc-df     { width: 16%; }
.hc-status { width: 10%; }

.run-badge  { color: var(--cyan);  font-weight: 700; font-size: 10px; }
.done-badge { color: var(--green); font-weight: 700; font-size: 10px; }

/* ── MINI SUMMARIES ── */
.mini-summaries { display: flex; flex-direction: column; gap: 6px; padding: 10px 14px; }

.mini-sum {
  border: 1px solid var(--border2);
  border-radius: 2px;
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.mini-sum.running { border-color: var(--cyan2); }
.mini-sum.done    { border-color: var(--border2); }

.mini-sum-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.mini-sum-body {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  flex-wrap: wrap;
}

/* ── FOOTER ── */
.footer {
  grid-column: 1 / -1;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  padding: 6px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 10px;
  color: var(--dim);
}

.footer-keys { display: flex; gap: 16px; }
.key-bind { display: flex; gap: 4px; align-items: center; }
.key-k {
  background: var(--bg3);
  border: 1px solid var(--border2);
  color: var(--white);
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 2px;
  font-family: var(--mono);
  font-size: 10px;
}

.footer-clock { color: var(--dim); font-size: 10px; }

/* ── CONNECTION STATUS ── */
.conn-indicator {
  position: fixed;
  top: 8px;
  right: 8px;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
  z-index: 10000;
  transition: all 0.3s;
}
.conn-indicator.disconnected {
  background: var(--red);
  box-shadow: 0 0 6px var(--red);
}

/* scrollbar */
::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
</style>
</head>
<body>

<div class="conn-indicator" id="conn"></div>

<div class="layout">

  <!-- HEADER -->
  <div class="header" id="header">
    <div class="header-row1">
      <span class="status-dot" id="sdot"></span>
      <span class="cycle-label" id="cycle-label">Cycle #1</span>
      <span class="header-sep">|</span>
      <span class="header-meta" id="acct-count">─ accounts</span>
      <span class="header-sep">|</span>
      <span class="header-meta">status: <span id="status-txt">─</span></span>
      <span class="status-badge" id="status-badge">RUNNING</span>
      <span class="status-badge perf" id="perf-badge" style="display:none">⚡ PERF MODE</span>
    </div>
    <div class="prog-row">
      <span class="prog-label">Progress</span>
      <div class="prog-track"><div class="prog-fill" id="prog-fill" style="width:0%"></div></div>
      <span class="prog-text" id="prog-text">0 / 0</span>
      <span class="prog-pct" id="prog-pct">0%</span>
    </div>
    <div class="stat-bar">
      <div class="stat-item">
        <span class="stat-label">WinterHub Balance</span>
        <span class="stat-value cyan" id="s-wh">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">YesCaptcha Points</span>
        <span class="stat-value white" id="s-yc">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Success Rate</span>
        <span class="stat-value green" id="s-ratio">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Speed</span>
        <span class="stat-value yellow" id="s-speed">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">ETA</span>
        <span class="stat-value yellow" id="s-eta">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Workers</span>
        <span class="stat-value magenta" id="s-workers">─</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Next Cycle</span>
        <span class="stat-value dim" id="s-next">─</span>
      </div>
    </div>
  </div>

  <!-- LEFT PANEL -->
  <div class="panel">

    <!-- ACTIVE -->
    <div class="panel-head">
      <span class="panel-icon">◈</span>
      <span class="panel-title">ACTIVE</span>
      <span class="panel-note" id="active-note">(0 workers running)</span>
    </div>
    <div class="panel-body" style="max-height:160px; overflow-y:auto;">
      <table class="acc-table">
        <thead><tr>
          <th class="c-num">#</th>
          <th class="c-user">User</th>
          <th class="c-stat">Status</th>
          <th class="c-rnd">Rnd</th>
          <th class="c-sol">Sol</th>
          <th class="c-err">Err</th>
          <th class="c-time">Time</th>
        </tr></thead>
        <tbody id="active-body"></tbody>
      </table>
    </div>

    <!-- RECENTLY COMPLETED -->
    <div class="panel-head">
      <span class="panel-icon">◈</span>
      <span class="panel-title">RECENTLY COMPLETED</span>
      <span class="panel-note" id="recent-note">(0 accounts)</span>
    </div>
    <div class="panel-body" style="flex:1; overflow-y:auto;">
      <table class="acc-table">
        <thead><tr>
          <th class="c-num">#</th>
          <th class="c-user">User</th>
          <th class="c-stat">Status</th>
          <th class="c-rnd">Rnd</th>
          <th class="c-sol">Sol</th>
          <th class="c-err">Err</th>
          <th class="c-time">Time</th>
        </tr></thead>
        <tbody id="recent-body"></tbody>
      </table>
    </div>

    <!-- BREAKDOWN -->
    <div class="breakdown" id="breakdown">
      <div class="bd-row">
        <span class="bd-label">Challenges</span>
        <span class="bd-key">PoW</span> <span class="bd-val cyan" id="bd-pow">0</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">PoS</span> <span class="bd-val cyan">0</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Captcha</span> <span class="bd-val cyan" id="bd-captcha">0</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">No Challenge</span> <span class="bd-val dim" id="bd-noch">0</span>
      </div>
      <div class="bd-row">
        <span class="bd-label">Captcha Rounds</span>
        <span class="bd-key">Average</span> <span class="bd-val cyan" id="bd-avg">─</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Best</span> <span class="bd-val green" id="bd-best">─</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Worst</span> <span class="bd-val yellow" id="bd-worst">─</span>
      </div>
      <div class="bd-row">
        <span class="bd-label">Errors</span>
        <span class="bd-key">Dead</span> <span class="bd-val red" id="bd-dead">0</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Failed</span> <span class="bd-val red" id="bd-failed">0</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Cookie</span> <span class="bd-val dim">─</span>
        <span class="bd-sep">·</span>
        <span class="bd-key">Busy</span> <span class="bd-val dim">─</span>
      </div>
    </div>

    <!-- CYCLE SUMMARY -->
    <div class="summary-box" id="summary-box">
      <div class="summary-head">CYCLE SUMMARY</div>
      <div class="summary-body">
        <div class="summary-row">
          <div class="mini-bar-wrap" id="sum-bar"></div>
          <span id="sum-ratio" style="color:var(--green);font-weight:700;">0% success</span>
          <span class="summary-sep">|</span>
          <span style="color:var(--green)">✓</span> <span id="sum-solved" style="color:var(--white);font-weight:700;">0</span>
          <span style="color:var(--red)">✗</span> <span id="sum-failed" style="color:var(--white);font-weight:700;">0</span>
          <span class="summary-sep">|</span>
          <span style="color:var(--dim)">elapsed</span> <span id="sum-elapsed" style="color:var(--white);font-weight:700;">─</span>
          <span class="summary-sep">|</span>
          <span style="color:var(--dim)">cost</span> <span id="sum-cost" style="color:var(--cyan);font-weight:700;">Rp 0</span>
        </div>
        <div class="summary-row">
          <span style="color:var(--dim)">Captcha</span>
          <span id="sum-cap-n" style="color:var(--white);font-weight:700;">0</span>
          <span id="sum-cap-cost" style="color:var(--dim)">× Rp 7 = </span>
          <span id="sum-cap-total" style="color:var(--cyan);font-weight:700;">Rp 0</span>
          &nbsp;&nbsp;&nbsp;
          <span style="color:var(--dim)">POW</span>
          <span id="sum-pow-n" style="color:var(--white);font-weight:700;">0</span>
          <span id="sum-pow-cost" style="color:var(--dim)">× Rp 5 = </span>
          <span id="sum-pow-total" style="color:var(--cyan);font-weight:700;">Rp 0</span>
          &nbsp;&nbsp;&nbsp;
          <span style="color:var(--dim)">POS</span>
          <span style="color:var(--white);font-weight:700;">0</span>
          <span style="color:var(--dim)">× Rp 5 = </span>
          <span style="color:var(--cyan);font-weight:700;">Rp 0</span>
        </div>
      </div>
    </div>

  </div><!-- /left panel -->

  <!-- RIGHT PANEL -->
  <div class="panel">

    <!-- LIVE FEED -->
    <div class="panel-head">
      <span class="panel-icon">◈</span>
      <span class="panel-title">LIVE FEED</span>
    </div>
    <div class="panel-body" style="flex:1; overflow-y:auto;" id="feed-wrap">
      <div class="log-wrap" id="feed-body"></div>
    </div>

    <!-- CYCLE HISTORY -->
    <div class="panel-head">
      <span class="panel-icon">◈</span>
      <span class="panel-title">CYCLE HISTORY</span>
    </div>
    <div class="panel-body" style="max-height:160px; overflow-y:auto;">
      <table class="history-table">
        <thead><tr>
          <th class="hc-n">Cycle</th>
          <th class="hc-prog">Progress</th>
          <th class="hc-ratio">Ratio</th>
          <th class="hc-time">Time</th>
          <th class="hc-cost">Cost</th>
          <th class="hc-df">Dead / Fail</th>
          <th class="hc-status">Status</th>
        </tr></thead>
        <tbody id="history-body"></tbody>
      </table>
    </div>

    <!-- CYCLE SUMMARIES -->
    <div class="panel-head">
      <span class="panel-icon">◈</span>
      <span class="panel-title">CYCLE SUMMARIES</span>
    </div>
    <div class="panel-body" style="overflow-y:auto; flex:1;">
      <div class="mini-summaries" id="mini-summaries"></div>
    </div>

  </div><!-- /right panel -->

  <!-- FOOTER -->
  <div class="footer">
    <div class="footer-keys">
      <div class="key-bind"><span class="key-k">Q</span> quit</div>
      <div class="key-bind"><span class="key-k">P</span> pause / resume</div>
      <div class="key-bind"><span class="key-k">R</span> refresh balance</div>
      <div class="key-bind"><span class="key-k">M</span> toggle perf mode</div>
    </div>
    <div class="footer-clock" id="footer-clock">─</div>
  </div>

</div><!-- /layout -->

<script>
// ── STATUS MAP ─────────────────────────────────────────────────────────────
const ST = {
  CAPTCHA_SUCCESS: { cls: 's-solved',  icon: '✦', label: 'SOLVED'   },
  NO_CAPTCHA:      { cls: 's-skip',    icon: '◎', label: 'SKIP'     },
  POW_SUCCESS:     { cls: 's-pow',     icon: '⚡', label: 'POW'      },
  POS_SUCCESS:     { cls: 's-solved',  icon: '◈', label: 'POS'      },
  NO_CHALLENGE:    { cls: 's-skip',    icon: '◎', label: 'SKIP'     },
  CAPTCHA_FAILED:  { cls: 's-fail',    icon: '✗', label: 'FAILED'   },
  INVALID_COOKIES: { cls: 's-cookie',  icon: '✗', label: 'COOKIE'   },
  SOLVER_ERROR:    { cls: 's-fail',    icon: '✗', label: 'ERROR'    },
  SERVER_BUSY:     { cls: 's-busy',    icon: '⧗', label: 'BUSY'     },
  GATEWAY_FAIL:    { cls: 's-dead',    icon: '✗', label: 'DEAD'     },
  PENDING:         { cls: 's-wait',    icon: '…', label: 'WAITING'  },
  IDLE:            { cls: 's-idle',    icon: '·', label: 'IDLE'     },
};

const LOG_CLS = {
  ok: 'ok', error: 'error', warn: 'warn',
  info: 'info', success: 'success',
};

function trunc(s, n) {
  return s.length > n ? s.slice(0, n-1) + '…' : s;
}

function fmtNum(n) {
  return Number(n).toLocaleString();
}

function miniBar(done, total, segs=16) {
  const f = Math.round(done / Math.max(total,1) * segs);
  let html = '<div class="mini-bar-wrap">';
  for (let i = 0; i < segs; i++) {
    html += `<div class="mini-seg ${i < f ? 'ok' : 'bad'}"></div>`;
  }
  return html + '</div>';
}

function accRow(acc, idx, dim=false) {
  const st  = ST[acc.status] || { cls: 's-idle', icon: '?', label: acc.status };
  const rnd = acc.rounds ? `${acc.rounds}r` : '─';
  const err = acc.errors > 0
    ? `<span class="err-cell">${acc.errors}</span>`
    : `<span class="err-zero">0</span>`;
  const tim = acc.last_solve_time || '─';
  const dimCls = dim ? ' dim-row' : '';
  const uCls   = dim ? 'dim' : '';
  return `<tr class="${dimCls}">
    <td class="c-num num-cell">${idx}</td>
    <td class="c-user user-cell ${uCls}">${trunc(acc.username, 16)}</td>
    <td class="c-stat">
      <div class="status-cell">
        <span class="s-dot ${st.cls}"></span>
        <span class="${st.cls}">${st.icon}  ${st.label}</span>
      </div>
    </td>
    <td class="c-rnd rnd-cell">${rnd}</td>
    <td class="c-sol sol-cell">${acc.solves}</td>
    <td class="c-err">${err}</td>
    <td class="c-time time-cell">${tim}</td>
  </tr>`;
}

// ── DOM REFS ───────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

// ── UPDATE UI ──────────────────────────────────────────────────────────────
let lastLogCount = 0;
let feedAtBottom = true;
const feedWrap = $('feed-wrap');

feedWrap.addEventListener('scroll', () => {
  feedAtBottom = feedWrap.scrollTop + feedWrap.clientHeight >= feedWrap.scrollHeight - 20;
});

function update(d) {
  const pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;

  // header
  $('sdot').className       = 'status-dot' + (d.is_paused ? ' paused' : '');
  $('cycle-label').textContent = `Cycle #${d.cycle}`;
  $('acct-count').textContent  = `${fmtNum(d.total)} accounts`;
  $('status-txt').textContent  = d.is_paused ? 'PAUSED' : 'running';
  $('status-badge').textContent = d.is_paused ? 'PAUSED' : 'RUNNING';
  $('status-badge').className   = 'status-badge' + (d.is_paused ? ' paused' : '');
  $('perf-badge').style.display = d.perf_mode ? '' : 'none';

  // progress
  $('prog-fill').style.width = pct + '%';
  $('prog-text').textContent = `${fmtNum(d.progress)} / ${fmtNum(d.total)}`;
  $('prog-pct').textContent  = pct + '%';

  // stat bar
  $('s-wh').textContent      = `Rp ${fmtNum(d.balance.wh)}`;
  $('s-yc').textContent      = d.balance.yc;
  $('s-ratio').textContent   = d.balance.ratio + '%';
  $('s-speed').textContent   = d.speed + ' / min';
  $('s-eta').textContent     = d.eta;
  $('s-workers').textContent = `${d.workers} / ${d.max_workers}`;
  $('s-next').textContent    = d.next_cycle_in > 0
    ? `${Math.floor(d.next_cycle_in/60)}m ${d.next_cycle_in%60}s`
    : '─';

  // active table
  $('active-note').textContent = `(${d.active.length} workers running)`;
  $('active-body').innerHTML = d.active
    .map((a, i) => accRow(a, i+1))
    .join('');

  // recent table
  const recent = d.recent.slice(-20);
  $('recent-note').textContent = `(${d.recent.length} accounts)`;
  $('recent-body').innerHTML = recent
    .map((a, i) => {
      const dim = ['NO_CAPTCHA','NO_CHALLENGE','IDLE'].includes(a.status);
      return accRow(a, d.active.length + i + 1, dim);
    })
    .join('');

  // breakdown
  const s = d.stats;
  $('bd-pow').textContent     = s.pow;
  $('bd-captcha').textContent = s.solved;
  $('bd-noch').textContent    = s.skip;
  $('bd-dead').textContent    = s.dead;
  $('bd-failed').textContent  = s.failed;
  $('bd-avg').textContent     = s.avg_r ? s.avg_r + 'r' : '─';
  $('bd-best').textContent    = s.best_r ? s.best_r + 'r' : '─';
  $('bd-worst').textContent   = s.worst_r ? s.worst_r + 'r' : '─';

  // summary
  const solved = s.solved + s.pow;
  const failed = s.dead + s.failed;
  const ratio  = d.balance.ratio;
  const segs   = 20;
  const filled = Math.round(solved / Math.max(s.total,1) * segs);
  let barHtml  = '';
  for (let i = 0; i < segs; i++) {
    barHtml += `<div class="mini-seg ${i < filled ? 'ok' : 'bad'}"></div>`;
  }
  $('sum-bar').innerHTML      = barHtml;
  $('sum-ratio').textContent  = ratio + '% success';
  $('sum-solved').textContent = solved;
  $('sum-failed').textContent = failed;
  $('sum-elapsed').textContent= d.elapsed;
  $('sum-cost').textContent   = `Rp ${fmtNum(s.cost)}`;
  $('sum-cap-n').textContent  = s.solved;
  $('sum-cap-total').textContent = `Rp ${fmtNum(s.cost_captcha)}`;
  $('sum-pow-n').textContent  = s.pow;
  $('sum-pow-total').textContent = `Rp ${fmtNum(s.cost_pow)}`;

  // live feed — only update if new logs
  if (d.logs.length !== lastLogCount) {
    lastLogCount = d.logs.length;
    const feedHtml = d.logs.slice(-60).map(e =>
      `<div class="log-line">
        <span class="log-ts">${e.ts}</span>
        <span class="log-msg ${LOG_CLS[e.level] || 'info'}">${e.msg}</span>
      </div>`
    ).join('');
    $('feed-body').innerHTML = feedHtml;
    if (feedAtBottom) {
      feedWrap.scrollTop = feedWrap.scrollHeight;
    }
  }

  // cycle history
  $('history-body').innerHTML = d.history.map(c => {
    const run    = c.running;
    const pc     = run ? 'var(--cyan)' : 'var(--green)';
    const stBadge= run
      ? '<span class="run-badge">● run</span>'
      : '<span class="done-badge">✓ done</span>';
    return `<tr>
      <td style="color:var(--white);font-weight:600;">Cycle ${c.n}</td>
      <td style="color:${pc}">${fmtNum(c.done)} / ${fmtNum(c.total)}</td>
      <td style="color:${pc};font-weight:700;">${c.ratio}%</td>
      <td style="color:${run?'var(--yellow)':'var(--dim)'}">${c.time}</td>
      <td style="color:var(--cyan)">Rp ${fmtNum(c.cost)}</td>
      <td style="color:var(--red)">${c.dead}d / ${c.failed}f</td>
      <td>${stBadge}</td>
    </tr>`;
  }).join('');

  // mini summaries
  $('mini-summaries').innerHTML = d.history.map(c => {
    const pct_c = Math.round(c.solved / Math.max(c.done,1) * 100);
    const run   = c.running;
    const col   = run ? 'var(--cyan)' : 'var(--green)';
    return `<div class="mini-sum ${run?'running':'done'}">
      <div class="mini-sum-head">
        <span style="color:var(--white);font-weight:700;">Cycle ${c.n}</span>
        <span style="color:${col};font-size:10px;font-weight:700;">${run?'● running':'✓ complete'}</span>
      </div>
      <div class="mini-sum-body">
        ${miniBar(c.solved, c.done)}
        <span style="color:${col};font-weight:700;">${pct_c}%</span>
        <span style="color:var(--dim)">|</span>
        <span style="color:var(--green)">✓</span>
        <span style="color:var(--white);font-weight:700;">${fmtNum(c.solved)}</span>
        <span style="color:var(--red)">✗</span>
        <span style="color:var(--white);font-weight:700;">${fmtNum(c.done - c.solved)}</span>
        <span style="color:var(--dim)">|</span>
        <span style="color:var(--dim)">cost</span>
        <span style="color:var(--cyan);font-weight:700;">Rp ${fmtNum(c.cost)}</span>
        <span style="color:var(--dim)">|</span>
        <span style="color:${run?'var(--yellow)':'var(--dim)'}">${c.time}</span>
      </div>
    </div>`;
  }).join('');

  // clock
  $('footer-clock').textContent = new Date().toTimeString().slice(0,8);
}

// ── SSE ───────────────────────────────────────────────────────────────────
const conn = $('conn');
let es;

function connect() {
  es = new EventSource('/events');
  es.onopen    = () => { conn.className = 'conn-indicator'; };
  es.onmessage = e  => { update(JSON.parse(e.data)); };
  es.onerror   = ()  => {
    conn.className = 'conn-indicator disconnected';
    es.close();
    setTimeout(connect, 2000);
  };
}

connect();

// ── KEYBOARD CONTROLS ──────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const key = e.key.toLowerCase();
  if (key === 'q') fetch('/cmd/quit',   {method:'POST'});
  if (key === 'p') fetch('/cmd/pause',  {method:'POST'});
  if (key === 'r') fetch('/cmd/refresh',{method:'POST'});
  if (key === 'm') fetch('/cmd/perf',   {method:'POST'});
});

// clock tick
setInterval(() => {
  $('footer-clock').textContent = new Date().toTimeString().slice(0,8);
}, 1000);
</script>
</body>
</html>"""

# ── WEB SERVER ────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")

async def handle_events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={
        "Content-Type":                "text/event-stream",
        "Cache-Control":               "no-cache",
        "X-Accel-Buffering":           "no",
        "Access-Control-Allow-Origin": "*",
    })
    await resp.prepare(request)
    with _sse_lock:
        _sse_clients.append(resp)
    try:
        # send initial state immediately
        state = request.app["state"]
        cfg   = request.app["cfg"]
        data  = "data: " + json.dumps(state.to_json()) + "\n\n"
        await resp.write(data.encode())
        # keep alive
        while not request.transport.is_closing():
            await asyncio.sleep(30)
            await resp.write(b": keepalive\n\n")
    except Exception:
        pass
    finally:
        with _sse_lock:
            if resp in _sse_clients:
                _sse_clients.remove(resp)
    return resp

async def handle_cmd(request: web.Request) -> web.Response:
    state  = request.app["state"]
    cfg    = request.app["cfg"]
    http   = request.app["http"]
    cmd    = request.match_info["cmd"]

    if cmd == "quit":
        state.should_quit = True
    elif cmd == "pause":
        state.is_paused = not state.is_paused
        log("paused" if state.is_paused else "resumed", "warn")
    elif cmd == "refresh":
        state.balance = await fetch_balance(http, cfg["winter_api_key"])
        log("balance refreshed", "info")
    elif cmd == "perf":
        state.perf_mode = not state.perf_mode
        log(f"performance mode {'ON' if state.perf_mode else 'OFF'}", "warn")

    await broadcast_state(state)
    return web.Response(text="ok")

async def handle_state(request: web.Request) -> web.Response:
    state = request.app["state"]
    return web.Response(
        text=json.dumps(state.to_json()),
        content_type="application/json",
    )

async def start_web_server(state: State, cfg: dict, http: aiohttp.ClientSession, port: int) -> None:
    app = web.Application()
    app["state"] = state
    app["cfg"]   = cfg
    app["http"]  = http
    app.router.add_get("/",            handle_index)
    app.router.add_get("/events",      handle_events)
    app.router.add_get("/state",       handle_state)
    app.router.add_post("/cmd/{cmd}",  handle_cmd)

    runner = web.AppRunner(app)
    await runner.setup()
    site   = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"\n  ✓ Dashboard: http://localhost:{port}\n")

# ── STARTUP ───────────────────────────────────────────────────────────────────

def print_check(label: str, result: str, note: str = "") -> None:
    w    = 32
    dots = "." * (w - len(label))
    col  = "\033[92m" if result == "OK" else "\033[91m" if result == "FAIL" else "\033[93m"
    line = f"  {label}{dots} {col}{result}\033[0m"
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
        print(f"  \033[91m✗\033[0m no valid accounts found")
        return False
    print_check("accounts.txt", "OK", f"{len(accounts):,} accounts")

    bat = get_battery()
    if bat is not None and bat < cfg.get("min_battery", 15):
        print_check("battery", "FAIL", f"{bat}%")
        return False
    print_check("battery", "OK", f"{bat}%" if bat else "unknown")

    bal = await fetch_balance(http, cfg["winter_api_key"])
    if not bal.get("success"):
        print_check("WinterHub API", "FAIL")
        print(f"  \033[91m✗\033[0m {bal.get('error') or bal.get('status', '?')}")
        return False
    wh_bal = bal["data"]["winterhub"]["balance"]
    print_check("WinterHub API", "OK")
    print_check("WH balance", "OK", f"Rp {wh_bal:,}")

    print(f"\n  \033[92mall checks passed.\033[0m\n")
    return True

async def check_update(http: aiohttp.ClientSession) -> bool:
    try:
        async with http.get(GITHUB_RAW, timeout=aiohttp.ClientTimeout(total=10)) as r:
            remote = await r.text()
            if f'VERSION    = "{VERSION}"' not in remote and f'VERSION = "{VERSION}"' not in remote:
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
        # 1. format check
        if not is_valid_cookie(acc["cookie"]):
            acc["status"] = "INVALID_COOKIES"
            acc["errors"] += 1
            acc["consecutive_fails"] += 1
            log(f"{acc['username']}  →  ✗ invalid cookie format", "error")
            _append_failed(acc)
            return

        # 2. gateway check (cached)
        now        = time.time()
        cache_secs = cfg.get("gateway_cache_minutes", 30) * 60
        if not acc["gateway_checked"] or (now - acc["gateway_ts"] > cache_secs):
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
                log(f"{acc['username']}  →  ✗ DEAD  (gateway rejected)", "error")
                _append_dead(acc)
                return

        # 3. delay — skip in perf mode
        if not state.perf_mode:
            await asyncio.sleep(cfg["delay_per_cookie"])

        # 4. solve
        acc["status"] = "PENDING"
        t0 = time.time()
        async with cap_sem:
            result = await do_solve(
                http, cfg["winter_api_key"], acc["cookie"],
                cfg["place_id"], cfg["solver_retries"], cfg["solver_retry_delay"],
            )
        acc["last_solve_time"] = f"{time.time()-t0:.1f}s"

        status        = result.get("status", "SOLVER_ERROR")
        acc["status"] = status
        acc["rounds"] = result.get("rounds")

        # auto-scale workers
        if status == BUSY_STATUS:
            state.busy_streak += 1
            if state.busy_streak >= 3 and state.workers > 1 and not state.perf_mode:
                state.workers -= 1
                log(f"SERVER_BUSY streak  ·  reducing workers to {state.workers}", "warn")
        else:
            if state.busy_streak > 0:
                state.busy_streak = max(0, state.busy_streak - 1)
                if state.workers < cfg["max_workers"]:
                    state.workers += 1

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
            log(f"{acc['username']}  →  ✦ {status}{rnd}  ·  {acc['last_solve_time']}", "ok")
        else:
            acc["errors"] += 1
            acc["consecutive_fails"] += 1
            state.cycle_stats["failed"] += 1
            log(f"{acc['username']}  →  ✗ {status}", "error")
            if status == "INVALID_COOKIES":
                _append_failed(acc)

        state.cycle_stats["total"] += 1

        if acc["consecutive_fails"] >= cfg.get("max_consecutive_fails", 3):
            _append_dead(acc)

    finally:
        if acc in state.active_accs:
            state.active_accs.remove(acc)
        state.recent_accs.append(acc)
        if len(state.recent_accs) > 30:
            state.recent_accs.pop(0)
        state.progress += 1
        await broadcast_state(state)

def _append_failed(acc: dict) -> None:
    with open(FAILED_PATH, "a") as f:
        f.write(f"{acc['username']}:{acc['cookie']}\n")

def _append_dead(acc: dict) -> None:
    date = datetime.now().strftime("%Y-%m-%d")
    with open(DEAD_PATH, "a") as f:
        f.write(f"{acc['username']}:{date}\n")
    log(f"{acc['username']}  →  marked dead", "warn")

# ── RUN CYCLE ─────────────────────────────────────────────────────────────────

async def run_cycle(
    accounts: list[dict],
    cfg:      dict,
    state:    State,
    http:     aiohttp.ClientSession,
) -> None:
    state.progress    = 0
    state.total       = len(accounts)
    state.cycle_start = time.time()
    state.cycle_stats = State._empty_stats()

    random.shuffle(accounts)
    open_log(state.cycle)
    log(f"Cycle {state.cycle} started  ·  {len(accounts):,} accounts  ·  {state.workers} workers  ·  shuffled", "info")
    if state.perf_mode:
        log("⚡ Performance mode ON  ·  grabbing all worker slots simultaneously", "warn")

    # performance mode: use much higher worker count to flood slots
    workers   = 20 if state.perf_mode else state.workers
    cap_sem   = asyncio.Semaphore(workers)
    gate_sem  = asyncio.Semaphore(cfg.get("max_gateway_workers", 10))
    t_start   = time.time()
    tasks:    list[asyncio.Task] = []

    for acc in accounts:
        if state.should_quit:
            break
        while state.is_paused:
            await asyncio.sleep(0.5)

        tasks.append(asyncio.create_task(
            process_account(acc, cfg, http, state, cap_sem, gate_sem)
        ))

        # in normal mode, small delay; perf mode fires all at once
        if not state.perf_mode:
            if len(tasks) % 5 == 0 and state.progress > 0:
                elapsed       = time.time() - t_start
                state.speed   = state.progress / (elapsed / 60)
                remaining     = len(accounts) - state.progress
                eta           = remaining / max(state.speed / 60, 0.001)
                m, s          = divmod(int(eta), 60)
                state.eta_str = f"{m}m {s}s"
                await broadcast_state(state)

    await asyncio.gather(*tasks, return_exceptions=True)

    cs = state.cycle_stats
    log(
        f"Cycle {state.cycle} complete  ·  "
        f"✓{cs['solved']+cs['pow']}  ✗{cs['dead']+cs['failed']}  ·  "
        f"{state.elapsed_str()}  ·  Rp {cs['cost']:,}",
        "ok",
    )
    close_log()

# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once",    action="store_true")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--port",    type=int, default=8080)
    parser.add_argument("--perf",    action="store_true", help="start in performance mode")
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

    port = args.port or cfg.get("web_port", 8080)

    async with aiohttp.ClientSession() as http:
        if not await run_startup_checks(cfg, http):
            sys.exit(1)

        if await check_update(http):
            print("\033[93m[!] script updated. please rerun.\033[0m")
            sys.exit(0)

        state             = State()
        state.workers     = cfg["max_workers"]
        state.max_workers = cfg["max_workers"]
        state.perf_mode = args.perf or cfg.get("performance_mode", False)
        state.balance   = await fetch_balance(http, cfg["winter_api_key"])

        prev = load_session()
        if prev:
            ans = input(
                f"\n  previous session found (cycle {prev.get('cycle',1)})  resume? [Y/n]: "
            ).strip().lower()
            if ans in ("", "y"):
                state.cycle = prev.get("cycle", 1)

        accounts = load_accounts(cfg)

        # start web server
        await start_web_server(state, cfg, http, port)
        print(f"  Open Chrome and go to: http://localhost:{port}")
        print(f"  Press Ctrl+C to stop\n")

        # main solve loop
        try:
            while not state.should_quit:
                try:
                    cfg = load_config()
                    if args.workers:
                        cfg["max_workers"] = args.workers
                except Exception:
                    pass

                bat = get_battery()
                if bat is not None and bat < cfg.get("min_battery", 15):
                    log(f"battery low ({bat}%)  ·  pausing", "warn")
                    notify("WinterHub Solver", f"Battery low ({bat}%) — paused")
                    state.is_paused = True

                state.balance = await fetch_balance(http, cfg["winter_api_key"])
                min_bal       = cfg.get("min_balance_warning", 0)
                if min_bal > 0 and state.wh_balance() < min_bal:
                    log(f"balance low: Rp {state.wh_balance():,}  ·  pausing", "warn")
                    notify("WinterHub Solver", f"Balance low: Rp {state.wh_balance():,}")
                    state.is_paused = True

                save_session({"cycle": state.cycle, "total": len(accounts)})
                await broadcast_state(state)

                await run_cycle(accounts, cfg, state, http)

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

                await broadcast_state(state)

                if cfg.get("discord_webhook"):
                    await send_discord(http, cfg["discord_webhook"], build_discord_msg(state))

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
                log(f"next cycle in {cfg['delay_minutes']} minutes", "info")
                for remaining in range(delay, 0, -1):
                    if state.should_quit:
                        break
                    state.next_cycle_in = remaining
                    await broadcast_state(state)
                    await asyncio.sleep(1)
                state.next_cycle_in = 0

        except KeyboardInterrupt:
            pass

        print("\n  [!] solver stopped.\n")

if __name__ == "__main__":
    asyncio.run(main())
