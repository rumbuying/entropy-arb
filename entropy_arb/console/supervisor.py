"""Engine worker lifecycle: spawn, watch, stop, restart.

Workers are child processes running main.py exactly as an operator would
(--config profile --symbol S --hedge V [--record-only] --web PORT). The
console adds nothing the CLI cannot do — it only remembers and automates.

Each worker gets its own loopback port for its embedded read-only state
server; the console polls/proxies that for live views and bridges websockets.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from collections import deque
from typing import Dict, Optional

import aiohttp

log = logging.getLogger("supervisor")

STOP_GRACE_SEC = 25.0   # engine settles in-flight orders + reconciles on TERM
LOG_TAIL = 500


def parse_worker_cmdline(argv, profiles_dir: str) -> Optional[dict]:
    """Recognize one of our engine workers in a process cmdline.

    Matches ``<python> main.py --config <profiles_dir>/<name>.yaml --symbol S
    --hedge V [--web P] [--record-only] ...`` and returns
    {profile, symbol, hedge, web_port, mode}, or None when the process is not
    one of ours. Used to adopt workers that survived a console restart."""
    try:
        argv = [str(a) for a in argv]
    except Exception:
        return None
    if "main.py" not in argv or "--config" not in argv:
        return None
    i = argv.index("--config")
    if i + 1 >= len(argv):
        return None
    cfg = os.path.abspath(argv[i + 1])
    want = os.path.abspath(os.path.join(profiles_dir, ""))
    if os.path.dirname(cfg) != want or not os.path.basename(cfg).endswith(".yaml"):
        return None
    profile = os.path.basename(cfg)[:-5]

    def opt(flag):
        return argv[argv.index(flag) + 1] if flag in argv \
            and argv.index(flag) + 1 < len(argv) else None

    symbol = opt("--symbol")
    hedge = opt("--hedge")
    if not symbol or not hedge:
        return None
    web_port = opt("--web")
    try:
        web_port = int(web_port) if web_port else 0
    except ValueError:
        web_port = 0
    return {"profile": profile, "symbol": symbol.upper(), "hedge": hedge,
            "base": opt("--base") or "hl", "web_port": web_port,
            "mode": "record" if "--record-only" in argv else "live"}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # alive but not ours to signal
    except OSError:
        return False


def _proc_start_time(pid: int) -> Optional[float]:
    """Epoch time the process started (from /proc), or None."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # field 22 (starttime in clock ticks); comm may contain spaces,
            # so split after the closing parenthesis
            after_comm = fh.read().split(")", 1)[1].split()
        ticks = float(after_comm[19])
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        return time.time() - uptime + ticks / hz
    except Exception:
        return None


class Worker:
    def __init__(self, wid: str, profile: str, symbol: str, hedge: str,
                 mode: str, web_port: int, base: str = "hl") -> None:
        self.id = wid
        self.profile = profile
        self.symbol = symbol
        self.hedge = hedge
        self.base = base              # base (entropy) leg venue, default hl
        self.mode = mode              # "live" | "record"
        self.web_port = web_port
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.adopted_pid: Optional[int] = None   # set for adopted workers
        self.started_ts = 0.0
        self.exit_code: Optional[int] = None
        self.stopped_ts: Optional[float] = None
        self.restarts = 0
        self.log_tail: deque = deque(maxlen=LOG_TAIL)
        self._reader_task: Optional[asyncio.Task] = None

    @property
    def pid(self) -> Optional[int]:
        if self.proc is not None:
            return self.proc.pid
        return self.adopted_pid

    @property
    def running(self) -> bool:
        if self.adopted_pid is not None:
            return pid_alive(self.adopted_pid)
        return self.proc is not None and self.proc.returncode is None

    def ui_state(self) -> str:
        if self.running:
            return "running"
        if self.exit_code is None:
            return "stopped"
        if self.exit_code == 0 or self.exit_code == -15 or \
                self.exit_code == -2:
            return "stopped"          # graceful TERM / INT
        return "errored"


class Supervisor:
    def __init__(self, project_root: str, profiles_dir: str,
                 port_range=(8801, 8899)) -> None:
        self.root = project_root
        self.profiles_dir = profiles_dir
        self.port_lo, self.port_hi = port_range
        self.workers: Dict[str, Worker] = {}
        self._seq = 0
        self._reserved: set = set()   # ports handed out this session
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------ ports

    def _alloc_port(self) -> int:
        used = {w.web_port for w in self.workers.values() if w.running}
        used |= self._reserved
        for p in range(self.port_lo, self.port_hi + 1):
            if p in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", p))
                    self._reserved.add(p)
                    return p
                except OSError:
                    continue
        raise RuntimeError("no free worker port in "
                           f"{self.port_lo}..{self.port_hi}")

    # ---------------------------------------------------------------- command

    def build_argv(self, w: Worker) -> list:
        cfg_path = os.path.join(self.profiles_dir, f"{w.profile}.yaml")
        argv = [sys.executable, "main.py",
                "--config", cfg_path,
                "--symbol", w.symbol, "--hedge", w.hedge]
        if w.base and w.base != "hl":
            argv += ["--base", w.base]
        argv += ["--web", str(w.web_port), "--no-dashboard",
                 # engine logs go to the profile's file; this mirrors them
                 # onto stdout so the console can tail them
                 "--log-stdout"]
        if w.mode == "record":
            argv.append("--record-only")
        return argv

    # ------------------------------------------------------------------ start

    def adopt_running(self) -> int:
        """Re-adopt engine workers that survived a console restart.

        The supervisor's registry is in-memory by design; without adoption a
        console restart would orphan live engines the UI can no longer see or
        stop. Scans /proc for our exact spawn shape and reconstructs worker
        records (with their profile log file tailed into the view)."""
        import glob
        adopted = 0
        known_pids = {w.pid for w in self.workers.values() if w.pid}
        for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                with open(cmdline_path, "rb") as fh:
                    raw = fh.read()
                argv = [a.decode(errors="replace")
                        for a in raw.split(b"\x00") if a]
            except (OSError, PermissionError):
                continue
            info = parse_worker_cmdline(argv, self.profiles_dir)
            if info is None:
                continue
            pid = int(cmdline_path.split("/")[2])
            if pid == os.getpid() or pid in known_pids:
                continue
            known_pids.add(pid)
            self._seq += 1
            w = Worker(f"w{self._seq}", info["profile"], info["symbol"],
                       info["hedge"], info["mode"], info["web_port"],
                       base=info.get("base", "hl"))
            w.adopted_pid = pid
            w.started_ts = _proc_start_time(pid) or time.time()
            self.workers[w.id] = w
            self._seed_log_tail(w)
            adopted += 1
            log.info("adopted worker %s: pid=%d %s/%s (%s)", w.id, pid,
                     w.symbol, w.hedge, w.mode)
        if adopted:
            log.info("adopted %d running worker(s) from a previous console",
                     adopted)
        return adopted

    def _seed_log_tail(self, w: Worker) -> None:
        """Pre-fill the view with the profile's own engine log (an adopted
        worker's stdout belongs to the previous console, not us)."""
        try:
            import yaml as _yaml
            with open(os.path.join(self.profiles_dir,
                                   f"{w.profile}.yaml")) as fh:
                log_file = (_yaml.safe_load(fh) or {}).get("logging", {}) \
                    .get("file")
            if not log_file or not os.path.exists(log_file):
                return
            with open(log_file, errors="replace") as fh:
                lines = fh.readlines()[-LOG_TAIL:]
            for ln in lines:
                w.log_tail.append(ln.rstrip())
        except Exception as e:
            log.debug("log seed failed for %s: %r", w.id, e)

    async def start(self, profile: str, symbol: str, hedge: str,
                    mode: str, base: str = "hl") -> Worker:
        if mode not in ("live", "record"):
            raise ValueError(f"bad mode {mode!r}")
        self._seq += 1
        w = Worker(f"w{self._seq}", profile, symbol, hedge, mode,
                   self._alloc_port(), base=base)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        w.proc = await asyncio.create_subprocess_exec(
            *self.build_argv(w),
            cwd=self.root, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        w.started_ts = time.time()
        w.exit_code = None
        w.stopped_ts = None
        w.log_tail.clear()
        self.workers[w.id] = w
        w._reader_task = asyncio.create_task(self._pump(w),
                                             name=f"worker-{w.id}-log")
        log.info("started worker %s: %s", w.id, " ".join(self.build_argv(w)))
        return w

    async def _pump(self, w: Worker) -> None:
        proc = w.proc
        try:
            while proc.returncode is None:
                line = await proc.stdout.readline()
                if not line:
                    break
                w.log_tail.append(line.decode(errors="replace").rstrip())
        except Exception:
            log.exception("log pump failed for %s", w.id)
        # EOF can beat the SIGCHLD notification: returncode may still be
        # None here even though the process is gone
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
        w.exit_code = proc.returncode
        w.stopped_ts = time.time()
        log.info("worker %s exited with %s", w.id,
                 w.exit_code if w.exit_code is not None else "signal")

    # ------------------------------------------------------- stop / restart

    async def stop(self, wid: str, grace: float = STOP_GRACE_SEC) -> bool:
        w = self.workers.get(wid)
        if not w or not w.running:
            return False
        if w.adopted_pid is not None:
            return await self._stop_adopted(w, grace)
        try:
            w.proc.terminate()          # SIGTERM: engine settles gracefully
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(w.proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            log.warning("worker %s did not exit in %.0fs — killing",
                        wid, grace)
            try:
                w.proc.kill()
            except ProcessLookupError:
                pass
            await w.proc.wait()
        # let the log pump observe EOF and record the exit code
        if w._reader_task:
            try:
                await asyncio.wait_for(w._reader_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if w.exit_code is None:
            w.exit_code = w.proc.returncode   # authoritative after wait()
        return True

    async def _stop_adopted(self, w: Worker, grace: float) -> bool:
        """SIGTERM a worker we did not spawn, poll for exit, then SIGKILL."""
        import signal
        pid = w.adopted_pid
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + grace
        while time.time() < deadline and pid_alive(pid):
            await asyncio.sleep(0.25)
        if pid_alive(pid):
            log.warning("adopted worker %s did not exit in %.0fs — killing",
                        w.id, grace)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.sleep(0.5)
        w.exit_code = -15                   # graceful TERM
        w.stopped_ts = time.time()
        w.adopted_pid = None
        w.log_tail.append(f"[console] adopted worker stopped (SIGTERM)")
        log.info("adopted worker %s stopped", w.id)
        return True

    async def restart(self, wid: str) -> Worker:
        w = self.workers.get(wid)
        if not w:
            raise KeyError(wid)
        await self.stop(wid)
        w2 = await self.start(w.profile, w.symbol, w.hedge, w.mode,
                              base=w.base)
        w2.restarts = w.restarts + 1
        return w2

    # ------------------------------------------------------------------ views

    def status(self, wid: str) -> dict:
        w = self.workers[wid]
        return {
            "id": w.id, "profile": w.profile, "symbol": w.symbol,
            "hedge": w.hedge, "base": w.base, "mode": w.mode,
            "web_port": w.web_port,
            "state": w.ui_state(),
            "uptime_sec": ((time.time() - w.started_ts) if w.running else None),
            "exit_code": w.exit_code,
            "restarts": w.restarts,
            "adopted": w.adopted_pid is not None,
        }

    def list(self) -> list:
        return [self.status(wid) for wid in self.workers]

    def logs(self, wid: str, tail: int = 100) -> list:
        w = self.workers[wid]
        return list(w.log_tail)[-tail:]

    # --------------------------------------------------------------- snapshot

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3.0))
        return self._session

    async def snapshot(self, wid: str) -> Optional[dict]:
        """Live snapshot from the worker's embedded server, or None."""
        w = self.workers.get(wid)
        if not w or not w.running:
            return None
        try:
            http = await self._http()
            async with http.get(
                    f"http://127.0.0.1:{w.web_port}/api/state") as r:
                if r.status != 200:
                    return None
                return await r.json()
        except Exception:
            return None

    # --------------------------------------------------------------- shutdown

    async def shutdown(self) -> None:
        running = [wid for wid, w in self.workers.items() if w.running]
        if running:
            log.info("stopping %d worker(s)", len(running))
            await asyncio.gather(*(self.stop(wid) for wid in running))
        if self._session and not self._session.closed:
            await self._session.close()
