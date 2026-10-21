#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llbot 看门狗：按“最后一次登录 / 踢出 / 扫码”事件判定状态。

- 容器没在跑 → 自动拉起；
- 被踢或停在扫码页 → 只标记状态，不反复重启。重启只会换一张新二维码，
  反而让正在扫的那张失效；这种情况需要人扫码。
"""
import calendar
import json
import re
import subprocess
import time

STATUS = "/opt/llbot-watchdog/status.json"
LOG = "/opt/llbot-watchdog/watchdog.log"
COOLDOWN = 300          # 两次自动拉起容器的最小间隔（秒）
CHECK_INTERVAL = 60     # 轮询间隔（秒）
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def sh(cmd, timeout=90):
    """Python 3.6 兼容：不能用 capture_output/text。"""
    try:
        p = subprocess.run(cmd, shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def ts_of(line):
    m = TS.match(line)
    if not m:
        return 0
    try:
        return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


def last_events():
    """返回 (last_login, last_kick, last_qr)，单位 epoch 秒。"""
    raw = sh("docker logs -t --since 168h llbot 2>&1 | tr -d '\\000' | "
             "grep -aE 'Login successful|\\[Kick\\]|请使用手机QQ扫描二维码登录|二维码已过期|已自动刷新'")
    last_login = last_kick = last_qr = 0
    for line in raw.splitlines():
        line = ANSI.sub("", line)
        t = ts_of(line)
        if not t:
            continue
        if "Login successful" in line:
            last_login = max(last_login, t)
        elif "[Kick]" in line:
            last_kick = max(last_kick, t)
        elif ("请使用手机QQ" in line) or ("二维码已过期" in line) or ("已自动刷新" in line):
            last_qr = max(last_qr, t)
    return last_login, last_kick, last_qr


def container_running():
    out = sh("docker inspect -f '{{.State.Running}}' llbot").strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def main():
    last_restart = 0
    prev_state = ""
    try:
        with open(STATUS, "r", encoding="utf-8") as f:
            last_restart = int((json.load(f) or {}).get("last_restart") or 0)
    except Exception:
        pass
    log("看门狗启动")
    while True:
        now = int(time.time())
        state = "unknown"
        online = False
        last_login = last_kick = last_qr = 0
        try:
            running = container_running()
            if running is None:
                state, online = "unknown", False
            elif not running:
                state, online = "down", False
                if now - last_restart > COOLDOWN:
                    log("容器未运行，拉起 llbot")
                    sh("docker start llbot", timeout=180)
                    last_restart = int(time.time())
            else:
                last_login, last_kick, last_qr = last_events()
                if last_login and last_login >= max(last_kick, last_qr):
                    state, online = "online", True
                elif last_qr and last_qr >= max(last_kick, last_login):
                    state, online = "need_qr", False
                elif last_kick:
                    state, online = "kicked", False
                else:
                    state, online = "unknown", False
            if state != prev_state:
                def hm(t):
                    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"
                log("状态变为 %s（login=%s kick=%s qr=%s）" %
                    (state, hm(last_login), hm(last_kick), hm(last_qr)))
                prev_state = state
            data = {
                "online": online,
                "state": state,
                "last_check": now,
                "last_login": last_login,
                "last_kick": last_kick,
                "last_qr": last_qr,
                "last_restart": last_restart,
                "restart_cooldown": COOLDOWN,
            }
            with open(STATUS, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            log("watchdog error: " + str(e))
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
