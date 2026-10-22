#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NapCat 桌面端（管理面板壳 · 左侧模型功能栏）
================================================
pywebview + 本地 HTTP 界面服务：
  - NapCat 面板（内嵌 WebUI iframe）
  - 运行状态
  - 人物档案（群 + 私聊，只读 memwatch）
  - 事件记忆 / 话题兴趣 / 记忆网络 / Token 统计 / 人物好感度 / 会话设置
  - MaiBot 面板
界面通过 fetch() 调本地 /api/* 接口（绕开 pywebview js_api 桥）。
自动建 SSH 隧道，点 X 缩到系统托盘。

依赖：pip install pywebview pythonnet paramiko pystray pillow
运行：python napcat_desktop.py
"""

import os
import re
import sys
import json
import socket
import threading
import time
import http.server
import socketserver
import base64
import urllib.request
import urllib.error
try:
    import tomllib
except Exception:
    tomllib = None
try:
    import websocket
except Exception:
    websocket = None

# pythonw（无控制台）下 stdout/stderr 是 None
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "napcat_desktop.log"), "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

# ==================== 配置 ====================
# 便携版：优先读同目录 config.json，其次读 QQBOT_* 环境变量，最后用默认值。
_CONFIG = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), "r", encoding="utf-8") as _f:
        _CONFIG = json.load(_f)
except Exception:
    _CONFIG = {}


def _cfg(key, default=None):
    return os.environ.get("QQBOT_" + key.upper(), _CONFIG.get(key, default))


SERVER            = _cfg("server", "CHANGE_ME_SERVER_IP")
SSH_USER          = _cfg("ssh_user", "admin")
SSH_KEY           = _cfg("ssh_key", os.path.expanduser("~/.ssh/id_ed25519"))
SSH_PORT          = int(_cfg("ssh_port", 22))
REMOTE_WEBUI_HOST = "127.0.0.1"
REMOTE_WEBUI_PORT = int(_cfg("llbot_port", 3080))
LOCAL_PORT        = 16099
WEBUI_TOKEN       = _cfg("llbot_webui_token", "")

# MaiBot WebUI
MAIBOT_REMOTE_PORT = int(_cfg("maibot_port", 18001))
MAIBOT_LOCAL_PORT  = 16097
MAIBOT_WEBUI_JSON  = "/opt/maibot/data/MaiMBot/webui.json"


# 模型 id → 友好名称
MODEL_LABELS = {
    "deepseek-flash": "V4flash · DeepSeek-V4.1-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
}

WINDOW_TITLE = "LLBot 桌面端 · 管理面板"
WINDOW_W     = 1280
WINDOW_H     = 880
INJECT_PORT  = 19333
_BALANCE_KEY = {"value": None}
_SF_KEY = {"value": None}
_BAL_CACHE = {"ts": 0.0, "data": None}
_SF_BAL_CACHE = {"ts": 0.0, "data": None}
_TOK_CACHE = {"ts": 0.0, "data": None}
UI_SETTINGS_FILE = os.environ.get("PANEL_UI_SETTINGS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_settings.json"))
UI_SETTINGS_REMOTE = "/opt/qqbot-panel/ui_settings.json"

# 存储容量监控：key / 显示名 / 服务器路径 / 默认上限(MB)
STORAGE_DIRS = [
    ("prompt_imgs", "MaiBot 图片缓存", "/opt/maibot/data/MaiMBot/prompt_imgs", 600),
    ("emoji", "MaiBot 表情库", "/opt/maibot/data/MaiMBot/emoji", 2000),
    ("images", "MaiBot 收发图片", "/opt/maibot/data/MaiMBot/images", 500),
    ("maibot_logs", "MaiBot 日志", "/opt/maibot/data/MaiMBot/logs", 1500),
    ("qq_emoji", "QQ 表情资源", "/opt/napcat/data/qq/nt_qq/global/nt_data/Emoji", 1000),
]
STORAGE_DEFAULTS = {
    "disk_warn": 85,
    "disk_crit": 94,
    "dirs": {key: limit for key, _label, _path, limit in STORAGE_DIRS},
}


def load_ui_theme(remote=None):
    try:
        if remote is not None:
            data = json.loads(remote.read(UI_SETTINGS_REMOTE))
        else:
            with open(UI_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        return "dark" if data.get("theme") == "dark" else "light"
    except Exception:
        return "light"

try:
    import paramiko
except Exception:
    paramiko = None
try:
    import webview
except Exception:
    webview = None
try:
    import pystray
except Exception:
    pystray = None
try:
    from PIL import Image, ImageDraw
except Exception:
    Image = ImageDraw = None


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


class Tunnel:
    def __init__(self, server, ssh_user, ssh_key, ssh_port, remote_host, remote_port, local_port):
        self.server = server
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key
        self.ssh_port = ssh_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port
        self.client = None
        self.transport = None
        self.sock = None
        self.running = False
        self._lock = threading.RLock()
        self._last_note = 0.0

    def _connect(self):
        """（重新）建立 SSH 连接，返回 transport"""
        with self._lock:
            old = self.client
            self.client = None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self.server, port=self.ssh_port, username=self.ssh_user,
                      key_filename=self.ssh_key, timeout=15,
                      allow_agent=False, look_for_keys=False)
            self.client = c
            self.transport = c.get_transport()
            try:
                # 30 秒一次心跳，防止链路静默死掉（运营商/NAT 超时最常见的坑）
                self.transport.set_keepalive(30)
            except Exception:
                pass
            return self.transport

    def _ensure_transport(self):
        """连接活着就复用，断了就重连——隧道挂了不该让整个面板跟着废"""
        t = self.transport
        if t is not None and t.is_active():
            return t
        with self._lock:
            if self.transport is not None and self.transport.is_active():
                return self.transport
            self._connect()
            print(f"[隧道] 已重连 {self.server}:{self.remote_port}", flush=True)
            return self.transport

    def _watchdog(self):
        """每 20 秒体检一次，SSH 静默断开时自动接回来"""
        while self.running:
            time.sleep(20)
            if not self.running:
                break
            try:
                self._ensure_transport()
            except Exception as e:
                now = time.time()
                if now - self._last_note > 60:
                    self._last_note = now
                    print(f"[隧道] 重连失败（{self.remote_port}）: {e}", flush=True)

    def start(self):
        self._connect()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.local_port))
        self.sock.listen(16)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        print(f"[隧道] 已建立 127.0.0.1:{self.local_port} -> {self.server}:{self.remote_port}", flush=True)

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            chan = None
            for attempt in (1, 2):
                try:
                    chan = self._ensure_transport().open_channel(
                        "direct-tcpip", (self.remote_host, self.remote_port), addr)
                    break
                except Exception:
                    # 第一次失败多半是连接刚好断，重连后再试一次
                    if attempt == 2:
                        now = time.time()
                        if now - self._last_note > 30:
                            self._last_note = now
                            print(f"[隧道] 转发失败（{self.remote_port}），已丢弃本次连接", flush=True)
                    continue
            if chan is None:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            threading.Thread(target=_pipe, args=(conn, chan), daemon=True).start()
            threading.Thread(target=_pipe, args=(chan, conn), daemon=True).start()

    def stop(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        print("[隧道] 已关闭", flush=True)


class Remote:
    def __init__(self, server, user, key, port):
        self.server = server
        self.user = user
        self.key = key
        self.port = port
        self.client = None
        self.lock = threading.RLock()

    def connect(self):
        with self.lock:
            if self.client is not None:
                t = self.client.get_transport()
                if t is None or not t.is_active():
                    try:
                        self.client.close()
                    except Exception:
                        pass
                    self.client = None
            if self.client is None:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(self.server, port=self.port, username=self.user,
                          key_filename=self.key, timeout=15,
                          allow_agent=False, look_for_keys=False)
                try:
                    c.get_transport().set_keepalive(30)
                except Exception:
                    pass
                self.client = c
        return self.client

    def read(self, path):
        with self.lock:
            sftp = self.connect().open_sftp()
            try:
                with sftp.open(path, "r") as f:
                    return f.read().decode("utf-8")
            finally:
                sftp.close()

    def write(self, path, content):
        with self.lock:
            sftp = self.connect().open_sftp()
            try:
                with sftp.open(path, "w") as f:
                    f.write(content.encode("utf-8"))
            finally:
                sftp.close()

    def exec(self, cmd):
        with self.lock:
            stdin, stdout, stderr = self.connect().exec_command(cmd, timeout=60)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            return code, out, err


class LocalSFTP:
    """本地文件接口，方法名对齐 paramiko.SFTPClient 的最小用法。"""
    def listdir(self, path):
        return os.listdir(path)

    def open(self, path, mode="r"):
        return open(path, "rb" if "b" not in mode else mode)

    def close(self):
        pass


class LocalRemote:
    """服务器本机模式：把原来的 SSH/SFTP 操作换成本地文件与 bash。"""
    def __init__(self):
        self.lock = threading.RLock()

    def connect(self):
        return self

    def open_sftp(self):
        return LocalSFTP()

    def read(self, path):
        with self.lock:
            try:
                with open(path, "rb") as f:
                    return f.read().decode("utf-8")
            except Exception:
                import subprocess
                out = subprocess.check_output(["sudo", "cat", path])
                return out.decode("utf-8")

    def write(self, path, content):
        with self.lock:
            try:
                with open(path, "wb") as f:
                    f.write(content.encode("utf-8"))
            except Exception:
                import subprocess
                p = subprocess.Popen(["sudo", "tee", path], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                p.communicate(content.encode("utf-8"))

    def exec(self, cmd):
        import subprocess
        with self.lock:
            p = subprocess.Popen(["/bin/bash", "-lc", cmd], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            try:
                out, err = p.communicate(timeout=60)
            except Exception:
                p.kill()
                out, err = p.communicate()
            return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def port_open(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def http_alive(port, timeout=2.0):
    """真发一个 HTTP 请求再判断死活。

    只看端口有没有 LISTENING 是不够的：SSH 隧道断掉时本地监听还在，
    accept 之后转不过去，界面就会表现为「显示已连接，但页面一片空白」。
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return s.recv(16).startswith(b"HTTP/")
        finally:
            s.close()
    except Exception:
        return False


class Api:
    def __init__(self, remote):
        self.remote = remote
        self.local_mode = isinstance(remote, LocalRemote)
        self._storage_cache = (0.0, None)

    def _fresh_remote(self):
        """一次性连接：绕开共享 Remote 被卡住时把整页拖死。"""
        if self.local_mode:
            return LocalRemote()
        return Remote(SERVER, SSH_USER, SSH_KEY, SSH_PORT)

    def _fresh_read(self, path):
        r = self._fresh_remote()
        try:
            return r.read(path)
        finally:
            try:
                if r.client is not None:
                    r.client.close()
            except Exception:
                pass

    def _fresh_exec(self, cmd):
        r = self._fresh_remote()
        try:
            return r.exec(cmd)
        finally:
            try:
                if r.client is not None:
                    r.client.close()
            except Exception:
                pass

    def get_llbot_status(self):
        """单独读看门狗状态，供顶部横幅轮询；不碰共享 SSH 连接。"""
        try:
            return json.loads(self._fresh_read("/opt/llbot-watchdog/status.json"))
        except Exception as e:
            return {"online": False, "state": "unknown", "error": str(e)}

    def get_status(self):
        groups = 0
        pc = 0
        try:
            p = self.get_profiles()
            gd = p.get("groups") or {}
            groups = len(gd)
            pc = sum(len((v or {}).get("users") or {}) for v in gd.values())
        except Exception:
            pass
        llbot = {}
        try:
            llbot = json.loads(self.remote.read("/opt/llbot-watchdog/status.json"))
        except Exception:
            llbot = {}
        return {"model": "deepseek-flash", "model_label": "MaiBot · V4flash",
                "effort": "按群设置", "profiles_count": pc, "groups_count": groups,
                "tunnel_ok": (True if self.local_mode else http_alive(LOCAL_PORT)),
                "maibot_ok": (True if self.local_mode else http_alive(MAIBOT_LOCAL_PORT)),
                "llbot": llbot}

    @staticmethod
    def _merge_storage(base, inc):
        out = {"disk_warn": int(base.get("disk_warn", STORAGE_DEFAULTS["disk_warn"])),
               "disk_crit": int(base.get("disk_crit", STORAGE_DEFAULTS["disk_crit"])),
               "dirs": dict(STORAGE_DEFAULTS["dirs"])}
        out["dirs"].update(base.get("dirs") or {})
        if isinstance(inc, dict):
            if "disk_warn" in inc:
                try:
                    out["disk_warn"] = max(1, min(100, int(inc["disk_warn"])))
                except (TypeError, ValueError):
                    pass
            if "disk_crit" in inc:
                try:
                    out["disk_crit"] = max(1, min(100, int(inc["disk_crit"])))
                except (TypeError, ValueError):
                    pass
            if isinstance(inc.get("dirs"), dict):
                for k, v in inc["dirs"].items():
                    if k in out["dirs"]:
                        try:
                            out["dirs"][k] = max(1, int(v))
                        except (TypeError, ValueError):
                            pass
        if out["disk_crit"] < out["disk_warn"]:
            out["disk_crit"] = out["disk_warn"]
        return out

    def _read_ui_settings(self):
        if self.local_mode:
            with open(UI_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return json.loads(self._fresh_read(UI_SETTINGS_REMOTE))

    def get_ui_settings(self):
        try:
            data = self._read_ui_settings()
        except Exception:
            data = {}
        return {"theme": "dark" if data.get("theme") == "dark" else "light",
                "storage": self._merge_storage(data.get("storage") or {}, None)}

    def save_ui_settings(self, content):
        try:
            data = json.loads(content)
            cur = self.get_ui_settings()
            theme = "dark" if data.get("theme", cur["theme"]) == "dark" else "light"
            storage = self._merge_storage(cur["storage"], data.get("storage"))
            payload = json.dumps({"theme": theme, "storage": storage}, ensure_ascii=False, indent=2)
            if self.local_mode:
                with open(UI_SETTINGS_FILE, "w", encoding="utf-8") as f:
                    f.write(payload)
            else:
                rr = self._fresh_remote()
                try:
                    rr.write(UI_SETTINGS_REMOTE, payload)
                finally:
                    try:
                        if rr.client is not None:
                            rr.client.close()
                    except Exception:
                        pass
            self._storage_cache = (0.0, None)   # 阈值变了，下次立刻重新采集
            return {"ok": True, "theme": theme, "storage": storage}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_storage(self):
        """磁盘 + 各缓存目录占用，超过阈值就抬 alert，面板顶上拉横幅。

        一次 SSH 往返取全部数字，结果缓存 55 秒——前端轮询不会把服务器敲坏。
        """
        now = time.time()
        ts, cached = self._storage_cache
        if cached and now - ts < 55:
            return cached
        st = self.get_ui_settings().get("storage") or {}
        warn = int(st.get("disk_warn", STORAGE_DEFAULTS["disk_warn"]))
        crit = int(st.get("disk_crit", STORAGE_DEFAULTS["disk_crit"]))
        limits = st.get("dirs") or STORAGE_DEFAULTS["dirs"]
        paths = [p for _k, _l, p, _lim in STORAGE_DIRS]
        cmd = ("sudo sh -c 'df -P / | tail -n 1; du -sm " + " ".join(paths) + " 2>/dev/null'")
        try:
            code, out, err = self.remote.exec(cmd)
        except Exception as e:
            return {"ok": False, "error": str(e), "alerts": [], "disk": {}, "dirs": []}
        disk, sizes = {}, {}
        for line in (out or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0].startswith("/dev/") and len(parts) >= 5:
                disk = {"total_gb": round(int(parts[1]) / 1048576.0, 1),
                        "used_gb": round(int(parts[2]) / 1048576.0, 1),
                        "free_gb": round(int(parts[3]) / 1048576.0, 1),
                        "pct": int(parts[4].rstrip("%"))}
            elif len(parts) == 2:
                try:
                    sizes[parts[1].strip()] = int(parts[0])
                except ValueError:
                    pass
        dirs = []
        for key, label, path, dflt in STORAGE_DIRS:
            mb = sizes.get(path, 0)
            limit = int(limits.get(key) or dflt)
            dirs.append({"key": key, "label": label, "path": path, "mb": mb,
                         "limit": limit, "over": mb > limit})
        alerts = []
        pct = int(disk.get("pct") or 0)
        if pct >= crit:
            alerts.append({"level": "crit", "text": "磁盘 %d%%（临界 %d%%），该清了" % (pct, crit)})
        elif pct >= warn:
            alerts.append({"level": "warn", "text": "磁盘 %d%%（预警 %d%%）" % (pct, warn)})
        for d in dirs:
            if not d["over"]:
                continue
            lvl = "crit" if d["mb"] >= d["limit"] * 1.2 else "warn"
            alerts.append({"level": lvl,
                           "text": "%s %dMB／上限 %dMB" % (d["label"], d["mb"], d["limit"])})
        res = {"ok": True, "disk": disk, "dirs": dirs, "alerts": alerts,
               "warn": warn, "crit": crit, "ts": int(now)}
        self._storage_cache = (now, res)
        if alerts:
            print("[存储] " + "；".join(a["text"] for a in alerts), flush=True)
        return res

    def get_profiles(self):
        """人物档案：群与私聊同等对待，只读 memwatch 每份会话的 profiles + affinity。"""
        groups_data, group_ids, latest, err = {}, [], 0, None
        try:
            with self.remote.lock:
                sftp = self.remote.connect().open_sftp()
                try:
                    files = [f for f in sftp.listdir("/opt/memwatch/data/groups") if f.endswith(".json")]
                    for fn in files:
                        gid = fn[:-5]
                        try:
                            with sftp.open("/opt/memwatch/data/groups/" + fn) as fh:
                                d = json.loads(fh.read().decode("utf-8"))
                        except Exception:
                            continue
                        if not isinstance(d, dict):
                            continue
                        g_upd = int(d.get("updated") or 0)
                        latest = max(latest, g_upd)
                        group_ids.append(gid)
                        aff_map = d.get("affinity") or {}
                        name2uid = {}
                        for uid, ent in aff_map.items():
                            if isinstance(ent, dict):
                                nm = ent.get("name")
                                if nm:
                                    name2uid[nm] = str(uid)
                        users = {}
                        for name, prof in (d.get("profiles") or {}).items():
                            if not isinstance(prof, dict):
                                continue
                            uid = name2uid.get(name, "")
                            key = uid or ("n:" + str(name))
                            u = users.setdefault(key, {"nickname": name, "uid": uid,
                                                       "notes": [], "updated": 0, "score": None,
                                                       "pos": 0, "sad": 0})
                            u["updated"] = max(u["updated"], int(prof.get("updated") or g_upd))
                            for note in prof.get("notes") or []:
                                if isinstance(note, dict):
                                    note = note.get("text", "")
                                t = str(note).strip()
                                if t and t not in u["notes"]:
                                    u["notes"].append(t)
                            ent = aff_map.get(uid) if uid else None
                            if isinstance(ent, dict):
                                u["score"] = float(ent.get("score", 50))
                                u["pos"] = int(ent.get("pos", 0))
                                u["sad"] = int(ent.get("sad", 0))
                        for uid, ent in aff_map.items():
                            if not isinstance(ent, dict):
                                continue
                            key = str(uid)
                            if key in users:
                                continue
                            users[key] = {"nickname": ent.get("name") or key, "uid": key,
                                          "notes": [], "updated": g_upd,
                                          "score": float(ent.get("score", 50)),
                                          "pos": int(ent.get("pos", 0)), "sad": int(ent.get("sad", 0))}
                        groups_data[gid] = {"users": users, "updated": g_upd}
                finally:
                    sftp.close()
        except Exception as e:
            err = str(e)
        return {"source": "memwatch", "groups": groups_data, "group_ids": sorted(group_ids),
                "latest": latest, "updated_at": int(time.time()), "error": err}

    def restart_bot(self):
        try:
            code, out, err = self.remote.exec("cd /opt/memwatch && sudo docker compose restart")
            return {"ok": code == 0}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_maibot_token(self):
        try:
            data = json.loads(self.remote.read(MAIBOT_WEBUI_JSON))
            return {"token": data.get("access_token", "")}
        except Exception as e:
            return {"token": "", "error": str(e)}

    def get_llbot_token(self):
        """LLBot WebUI 的访问密码（就是第一次进面板要填的那个）。"""
        try:
            tok = self.remote.read("/opt/llbot/llbot_config/webui_token.txt").strip()
            return {"token": tok}
        except Exception as e:
            return {"token": "", "error": str(e)}

    def get_deepseek_balance(self):
        """查 DeepSeek 账户余额（CNY）"""
        now = time.time()
        if _BAL_CACHE["data"] and now - _BAL_CACHE["ts"] < 60:
            return _BAL_CACHE["data"]
        try:
            key = _BALANCE_KEY.get("value")
            if not key:
                comp = self._fresh_read("/opt/memwatch/compose.yaml")
                m = re.search(r"DEEPSEEK_KEY:\s*(\S+)", comp)
                if not m:
                    return {"error": "compose.yaml 里没找到 DEEPSEEK_KEY"}
                key = m.group(1)
                _BALANCE_KEY["value"] = key
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
            infos = data.get("balance_infos") or []
            info = next((x for x in infos if x.get("currency") == "CNY"), infos[0] if infos else {})
            res = {"available": bool(data.get("is_available")), "currency": info.get("currency", "CNY"),
                   "total": float(info.get("total_balance") or 0),
                   "granted": float(info.get("granted_balance") or 0),
                   "topped_up": float(info.get("topped_up_balance") or 0)}
            _BAL_CACHE["ts"] = now
            _BAL_CACHE["data"] = res
            return res
        except Exception as e:
            return {"error": str(e)}

    def get_siliconflow_balance(self):
        """查 SiliconFlow 账户余额（CNY）。接口 410 时明确标记已下线。"""
        now = time.time()
        if _SF_BAL_CACHE["data"] and now - _SF_BAL_CACHE["ts"] < 60:
            return _SF_BAL_CACHE["data"]
        try:
            key = _SF_KEY.get("value")
            if not key:
                raw = self._fresh_read("/opt/maibot/docker-config/mmc/model_config.toml")
                data = tomllib.loads(raw) if tomllib else {}
                for p in (data.get("api_providers") or []):
                    if str(p.get("name") or "").lower() == "siliconflow" and p.get("api_key"):
                        key = p["api_key"]
                        break
                if not key:
                    return {"error": "model_config.toml 里没找到 SiliconFlow key"}
                _SF_KEY["value"] = key

            url = "https://api.siliconflow.cn/v1/user/info"
            last = None
            for headers in ({"Authorization": "Bearer " + key}, {"X-API-Key": key}):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=6) as r:
                        data = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    body = ""
                    try:
                        body = e.read().decode("utf-8", "ignore")
                    except Exception:
                        pass
                    if e.code == 410 or "deprecated" in body:
                        return {"error": "SiliconFlow 余额接口已下线（410）", "deprecated": True}
                    if e.code == 401 or "30014" in body:
                        last = {"error": "SiliconFlow key 无效（401）"}
                        continue
                    last = {"error": "HTTP %s" % e.code}
                    continue
            else:
                return last or {"error": "SiliconFlow 余额读取失败"}

            d = data.get("data") or {}
            if not d:
                return {"error": str(data.get("message") or "SiliconFlow 余额返回为空")}
            total = float(d.get("totalBalance") or 0)
            charge = float(d.get("chargeBalance") or 0)
            res = {"available": bool(data.get("status", True)), "currency": "CNY",
                   "total": round(total, 4), "topped_up": round(charge, 4),
                   "granted": round(total - charge, 4)}
            _SF_BAL_CACHE["ts"] = now
            _SF_BAL_CACHE["data"] = res
            return res
        except Exception as e:
            return {"error": str(e)}

    def get_token_stats(self):
        """远程跑 token_stats.py：MaiBot 用量库 + memwatch 事件记账，按群隔离"""
        now = time.time()
        if _TOK_CACHE["data"] and now - _TOK_CACHE["ts"] < 10:
            return _TOK_CACHE["data"]
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_stats.py")
            with open(path, "r", encoding="utf-8") as f:
                script = f.read()
            b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
            code, out, err = self._fresh_exec("echo %s | base64 -d | sudo python3 -" % b64)
            if code != 0:
                return {"error": (err or out or "远程统计脚本失败")[-800:]}
            data = json.loads(out.strip().splitlines()[-1])
            data["balance"] = self.get_deepseek_balance()
            data["sf_balance"] = self.get_siliconflow_balance()
            _TOK_CACHE["ts"] = time.time()
            _TOK_CACHE["data"] = data
            return data
        except Exception as e:
            return {"error": str(e)}

    def get_event_memory(self):
        """读取事件记忆服务写出的每群 JSON"""
        try:
            with self.remote.lock:
                sftp = self.remote.connect().open_sftp()
                try:
                    try:
                        files = [f for f in sftp.listdir("/opt/memwatch/data/groups") if f.endswith(".json")]
                    except Exception:
                        files = []
                    out = {}
                    for fn in files:
                        try:
                            with sftp.open("/opt/memwatch/data/groups/" + fn) as fh:
                                out[fn[:-5]] = json.loads(fh.read().decode("utf-8"))
                        except Exception:
                            pass
                    return out
                finally:
                    sftp.close()
        except Exception as e:
            return {"error": str(e)}

    def get_changelog(self):
        """更新日志：读服务器上的 /opt/qqbot-panel/CHANGELOG.md。"""
        path = "/opt/qqbot-panel/CHANGELOG.md"
        try:
            with self.remote.lock:
                sftp = self.remote.connect().open_sftp()
                try:
                    with sftp.open(path) as fh:
                        text = fh.read().decode("utf-8")
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            return {"ok": True, "text": text}
        except Exception as e:
            return {"ok": False, "text": "", "error": str(e)}

    def get_experiences(self):
        """全局经验库：不按群隔离，一条经验所有会话共享。"""
        try:
            with self.remote.lock:
                sftp = self.remote.connect().open_sftp()
                try:
                    try:
                        with sftp.open("/opt/memwatch/data/experiences.json") as fh:
                            data = json.loads(fh.read().decode("utf-8"))
                    except Exception:
                        data = {}
                    items = []
                    for v in (data.get("items") or {}).values():
                        if not isinstance(v, dict):
                            continue
                        items.append({
                            "id": v.get("id"),
                            "question": v.get("question") or "",
                            "learned": v.get("learned") or "",
                            "status": v.get("status") or "open",
                            "source": v.get("source") or "",
                            "sources": v.get("sources") or [],
                            "topic": v.get("topic") or "",
                            "created": int(v.get("created") or 0),
                            "updated": int(v.get("updated") or 0),
                            "hits": int(v.get("hits") or 0),
                            "category": v.get("category") or "",
                        })
                    items.sort(key=lambda x: -x["updated"])
                    return {"items": items, "updated": int(data.get("updated") or 0)}
                finally:
                    sftp.close()
        except Exception as e:
            return {"items": [], "error": str(e)}

    def get_group_settings(self):
        """群与私聊的推理档位设置。groups 里每项带 kind/name，前端直接出标签。"""
        try:
            settings = {}
            try:
                settings = json.loads(self.remote.read("/opt/memwatch/data/group_settings.json"))
            except Exception:
                pass
            if not isinstance(settings, dict):
                settings = {}
            rows = {}
            try:
                with self.remote.lock:
                    sftp = self.remote.connect().open_sftp()
                    try:
                        for f in sftp.listdir("/opt/memwatch/data/groups"):
                            if not f.endswith(".json"):
                                continue
                            key = f[:-5]
                            item = {"id": key, "kind": "private" if key.startswith("p") else "group", "name": ""}
                            try:
                                with sftp.open("/opt/memwatch/data/groups/" + f) as fh:
                                    g = json.loads(fh.read().decode("utf-8"))
                                aff = g.get("affinity") or {}
                                for _uid, ent in aff.items():
                                    if isinstance(ent, dict) and ent.get("name"):
                                        item["name"] = ent["name"]
                                        break
                                if not item["name"]:
                                    prof = g.get("profiles") or {}
                                    if prof:
                                        item["name"] = next(iter(prof))
                            except Exception:
                                pass
                            rows[key] = item
                    finally:
                        sftp.close()
            except Exception:
                pass
            for k in settings:   # 有设置但还没聊过的，也列出来
                rows.setdefault(k, {"id": k, "kind": "private" if str(k).startswith("p") else "group", "name": ""})
            return {"settings": settings, "groups": [rows[k] for k in sorted(rows)]}
        except Exception as e:
            return {"settings": {}, "groups": [], "error": str(e)}

    def save_group_settings(self, content):
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                return {"ok": False, "error": "格式错误"}
            self.remote.write("/opt/memwatch/data/group_settings.json", json.dumps(data, ensure_ascii=False, indent=2))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def start_ui_server(html_text, api, host="127.0.0.1", port=0, auth=None):
    """本地 HTTP 服务：既提供界面，也做 /api/* 网关。auth=(user,password) 时启用 Basic Auth。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def _auth_ok(self):
            if not auth:
                return True
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                u, p = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)
            except Exception:
                return False
            return u == auth[0] and p == auth[1]

        def _deny(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="QQbot Panel"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def _proxy_ws(self, upstream_path, port=3080):
            """把 /llbot/ 或 /maibot/ 下的 WebSocket 原样转到本机端口。"""
            import select
            crlf = bytes((13, 10))
            blank = crlf + crlf
            up = socket.create_connection(("127.0.0.1", port), timeout=10)
            lines = ["GET " + upstream_path + " HTTP/1.1"]
            for k, v in self.headers.items():
                if k.lower() == "host":
                    v = "127.0.0.1:%d" % port
                lines.append(k + ": " + v)
            up.sendall((crlf.join([x.encode("utf-8", "replace") for x in lines]) + blank + blank))
            buf = b""
            while blank not in buf:
                d = up.recv(4096)
                if not d:
                    break
                buf += d
            if blank not in buf:
                up.close()
                return
            self.connection.sendall(buf)
            if b" 101 " not in buf.split(crlf)[0]:
                up.close()
                return
            self.close_connection = True
            socks = [self.connection, up]
            try:
                while True:
                    r, _, _ = select.select(socks, [], [], 60)
                    if not r:
                        break
                    for s in r:
                        d = s.recv(65536)
                        if not d:
                            return
                        (self.connection if s is up else up).sendall(d)
            finally:
                try:
                    up.close()
                except Exception:
                    pass

        def _proxy_webui(self, path, port, prefix):
            """通用 WebUI 反向代理：prefix/* -> 127.0.0.1:port/*，并改写绝对路径。"""
            import http.client
            query = self.path[len(path):]
            upstream_path = (path[len(prefix):] or "/") + query
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._proxy_ws(upstream_path, port)
                return
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            headers = {}
            for k, v in self.headers.items():
                if k.lower() in ("host", "connection", "content-length", "accept-encoding"):
                    continue
                headers[k] = v
            headers["Accept-Encoding"] = "identity"
            conn.request(self.command, upstream_path, body, headers)
            resp = conn.getresponse()
            data = resp.read()
            ctype = (resp.getheader("Content-Type") or "").lower()
            if any(x in ctype for x in ("text/html", "javascript", "text/css")):
                txt = data.decode("utf-8", "replace")
                txt = (txt.replace('"/assets/', '"' + prefix + '/assets/')
                          .replace("'/assets/", "'" + prefix + "/assets/")
                          .replace("url(/assets/", "url(" + prefix + "/assets/")
                          .replace('"/api', '"' + prefix + '/api')
                          .replace("'/api", "'" + prefix + "/api")
                          .replace("`/api", "`" + prefix + "/api"))
                data = txt.encode("utf-8")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("content-length", "content-encoding", "transfer-encoding", "connection"):
                    continue
                if k.lower() == "location" and v.startswith("/"):
                    v = prefix + "/" + v.lstrip("/")
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            conn.close()

        def _proxy_llbot(self, path):
            self._proxy_webui(path, 3080, "/llbot")

        def _proxy_maibot(self, path):
            self._proxy_webui(path, 18001, "/maibot")

        def do_GET(self):
            if not self._auth_ok():
                self._deny()
                return
            p = self.path.split("?")[0]
            if p == "/llbot" or p.startswith("/llbot/"):
                self._proxy_llbot(p)
                return
            if p == "/maibot" or p.startswith("/maibot/"):
                self._proxy_maibot(p)
                return
            try:
                if p in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", html_text)
                elif p == "/api/status":
                    self._send(200, "application/json", json.dumps(api.get_status(), ensure_ascii=False))
                elif p == "/api/llbot_status":
                    self._send(200, "application/json", json.dumps(api.get_llbot_status(), ensure_ascii=False))
                elif p == "/api/profiles":
                    self._send(200, "application/json", json.dumps(api.get_profiles(), ensure_ascii=False))
                elif p == "/api/llbot_token":
                    self._send(200, "application/json", json.dumps(api.get_llbot_token(), ensure_ascii=False))
                elif p == "/api/maibot":
                    self._send(200, "application/json", json.dumps(api.get_maibot_token(), ensure_ascii=False))
                elif p == "/api/memories":
                    self._send(200, "application/json", json.dumps(api.get_event_memory(), ensure_ascii=False))
                elif p == "/api/changelog":
                    self._send(200, "application/json", json.dumps(api.get_changelog(), ensure_ascii=False))
                elif p == "/api/experiences":
                    self._send(200, "application/json", json.dumps(api.get_experiences(), ensure_ascii=False))
                elif p == "/api/token_stats":
                    self._send(200, "application/json", json.dumps(api.get_token_stats(), ensure_ascii=False))
                elif p == "/api/group_settings":
                    self._send(200, "application/json", json.dumps(api.get_group_settings(), ensure_ascii=False))
                elif p == "/api/ui_settings":
                    self._send(200, "application/json", json.dumps(api.get_ui_settings(), ensure_ascii=False))
                elif p == "/api/storage":
                    self._send(200, "application/json", json.dumps(api.get_storage(), ensure_ascii=False))
                else:
                    self._send(404, "text/plain", "not found")
            except Exception as e:
                self._send(500, "text/plain", str(e))

        def do_POST(self):
            if not self._auth_ok():
                self._deny()
                return
            p = self.path.split("?")[0]
            if p == "/llbot" or p.startswith("/llbot/"):
                self._proxy_llbot(p)
                return
            if p == "/maibot" or p.startswith("/maibot/"):
                self._proxy_maibot(p)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                if p == "/api/restart":
                    self._send(200, "application/json", json.dumps(api.restart_bot(), ensure_ascii=False))
                elif p == "/api/group_settings":
                    self._send(200, "application/json", json.dumps(api.save_group_settings(body), ensure_ascii=False))
                elif p == "/api/ui_settings":
                    self._send(200, "application/json", json.dumps(api.save_ui_settings(body), ensure_ascii=False))
                else:
                    self._send(404, "text/plain", "not found")
            except Exception as e:
                self._send(500, "text/plain", str(e))

        def log_message(self, *args):
            pass

    server_cls = getattr(http.server, "ThreadingHTTPServer", None)
    if server_cls is None:
        class server_cls(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
    httpd = server_cls((host, port), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 12, 52, 52), fill=(38, 130, 220, 255))
    d.polygon([(48, 26), (62, 16), (62, 36)], fill=(38, 130, 220, 255))
    d.ellipse((18, 26, 24, 32), fill=(255, 255, 255, 255))
    return img


def load_icon():
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("napcat.ico", "napcat_icon.png"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                pass
    return make_tray_image()


HTML_UI = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<script>document.documentElement.dataset.theme='__DSH_THEME__';</script>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
#app{display:flex;height:100vh;height:100dvh}
#sidebar{width:200px;background:#ffffff;color:#4a5568;display:flex;flex-direction:column;flex-shrink:0;min-height:0;border-right:1px solid #e6eaf2}
.brand{padding:18px 16px;font-size:15px;font-weight:600;color:#222a3a;border-bottom:1px solid #eef1f6;flex:none}
nav{padding:10px 8px;display:flex;flex-direction:column;gap:4px;flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:#c7d2e4 transparent}
nav::-webkit-scrollbar{width:6px}
nav::-webkit-scrollbar-thumb{background:#c7d2e4;border-radius:6px}
nav::-webkit-scrollbar-track{background:transparent}
.nav-btn{text-align:left;padding:10px 12px;border:none;background:transparent;color:#5b6678;border-radius:8px;cursor:pointer;font-size:14px}
.nav-btn:hover{background:#f2f5fb;color:#222a3a}
.nav-btn.active{background:#eef4ff;color:#2f6fed;font-weight:600}
.foot{margin-top:0;padding:12px 16px;font-size:12px;color:#8a94a6;flex:none}
#content{flex:1;overflow:hidden;display:flex;flex-direction:column;background:#f5f7fb}
.view{display:none;flex:1;overflow:auto}
.view.active{display:flex}
#napcat-frame{flex:1;border:none;width:100%;height:100%}
#maibot-frame{flex:1;border:none;width:100%;height:100%}
#view-maibot{flex-direction:column}
.panel-pad{padding:20px 24px;width:100%}
h2{font-size:16px;margin:0 0 14px;color:#222a3a}
.card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}
.ev-card .ev-head{display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer;user-select:none;margin-bottom:8px}
.ev-card .ev-head:hover .ev-title{color:#2f6fed}
.ev-card .ev-title{font-weight:600;font-size:14px;transition:color .15s}
.ev-card .ev-arrow{color:#8a94a6;font-size:13px;transition:transform .15s;flex:none}
.ev-card.collapsed .ev-arrow{transform:rotate(-90deg)}
.ev-card.collapsed .ev-body{display:none}
.ev-card .ev-body{padding-top:2px}
.ev-card .ev-body .hint{margin-top:6px}
.ev-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:-2px 0 12px}
.ev-search{flex:0 1 260px;min-width:150px;border:1px solid #d6dbe5;border-radius:6px;padding:6px 9px;font-size:13px;background:#fff;outline:none}
.ev-search:focus{border-color:#2f6fed}
.ev-head-main{min-width:0;flex:1}
.ev-sub{font-size:11.5px;color:#8a94a6;margin-top:3px}
.ev-stats{display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:flex-end;max-width:48%}
.ev-stat{font-size:11px;border:1px solid #dfe5ef;border-radius:999px;padding:1px 7px;white-space:nowrap;background:#fff;color:#6b7688}
.ev-day{font-size:11.5px;font-weight:600;color:#8a94a6;margin:12px 0 2px;letter-spacing:.3px}
.ev-day:first-child{margin-top:2px}
.ev-item{display:grid;grid-template-columns:44px minmax(0,1fr);gap:10px;padding:7px 0;border-top:1px solid #f0f3f8}
.ev-day + .ev-item{border-top:none}
.ev-time{font-size:12px;color:#9aa4b5;font-variant-numeric:tabular-nums;padding-top:2px}
.ev-line{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;font-size:13px;line-height:1.55}
.ev-emo{font-weight:700;white-space:nowrap}
.ev-emo small{font-weight:500;color:#8a94a6;font-size:11px;margin-left:2px}
.ev-chip{font-size:11.5px;border-radius:4px;padding:0 5px;white-space:nowrap}
.ev-chip.topic{color:#2f6fed;background:#eef3fb}
.ev-chip.skill{color:#6b7688;background:#f2f4f8}
.ev-sum{flex:1;min-width:200px;color:#3a4456}
.ev-meta{display:flex;gap:12px;flex-wrap:wrap;margin-top:3px;font-size:11.5px;color:#9aa4b5}
.ev-q{margin:3px 0 0;font-size:12px;color:#b9770e}
.ev-more{margin-top:8px;background:#f2f5fb;border:1px solid #e3e9f4;border-radius:7px;color:#2f6fed;font-size:12px;padding:6px 10px;cursor:pointer;width:100%}
.ev-more:hover{background:#e9f0ff}
.ev-profs{margin-top:10px;font-size:12px;color:#6b7688}
.ev-profs summary{cursor:pointer;color:#8a94a6;font-size:12px;margin-bottom:2px}
.ev-prof{padding:4px 0;border-top:1px dashed #eef1f6;line-height:1.6}
html[data-theme="dark"] .ev-sub,html[data-theme="dark"] .ev-meta,html[data-theme="dark"] .ev-time{color:#8a96aa}
html[data-theme="dark"] .ev-stat{background:#171d28;border-color:#2a3446;color:#aab4c6}
html[data-theme="dark"] .ev-item{border-top-color:#232b39}
html[data-theme="dark"] .ev-day{color:#8a96aa}
html[data-theme="dark"] .ev-sum{color:#dbe3f0}
html[data-theme="dark"] .ev-chip.topic{background:#1a2942;color:#8fb4ff}
html[data-theme="dark"] .ev-chip.skill{background:#222a38;color:#aab4c6}
html[data-theme="dark"] .ev-more{background:#1a2333;border-color:#2a3446;color:#8fb4ff}
html[data-theme="dark"] .ev-more:hover{background:#1e2a40}
html[data-theme="dark"] .ev-q{color:#e0a33a}
html[data-theme="dark"] .ev-prof{border-top-color:#232b39}
.tp-card .tp-head{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;margin-bottom:6px}
.tp-card .tp-title{font-weight:600;font-size:14px;color:#222a3a}
.tp-card .tp-sub{font-size:11.5px;color:#8a94a6}
.tp-card .tp-arrow{color:#8a94a6;font-size:13px;flex:none;transition:transform .15s;margin-left:auto}
.tp-card.collapsed .tp-arrow{transform:rotate(-90deg)}
.tp-card.collapsed .tp-body{display:none}
.tp-row{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #f0f3f8;font-size:13px}
.tp-row.fading{opacity:.55}
.tp-name{min-width:150px;font-weight:600;color:#3a4456}
.tp-bar{flex:1;background:#eef1f6;border-radius:4px;height:8px;overflow:hidden}
.tp-bar i{display:block;height:8px;border-radius:4px}
.tp-val{min-width:96px;color:#6b7688;text-align:right;font-size:12px;font-variant-numeric:tabular-nums}
.tp-meta{min-width:120px;color:#9aa4b5;font-size:11px;text-align:right}
.tp-forgot{margin-top:10px;font-size:12px;color:#6b7688}
.tp-forgot summary{cursor:pointer;color:#8a94a6;font-size:12px;margin-bottom:2px}
.tp-forgot .tp-frow{padding:3px 0;border-top:1px dashed #eef1f6;line-height:1.6}
html[data-theme="dark"] .tp-card .tp-title{color:#e6ebf5}
html[data-theme="dark"] .tp-card .tp-sub,html[data-theme="dark"] .tp-meta,html[data-theme="dark"] .tp-val{color:#8a96aa}
html[data-theme="dark"] .tp-row{border-top-color:#232b39}
html[data-theme="dark"] .tp-name{color:#dbe3f0}
html[data-theme="dark"] .tp-bar{background:#232b39}
html[data-theme="dark"] .tp-forgot{color:#aab4c6}
html[data-theme="dark"] .tp-frow{border-top-color:#232b39}
.prof-head td{background:#eef3fb;padding:0}
.prof-head-in{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;user-select:none;border-radius:6px}
.prof-head-in:hover{background:#e4eeff}
.prof-arrow{color:#2f6fed;font-size:14px;font-weight:700;line-height:1;width:14px;text-align:center;flex:none}
.prof-head-label{font-weight:700;color:#2f6fed;font-size:13px}
.prof-head-meta{margin-left:auto;font-size:11.5px;color:#8a94a6;font-weight:400}
.prof-row.prof-hidden{display:none}
.prof-notes{font-size:12.5px;line-height:1.65;color:#3a4456}
.prof-notes.collapsed .prof-extra{display:none}
.prof-more{margin-top:4px;background:none;border:none;padding:0;color:#2f6fed;font-size:11.5px;cursor:pointer}
.prof-more:hover{text-decoration:underline}
html[data-theme="dark"] .prof-head td{background:#1a2942}
html[data-theme="dark"] .prof-head-in:hover{background:#20304c}
html[data-theme="dark"] .prof-head-label{color:#8fb4ff}
html[data-theme="dark"] .prof-head-meta{color:#8a96aa}
html[data-theme="dark"] .prof-notes{color:#c9d4e6}
html[data-theme="dark"] .prof-more{color:#8fb4ff}
html[data-theme="dark"] .ev-card .ev-head:hover .ev-title{color:#7ea6ff}
html[data-theme="dark"] .ev-card .ev-arrow{color:#8a96aa}
.kv{display:grid;grid-template-columns:120px 1fr;row-gap:10px;font-size:14px}
.kv .k{color:#6b7688}
.kv .v{color:#222a3a;font-weight:500}
button.act{background:#2f6fed;color:#fff;border:none;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px}
button.act:hover{background:#255cc8}
textarea{width:100%;min-height:320px;border:1px solid #d6dbe5;border-radius:8px;padding:10px;font-family:Consolas,monospace;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #edf0f5;vertical-align:top}
th{color:#6b7688;font-weight:500}
td input{width:100%;border:1px solid #d6dbe5;border-radius:6px;padding:6px 8px;font-size:13px}
select{border:1px solid #d6dbe5;border-radius:6px;padding:6px 8px;font-size:13px;background:#fff}
.hint{font-size:12px;color:#8a94a6;margin-top:10px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#222a3a;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
#storage-banner{display:none;align-items:center;gap:10px;padding:9px 16px;font-size:13px;background:#fff4e5;color:#8a4b00;border-bottom:1px solid #f0d9b5;flex:0 0 auto}
#storage-banner.crit{background:#fdecea;color:#a01b16;border-bottom-color:#f5c2c0}
#storage-banner .sb-text{flex:1}
#storage-banner button{background:transparent;border:none;color:inherit;cursor:pointer;font-size:16px;line-height:1;padding:0 4px}
#llbot-banner{display:none;align-items:center;gap:10px;padding:9px 16px;font-size:13px;background:#fff4e5;color:#8a4b00;border-bottom:1px solid #f0d9b5;flex:0 0 auto}
#llbot-banner .lb-text{flex:1}
#llbot-banner button{background:#d68910;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap}
#llbot-banner button.ghost{background:transparent;color:#8a4b00;border:1px solid #e0b978}
#llbot-banner.crit{background:#fdecea;color:#a01b16;border-bottom-color:#f5c2c0}
html[data-theme="dark"] #llbot-banner{background:#3a2a12;color:#f0c27a;border-bottom-color:#5a4218}
html[data-theme="dark"] #llbot-banner.crit{background:#3a1614;color:#ff9a92;border-bottom-color:#5c2420}
html[data-theme="dark"] #llbot-banner button.ghost{color:#f0c27a;border-color:#6a4f20}
.sb-bar{height:8px;background:#eef1f6;border-radius:4px;overflow:hidden;margin-top:4px}
.sb-bar i{display:block;height:8px;border-radius:4px}
.sb-row{display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid #f0f3f8;font-size:13px}
.sb-row .sb-name{min-width:130px;font-weight:600;color:#3a4456}
.sb-row .sb-num{min-width:150px;color:#6b7688;text-align:right;font-variant-numeric:tabular-nums}
.sb-row .sb-wrap{flex:1}
.en-card{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:12px}
.en-head{display:flex;align-items:baseline;gap:10px;font-size:14px;color:#222a3a}
.en-head b{font-size:15px}
.en-tier{font-size:12px;padding:1px 8px;border-radius:999px;background:#eef3fb;color:#2f6fed}
.en-tier.warn{background:#fdf1e3;color:#b26a00}
.en-tier.crit{background:#fdecea;color:#a01b16}
.en-num{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;margin:6px 0 2px;color:#222a3a}
.en-num span{font-size:13px;font-weight:400;color:#8a94a6;margin-left:4px}
.en-meta{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:#6b7688;margin-top:8px}
.en-spark{width:100%;height:48px;margin-top:10px;display:block}
.en-grid{stroke:#e8edf5;stroke-width:1;stroke-dasharray:3 3}
.en-grid-v{stroke:#f0f3f8;stroke-width:1;stroke-dasharray:2 4}
html[data-theme="dark"] .en-grid{stroke:#2a3444}
html[data-theme="dark"] .en-grid-v{stroke:#1f2733}
.en-axis{display:flex;justify-content:space-between;font-size:10px;color:#9aa6b6;margin-top:2px;letter-spacing:.02em}
html[data-theme="dark"] .en-axis{color:#7d8899}
html[data-theme="dark"] .en-card{background:#161c26;box-shadow:none}
html[data-theme="dark"] .en-head,html[data-theme="dark"] .en-num{color:#e6ebf5}
html[data-theme="dark"] .en-meta{color:#8a96aa}
.net-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.net-tab{padding:6px 12px;border:1px solid #d6dbe5;background:#fff;border-radius:8px;cursor:pointer;font-size:13px;color:#4a5568}
.net-tab.active{background:#2f6fed;color:#fff;border-color:#2f6fed}
.net-toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:10px;font-size:13px;color:#4a5568}
#net-canvas-wrap{width:100%;height:560px;background:radial-gradient(circle at 50% 45%,#ffffff 0%,#f4f7fd 70%,#edf2fa 100%);border:1px solid #e6eaf2;border-radius:12px;overflow:hidden}
#net-svg{width:100%;height:100%;display:block;cursor:grab;user-select:none}
.net-node{cursor:pointer}
.net-node text{pointer-events:none;font-size:10px;fill:#4a5568;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}
.net-node.note text{opacity:0}
.net-node.note:hover text{opacity:1}
.net-node.dim{opacity:.12}
.net-link{stroke-linecap:round}
.net-link.dim{opacity:.06}
.net-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:#6b7688;margin-top:10px}
.net-legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:middle}
#net-detail{min-height:34px;margin-top:10px;font-size:13px;color:#4a5568}
#net-detail b{color:#222a3a}
.tok-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.tok-card{background:#fff;border-radius:10px;padding:12px 14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.tok-card .k{font-size:12px;color:#8a94a6;margin-bottom:6px}
.tok-card .v{font-size:19px;font-weight:600;color:#222a3a;font-variant-numeric:tabular-nums}
.tok-card .s{font-size:11px;color:#8a94a6;margin-top:4px}
.tok-tabs{display:flex;gap:8px;margin-bottom:10px}
.tok-tab{padding:5px 12px;border:1px solid #d6dbe5;background:#fff;border-radius:8px;cursor:pointer;font-size:12px;color:#4a5568}
.tok-tab.active{background:#2f6fed;color:#fff;border-color:#2f6fed}
#tok-table td.num,#tok-table th.num{text-align:right;font-variant-numeric:tabular-nums}
.tok-bar{height:6px;background:#eef1f6;border-radius:3px;overflow:hidden;margin-top:5px}
.tok-bar i{display:block;height:6px;background:#2f6fed;border-radius:3px}
.side-bottom{margin-top:4px;padding:6px 8px 0;flex:none}
.settings-btn{display:flex;align-items:center;gap:8px}
.settings-btn svg{flex-shrink:0}
.foot{margin-top:8px}
.setting-row{display:flex;align-items:center;justify-content:space-between;gap:20px}
.setting-title{font-size:14px;font-weight:600;color:#222a3a;margin-bottom:4px}
.switch{position:relative;display:inline-block;width:44px;height:24px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.switch .slider{position:absolute;cursor:pointer;inset:0;background:#d6dbe5;border-radius:24px;transition:.2s}
.switch .slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked + .slider{background:#2f6fed}
.switch input:checked + .slider:before{transform:translateX(20px)}
html[data-theme="dark"]{color-scheme:dark}
html[data-theme="dark"] body{background:#0f131a;color:#e6ebf5}
html[data-theme="dark"] #sidebar{background:#151a24;border-right-color:#252c3a}
html[data-theme="dark"] #side-keys code{background:#1d2430;color:#c8d1e0}
html[data-theme="dark"] .brand{color:#f2f5fa;border-bottom-color:#252c3a}
html[data-theme="dark"] .nav-btn{color:#aab4c6}
html[data-theme="dark"] .nav-btn:hover{background:#222a38;color:#fff}
html[data-theme="dark"] .nav-btn.active{background:#1e2f52;color:#8fb4ff}
html[data-theme="dark"] .foot{color:#6f7b8f}
html[data-theme="dark"] #content{background:#0f131a}
html[data-theme="dark"] h2{color:#e8edf7}
html[data-theme="dark"] .card,html[data-theme="dark"] .tok-card{background:#171d28;box-shadow:0 1px 3px rgba(0,0,0,.4)}
html[data-theme="dark"] .kv .k,html[data-theme="dark"] .hint,html[data-theme="dark"] th,html[data-theme="dark"] .tok-card .k,html[data-theme="dark"] .tok-card .s{color:#8a96aa}
html[data-theme="dark"] .kv .v,html[data-theme="dark"] td,html[data-theme="dark"] .tok-card .v,html[data-theme="dark"] .setting-title{color:#e6ebf5}
html[data-theme="dark"] th,html[data-theme="dark"] td{border-color:#232b39}
html[data-theme="dark"] input,html[data-theme="dark"] textarea,html[data-theme="dark"] select{background:#111722;color:#e6ebf5;border-color:#2a3446}
html[data-theme="dark"] .tok-bar{background:#232b39}
html[data-theme="dark"] #net-canvas-wrap{background:radial-gradient(circle at 50% 45%,#151b26 0%,#0f131a 70%,#0b0f16 100%);border-color:#242c3a}
html[data-theme="dark"] .net-node text{fill:#c9d4e6!important;stroke:#0f131a!important}
html[data-theme="dark"] .net-link{stroke:#2c3a52!important}
html[data-theme="dark"] .net-tab,html[data-theme="dark"] .tok-tab{background:#171d28;border-color:#2a3446;color:#b8c4d8}
html[data-theme="dark"] .net-tab.active,html[data-theme="dark"] .tok-tab.active{background:#2f6fed;color:#fff}
html[data-theme="dark"] .net-legend,html[data-theme="dark"] #net-detail{color:#8a96aa}
html[data-theme="dark"] #net-detail b{color:#e6ebf5}
html[data-theme="dark"] .switch .slider{background:#2a3446}
html[data-theme="dark"] .maibot-bar{background:#171d28!important;border-bottom-color:#252c3a!important}
html[data-theme="dark"] .maibot-bar span{color:#8a96aa!important}
html[data-theme="dark"] #maibot-token{background:#111722!important;color:#e6ebf5!important}
#groups-table .grp-energy,#new-group-energy{background:#fff;color:#222a3a}
html[data-theme="dark"] #groups-table .grp-energy,html[data-theme="dark"] #new-group-energy{background:#1b2230;color:#e6ebf5;border-color:#3a465c!important}
@media (max-width:760px){
  html,body{max-width:100%;overflow-x:hidden}
  #app{flex-direction:column;width:100%;max-width:100vw;overflow:hidden}
  #sidebar{width:100%;height:auto;flex-direction:column;border-right:none;border-bottom:1px solid #e6eaf2;padding:8px 8px 4px;gap:4px}
  #sidebar{width:100%!important;max-width:100%;overflow-x:hidden}
  #sidebar .brand,#sidebar #side-keys,#sidebar .foot{display:none!important}
  #sidebar nav{display:flex!important;flex-direction:column!important;flex-wrap:nowrap!important;gap:4px;padding:0;width:100%!important;max-width:100%!important;overflow-y:auto!important;overflow-x:hidden!important;max-height:40vh;-webkit-overflow-scrolling:touch;touch-action:pan-y;overscroll-behavior:contain}
  #sidebar .nav-btn{font-size:13px;padding:10px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:left;border-radius:9px;flex:none!important;min-width:0!important;width:100%!important;max-width:100%!important}
  #sidebar .side-bottom{margin-top:2px;padding:2px 0 0;display:flex}
  #sidebar .side-bottom .nav-btn{font-size:11.5px;padding:6px 10px}
  #content{min-width:0;min-height:0}
  #content{max-width:100vw;overflow-x:hidden}
  .panel-pad{padding:12px;max-width:100%;overflow-x:hidden}
  h2{font-size:15px;margin-bottom:10px}
  .card{padding:13px;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,.16);margin-bottom:12px;max-width:100%;overflow-wrap:anywhere}
  table{display:block;max-width:100%;overflow-x:auto;white-space:normal}
  .sb-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 10px;padding:9px 0}
  .sb-row .sb-name{min-width:0;font-size:13px}
  .sb-row .sb-num{min-width:0;font-size:12px}
  .sb-row .sb-wrap{grid-column:1 / -1;width:100%}
  .sb-bar{height:10px;border-radius:6px}
  .sb-bar i{height:10px;border-radius:6px}
  .en-card{padding:14px;border-radius:14px}
  .en-num{font-size:36px}
  .en-meta{gap:8px 14px;font-size:11.5px}
  .ev-card .ev-head{flex-wrap:wrap}
  .ev-stats{max-width:100%;justify-content:flex-start}
  .ev-item{grid-template-columns:38px minmax(0,1fr);gap:7px}
  .tp-meta{display:none}
  .tok-cards{grid-template-columns:repeat(2,minmax(0,1fr))}
  .tok-card .v{font-size:17px}
  #groups-table input{width:78px}
  table{font-size:12px}
}
/* ===== UI refresh: top bar, grouped nav, mobile drawer ===== */
#sidebar{width:216px;background:#fbfcff;border-right:1px solid #e8edf6}
.brand{padding:17px 16px 13px;font-size:15.5px;letter-spacing:.01em}
nav{padding:4px 10px 10px;gap:2px}
.nav-group-title{font-size:10px;letter-spacing:.1em;color:#98a2b3;padding:12px 8px 4px;font-weight:700;text-transform:uppercase}
.nav-group-title:first-child{padding-top:4px}
.nav-btn{position:relative;padding:9px 12px;border-radius:10px;font-size:13.5px;transition:background .15s,color .15s}
.nav-btn.active{background:#eaf1ff;color:#245fd6;font-weight:700;box-shadow:inset 0 0 0 1px #dbe7ff}
.nav-btn.active::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:3px;border-radius:3px;background:#2f6fed}
.side-bottom{margin-top:4px;padding:8px 10px 0;border-top:1px solid #eef2f8}
.foot{padding:9px 16px 12px;font-size:11px}
#content{background:#f4f7fc}
#topbar{display:flex;align-items:center;gap:12px;height:56px;padding:0 22px;background:rgba(255,255,255,.94);border-bottom:1px solid #e8edf6;flex:none;z-index:10;backdrop-filter:blur(8px)}
#menu-toggle{display:none;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid #e2e8f2;background:#fff;border-radius:9px;color:#334155;font-size:17px;cursor:pointer}
.tb-title{font-size:15px;font-weight:750;color:#1f2937;line-height:1.2}
.tb-sub{font-size:11px;color:#8a94a6;margin-top:2px}
.tb-spacer{flex:1}
.tb-status{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border-radius:999px;background:#eef8f1;color:#237a46;font-size:11px;font-weight:600;white-space:nowrap}
.tb-dot{width:6px;height:6px;border-radius:50%;background:#2e9d5a;box-shadow:0 0 0 3px rgba(46,157,90,.12)}
.tb-refresh{border:1px solid #dfe6f2;background:#fff;color:#44506a;border-radius:8px;padding:6px 11px;font-size:12px;cursor:pointer}
.tb-refresh:hover{background:#f5f8ff;border-color:#c9d8f5;color:#245fd6}
#nav-backdrop{display:none}
.panel-pad{padding:22px 26px}
.card{border:1px solid #e9eef7;box-shadow:0 2px 10px rgba(30,50,90,.045);border-radius:12px}
html[data-theme="dark"] #topbar{background:rgba(17,23,34,.94);border-bottom-color:#252c3a}
html[data-theme="dark"] .tb-title{color:#eef3fb}
html[data-theme="dark"] .tb-sub{color:#8a96aa}
html[data-theme="dark"] .tb-status{background:#173423;color:#7fd6a1}
html[data-theme="dark"] .tb-refresh{background:#151b28;border-color:#2a3446;color:#b8c4d8}
html[data-theme="dark"] .nav-group-title{color:#6f7b8f}
html[data-theme="dark"] .nav-btn.active{background:#1e2f52;box-shadow:inset 0 0 0 1px #2a4270}
@media (max-width:760px){
  #topbar{display:flex;height:54px;padding:0 10px;position:sticky;top:0}
  #menu-toggle{display:inline-flex}
  #sidebar{position:fixed;left:0;top:0;bottom:0;width:min(82vw,310px);height:100dvh;z-index:60;transform:translateX(-102%);transition:transform .22s ease;box-shadow:18px 0 40px rgba(15,23,42,.25);padding:8px 8px 8px;overflow:hidden}
  #sidebar.open{transform:none}
  #sidebar .brand{display:flex!important}
  #sidebar .foot{display:none!important}
  #sidebar nav{display:flex!important;flex-direction:column!important;max-height:none!important;height:auto!important;flex:1 1 auto;overflow-y:auto!important;padding:2px 4px 6px!important;gap:2px!important;touch-action:pan-y}
  #sidebar .nav-btn{text-align:left!important;width:100%!important;max-width:100%!important;font-size:13.5px;padding:10px 12px!important;white-space:nowrap!important}
  #sidebar .side-bottom{display:flex!important;margin-top:4px}
  #sidebar #side-keys{display:none!important}
  #nav-backdrop.show{display:block;position:fixed;inset:0;background:rgba(10,18,35,.44);z-index:55}
  .panel-pad{padding:14px 12px}
  .tb-sub{display:none}
  .tb-status{font-size:10.5px;padding:4px 8px}
  .tb-refresh{padding:5px 9px}
}
@media (min-width:761px) and (max-width:1024px){
  #sidebar{width:200px}
  .nav-group-title{padding:8px 8px 2px}
}
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="brand">🐋 LLBot 桌面端</div>
    <div id="side-keys" style="padding:0 10px 12px;display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:#8a94a6">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="width:34px;flex:0 0 auto">LLBot</span>
        <code id="llbot-token" style="flex:1;min-width:0;color:#4a5568;background:#f2f4f8;border-radius:5px;padding:2px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;user-select:all" onclick="toggleLlbBotToken()" title="点击显示/隐藏">••••</code>
        <button onclick="copyLlbBotToken()" title="复制密码" style="background:transparent;border:none;cursor:pointer;color:#8a94a6;font-size:13px;padding:0 2px">⧉</button>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="width:34px;flex:0 0 auto">MaiBot</span>
        <code id="maibot-token" style="flex:1;min-width:0;color:#4a5568;background:#f2f4f8;border-radius:5px;padding:2px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;user-select:all" onclick="toggleMaiBotToken()" title="点击显示/隐藏">••••</code>
        <button onclick="copyMaiBotToken()" title="复制密码" style="background:transparent;border:none;cursor:pointer;color:#8a94a6;font-size:13px;padding:0 2px">⧉</button>
      </div>
    </div>
    <nav>
      <div class="nav-group-title">总览</div>
      <button class="nav-btn active" data-view="panel">LLBot 面板</button>
      <button class="nav-btn" data-view="status">运行状态</button>
      <button class="nav-btn" data-view="energy">精力</button>
      <div class="nav-group-title">记忆</div>
      <button class="nav-btn" data-view="profiles">人物档案</button>
      <button class="nav-btn" data-view="events">事件记忆</button>
      <button class="nav-btn" data-view="topics">话题兴趣</button>
      <button class="nav-btn" data-view="network">记忆网络</button>
      <button class="nav-btn" data-view="experiences">经验</button>
      <div class="nav-group-title">数据与接入</div>
      <button class="nav-btn" data-view="tokens">Token 统计</button>
      <button class="nav-btn" data-view="affinity">人物好感度</button>
      <button class="nav-btn" data-view="groups">会话设置</button>
      <button class="nav-btn" data-view="maibot">MaiBot 面板</button>
      <button class="nav-btn" data-view="changelog">更新日志</button>
    </nav>
    <div class="side-bottom">
      <button class="nav-btn settings-btn" data-view="settings" title="设置">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
        <span>设置</span>
      </button>
    </div>
    <div class="foot">V4flash · 大肥鱼</div>
  </aside>
  <main id="content">
    <header id="topbar">
      <button id="menu-toggle" aria-label="菜单" onclick="setMenu(true)">☰</button>
      <div><div class="tb-title" id="page-title">LLBot 面板</div><div class="tb-sub" id="page-sub">协议端控制台</div></div>
      <div class="tb-spacer"></div>
      <div class="tb-status"><span class="tb-dot"></span><span>本地面板</span></div>
      <button class="tb-refresh" onclick="refreshCurrent()">刷新</button>
    </header>
    <div id="nav-backdrop" onclick="setMenu(false)"></div>
    <div id="storage-banner"><span class="sb-text" id="storage-banner-text"></span><button title="本次隐藏" onclick="dismissStorage()">×</button></div>
    <div id="mute-banner" style="display:none;align-items:center;gap:10px;padding:9px 16px;font-size:13px;background:#fdecea;color:#a01b16;border-bottom:1px solid #f5c2c0"><span style="flex:1" id="mute-banner-text"></span><button title="本次隐藏" onclick="dismissMute()" style="background:transparent;border:none;color:inherit;cursor:pointer;font-size:16px">×</button></div>
    <div id="llbot-banner"><span class="lb-text" id="llbot-banner-text"></span><button onclick="openLlbBotPanel()">打开 LLBot 面板</button><button class="ghost" onclick="loadLlbBotStatus()">刷新</button></div>
    <section id="view-panel" class="view active">
      <iframe id="napcat-frame" src="__WEBUI_URL__"></iframe>
    </section>
    <section id="view-status" class="view">
      <div class="panel-pad">
        <h2>运行状态</h2>
        <div class="card">
          <div class="kv">
            <div class="k">大脑</div><div class="v" id="st-model">-</div>
            <div class="k">推理等级</div><div class="v" id="st-effort">-</div>
            <div class="k">人物档案</div><div class="v" id="st-profiles">-</div>
            <div class="k">群聊数</div><div class="v" id="st-groups">-</div>
            <div class="k">隧道</div><div class="v" id="st-tunnel">-</div>
            <div class="k">QQ 链路</div><div class="v" id="st-llbot">-</div>
          </div>
        </div>
        <div class="card">
          <div class="hint" style="margin-top:0">存储容量（每 5 分钟刷新，超阈值会在顶部拉横幅）</div>
          <div class="kv">
            <div class="k">磁盘</div><div class="v" id="st-disk">-</div>
            <div class="k">预警 / 临界</div><div class="v" id="st-disk-th">-</div>
          </div>
          <div class="sb-bar"><i id="st-disk-bar" style="width:0;background:#2e9d5a"></i></div>
          <div id="storage-dirs" style="margin-top:14px"></div>
        </div>
        <div class="card">
          <div class="hint" style="margin-top:0">预警阈值（存本机 ui_settings.json）</div>
          <div class="kv">
            <div class="k">磁盘预警 %</div><div class="v"><input id="sb-warn" type="number" min="1" max="100" style="width:90px"></div>
            <div class="k">磁盘临界 %</div><div class="v"><input id="sb-crit" type="number" min="1" max="100" style="width:90px"></div>
          </div>
          <div id="sb-dir-limits" style="margin-top:12px"></div>
          <div style="margin-top:12px"><button class="act" onclick="saveStorageSettings()">保存阈值</button></div>
          <div class="hint">目录上限单位 MB；超上限拉黄色横幅，超 120% 变红。</div>
        </div>
        <div class="card">
          <div class="hint" style="margin-top:0">精力（每群独立 · token 消耗驱动，每分钟结算一次）</div>
          <div id="energy-list"></div>
          <div class="hint">70 以上精神不错；40 以下开始少说话；18 以下基本只回点名；6 以下见底，会在群里说一句。</div>
        </div>
        <div class="card">
          <button class="act" onclick="restartBot()">重启记忆服务</button>
          <div class="hint">重启 memwatch：事件记忆、好感度、记忆网络会一起重载。</div>
        </div>
      </div>
    </section>
    <section id="view-energy" class="view">
      <div class="panel-pad">
        <h2>精力（每群独立 · token 消耗驱动）</h2>
        <div class="card">
          <div class="hint" style="margin-top:0">结算规则（每 60 秒一次）</div>
          <div class="kv" style="font-size:12.5px;line-height:1.7">
            <div class="k">消耗</div><div class="v">0.03 × 千 token × 心情因子</div>
            <div class="k">心情因子</div><div class="v">1 − 0.35·valence + 0.10·arousal（0.6 ~ 1.6）</div>
            <div class="k">恢复</div><div class="v">(100 − E) × (1 − e<sup>−Δt/τ</sup>)，τ=4h，夜里 22:00-08:00 τ=2h</div>
          </div>
        </div>
        <div class="net-tabs conv-filter" style="margin-bottom:10px"><button class="net-tab active" data-scope="all" onclick="setConvFilter('all')">全部</button><button class="net-tab" data-scope="group" onclick="setConvFilter('group')">群聊</button><button class="net-tab" data-scope="private" onclick="setConvFilter('private')">私聊</button></div>
        <div id="energy-cards"></div>
        <div class="hint">档位：≥70 精神不错 · ≥40 有点累（少接话）· ≥18 很累（只回点名）· ≥6 快没电（除非点名不发言）· &lt;6 没电（群里说一句，6 小时冷却）</div>
      </div>
    </section>
    <section id="view-profiles" class="view">
      <div class="panel-pad">
        <h2>人物档案（群 · 私聊）</h2>
        <div class="card">
          <div class="net-tabs" id="prof-filter" style="margin-bottom:10px">
            <button class="net-tab active" data-scope="all" onclick="setProfFilter('all')">全部</button>
            <button class="net-tab" data-scope="group" onclick="setProfFilter('group')">群聊</button>
            <button class="net-tab" data-scope="private" onclick="setProfFilter('private')">私聊</button>
            <button class="net-tab" id="prof-fold-all" style="margin-left:auto" onclick="toggleAllProfiles()">全部折叠</button>
          </div>
          <table id="profiles-table">
            <thead><tr><th style="width:190px">昵称</th><th>印象（机器人对这些人的长期记录）</th><th style="width:120px">最近更新</th></tr></thead>
            <tbody id="profiles-body"></tbody>
          </table>
          <div class="hint">只读。数据源是 memwatch 记忆服务（<code>/opt/memwatch/data/groups/*.json</code>），群里聊着就自动长，每 20 秒同步一次。点群/私聊标题折叠整组；印象超过 3 条时可点「展开全部」。<span id="profiles-sync" style="margin-left:8px;color:#2f6fed"></span></div>
          <div style="margin-top:12px"><button class="act" onclick="reloadProfilesNow()">重新读取</button></div>
        </div>
      </div>
    </section>
    <section id="view-events" class="view">
      <div class="panel-pad">
        <h2>事件记忆（群 / 私聊隔离 · 心情加权）</h2>
        <div class="net-tabs conv-filter" style="margin-bottom:10px"><button class="net-tab active" data-scope="all" onclick="setConvFilter('all')">全部</button><button class="net-tab" data-scope="group" onclick="setConvFilter('group')">群聊</button><button class="net-tab" data-scope="private" onclick="setConvFilter('private')">私聊</button><button class="net-tab" id="ev-fold-all" style="margin-left:auto" onclick="toggleAllEvents()">全部折叠</button></div>
        <div class="ev-toolbar">
          <select id="ev-emo" onchange="setEvEmotion(this.value)" title="按情绪筛选">
            <option value="all">全部情绪</option>
            <option value="joy">高兴</option><option value="trust">信任</option><option value="anticipation">期待</option><option value="surprise">惊喜</option><option value="fear">害怕</option><option value="sadness">伤心</option><option value="disgust">嫌弃</option><option value="anger">生气</option><option value="confusion">困惑</option><option value="neutral">平静</option>
          </select>
          <input id="ev-search" class="ev-search" type="search" placeholder="搜事件 / 话题 / 疑问 / 人物" oninput="onEvSearch(this.value)">
          <span id="ev-count" class="hint" style="margin:0"></span>
        </div>
        <div id="events-list"></div>
        <div class="hint">按时间线从新到旧排列，默认每个会话显示最近 20 条；积极记忆权重高、伤心记忆低；每 24h 衰减一半，激活度低于 0.06 进遗忘区留档。</div>
      </div>
    </section>
    <section id="view-topics" class="view">
      <div class="panel-pad">
        <h2>话题兴趣（群 / 私聊隔离 · 心情加权）</h2>
        <div class="net-tabs conv-filter" style="margin-bottom:10px"><button class="net-tab active" data-scope="all" onclick="setConvFilter('all')">全部</button><button class="net-tab" data-scope="group" onclick="setConvFilter('group')">群聊</button><button class="net-tab" data-scope="private" onclick="setConvFilter('private')">私聊</button><button class="net-tab" id="tp-fold-all" style="margin-left:auto" onclick="toggleAllTopics()">全部折叠</button></div>
        <div id="topics-list"></div>
        <div class="hint">话题按事件心情权重加减兴趣，按艾宾浩斯衰减；兴趣 ≥3 标记「感兴趣」（★），激活度低于 0.05 直接删除、不再进画像。点会话标题可折叠。</div>
      </div>
    </section>
    <section id="view-network" class="view">
      <div class="panel-pad">
        <h2>记忆网络（群 / 私聊隔离 · 人 / 事件 / 印象）</h2>
        <div class="card">
          <div class="net-tabs conv-filter" style="margin-bottom:10px"><button class="net-tab active" data-scope="all" onclick="setConvFilter('all')">全部</button><button class="net-tab" data-scope="group" onclick="setConvFilter('group')">群聊</button><button class="net-tab" data-scope="private" onclick="setConvFilter('private')">私聊</button></div>
          <div id="net-groups" class="net-tabs"></div>
          <div class="net-toolbar">
            <label><input type="checkbox" id="net-show-notes" checked onchange="renderNetwork(netActiveGid)"> 显示印象节点</label>
            <span id="net-stats" class="hint" style="margin:0"></span>
            <button class="act" onclick="loadNetwork()" style="padding:5px 12px;font-size:12px">刷新网络</button>
            <span style="display:flex;gap:6px;align-items:center;margin-left:auto">
              <button class="act" onclick="netZoom(1.2)" title="放大" style="padding:3px 10px;font-size:13px">＋</button>
              <button class="act" onclick="netZoom(0.83)" title="缩小" style="padding:3px 10px;font-size:13px">－</button>
              <button class="act" onclick="netZoomFit()" title="缩放到刚好装下所有节点" style="padding:5px 12px;font-size:12px">适应</button>
              <button class="act" onclick="netZoomReset()" title="100%" style="padding:5px 12px;font-size:12px">100%</button>
              <span id="net-zoom" class="hint" style="margin:0;min-width:42px;text-align:right">100%</span>
            </span>
          </div>
          <div id="net-canvas-wrap"><svg id="net-svg" viewBox="0 0 900 560" preserveAspectRatio="xMidYMid meet"></svg></div>
          <div class="net-legend">
            <span><i style="background:#2f6fed"></i>人物</span>
            <span><i style="background:#2e9d5a"></i>高兴/信任</span>
            <span><i style="background:#3a9ad9"></i>期待</span>
            <span><i style="background:#9b6bd6"></i>惊喜</span>
            <span><i style="background:#d68910"></i>困惑</span>
            <span><i style="background:#c0392b"></i>生气/害怕/伤心</span>
            <span><i style="background:#d68910"></i>话题</span>
            <span><i style="background:#9b6bd6"></i>印象</span>
            <span><i style="background:#8a94a6;opacity:.3"></i>淡忘（激活度 &lt; 0.06）</span>
          </div>
          <div id="net-detail" class="hint" style="margin-top:8px">滚轮缩放、空白处拖动平移、双击复位；点节点看详情，拖节点可以摆位置。节点越淡＝越接近想不起来（艾宾浩斯 × 情绪权重 × 关联度），低于阈值的不再画。</div>
        </div>
      </div>
    </section>
    <section id="view-experiences" class="view">
      <div class="panel-pad">
        <h2>经验（全局共享 · 不按群隔离）</h2>
        <div class="hint" style="margin-bottom:10px">记录「原来不懂、后来搞懂」的知识：困惑时联网查到的，或后来被群友解释清楚的。所有群和私聊共用同一份经验。</div>
        <div id="experiences-list"></div>
      </div>
    </section>
    <section id="view-tokens" class="view">
      <div class="panel-pad">
        <h2>Token 统计（群 / 私聊分开 · 实时）</h2>
        <div class="tok-cards">
          <div class="tok-card"><div class="k">DeepSeek 余额</div><div class="v" id="tok-balance">-</div><div class="s" id="tok-balance-s">读取中</div></div>
          <div class="tok-card"><div class="k">SiliconFlow 余额</div><div class="v" id="tok-sf-balance">-</div><div class="s" id="tok-sf-balance-s">读取中</div></div>
          <div class="tok-card"><div class="k" id="tok-period-k">今日消费</div><div class="v" id="tok-cost-period">-</div><div class="s">MaiBot + 事件记忆 + SF 付费部分</div></div>
          <div class="tok-card"><div class="k" id="tok-tokens-k">今日 Tokens</div><div class="v" id="tok-tokens-period">-</div><div class="s" id="tok-req-period">-</div></div>
          <div class="tok-card"><div class="k">累计消费</div><div class="v" id="tok-costall">-</div><div class="s">自开始记账</div></div>
          <div class="tok-card"><div class="k">累计 Tokens</div><div class="v" id="tok-tokensall">-</div><div class="s" id="tok-reqall">-</div></div>
        </div>
        <div class="card">
          <div class="net-toolbar">
            <div class="tok-tabs">
              <button class="tok-tab active" id="tok-tab-day" onclick="setTokenPeriod('day')">今日</button>
              <button class="tok-tab" id="tok-tab-week" onclick="setTokenPeriod('week')">本周</button>
              <button class="tok-tab" id="tok-tab-month" onclick="setTokenPeriod('month')">本月</button>
              <button class="tok-tab" id="tok-tab-all" onclick="setTokenPeriod('all')">累计</button>
            </div>
            <div class="tok-tabs" id="tok-scope-tabs">
              <button class="tok-tab active" data-scope="all" onclick="setTokScope('all')">全部</button>
              <button class="tok-tab" data-scope="group" onclick="setTokScope('group')">群聊</button>
              <button class="tok-tab" data-scope="private" onclick="setTokScope('private')">私聊</button>
            </div>
            <button class="act" onclick="loadTokenStats()" style="padding:5px 12px;font-size:12px">刷新</button>
            <span class="hint" style="margin:0" id="tok-updated">-</span>
          </div>
          <table id="tok-table">
            <thead><tr>
              <th style="width:210px">群</th><th class="num">总请求</th>
              <th class="num">输入（命中 / 未命中）</th><th class="num">输出</th>
              <th class="num">总 Tokens</th><th class="num">SiliconFlow</th><th class="num">消费</th><th class="num">事件调用</th>
            </tr></thead>
            <tbody></tbody>
          </table>
          <div class="hint" id="tok-hint">每 30 秒自动刷新；MaiBot 提供 token 用量，DeepSeek 按官方价（含高峰/空闲与缓存命中）重算；SiliconFlow 的 Qwen3-VL 按 ¥0.7/¥2.8 计入，bge-m3 免费；事件记忆单独记账。</div>
          <div class="hint" style="margin-top:4px">计价基准（每百万 tokens）：Flash 输入 ￥1/￥2、输出 ￥4/￥8；Pro 输入 ￥4.5/￥9、输出 ￥13.5/￥27（空闲/高峰）；SiliconFlow Qwen3-VL 输入 ¥0.7、输出 ¥2.8。</div>
        </div>
      </div>
    </section>
    <section id="view-affinity" class="view">
      <div class="panel-pad">
        <h2>人物好感度（群 / 私聊隔离）</h2>
        <div class="net-tabs conv-filter" style="margin-bottom:10px"><button class="net-tab active" data-scope="all" onclick="setConvFilter('all')">全部</button><button class="net-tab" data-scope="group" onclick="setConvFilter('group')">群聊</button><button class="net-tab" data-scope="private" onclick="setConvFilter('private')">私聊</button></div>
        <div id="affinity-list"></div>
        <div class="hint">好感度 0~100，初始 50：事件里出现的人，群心情高兴就加分、伤心就减分；久不互动会慢慢回到 50。数据按会话隔离存在 memwatch；同一个人跨会话分数是统一的。</div>
      </div>
    </section>
    <section id="view-groups" class="view">
      <div class="panel-pad">
        <h2>会话设置 · 推理等级与精力系数（群 / 私聊，各自隔离）</h2>
        <div class="card">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
            <input id="new-group-id" placeholder="群号，或 p+QQ号 加私聊" style="width:180px;border:1px solid #d6dbe5;border-radius:6px;padding:6px 8px;font-size:13px">
            <select id="new-group-effort"><option value="off">off</option><option value="low" selected>low</option><option value="high">high</option><option value="max">max</option></select>
            <input id="new-group-energy" type="number" min="0" step="0.005" value="0.045" title="每 1000 token 扣多少精力" style="width:110px;border:1px solid #d6dbe5;border-radius:6px;padding:6px 8px;font-size:13px">
            <button class="act" onclick="addGroup()">添加会话</button>
          </div>
          <table id="groups-table">
            <thead><tr><th style="width:280px">会话</th><th style="width:150px">推理等级</th><th style="width:170px">精力系数 / 千token</th></tr></thead>
            <tbody></tbody>
          </table>
          <div class="hint">off=不推理 / low=低 / high=高 / max=最高。精力系数是每 1000 token 扣多少精力，默认 0.045，群和私聊都能单独调；改完点保存。</div>
          <div style="margin-top:12px"><button class="act" onclick="saveGroups()">保存设置</button></div>
        </div>
      </div>
    </section>
    <section id="view-maibot" class="view">
      <iframe id="maibot-frame" src="about:blank"></iframe>
    </section>
    <section id="view-changelog" class="view">
      <div class="panel-pad">
        <h2>更新日志</h2>
        <div class="hint" style="margin-bottom:10px">服务器上的实际变更记录：改了什么、为什么、怎么回退。内容来自 <code>/opt/qqbot-panel/CHANGELOG.md</code>。</div>
        <div id="changelog-body" style="font-size:13px;line-height:1.7"></div>
      </div>
    </section>
    <section id="view-settings" class="view">
      <div class="panel-pad">
        <h2>设置</h2>
        <div class="card">
          <div class="setting-row">
            <div>
              <div class="setting-title">深色模式</div>
              <div class="hint" style="margin:0">同时切换软件外壳、NapCat 和 MaiBot</div>
            </div>
            <label class="switch">
              <input type="checkbox" id="theme-dark" onchange="setTheme(this.checked?'dark':'light')">
              <span class="slider"></span>
            </label>
          </div>
        </div>
        <div class="hint">主题偏好保存在本机，下次打开继续用。</div>
      </div>
    </section>
  </main>
</div>
<div id="toast" class="toast"></div>
<script>
function toast(msg){var t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2200);}
function applyTheme(mode,save){mode=mode==='dark'?'dark':'light';document.documentElement.dataset.theme=mode;if(save!==false){try{localStorage.setItem('dsh-theme',mode)}catch(e){}}var cb=document.getElementById('theme-dark');if(cb)cb.checked=(mode==='dark');var msg={type:'dsh-theme',mode:mode};['napcat-frame','maibot-frame'].forEach(function(id){var f=document.getElementById(id);if(f&&f.contentWindow){try{f.contentWindow.postMessage(msg,'*')}catch(e){}}});}
function setTheme(mode){applyTheme(mode,true);fetch('/api/ui_settings',{method:'POST',body:JSON.stringify({theme:mode})}).catch(function(){});}
function loadUiTheme(){fetch('/api/ui_settings').then(function(r){return r.json()}).then(function(d){if(d&&d.theme)applyTheme(d.theme,false);}).catch(function(){});}
function syncTheme(){applyTheme(document.documentElement.dataset.theme||'light',false);}
document.getElementById('napcat-frame').addEventListener('load',syncTheme);
document.getElementById('maibot-frame').addEventListener('load',syncTheme);
window.addEventListener('load',function(){loadUiTheme();syncTheme();setTimeout(syncTheme,1200);setTimeout(syncTheme,3000);});
document.querySelectorAll('.nav-btn').forEach(function(b){b.addEventListener('click',function(){document.querySelectorAll('.nav-btn').forEach(function(x){x.classList.remove('active')});document.querySelectorAll('.view').forEach(function(x){x.classList.remove('active')});b.classList.add('active');document.getElementById('view-'+b.dataset.view).classList.add('active');});});
setInterval(function(){var a=document.querySelector('.nav-btn.active');if(!a)return;var v=a.dataset.view;if(v==='affinity')loadAffinity();else if(v==='events')loadEvents();else if(v==='topics')loadTopics();else if(v==='topics')loadTopics();else if(v==='profiles')refreshProfiles();else if(v==='status')loadStatus();},20000);
function loadStatus(){fetch('/api/status').then(function(r){return r.json()}).then(function(s){document.getElementById('st-model').textContent=s.model_label||s.model||'-';document.getElementById('st-effort').textContent=s.effort||'-';document.getElementById('st-profiles').textContent=(s.profiles_count||0)+' 人';document.getElementById('st-groups').textContent=(s.groups_count||0)+' 个';document.getElementById('st-tunnel').textContent=s.tunnel_ok?'已连接':'未连接';var lb=s.llbot||{};var lbEl=document.getElementById('st-llbot');if(lbEl){if(lb.online){lbEl.textContent='在线';lbEl.style.color='#2e9d5a';}else{var st=lb.state;var txt=st==='need_qr'?'待扫码':(st==='kicked'?'被踢下线':(st==='down'?'容器未运行':(st==='unknown'?'状态未知':'掉线')));if(lb.last_kick&&(st==='kicked'||st==='need_qr'))txt+='（'+new Date(lb.last_kick*1000).toLocaleTimeString()+'）';lbEl.textContent=txt;lbEl.style.color=st==='need_qr'?'#d68910':'#c0392b';}}}).catch(function(){toast('读取状态失败')});}
function openLlbBotPanel(){var b=document.querySelector('.nav-btn[data-view="panel"]');if(b)b.click();}
function loadLlbBotStatus(){fetch('/api/llbot_status').then(function(r){return r.json()}).then(function(s){var b=document.getElementById('llbot-banner'),t=document.getElementById('llbot-banner-text');if(!b||!t)return;var st=(s&&s.state)||'unknown';if(s&&s.online){b.style.display='none';return;}b.classList.toggle('crit',st==='down');if(st==='need_qr'){t.textContent='🐟 QQ 待扫码：大肥鱼掉线了，用手机 QQ 扫 LLBot 面板里的二维码。';b.style.display='flex';}else if(st==='kicked'){t.textContent='QQ 被踢下线，正在等新二维码……';b.style.display='flex';}else if(st==='down'){t.textContent='llbot 容器没在跑，看门狗会尝试拉起。';b.style.display='flex';}else{b.style.display='none';}}).catch(function(){});}
var _storageDismissed=false;
function fmtSize(mb){return mb>=1024?((mb/1024).toFixed(1)+' GB'):(mb+' MB');}
function storageColor(r){return r>=1?'#c0392b':(r>=0.85?'#d68910':'#2e9d5a');}
function renderStorage(s){var banner=document.getElementById('storage-banner'),txt=document.getElementById('storage-banner-text');var alerts=(s&&s.alerts)||[];if(alerts.length&&!_storageDismissed){banner.style.display='flex';banner.className=alerts.some(function(a){return a.level==='crit'})?'crit':'';txt.textContent='⚠ 存储预警：'+alerts.map(function(a){return a.text}).join(' · ');}else{banner.style.display='none';}var d=(s&&s.disk)||{};var dn=document.getElementById('st-disk');if(dn){dn.textContent=(d.total_gb!=null)?(d.used_gb+' / '+d.total_gb+' GB（'+d.pct+'%，剩 '+d.free_gb+' GB）'):'-';}var th=document.getElementById('st-disk-th');if(th){th.textContent=(s&&s.warn!=null)?(s.warn+'% / '+s.crit+'%'):'-';}var bar=document.getElementById('st-disk-bar');if(bar&&d.pct!=null){bar.style.width=Math.min(100,d.pct)+'%';bar.style.background=storageColor(d.pct/100);}var box=document.getElementById('storage-dirs');if(!box)return;var dirs=(s&&s.dirs)||[];if(!dirs.length){box.innerHTML='<div class="hint">'+((s&&s.error)?('读取失败：'+esc(s.error)):'暂无数据')+'</div>';return;}box.innerHTML=dirs.map(function(x){var ratio=x.mb/Math.max(1,x.limit);var color=storageColor(ratio);return '<div class="sb-row"><span class="sb-name">'+esc(x.label)+'</span><span class="sb-wrap"><div class="sb-bar"><i style="width:'+Math.min(100,ratio*100).toFixed(1)+'%;background:'+color+'"></i></div></span><span class="sb-num">'+fmtSize(x.mb)+' / '+fmtSize(x.limit)+(x.over?' ⚠':'')+'</span></div>';}).join('');var w=document.getElementById('sb-warn');if(w&&s&&s.warn!=null)w.value=s.warn;var c=document.getElementById('sb-crit');if(c&&s&&s.crit!=null)c.value=s.crit;var lb=document.getElementById('sb-dir-limits');if(lb){lb.innerHTML='<div class="hint" style="margin:0 0 6px">目录上限（MB）</div>'+dirs.map(function(x){return '<div class="sb-row"><span class="sb-name">'+esc(x.label)+'</span><span class="sb-wrap"></span><input class="sb-lim" data-key="'+esc(x.key)+'" type="number" min="1" value="'+x.limit+'" style="width:110px"></div>';}).join('');}}
function saveStorageSettings(){var w=parseInt((document.getElementById('sb-warn')||{}).value,10);var c=parseInt((document.getElementById('sb-crit')||{}).value,10);var dirs={};document.querySelectorAll('.sb-lim').forEach(function(i){var v=parseInt(i.value,10);if(!isNaN(v)&&v>0)dirs[i.dataset.key||i.getAttribute('data-key')]=v;});var st={dirs:dirs};if(!isNaN(w))st.disk_warn=w;if(!isNaN(c))st.disk_crit=c;fetch('/api/ui_settings',{method:'POST',body:JSON.stringify({storage:st})}).then(function(r){return r.json()}).then(function(r){toast(r.ok?'阈值已保存':'保存失败');loadStorage();}).catch(function(){toast('保存失败')});}
var _muteDismissed=false;
function dismissMute(){_muteDismissed=true;var b=document.getElementById('mute-banner');if(b)b.style.display='none';}
function loadMute(){fetch('/api/memories').then(function(r){return r.json()}).then(function(m){var now=Date.now()/1000;var out=[];Object.keys(m||{}).forEach(function(gid){if(gid==='error')return;var g=m[gid]||{};var mu=g.muted;if(!mu)return;var until=Number(mu.until)||0;if(until>0&&until<now)return;var left=until>0?Math.max(1,Math.round((until-now)/60)):0;out.push(convLabel(gid,g).replace('🔇','').trim()+' 被禁言'+(until>0?('，剩 '+left+' 分钟'):'（未定期限）'));});var b=document.getElementById('mute-banner');if(!b)return;if(!out.length||_muteDismissed){b.style.display='none';return;}document.getElementById('mute-banner-text').textContent='🔇 '+out.join(' · ')+'，消息发不出去，等解禁或先把这个群设成 off。';b.style.display='flex';}).catch(function(){});}
function loadStorage(){fetch('/api/storage').then(function(r){return r.json()}).then(renderStorage).catch(function(){});}
function energyColor(v){return v>=70?'#2e9d5a':(v>=40?'#3a9ad9':(v>=18?'#d68910':'#c0392b'));}
function fmtNum(n){n=Number(n||0);if(n>=1000000)return (n/1000000).toFixed(2)+'M';if(n>=1000)return (n/1000).toFixed(1)+'k';return String(Math.round(n));}
function energySpark(hist,color){var pts=(hist||[]).filter(function(p){return p&&p.v!=null;}).slice(-144);if(pts.length<2)return '<div class="hint" style="margin:8px 0 0">曲线还在攒（每 10 分钟一点）</div>';var hasTs=pts.every(function(p){return Number(p.ts)>0;});if(hasTs)pts.sort(function(a,b){return Number(a.ts)-Number(b.ts);});var vs=pts.map(function(p){return Number(p.v)||0;});var mn=Math.min.apply(null,vs),mx=Math.max.apply(null,vs);if(mx-mn<5){mn=Math.max(0,mn-2.5);mx=mx+2.5;}var W=240,H=48,n=pts.length;var t0=hasTs?Number(pts[0].ts):0,t1=hasTs?Number(pts[n-1].ts):0,span=t1-t0;var xOf=function(p,i){if(hasTs&&span>0)return ((Number(p.ts)-t0)/span)*W;return (i/(n-1))*W;};var d=pts.map(function(p,i){var x=xOf(p,i);var y=H-((Number(p.v)-mn)/(mx-mn))*H;return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);}).join(' ');var gl=['<line class="en-grid" x1="0" y1="6" x2="240" y2="6"/>','<line class="en-grid" x1="0" y1="24" x2="240" y2="24"/>','<line class="en-grid" x1="0" y1="42" x2="240" y2="42"/>'];for(var i=1;i<4;i++){var gx=(240*i/4).toFixed(1);gl.push('<line class="en-grid-v" x1="'+gx+'" y1="0" x2="'+gx+'" y2="48"/>');}var axis='';if(hasTs&&span>0){var f=function(t){var dt=new Date(t*1000);var h=dt.getHours(),m=dt.getMinutes();return (h<10?'0':'')+h+':'+(m<10?'0':'')+m;};var hrs=Math.max(0.1,Math.round(span/360)/10);axis='<div class="en-axis"><span>'+f(t0)+'</span><span>近 '+hrs+' 小时</span><span>'+f(t1)+'</span></div>';}return '<svg class="en-spark" viewBox="0 0 240 48" preserveAspectRatio="none"><g>'+gl.join('')+'</g><path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="1.6" stroke-linejoin="round"/></svg>'+axis;}
function renderEnergyCards(m){var box=document.getElementById('energy-cards');if(!box)return;var keys=Object.keys(m||{}).filter(function(k){return k!=='error'&&scopeMatch(k)}).sort();if(!keys.length){box.innerHTML='<div class="card"><div class="hint" style="margin:0">暂无群数据</div></div>';return;}box.innerHTML=keys.map(function(gid){var g=m[gid]||{},e=g.energy||{};var v=Number(e.value==null?100:e.value);var tier=e.tier_cn||'精神不错';var cls=(v<18)?'crit':((v<40)?'warn':'');var color=energyColor(v);var upd=e.updated?(new Date(e.updated*1000).toLocaleTimeString()):'-';var meta=[['上次消耗',(e.last_cost==null?'-':Number(e.last_cost).toFixed(2))],['上次恢复',(e.last_recover==null?'-':Number(e.last_recover).toFixed(2))],['心情因子',(e.mood_factor==null?'-':Number(e.mood_factor).toFixed(2))],['实时心情',((g.mood||{}).label||'-')],['系数/千token',(e.cost_rate==null?'-':Number(e.cost_rate).toFixed(3))],['累计 token',(e.tokens==null?'-':fmtNum(e.tokens))]];return '<div class="en-card"><div class="en-head"><b>'+esc(convLabel(gid,g))+'</b><span class="en-tier '+cls+'">'+esc(tier)+'</span></div><div class="en-num">'+v.toFixed(1)+'<span>/ 100</span></div><div class="sb-bar"><i style="width:'+Math.max(0,Math.min(100,v)).toFixed(1)+'%;background:'+color+'"></i></div><div class="en-meta">'+meta.map(function(kv){return '<span>'+kv[0]+' '+esc(kv[1])+'</span>';}).join('')+'<span>更新 '+esc(upd)+'</span></div>'+energySpark(e.history,color)+'</div>';}).join('');}
function convLabel(gid,obj){obj=obj||{};var mu=obj.muted;var muted=!!(mu&&(Number(mu.until)===0||Number(mu.until)*1000>Date.now()));var isP=String(gid).charAt(0)==='p';var nm='',k;var aff=obj.affinity||{};for(k in aff){if(aff[k]&&aff[k].name){nm=aff[k].name;break;}}if(!nm){var us=obj.users||{};for(k in us){if(us[k]&&us[k].nickname){nm=us[k].nickname;break;}}}return (muted?'🔇 ':'')+(isP?'私聊 ':'群 ')+(isP?(nm||('QQ '+String(gid).slice(1))):gid);}
function loadEnergy(){fetch('/api/memories').then(function(r){return r.json()}).then(function(m){var box=document.getElementById('energy-list');if(box){var keys=Object.keys(m||{}).filter(function(k){return k!=='error'});if(!keys.length){box.innerHTML='<div class="hint">暂无群数据</div>';}else{keys.sort();box.innerHTML=keys.map(function(gid){var g=m[gid]||{},e=g.energy||{};var v=Number(e.value==null?100:e.value);var tier=e.tier_cn||'精神不错';var color=energyColor(v);return '<div class="sb-row"><span class="sb-name">'+esc(convLabel(gid,g))+'</span><span class="sb-wrap"><div class="sb-bar"><i style="width:'+Math.max(0,Math.min(100,v)).toFixed(1)+'%;background:'+color+'"></i></div></span><span class="sb-num">'+v.toFixed(0)+' / 100 · '+esc(tier)+'</span></div>';}).join('');}}renderEnergyCards(m);}).catch(function(){var b=document.getElementById('energy-list');if(b)b.innerHTML='<div class="hint">读取失败</div>';});}
function dismissStorage(){_storageDismissed=true;var b=document.getElementById('storage-banner');if(b)b.style.display='none';}
function ago(ts){var s=Math.floor(Date.now()/1000-Number(ts||0));if(!ts||s<0)return '-';if(s<60)return'刚刚';if(s<3600)return Math.floor(s/60)+' 分钟前';if(s<86400)return Math.floor(s/3600)+' 小时前';return Math.floor(s/86400)+' 天前';}
function refreshProfiles(){loadProfiles();}
function reloadProfilesNow(){var n=document.getElementById('profiles-sync');if(n)n.textContent='读取中…';loadProfiles();}
var profFilter='all';
function setProfFilter(scope){profFilter=scope||'all';document.querySelectorAll('#prof-filter .net-tab').forEach(function(b){b.classList.toggle('active',b.dataset.scope===profFilter)});loadProfiles();}
var profGroups=null,profCollapsed={},profNotesOpen={};
function profLast(g){var us=(g||{}).users||{};var t=0;Object.keys(us).forEach(function(k){var u=Number((us[k]||{}).updated)||0;if(u>t)t=u;});return t;}
function applyProfGroup(gid){var closed=!!profCollapsed[gid];document.querySelectorAll('#profiles-body .prof-row[data-gid="'+gid+'"]').forEach(function(tr){tr.classList.toggle('prof-hidden',closed);});var a=document.querySelector('#profiles-body .prof-head[data-gid="'+gid+'"] .prof-arrow');if(a)a.textContent=closed?'▸':'▾';}
function updateProfFoldAll(){var btn=document.getElementById('prof-fold-all');if(!btn)return;var heads=document.querySelectorAll('#profiles-body .prof-head');var n=0;heads.forEach(function(h){if(profCollapsed[h.dataset.gid])n+=1;});btn.textContent=(heads.length&&n===heads.length)?'全部展开':'全部折叠';}
function toggleProfGroup(gid){profCollapsed[gid]=!profCollapsed[gid];applyProfGroup(gid);updateProfFoldAll();}
function toggleAllProfiles(){var heads=document.querySelectorAll('#profiles-body .prof-head');if(!heads.length)return;var all=true;heads.forEach(function(h){if(!profCollapsed[h.dataset.gid])all=false;});heads.forEach(function(h){profCollapsed[h.dataset.gid]=!all;applyProfGroup(h.dataset.gid);});updateProfFoldAll();}
function toggleProfNotes(btn){var box=btn.parentNode;var closed=!box.classList.contains('collapsed');box.classList.toggle('collapsed',closed);profNotesOpen[box.dataset.gid+':'+box.dataset.uid]=!closed;btn.textContent=closed?('展开全部 '+box.dataset.total+' 条'):'收起到 3 条';}
function renderProfiles(){
  var body=document.getElementById('profiles-body');if(!body)return;
  var groups=(profGroups&&profGroups.groups)||{};
  body.innerHTML='';
  var gids=Object.keys(groups).filter(function(k){if(profFilter==='all')return true;return (k.charAt(0)==='p')===(profFilter==='private');});
  gids.sort(function(a,b){return profLast(groups[b])-profLast(groups[a]);});
  if(!gids.length){body.innerHTML='<tr><td colspan="3" class="hint">暂无档案，等群聊或私聊攒够消息会自动长出来。</td></tr>';updateProfFoldAll();return;}
  gids.forEach(function(gid){
    var g=groups[gid]||{},users=g.users||{};
    var hdr=document.createElement('tr');hdr.className='prof-head';hdr.dataset.gid=gid;
    var htd=document.createElement('td');htd.colSpan=3;
    var box=document.createElement('div');box.className='prof-head-in';
    var ar=document.createElement('span');ar.className='prof-arrow';ar.textContent=profCollapsed[gid]?'▸':'▾';
    var lb=document.createElement('span');lb.className='prof-head-label';lb.textContent=convLabel(gid,{affinity:g.affinity,users:users})+'（'+Object.keys(users).length+' 人）';
    box.appendChild(ar);box.appendChild(lb);
    var last=profLast(g);
    if(last){var mt=document.createElement('span');mt.className='prof-head-meta';mt.textContent='最近 '+ago(last);box.appendChild(mt);}
    htd.appendChild(box);hdr.appendChild(htd);
    hdr.addEventListener('click',function(){toggleProfGroup(gid);});
    body.appendChild(hdr);
    var keys=Object.keys(users).sort(function(a,b){return (Number((users[b]||{}).score)||0)-(Number((users[a]||{}).score)||0);});
    keys.forEach(function(uid){
      var u=users[uid]||{};
      var tr=document.createElement('tr');tr.className='prof-row';tr.dataset.gid=gid;
      if(profCollapsed[gid])tr.classList.add('prof-hidden');
      var td1=document.createElement('td');
      var sc=(u.score!==null&&u.score!==undefined)?Number(u.score):null;
      var aff=sc!==null?(' <span style="font-size:11px;color:'+affinityColor(sc)+';border:1px solid '+affinityColor(sc)+';border-radius:4px;padding:0 4px">'+sc.toFixed(2)+'</span>'):'';
      td1.innerHTML='<b>'+esc(u.nickname||uid)+'</b>'+aff+(u.uid?('<br><span style="color:#8a94a6;font-size:11px">'+esc(u.uid)+'</span>'):'');
      var td2=document.createElement('td');
      var notes=u.notes||[];
      if(!notes.length){td2.innerHTML='<span class="hint">（还没形成印象）</span>';}
      else{
        var wr=document.createElement('div');wr.className='prof-notes';
        var key=gid+':'+uid;
        wr.dataset.gid=gid;wr.dataset.uid=String(uid);wr.dataset.total=notes.length;
        var open=!!profNotesOpen[key];
        if(!open&&notes.length>3)wr.classList.add('collapsed');
        notes.forEach(function(n,i){var d=document.createElement('div');d.className='prof-note'+(i>=3?' prof-extra':'');d.textContent='· '+n;wr.appendChild(d);});
        if(notes.length>3){
          var b=document.createElement('button');b.type='button';b.className='prof-more';
          b.textContent=open?'收起到 3 条':('展开全部 '+notes.length+' 条');
          b.addEventListener('click',function(e){e.stopPropagation();toggleProfNotes(b);});
          wr.appendChild(b);
        }
        td2.appendChild(wr);
      }
      var td3=document.createElement('td');td3.style.cssText='color:#8a94a6;font-size:12px';td3.textContent=ago(u.updated);if(u.updated)td3.title=new Date(u.updated*1000).toLocaleString();
      tr.appendChild(td1);tr.appendChild(td2);tr.appendChild(td3);
      body.appendChild(tr);
    });
  });
  updateProfFoldAll();
}
function loadProfiles(){
  fetch('/api/profiles').then(function(r){return r.json()}).then(function(p){
    profGroups=p||{};
    renderProfiles();
    var n=document.getElementById('profiles-sync');
    if(n){
      var gids=Object.keys((p&&p.groups)||{}).filter(function(k){if(profFilter==='all')return true;return (k.charAt(0)==='p')===(profFilter==='private');});
      var s='只读 · 源 memwatch';
      s+='（'+gids.length+' '+(profFilter==='private'?'个私聊':(profFilter==='group'?'个群':'个会话'))+'）';
      s+=' · 已同步 '+new Date().toLocaleTimeString();
      if(p&&p.latest)s+=' · 数据更新于 '+new Date(p.latest*1000).toLocaleTimeString();
      if(p&&p.error)s+=' · 错误：'+p.error;
      n.textContent=s;
    }
  }).catch(function(){var n=document.getElementById('profiles-sync');if(n)n.textContent='读取失败';});
}
function restartBot(){toast('正在重启机器人…');fetch('/api/restart',{method:'POST'}).then(function(r){return r.json()}).then(function(r){toast(r.ok?'记忆服务已重启':'重启失败')}).catch(function(){toast('重启失败')});}var _maibotToken='',_maibotShow=false;
function toggleMaiBotToken(){_maibotShow=!_maibotShow;document.getElementById('maibot-token').textContent=_maibotShow?_maibotToken:'••••••••';}
var _llbotToken='',_llbotShow=false;
function toggleLlbBotToken(){_llbotShow=!_llbotShow;document.getElementById('llbot-token').textContent=_llbotShow?_llbotToken:'••••••••';}
function loadLlbBotToken(){fetch('/api/llbot_token').then(function(r){return r.json()}).then(function(d){_llbotToken=(d&&d.token)||'';document.getElementById('llbot-token').textContent=_llbotShow?_llbotToken:'••••••••';}).catch(function(){});}
function copyText(t){if(!t){toast('暂无密码');return;}function fallback(){try{var ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();var ok=document.execCommand('copy');document.body.removeChild(ta);toast(ok?'已复制访问密码':'复制失败');}catch(e){toast('复制失败');}}if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(function(){toast('已复制访问密码')}).catch(fallback);}else{fallback();}}
function copyLlbBotToken(){copyText(_llbotToken);}
function loadMaiBot(){fetch('/api/maibot').then(function(r){return r.json()}).then(function(d){if(d&&d.token){_maibotToken=d.token;document.getElementById('maibot-token').textContent=_maibotShow?_maibotToken:'••••••••';var _mbBase=(location.port==='8090')?'/maibot':'__MAIBOT_BASE__';document.getElementById('maibot-frame').src=_mbBase+'/?token='+encodeURIComponent(d.token);}else{toast('MaiBot Token 未读到');}}).catch(function(){toast('读取 MaiBot Token 失败')});}
function copyMaiBotToken(){copyText(_maibotToken);}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function emotionInfo(key){var M={joy:['高兴','#2e9d5a'],trust:['信任','#1f9d8a'],anticipation:['期待','#3a9ad9'],surprise:['惊喜','#9b6bd6'],fear:['害怕','#c0392b'],sadness:['伤心','#4a6fa5'],disgust:['嫌弃','#b06a3b'],anger:['生气','#c0392b'],confusion:['困惑','#d68910'],neutral:['平静','#8a94a6']};return M[key]||M.neutral;}
function emotionOf(ev){if(ev&&ev.emotion)return ev.emotion;var m=ev&&ev.mood;return m==='positive'?'joy':(m==='sad'?'sadness':'neutral');}
function skillOf(ev){return (ev&&ev.skill)||'';}
function skillCn(s){var M={engage:'顺着聊',explore:'主动探索',reformulate:'换词追问',probe:'小心探问',soothe:'安抚',avoid_topic:'避让话题',defuse:'降温',chat:'闲聊'};return M[s]||s||'';}
var convFilter='all';
function scopeMatch(k){if(convFilter==='all')return true;return (String(k).charAt(0)==='p')===(convFilter==='private');}
function setConvFilter(scope){convFilter=scope||'all';document.querySelectorAll('.conv-filter .net-tab').forEach(function(b){b.classList.toggle('active',b.dataset.scope===convFilter)});var a=document.querySelector('.nav-btn.active');var v=a&&a.dataset.view;var f=v&&window.VIEW_LOADERS&&VIEW_LOADERS[v];if(f)f();}
var evCollapsed={};
function updateEvFoldAll(){var btn=document.getElementById('ev-fold-all');if(!btn)return;var cards=document.querySelectorAll('#events-list .ev-card');var collapsed=document.querySelectorAll('#events-list .ev-card.collapsed');btn.textContent=(cards.length&&collapsed.length===cards.length)?'全部展开':'全部折叠';}
function toggleAllEvents(){var cards=document.querySelectorAll('#events-list .ev-card');if(!cards.length)return;var allCollapsed=true;cards.forEach(function(c){if(!c.classList.contains('collapsed'))allCollapsed=false;});cards.forEach(function(c){var gid=c.dataset.gid;if(allCollapsed){c.classList.remove('collapsed');evCollapsed[gid]=false;}else{c.classList.add('collapsed');evCollapsed[gid]=true;}});updateEvFoldAll();}
var evData=null,evEmotionFilter='all',evSearch='',evExpanded={};
var EV_PAGE=20;
function evClock(ts){var d=new Date((Number(ts)||0)*1000);return('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
function evDayKey(ts){var d=new Date((Number(ts)||0)*1000);var n=new Date();var a=new Date(d.getFullYear(),d.getMonth(),d.getDate());var b=new Date(n.getFullYear(),n.getMonth(),n.getDate());var diff=Math.round((b-a)/86400000);if(diff===0)return'今天';if(diff===1)return'昨天';var s=('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2);return d.getFullYear()===n.getFullYear()?s:(d.getFullYear()+'-'+s);}
function evStats(evs){var c={};evs.forEach(function(ev){var k=emotionOf(ev);c[k]=(c[k]||0)+1;});return Object.keys(c).map(function(k){return{k:k,n:c[k]};}).sort(function(x,y){return y.n-x.n;});}
function evStatChips(evs){var s=evStats(evs),out=[],rest=0;s.forEach(function(x,i){if(i<4){var info=emotionInfo(x.k);out.push('<span class="ev-stat" style="color:'+info[1]+';border-color:'+info[1]+';background:'+info[1]+'14">'+info[0]+' '+x.n+'</span>');}else{rest+=x.n;}});if(rest)out.push('<span class="ev-stat">其他 '+rest+'</span>');return out.join('');}
function evMatch(ev){if(evEmotionFilter!=='all'&&emotionOf(ev)!==evEmotionFilter)return false;if(!evSearch)return true;var parts=(ev.participants||[]).map(function(p){return p.name||'';}).join(' ');var hay=[ev.summary,ev.topic,ev.question,ev.secondary?emotionInfo(ev.secondary)[0]:'',parts].join(' ').toLowerCase();return hay.indexOf(evSearch)>=0;}
function evRowHtml(ev){var emo=emotionOf(ev),info=emotionInfo(emo);var sec=(ev.secondary&&ev.secondary!==emo)?('<small>· '+esc(emotionInfo(ev.secondary)[0])+'</small>'):'';var parts=(ev.participants||[]).map(function(p){return p.name||'';}).filter(Boolean);var ps=parts.length?(parts.slice(0,4).join('、')+(parts.length>4?(' 等 '+parts.length+' 人'):'')):'';var meta=[];if(ev.weight!=null)meta.push('权重 '+Number(ev.weight).toFixed(2));if(ev.intensity!=null)meta.push('强度 '+Math.round(Number(ev.intensity)*100)+'%');if(ps)meta.push(ps);var h='<div class="ev-item"><div class="ev-time">'+evClock(ev.ts)+'</div><div class="ev-main"><div class="ev-line"><span class="ev-emo" style="color:'+info[1]+'">'+info[0]+sec+'</span>';if(ev.topic)h+='<span class="ev-chip topic">'+esc(ev.topic)+'</span>';if(skillOf(ev))h+='<span class="ev-chip skill">'+esc(skillCn(skillOf(ev)))+'</span>';h+='<span class="ev-sum">'+esc(ev.summary||'')+'</span></div>';if(meta.length)h+='<div class="ev-meta">'+meta.map(function(x){return esc(x);}).join(' · ')+'</div>';if(ev.question)h+='<div class="ev-q">❓ '+esc(ev.question)+'</div>';h+='</div></div>';return h;}
function evProfilesHtml(g){var profs=g.profiles||{},pk=Object.keys(profs);if(!pk.length)return'';return'<details class="ev-profs"><summary>人物档案 '+pk.length+' 人</summary>'+pk.map(function(n){var p=profs[n]||{};return'<div class="ev-prof"><b>'+esc(n)+'</b> '+esc(((p.notes||[]).join('；')))+'</div>';}).join('')+'</details>';}
function evForgottenHtml(g){var evs=g.forgotten||[];if(!evs.length)return'';return'<details class="ev-profs"><summary>遗忘区 '+evs.length+' 条（留档，不参与上下文）</summary>'+evs.slice().reverse().slice(0,30).map(function(ev){return'<div class="ev-prof">'+evClock(ev.ts)+' · '+esc(emotionInfo(emotionOf(ev))[0])+' · '+esc(ev.summary||'')+'</div>';}).join('')+'</details>';}
function renderEvents(){var box=document.getElementById('events-list');if(!box)return;var cnt=document.getElementById('ev-count');var m=evData;if(!m){box.innerHTML='<div class="card hint">正在读取事件记忆…</div>';return;}var keys=Object.keys(m||{}).filter(function(k){return k!=='error'&&scopeMatch(k)});var total=0,shown=0;keys.forEach(function(gid){var g=m[gid]||{};(g.events||[]).forEach(function(ev){total+=1;if(evMatch(ev))shown+=1;});});if(cnt)cnt.textContent=total?('共 '+total+' 条事件 · 当前显示 '+shown+' 条'):'';if(!keys.length){box.innerHTML='<div class="card hint">暂无事件记忆，等群聊或私聊攒够消息会自动生成。</div>';if(cnt)cnt.textContent='';updateEvFoldAll();return;}keys.sort(function(a,b){function last(g){return ((g||{}).events||[]).reduce(function(x,ev){return Math.max(x,Number(ev.ts)||0);},0);}return last(m[b])-last(m[a]);});box.innerHTML='';var any=false;keys.forEach(function(gid){var g=m[gid]||{};var all=(g.events||[]);var evs=all.slice().sort(function(a,b){return (Number(b.ts)||0)-(Number(a.ts)||0);}).filter(evMatch);if(!evs.length&&(evEmotionFilter!=='all'||evSearch))return;any=true;var card=document.createElement('div');card.className='card ev-card';card.dataset.gid=gid;var head=document.createElement('div');head.className='ev-head';var main=document.createElement('div');main.className='ev-head-main';var title=document.createElement('div');title.className='ev-title';title.textContent=convLabel(gid,g);var sub=document.createElement('div');sub.className='ev-sub';var aff=g.affinity||{},pc=Object.keys(aff).length,nc=0,pr=g.profiles||{};Object.keys(pr).forEach(function(n){nc+=((pr[n].notes)||[]).length;});var forgot=(g.forgotten||[]).length;var subParts=[pc+' 人',all.length+' 事件',nc+' 印象'];if(forgot)subParts.push('遗忘区 '+forgot);if(evs.length!==all.length)subParts.push('筛选后 '+evs.length+' 条');sub.textContent=subParts.join(' · ');main.appendChild(title);main.appendChild(sub);var stats=document.createElement('div');stats.className='ev-stats';var _ml=(g.mood&&g.mood.label)?g.mood.label:'';stats.innerHTML=(_ml?('<span class="ev-stat" style="color:#2f6fed;border-color:#2f6fed;background:#2f6fed14">实时心情 '+esc(_ml)+'</span>'):'')+evStatChips(all);var arrow=document.createElement('span');arrow.className='ev-arrow';arrow.textContent='▾';head.appendChild(main);head.appendChild(stats);head.appendChild(arrow);var body=document.createElement('div');body.className='ev-body';if(!evs.length){body.innerHTML='<div class="hint">（暂无事件）</div>';}else{var limit=evExpanded[gid]?evs.length:EV_PAGE;var shownEvs=evs.slice(0,limit);var html='',lastDay='';shownEvs.forEach(function(ev){var day=evDayKey(ev.ts);if(day!==lastDay){html+='<div class="ev-day">'+esc(day)+'</div>';lastDay=day;}html+=evRowHtml(ev);});body.innerHTML=html;if(evs.length>EV_PAGE){var more=document.createElement('button');more.className='ev-more';more.textContent=evExpanded[gid]?('收起到最近 '+EV_PAGE+' 条'):('展开全部 '+evs.length+' 条');more.onclick=function(){evExpanded[gid]=!evExpanded[gid];renderEvents();};body.appendChild(more);}}body.insertAdjacentHTML('beforeend',evProfilesHtml(g));body.insertAdjacentHTML('beforeend',evForgottenHtml(g));card.appendChild(head);card.appendChild(body);if(evCollapsed[gid])card.classList.add('collapsed');head.addEventListener('click',function(){var closed=card.classList.toggle('collapsed');evCollapsed[gid]=closed;updateEvFoldAll();});box.appendChild(card);});if(!any)box.innerHTML='<div class="card hint">没有符合筛选的事件。</div>';updateEvFoldAll();}
function loadEvents(){fetch('/api/memories').then(function(r){return r.json()}).then(function(m){evData=m||{};renderEvents();}).catch(function(){toast('读取事件记忆失败')});}
function onEvSearch(v){evSearch=String(v||'').trim().toLowerCase();renderEvents();}
function setEvEmotion(v){evEmotionFilter=v||'all';renderEvents();}

function affinityLabel(s){if(s>=75)return'亲近';if(s>=55)return'友好';if(s>=45)return'平常';if(s>=30)return'冷淡';return'反感';}
function affinityColor(s){if(s>=75)return'#2e9d5a';if(s>=55)return'#3a9ad9';if(s>=45)return'#8a94a6';if(s>=30)return'#d68910';return'#c0392b';}
function loadAffinity(){fetch('/api/memories').then(function(r){return r.json()}).then(function(m){var box=document.getElementById('affinity-list');box.innerHTML='';var keys=Object.keys(m||{}).filter(function(k){return k!=='error'&&scopeMatch(k)});var any=false;keys.forEach(function(gid){var aff=(m[gid]||{}).affinity||{};var ids=Object.keys(aff);if(!ids.length)return;any=true;var card=document.createElement('div');card.className='card';var h=document.createElement('div');h.style.cssText='display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-weight:600;font-size:14px;user-select:none;gap:8px';var rows=ids.map(function(uid){return aff[uid];}).sort(function(a,b){return (Number(b.score)||0)-(Number(a.score)||0);});var avg=0;ids.forEach(function(u){avg+=Number(aff[u].score)||0;});avg=avg/ids.length;var title=document.createElement('span');title.innerHTML=esc(convLabel(gid,m[gid]))+' <span style="color:#8a94a6;font-weight:400;font-size:12px">'+ids.length+' 人 · 均 '+avg.toFixed(1)+'</span>';var arrow=document.createElement('span');arrow.style.cssText='color:#8a94a6;font-size:12px;white-space:nowrap';arrow.textContent='▸ 展开';h.appendChild(title);h.appendChild(arrow);var body=document.createElement('div');body.style.cssText='display:none;margin-top:8px';rows.forEach(function(e){var score=Number(e.score||50);var row=document.createElement('div');row.style.cssText='display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #f0f3f8;font-size:13px';var nm=document.createElement('span');nm.style.cssText='min-width:120px;font-weight:600';nm.textContent=e.name||'?';var bar=document.createElement('div');bar.style.cssText='flex:1;background:#eef1f6;border-radius:4px;height:8px;overflow:hidden';var fill=document.createElement('div');fill.style.cssText='height:8px;border-radius:4px;width:'+Math.max(0,Math.min(100,score))+'%;background:'+affinityColor(score);bar.appendChild(fill);var val=document.createElement('span');val.style.cssText='min-width:112px;color:#6b7688;text-align:right';val.textContent=score.toFixed(2)+'（'+affinityLabel(score)+'）';var cnt=document.createElement('span');cnt.style.cssText='min-width:78px;color:#8a94a6;font-size:12px';cnt.textContent='高兴/伤心 '+((e.pos||0))+'/'+((e.sad||0));row.appendChild(nm);row.appendChild(bar);row.appendChild(val);row.appendChild(cnt);body.appendChild(row);});h.addEventListener('click',function(){var open=body.style.display!=='none';body.style.display=open?'none':'block';arrow.textContent=open?'▸ 展开':'▾ 收起';});card.appendChild(h);card.appendChild(body);box.appendChild(card);});if(!any){box.innerHTML='<div class="card hint">暂无好感度数据，等会话攒够消息生成事件后自动出现。</div>';}}).catch(function(){toast('读取好感度失败')});}
var NET_W=900,NET_H=560,NET_NS='http://www.w3.org/2000/svg';
var netVB={x:0,y:0,w:NET_W,h:NET_H};
var netData=null,netGroups={},netActiveGid=null,netBound=false;
function loadNetwork(){fetch('/api/memories').then(function(r){return r.json()}).then(function(m){netGroups={};var keys=Object.keys(m||{}).filter(function(k){return k!=='error'&&scopeMatch(k)});keys.sort();keys.forEach(function(gid){netGroups[gid]=m[gid];});var box=document.getElementById('net-groups');box.innerHTML='';if(keys.length===0){box.innerHTML='<span class="hint">暂无会话数据，等群聊或私聊出现后自动出现。</span>';return;}keys.forEach(function(gid){var b=document.createElement('button');b.className='net-tab';b.dataset.gid=gid;b.textContent=convLabel(gid,netGroups[gid]);b.onclick=function(){setNetworkGroup(gid)};box.appendChild(b);});setNetworkGroup(netGroups[netActiveGid]?netActiveGid:keys[0]);}).catch(function(){toast('读取记忆网络失败')});}
function setNetworkGroup(gid){netActiveGid=gid;document.querySelectorAll('.net-tab').forEach(function(b){b.classList.toggle('active',b.dataset.gid===gid)});renderNetwork(gid);}
var FORGET_ACT=0.06,FADE_ACT=0.22;
function actOf(x,def){var v=Number(x);return isNaN(v)?def:v;}
function buildNetwork(g,showNotes){var nodes=[],links=[],aff=g.affinity||{},prof=g.profiles||{},name2uid={},forgotten=0;Object.keys(aff).forEach(function(uid){var e=aff[uid]||{};if(e.name)name2uid[e.name]=uid;nodes.push({id:'p:'+uid,type:'person',label:e.name||uid,score:Number(e.score||50),act:actOf(e.act,1),r:10});});Object.keys(prof).forEach(function(name){if(name2uid[name])return;nodes.push({id:'pn:'+name,type:'person',label:name,score:50,act:0.6,r:8});});var ni=0;if(showNotes){Object.keys(prof).forEach(function(name){var p=prof[name]||{},uid=name2uid[name],pid=uid?('p:'+uid):('pn:'+name),notes=p.notes||[],nacts=p.note_act||{};notes.forEach(function(note){var _a=actOf(nacts[note],1);if(_a<FORGET_ACT){forgotten++;return;}var nid='n:'+(ni++);nodes.push({id:nid,type:'note',label:String(note),owner:name,act:_a,r:4.5});links.push({source:pid,target:nid,kind:'note'});});});}var pids={};nodes.forEach(function(n){pids[n.id]=1;});var topicNodes={};var evs=(g.events||[]);evs.forEach(function(ev){var _a=actOf(ev.act,1);if(_a<FORGET_ACT){forgotten++;return;}var eid='e:'+ev.id,_em=emotionOf(ev),_info=emotionInfo(_em);nodes.push({id:eid,type:'event',mood:_em,color:_info[1],label:String(ev.summary||'事件'),score:Number(ev.weight||0),act:_a,ev:ev,r:9});var linked=false;(ev.participants||[]).forEach(function(p){var pid='p:'+p.uid;if(pids[pid]){links.push({source:eid,target:pid,kind:'event'});linked=true;}});if(!linked){var s=String(ev.summary||'');Object.keys(name2uid).forEach(function(name){if(name&&s.indexOf(name)>=0&&pids['p:'+name2uid[name]]){links.push({source:eid,target:'p:'+name2uid[name],kind:'event'});}});}});nodes.filter(function(n){return n.type==='event';}).forEach(function(n){var ev=n.ev||{};var tn=ev.topic;if(!tn)return;var tid='t:'+tn;if(!topicNodes[tid]){topicNodes[tid]=1;var ti=(g.topics&&g.topics[tn]||{}).interest||0;nodes.push({id:tid,type:'topic',label:tn,interest:Number(ti),act:actOf((g.topics&&g.topics[tn]||{}).act,0.6),r:7});}links.push({source:n.id,target:tid,kind:'topic'});});return {nodes:nodes,links:links,forgotten:forgotten};}
function layoutNetwork(nodes,links){var W=900,H=560,i;nodes.forEach(function(n,idx){var a=idx*2.399963,rr=70+Math.sqrt(idx)*30;n.x=W/2+Math.cos(a)*rr;n.y=H/2+Math.sin(a)*rr;n.vx=0;n.vy=0;});var byId={};nodes.forEach(function(n){byId[n.id]=n;});var es=links.map(function(l){return {s:byId[l.source],t:byId[l.target],kind:l.kind};}).filter(function(e){return e.s&&e.t;});for(var it=0;it<280;it++){var cool=1-it/280;for(i=0;i<nodes.length;i++){for(var j=i+1;j<nodes.length;j++){var a=nodes[i],b=nodes[j],dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy;if(d2<1)d2=1;if(d2>26000)continue;var d=Math.sqrt(d2),f=Math.min(1100/d2,4),ux=dx/d,uy=dy/d;a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f;}}es.forEach(function(e){var dx=e.t.x-e.s.x,dy=e.t.y-e.s.y,d=Math.sqrt(dx*dx+dy*dy)||0.01,target=e.kind==='event'?110:(e.kind==='note'?62:100),f=(d-target)*0.006,ux=dx/d,uy=dy/d;e.s.vx+=ux*f;e.s.vy+=uy*f;e.t.vx-=ux*f;e.t.vy-=uy*f;});nodes.forEach(function(n){n.vx+=(W/2-n.x)*0.0025;n.vy+=(H/2-n.y)*0.0025;n.vx*=0.84;n.vy*=0.84;n.x+=n.vx*cool;n.y+=n.vy*cool;n.x=Math.max(26,Math.min(W-26,n.x));n.y=Math.max(24,Math.min(H-26,n.y));});}}
function netColor(n){if(n.type==='person')return '#2f6fed';if(n.type==='note')return '#9b6bd6';if(n.type==='topic')return '#d68910';return n.color||'#8a94a6';}
function netMoodLabel(m){return emotionInfo(m)[0];}
function netTime(ts){var age=Math.max(0,Math.floor(Date.now()/1000-Number(ts||0)));return age<3600?(Math.floor(age/60)+'分钟前'):(Math.floor(age/3600)+'小时前');}
function netTrunc(s,n){s=String(s||'');return s.length>n?(s.slice(0,n)+'…'):s;}
function renderNetwork(gid){if(!gid||!netGroups[gid])return;var showNotes=document.getElementById('net-show-notes').checked;netData=buildNetwork(netGroups[gid],showNotes);layoutNetwork(netData.nodes,netData.links);document.getElementById('net-stats').textContent='人 '+netData.nodes.filter(function(n){return n.type==='person'}).length+' · 事件 '+netData.nodes.filter(function(n){return n.type==='event'}).length+' · 印象 '+netData.nodes.filter(function(n){return n.type==='note'}).length+' · 连接 '+netData.links.length+(netData.forgotten?' · 淡忘 '+netData.forgotten:'');drawNet();netBind();applyNetVB();}
function drawNet(){var svg=document.getElementById('net-svg');if(!svg||!netData)return;applyNetVB();while(svg.firstChild)svg.removeChild(svg.firstChild);var byId={};netData.nodes.forEach(function(n){byId[n.id]=n;});var linkEls=[];netData.links.forEach(function(l){var s=byId[l.source],t=byId[l.target];if(!s||!t)return;var line=document.createElementNS(NET_NS,'line');line.setAttribute('x1',s.x);line.setAttribute('y1',s.y);line.setAttribute('x2',t.x);line.setAttribute('y2',t.y);line.setAttribute('stroke',l.kind==='event'?'#c7d2e4':(l.kind==='topic'?'#ecd3a8':'#e0d6f0'));line.setAttribute('stroke-width',l.kind==='event'?1.8:1.1);line.setAttribute('class','net-link');line.dataset.s=l.source;line.dataset.t=l.target;svg.appendChild(line);linkEls.push(line);});netData.nodes.forEach(function(n){var g=document.createElementNS(NET_NS,'g');g.setAttribute('class','net-node'+(n.type==='note'?' note':''));g.dataset.id=n.id;var c=document.createElementNS(NET_NS,'circle');c.setAttribute('cx',n.x);c.setAttribute('cy',n.y);c.setAttribute('r',(n.r*(0.72+0.28*actOf(n.act,1))).toFixed(2));c.setAttribute('fill',netColor(n));c.setAttribute('fill-opacity',(((n.type==='note'?0.85:0.95)*(0.25+0.75*actOf(n.act,1)))).toFixed(3));c.setAttribute('stroke','#fff');c.setAttribute('stroke-width','1.5');g.appendChild(c);var tx=document.createElementNS(NET_NS,'text');tx.setAttribute('x',n.x);tx.setAttribute('y',n.y+n.r+11);tx.setAttribute('text-anchor','middle');tx.textContent=n.type==='note'?netTrunc(n.label,10):(n.type==='event'?netTrunc(n.label,12):n.label);g.appendChild(tx);var title=document.createElementNS(NET_NS,'title');title.textContent=n.type==='person'?(n.label+' · 好感 '+n.score.toFixed(0)):(n.type==='event'?(netMoodLabel(n.mood)+' · '+n.label):(n.type==='topic'?('话题 · '+n.label+' · 兴趣 '+Number(n.interest||0).toFixed(1)):n.label));g.appendChild(title);g.addEventListener('mouseenter',function(){highlightNet(n.id,linkEls)});g.addEventListener('mouseleave',function(){clearNetHighlight(linkEls)});g.addEventListener('click',function(){showNetDetail(n)});g.addEventListener('pointerdown',function(ev){showNetDetail(n);var p=svgPoint(ev);netDrag={node:n,ox:p.x-n.x,oy:p.y-n.y};try{svg.setPointerCapture(ev.pointerId)}catch(e){}svg.style.cursor='grabbing';});svg.appendChild(g);});svg.onpointermove=function(ev){if(netPan){var r=svg.getBoundingClientRect();var dx=(ev.clientX-netPan.x)*netVB.w/Math.max(1,r.width);var dy=(ev.clientY-netPan.y)*netVB.h/Math.max(1,r.height);netVB.x=netPan.vbX-dx;netVB.y=netPan.vbY-dy;applyNetVB();return;}if(!netDrag)return;var p=svgPoint(ev);netDrag.node.x=p.x-netDrag.ox;netDrag.node.y=p.y-netDrag.oy;if(!netRaf){netRaf=requestAnimationFrame(function(){netRaf=0;drawNet();});}};svg.onpointerup=function(ev){if(netPan){netPan=null;}if(netDrag){netDrag=null;}svg.style.cursor='grab';try{svg.releasePointerCapture(ev.pointerId)}catch(e){}};}
function svgPoint(ev){var svg=document.getElementById('net-svg'),r=svg.getBoundingClientRect();return {x:netVB.x+(ev.clientX-r.left)*netVB.w/Math.max(1,r.width),y:netVB.y+(ev.clientY-r.top)*netVB.h/Math.max(1,r.height)};}
function applyNetVB(){var svg=document.getElementById('net-svg');if(svg)svg.setAttribute('viewBox',netVB.x.toFixed(2)+' '+netVB.y.toFixed(2)+' '+netVB.w.toFixed(2)+' '+netVB.h.toFixed(2));var z=document.getElementById('net-zoom');if(z)z.textContent=Math.round(NET_W/netVB.w*100)+'%';}
function netZoom(f,cx,cy){var nw=Math.max(NET_W/8,Math.min(NET_W*1.2,netVB.w/f));var k=nw/netVB.w;if(cx==null){cx=netVB.x+netVB.w/2;cy=netVB.y+netVB.h/2;}netVB.x=cx-(cx-netVB.x)*k;netVB.y=cy-(cy-netVB.y)*k;netVB.w=nw;netVB.h=nw*NET_H/NET_W;applyNetVB();}
function netZoomReset(){netVB={x:0,y:0,w:NET_W,h:NET_H};applyNetVB();}
function netZoomFit(){if(!netData||!netData.nodes.length){netZoomReset();return;}var xs=netData.nodes.map(function(n){return n.x;}),ys=netData.nodes.map(function(n){return n.y;});var minx=Math.min.apply(null,xs),maxx=Math.max.apply(null,xs),miny=Math.min.apply(null,ys),maxy=Math.max.apply(null,ys);var pad=60;var w=Math.max(120,(maxx-minx)+pad*2),h=Math.max(80,(maxy-miny)+pad*2);var ratio=Math.max(w/NET_W,h/NET_H);netVB.w=NET_W*ratio;netVB.h=NET_H*ratio;netVB.x=(minx+maxx)/2-netVB.w/2;netVB.y=(miny+maxy)/2-netVB.h/2;applyNetVB();}
function netBind(){if(netBound)return;var svg=document.getElementById('net-svg');if(!svg)return;netBound=true;
 svg.addEventListener('wheel',function(ev){ev.preventDefault();var p=svgPoint(ev);netZoom(ev.deltaY<0?1.12:0.89,p.x,p.y);},{passive:false});
 svg.addEventListener('dblclick',function(){netZoomReset();});
 svg.addEventListener('pointerdown',function(ev){if(netDrag)return;if(ev.target&&ev.target.closest&&ev.target.closest('.net-node'))return;netPan={x:ev.clientX,y:ev.clientY,vbX:netVB.x,vbY:netVB.y};try{svg.setPointerCapture(ev.pointerId)}catch(e){}svg.style.cursor='grabbing';});
}
function highlightNet(id,linkEls){var near={};near[id]=1;netData.links.forEach(function(l){if(l.source===id)near[l.target]=1;if(l.target===id)near[l.source]=1;});document.querySelectorAll('#net-svg .net-node').forEach(function(g){g.classList.toggle('dim',!near[g.dataset.id]);});linkEls.forEach(function(line){line.classList.toggle('dim',!(near[line.dataset.s]&&near[line.dataset.t]));});}
function clearNetHighlight(linkEls){document.querySelectorAll('#net-svg .net-node').forEach(function(g){g.classList.remove('dim')});linkEls.forEach(function(line){line.classList.remove('dim')});}
function showNetDetail(n){var box=document.getElementById('net-detail');if(n.type==='person'){box.innerHTML='<b>'+esc(n.label)+'</b>　好感 '+n.score.toFixed(0)+'（'+affinityLabel(n.score)+'）';}else if(n.type==='event'){box.innerHTML='<b>事件</b>　'+netMoodLabel(n.mood)+' · 权重 '+n.score.toFixed(2)+' · '+netTime(n.ev.ts)+'<br>'+esc(n.label);}else if(n.type==='topic'){box.innerHTML='<b>话题</b>　'+esc(n.label)+' · 兴趣 '+Number(n.interest||0).toFixed(1);}else{box.innerHTML='<b>'+esc(n.owner||'')+'</b> 的印象<br>'+esc(n.label);}}
var tokPeriod='day',tokData=null,tokBusy=false,tokTimer=null;
function tokNum(n){n=Number(n||0);return n.toLocaleString('zh-CN');}
function tokMoney(v){v=Number(v||0);if(v>=1)return '¥'+v.toFixed(2);if(v>0)return '¥'+v.toFixed(4);return '¥0';}
function tokPeriodLabel(p){return p==='day'?'今日':(p==='week'?'本周':(p==='month'?'本月':'累计'));}
function renderTokenCards(){if(!tokData)return;var p=tokData[tokPeriod]||tokData.day||{};var t=p.total||{};var label=tokPeriodLabel(tokPeriod);var k=document.getElementById('tok-period-k'),tk=document.getElementById('tok-tokens-k');if(k)k.textContent=label+'消费';if(tk)tk.textContent=label+' Tokens';var c=document.getElementById('tok-cost-period');if(c)c.textContent=tokMoney(t.all_cost);var tt=document.getElementById('tok-tokens-period');if(tt)tt.textContent=tokNum(t.all_tokens);var rq=document.getElementById('tok-req-period');if(rq)rq.textContent='请求 '+tokNum((t.req||0)+(t.mw_req||0))+' · SF '+tokNum(t.sf_tokens||0)+(Number(t.sf_cost||0)?(' · '+tokMoney(t.sf_cost)):'');var a=tokData.all||{},at=a.total||{};var ac=document.getElementById('tok-costall'),atk=document.getElementById('tok-tokensall'),ar=document.getElementById('tok-reqall');if(ac)ac.textContent=tokMoney(at.all_cost);if(atk)atk.textContent=tokNum(at.all_tokens);if(ar)ar.textContent='请求 '+tokNum((at.req||0)+(at.mw_req||0))+' · SF '+tokNum(at.sf_tokens||0);}
function setTokenPeriod(p){tokPeriod=p;['day','week','month','all'].forEach(function(k){var b=document.getElementById('tok-tab-'+k);if(b)b.classList.toggle('active',k===p);});renderTokenCards();renderTokenTable();}
function loadTokenStats(){if(tokBusy)return;tokBusy=true;fetch('/api/token_stats').then(function(r){return r.json()}).then(function(d){tokBusy=false;if(!d||d.error){document.getElementById('tok-hint').textContent='读取失败：'+((d&&d.error)||'未知错误');return;}tokData=d;var bal=d.balance||{},bv=document.getElementById('tok-balance'),bs=document.getElementById('tok-balance-s');if(bal.error){bv.textContent='—';bs.textContent='余额读取失败';}else{bv.textContent=tokMoney(bal.total);bs.textContent=(bal.available?'可用':'不可用')+' · 充值 '+tokMoney(bal.topped_up)+' / 赠金 '+tokMoney(bal.granted);}var sf=d.sf_balance||{},sv=document.getElementById('tok-sf-balance'),ss=document.getElementById('tok-sf-balance-s');if(sf.error){sv.textContent='—';ss.textContent=sf.error;}else{sv.textContent=tokMoney(sf.total);ss.textContent=(sf.available?'可用':'不可用')+' · 充值 '+tokMoney(sf.topped_up)+' / 赠送 '+tokMoney(sf.granted);}document.getElementById('tok-updated').textContent='更新于 '+(d.generated_at||'');renderTokenCards();renderTokenTable();}).catch(function(e){tokBusy=false;document.getElementById('tok-hint').textContent='读取失败：'+e;});}
var tokScope='all';
function setTokScope(scope){tokScope=scope||'all';document.querySelectorAll('#tok-scope-tabs .tok-tab').forEach(function(b){b.classList.toggle('active',b.dataset.scope===tokScope)});renderTokenTable();}
function renderTokenTable(){var tb=document.querySelector('#tok-table tbody');if(!tb||!tokData)return;var period=tokData[tokPeriod]||tokData.day;var rows=(period.groups||[]).slice().filter(function(r){var k=r.kind||(String(r.gid||'').charAt(0)==='p'?'private':(r.gid?'group':'system'));if(tokScope==='all')return true;return k===tokScope;}).sort(function(a,b){return (Number(b.all_cost)||0)-(Number(a.all_cost)||0);});var total=period.total||{};tb.innerHTML='';if(rows.length===0){tb.innerHTML='<tr><td colspan="8" class="hint">这个时间段还没有用量。</td></tr>';return;}var maxCost=Math.max.apply(null,rows.map(function(r){return Number(r.all_cost)||0;}).concat([0.000001]));rows.forEach(function(r){var tr=document.createElement('tr');var td1=document.createElement('td');var _k=r.kind||(String(r.gid||'').charAt(0)==='p'?'private':(r.gid?'group':'system'));var _sub=_k==='private'?('QQ '+esc(String(r.gid||'').slice(1))):(_k==='group'?('群 '+esc(r.gid)):'未归属');td1.innerHTML='<b>'+esc(r.name||_sub)+'</b><div class="hint" style="margin:2px 0 0">'+_sub+'</div>';var td2=document.createElement('td');td2.className='num';td2.textContent=tokNum((r.req||0)+(r.mw_req||0));var td3=document.createElement('td');td3.className='num';td3.textContent=tokNum(r.hit)+' / '+tokNum(r.miss);var td4=document.createElement('td');td4.className='num';td4.textContent=tokNum(r.out);var td5=document.createElement('td');td5.className='num';td5.textContent=tokNum(r.all_tokens);var td6=document.createElement('td');td6.className='num';var sfTok=Number(r.sf_tokens)||0;td6.innerHTML=sfTok?(tokNum(sfTok)+'<div class="hint" style="margin:0;font-size:11px">'+tokNum(r.sf_req||0)+' 次'+(Number(r.sf_cost||0)?(' · '+tokMoney(r.sf_cost)):'')+'</div>'):'—';var td7=document.createElement('td');td7.className='num';td7.innerHTML=tokMoney(r.all_cost)+'<div class="tok-bar"><i style="width:'+Math.max(2,Math.min(100,(Number(r.all_cost)||0)/maxCost*100))+'%"></i></div>';var td8=document.createElement('td');td8.className='num';td8.textContent=tokNum(r.mw_req);tr.appendChild(td1);tr.appendChild(td2);tr.appendChild(td3);tr.appendChild(td4);tr.appendChild(td5);tr.appendChild(td6);tr.appendChild(td7);tr.appendChild(td8);tb.appendChild(tr);});var ft={req:0,mw_req:0,all_tokens:0,all_cost:0,sf_tokens:0};rows.forEach(function(r){ft.req+=Number(r.req||0);ft.mw_req+=Number(r.mw_req||0);ft.all_tokens+=Number(r.all_tokens||0);ft.all_cost+=Number(r.all_cost||0);ft.sf_tokens+=Number(r.sf_tokens||0);});var tr=document.createElement('tr');tr.style.fontWeight='600';tr.innerHTML='<td>合计</td><td class="num">'+tokNum(ft.req+ft.mw_req)+'</td><td class="num">-</td><td class="num">-</td><td class="num">'+tokNum(ft.all_tokens)+'</td><td class="num">'+tokNum(ft.sf_tokens)+'</td><td class="num">'+tokMoney(ft.all_cost)+'</td><td class="num">'+tokNum(ft.mw_req)+'</td>';tb.appendChild(tr);document.getElementById('tok-hint').textContent='每 30 秒自动刷新；MaiBot 提供 token 用量，DeepSeek 按官方价（含高峰/空闲与缓存命中）重算；SiliconFlow 的 Qwen3-VL 按 ¥0.7/¥2.8 计入，bge-m3 免费；事件记忆单独记账。';}
function convRowLabel(it){it=it||{};var id=String(it.id||'');var kind=it.kind||(id.charAt(0)==='p'?'private':'group');var nm=it.name||'';return (kind==='private'?'私聊 ':'群 ')+(kind==='private'?(nm||('QQ '+id.slice(1))):id);}
function buildGroupRow(gid,label,effort,energy){var tr=document.createElement('tr');var td1=document.createElement('td');td1.textContent=label;var td2=document.createElement('td');var sel=document.createElement('select');sel.className='grp-effort';sel.dataset.gid=gid;['off','low','high','max'].forEach(function(v){var o=document.createElement('option');o.value=v;o.textContent=v;if(v===effort)o.selected=true;sel.appendChild(o);});td2.appendChild(sel);var td3=document.createElement('td');var inp=document.createElement('input');inp.type='number';inp.min='0';inp.step='0.005';inp.className='grp-energy';inp.dataset.gid=gid;inp.value=Number(energy).toFixed(3);inp.title='每 1000 token 扣多少精力';inp.style.cssText='width:110px;border:1px solid #d6dbe5;border-radius:6px;padding:5px 7px;font-size:13px';td3.appendChild(inp);tr.appendChild(td1);tr.appendChild(td2);tr.appendChild(td3);return tr;}
function loadGroups(){fetch('/api/group_settings').then(function(r){return r.json()}).then(function(d){var settings=d.settings||{};var groups=d.groups||[];var tb=document.querySelector('#groups-table tbody');tb.innerHTML='';if(groups.length===0){tb.innerHTML='<tr><td colspan="3" class="hint">暂无会话（等群聊或私聊出现后自动加入）</td></tr>';return;}groups.forEach(function(it){var gid=it.id;var st=settings[gid]||{};var cur=st.effort||'low';var ec=(st.energy_cost===undefined||st.energy_cost===null||st.energy_cost==='')?0.045:Number(st.energy_cost);tb.appendChild(buildGroupRow(gid,convRowLabel(it),cur,ec));});}).catch(function(){toast('读取群设置失败')});}
function saveGroups(){var out={};document.querySelectorAll('#groups-table tbody tr').forEach(function(tr){var sel=tr.querySelector('.grp-effort');var inp=tr.querySelector('.grp-energy');if(!sel)return;var ec=parseFloat(inp?inp.value:'');if(!isFinite(ec)||ec<0)ec=0.045;out[sel.dataset.gid]={effort:sel.value,energy_cost:ec,updated:Math.floor(Date.now()/1000)};});fetch('/api/group_settings',{method:'POST',body:JSON.stringify(out)}).then(function(r){return r.json()}).then(function(r){toast(r.ok?'会话设置已保存':'保存失败')}).catch(function(){toast('保存失败')});}
function addGroup(){var gid=(document.getElementById('new-group-id').value||'').trim();if(!gid){toast('请输入群号或 p+QQ号');return;}var eff=document.getElementById('new-group-effort').value;var ec=parseFloat(document.getElementById('new-group-energy').value);if(!isFinite(ec)||ec<0)ec=0.045;var it={id:gid,kind:(gid.charAt(0)==='p'?'private':'group'),name:''};var tb=document.querySelector('#groups-table tbody');tb.appendChild(buildGroupRow(gid,convRowLabel(it),eff,ec));document.getElementById('new-group-id').value='';}
var topicData=null,topicCollapsed={};
function updateTopicFoldAll(){var btn=document.getElementById('tp-fold-all');if(!btn)return;var cards=document.querySelectorAll('#topics-list .tp-card');var closed=document.querySelectorAll('#topics-list .tp-card.collapsed');btn.textContent=(cards.length&&closed.length===cards.length)?'全部展开':'全部折叠';}
function toggleAllTopics(){var cards=document.querySelectorAll('#topics-list .tp-card');if(!cards.length)return;var all=true;cards.forEach(function(c){if(!c.classList.contains('collapsed'))all=false;});cards.forEach(function(c){var gid=c.dataset.gid;if(all){c.classList.remove('collapsed');topicCollapsed[gid]=false;}else{c.classList.add('collapsed');topicCollapsed[gid]=true;}});updateTopicFoldAll();}
function toggleTopicGroup(gid){var c=document.querySelector('#topics-list .tp-card[data-gid="'+gid+'"]');if(!c)return;var closed=c.classList.toggle('collapsed');topicCollapsed[gid]=closed;updateTopicFoldAll();}
function renderTopics(){
  var box=document.getElementById('topics-list');if(!box)return;var m=topicData;if(!m){box.innerHTML='<div class="card hint">正在读取话题…</div>';return;}
  box.innerHTML='';var keys=Object.keys(m||{}).filter(function(k){return k!=='error'&&scopeMatch(k);});var any=false,total=0,forgTotal=0;
  keys.forEach(function(gid){
    var g=m[gid]||{},tp=g.topics||{},ids=Object.keys(tp),forg=g.forgotten_topics||[];
    if(!ids.length&&!forg.length)return;any=true;total+=ids.length;forgTotal+=forg.length;
    var card=document.createElement('div');card.className='card tp-card';card.dataset.gid=gid;
    var head=document.createElement('div');head.className='tp-head';
    var title=document.createElement('div');title.className='tp-title';title.textContent=convLabel(gid,g);
    var intd=ids.filter(function(k){return Number((tp[k]||{}).interest)>=3;}).length;
    var sub=document.createElement('div');sub.className='tp-sub';sub.textContent=ids.length+' 话题'+(intd?' · '+intd+' 个感兴趣':'')+(forg.length?' · 遗忘区 '+forg.length:'');
    var arrow=document.createElement('span');arrow.className='tp-arrow';arrow.textContent='▾';
    head.appendChild(title);head.appendChild(sub);head.appendChild(arrow);
    var body=document.createElement('div');body.className='tp-body';
    ids.sort(function(a,b){return (Number(tp[b].interest)||0)-(Number(tp[a].interest)||0);}).forEach(function(name){
      var t=tp[name]||{};var iv=Number(t.interest)||0;var act=(t.act==null?Math.min(1,Math.abs(iv)/3):Number(t.act));var intt=!!t.interested;
      var row=document.createElement('div');row.className='tp-row'+(act<0.15?' fading':'');row.title='激活度 '+act.toFixed(2);
      var nm=document.createElement('span');nm.className='tp-name';nm.textContent=(intt?'★ ':'')+name;
      var bar=document.createElement('div');bar.className='tp-bar';var fill=document.createElement('i');fill.style.width=Math.max(0,Math.min(100,Math.abs(iv)/6*100))+'%';fill.style.background=(iv<0?'#c0392b':(intt?'#e0a021':'#2f6fed'));bar.appendChild(fill);
      var val=document.createElement('span');val.className='tp-val';val.textContent=iv.toFixed(1)+(intt?' 感兴趣':(iv<0?' 避让':''));
      var meta=document.createElement('span');meta.className='tp-meta';meta.textContent='激活 '+act.toFixed(2)+(t.count?' · '+t.count+' 次':'')+((t.updated||t.last)?' · '+ago(t.updated||t.last):'');
      row.appendChild(nm);row.appendChild(bar);row.appendChild(val);row.appendChild(meta);body.appendChild(row);
    });
    if(forg.length){
      var det=document.createElement('details');det.className='tp-forgot';
      det.innerHTML='<summary>遗忘区 '+forg.length+' 条（留档，不再进画像）</summary>'+forg.slice().reverse().map(function(f){return '<div class="tp-frow">'+esc(f.name||'')+' · 兴趣 '+Number(f.interest||0).toFixed(1)+' · '+(f.forgotten_at?ago(f.forgotten_at):'')+'</div>';}).join('');
      body.appendChild(det);
    }
    card.appendChild(head);card.appendChild(body);
    if(topicCollapsed[gid]===undefined)topicCollapsed[gid]=ids.length>10;
    if(topicCollapsed[gid])card.classList.add('collapsed');
    head.addEventListener('click',function(){toggleTopicGroup(gid);});
    box.appendChild(card);
  });
  if(!any)box.innerHTML='<div class="card hint">暂无话题，等群里生成事件后自动出现。</div>';
  updateTopicFoldAll();
}
function loadTopics(){if(!topicData)renderTopics();fetch('/api/memories').then(function(r){return r.json()}).then(function(m){topicData=m||{};renderTopics();}).catch(function(){toast('读取话题失败')});}
if(location.port==='8090'){document.getElementById('napcat-frame').src='/llbot/';}
loadStatus();loadProfiles();loadMaiBot();loadLlbBotToken();loadEvents();loadAffinity();loadGroups();loadNetwork();loadTokenStats();loadTopics();loadStorage();loadEnergy();loadMute();loadLlbBotStatus();
setInterval(loadStorage,300000);
setInterval(loadEnergy,60000);
setInterval(loadMute,60000);
setInterval(loadLlbBotStatus,30000);
(function(){var b=document.querySelector('.nav-btn[data-view="status"]');if(b)b.addEventListener('click',function(){loadStatus();loadStorage();});})();
function loadExperiences(){fetch('/api/experiences').then(function(r){return r.json()}).then(function(d){var box=document.getElementById('experiences-list');if(!box)return;var items=(d&&d.items)||[];if(!items.length){box.innerHTML='<div class="card hint">还没有经验。等它遇到不懂的东西、联网查到或被人解释清楚，就会记在这里。</div>';return;}box.innerHTML=items.map(function(it){var ok=it.status==='resolved';var color=ok?'#2e9d5a':'#d68910';var src=it.source==='later'?'群友解释':(it.source==='search'?'联网查证':'记录');var links=(it.sources||[]).slice(0,3).map(function(h){var u=String(h.url||'');var t=String(h.title||u||'来源');return u?('<a href="'+esc(u)+'" target="_blank" rel="noopener" style="color:#2f6fed;text-decoration:none">'+esc(t)+'</a>'):esc(t);}).join(' · ');return '<div class="card" style="margin-bottom:10px"><div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap"><span style="color:'+color+';font-weight:700;font-size:12px;border:1px solid '+color+';border-radius:4px;padding:0 5px">'+(ok?'已搞懂':'待解')+'</span><b style="font-size:14px">'+esc(it.question||'（无问题）')+'</b><span class="hint" style="margin-left:auto">'+esc(ago(it.updated))+'</span></div><div style="margin-top:8px;font-size:13px;line-height:1.7;color:#3a4456">'+(ok?esc(it.learned):'<span class="hint">还没搞懂</span>')+'</div><div class="hint" style="margin-top:7px">'+esc(src)+(it.topic?(' · 话题 '+esc(it.topic)):'')+(it.hits>1?(' · 出现 '+it.hits+' 次'):'')+(links?(' · '+links):'')+'</div></div>';}).join('');}).catch(function(){var box=document.getElementById('experiences-list');if(box)box.innerHTML='<div class="card hint">读取经验失败</div>';});}
/* ============ 更新日志（读 /opt/qqbot-panel/CHANGELOG.md） ============ */
function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function mdInline(s){
  return mdEsc(s)
    .replace(/`([^`]+)`/g,'<code style="background:#f2f4f8;border-radius:4px;padding:1px 5px;font-size:12px">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
}
function mdToHtml(md){
  var out=[],lines=String(md||'').split(/\r?\n/),inCode=false,code=[];
  var PRE='<pre style="background:#f7f9fc;border:1px solid #e8edf6;border-radius:8px;padding:10px;overflow:auto;font-size:12px;line-height:1.55;margin:8px 0">';
  for(var i=0;i<lines.length;i++){
    var L=lines[i];
    if(/^```/.test(L)){
      if(inCode){out.push(PRE+mdEsc(code.join('\n'))+'</pre>');code=[];inCode=false;} else {inCode=true;}
      continue;
    }
    if(inCode){code.push(L);continue;}
    if(/^\s*$/.test(L)){continue;}
    if(/^#\s+/.test(L)){out.push('<h2 style="margin:16px 0 8px;font-size:17px">'+mdInline(L.replace(/^#\s+/,''))+'</h2>');continue;}
    if(/^##\s+/.test(L)){out.push('<h3 style="margin:18px 0 8px;font-size:15px;padding-bottom:6px;border-bottom:1px solid #e8edf6">'+mdInline(L.replace(/^##\s+/,''))+'</h3>');continue;}
    if(/^###\s+/.test(L)){out.push('<h4 style="margin:12px 0 6px;font-size:13.5px">'+mdInline(L.replace(/^###\s+/,''))+'</h4>');continue;}
    if(/^>\s?/.test(L)){out.push('<div style="margin:6px 0;padding:6px 10px;border-left:3px solid #cbd5e1;color:#64748b;font-size:12.5px">'+mdInline(L.replace(/^>\s?/,''))+'</div>');continue;}
    if(/^---+$/.test(L.replace(/\s/g,''))){out.push('<hr style="border:none;border-top:1px solid #e8edf6;margin:14px 0">');continue;}
    if(/^\|/.test(L)){
      var rows=[];
      while(i<lines.length&&/^\|/.test(lines[i])){rows.push(lines[i]);i++;}
      i--;
      var cells=rows.map(function(r){return r.replace(/^\|/,'').replace(/\|$/,'').split('|').map(function(c){return c.trim();});});
      var html='<table style="border-collapse:collapse;margin:8px 0;font-size:12.5px">';
      cells.forEach(function(cs,ri){
        if(ri===1&&cs.every(function(c){return /^:?-{2,}:?$/.test(c);})){return;}
        html+='<tr>';
        cs.forEach(function(c){
          html+=ri===0
            ?'<th style="border:1px solid #e2e8f0;padding:5px 8px;background:#f7f9fc;text-align:left;font-weight:600">'+mdInline(c)+'</th>'
            :'<td style="border:1px solid #e2e8f0;padding:5px 8px;vertical-align:top">'+mdInline(c)+'</td>';
        });
        html+='</tr>';
      });
      out.push(html+'</table>');
      continue;
    }
    if(/^\s*[-*]\s+/.test(L)){out.push('<div style="margin:3px 0 3px 14px;text-indent:-10px">• '+mdInline(L.replace(/^\s*[-*]\s+/,''))+'</div>');continue;}
    out.push('<p style="margin:6px 0">'+mdInline(L)+'</p>');
  }
  if(inCode&&code.length){out.push(PRE+mdEsc(code.join('\n'))+'</pre>');}
  return out.join('');
}
function loadChangelog(){
  var box=document.getElementById('changelog-body');if(!box)return;
  box.innerHTML='<div class="hint">加载中…</div>';
  fetch('/api/changelog').then(function(r){return r.json();}).then(function(d){
    if(!d||d.ok===false){box.innerHTML='<div class="hint">读取失败：'+mdEsc((d&&d.error)||'未知错误')+'</div>';return;}
    box.innerHTML=mdToHtml(d.text||'');
  }).catch(function(e){box.innerHTML='<div class="hint">读取失败：'+mdEsc(String(e))+'</div>';});
}
var VIEW_LOADERS={status:loadStatus,energy:loadEnergy,profiles:loadProfiles,events:loadEvents,topics:loadTopics,network:loadNetwork,experiences:loadExperiences,tokens:loadTokenStats,affinity:loadAffinity,groups:loadGroups,changelog:loadChangelog,settings:function(){var cb=document.getElementById('theme-dark');if(cb)cb.checked=(document.documentElement.dataset.theme==='dark');}};
var NAV_META={panel:['LLBot 面板','协议端控制台'],status:['运行状态','服务与链路'],energy:['精力','每会话电量'],profiles:['人物档案','按会话归档'],events:['事件记忆','按情绪加权'],topics:['话题兴趣','会话关注点'],network:['记忆网络','人物/事件/印象'],experiences:['经验','全局共享知识'],tokens:['Token 统计','用量与费用'],affinity:['人物好感度','跨会话统一'],groups:['会话设置','推理档位与精力系数'],maibot:['MaiBot 面板','聊天与配置'],changelog:['更新日志','服务器变更记录'],settings:['设置','主题与偏好']};
function setPageTitle(v){var m=NAV_META[v]||NAV_META.panel;var t=document.getElementById('page-title'),s=document.getElementById('page-sub');if(t)t.textContent=m[0];if(s)s.textContent=m[1];}
function setMenu(open){var sb=document.getElementById('sidebar'),bd=document.getElementById('nav-backdrop');if(!sb)return;if(open&&window.innerWidth>760)return;sb.classList.toggle('open',!!open);if(bd)bd.classList.toggle('show',!!open);document.body.style.overflow=open?'hidden':'';}
function refreshCurrent(){var a=document.querySelector('.nav-btn.active');var v=a&&a.dataset.view;var f=v&&VIEW_LOADERS[v];if(f)f();toast('已刷新');}
document.addEventListener('keydown',function(e){if(e.key==='Escape')setMenu(false);});
setPageTitle('panel');
document.querySelectorAll('.nav-btn').forEach(function(b){b.addEventListener('click',function(){setMenu(false);setPageTitle(b.dataset.view);var f=VIEW_LOADERS[b.dataset.view];if(f)setTimeout(f,30);});});
setInterval(function(){var a=document.querySelector('.nav-btn.active');if(!a)return;var v=a.dataset.view;if(v==='status')loadStatus();else if(v==='events')loadEvents();else if(v==='topics')loadTopics();else if(v==='affinity')loadAffinity();else if(v==='tokens')loadTokenStats();},20000);
if(location.hash){var _hv=location.hash.slice(1);var _hb=document.querySelector('.nav-btn[data-view="'+_hv+'"]');if(_hb){setTimeout(function(){_hb.click();},60);}}

/* 经验按类别分组展示（覆盖上面的平铺版本） */
var EXP_CATS=['梗与暗号','人物关系','技术','常识科普','群规约定','其他'];
function expCardHtml(it){
  var ok=it.status==='resolved';
  var color=ok?'#2e9d5a':'#d68910';
  var src=it.source==='later'?'群友解释':(it.source==='search'?'联网查证':'记录');
  var links=(it.sources||[]).slice(0,3).map(function(h){var u=String(h.url||'');var t=String(h.title||u||'来源');
    return u?('<a href="'+esc(u)+'" target="_blank" rel="noopener" style="color:#2f6fed;text-decoration:none">'+esc(t)+'</a>'):esc(t);}).join(' · ');
  var body=it.learned?('<div style="margin-top:6px;font-size:13px;line-height:1.7;color:#3a4356">'+esc(it.learned)+'</div>'):'';
  var meta='<div class="hint" style="margin-top:6px">来源：'+esc(src)+(links?(' · '+links):'')+'</div>';
  return '<div class="card" style="margin-bottom:8px">'
    +'<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">'
    +'<span style="color:'+color+';font-weight:700;font-size:12px;border:1px solid '+color+';border-radius:4px;padding:0 5px">'+(ok?'已搞懂':'待解')+'</span>'
    +'<b style="font-size:14px">'+esc(it.question||'（无问题）')+'</b>'
    +'<span class="hint" style="margin-left:auto">'+esc(ago(it.updated))+'</span></div>'
    +body+meta+'</div>';
}
function loadExperiences(){
  fetch('/api/experiences').then(function(r){return r.json()}).then(function(d){
    var box=document.getElementById('experiences-list');if(!box)return;
    var items=(d&&d.items)||[];
    if(!items.length){box.innerHTML='<div class="card hint">还没有经验。等它遇到不懂的东西、联网查到或被人解释清楚，就会记在这里。</div>';return;}
    var map={},order=EXP_CATS.slice();
    items.forEach(function(it){
      var c=String(it.category||'').trim();
      if(order.indexOf(c)<0)c='其他';
      (map[c]=map[c]||[]).push(it);
    });
    order=order.filter(function(c){return map[c]&&map[c].length;});
    var total=items.length,resolved=items.filter(function(it){return it.status==='resolved';}).length;
    var head='<div class="hint" style="margin-bottom:10px">共 '+total+' 条，已搞懂 '+resolved+' 条，分 '+order.length+' 类</div>';
    var html=[head];
    order.forEach(function(c){
      var arr=map[c].sort(function(a,b){return (b.updated||0)-(a.updated||0)});
      html.push('<div class="card" style="margin-bottom:14px;padding:12px 14px;background:#f8fafd">'
        +'<div style="font-weight:600;font-size:14px;margin-bottom:8px">'+esc(c)
        +'<span class="hint" style="font-weight:400;margin-left:8px">'+arr.length+' 条</span></div>'
        +arr.map(expCardHtml).join('')+'</div>');
    });
    box.innerHTML=html.join('');
  }).catch(function(){});
}
</script>
</body>
</html>"""


def _inject_theme():
    """通过 WebView2 的 CDP 给主页面和两个 iframe 注入统一主题。"""
    if websocket is None:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_theme.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        print(f"[UI] 读不到 ui_theme.js: {e}", flush=True)
        return

    def cdp():
        with urllib.request.urlopen(f"http://127.0.0.1:{INJECT_PORT}/json", timeout=2) as r:
            targets = json.load(r)
        page = next((t for t in targets if t.get("type") == "page"
                     and str(t.get("url", "")).startswith("http://127.0.0.1")), None)
        if not page:
            page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise RuntimeError("no page target")
        return websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5,
                                           suppress_origin=True)

    def send(ws, i, method, params=None):
        ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i:
                return m

    ws = None
    for _ in range(120):
        try:
            ws = cdp()
            break
        except Exception:
            time.sleep(0.5)
    if ws is None:
        return
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Page.addScriptToEvaluateOnNewDocument", {"source": source})
        ws.send(json.dumps({"id": 3, "method": "Page.reload",
                            "params": {"ignoreCache": True}}))
    except Exception:
        pass
    time.sleep(2)
    try:
        ws.close()
    except Exception:
        pass

    injected = set()
    for _ in range(30):
        try:
            ws = cdp()
            send(ws, 10, "Page.enable")
            tree = send(ws, 11, "Page.getFrameTree").get("result", {}).get("frameTree") or {}
            stack = [tree]
            while stack:
                node = stack.pop()
                frame = node.get("frame") or {}
                fid = frame.get("id")
                url = str(frame.get("url") or "")
                for child in node.get("childFrames") or []:
                    stack.append(child)
                if not fid or fid in injected:
                    continue
                if "16097" not in url and "16099" not in url:
                    continue
                try:
                    r = send(ws, 20 + len(injected), "Page.createIsolatedWorld",
                             {"frameId": fid, "worldName": "dsh_theme"})
                    ctx = (r.get("result") or {}).get("executionContextId")
                    if ctx:
                        send(ws, 40 + len(injected), "Runtime.evaluate",
                             {"contextId": ctx, "expression": source})
                        injected.add(fid)
                except Exception:
                    pass
            ws.close()
            if len(injected) >= 2:
                break
        except Exception:
            pass
        time.sleep(1)
    print(f"[UI] 统一主题已注入（frame {len(injected)} 个）", flush=True)


def serve_headless():
    """服务器模式：本机文件/命令后端 + 手机可访问的 HTTP 面板。"""
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("PANEL_PORT", "8090"))
    public_host = os.environ.get("PANEL_PUBLIC_HOST", "127.0.0.1")
    user = os.environ.get("PANEL_USER", "admin")
    password = os.environ.get("PANEL_PASSWORD", "")
    auth = (user, password) if password else None
    html = HTML_UI.replace("__WEBUI_URL__", "/llbot/").replace(
        "__MAIBOT_BASE__", "http://%s:18001" % public_host).replace(
        "__DSH_THEME__", load_ui_theme())
    api = Api(LocalRemote())
    httpd, actual = start_ui_server(html, api, host=host, port=port, auth=auth)
    print("[手机端] 服务已启动 http://%s:%s/（auth=%s）" % (host, actual, "on" if auth else "off"), flush=True)
    while True:
        time.sleep(3600)


def main():
    if os.environ.get("PANEL_SERVER") == "1":
        serve_headless()
        return
    tunnel = None
    if not port_open(LOCAL_PORT):
        tunnel = Tunnel(SERVER, SSH_USER, SSH_KEY, SSH_PORT,
                        REMOTE_WEBUI_HOST, REMOTE_WEBUI_PORT, LOCAL_PORT)
        tunnel.start()
        for _ in range(50):
            if port_open(LOCAL_PORT):
                break
            time.sleep(0.2)

    maibot_tunnel = None
    if not port_open(MAIBOT_LOCAL_PORT):
        try:
            maibot_tunnel = Tunnel(SERVER, SSH_USER, SSH_KEY, SSH_PORT,
                                   REMOTE_WEBUI_HOST, MAIBOT_REMOTE_PORT, MAIBOT_LOCAL_PORT)
            maibot_tunnel.start()
            for _ in range(50):
                if port_open(MAIBOT_LOCAL_PORT):
                    break
                time.sleep(0.2)
        except Exception as e:
            print(f"[MaiBot 隧道] 建立失败: {e}", flush=True)
            maibot_tunnel = None

    remote = Remote(SERVER, SSH_USER, SSH_KEY, SSH_PORT)
    try:
        remote.connect()
    except Exception as e:
        print(f"[远程] 连接失败: {e}", flush=True)

    api = Api(remote)
    url = f"http://127.0.0.1:{LOCAL_PORT}/"
    html = HTML_UI.replace("__WEBUI_URL__", url).replace(
        "__MAIBOT_BASE__", f"http://127.0.0.1:{MAIBOT_LOCAL_PORT}").replace(
        "__DSH_THEME__", load_ui_theme(remote))

    ui_httpd, ui_port = start_ui_server(html, api)
    ui_url = f"http://127.0.0.1:{ui_port}/"
    print(f"[桌面端] 界面服务 {ui_url}", flush=True)

    webview.settings["REMOTE_DEBUGGING_PORT"] = INJECT_PORT
    window = webview.create_window(WINDOW_TITLE, url=ui_url, width=WINDOW_W, height=WINDOW_H)

    tray = None

    def on_closing():
        print("[桌面端] 最小化到系统托盘", flush=True)
        window.hide()
        return False

    def show_window(icon=None, item=None):
        window.show()

    def quit_app(icon=None, item=None):
        print("[桌面端] 从托盘退出", flush=True)
        if icon:
            icon.stop()
        window.destroy()

    window.events.closing += on_closing

    tray = pystray.Icon(
        "napcat-desktop",
        load_icon(),
        "NapCat 桌面端",
        menu=pystray.Menu(
            pystray.MenuItem("显示面板", show_window, default=True),
            pystray.MenuItem("退出", quit_app),
        ),
    )
    threading.Thread(target=tray.run, daemon=True).start()
    threading.Thread(target=_inject_theme, daemon=True).start()

    webview.start()

    try:
        tray.stop()
    except Exception:
        pass
    if tunnel:
        tunnel.stop()
    if maibot_tunnel:
        maibot_tunnel.stop()


if __name__ == "__main__":
    main()
