#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lan_transfer.py  -  局域网数据转移工具 (零依赖 / 仅 Python 标准库)

两种角色, 同一个文件:

  发送端(旧电脑 / 数据所在机器):
      python lan_transfer.py serve --root "D:\\要共享的目录" --port 8788

  接收端(新电脑 / 数据要落到的地方):
      python lan_transfer.py pull  --port 8899

接收端会在本机起一个网页界面 (默认 http://127.0.0.1:8899):
  连接发送端 -> 看到远程文件列表 -> 勾选要转移的内容 -> 选本机目标目录 -> 拉取转存

特性:
  * 零第三方依赖, 只用标准库 (无 requests / 无 Flask)
  * HTTP/1.1 + Range 断点续传, 大文件中断后可续
  * 多线程并发拉取, 实时进度 / 速度 / 剩余时间
  * 局域网 UDP 自动发现发送端, 免手输 IP
  * 按相对路径原样落盘, 或平铺到目标目录
  * 冲突策略: 跳过 / 覆盖 / 自动重命名 / 保留较新
  * 可选传后 MD5 校验
  * 可选"传输完成后删除源文件"(移动式转存), 发送端必须带 --allow-delete 才生效
"""

import argparse
import errno
import hashlib
import http.server
import json
import os
import platform
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

VERSION = "1.0.0"
APP_TAG = "lan-transfer"
DISCOVERY_PORT = 45888
DEFAULT_SOURCE_PORT = 8788
DEFAULT_UI_PORT = 8899
CHUNK = 1 << 20  # 1MB

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def human(n):
    if n is None:
        return "-"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" if u == "B" else "%.2f %s") % (n, u)
        n /= 1024.0


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def normalize_rel(rel):
    """把任意用户输入规范成 'a/b/c' 形式的安全相对路径 (不允许 ../)。"""
    rel = (rel or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def safe_join(root, rel):
    """把相对路径拼到 root 下, 并确保结果没有越界。"""
    parts = [p for p in normalize_rel(rel).split("/") if p]
    target = os.path.realpath(os.path.join(root, *parts)) if parts else os.path.realpath(root)
    root_real = os.path.realpath(root)
    if target != root_real and not target.startswith(root_real + os.sep):
        raise ValueError("路径越界: %s" % rel)
    return target


def rel_dirname(rel):
    rel = normalize_rel(rel)
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def rel_basename(rel):
    return normalize_rel(rel).rsplit("/", 1)[-1]


def is_hidden(name):
    if name.startswith("."):
        return True
    if os.name == "nt":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(name))
            return attrs != -1 and bool(attrs & 2)
        except Exception:
            return False
    return False


def list_local_roots():
    if os.name == "nt":
        roots = []
        for c in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
            p = "%s:\\" % c
            if os.path.exists(p):
                roots.append(p)
        return roots or ["C:\\"]
    return ["/"]


def hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


# --------------------------------------------------------------------------
# 发送端 (数据源)
# --------------------------------------------------------------------------

class SourceApp(object):
    def __init__(self, root, allow_delete=False, verbose=True):
        self.root = os.path.realpath(root)
        self.allow_delete = allow_delete
        self.verbose = verbose
        if not os.path.isdir(self.root):
            raise SystemExit("目录不存在: %s" % self.root)

    def api_info(self):
        return {
            "app": APP_TAG,
            "version": VERSION,
            "hostname": hostname(),
            "platform": platform.platform(),
            "root": self.root,
            "root_name": os.path.basename(self.root.rstrip("\\/")) or self.root,
            "ips": local_ips(),
            "allow_delete": self.allow_delete,
            "sep": os.sep,
        }

    def api_list(self, rel):
        target = safe_join(self.root, rel)
        if not os.path.isdir(target):
            raise ValueError("不是目录: %s" % rel)
        entries = []
        try:
            names = os.listdir(target)
        except PermissionError:
            raise ValueError("无权限读取: %s" % rel)
        for name in names:
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            isdir = os.path.isdir(full)
            sub = (normalize_rel(rel) + "/" + name).lstrip("/")
            entries.append({
                "name": name,
                "path": sub,
                "dir": isdir,
                "size": 0 if isdir else st.st_size,
                "mtime": st.st_mtime,
                "hidden": is_hidden(name),
            })
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {
            "path": normalize_rel(rel),
            "parent": rel_dirname(rel) if normalize_rel(rel) else None,
            "entries": entries,
        }

    def api_walk(self, rel, limit=300000):
        base = safe_join(self.root, rel)
        if os.path.isfile(base):
            st = os.stat(base)
            return {"files": [{"path": normalize_rel(rel), "size": st.st_size, "mtime": st.st_mtime}],
                    "truncated": False}
        files, truncated = [], False
        prefix = normalize_rel(rel)
        for dirpath, dirnames, filenames in os.walk(base):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                inner = os.path.relpath(full, base).replace(os.sep, "/")
                sub = (prefix + "/" + inner).lstrip("/") if prefix else inner
                files.append({"path": sub, "size": st.st_size, "mtime": st.st_mtime})
                if len(files) >= limit:
                    truncated = True
                    return {"files": files, "truncated": truncated}
        files.sort(key=lambda f: f["path"].lower())
        return {"files": files, "truncated": truncated}

    def api_search(self, rel, kw, limit=2000):
        base = safe_join(self.root, rel)
        kw = (kw or "").lower()
        hits, truncated = [], False
        if not kw:
            return {"hits": [], "truncated": False}
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            for name in (dirnames + filenames):
                if kw in name.lower():
                    sub = (rel_dir + "/" + name).lstrip("/")
                    full = os.path.join(dirpath, name)
                    isdir = os.path.isdir(full)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    hits.append({
                        "name": name, "path": sub, "dir": isdir,
                        "size": 0 if isdir else st.st_size, "mtime": st.st_mtime,
                        "hidden": is_hidden(name),
                    })
                    if len(hits) >= limit:
                        truncated = True
                        return {"hits": hits, "truncated": truncated}
            # 不深入隐藏/系统目录即可, 这里保持全量扫描
        hits.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"hits": hits, "truncated": truncated}

    def api_hash(self, rel):
        target = safe_join(self.root, rel)
        if not os.path.isfile(target):
            raise ValueError("不是文件: %s" % rel)
        h = hashlib.md5()
        size = 0
        with open(target, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        st = os.stat(target)
        return {"md5": h.hexdigest(), "size": size, "mtime": st.st_mtime}

    def api_delete(self, rel):
        if not self.allow_delete:
            raise PermissionError("发送端未开启删除权限 (启动时加 --allow-delete)")
        target = safe_join(self.root, rel)
        if os.path.realpath(target) == os.path.realpath(self.root):
            raise PermissionError("拒绝删除根目录")
        if os.path.isdir(target):
            os.rmdir(target)  # 只删空目录
        else:
            os.remove(target)
        return {"ok": True}


class _HandlerBase(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LanTransfer/" + VERSION

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 输出助手 -------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, text, code=200, ctype="text/html; charset=utf-8"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json_body(self):
        """只读一次; 重复调用返回缓存 (避免 keep-alive 下二次读阻塞)。"""
        if getattr(self, "_body_read", False):
            return getattr(self, "_body_cache", {})
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self._body_cache = {}
            return self._body_cache
        raw = self.rfile.read(n)
        try:
            self._body_cache = json.loads(raw.decode("utf-8"))
        except Exception:
            self._body_cache = {}
        return self._body_cache

    def handle_one_request(self):
        try:
            return http.server.BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def handle(self):
        try:
            return http.server.BaseHTTPRequestHandler.handle(self)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True


class SourceHandler(_HandlerBase):
    """发送端 HTTP 处理器: /api/info /api/list /api/walk /api/search /api/download /api/hash /api/delete"""

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def _auth_ok(self, qs):
        token = getattr(self.server, "token", "") or ""
        if not token:
            return True
        given = (qs.get("token", [""])[0] or self.headers.get("X-Token", "") or "")
        return given == token

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        app = self.server.app

        if not self._auth_ok(qs):
            self.read_json_body()
            return self.send_json({"error": "unauthorized", "message": "访问令牌错误"}, 401)

        try:
            if route == "/api/info":
                return self.send_json(app.api_info())
            if route == "/api/list":
                return self.send_json(app.api_list(qs.get("path", [""])[0]))
            if route == "/api/walk":
                return self.send_json(app.api_walk(qs.get("path", [""])[0]))
            if route == "/api/search":
                return self.send_json(app.api_search(qs.get("path", [""])[0], qs.get("q", [""])[0]))
            if route == "/api/hash":
                return self.send_json(app.api_hash(qs.get("path", [""])[0]))
            if route == "/api/download":
                return self._download(qs, method)
            if route == "/api/delete" and method == "POST":
                body = self.read_json_body()
                return self.send_json(app.api_delete(body.get("path", "")))
            return self.send_json({"error": "not_found", "message": "未知接口"}, 404)
        except PermissionError as e:
            self.read_json_body()
            return self.send_json({"error": "forbidden", "message": str(e)}, 403)
        except ValueError as e:
            self.read_json_body()
            return self.send_json({"error": "bad_request", "message": str(e)}, 400)
        except Exception as e:  # noqa
            self.read_json_body()
            return self.send_json({"error": "server_error", "message": repr(e)}, 500)

    def _download(self, qs, method):
        app = self.server.app
        rel = qs.get("path", [""])[0]
        target = safe_join(app.root, rel)
        if not os.path.isfile(target):
            return self.send_json({"error": "not_found", "message": "文件不存在: %s" % rel}, 404)

        size = os.path.getsize(target)
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":
                    if b:
                        start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200

        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("X-File-Size", str(size))
        self.send_header("X-File-Mtime", "%.0f" % os.path.getmtime(target))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % urllib.parse.quote(rel_basename(rel)))
        self.end_headers()
        if method == "HEAD":
            return

        remaining = length
        try:
            with open(target, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except OSError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                raise
            self.close_connection = True


class ThreadingHTTPServerReuse(http.server.ThreadingHTTPServer):
    """
    Windows 下 SO_REUSEADDR 会让第二个进程也能绑上同一个端口 (静默抢占/串台),
    所以只在类 Unix 上启用, Windows 让它老老实实报"端口被占用"。
    """
    daemon_threads = True
    allow_reuse_address = (os.name != "nt")
    request_queue_size = 128

    def server_bind(self):
        if os.name == "nt":
            try:
                # 独占绑定: 防止别的进程偷偷共用同一端口
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        http.server.ThreadingHTTPServer.server_bind(self)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        http.server.ThreadingHTTPServer.handle_error(self, request, client_address)


def _make_server(addr, handler, what):
    try:
        return ThreadingHTTPServerReuse(addr, handler)
    except OSError as e:
        raise SystemExit(
            "\n[错误] %s 无法监听 %s:%d —— %s\n"
            "提示: 换个端口 (--port), 或先结束占用该端口的程序。\n"
            "      Windows 查看占用: netstat -ano | findstr %d\n" % (what, addr[0], addr[1], e, addr[1]))


def build_source_server(root, port, token="", allow_delete=False, verbose=True, bind="0.0.0.0"):
    app = SourceApp(root, allow_delete=allow_delete, verbose=verbose)
    srv = _make_server((bind, port), SourceHandler, "发送端")
    srv.app = app
    srv.token = token
    srv.verbose = verbose
    return srv


def start_beacon(port, root_name, udp_port=DISCOVERY_PORT, stop_event=None):
    """局域网广播, 让接收端自动发现发送端。"""
    stop_event = stop_event or threading.Event()

    def loop():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = json.dumps({
            "app": APP_TAG, "version": VERSION, "port": port,
            "host": hostname(), "root": root_name, "ips": local_ips(),
        }, ensure_ascii=False).encode("utf-8")
        while not stop_event.is_set():
            for dest in ("255.255.255.255", "<broadcast>"):
                try:
                    sock.sendto(payload, (dest, udp_port))
                except OSError:
                    pass
            stop_event.wait(2.0)
        sock.close()

    t = threading.Thread(target=loop, name="beacon", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# 接收端 (新电脑): 本地网页 UI + 拉取引擎
# --------------------------------------------------------------------------

class Remote(object):
    def __init__(self):
        self.host = ""
        self.port = DEFAULT_SOURCE_PORT
        self.token = ""
        self.info = None
        self.lock = threading.Lock()

    def base(self):
        return "http://%s:%d" % (self.host, self.port)

    def url(self, path, params=None):
        params = dict(params or {})
        if self.token:
            params["token"] = self.token
        q = urllib.parse.urlencode(params)
        return self.base() + path + ("?" + q if q else "")

    def call(self, path, params=None, timeout=30):
        req = urllib.request.Request(self.url(path, params))
        if self.token:
            req.add_header("X-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                msg = ""
            raise RuntimeError("发送端返回 %s %s" % (e.code, msg))
        except urllib.error.URLError as e:
            raise RuntimeError("无法连接发送端 %s:%s (%s)" % (self.host, self.port, e.reason))

    def post(self, path, payload, timeout=30):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data,
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("X-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                msg = ""
            raise RuntimeError("发送端返回 %s %s" % (e.code, msg))

    def connect(self):
        info = self.call("/api/info", timeout=6)
        if info.get("app") != APP_TAG:
            raise RuntimeError("目标不是本工具的发送端")
        with self.lock:
            self.info = info
        return info


class TransferFile(object):
    __slots__ = ("rel", "dest_rel", "size", "mtime", "done", "status", "dest", "err", "md5")

    def __init__(self, rel, dest_rel, size, mtime):
        self.rel = rel
        self.dest_rel = dest_rel
        self.size = size or 0
        self.mtime = mtime or 0
        self.done = 0
        self.status = "pending"   # pending/running/done/failed/skipped/stopped
        self.dest = ""
        self.err = ""
        self.md5 = ""


class Transfer(object):
    def __init__(self, remote, files, dest, policy="skip", flatten=False,
                 verify=False, delete_source=False, workers=3, name=""):
        self.id = uuid.uuid4().hex[:12]
        self.remote = remote
        self.files = files
        self.dest = dest
        self.policy = policy
        self.flatten = flatten
        self.verify = verify
        self.delete_source = delete_source
        self.workers = max(1, int(workers))
        self.name = name
        self.created = time.time()
        self.state = "running"   # running/done/stopped
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads = []
        self.done_bytes = 0
        self.skipped_bytes = 0
        self.failed = 0
        self.done_files = 0
        self.skipped = 0
        self.started = time.time()
        self.speed = 0.0
        self._recent = []  # (ts, bytes)
        self.msg = ""

    # ---- 统计 -----------------------------------------------------------
    @property
    def total_bytes(self):
        return sum(f.size for f in self.files)

    @property
    def total_files(self):
        return len(self.files)

    def snapshot(self, max_files=400):
        with self.lock:
            elapsed = max(0.001, time.time() - self.started)
            speed = self.done_bytes / elapsed if self.state == "running" else self.speed
            left = max(0, self.total_bytes - self.done_bytes - self.skipped_bytes)
            eta = int(left / speed) if speed > 1 else None
            files = []
            for f in self.files[:max_files]:
                files.append({
                    "rel": f.rel, "dest": f.dest, "size": f.size, "done": f.done,
                    "status": f.status, "err": f.err,
                })
            return {
                "id": self.id,
                "state": self.state,
                "dest": self.dest,
                "policy": self.policy,
                "flatten": self.flatten,
                "msg": self.msg,
                "total_files": self.total_files,
                "done_files": self.done_files,
                "skipped": self.skipped,
                "failed": self.failed,
                "total_bytes": self.total_bytes,
                "done_bytes": self.done_bytes,
                "skipped_bytes": self.skipped_bytes,
                "speed": speed,
                "eta": eta,
                "elapsed": elapsed,
                "files": files,
                "files_truncated": len(self.files) > max_files,
                "created": self.created,
            }

    # ---- 主流程 ---------------------------------------------------------
    def start(self):
        t = threading.Thread(target=self._run, name="transfer-%s" % self.id, daemon=True)
        t.start()
        self.threads.append(t)
        return self

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.msg = "正在停止..."

    def _run(self):
        q = queue.Queue()
        for f in self.files:
            q.put(f)
        workers = [threading.Thread(target=self._worker, args=(q,), daemon=True)
                   for _ in range(self.workers)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        with self.lock:
            self.state = "stopped" if self.stop_event.is_set() else "done"
            self.speed = self.done_bytes / max(0.001, time.time() - self.started)

    def _worker(self, q):
        while not self.stop_event.is_set():
            try:
                f = q.get_nowait()
            except queue.Empty:
                return
            try:
                self._one(f)
            except Exception as e:  # noqa
                with self.lock:
                    f.status = "failed"
                    f.err = str(e)
                    self.failed += 1
            finally:
                q.task_done()

    def _dest_abs(self, rel):
        parts = [p for p in normalize_rel(rel).split("/") if p]
        if self.flatten and parts:
            parts = parts[-1:]
        target = os.path.join(self.dest, *parts)
        dest_real = os.path.realpath(self.dest)
        if not os.path.realpath(target).startswith(dest_real + os.sep):
            raise RuntimeError("目标路径越界: %s" % rel)
        return target

    def _one(self, f):
        dest_abs = self._dest_abs(f.dest_rel)
        f.dest = dest_abs
        os.makedirs(os.path.dirname(dest_abs) or self.dest, exist_ok=True)
        partial = dest_abs + ".ltdownload"

        # 冲突处理
        if os.path.exists(dest_abs):
            mode = self.policy
            if mode == "skip":
                with self.lock:
                    f.status = "skipped"
                    f.done = 0
                    self.skipped += 1
                    self.skipped_bytes += f.size
                return
            if mode == "newer" and os.path.isfile(dest_abs):
                local_m = os.path.getmtime(dest_abs)
                if local_m >= f.mtime > 0:
                    with self.lock:
                        f.status = "skipped"
                        self.skipped += 1
                        self.skipped_bytes += f.size
                    return
            if mode == "rename":
                base, ext = os.path.splitext(dest_abs)
                i = 1
                while os.path.exists(dest_abs):
                    dest_abs = "%s (%d)%s" % (base, i, ext)
                    i += 1
                partial = dest_abs + ".ltdownload"
                f.dest = dest_abs

        with self.lock:
            f.status = "running"

        offset = 0
        if os.path.exists(partial):
            offset = os.path.getsize(partial)
            if f.size and offset > f.size:
                offset = 0
            elif f.size and offset == f.size:
                pass  # 已完整, 直接落盘

        if f.size and offset == f.size:
            with self.lock:
                f.done = offset
                self.done_bytes += offset
        else:
            self._fetch(f, partial, offset)

        if self.stop_event.is_set() and f.status == "running":
            with self.lock:
                f.status = "stopped"
            return

        os.replace(partial, dest_abs)
        try:
            if f.mtime:
                os.utime(dest_abs, (f.mtime, f.mtime))
        except OSError:
            pass

        if self.verify:
            local_md5 = md5_file(dest_abs)
            remote_hash = self.remote.call("/api/hash", {"path": f.rel}, timeout=3600)
            if remote_hash.get("md5") != local_md5:
                with self.lock:
                    f.status = "failed"
                    f.err = "MD5 校验不一致"
                    self.failed += 1
                    self.done_bytes -= f.done
                try:
                    os.remove(dest_abs)
                except OSError:
                    pass
                return
            f.md5 = local_md5

        if self.delete_source:
            try:
                self.remote.post("/api/delete", {"path": f.rel})
            except Exception as e:  # noqa
                with self.lock:
                    self.msg = "源文件删除失败: %s" % e

        with self.lock:
            f.status = "done"
            self.done_bytes += max(0, (f.size or f.done) - f.done)
            f.done = f.size or f.done
            self.done_files += 1

    def _fetch(self, f, partial, offset):
        url = self.remote.url("/api/download", {"path": f.rel})
        headers = {}
        if self.remote.token:
            headers["X-Token"] = self.remote.token
        if offset:
            headers["Range"] = "bytes=%d-" % offset

        resp = None
        while True:
            req = urllib.request.Request(url, headers=headers)
            try:
                resp = urllib.request.urlopen(req, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code == 416 and offset:
                    return       # 服务端认为已完整
                raise RuntimeError("HTTP %s" % e.code)
            if offset and getattr(resp, "status", 200) != 206:
                resp.close()          # 服务端不支持续传, 从头下载
                offset = 0
                headers.pop("Range", None)
                with open(partial, "wb"):
                    pass
                continue
            break

        mode = "ab" if offset else "wb"
        downloaded = offset
        last_tick = time.time()
        last_bytes = downloaded
        with self.lock:
            f.done = offset
            self.done_bytes += offset
        try:
            with open(partial, mode) as out:
                while True:
                    if self.stop_event.is_set():
                        return
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    with self.lock:
                        f.done = downloaded
                        self.done_bytes += len(chunk)
                    now = time.time()
                    if now - last_tick >= 0.5:
                        with self.lock:
                            self.speed = (downloaded - last_bytes) / (now - last_tick)
                        last_tick, last_bytes = now, downloaded
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if f.size and downloaded != f.size:
            raise RuntimeError("大小不一致: 收到 %d / 应为 %d" % (downloaded, f.size))
        with self.lock:
            f.done = downloaded


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            c = f.read(CHUNK)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 接收端 HTTP 处理器: 本地 UI + 代理远程 + 传输管理
# --------------------------------------------------------------------------

class PullApp(object):
    def __init__(self, ui_port=DEFAULT_UI_PORT, dest=None, remote=None, verbose=True):
        self.ui_port = ui_port
        self.dest = os.path.realpath(dest or os.path.join(os.path.expanduser("~"), "LanTransfer"))
        self.remote = remote or Remote()
        self.transfers = []
        self.lock = threading.Lock()
        self.verbose = verbose
        self.discovered = {}
        os.makedirs(self.dest, exist_ok=True)

    # ---- 发现 -----------------------------------------------------------
    def start_discovery(self, udp_port=DISCOVERY_PORT):
        def loop():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", udp_port))
            except OSError as e:
                log("自动发现端口 %d 被占用, 已跳过自动发现 (可手动填 IP): %s" % (udp_port, e))
                return
            sock.settimeout(1.0)
            while True:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    info = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if info.get("app") != APP_TAG:
                    continue
                info["ip"] = addr[0]
                info["seen"] = time.time()
                with self.lock:
                    self.discovered[(addr[0], info.get("port"))] = info
        threading.Thread(target=loop, name="discovery", daemon=True).start()

    def discover_list(self):
        now = time.time()
        out = []
        with self.lock:
            for key, info in list(self.discovered.items()):
                if now - info["seen"] > 15:
                    continue
                out.append(info)
        out.sort(key=lambda i: i.get("host", ""))
        return out

    # ---- 远程调用 -------------------------------------------------------
    def remote_call(self, path, params):
        return self.remote.call(path, params)

    # ---- 传输 -----------------------------------------------------------
    def create_transfer(self, payload):
        items = payload.get("items") or []
        if not items:
            raise ValueError("没有选择要转移的内容")
        dest = payload.get("dest") or self.dest
        dest = os.path.expandvars(os.path.expanduser(dest))
        dest = os.path.abspath(dest)
        policy = payload.get("policy") or "skip"
        flatten = bool(payload.get("flatten"))
        verify = bool(payload.get("verify"))
        delete_source = bool(payload.get("delete_source"))
        workers = int(payload.get("workers") or 3)

        if not self.remote.host:
            raise ValueError("尚未连接发送端")
        if policy not in ("skip", "overwrite", "rename", "newer"):
            policy = "skip"
        if delete_source and not (self.remote.info or {}).get("allow_delete"):
            raise ValueError("发送端没有开启删除权限, 无法执行'完成后删除源文件'")

        # 展开目录, 汇总文件清单
        # 落盘规则: 以"所选项自身"为基准复刻结构 —— 选中的文件夹会连文件夹一起转存,
        #           选中的单个文件直接落在目标目录下 (与操作系统复制粘贴一致)
        files, seen = [], set()
        for it in items:
            rel = normalize_rel(it.get("path"))
            base = rel_dirname(rel)          # 去掉这一层, 保留所选项自身
            if it.get("dir"):
                res = self.remote_call("/api/walk", {"path": rel})
                for f in res.get("files", []):
                    inner = f["path"][len(base) + 1:] if base else f["path"]
                    drel = rel_basename(inner) if flatten else inner
                    key = drel
                    if key in seen:
                        continue
                    seen.add(key)
                    files.append(TransferFile(f["path"], drel, f.get("size"), f.get("mtime")))
            else:
                if not rel:
                    continue
                drel = rel_basename(rel)
                if drel in seen:
                    continue
                seen.add(drel)
                files.append(TransferFile(rel, drel, it.get("size"), it.get("mtime")))

        if not files:
            raise ValueError("所选内容里没有文件")

        os.makedirs(dest, exist_ok=True)
        t = Transfer(self.remote, files, dest, policy=policy, flatten=flatten,
                     verify=verify, delete_source=delete_source, workers=workers,
                     name=payload.get("name") or "")
        with self.lock:
            self.transfers.append(t)
        t.start()
        return t

    def state(self):
        with self.lock:
            transfers = [t.snapshot() for t in self.transfers[-8:]]
        return {
            "app": APP_TAG,
            "version": VERSION,
            "ui_port": self.ui_port,
            "dest": self.dest,
            "remote": {
                "host": self.remote.host,
                "port": self.remote.port,
                "connected": bool(self.remote.info),
                "info": self.remote.info,
            },
            "discovered": self.discover_list(),
            "locale_ips": local_ips(),
            "root_paths": list_local_roots(),
            "transfers": transfers,
        }


class PullHandler(_HandlerBase):
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"
        app = self.server.app
        try:
            if route in ("/", "/index.html"):
                return self._serve_ui()
            if route == "/api/state":
                return self.send_json(app.state())
            if route == "/api/local/list":
                return self._local_list(qs)
            if route == "/api/local/mkdir" and method == "POST":
                body = self.read_json_body()
                p = os.path.abspath(os.path.expandvars(os.path.expanduser(body.get("path", ""))))
                os.makedirs(p, exist_ok=True)
                return self.send_json({"ok": True, "path": p})
            if route == "/api/remote/list":
                return self.send_json(app.remote_call("/api/list", {"path": qs.get("path", [""])[0]}))
            if route == "/api/remote/walk":
                return self.send_json(app.remote_call("/api/walk", {"path": qs.get("path", [""])[0]}))
            if route == "/api/remote/search":
                return self.send_json(app.remote_call("/api/search", {
                    "path": qs.get("path", [""])[0], "q": qs.get("q", [""])[0]}))
            if route == "/api/connect" and method == "POST":
                return self._connect()
            if route == "/api/transfer" and method == "POST":
                body = self.read_json_body()
                t = app.create_transfer(body)
                return self.send_json({"ok": True, "id": t.id, "files": t.total_files,
                                       "bytes": t.total_bytes})
            if route == "/api/task/stop" and method == "POST":
                body = self.read_json_body()
                for t in app.transfers:
                    if t.id == body.get("id"):
                        t.stop()
                        return self.send_json({"ok": True})
                return self.send_json({"error": "not_found"}, 404)
            if route == "/api/tasks/clear" and method == "POST":
                with app.lock:
                    app.transfers = [t for t in app.transfers if t.state == "running"]
                return self.send_json({"ok": True, "kept": len(app.transfers)})
            if route == "/api/pick" and method == "POST":
                body = self.read_json_body()
                start = body.get("path") or app.dest
                picked = native_pick_dir(start)
                if picked:
                    app.dest = picked
                return self.send_json({"ok": bool(picked), "path": picked or ""})
            return self.send_json({"error": "not_found", "message": "未知接口"}, 404)
        except ValueError as e:
            self.read_json_body()
            return self.send_json({"error": "bad_request", "message": str(e)}, 400)
        except RuntimeError as e:
            self.read_json_body()
            return self.send_json({"error": "remote_error", "message": str(e)}, 502)
        except Exception as e:  # noqa
            self.read_json_body()
            return self.send_json({"error": "server_error", "message": repr(e)}, 500)

    def _serve_ui(self):
        ui = os.path.join(HERE, "ui.html")
        if not os.path.isfile(ui):
            return self.send_text("<h1>ui.html 缺失</h1><p>请把 ui.html 与 lan_transfer.py 放在同一目录。</p>",
                                  code=500)
        with open(ui, "rb") as f:
            return self.send_text(f.read(), ctype="text/html; charset=utf-8")

    def _local_list(self, qs):
        raw = qs.get("path", [""])[0]
        if not raw:
            return self.send_json({"path": "", "parent": None,
                                   "roots": list_local_roots(), "entries": []})
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
        if not os.path.isdir(p):
            return self.send_json({"error": "bad_request", "message": "不是目录: %s" % p}, 400)
        entries = []
        try:
            for name in os.listdir(p):
                full = os.path.join(p, name)
                if os.path.isdir(full):
                    entries.append({"name": name, "path": full, "dir": True})
        except PermissionError:
            return self.send_json({"error": "forbidden", "message": "无权限读取: %s" % p}, 403)
        entries.sort(key=lambda e: e["name"].lower())
        parent = os.path.dirname(p.rstrip("\\/")) or None
        return self.send_json({"path": p, "parent": parent, "roots": list_local_roots(),
                               "entries": entries})

    def _connect(self):
        body = self.read_json_body()
        app = self.server.app
        host = (body.get("host") or "").strip()
        if not host:
            raise ValueError("请填写发送端 IP")
        port = int(body.get("port") or DEFAULT_SOURCE_PORT)
        token = (body.get("token") or "").strip()
        old = (app.remote.host, app.remote.port, app.remote.token)
        app.remote.host, app.remote.port, app.remote.token = host, port, token
        try:
            info = app.remote.connect()
        except Exception as e:
            app.remote.host, app.remote.port, app.remote.token = old
            app.remote.info = None
            self.read_json_body()
            return self.send_json({"error": "connect_failed", "message": str(e)}, 502)
        return self.send_json({"ok": True, "info": info})


def build_pull_server(ui_port=DEFAULT_UI_PORT, dest=None, verbose=True, bind="127.0.0.1"):
    app = PullApp(ui_port=ui_port, dest=dest, verbose=verbose)
    app.start_discovery()
    srv = _make_server((bind, ui_port), PullHandler, "接收端界面")
    srv.app = app
    srv.verbose = verbose
    return srv


def native_pick_dir(start=""):
    """可选的原生目录选择框 (Windows 用 tkinter)。失败返回空串。"""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return ""
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askdirectory(initialdir=start or os.path.expanduser("~"),
                                        title="选择目标目录")
        root.destroy()
        return picked or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def cmd_serve(args):
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(args.root)))
    if not os.path.isdir(root):
        raise SystemExit("根目录不存在: %s" % root)
    srv = build_source_server(root, args.port, token=args.token or "",
                              allow_delete=args.allow_delete, verbose=not args.quiet)
    stop = threading.Event()
    if not args.no_beacon:
        start_beacon(args.port, os.path.basename(root.rstrip("\\/")) or root,
                     args.discovery_port, stop)
    log("=" * 68)
    log("  发送端已启动 (数据源)")
    log("  共享根目录 : %s" % root)
    log("  监听端口   : %d" % args.port)
    log("  访问令牌   : %s" % (args.token or "(未设置, 局域网内任何人可读)"))
    log("  删除权限   : %s" % ("已开启" if args.allow_delete else "已关闭"))
    for ip in local_ips():
        log("  接收端连接 : http://%s:%d  (接收端里填 IP = %s)" % (ip, args.port, ip))
    log("  自动发现   : %s" % ("开启" if not args.no_beacon else "关闭"))
    log("  按 Ctrl+C 停止")
    log("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("正在停止...")
        stop.set()
        srv.shutdown()


def cmd_pull(args):
    srv = build_pull_server(args.port, dest=args.dest, verbose=not args.quiet)
    url = "http://127.0.0.1:%d/" % args.port
    log("=" * 68)
    log("  接收端已启动")
    log("  界面地址   : %s" % url)
    log("  默认目标目录: %s" % srv.app.dest)
    log("  本机 IP    : %s" % ", ".join(local_ips() or ["(未检测到)"]))
    log("  按 Ctrl+C 停止")
    log("=" * 68)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("正在停止...")
        srv.shutdown()


def cmd_gui(args):
    """单机模式: 同时开网页界面, 目标是本机 (用于本机自测)。"""
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(args.root)))
    srv = build_source_server(root, args.source_port, token=args.token or "",
                              allow_delete=args.allow_delete, verbose=not args.quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    psrv = build_pull_server(args.port, dest=args.dest, verbose=not args.quiet)
    psrv.app.remote.host = "127.0.0.1"
    psrv.app.remote.port = args.source_port
    psrv.app.remote.token = args.token or ""
    try:
        info = psrv.app.remote.connect()
        log("已自动连接本机发送端: %s" % info.get("root"))
    except Exception as e:  # noqa
        log("自动连接失败(可在页面里手动连接): %s" % e)
    url = "http://127.0.0.1:%d/" % args.port
    log("发送端: http://127.0.0.1:%d  (根目录 %s)" % (args.source_port, root))
    log("界面  : %s" % url)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        psrv.serve_forever()
    except KeyboardInterrupt:
        psrv.shutdown()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="lan_transfer.py",
        description="局域网数据转移工具 (零依赖, 发送端/接收端二合一)")
    sub = ap.add_subparsers(dest="mode")

    s = sub.add_parser("serve", help="发送端: 在旧电脑上跑, 对外提供文件列表与下载")
    s.add_argument("--root", "-r", required=True, help="要共享的根目录, 例如 D:\\ 或 D:\\资料")
    s.add_argument("--port", "-p", type=int, default=DEFAULT_SOURCE_PORT, help="监听端口 (默认 8788)")
    s.add_argument("--token", "-t", default="", help="访问令牌, 建议设置 (局域网内共享密码)")
    s.add_argument("--allow-delete", action="store_true", help="允许接收端在传输完成后删除源文件")
    s.add_argument("--no-beacon", action="store_true", help="关闭 UDP 自动发现广播")
    s.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_serve)

    p = sub.add_parser("pull", help="接收端: 在新电脑上跑, 打开网页界面拉取数据")
    p.add_argument("--port", "-p", type=int, default=DEFAULT_UI_PORT, help="界面端口 (默认 8899)")
    p.add_argument("--dest", "-d", default=None, help="默认目标目录")
    p.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_pull)

    g = sub.add_parser("gui", help="单机自测: 同一台机器同时开发送端和界面")
    g.add_argument("--root", "-r", required=True)
    g.add_argument("--source-port", type=int, default=DEFAULT_SOURCE_PORT)
    g.add_argument("--port", "-p", type=int, default=DEFAULT_UI_PORT)
    g.add_argument("--dest", "-d", default=None)
    g.add_argument("--token", "-t", default="")
    g.add_argument("--allow-delete", action="store_true")
    g.add_argument("--no-browser", action="store_true")
    g.add_argument("--quiet", action="store_true")
    g.set_defaults(func=cmd_gui)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
