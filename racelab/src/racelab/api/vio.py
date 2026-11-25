"""Local-only experiment process management; independent of the CU and its safety loop."""
from __future__ import annotations

import asyncio
import ipaddress
import hashlib
import json
import os
import shlex
import socket
import math
import shutil
import sys
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import asyncssh
from aiohttp import web


def loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


class Vio:
    def __init__(self):
        self.ssh = None
        self.host = ""
        self.remote = None
        self.local = None
        self.remote_task = None
        self.local_task = None
        self.logs = {"pi": deque(maxlen=150), "receiver": deque(maxlen=150)}
        self.dataset_root = None
        self.active_dataset = None
        self.receiver_file = ""
        self.local_rc = None
        self.remote_rc = None
        self.lock = asyncio.Lock()
        self.media_lock = asyncio.Lock()
        self.cache = tempfile.TemporaryDirectory(prefix="racelab-vio-")
        self.live_processes = set()
        self.local_stopping = False
        self.preview_port = 0

    def connected(self):
        return self.ssh is not None and not self.ssh.is_closed()

    def remote_running(self):
        return self.remote_task is not None and not self.remote_task.done()

    def local_running(self):
        return self.local is not None and self.local.returncode is None

    def datasets(self):
        root = self.dataset_root
        if not root or not root.is_dir():
            return []
        result = []
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            if self.find_video(directory) is not None:
                result.append(directory.name)
        return sorted(result, reverse=True)

    @staticmethod
    def find_video(directory):
        # The receiver renames video.mkv on shutdown and records its name in summary.json.
        candidates = []
        try:
            name = json.loads((directory / "summary.json").read_text(encoding="utf-8")).get("video_file")
            if isinstance(name, str) and Path(name).name == name and name.endswith(".mkv"):
                candidates.append(directory / name)
        except (OSError, ValueError, AttributeError):
            pass
        candidates.append(directory / "video.mkv")
        candidates.extend(sorted(directory.glob("video_*.mkv")))
        return next((p for p in candidates if p.is_file() and not p.is_symlink()), None)

    def receiver_config(self, data):
        script = Path(str(data.get("file", ""))).expanduser().resolve()
        if not script.is_file() or script.suffix.lower() != ".py":
            raise ValueError("Select an existing Python receiver file")
        args = shlex.split(str(data.get("args", "")))
        if any(a.startswith("--carrera") for a in args):
            raise ValueError("RaceLab already owns the CU link; omit receiver Carrera options")
        output = "dataset"
        for i, arg in enumerate(args):
            if arg == "--output" and i + 1 < len(args):
                output = args[i + 1]
            elif arg.startswith("--output="):
                output = arg.split("=", 1)[1]
        self.dataset_root = (script.parent / Path(output).expanduser()).resolve()
        self.receiver_file = str(script)
        return script, args

    def state(self):
        return {
            "connected": self.connected(), "host": self.host,
            "pi_running": self.remote_running(), "receiver_running": self.local_running(),
            "pi_exit": self.remote_rc, "receiver_exit": self.local_rc,
            "logs": {key: list(value) for key, value in self.logs.items()},
            "dataset_root": str(self.dataset_root or ""),
            "active_dataset": self.active_dataset.name if self.active_dataset else "",
            "datasets": self.datasets(), "receiver_file": self.receiver_file,
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "preview_port": self.preview_port,
        }

    async def collect_local(self, proc):
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self.logs["receiver"].append(line)
                if line.startswith("Dataset directory: "):
                    actual = Path(line.removeprefix("Dataset directory: ")).resolve()
                    self.active_dataset = actual
                    self.dataset_root = actual.parent
        finally:
            self.local_rc = await proc.wait()

    async def collect_remote(self, proc):
        try:
            async for line in proc.stdout:
                self.logs["pi"].append(line.rstrip())
            result = await proc.wait()
            self.remote_rc = result.exit_status
        except Exception as exc:
            self.logs["pi"].append(str(exc))
            self.remote_rc = -1

    async def stop_local(self):
        if not self.local_running():
            return
        self.local_stopping = True
        try:
            # Close the growing MKV before Windows attempts the receiver's rename.
            for proc in list(self.live_processes):
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            self.local.stdin.write(b"stop\n")
            await self.local.stdin.drain()
            await asyncio.wait_for(asyncio.shield(self.local_task), 15)
        except TimeoutError:
            raise ValueError("Receiver has not stopped; inspect logs before terminating it manually")
        finally:
            self.local_stopping = False

    async def stop_remote(self):
        if not self.remote_running():
            return
        self.remote.stdin.write("\x03")
        try:
            await asyncio.wait_for(asyncio.shield(self.remote_task), 15)
        except TimeoutError:
            raise ValueError("Pi-send has not stopped; inspect the remote terminal")

    async def action(self, op, data):
        async with self.lock:
            if op == "connect":
                if self.connected():
                    raise ValueError("Disconnect the current SSH connection first")
                host = str(data.get("host", "")).strip()
                user = str(data.get("user", "")).strip()
                if not host or not user:
                    raise ValueError("IP and SSH username are required")
                options = dict(username=user, port=int(data.get("port", 22)), connect_timeout=10)
                if data.get("password"):
                    options["password"] = str(data["password"])
                if data.get("key"):
                    options["client_keys"] = [str(Path(data["key"]).expanduser())]
                # Default AsyncSSH known_hosts verification is intentional.
                self.ssh = await asyncssh.connect(host, **options)
                self.host = host
            elif op == "disconnect":
                if self.remote_running():
                    raise ValueError("Stop Pi-send before disconnecting")
                if self.ssh:
                    self.ssh.close()
                    await self.ssh.wait_closed()
                self.ssh = None
            elif op == "pi-start":
                if not self.connected() or self.remote_running():
                    raise ValueError("SSH must be connected and Pi-send idle")
                directory = str(data.get("directory", "")).strip()
                command = str(data.get("command", "")).strip()
                if not directory or not command:
                    raise ValueError("Directory and command are required")
                params = data.get("params", {})
                allowed = {"video-port", "data-port", "width", "height", "fps", "mjpeg-bitrate",
                           "rtp-mtu", "lens-position", "af-log-interval", "exposure-us", "gain",
                           "imu-rate", "mag-rate", "light-rate", "imu-ready-poll-sleep", "af-mode"}
                if not isinstance(params, dict) or set(params) - allowed:
                    raise ValueError("Invalid Pi-send parameters")
                for key, value in params.items():
                    if key == "af-mode":
                        if value not in ("auto", "manual", "continuous"):
                            raise ValueError("Invalid autofocus mode")
                    elif not math.isfinite(float(value)) or float(value) < 0:
                        raise ValueError("Invalid numeric parameter: " + key)
                    command += " --" + key + " " + shlex.quote(str(value))
                path = '"$HOME"' + ("/" + shlex.quote(directory[2:]) if directory.startswith("~/") else "") if directory == "~" or directory.startswith("~/") else shlex.quote(directory)
                self.logs["pi"].clear()
                self.remote_rc = None
                self.remote = await self.ssh.create_process(
                    "cd " + path + " && exec sh -c " + shlex.quote(command),
                    term_type="xterm", encoding="utf-8", errors="replace")
                self.remote_task = asyncio.create_task(self.collect_remote(self.remote))
            elif op == "pi-stop":
                await self.stop_remote()
            elif op == "receiver-config":
                if not self.local_running():
                    self.receiver_config(data)
            elif op == "receiver-start":
                if self.local_running():
                    raise ValueError("Receiver already running")
                script, args = self.receiver_config(data)
                if any(x == "--preview-port" or x.startswith("--preview-port=") for x in args):
                    raise ValueError("RaceLab manages the preview port")
                # Reserve an available loopback port for the receiver (released before launch).
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    self.preview_port = probe.getsockname()[1]
                args += ["--preview-port", str(self.preview_port)]
                self.active_dataset = None
                self.logs["receiver"].clear()
                self.local_rc = None
                python = str(data.get("python", "")).strip() or sys.executable
                # pythonw.exe has no reliable standard streams on Windows.
                if Path(python).name.lower() == "pythonw.exe":
                    python = str(Path(python).with_name("python.exe"))
                self.local = await asyncio.create_subprocess_exec(
                    python, "-u", str(Path(__file__).with_name("vio_runner.py")), str(script), *args,
                    cwd=str(script.parent), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                self.local_task = asyncio.create_task(self.collect_local(self.local))
            elif op == "receiver-stop":
                await self.stop_local()
            elif op == "browse":
                return {"file": await asyncio.to_thread(self.browse)}
            else:
                raise ValueError("Unknown VIO operation")
        return self.state()

    @staticmethod
    def browse():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askopenfilename(title="Select laptop receiver", filetypes=[("Python", "*.py")])
        finally:
            root.destroy()

    def video(self, name):
        if name not in self.datasets():
            raise ValueError("Unknown dataset")
        return self.find_video(self.dataset_root / name)

    async def replay(self, name):
        source = self.video(name)
        if self.local_running() and self.active_dataset == source.parent:
            raise ValueError("Stop recording before replaying this dataset")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("Install FFmpeg and add it to PATH for embedded replay")
        stat = source.stat()
        key = (str(source), stat.st_mtime_ns, stat.st_size)
        async with self.media_lock:
            digest = hashlib.sha256(repr(key).encode()).hexdigest()
            target = Path(self.cache.name) / (digest + ".mp4")
            if not target.exists():
                temporary = target.with_suffix(".tmp.mp4")
                proc = await asyncio.create_subprocess_exec(
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(temporary),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, err = await proc.communicate()
                if proc.returncode:
                    temporary.unlink(missing_ok=True)
                    raise ValueError(err.decode(errors="replace")[-1000:])
                temporary.replace(target)
            return target

    async def live(self, request):
        port = self.preview_port
        proc = self.local
        if not port or not self.local_running() or self.local_stopping:
            raise ValueError("Start the receiver first")
        # GStreamer starts its TCP sink asynchronously. Wait briefly for readiness.
        for _ in range(50):
            if self.local is not proc or not self.local_running() or self.local_stopping:
                raise ValueError("Receiver stopped")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise ValueError("Receiver preview unavailable; inspect receiver logs")
        response = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=racelab",
            "Cache-Control": "no-store"})
        try:
            await response.prepare(request)
            while self.local is proc and self.local_running() and not self.local_stopping:
                if request.transport is None or request.transport.is_closing():
                    break
                try:
                    chunk = await asyncio.wait_for(reader.read(32768), 1)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                await asyncio.wait_for(response.write(chunk), 2)
        except (ConnectionError, asyncio.CancelledError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        return response

    async def close(self):
        for stop in (self.stop_remote, self.stop_local):
            try:
                await stop()
            except Exception:
                pass
        if self.ssh:
            self.ssh.close()
            await self.ssh.wait_closed()
        self.cache.cleanup()


def register_vio(app):
    vio = Vio()

    def guard(request):
        # These routes execute locally selected programs, never expose them over LAN.
        host = urlsplit("//" + request.host).hostname
        origin = request.headers.get("Origin")
        if not loopback(request.remote or "") or not loopback(host or ""):
            raise web.HTTPForbidden(text="VIO is only available on localhost")
        if origin and origin.rstrip("/") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="VIO requires same origin")

    async def state(request):
        guard(request)
        return web.json_response(vio.state())

    async def command(request):
        guard(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return web.json_response(await vio.action(request.match_info["action"], data))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def replay(request):
        guard(request)
        try:
            return web.FileResponse(await vio.replay(request.match_info["name"]),
                                    headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def live(request):
        guard(request)
        try:
            return await vio.live(request)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app.router.add_get("/api/vio/state", state)
    app.router.add_post("/api/vio/action/{action}", command)
    app.router.add_get("/api/vio/replay/{name}", replay)
    app.router.add_get("/api/vio/live", live)

    async def cleanup(_app):
        await vio.close()
    app.on_cleanup.append(cleanup)
