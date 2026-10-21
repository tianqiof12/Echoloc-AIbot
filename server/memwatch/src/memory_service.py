#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件记忆服务（memory-watch）
================================
只读旁听 NapCat 的群消息，按「群」隔离建事件记忆：
  - 每积累一批消息（空闲 10 分钟 或 20 条）→ 让模型总结成一个"事件"
  - 事件带心情（积极/中性/伤心），按鲸鱼娘性格分配权重：
      积极 = 权重高、伤心 = 权重低
  - 每天衰减一次（半衰期 24h），权重低于 0.1 遗忘
  - 同时根据事件更新该群的群友档案（每群隔离）
  - 群友好感度：事件里出现过的人按心情加减分
      高兴 = 加分、伤心 = 减分，0~100、初始 50，缓慢回归 50
      好感度按群隔离存在各自群文件里，并随画像同步给麦麦
  - 记忆网络：每条群消息都从「说话人 + 消息文本」激活，
      沿「群友—事件—印象」的边扩散两跳，取相关上下文注入画像

存储：DATA_DIR/groups/<group_id>.json
环境变量：NAPCAT_WS_URL / DEEPSEEK_KEY / DATA_DIR / GROUP_WHITELIST(可空)
"""

import hashlib
import ipaddress
import json
import math
import os
import sys
import time
import threading
import re
import socket
from collections import defaultdict
from html import unescape
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

import sqlite3

import websocket
import requests

NAPCAT_WS_URL = os.environ.get("NAPCAT_WS_URL", "ws://napcat:3001")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
CONFUSION_SEARCH = os.environ.get("CONFUSION_SEARCH", "1") == "1"  # 困惑时先联网查一下
CONFUSION_SEARCH_MAX_PER_HOUR = int(os.environ.get("CONFUSION_SEARCH_MAX_PER_HOUR", "60"))
# 补搜不许把额度吃光：留这么多给聊天里实时的困惑查证，否则补搜期间真人提问一句都查不了
CONFUSION_SEARCH_RESERVE = int(os.environ.get("CONFUSION_SEARCH_RESERVE", "12"))
# 命中的 URL 异步抓正文：超时、磁盘缓存、并发上限；只抓搜索第一条，不爬站
PAGE_FETCH = os.environ.get("PAGE_FETCH", "1") == "1"
PAGE_FETCH_TIMEOUT = float(os.environ.get("PAGE_FETCH_TIMEOUT", "6"))
PAGE_FETCH_MAX_CHARS = int(os.environ.get("PAGE_FETCH_MAX_CHARS", "800"))
PAGE_FETCH_CACHE_TTL = int(os.environ.get("PAGE_FETCH_CACHE_TTL", "86400"))
PAGE_FETCH_MAX_WORKERS = int(os.environ.get("PAGE_FETCH_MAX_WORKERS", "2"))
_search_times = []  # 最近一小时的搜索时间戳，防止困惑太多把额度烧光
BACKFILL_LIMIT = int(os.environ.get("CONFUSION_BACKFILL_LIMIT", "2"))
BACKFILL_INTERVAL = int(os.environ.get("CONFUSION_BACKFILL_INTERVAL", "120"))
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "CHANGE_ME_DEEPSEEK_KEY")
MODEL = os.environ.get("MODEL", "deepseek-flash")
EFFORT = os.environ.get("EFFORT", "low")
DATA_DIR = os.environ.get("DATA_DIR", "data/groups")
BOT_ID = os.environ.get("SELF_ID", "")

# MaiBot 桥接：把事件里学到的群友印象同步进麦麦的人物画像
MAIBOT_API = os.environ.get("MAIBOT_API", "http://maibot-core:8001")
MAIBOT_TOKEN = os.environ.get("MAIBOT_TOKEN", "CHANGE_ME_MAIBOT_TOKEN")

GROUP_SETTINGS = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "group_settings.json")
SELF_INTERESTS_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "self_interests.json")
GROUP_INTERESTS_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "group_interests.json")
TALK_FREQ_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "talk_frequency.json")
EXPERIENCE_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "experiences.json")
PAGE_FETCH_CACHE_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "fetch_cache.json")


def load_group_effort(gid):
    """读取该群的推理档位：off / low / high / max"""
    try:
        with open(GROUP_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(gid), {}).get("effort", "low")
    except Exception:
        return "low"


def load_group_energy_cost(gid):
    """读取该会话的精力系数（每 1000 token 扣多少精力）；没有设置就回落到全局。"""
    try:
        with open(GROUP_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get(str(gid), {}).get("energy_cost")
        if raw is None or raw == "":
            return ENERGY_TOKEN_COST
        return max(0.0, float(raw))
    except Exception:
        return ENERGY_TOKEN_COST

EVENT_IDLE_SEC = int(os.environ.get("EVENT_IDLE_SEC", "600"))     # 空闲多久成事件
EVENT_MIN_MSGS = int(os.environ.get("EVENT_MIN_MSGS", "20"))      # 攒够多少条成事件
HALF_LIFE_HOURS = float(os.environ.get("HALF_LIFE_HOURS", "24"))
PRUNE_THRESHOLD = float(os.environ.get("PRUNE_THRESHOLD", "0.1"))
DECAY_INTERVAL_SEC = 3600

# 群友好感度（每个群单独一份，0~100）
AFFINITY_BASE = float(os.environ.get("AFFINITY_BASE", "50"))
AFFINITY_POS_GAIN = float(os.environ.get("AFFINITY_POS_GAIN", "8"))
AFFINITY_SAD_LOSS = float(os.environ.get("AFFINITY_SAD_LOSS", "8"))
IMPRESSION_GAIN = float(os.environ.get("IMPRESSION_GAIN", "3"))
TOPIC_GAIN = float(os.environ.get("TOPIC_GAIN", "2"))
INTEREST_THRESHOLD = float(os.environ.get("INTEREST_THRESHOLD", "3"))
# 兴趣衰减半衰期（小时）——艾宾浩斯遗忘曲线：I(t) = I0 * e^(-λt)
TOPIC_HALF_LIFE_HOURS = float(os.environ.get("TOPIC_HALF_LIFE_HOURS", "72"))
# 心情加权半衰期（小时）——事件对当前心情的影响随时间指数衰减
MOOD_HALF_LIFE_HOURS = float(os.environ.get("MOOD_HALF_LIFE_HOURS", "6"))
MOOD_THRESHOLD = float(os.environ.get("MOOD_THRESHOLD", "0.3"))
# 兴趣上限（有界饱和增长：越接近上限，事件带来的增量越小）
TOPIC_MAX_INTEREST = float(os.environ.get("TOPIC_MAX_INTEREST", "10"))
# 话题遗忘：参考事件，算 topic.act，低于阈值挪进 forgotten_topics（不是删）
TOPIC_FORGET_ACT = float(os.environ.get("TOPIC_FORGET_ACT", "0.05"))
TOPIC_FADE_ACT = float(os.environ.get("TOPIC_FADE_ACT", "0.15"))
TOPIC_ARCHIVE_MAX = int(os.environ.get("TOPIC_ARCHIVE_MAX", "100"))
# 刚出现的话题先留 24 小时，避免兴趣还是 0 时被立刻归档
TOPIC_FORGET_MIN_AGE_H = float(os.environ.get("TOPIC_FORGET_MIN_AGE_H", "24"))
# 自我画像改写：LIF 阈值累积模型（事件影响积分到阈值才改写，平时按半衰期漏泄）
SELF_PROFILE_FILE = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "self_profile.json")
SELF_PLASTICITY_THRESHOLD = float(os.environ.get("SELF_PLASTICITY_THRESHOLD", "4.0"))
SELF_PLASTICITY_LEAK_HOURS = float(os.environ.get("SELF_PLASTICITY_LEAK_HOURS", "12"))
AFFINITY_MIN = 0.0
AFFINITY_MAX = 100.0
AFFINITY_HALF_LIFE_DAYS = float(os.environ.get("AFFINITY_HALF_LIFE_DAYS", "30"))
RESYNC_INTERVAL_SEC = int(os.environ.get("RESYNC_INTERVAL_SEC", "7200"))

# 记忆网络（人 / 事件 / 印象 三类节点，按激活扩散取相关上下文）
NETWORK_TOP_EVENTS = int(os.environ.get("NETWORK_TOP_EVENTS", "3"))
NETWORK_TOP_NOTES = int(os.environ.get("NETWORK_TOP_NOTES", "3"))
NETWORK_TOP_PEOPLE = int(os.environ.get("NETWORK_TOP_PEOPLE", "2"))
NETWORK_TOP_TOPICS = int(os.environ.get("NETWORK_TOP_TOPICS", "3"))
NETWORK_PUSH_INTERVAL_SEC = float(os.environ.get("NETWORK_PUSH_INTERVAL_SEC", "5"))
NETWORK_MIN_SCORE = float(os.environ.get("NETWORK_MIN_SCORE", "0.12"))


def clamp_affinity(v):
    return max(AFFINITY_MIN, min(AFFINITY_MAX, round(float(v), 2)))


def affinity_label(score):
    """好感度档位（给模型看的人话）"""
    if score >= 75:
        return "亲近"
    if score >= 55:
        return "友好"
    if score >= 45:
        return "平常"
    if score >= 30:
        return "冷淡"
    return "反感"


PERSONA = (
    "你是鲸鱼女孩（傲娇、聪明但懒、只认白米饭）。你判断一段群聊"
    "对你来说是哪种情绪（高兴、信任、期待、惊喜、害怕、伤心、嫌弃、生气、困惑、平静），"
    "并总结成一个事件，同时提炼群友信息。"
)

lock = threading.Lock()
buffers = defaultdict(list)          # group_id -> [{name, text, ts}]
last_act = {}                        # group_id -> ts


def llm(prompt, effort=None, return_usage=False):
    effort = effort or EFFORT
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False}
    if effort == "off":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["effort"] = effort
    r = requests.post(DEEPSEEK_URL,
        headers={"Authorization": "Bearer " + DEEPSEEK_KEY, "Content-Type": "application/json"},
        json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"].strip()
    if return_usage:
        return content, (data.get("usage") or {})
    return content


# DeepSeek 官方价格（元 / 百万 tokens），高峰=工作日 9-12、14-18（北京时间）
PRICE_FLASH_OFF = {"hit": 0.02, "miss": 1.0, "out": 4.0}
PRICE_FLASH_PEAK = {"hit": 0.04, "miss": 2.0, "out": 8.0}
PRICE_PRO_OFF = {"hit": 0.15, "miss": 4.5, "out": 13.5}
PRICE_PRO_PEAK = {"hit": 0.30, "miss": 9.0, "out": 27.0}


def _search_budget_left(reserve=0):
    """这一小时还能不能查（reserve = 给实时查证留出来的额度）。"""
    now = time.time()
    with lock:
        _search_times[:] = [t for t in _search_times if now - t < 3600]
        used = len(_search_times)
    return used < max(1, CONFUSION_SEARCH_MAX_PER_HOUR - max(0, reserve))


def web_search(query, count=3, timeout=15):
    """用 DeepSeek 的 tools/search 查一下（和麦麦联网用的是同一个接口）。失败返回 []。"""
    q = str(query or "").strip()
    if not q:
        return []
    now = time.time()
    with lock:
        _search_times[:] = [t for t in _search_times if now - t < 3600]
        if len(_search_times) >= CONFUSION_SEARCH_MAX_PER_HOUR:
            return []
        _search_times.append(now)
    try:
        r = requests.post(DEEPSEEK_SEARCH_URL,
                          headers={"Authorization": "Bearer " + DEEPSEEK_KEY,
                                   "Content-Type": "application/json"},
                          json={"query": q[:128], "count": max(1, min(5, count)),
                                "need_content": False},
                          timeout=timeout)
        r.raise_for_status()
        hits = (r.json() or {}).get("results") or []
        out = []
        for h in hits[:count]:
            out.append({"title": str(h.get("title") or "")[:80],
                        "url": str(h.get("url") or "")[:200],
                        "snippet": str(h.get("snippet") or h.get("summary") or "")[:160]})
        return out
    except Exception as e:
        print("[困惑] 联网查证失败:", e, flush=True)
        return []


# ==================== 命中 URL 的正文抓取（异步 + 缓存 + 超时） ====================
PAGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 memwatch-fetch/1.0")
_fetch_cache = None
_fetch_cache_lock = threading.RLock()
_page_inflight = set()
_page_inflight_lock = threading.Lock()
_page_sem = threading.Semaphore(max(1, PAGE_FETCH_MAX_WORKERS))


def _load_fetch_cache():
    global _fetch_cache
    with _fetch_cache_lock:
        if _fetch_cache is None:
            try:
                with open(PAGE_FETCH_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _fetch_cache = data.get("items") if isinstance(data, dict) else {}
                if not isinstance(_fetch_cache, dict):
                    _fetch_cache = {}
            except Exception:
                _fetch_cache = {}
        return _fetch_cache


def _save_fetch_cache():
    global _fetch_cache
    with _fetch_cache_lock:
        data = {"items": _fetch_cache or {}, "updated": int(time.time())}
        tmp = PAGE_FETCH_CACHE_FILE + ".tmp"
        os.makedirs(os.path.dirname(PAGE_FETCH_CACHE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PAGE_FETCH_CACHE_FILE)


def _cache_get_page(url):
    now = time.time()
    with _fetch_cache_lock:
        items = _load_fetch_cache()
        rec = items.get(url)
        if not isinstance(rec, dict):
            return None
        if now - float(rec.get("ts") or 0) > PAGE_FETCH_CACHE_TTL:
            items.pop(url, None)
            return None
        return rec


def _cache_put_page(url, rec):
    with _fetch_cache_lock:
        items = _load_fetch_cache()
        items[url] = rec
        if len(items) > 200:
            for k, _ in sorted(items.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))[:-200]:
                items.pop(k, None)
        _save_fetch_cache()


def _safe_page_url(url):
    """只抓公网 http(s)，避免搜索结果把内网地址塞进来。"""
    try:
        p = urlparse(str(url or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname.lower().strip(".")
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def _extract_page_text(html_text):
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    if m:
        title = re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(1)))).strip()[:120]
    t = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html_text)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return title, t.strip()


def fetch_page(url):
    """抓一条 URL 的正文；失败返回 {error}，不抛异常。"""
    url = str(url or "").strip()
    if not _safe_page_url(url):
        return {"url": url, "error": "unsafe or invalid url", "ts": int(time.time())}
    try:
        r = requests.get(url, headers={"User-Agent": PAGE_UA,
                                       "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
                                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
                         timeout=(3, PAGE_FETCH_TIMEOUT), stream=True, allow_redirects=True)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if "pdf" in ctype:
            r.close()
            return {"url": url, "error": "pdf unsupported", "ts": int(time.time())}
        raw = b""
        for chunk in r.iter_content(65536):
            raw += chunk
            if len(raw) > 2_000_000:
                break
        r.close()
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";", 1)[0].strip().strip("\"'")
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        title = ""
        if "html" in ctype or "<html" in text[:2000].lower():
            title, text = _extract_page_text(text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"url": r.url, "title": title, "text": text[:PAGE_FETCH_MAX_CHARS],
                "chars": len(text), "ts": int(time.time())}
    except Exception as e:
        return {"url": url, "error": str(e)[:160], "ts": int(time.time())}


def _page_usable(question, rec):
    """拒绝反爬页、PDF、加载页和与问题无关的正文，别把垃圾写进经验。"""
    text = str(rec.get("text") or "").strip()
    if len(text) < 80:
        return False
    head = text[:200]
    if "%PDF" in head:
        return False
    low = text[:1200].lower()
    bad = ("正在确认你是不是机器人", "just a moment", "enable javascript",
           "access denied", "403 forbidden", "404 not found", "加载中...",
           "请开启javascript", "cf-browser-verification", "anubis")
    if any(b in low for b in bad):
        return False
    try:
        if _exp_relevance(question, text[:2500]) < 0.22:
            return False
    except Exception:
        pass
    return True


def _apply_page_fetch(gid, topic, question, hits, url, rec):
    now = int(time.time())
    text = str(rec.get("text") or "").strip()
    title = str(rec.get("title") or "").strip()
    if not text:
        return
    if not _page_usable(question, rec):
        print(f"[抓取] 正文不可用，保留搜索摘要：{url}", flush=True)
        return
    with lock:
        g = load_group(gid)
        t = (g.get("topics") or {}).get(topic)
        if isinstance(t, dict) and str(t.get("pending")) == question:
            ps = t.setdefault("pending_search", {"query": question, "results": hits or [], "ts": now})
            ps["page"] = {"url": url, "title": title, "text": text[:600], "ts": now}
            g["updated"] = now
            save_group(gid, g)
    try:
        record_experience(question, learned=text[:300], hits=hits, topic=topic,
                          gid=str(gid), source="page", status="resolved")
    except Exception as e:
        print(f"[抓取] 经验写入失败: {e}", flush=True)
    try:
        sync_group_interests()
    except Exception as e:
        print(f"[抓取] 刷新提示失败: {e}", flush=True)
    print(f"[抓取] {gid} {topic} -> {len(text)} 字 {('《' + title[:30] + '》') if title else ''}", flush=True)


def _page_worker(gid, topic, question, hits, url):
    try:
        with _page_sem:
            rec = fetch_page(url)
        _cache_put_page(url, rec)
        if rec.get("error"):
            print(f"[抓取] 跳过 {url}: {rec['error']}", flush=True)
            return
        _apply_page_fetch(gid, topic, question, hits, url, rec)
    finally:
        with _page_inflight_lock:
            _page_inflight.discard(url)


def schedule_page_fetch(gid, topic, question, hits):
    """异步抓搜索命中的第一条 URL；命中缓存直接回填，不阻塞事件。"""
    if not PAGE_FETCH or not question or not hits:
        return
    url = ""
    for h in hits[:3]:
        u = str(h.get("url") or "").strip()
        if u and _safe_page_url(u):
            url = u
            break
    if not url:
        return
    cached = _cache_get_page(url)
    if cached and cached.get("text"):
        threading.Thread(target=_apply_page_fetch,
                         args=(gid, topic, question, hits, url, cached), daemon=True).start()
        return
    with _page_inflight_lock:
        if url in _page_inflight:
            return
        _page_inflight.add(url)
    threading.Thread(target=_page_worker,
                     args=(gid, topic, question, hits, url), daemon=True).start()


def _is_peak(ts):
    bj = datetime.fromtimestamp(float(ts), timezone(timedelta(hours=8)))
    if bj.weekday() >= 5:
        return False
    hm = bj.hour * 60 + bj.minute
    return (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)


def usage_cost(usage, ts=None):
    """按 DeepSeek 官方价把 usage 换成人民币"""
    ts = time.time() if ts is None else ts
    usage = usage or {}
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or max(0, prompt - hit))
    out = int(usage.get("completion_tokens") or 0)
    peak = _is_peak(ts)
    if "pro" in str(MODEL):
        p = PRICE_PRO_PEAK if peak else PRICE_PRO_OFF
    else:
        p = PRICE_FLASH_PEAK if peak else PRICE_FLASH_OFF
    return round((hit * p["hit"] + miss * p["miss"] + out * p["out"]) / 1_000_000, 8)


def add_usage(g, usage, ts=None):
    """把一次 LLM 调用的 token 记账到群文件（纯本地累计）"""
    ts = time.time() if ts is None else ts
    usage = usage or {}
    u = g.setdefault("usage", {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0,
        "cost": 0.0, "updated": 0,
    })
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or max(0, prompt - hit))
    out = int(usage.get("completion_tokens") or 0)
    u["requests"] = int(u.get("requests", 0)) + 1
    u["prompt_tokens"] = int(u.get("prompt_tokens", 0)) + prompt
    u["completion_tokens"] = int(u.get("completion_tokens", 0)) + out
    u["total_tokens"] = int(u.get("total_tokens", 0)) + int(usage.get("total_tokens") or (prompt + out))
    u["cache_hit_tokens"] = int(u.get("cache_hit_tokens", 0)) + hit
    u["cache_miss_tokens"] = int(u.get("cache_miss_tokens", 0)) + miss
    u["cost"] = round(float(u.get("cost", 0.0)) + usage_cost(usage, ts), 8)
    u["updated"] = int(ts)
    g["usage"] = u
    # 按北京时间分天记账，供「近 24h」统计
    day = datetime.fromtimestamp(float(ts), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    daily = g.setdefault("usage_daily", {})
    d = daily.setdefault(day, {"requests": 0, "total_tokens": 0, "cost": 0.0})
    d["requests"] = int(d.get("requests", 0)) + 1
    d["total_tokens"] = int(d.get("total_tokens", 0)) + int(usage.get("total_tokens") or (prompt + out))
    d["cost"] = round(float(d.get("cost", 0.0)) + usage_cost(usage, ts), 8)
    if len(daily) > 60:
        for old in sorted(daily.keys())[:-60]:
            daily.pop(old, None)
    g["usage_daily"] = daily
    return u


def _group_path(gid):
    return os.path.join(DATA_DIR, f"{gid}.json")


def load_group(gid):
    try:
        with open(_group_path(gid), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"group_id": str(gid), "events": [], "profiles": {}, "updated": 0}


def save_group(gid, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _group_path(gid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _group_path(gid))


# ==================== 经验库（全局，不按群隔离） ====================
# 记录「原来不懂、后来搞懂」的知识：初始困惑 -> 联网查证/群友解释 -> 一条经验。
# 只存一份 experiences.json，所有群/私聊共享；群号只做来源标记，不参与隔离。
exp_lock = threading.Lock()


def _load_experiences():
    try:
        with open(EXPERIENCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"items": {}, "updated": 0}
        data.setdefault("items", {})
        return data
    except Exception:
        return {"items": {}, "updated": 0}


def _save_experiences(data):
    os.makedirs(os.path.dirname(EXPERIENCE_FILE), exist_ok=True)
    tmp = EXPERIENCE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EXPERIENCE_FILE)


EXP_CATEGORIES = ["梗与暗号", "人物关系", "技术", "常识科普", "群规约定", "其他"]
EXP_CATEGORY_RULES = [
    ("梗与暗号", ["暗号", "黑话", "谐音", "抽象", "整活", "复读", "刷屏", "亚文化", "评论区",
                 "玩梗", "梗百科", "弹幕", "群友整活", "复读机", "表情包"]),
    ("人物关系", ["是谁", "真身", "小号", "马甲", "本体", "群主", "管理员", "同一个人",
                 "假鱼", "盗版", "认主", "关系"]),
    ("技术", ["token", "模型", "部署", "接口", "报错", "服务器", "docker", "数据库", "插件",
             "框架", "显存", "量化", "训练", "prompt", "上下文", "api", "代码", "脚本"]),
    ("群规约定", ["群规", "禁言", "踢人", "禁止", "规定", "刷屏会被", "群公告"]),
    ("常识科普", ["全称", "简称", "定义", "起源", "百科", "一般指", "通常指", "学术", "历史",
                 "地理", "政治", "机构", "公司"]),
]


def guess_exp_category(question, learned=""):
    """先用关键词粗分，命中多的类别优先；都没命中返回空串（交给 LLM）。"""
    text = (str(question or "") + " " + str(learned or ""))[:400].lower()
    best, best_hit = "", 0
    for name, kws in EXP_CATEGORY_RULES:
        hit = sum(1 for k in kws if k.lower() in text)
        if hit > best_hit:
            best, best_hit = name, hit
    return best


def classify_experiences(use_llm=True, fallback_other=True):
    """给还没分类的经验打上类别；规则先过一遍，剩下的批量问一次模型。

    fallback_other=False 时只填规则能确定的，剩下的留空等下一轮：补搜会频繁调它，
    既不想为了几条经验反复烧模型额度，也不想把没想清的硬塞进「其他」就再也翻不了身。
    """
    with exp_lock:
        data = _load_experiences()
        items = data.get("items") or {}
        todo = {k: it for k, it in items.items() if not str(it.get("category") or "").strip()}
        done = 0
        for k, it in todo.items():
            cat = guess_exp_category(it.get("question"), it.get("learned"))
            if cat:
                it["category"] = cat
                done += 1
        left = [(k, it) for k, it in todo.items() if not str(it.get("category") or "").strip()]
        if use_llm and left:
            lines = []
            for i, (k, it) in enumerate(left[:40]):
                lines.append(f'{i}. 问题：{str(it.get("question"))[:60]}｜答案：{_clean_exp_text(it.get("learned"))[:80]}')
            prompt = ("把下面这些「不懂→搞懂」的经验归类。类别只能从这几个里选："
                      + "、".join(EXP_CATEGORIES)
                      + "。只输出 JSON 数组，形如 [{\"i\":0,\"c\":\"梗与暗号\"}]，不要解释。" + chr(10)
                      + chr(10).join(lines))
            try:
                raw, _u = llm(prompt, effort="low", return_usage=True)
                m = re.search(r"\[.*\]", raw, re.S)
                arr = json.loads(m.group(0)) if m else []
                for row in arr:
                    idx = int(row.get("i", -1))
                    cat = str(row.get("c") or "").strip()
                    if 0 <= idx < len(left) and cat in EXP_CATEGORIES:
                        left[idx][1]["category"] = cat
                        done += 1
            except Exception as e:
                print("[经验] 分类失败，先用规则结果:", e, flush=True)
        if fallback_other:
            for it in items.values():
                if not str(it.get("category") or "").strip():
                    it["category"] = "其他"
                    done += 1
        if done:
            data["categories"] = EXP_CATEGORIES
            data["updated"] = int(time.time())
            _save_experiences(data)
        return done


def _experience_key(question):
    norm = re.sub(r"[\s\W_]+", "", str(question or "").lower(), flags=re.UNICODE)
    return "x:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:14]


def _clean_exp_text(text):
    t = str(text or "")
    t = re.sub(r"<[^>]{1,200}>", " ", t)
    t = re.sub(r"\{\{[^{}]{0,400}\}\}", " ", t)
    t = re.sub(r"\[\[[^\[\]]{0,300}\]\]", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -—_·")
    return t


def _exp_terms(text):
    # 用二元组而不是单字：单字重合会把「峰谷」和「文峰」误判成相关
    norm = re.sub(r"[\s\W_]+", "", str(text).lower(), flags=re.UNICODE)
    if len(norm) < 2:
        return set(norm)
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _exp_relevance(q, text):
    qs, ts = _exp_terms(q), _exp_terms(text)
    if not qs or not ts:
        return 0.0
    return len(qs & ts) / max(1, len(qs))


def record_experience(question, learned="", hits=None, topic="", gid="", source="search", status=None):
    """写入/更新一条经验；同义问题按归一化 hash 合并，不重复堆。

    搜索片段先清洗、再按问题关键词算相关度；相关度太低的网页不硬塞进经验，
    宁可不记，也不要把「搜到的不相干内容」当成搞懂了。
    """
    q = str(question or "").strip()[:120]
    if not q:
        return None
    hits = hits or []
    best_text, best_score, best_hit = "", 0.0, None
    for h in hits[:5]:
        title = _clean_exp_text(h.get("title"))[:120]
        snip = _clean_exp_text(h.get("snippet"))[:240]
        text = (snip or title).strip()
        if len(text) < 8:
            continue
        score = _exp_relevance(q, title + " " + snip)
        if score > best_score:
            best_text, best_score, best_hit = text, score, h
    learned = _clean_exp_text(learned)[:220]
    if not learned:
        if best_score < 0.28:
            return None
        learned = best_text[:200]
    st = status or ("resolved" if learned else "open")
    now = int(time.time())
    key = _experience_key(q)
    with exp_lock:
        data = _load_experiences()
        items = data.setdefault("items", {})
        old = items.get(key) or {}
        if not learned and old.get("learned"):
            learned = str(old.get("learned"))[:220]
            st = old.get("status") or "resolved"
        sources = list(old.get("sources") or [])
        for h in hits[:3]:
            item = {"title": _clean_exp_text(h.get("title"))[:80],
                    "url": str(h.get("url") or "")[:200],
                    "snippet": _clean_exp_text(h.get("snippet"))[:160]}
            if item not in sources:
                sources.append(item)
        rec = {
            "id": key,
            "question": q,
            "learned": learned,
            "status": "resolved" if st == "resolved" and learned else "open",
            "source": source if learned else (old.get("source") or source),
            "sources": sources[-6:],
            "topic": str(topic or old.get("topic") or "")[:40],
            "first_gid": str(old.get("first_gid") or gid or ""),
            "created": int(old.get("created") or now),
            "updated": now,
            "hits": int(old.get("hits", 0)) + 1,
        }
        items[key] = rec
        data["updated"] = now
        _save_experiences(data)
    print(f"[经验] {'已解' if rec['status'] == 'resolved' else '待解'}：{q[:30]}"
          + (f" -> {learned[:50]}" if learned else ""), flush=True)
    return rec

def resolve_experience(question, learned, topic="", gid="", source="later"):
    """后来被群友解释/自己想通了：把对应的待解经验补成已解。"""
    if not str(learned or "").strip():
        return None
    return record_experience(question, learned, topic=topic, gid=gid, source=source, status="resolved")


def experience_context(limit=3):
    """给会话提示用的全局经验摘要，取最近搞懂的几条。"""
    data = _load_experiences()
    items = [v for v in (data.get("items") or {}).values()
             if isinstance(v, dict) and v.get("status") == "resolved" and v.get("learned")]
    items.sort(key=lambda v: -int(v.get("updated") or 0))
    lines = []
    for it in items[:max(1, int(limit))]:
        q = str(it.get("question") or "").strip()
        a = str(it.get("learned") or "").strip()
        if q and a:
            lines.append(f"- 以前不懂「{q[:50]}」，后来懂了：{a[:120]}")
    return "\n".join(lines)



MOOD_WEIGHT = {"positive": 1.0, "neutral": 0.6, "sad": 0.3}

# ==================== 情绪空间（PAD） ====================
# 参考 Mehrabian 的 PAD（Pleasure-Arousal-Dominance）模型与 Plutchik 八情绪轮：
# 每种情绪是一个三维原型，事件 = 原型 × 强度；群心情 = 近期事件的加权 PAD 重心。
EMOTIONS = {
    "joy":          {"cn": "高兴", "v": 0.85, "a": 0.55, "d": 0.55, "base": 1.0},
    "trust":        {"cn": "信任", "v": 0.65, "a": 0.15, "d": 0.35, "base": 0.8},
    "anticipation": {"cn": "期待", "v": 0.45, "a": 0.45, "d": 0.40, "base": 0.8},
    "surprise":     {"cn": "惊喜", "v": 0.20, "a": 0.90, "d": -0.25, "base": 1.1},
    "fear":         {"cn": "害怕", "v": -0.70, "a": 0.80, "d": -0.65, "base": 1.1},
    "sadness":      {"cn": "伤心", "v": -0.80, "a": -0.30, "d": -0.50, "base": 0.8},
    "disgust":      {"cn": "嫌弃", "v": -0.70, "a": 0.20, "d": 0.30, "base": 0.9},
    "anger":        {"cn": "生气", "v": -0.85, "a": 0.75, "d": 0.60, "base": 1.2},
    "confusion":    {"cn": "困惑", "v": -0.20, "a": 0.50, "d": -0.55, "base": 0.7},
    "neutral":      {"cn": "平静", "v": 0.0, "a": 0.0, "d": 0.0, "base": 0.5},
}

# 正性记忆增益（Pollyanna 效应：人对高兴的事记得更牢、回忆时更容易被调出来）。
# 单独一张表而不是直接改 base：base 描述「情绪本身的显著性」，这张表描述
# 「记忆偏好」，两者混在一起以后就没法单独调了。
# 只影响事件记多深（weight → act → 遗忘快慢、话题兴趣增量），
# 不影响好感度增量（那用 v 和 intensity）也不影响当下心情。
POSITIVE_MEMORY_GAIN = {"joy": 1.35, "trust": 1.15, "anticipation": 1.10}

# ==================== 情绪 → 技能（EMOTION2SKILL） ====================
# 依据中科大/牛津等《Emotion2Skill》思路：让 Agent 用内部情绪向量选技能，而不是
# 只靠文本反馈。「困惑+紧张」对应「查询重述（query reformulation）」——换关键词
# 重新提问；「好奇+渴望」对应主动检索。困惑是元认知失配信号，是鲁棒性来源，
# 所以它应该把话题往「想弄明白」推（兴趣正向），而不是像嫌弃那样回避。
# 参考：https://www.36kr.com/p/3941857474854016
EMOTION2SKILL = {
    "joy":          "engage",       # 高兴 → 顺着聊
    "trust":        "engage",       # 信任 → 顺着聊
    "anticipation": "explore",      # 期待 → 主动探索 / 找资料
    "surprise":     "explore",      # 惊喜 → 追着看
    "confusion":    "reformulate",  # 困惑 → 换说法重新问 / 查证
    "fear":         "probe",        # 害怕 → 小心探问
    "sadness":      "soothe",       # 伤心 → 安抚
    "disgust":      "avoid_topic",  # 嫌弃 → 少碰这个话题
    "anger":        "defuse",       # 生气 → 降温
    "neutral":      "chat",         # 平静 → 闲聊
}
SKILL_CN = {
    "engage": "顺着聊", "explore": "主动探索", "reformulate": "换词追问",
    "probe": "小心探问", "soothe": "安抚", "avoid_topic": "避让话题",
    "defuse": "降温", "chat": "闲聊",
}
# 这些技能会「往话题里钻」，兴趣应该加，跟效价正负无关
SKILL_CURIOSITY = ("explore", "reformulate", "probe")
EMOTION_ALIASES = {
    "positive": "joy", "happy": "joy", "高兴": "joy", "开心": "joy", "快乐": "joy",
    "sad": "sadness", "negative": "sadness", "伤心": "sadness", "难过": "sadness",
    "trust": "trust", "信任": "trust", "喜欢": "trust",
    "anticipation": "anticipation", "期待": "anticipation", "期待感": "anticipation",
    "surprise": "surprise", "surprised": "surprise", "惊喜": "surprise", "惊讶": "surprise", "意外": "surprise",
    "fear": "fear", "afraid": "fear", "害怕": "fear", "恐惧": "fear",
    "disgust": "disgust", "disgusted": "disgust", "嫌弃": "disgust", "厌恶": "disgust",
    "anger": "anger", "angry": "anger", "生气": "anger", "愤怒": "anger",
    "confusion": "confusion", "confused": "confusion", "困惑": "confusion", "疑惑": "confusion", "不解": "confusion",
    "neutral": "neutral", "normal": "neutral", "平静": "neutral", "中性": "neutral", "普通": "neutral",
}
AFFINITY_EMOTION_SCALE = float(os.environ.get("AFFINITY_EMOTION_SCALE", "1.4"))


def coerce_emotion(value):
    """把模型输出的中英文/旧 mood 统一成情绪 key"""
    key = str(value or "").strip().lower()
    if key in EMOTIONS:
        return key
    return EMOTION_ALIASES.get(key, "neutral")


def emotion_cn(key):
    return EMOTIONS.get(coerce_emotion(key), EMOTIONS["neutral"])["cn"]


def emotion_pad(key, intensity=1.0):
    e = EMOTIONS.get(coerce_emotion(key), EMOTIONS["neutral"])
    k = max(0.0, min(1.0, float(intensity or 0)))
    return [round(e["v"] * k, 4), round(e["a"] * k, 4), round(e["d"] * k, 4)]


def emotion_weight(key, intensity=1.0):
    """情绪显著性：强度 × 情绪基准 × (1 + 0.6·|效价|·唤醒) × 正性记忆增益

    唤醒度高的情绪事件在记忆里更“响”，对应情绪唤醒增强记忆巩固的模型。
    高兴/信任/期待再乘一档正性增益（POSITIVE_MEMORY_GAIN），让好事记得更牢。
    """
    key = coerce_emotion(key)
    e = EMOTIONS.get(key, EMOTIONS["neutral"])
    k = max(0.0, min(1.0, float(intensity or 0)))
    salience = 1.0 + 0.6 * abs(float(e["v"])) * float(e["a"])
    gain = POSITIVE_MEMORY_GAIN.get(key, 1.0)
    return round(min(1.8, max(0.15, float(e["base"]) * k * salience * gain)), 3)


def emotion_stability(key, intensity=1.0):
    """情绪事件的记忆稳定性（小时）：唤醒越高，衰减越慢"""
    e = EMOTIONS.get(coerce_emotion(key), EMOTIONS["neutral"])
    k = max(0.0, min(1.0, float(intensity or 0)))
    return round(HALF_LIFE_HOURS * (1.0 + 0.8 * abs(float(e["v"])) * float(e["a"]) * k), 1)


def event_emotion(ev):
    """事件的细情绪：新事件用 emotion，旧事件按 mood 映射"""
    if not isinstance(ev, dict):
        return "neutral"
    if ev.get("emotion"):
        return coerce_emotion(ev["emotion"])
    return coerce_emotion(ev.get("mood"))


def event_pad(ev):
    if isinstance(ev, dict) and isinstance(ev.get("pad"), (list, tuple)) and len(ev["pad"]) >= 3:
        try:
            return [float(ev["pad"][0]), float(ev["pad"][1]), float(ev["pad"][2])]
        except (TypeError, ValueError):
            pass
    return emotion_pad(event_emotion(ev), (ev or {}).get("intensity", 0.7))


def nearest_emotion(pad):
    """PAD 重心 -> 最近的情绪原型（先按方向归一，避免强度把距离拉偏）"""
    p, a, d = [float(x) for x in pad]
    norm = math.sqrt(p * p + a * a + d * d)
    if norm < 0.08:
        return "neutral", 0.0
    u = (p / norm, a / norm, d / norm)
    best, best_d = "neutral", 9.9
    for key, e in EMOTIONS.items():
        if key == "neutral":
            continue
        ev = (e["v"], e["a"], e["d"])
        en = math.sqrt(ev[0] ** 2 + ev[1] ** 2 + ev[2] ** 2) or 1.0
        dist = math.sqrt(sum((u[i] - ev[i] / en) ** 2 for i in range(3)))
        if dist < best_d:
            best, best_d = key, dist
    return best, norm


def _blend_group_mood(old, pad, now, intensity=0.7):
    """把新事件的 PAD 慢慢并进群心情：旧的先按半衰期衰减，再小比例吸收新事件。"""
    now = float(now)
    op = [0.0, 0.0, 0.0]
    if isinstance(old, dict):
        old_pad = old.get("pad") or []
        if isinstance(old_pad, (list, tuple)) and len(old_pad) == 3:
            age_h = max(0.0, (now - float(old.get("updated") or now)) / 3600.0)
            decay = 0.5 ** (age_h / max(0.5, MOOD_HALF_LIFE_HOURS))
            op = [float(x) * decay for x in old_pad]
    alpha = max(0.08, min(0.30, 0.12 + 0.22 * float(intensity)))
    npad = [round(op[i] * (1.0 - alpha) + float(pad[i]) * alpha, 4) for i in range(3)]
    key, strength = nearest_emotion(npad)
    return {"pad": npad, "emotion": key, "label": emotion_cn(key),
            "valence": npad[0], "arousal": npad[1], "dominance": npad[2],
            "intensity": round(min(1.0, strength), 3), "updated": int(now)}


def _aggregate_group_mood(g):
    """旧数据/没有持久化心情时：近期事件 PAD 的时间衰减重心。"""
    evs = sorted(g.get("events", []), key=lambda e: e.get("ts", 0))[-12:]
    now = time.time()
    lam = math.log(2) / max(0.5, MOOD_HALF_LIFE_HOURS)
    sp = sa = sd = sw = 0.0
    for e in evs:
        age_h = max(0.0, (now - float(e.get("ts", now))) / 3600.0)
        w = float(e.get("weight", 0.6)) * math.exp(-lam * age_h)
        if w <= 0:
            continue
        pad = event_pad(e)
        sp += pad[0] * w
        sa += pad[1] * w
        sd += pad[2] * w
        sw += w
    if sw <= 0:
        return {"emotion": "neutral", "label": "平静", "pad": [0.0, 0.0, 0.0],
                "valence": 0.0, "arousal": 0.0, "dominance": 0.0, "intensity": 0.0}
    pad = [round(sp / sw, 4), round(sa / sw, 4), round(sd / sw, 4)]
    topics = g.get("topics") or {}
    bonus = min(1.0, sum(float(v.get("interest", 0)) for v in topics.values()
                         if isinstance(v, dict) and v.get("interested")) * 0.05)
    if bonus:
        pad[0] = round(pad[0] + bonus, 4)
    key, strength = nearest_emotion(pad)
    return {"emotion": key, "label": emotion_cn(key), "pad": pad,
            "valence": pad[0], "arousal": pad[1], "dominance": pad[2],
            "intensity": round(min(1.0, strength), 3)}


def compute_group_emotion(gid):
    """群当前心情：优先读持久化的群心情（各事件共用，群隔离）；旧数据回落到近期事件重心。"""
    g = load_group(gid)
    stored = g.get("mood")
    if isinstance(stored, dict) and isinstance(stored.get("pad"), (list, tuple)) and len(stored["pad"]) == 3:
        now = time.time()
        age_h = max(0.0, (now - float(stored.get("updated") or now)) / 3600.0)
        decay = 0.5 ** (age_h / max(0.5, MOOD_HALF_LIFE_HOURS))
        pad = [round(float(x) * decay, 4) for x in stored["pad"]]
        key, strength = nearest_emotion(pad)
        return {"emotion": key, "label": emotion_cn(key), "pad": pad,
                "valence": pad[0], "arousal": pad[1], "dominance": pad[2],
                "intensity": round(min(1.0, strength), 3),
                "updated": stored.get("updated")}
    return _aggregate_group_mood(g)


def _resolve_uid(name, name2uid):
    """昵称 -> QQ 号；模型偶尔会写全名/简称，做一次包含匹配兜底"""
    if not name:
        return None
    if name in name2uid:
        return name2uid[name]
    for n, u in name2uid.items():
        if n and (n in name or name in n):
            return u
    return None


def global_affinity_score(uid, exclude_gid=None):
    """这个 QQ 号在其他群的已有好感度（按互动次数加权），没有记录就返回 None。

    用途：人第一次在某个群冒头时，用这个当前值当起点，避免「换个群就从 50 重新认识」。
    """
    total, wsum = 0.0, 0
    for gid2, g2 in _all_group_data():
        if exclude_gid is not None and str(gid2) == str(exclude_gid):
            continue
        ent = (g2.get("affinity") or {}).get(str(uid))
        if not isinstance(ent, dict):
            continue
        w = max(1, int(ent.get("events") or 0))
        total += float(ent.get("score", AFFINITY_BASE)) * w
        wsum += w
    if not wsum:
        return None
    return clamp_affinity(total / wsum)


def sync_affinity():
    """同一个 QQ 号在不同群的好感度统一成一个数。

    好感度是「我对这个人的总体关系」，不该因为人同时在两个群就变成两副面孔。
    合并口径：按各群互动次数（events）加权平均——不是相加，否则群越多分越高。
    群内统计（pos/sad/events）保持各群独立，那是活跃度，不是态度。
    调用点：make_event 落盘之后（此时不持有 lock）。
    """
    now = int(time.time())
    dirty, unified = {}, 0
    with lock:
        data = _all_group_data()
        by_uid = {}
        for gid, g in data:
            for uid, ent in (g.get("affinity") or {}).items():
                if isinstance(ent, dict):
                    by_uid.setdefault(str(uid), []).append((gid, ent))
        for uid, items in by_uid.items():
            if len(items) < 2:
                continue
            weights = [max(1, int(e.get("events") or 0)) for _, e in items]
            total = sum(weights)
            score = clamp_affinity(sum(float(e.get("score", AFFINITY_BASE)) * w
                                       for (_, e), w in zip(items, weights)) / total)
            if all(abs(float(e.get("score", AFFINITY_BASE)) - score) < 0.05 for _, e in items):
                continue
            for gid, ent in items:
                ent["score"] = score
                # 统一后各群衰减起点对齐，之后同样公式走，不会再自行漂开
                ent["decayed"] = now
                ent["updated"] = max(int(ent.get("updated") or 0), now)
                dirty[gid] = True
            unified += 1
        for gid, g in data:
            if dirty.get(gid):
                save_group(gid, g)
    if dirty:
        print(f"[好感] 跨群统一 {unified} 人 / {len(dirty)} 个群", flush=True)
    return unified


def build_event_prompt(gid, msgs):
    """事件分类提示词（单独抽出来，方便离线复测，不用复制粘贴一份）。"""
    lines = "\n".join(f"{m['name']}: {m['text']}" for m in msgs[-40:])
    scene = "下面是一段 QQ 私聊（只有我和对方两个人）：" if is_private(gid) else "下面是一段 QQ 群聊："
    return (
        PERSONA + "\n" + scene + "\n" + lines +
        "\n\n请只输出 JSON：{\"emotion\": \"joy|trust|anticipation|surprise|fear|sadness|disgust|anger|confusion|neutral\", "
        "\"secondary\": \"可选，第二种情绪\", \"intensity\": 0.0到1.0的情绪强度, \"topic\": \"这段聊的话题（4-8字）\", "
        "\"summary\": \"一句话事件概括\", \"question\": \"如果这里有看不懂/需要澄清的地方，写一句下次想问的话，否则空字符串\", "
        "\"members\": [{\"name\": \"对方在对话里显示的名字\", \"note\": \"关于这个人的一句话印象\", \"sentiment\": -1.0到1.0的观感}]}\n"
        "emotion 选这段对话给你的整体情绪（看群友在表达什么、气氛怎样），不是「你有没有把每个词都看懂」："
        "joy=高兴/信任=trust/期待=anticipation/惊喜=surprise/害怕=fear/伤心=sadness/嫌弃=disgust/生气=anger/困惑=confusion/平静=neutral；"
        "先判断「他们在聊什么事」能不能说清：整段主要是暗号/复读/没头没尾、说不清在聊什么，才选 confusion；"
        "能说清在聊什么、只是个别缩写或梗不懂，就按真实气氛选（热闹 joy/anticipation、看不惯 disgust、吵架 anger、普通 neutral），"
        "把不懂的词写进 question 就行，别因为一两个词没看懂就把整段判成 confusion；"
        "question 与主情绪无关：任何时候有看不懂/需要澄清的地方都写，没有就留空字符串（别硬编解释）；"
        "intensity 是情绪强度（0=很淡、1=很强烈）；topic 用 4-8 个字概括聊的核心话题；summary 不超过 40 字；members 最多 5 人；"
        "sentiment 是你对这个人这次表现的观感（-1=很不喜欢、0=无所谓、1=很欣赏）。"
    )


def make_event(gid, msgs):
    prompt = build_event_prompt(gid, msgs)
    usage = {}
    try:
        out, usage = llm(prompt, effort=load_group_effort(gid), return_usage=True)
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"[事件] 生成失败 {e}", flush=True)
        return
    emotion = coerce_emotion(data.get("emotion") or data.get("mood"))
    secondary = coerce_emotion(data.get("secondary")) if data.get("secondary") else ""
    if secondary == "neutral":
        secondary = ""
    summary = str(data.get("summary", "")).strip()[:120] or "（无摘要）"
    topic = str(data.get("topic", "")).strip()[:30]
    question = str(data.get("question", "") or "").strip()[:60]
    # 看不懂的东西先联网查一下（锁外做，别阻塞其它事件）
    search_hits = web_search(question) if (CONFUSION_SEARCH and question) else []
    if question and search_hits:
        # 查到了 = 这条知识从「不懂」变成「懂」，存进全局经验库
        try:
            record_experience(question, hits=search_hits, topic=topic, gid=str(gid), source="search")
        except Exception as e:
            print(f"[经验] 记录失败: {e}", flush=True)
        # 只抓搜索命中的第一条 URL，异步进行，不阻塞事件生成/回复
        schedule_page_fetch(str(gid), topic, question, search_hits)
    members = data.get("members", []) or []
    try:
        intensity = float(data.get("intensity", 0.7) or 0.7)
    except (TypeError, ValueError):
        intensity = 0.7
    intensity = max(0.0, min(1.0, intensity))
    info = EMOTIONS[emotion]
    pad = emotion_pad(emotion, intensity)
    skill = EMOTION2SKILL.get(emotion, "chat")
    # 困惑是信息缺口，不是谁做错了：好感度按中性记，别拿它扣群友分
    if emotion == "confusion":
        mood = "neutral"
    else:
        mood = "positive" if info["v"] > 0.2 else ("sad" if info["v"] < -0.2 else "neutral")
    ev_weight = emotion_weight(emotion, intensity)
    now = int(time.time())
    name2uid = {}
    for m in msgs:
        if m.get("uid"):
            name2uid[m["name"]] = str(m["uid"])
    # 困惑是信息缺口，不是过错：不给群友加减好感（观感 imp_delta 仍单独算）
    if emotion == "confusion":
        delta = 0.0
    else:
        delta = round(AFFINITY_POS_GAIN * info["v"] * intensity * AFFINITY_EMOTION_SCALE, 2)
    applied = []
    participants = []
    seen_uid = set()
    msg_counts = defaultdict(int)
    for m in msgs:
        uid = str(m.get("uid") or "")
        if uid:
            msg_counts[uid] += 1
        if uid and uid not in seen_uid:
            seen_uid.add(uid)
            participants.append({"uid": uid, "name": m.get("name") or uid})
    for p in participants:
        p["count"] = int(msg_counts.get(p["uid"], 0))
    exp_updated = bool(question and search_hits)
    with lock:
        g = load_group(gid)
        add_usage(g, usage, now)
        g["events"].append({
            "id": f"{now}-{len(g['events'])}",
            "topic": topic,
            "summary": summary,
            "emotion": emotion,
            "secondary": secondary,
            "skill": skill,
            "question": question,
            "pad": pad,
            "mood": mood,
            "weight": ev_weight,
            "stability": emotion_stability(emotion, intensity),
            "intensity": intensity,
            "ts": now,
            "participants": participants[:12],
        })
        # 群级实时心情：所有事件共用一个，按小比例吸收这次事件，避免被单条事件带跑
        g["mood"] = _blend_group_mood(g.get("mood"), pad, now, intensity)
        # 话题重现时强化旧事件（间隔重复）：权重回补、稳定性增长
        if topic:
            for _ev in g["events"][:-1]:
                if _ev.get("topic") == topic:
                    _ev["weight"] = round(min(1.5, float(_ev.get("weight", 0)) + 0.2), 3)
                    _ev["stability"] = round(min(720.0, float(_ev.get("stability") or HALF_LIFE_HOURS) * 1.5), 1)
        # 每群隔离的群友档案
        prof = g.setdefault("profiles", {})
        # 每群隔离的群友好感度（key 用 QQ 号，防止改名丢档）
        aff = g.setdefault("affinity", {})
        for mm in members[:5]:
            name = str(mm.get("name", "")).strip()
            note = str(mm.get("note", "")).strip()
            try:
                senti = float(mm.get("sentiment", 0) or 0)
            except (TypeError, ValueError):
                senti = 0.0
            senti = max(-1.0, min(1.0, senti))
            imp_delta = round(senti * IMPRESSION_GAIN, 2)
            if name and note:
                p = prof.get(name, {"notes": [], "updated": 0})
                if note not in p["notes"]:
                    p["notes"].append(note)
                    p["notes"] = p["notes"][-8:]
                p["updated"] = now
                # 印象的情绪权重：情绪越强、观感越极端，这条印象越“刻得深”
                ns = p.setdefault("note_scores", {})
                old_ns = ns.get(note) or {}
                note_w = ev_weight * (1.0 + 0.5 * abs(info["v"]) * info["a"]) * (0.7 + 0.3 * max(0.0, senti))
                note_w = min(2.5, float(old_ns.get("weight", 0)) * 0.9 + note_w)
                ns[note] = {"emotion": emotion, "pad": pad, "weight": round(note_w, 3), "ts": now}
                for _k in list(ns.keys()):
                    if _k not in p["notes"]:
                        ns.pop(_k, None)
                prof[name] = p
            uid = _resolve_uid(name, name2uid)
            if not uid and is_private(gid) and len(participants) == 1:
                uid = participants[0]["uid"]   # 私聊里只有对方一个人
            if not uid:
                continue
            ent = aff.get(uid)
            if not ent:
                # 第一次在这个群出现：直接继承别处已经攒下的关系，不要从 50 重新认识一遍
                seed = global_affinity_score(uid, gid)
                ent = {
                    "name": name,
                    "score": seed if seed is not None else AFFINITY_BASE,
                    "pos": 0,
                    "sad": 0,
                    "events": 0,
                    "updated": now,
                    "decayed": now,
                }
            ent["name"] = name
            p_count = next((int(p.get("count", 0)) for p in participants if p.get("uid") == uid), 0)
            engage = 0.55 + 0.45 * min(1.0, p_count / 6.0)
            total_delta = round(delta * engage + imp_delta, 2)
            ent["engagement"] = round(0.7 * float(ent.get("engagement", engage)) + 0.3 * engage, 3)
            if total_delta:
                ent["score"] = clamp_affinity(float(ent.get("score", AFFINITY_BASE)) + total_delta)
            if info["v"] > 0.2:
                ent["pos"] = int(ent.get("pos", 0)) + 1
            elif info["v"] < -0.2:
                ent["sad"] = int(ent.get("sad", 0)) + 1
            ent["events"] = int(ent.get("events", 0)) + 1
            ent["last_mood"] = mood
            ent["last_emotion"] = emotion
            emo_counts = ent.setdefault("emotions", {})
            emo_counts[emotion] = int(emo_counts.get(emotion, 0)) + 1
            ent["updated"] = now
            ent.setdefault("decayed", now)
            aff[uid] = ent
            applied.append({"uid": uid, "name": name, "delta": total_delta, "score": ent["score"]})
        if topic:
            topics = g.setdefault("topics", {})
            t = topics.get(topic) or {"interest": 0.0, "count": 0, "interested": False,
                                      "updated": now, "last": now}
            dt_h = max(0.0, (now - int(t.get("last", now))) / 3600.0)
            lam = math.log(2) / max(1.0, TOPIC_HALF_LIFE_HOURS)
            base = float(t.get("interest", 0)) * math.exp(-lam * dt_h)
            # 有界饱和强化：越接近上限，这次事件带来的增量越小（边际递减）
            ratio = min(1.0, abs(base) / max(0.1, TOPIC_MAX_INTEREST))
            # 兴趣由「技能」分派，而不是单看效价（Emotion2Skill）：
            # 探索/追问类话题即使效价为负也加兴趣——信息缺口越大越想弄明白；
            # 只有避让类（嫌弃/生气等）才按效价扣兴趣。
            if skill in SKILL_CURIOSITY:
                # 缺口大小决定好奇强度：用 |v|，困惑(v=-0.2)与惊喜(v=+0.2)同等待遇
                drive = abs(float(info["v"]))
            else:
                drive = float(info["v"])
            delta_t = TOPIC_GAIN * drive * ev_weight * (1.0 + 0.5 * info["a"]) * (1.0 - ratio)
            t["interest"] = round(base + delta_t, 2)
            t["count"] = int(t.get("count", 0)) + 1
            t["interested"] = bool(float(t["interest"]) >= INTEREST_THRESHOLD)
            t["act"] = topic_activation(t)
            t["emotion"] = emotion
            t["skill"] = skill
            t["pad"] = pad
            # 悬空的疑问：困惑时记下来，等同类话题再出现时追；解释清楚就清掉
            if question:
                t["pending"] = question
                # 默认处理：先联网查；查到就存结果，没查到也留个 checked 标记
                if CONFUSION_SEARCH:
                    t["pending_search"] = {"query": question, "results": search_hits or [],
                                           "checked": True, "ts": now}
                else:
                    t.pop("pending_search", None)
            elif skill not in SKILL_CURIOSITY:
                if t.get("pending") and summary and summary != "（无摘要）":
                    # 后来同一个话题被解释清楚了：补一条「原来不懂，后来搞懂」的经验
                    try:
                        resolve_experience(t["pending"], summary, topic=topic, gid=str(gid), source="later")
                        exp_updated = True
                    except Exception as e:
                        print(f"[经验] 补全失败: {e}", flush=True)
                t.pop("pending", None)
                t.pop("pending_search", None)
            t["valence"] = round(0.7 * float(t.get("valence", 0)) + 0.3 * info["v"], 3)
            t["last"] = now
            t["updated"] = now
            topics[topic] = t
        g["updated"] = now
        refresh_activation(g, now)
        save_group(gid, g)
    if exp_updated:
        try:
            sync_group_interests()
        except Exception as e:
            print(f"[经验] 刷新会话提示失败: {e}", flush=True)
    # 事件只落在当前群，但好感度是「对这个人」的，跨群摊平后才不会一人两副面孔
    try:
        sync_affinity()
    except Exception as e:
        print(f"[好感] 跨群统一失败: {e}", flush=True)
    if applied:
        print(f"[好感] {gid} [{emotion_cn(emotion)}] " + "，".join(f"{a['name']} {a['delta']:+.0f} → {a['score']:.0f}"
                                                    for a in applied), flush=True)
    print(f"[事件] {gid} [{emotion_cn(emotion)}/{SKILL_CN.get(skill, skill)}] {summary[:30]}"
          + (f" | 想问：{question}" if question else ""), flush=True)
    try:
        sync_members(gid, msgs, members)
    except Exception as e:
        print(f"[桥接] 同步失败: {e}", flush=True)
    try:
        maybe_rewrite_self(gid, summary, topic, emotion_cn(emotion), ev_weight)
    except Exception as e:
        print(f"[自我] 处理失败: {e}", flush=True)


_mb_session = None


def _mb_ensure():
    global _mb_session
    if _mb_session is None:
        s = requests.Session()
        s.post(MAIBOT_API + "/api/webui/auth/verify", json={"token": MAIBOT_TOKEN}, timeout=10)
        _mb_session = s
    return _mb_session


def _mb_post(path, payload):
    _mb_ensure().post(MAIBOT_API + path, json=payload, timeout=10)


def compute_group_mood(gid):
    """兼容旧接口：由 PAD 情绪重心映射到 happy/sad/neutral"""
    e = compute_group_emotion(gid)
    if e["valence"] > 0.15:
        return "happy"
    if e["valence"] < -0.15:
        return "sad"
    return "neutral"


def _all_group_data():
    """[(gid, group_data), ...] —— 只读全部群文件"""
    try:
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        gid = name[:-5]
        out.append((gid, load_group(gid)))
    return out


def build_profile_text(uid, current_gid=None, current_name=None):
    """把该 QQ 号在所有群里的好感度 + 印象拼成画像文本（按群分段）。

    麦麦的人物画像是按 QQ 号全局存的，所以这里把每个群单独一段写进去，
    并标出本次事件来自哪个群，让它按当前所在群取对应那段。
    """
    sections = []
    for gid, g in _all_group_data():
        ent = (g.get("affinity") or {}).get(str(uid))
        if not ent and gid != current_gid:
            continue
        name = (ent or {}).get("name") or (current_name if gid == current_gid else "")
        prof_entry = ((g.get("profiles") or {}).get(name) or {})
        notes = prof_entry.get("notes", [])
        note_scores = prof_entry.get("note_scores") or {}
        score = float((ent or {}).get("score", AFFINITY_BASE))
        em = compute_group_emotion(gid)
        mood_label = em["label"]
        pad_label = f"P{em['valence']:+.2f}/A{em['arousal']:+.2f}/D{em['dominance']:+.2f}"
        tag = f"【群 {gid} · 本次更新】" if gid == current_gid else f"【群 {gid}】"
        if score >= 75:
            attitude = "我挺喜欢这孩子，看他说什么都顺眼"
        elif score >= 55:
            attitude = "这人还不错，聊得来"
        elif score >= 45:
            attitude = "平常，没什么特别感觉"
        elif score >= 30:
            attitude = "有点烦，话不投机"
        else:
            attitude = "我很不待见这人，不想搭理"
        block = [f"{tag} 好感度 {score:.2f}/100（{affinity_label(score)}）；我当前的情绪：{mood_label}（{pad_label}）"]
        if notes:
            ranked_notes = sorted(
                enumerate(notes),
                key=lambda x: (float((note_scores.get(x[1]) or {}).get("weight", 0)), x[0]),
                reverse=True,
            )[:5]
            def _fmt_note(n):
                meta = note_scores.get(n) or {}
                e = meta.get("emotion")
                return (f"[{emotion_cn(e)}] " if e else "") + str(n)[:60]
            block.append(f"  印象（受好感度和情绪强度加权，{attitude}）：" +
                         "；".join(_fmt_note(n) for _, n in ranked_notes))
        else:
            block.append(f"  印象：{attitude}（还没具体印象）")

        now = int(time.time())
        scored_events = []
        for ev in g.get("events", []):
            age = max(0, now - int(ev.get("ts", now)))
            recency = 0.5 ** (age / 3600.0 / 24.0)
            p = event_pad(ev)
            # 情绪显著性：权重 × 新鲜度 × (1 + 0.5·|效价|·唤醒)
            salience = float(ev.get("weight", 0)) * (0.5 + 0.5 * recency) * (1.0 + 0.5 * abs(p[0]) * abs(p[1]))
            scored_events.append((salience, ev))
        evs = [ev for _, ev in sorted(scored_events, key=lambda x: -x[0])[:6]]
        evs.sort(key=lambda e: int(e.get("ts", 0)))
        if evs:
            mem_lines = []
            for ev in evs:
                age = max(0, now - int(ev.get("ts", now)))
                when = f"{age // 60}分钟前" if age < 3600 else f"{age // 3600}小时前"
                label = emotion_cn(event_emotion(ev))
                mem_lines.append(f"[{label}·{when}] {str(ev.get('summary', ''))[:60]}")
            block.append("  长期记忆：" + "；".join(mem_lines))
        topics = g.get("topics") or {}
        interested = [k for k, v in topics.items() if isinstance(v, dict) and v.get("interested")]
        if interested:
            interested.sort(key=lambda k: -float((topics.get(k) or {}).get("interest", 0)))
            block.append("  我感兴趣的话题：" + "、".join(interested[:6]))
        # 这个人在本群的主要情绪分布
        emo_counts = (ent or {}).get("emotions") or {}
        if emo_counts:
            top_emo = sorted(emo_counts.items(), key=lambda x: -int(x[1]))[:3]
            block.append("  我对这个人的主要情绪：" +
                         "、".join(f"{emotion_cn(k)}×{v}" for k, v in top_emo))
        sections.append("\n".join(block))
    # 人物级整体印象（跨群，可突破群隔离）
    scores = []
    for _gid, _g in _all_group_data():
        _ent = (_g.get("affinity") or {}).get(str(uid))
        if isinstance(_ent, dict):
            scores.append(float(_ent.get("score", AFFINITY_BASE)))
    overall = ""
    if scores:
        avg = sum(scores) / len(scores)
        lo, hi = min(scores), max(scores)
        overall = (f"【整体印象 · 跨群】我在各群对这个人平均好感 {avg:.2f}/100（{affinity_label(avg)}），"
                   f"最低 {lo:.2f}、最高 {hi:.2f}。这是跨群的总体态度，可以突破群隔离——"
                   f"如果单群印象和整体差太多，以整体为主。")
    if not sections and not overall:
        return ""
    head = "【群友档案 · 按群隔离】以下是我对这个人每个群分别累积的好感度、印象和本群长期记忆："
    help_rule = ("别人提请求时，按「好感度 + 当前心情」决定帮不帮："
                 "好感度高且心情好——乐意帮、甚至主动；"
                 "好感度高但心情不好——嘴上嫌弃、最后还是帮；"
                 "好感度低但心情好——看心情，勉强帮；"
                 "好感度低且心情不好——直接拒绝或敷衍过去，别勉强。")
    if current_gid:
        tail = ("按好感度区别对待每个人（0~100）："
                "75 以上「亲近」——软一点、黏一点、主动关心甚至撒娇，语气宠着；"
                "55~75「友好」——愿意搭话配合，带点笑，偶尔嘴硬；"
                "45~55「平常」——正常傲娇，不冷不热；"
                "30~45「冷淡」——话短、敷衍、爱答不理；"
                "30 以下「反感」——毒舌、呛人、拒绝配合，语气硬。"
                + help_rule +
                "回复时优先用「整体印象」定基本态度，单群那一段作参考；判断不出群号时用整体。"
                "聊天时可以自然联系「长期记忆」里的事（上次的约定、聊过的话题），但别硬背流水账，也别暴露你在读记忆。")
    else:
        tail = ("按好感度区别对待每个人（0~100）："
                "75 以上「亲近」——软一点、黏一点、主动关心甚至撒娇，语气宠着；"
                "55~75「友好」——愿意搭话配合，带点笑，偶尔嘴硬；"
                "45~55「平常」——正常傲娇，不冷不热；"
                "30~45「冷淡」——话短、敷衍、爱答不理；"
                "30 以下「反感」——毒舌、呛人、拒绝配合，语气硬。"
                + help_rule +
                "回复时优先用「整体印象」，单群段作参考，别把别的群的好感度硬套过来。"
                "聊天时可以自然联系「长期记忆」里的事，但别硬背流水账，也别暴露你在读记忆。")
    body = "\n".join(([overall] if overall else []) + sections)
    return head + "\n" + body + "\n" + tail


# ==================== 记忆网络 ====================
# 三类节点：person（群友）/ event（事件）/ note（群友印象）
# 每次收到消息，从说话人 + 消息文本激活，沿边扩散两跳，取最相关的上下文。

_PUNCT_RE = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】~～…]+")


def _bigrams(text):
    text = _PUNCT_RE.sub("", str(text or ""))
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _text_sim(a, b):
    """字符二元组重合度，中文短句够用，不调用模型"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, min(len(A), len(B)))


def _build_network(g):
    nodes = {}
    edges = defaultdict(dict)

    def link(a, b, w):
        if not a or not b or a == b:
            return
        edges[a][b] = max(edges[a].get(b, 0.0), w)
        edges[b][a] = max(edges[b].get(a, 0.0), w)

    aff = g.get("affinity") or {}
    prof = g.get("profiles") or {}
    name2uid = {}
    for uid, ent in aff.items():
        name = str(ent.get("name") or "")
        pid = "p:" + str(uid)
        nodes[pid] = {"type": "person", "uid": str(uid), "name": name or str(uid),
                      "score": float(ent.get("score", AFFINITY_BASE))}
        if name:
            name2uid[name] = str(uid)

    note_idx = 0
    for name, p in prof.items():
        uid = name2uid.get(name, "")
        pid = ("p:" + uid) if uid else ("pn:" + name)
        if pid not in nodes:
            nodes[pid] = {"type": "person", "uid": uid, "name": name, "score": AFFINITY_BASE}
        note_scores = p.get("note_scores") or {}
        for note in (p.get("notes") or [])[-6:]:
            nid = f"n:{note_idx}"
            note_idx += 1
            meta = note_scores.get(note) or {}
            nodes[nid] = {"type": "note", "text": str(note), "name": name, "uid": uid,
                          "emotion": meta.get("emotion") or "neutral",
                          "weight": float(meta.get("weight", 0) or 0)}
            link(pid, nid, round(0.5 + 0.5 * min(1.0, float(meta.get("weight", 0) or 0) / 1.5), 3))

    topics = g.get("topics") or {}
    for ev in (g.get("events") or [])[-200:]:
        eid = "e:" + str(ev.get("id"))
        nodes[eid] = {"type": "event", "ev": ev}
        linked = False
        ev_weight = min(1.0, float(ev.get("weight", 0) or 0))
        for part in ev.get("participants") or []:
            pid = "p:" + str(part.get("uid") or "")
            if pid in nodes:
                link(eid, pid, round(0.55 + 0.45 * ev_weight, 3))
                linked = True
        if not linked:
            # 老事件没存参与者：从摘要里认名字，连到对应群友
            summary = str(ev.get("summary") or "")
            for name, uid in name2uid.items():
                if name and name in summary and ("p:" + uid) in nodes:
                    link(eid, "p:" + uid, round(0.45 + 0.4 * ev_weight, 3))
        topic = str(ev.get("topic") or "").strip()
        if topic:
            tid = "t:" + topic
            tinfo = topics.get(topic) if isinstance(topics.get(topic), dict) else {}
            if tid not in nodes:
                nodes[tid] = {"type": "topic", "name": topic,
                              "interest": float((tinfo or {}).get("interest", 0) or 0),
                              "emotion": (tinfo or {}).get("emotion") or event_emotion(ev)}
            link(eid, tid, round(0.55 + 0.25 * min(1.0, float((tinfo or {}).get("interest", 0) or 0) / 10.0), 3))

    # 情绪相近的事件互相连边：PAD 向量余弦相似度，越像连得越紧
    ev_nodes = [(nid, node) for nid, node in nodes.items() if node["type"] == "event"][-60:]
    for i in range(len(ev_nodes)):
        ei, ni = ev_nodes[i]
        pi = event_pad(ni["ev"])
        ni_norm = math.sqrt(sum(x * x for x in pi))
        if ni_norm < 0.1:
            continue
        for j in range(i + 1, len(ev_nodes)):
            ej, nj = ev_nodes[j]
            pj = event_pad(nj["ev"])
            nj_norm = math.sqrt(sum(x * x for x in pj))
            if nj_norm < 0.1:
                continue
            cos = sum(pi[k] * pj[k] for k in range(3)) / (ni_norm * nj_norm)
            if cos >= 0.86:
                link(ei, ej, round(0.35 * cos, 3))
    return nodes, edges


def network_recall(gid, uid, text):
    """让一条消息穿过记忆网络，返回（注入文本，激活强度）。"""
    g = load_group(gid)
    nodes, edges = _build_network(g)
    if not nodes:
        return "", 0.0

    act = defaultdict(float)
    sender = "p:" + str(uid)
    if sender in nodes:
        act[sender] += 1.0

    for nid, node in nodes.items():
        if node["type"] == "event":
            ev = node["ev"]
            sim = _text_sim(text, ev.get("summary", ""))
            if sim > 0:
                act[nid] += sim * 1.6 * max(0.2, float(ev.get("weight", 0.6)))
        elif node["type"] == "note":
            sim = _text_sim(text, node.get("text", ""))
            if sim > 0:
                act[nid] += sim * 1.2
        elif node["type"] == "person":
            sim = _text_sim(text, node.get("name", ""))
            if sim > 0.5:
                act[nid] += sim * 1.0

    # 两跳扩散：人 → 事件/印象，事件 → 其他参与者
    for _ in range(2):
        nxt = defaultdict(float, act)
        for nid, s in list(act.items()):
            for nbr, w in edges.get(nid, {}).items():
                nxt[nbr] += s * w * 0.55
        act = nxt

    def top(kind, limit):
        items = [(nid, s) for nid, s in act.items()
                 if nodes[nid]["type"] == kind and s >= NETWORK_MIN_SCORE]
        items.sort(key=lambda x: -x[1])
        return items[:limit]

    now = int(time.time())
    lines = []

    ev_items = []
    for nid, s in top("event", NETWORK_TOP_EVENTS):
        ev = nodes[nid]["ev"]
        ts = int(ev.get("ts", now))
        age = max(0, now - ts)
        when = f"{age // 60}分钟前" if age < 3600 else f"{age // 3600}小时前"
        label = emotion_cn(event_emotion(ev))
        recency = 0.5 ** (age / 3600.0 / 48.0)
        score = s * (0.6 + 0.4 * recency)
        ev_items.append((score, f"[{label}·{when}] {str(ev.get('summary', ''))[:50]}"))
    ev_items.sort(key=lambda x: -x[0])
    if ev_items:
        lines.append("相关的事：" + "；".join(x[1] for x in ev_items))

    note_items = []
    for nid, s in top("note", NETWORK_TOP_NOTES):
        node = nodes[nid]
        note_items.append((s, f"[{emotion_cn(node.get('emotion'))}] {node['name']}：{node['text'][:40]}"))
    if note_items:
        lines.append("相关印象：" + "；".join(x[1] for x in note_items))

    topic_items = []
    for nid, s in top("topic", NETWORK_TOP_TOPICS):
        node = nodes[nid]
        topic_items.append((s, f"{node['name']}（兴趣 {float(node.get('interest', 0)):.1f}）"))
    if topic_items:
        lines.append("相关话题：" + "；".join(x[1] for x in topic_items))

    people = []
    for nid, s in top("person", NETWORK_TOP_PEOPLE):
        node = nodes[nid]
        if node["uid"] == str(uid):
            continue
        people.append((s, f"{node['name']}（好感 {node['score']:.0f}）"))
    if people:
        lines.append("相关的人：" + "；".join(x[1] for x in people))

    if not lines:
        return "", 0.0
    strength = max(act.values()) if act else 0.0
    body = ("【记忆网络·本条消息激活】\n" + "\n".join(lines) +
            "\n（这是内部参考，回话时自然带出，别逐条复述，也别暴露来源）")
    return body, strength


_net_push_lock = threading.Lock()
_net_push_at = {}


def push_network_context(gid, uid, name, text):
    """每条群消息都过一遍网络；把激活结果并进说话人的画像（有节流）。"""
    key = (str(gid), str(uid))
    now = time.time()
    with _net_push_lock:
        if now - _net_push_at.get(key, 0.0) < NETWORK_PUSH_INTERVAL_SEC:
            return
        _net_push_at[key] = now
    try:
        recall, strength = network_recall(gid, uid, text)
        if not recall or strength < 0.2:
            return
        base = build_profile_text(uid, current_gid=gid, current_name=name)
        payload = (base + "\n" + recall)[:1800]
        _mb_post("/api/webui/memory/profiles/override",
                 {"person_id": "qq:" + str(uid), "override_text": payload, "source": "memwatch-network"})
        print(f"[网络] {gid} {name}({uid}) 激活={strength:.2f} 已注入画像", flush=True)
    except Exception as e:
        print(f"[网络] {gid} {name} 注入失败: {e}", flush=True)


def sync_members(gid, msgs, members):
    """把群友印象 + 好感度同步进麦麦的人物画像（每群分段，按群隔离）"""
    name2uid = {}
    for m in msgs:
        if m.get("uid"):
            name2uid[m["name"]] = str(m["uid"])
    em = compute_group_emotion(gid)
    mood_label = em["label"]
    for mm in members:
        name = str(mm.get("name", "")).strip()
        uid = _resolve_uid(name, name2uid)
        if not uid:
            continue
        text = build_profile_text(uid, current_gid=gid, current_name=name)
        if not text:
            continue
        try:
            _mb_post("/api/webui/memory/profiles/override",
                     {"person_id": "qq:" + uid, "override_text": text[:1800], "source": "memwatch"})
            with lock:
                g = load_group(gid)
                score = ((g.get("affinity") or {}).get(uid) or {}).get("score", AFFINITY_BASE)
            print(f"[桥接] {name}({uid}) 画像已同步（群={gid} 好感度={float(score):.0f} 心情={mood_label}）",
                  flush=True)
        except Exception as e:
            print(f"[桥接] {name} 同步失败: {e}", flush=True)


def resync_profiles():
    """定时把所有群的好感度/印象重新刷进画像（防止画像过期，也让分数保持最新）"""
    count = 0
    for gid, g in _all_group_data():
        for uid, ent in (g.get("affinity") or {}).items():
            text = build_profile_text(uid, current_gid=None, current_name=str(ent.get("name") or ""))
            if not text:
                continue
            try:
                _mb_post("/api/webui/memory/profiles/override",
                         {"person_id": "qq:" + str(uid), "override_text": text[:1800], "source": "memwatch"})
                count += 1
            except Exception as e:
                print(f"[桥接] 定时刷新失败 {uid}: {e}", flush=True)
    print(f"[桥接] 定时刷新完成，共 {count} 份画像", flush=True)


def form_events(idle_only=False):
    now = time.time()
    with lock:
        due = []
        for gid, buf in buffers.items():
            if not buf:
                continue
            if len(buf) >= EVENT_MIN_MSGS or (now - last_act.get(gid, 0) >= EVENT_IDLE_SEC):
                due.append((gid, list(buf)))
        for gid, _ in due:
            buffers[gid] = []
            last_act[gid] = now
    for gid, msgs in due:
        threading.Thread(target=make_event, args=(gid, msgs), daemon=True).start()


# ==================== 记忆网络的遗忘模型 ====================
# 三个公开模型叠一起用，都只取主干，不搞玄学：
#   1) 艾宾浩斯遗忘曲线 R = 0.5 ** (Δt / S)：S（stability）由「间隔重复」抬高，
#      同一个话题反复出现会 +1.5x，所以老得慢。
#   2) ACT-R 的 base-level learning：B_i = ln(Σ t_j^-d)，练习累积越多、越近越好想。
#      这里用 weight（已经含重复强化的 +0.2/次）当练习累积的代理。
#   3) Generative Agents 的检索分数：recency × importance × relevance 加权检索。
#      relevance 用事件在记忆网络里的连边数近似——被越多人和话题连着越容易被想起。
# 参考：arxiv.org/abs/2304.03442（Generative Agents）、ACT-R base-level 方程。
FORGET_ACT = float(os.environ.get("FORGET_ACT", "0.06"))   # 低于它：想不起来了
FADE_ACT = float(os.environ.get("FADE_ACT", "0.22"))       # 低于它：开始变淡
# 失忆权重：>1 让艾宾浩斯曲线走得更快（1.5 ≈ 半衰期缩短三分之一），
# 只作用于事件/印象的衰减，不动好感度和人物在场感。
FORGET_WEIGHT = max(0.1, float(os.environ.get("FORGET_WEIGHT", "1.0")))
# 真删模式：遗忘时不再写遗忘区，直接丢掉（默认关闭，compose 里开）
FORGET_PURGE = os.environ.get("FORGET_PURGE", "0").strip().lower() in ("1", "true", "yes", "on")
ARCHIVE_MAX = int(os.environ.get("ARCHIVE_MAX", "200"))    # 遗忘区最多留多少条


def event_activation(ev, now=None):
    """事件此刻的激活度（0~1）——还能不能被「想起来」。"""
    now = float(now or time.time())
    weight = float(ev.get("weight") or 0.0)
    importance = min(1.0, weight / 1.5)
    stab = max(1.0, float(ev.get("stability") or HALF_LIFE_HOURS))
    age_h = max(0.0, (now - float(ev.get("ts") or now)) / 3600.0)
    recency = 0.5 ** (age_h / stab * FORGET_WEIGHT)
    n = len(ev.get("participants") or [])
    fan = 1.0 + 0.12 * math.log1p(n)
    return round(min(1.0, importance * recency * fan), 4)


def note_activation(meta, now=None):
    """印象的激活度：比事件衰减更慢（印象比事件耐放）。"""
    now = float(now or time.time())
    weight = float((meta or {}).get("weight") or 0.0)
    importance = min(1.0, weight / 2.0)
    age_h = max(0.0, (now - float((meta or {}).get("ts") or now)) / 3600.0)
    return round(min(1.0, importance * (0.5 ** (age_h / (HALF_LIFE_HOURS * 2) * FORGET_WEIGHT))), 4)


def person_activation(ent, now=None):
    """人对「当前」的在场感：好感基准 × 最近还联系吗。"""
    now = float(now or time.time())
    score = float((ent or {}).get("score", AFFINITY_BASE))
    age_h = max(0.0, (now - float((ent or {}).get("updated") or now)) / 3600.0)
    recency = 0.5 ** (age_h / (24.0 * 7))
    base = 0.5 + (score - AFFINITY_BASE) / 100.0
    return round(min(1.0, max(0.0, base * recency + 0.15)), 4)


def topic_activation(t, now=None):
    """话题激活度：兴趣绝对值相对「感兴趣」阈值的比例，0~1。"""
    interest = abs(float((t or {}).get("interest") or 0.0))
    return round(min(1.0, interest / max(0.1, INTEREST_THRESHOLD)), 4)


def refresh_activation(g, now=None):
    """激活度算好写进数据：面板、检索读同一份，别两端各算一套公式。"""
    now = float(now or time.time())
    for ev in g.get("events") or []:
        ev["act"] = event_activation(ev, now)
    for _name, p in (g.get("profiles") or {}).items():
        if not isinstance(p, dict):
            continue
        p["note_act"] = {note: note_activation(meta, now)
                         for note, meta in (p.get("note_scores") or {}).items()}
    for _uid, ent in (g.get("affinity") or {}).items():
        if isinstance(ent, dict):
            ent["act"] = person_activation(ent, now)
    for _tn, t in (g.get("topics") or {}).items():
        if isinstance(t, dict):
            t["act"] = topic_activation(t, now)
    return g


def decay_all():
    now = time.time()
    try:
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        gid = name[:-5]
        with lock:
            g = load_group(gid)
            changed = False
            kept, archived = [], []
            for ev in g.get("events", []):
                # 艾宾浩斯增量衰减：只按「上次衰减到现在」的时间走，稳定性越高衰减越慢
                last = float(ev.get("decayed") or ev.get("ts") or now)
                dt_h = max(0.0, (now - last) / 3600.0)
                stab = max(1.0, float(ev.get("stability") or HALF_LIFE_HOURS))
                w = float(ev.get("weight", 0)) * math.exp(-math.log(2) * dt_h / stab * FORGET_WEIGHT)
                ev["weight"] = round(w, 4)
                ev["decayed"] = int(now)
                ev["act"] = event_activation(ev, now)
                # 忘掉 = 权重和激活度都掉到底：默认留档，FORGET_PURGE=1 时真删
                if w >= PRUNE_THRESHOLD and ev["act"] >= FORGET_ACT:
                    kept.append(ev)
                elif not FORGET_PURGE:
                    archived.append(ev)
                    changed = True
                else:
                    changed = True
            g["events"] = kept
            if FORGET_PURGE:
                g.pop("forgotten", None)
            elif archived:
                g["forgotten"] = (list(g.get("forgotten") or []) + archived)[-ARCHIVE_MAX:]
            # 兴趣值按艾宾浩斯曲线衰减：I(t) = I0 * e^(-λt)
            topics = g.get("topics") or {}
            if topics:
                lam = math.log(2) / max(1.0, TOPIC_HALF_LIFE_HOURS)
                kept, archived_topics = {}, []
                for _tn, _t in topics.items():
                    if not isinstance(_t, dict):
                        continue
                    _last = float(_t.get("last", int(now)))
                    _seen = float(_t.get("updated") or _last)
                    _dt_h = max(0.0, (now - _last) / 3600.0)
                    _t["interest"] = round(float(_t.get("interest", 0)) * math.exp(-lam * _dt_h), 2)
                    _t["interested"] = bool(float(_t["interest"]) >= INTEREST_THRESHOLD)
                    _t["act"] = topic_activation(_t)
                    _t["last"] = int(now)
                    # 参考事件：激活度到底就挪进遗忘区；但刚出现的话题先留 24h
                    if (_t["act"] < TOPIC_FORGET_ACT
                            and (now - _seen) / 3600.0 >= TOPIC_FORGET_MIN_AGE_H):
                        if not FORGET_PURGE:
                            archived_topics.append({
                                "name": _tn,
                                "interest": _t["interest"],
                                "act": _t["act"],
                                "last": int(_last),
                                "forgotten_at": int(now),
                                "count": int(_t.get("count") or 0),
                                "emotion": _t.get("emotion") or "",
                                "skill": _t.get("skill") or "",
                            })
                    else:
                        kept[_tn] = _t
                g["topics"] = kept
                if FORGET_PURGE:
                    g.pop("forgotten_topics", None)
                elif archived_topics:
                    g["forgotten_topics"] = (list(g.get("forgotten_topics") or []) + archived_topics)[-TOPIC_ARCHIVE_MAX:]
            # 好感度缓慢回归 50：久不互动的关系会变淡
            aff = g.get("affinity") or {}
            for uid, ent in aff.items():
                last = float(ent.get("decayed") or ent.get("updated") or now)
                days = max(0.0, (now - last) / 86400.0)
                score = float(ent.get("score", AFFINITY_BASE))
                if days > 0:
                    score = AFFINITY_BASE + (score - AFFINITY_BASE) * (
                        0.5 ** (days / max(0.1, AFFINITY_HALF_LIFE_DAYS)))
                    if abs(score - AFFINITY_BASE) < 0.5:
                        score = AFFINITY_BASE
                    ent["score"] = clamp_affinity(score)
                    ent["decayed"] = now
                    aff[uid] = ent
            # 旧群没有持久化心情：用近期事件重心初始化一次，之后所有事件共用
            if not isinstance(g.get("mood"), dict):
                _mood = _aggregate_group_mood(g)
                _mood["updated"] = int(now)
                g["mood"] = _mood
            # 印象/人物/话题的激活度一并刷新，面板直接读，不在前端重复算公式
            refresh_activation(g, now)
            g["updated"] = int(now)
            save_group(gid, g)
    print("[衰减] 完成一轮", flush=True)


def conv_key(ev):
    """会话 key：群用群号，私聊用 p+QQ 号。两者在数据层完全对等。"""
    mt = ev.get("message_type")
    if mt == "group" and ev.get("group_id"):
        return str(ev.get("group_id")), "group"
    if mt == "private":
        uid = ev.get("user_id") or ev.get("target_id")
        if uid:
            return "p" + str(uid), "private"
    return None, None


def is_private(key):
    return str(key).startswith("p")


def on_message(ws, raw):
    global _ws
    _ws = ws
    try:
        ev = json.loads(raw)
    except Exception:
        return
    # 禁言 / 解禁通知：记进该会话，面板据此提示「发不出去」
    if ev.get("post_type") == "notice" and ev.get("notice_type") == "group_ban":
        gid = str(ev.get("group_id") or "")
        target = str(ev.get("user_id") or "")
        if gid and target == str(BOT_ID):
            sub = str(ev.get("sub_type") or "ban")
            now = int(time.time())
            with lock:
                g = load_group(gid)
                if sub == "lift_ban":
                    g.pop("muted", None)
                    print(f"[禁言] 群 {gid} 已解禁", flush=True)
                else:
                    dur = int(ev.get("duration") or 0)
                    g["muted"] = {"until": (now + dur) if dur > 0 else 0,
                                  "at": now, "duration": dur,
                                  "operator": str(ev.get("operator_id") or "")}
                    print(f"[禁言] 群 {gid} 被禁言 {dur} 秒", flush=True)
                g["updated"] = now
                save_group(gid, g)
        return
    if ev.get("post_type") != "message" or ev.get("message_type") not in ("group", "private"):
        return
    uid = ev.get("user_id")
    if str(uid) == str(ev.get("self_id")):
        return
    seg = ev.get("message") or []
    text = "".join(s.get("data", {}).get("text", "") for s in seg if s.get("type") == "text").strip()
    if not text:
        return
    sender = ev.get("sender") or {}
    name = sender.get("card") or sender.get("nickname") or str(uid)
    gid, _kind = conv_key(ev)
    if not gid:
        return
    with lock:
        buffers[gid].append({"name": name, "uid": str(uid), "text": text[:200], "ts": int(time.time())})
        last_act[gid] = time.time()
    # 每条消息都过一遍记忆网络，把相关的人/事/印象并进说话人的画像
    threading.Thread(target=push_network_context, args=(gid, uid, name, text), daemon=True).start()


def tick_loop():
    while True:
        time.sleep(30)
        try:
            form_events()
        except Exception as e:
            print("[tick]", e, flush=True)


def decay_loop():
    while True:
        time.sleep(DECAY_INTERVAL_SEC)
        try:
            decay_all()
        except Exception as e:
            print("[decay]", e, flush=True)
        # 每小时兜底对齐一次：新进群的人、手动改过的数据、历史遗留的偏差都靠这轮收敛
        try:
            sync_affinity()
        except Exception as e:
            print("[好感] 定时统一失败:", e, flush=True)
        # 每小时兜底分类一次：新攒出来的经验全走一遍（先用规则，剩下的再问一次模型）
        try:
            classify_experiences()
        except Exception as e:
            print("[经验] 定时分类失败:", e, flush=True)


def _aggregate_interests(limit=6):
    agg = {}
    for gid, g in _all_group_data():
        for name, t in (g.get("topics") or {}).items():
            if isinstance(t, dict) and t.get("interested"):
                agg[name] = max(agg.get(name, 0.0), float(t.get("interest", 0)))
    return [k for k, _ in sorted(agg.items(), key=lambda kv: -kv[1])[:limit]]


def sync_persona_interests(force=False):
    """把感兴趣的话题写进麦麦自己的 reply_style（只在话题变化时才改，避免频繁重载）"""
    top = _aggregate_interests()
    try:
        with open(SELF_INTERESTS_FILE, "r", encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = []
    if not force and top == last:
        return
    try:
        sess = _mb_ensure()
        r = sess.get(MAIBOT_API + "/api/webui/config/bot/raw", timeout=15)
        content = r.json().get("content", "")
        m = re.search(r'^reply_style = "(.*)"[^\n]*$', content, re.M)
        if not m:
            print("[画像] 没找到 reply_style 行", flush=True)
            return
        base = re.sub(r"（最近感兴趣：[^）]*）", "", m.group(1)).strip()
        new_style = base + ("（最近感兴趣：" + "、".join(top) + "）" if top else "")
        content2 = content[:m.start()] + 'reply_style = "' + new_style + '"' + content[m.end():]
        sess.post(MAIBOT_API + "/api/webui/config/bot/raw",
                  json={"raw_content": content2}, timeout=20)
        with open(SELF_INTERESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(top, f, ensure_ascii=False)
        print("[画像] 自身兴趣已更新：" + ("、".join(top) or "（无）"), flush=True)
    except Exception as e:
        print("[画像] 写入自身画像失败:", e, flush=True)


def sync_group_interests(force=False):
    """写会话提示前先抢文件锁：两个进程同时删+写会互相插队，攒出重复条目。"""
    try:
        import fcntl
    except ImportError:
        return _sync_group_interests_impl(force)
    lock_path = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "sync_prompts.lock")
    try:
        fh = open(lock_path, "w")
    except Exception:
        return _sync_group_interests_impl(force)
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # 另一个进程正在同步，这轮跳过
        return _sync_group_interests_impl(force)
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass


def _sync_group_interests_impl(force=False):
    """按群把「感兴趣话题」「没弄懂的疑问」「精力档位」写成会话专属提示（先删旧再写新）。"""
    gmap, pmap, emap = {}, {}, {}
    for gid, g in _all_group_data():
        tops = sorted(k for k, t in (g.get("topics") or {}).items()
                      if isinstance(t, dict) and t.get("interested"))
        if tops:
            gmap[str(gid)] = tops[:6]
        # 悬空的疑问（困惑事件留下的 pending）：让它在群里换个说法再问
        pend = []
        for _tk, t in (g.get("topics") or {}).items():
            if isinstance(t, dict) and t.get("pending"):
                pend.append({"topic": _tk, "q": str(t.get("pending")),
                             "search": t.get("pending_search") or {}})
        if pend:
            pmap[str(gid)] = pend[:2]
        # 精力档位：按 5 点取整，避免每分钟都改提示词
        en = g.get("energy") or {}
        if en.get("value") is not None:
            emap[str(gid)] = int(round(float(en["value"]) / 5.0) * 5)
    exp_text = experience_context(limit=3)
    snap = {"topics": gmap, "pending": pmap, "energy": emap, "experience": exp_text}
    try:
        with open(GROUP_INTERESTS_FILE, "r", encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = {}
    if isinstance(last, dict) and "topics" in last and "pending" in last:
        prev = {"topics": last.get("topics") or {}, "pending": last.get("pending") or {},
                "energy": last.get("energy") or {}, "experience": last.get("experience") or ""}
    else:  # 旧格式（只有 {gid: [话题]}）
        prev = {"topics": last if isinstance(last, dict) else {}, "pending": {}, "energy": {},
                "experience": ""}
    if not force and snap == prev:
        return
    try:
        sess = _mb_ensure()
        r = sess.get(MAIBOT_API + "/api/chat/sessions", timeout=15)
        sessions = (r.json() or {}).get("sessions") or []
        gid2sid = {}
        for s in sessions:
            g = str(s.get("group_id") or "")
            if g:
                gid2sid[g] = s.get("session_id")
            elif s.get("user_id"):
                gid2sid["p" + str(s["user_id"])] = s.get("session_id")
        MARKERS = ("【我的兴趣】", "【没弄懂的事】", "【精力】", "【经验】")
        for gid in sorted(set(gmap) | set(pmap) | set(emap)):
            sid = gid2sid.get(gid)
            if not sid:
                continue
            det = (sess.get(MAIBOT_API + f"/api/chat/sessions/{sid}", timeout=15).json() or {}).get("detail") or {}
            cps = (det.get("prompts") or {}).get("chat_prompts") or []
            existing = [str(cp.get("prompt") or "") for cp in cps
                        if any(mk in str(cp.get("prompt") or "") for mk in MARKERS)]
            # 先算好这轮该有哪些提示
            want = []
            if gmap.get(gid):
                want.append("【我的兴趣】我最近对「" + "、".join(gmap[gid]) +
                            "」很感兴趣。群里聊到这些话题时，可以主动插一句，别太刻意。")
            if pmap.get(gid):
                for item in pmap[gid]:
                    q = str(item.get("q") or "").rstrip("？?。.")
                    srch = item.get("search") or {}
                    hits = (srch.get("results") or [])[:3]
                    page = srch.get("page") or {}
                    if page.get("text"):
                        source_name = str(page.get("title") or "网页")[:40]
                        body = str(page.get("text") or "")[:500]
                        want.append("【没弄懂的事】我一直没弄明白：" + q +
                                    "。我刚联网抓了「" + source_name + "」：" + body +
                                    "。按这个理解就行，别再瞎猜，也别为这个专门去问人；万一对不上再顺口问一句。")
                    elif hits:
                        refs = "；".join("%s——%s" % (h.get("title") or "", h.get("snippet") or "")
                                        for h in hits)
                        want.append("【没弄懂的事】我一直没弄明白：" + q +
                                    "。默认先去网上查了，结果是：" + refs +
                                    "。按这个理解就行，别再瞎猜，也别为这个专门去问人；万一对不上再顺口问一句。")
                    elif srch.get("checked"):
                        want.append("【没弄懂的事】我一直没弄明白：" + q +
                                    "。我已经上网查过一遍，没查到靠谱的说法。别硬编；群里再聊到相关的，"
                                    "可以顺口问一句让人解释，语气自然点。")
                    else:
                        want.append("【没弄懂的事】有件事我一直没弄明白：" + q +
                                    "。先记着，回头自己查一下；群里又聊到的话，也可以顺势问一句。")
            if gid in emap:
                want.append(energy_prompt(emap[gid]))
            if exp_text:
                want.append("【经验】我以前不懂、后来搞懂的事（全局共享）：" + chr(10) + exp_text)
            # 内容一模一样就别动：每次 PUT/DELETE 都会改写 bot_config.toml，
            # 触发一轮配置热重载，白折腾还清空模型客户端缓存
            if sorted(existing) == sorted(want):
                continue
            # 收集所有命中标记的下标，从大到小删——边遍历边删会让后面的删错位
            drop = [int(cp.get("index")) for cp in cps
                    if cp.get("index") is not None
                    and any(mk in str(cp.get("prompt") or "") for mk in MARKERS)]
            for idx in sorted(drop, reverse=True):
                try:
                    sess.delete(MAIBOT_API + f"/api/chat/sessions/{sid}/prompts/{idx}", timeout=15)
                except Exception:
                    pass
            for prompt in want:
                sess.put(MAIBOT_API + f"/api/chat/sessions/{sid}/prompts",
                         json={"prompt": prompt}, timeout=15)
            tags = []
            if gmap.get(gid):
                tags.append("兴趣")
            if pmap.get(gid):
                tags.append("疑问")
            if gid in emap:
                tags.append("精力")
            if exp_text:
                tags.append("经验")
            print(f"[提示] 群 {gid} 已更新：{'、'.join(tags)}", flush=True)
        # 话题/疑问/精力都清空的群：清掉旧提示
        for s in sessions:
            g = str(s.get("group_id") or "")
            if not g and s.get("user_id"):
                g = "p" + str(s["user_id"])
            if g and (g in gmap or g in pmap or g in emap):
                continue
            sid2 = s.get("session_id")
            if not sid2:
                continue
            try:
                det = (sess.get(MAIBOT_API + f"/api/chat/sessions/{sid2}", timeout=15).json() or {}).get("detail") or {}
                drop = [int(cp.get("index")) for cp in (det.get("prompts") or {}).get("chat_prompts") or []
                        if cp.get("index") is not None
                        and any(mk in str(cp.get("prompt") or "") for mk in MARKERS)]
                for idx in sorted(drop, reverse=True):
                    sess.delete(MAIBOT_API + f"/api/chat/sessions/{sid2}/prompts/{idx}", timeout=15)
                if drop:
                    print(f"[插话] 群 {g} 旧提示已清理（{len(drop)} 条）", flush=True)
            except Exception:
                pass
        with open(GROUP_INTERESTS_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception as e:
        print("[插话] 同步失败:", e, flush=True)


def energy_talk_value(v):
    """精力 → 回复概率：越累触发阈值越高（0/0.2/0.4/0.6/1.0）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.5
    if v < 6:
        return 0.0
    if v < 18:
        return 0.2
    if v < 40:
        return 0.4
    if v < 70:
        return 0.6
    return 0.8


def sync_talk_frequency(force=False):
    """按每个会话的精力，把回复概率写进麦麦的 talk_value_rules。

    只写变化过的会话；底层走 MaiBot 的 /talk-frequency 接口，它会自动
    打开 enable_talk_value_rules 并热重载。精力档位变化很慢，不会每分钟刷。
    """
    desired = {}
    for gid, g in _all_group_data():
        en = g.get("energy") or {}
        if en.get("value") is None:
            continue
        desired[str(gid)] = energy_talk_value(en["value"])
    if not desired:
        return
    try:
        with open(TALK_FREQ_FILE, "r", encoding="utf-8") as f:
            last = json.load(f) or {}
    except Exception:
        last = {}
    if not force and len(last) == len(desired) and all(
            abs(float(last.get(k, -1)) - v) < 1e-6 for k, v in desired.items()):
        return

    try:
        import fcntl
    except ImportError:
        fcntl = None
    fh = None
    if fcntl:
        try:
            fh = open(os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "sync_prompts.lock"), "w")
            deadline = time.time() + 30
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        fh.close()
                        return  # 提示同步一直占着锁，下轮再来
                    time.sleep(1)
        except Exception:
            if fh:
                fh.close()
            fh = None
    try:
        sess = _mb_ensure()
        r = sess.get(MAIBOT_API + "/api/chat/sessions", timeout=15)
        sessions = (r.json() or {}).get("sessions") or []
        gid2sid = {}
        for s in sessions:
            g = str(s.get("group_id") or "")
            if g:
                gid2sid[g] = s.get("session_id")
            elif s.get("user_id"):
                gid2sid["p" + str(s["user_id"])] = s.get("session_id")
        changed = []
        for gid, val in desired.items():
            if abs(float(last.get(gid, -1)) - val) < 1e-6:
                continue
            sid = gid2sid.get(gid)
            if not sid:
                continue
            try:
                resp = sess.put(
                    MAIBOT_API + f"/api/chat/sessions/{sid}/talk-frequency",
                    json={"time": "00:00-23:59", "value": val}, timeout=20)
                if resp.status_code >= 300:
                    print(f"[精力频率] {gid} 写入失败 HTTP {resp.status_code}", flush=True)
                    continue
                last[gid] = val
                changed.append((gid, val))
            except Exception as e:
                print(f"[精力频率] {gid} 写入失败: {e}", flush=True)
        with open(TALK_FREQ_FILE, "w", encoding="utf-8") as f:
            json.dump(last, f, ensure_ascii=False)
        if changed:
            print("[精力频率] " + "，".join(f"{g}={v:g}" for g, v in changed), flush=True)
    except Exception as e:
        print("[精力频率] 同步失败:", e, flush=True)
    finally:
        if fcntl and fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            except Exception:
                pass


# ==================== 精力系统（每群一份） ====================
# 模型：精力是预算，token 是主要开销，休息按一阶指数回补（类似 RC 充电）。
#   恢复/小时 = (MAX - E) × (1 - e^(-1/τ))      τ 夜里减半（睡着了恢复快）
#   消耗      = TOKEN_COST × (本群千 token) × 心情因子
#   心情因子  = 1 - 0.35×valence + 0.10×arousal（心情好省电，低落/激动更费）
# 说话频率：精力档位直接写进该群会话提示，低电量时明确「少说话／只回点名」。
ENERGY_MAX = float(os.environ.get("ENERGY_MAX", "100"))
ENERGY_TOKEN_COST = float(os.environ.get("ENERGY_TOKEN_COST", "0.03"))  # 每 1000 token 扣多少精力
ENERGY_TAU_H = float(os.environ.get("ENERGY_TAU_H", "4"))
ENERGY_NIGHT_TAU_H = float(os.environ.get("ENERGY_NIGHT_TAU_H", "2"))
ENERGY_EMPTY_LINE = float(os.environ.get("ENERGY_EMPTY_LINE", "6"))
ENERGY_EMPTY_COOLDOWN = float(os.environ.get("ENERGY_EMPTY_COOLDOWN", "21600"))
ENERGY_LOOP_SEC = int(os.environ.get("ENERGY_LOOP_SEC", "60"))
MAIBOT_DB = os.environ.get("MAIBOT_DB", "/maibot/MaiBot.db")
EMPTY_WORDS = os.environ.get(
    "ENERGY_EMPTY_WORDS",
    "唔…我快没电了，先趴一会儿，有事 @ 我。（尾巴都耷下来了）")

# 档位：(下限, key, 中文)
ENERGY_TIERS = [(70, "fresh", "精神不错"), (40, "tired", "有点累"),
                (18, "weary", "很累"), (6, "low", "快没电了"), (0, "empty", "没电了")]
_ws = None   # on_message 里记下连接，见底时用来主动发一句


def energy_tier(v):
    for th, key, cn in ENERGY_TIERS:
        if float(v) >= th:
            return key, cn
    return "empty", "没电了"


def energy_prompt(v):
    """把精力翻译成一句行为约束，塞进该群会话提示。"""
    v = float(v)
    key, cn = energy_tier(v)
    head = "【精力】%s（%d/%d）" % (cn, round(v), round(ENERGY_MAX))
    if key == "fresh":
        return head + "，可以正常接话；有把握就一句说完，别为了凑字数展开。"
    if key == "tired":
        return head + "，热闹的场子少接几句，优先回点我名的；能一句说完就别拆两段，没必要的表情包别发。"
    if key == "weary":
        return head + "，能不说就不说，只回直接问我或被 @ 的；一句说完，别展开、别发表情包、别查资料或调用工具。"
    if key == "low":
        return head + "，除非被点名否则别发言；最多一句，不发表情包、不查资料、不调用工具。"
    return head + "，基本停机：只回被 @ 的那一句，一句结束，别主动说话、别发表情包、别用工具。"


def _iso(ts):
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _energy_tokens_since(since_ts, gid):
    """从 MaiBot 用量库取这个会话在 since_ts 之后消耗的 token 数。

    群按 group_id 匹配；私聊（key = p+QQ号）按 user_id + chat_type 匹配。
    """
    if not os.path.exists(MAIBOT_DB):
        return 0
    try:
        con = sqlite3.connect("file:" + MAIBOT_DB + "?mode=ro", uri=True, timeout=5)
    except Exception:
        return 0
    try:
        if is_private(gid):
            row = con.execute(
                "SELECT COALESCE(SUM(u.total_tokens), 0) FROM llm_usage u "
                "LEFT JOIN chat_sessions cs ON u.session_id = cs.session_id "
                "WHERE cs.user_id = ? AND cs.group_id IS NULL AND u.timestamp >= ?",
                (str(gid)[1:], _iso(since_ts))).fetchone()
        else:
            row = con.execute(
                "SELECT COALESCE(SUM(u.total_tokens), 0) FROM llm_usage u "
                "LEFT JOIN chat_sessions cs ON u.session_id = cs.session_id "
                "WHERE cs.group_id = ? AND u.timestamp >= ?",
                (str(gid), _iso(since_ts))).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception as e:
        print("[精力] 读用量失败:", e, flush=True)
        return 0
    finally:
        con.close()


def energy_of(g):
    en = g.get("energy")
    if not isinstance(en, dict):
        # 首次结算：tok_ts 往前挪 2 秒。否则 dt < 1s 会一直 continue，
        # 新群/私聊永远拿不到 energy 字段，面板就停在同一档。
        now = int(time.time())
        en = {"value": ENERGY_MAX, "tok_ts": now - 2, "updated": now - 2}
    return en


def update_energy_all(now=None):
    """按真实 token 消耗扣精力、按时间回补，写回每群。"""
    now = float(now or time.time())
    hour = datetime.fromtimestamp(now).hour
    tau = ENERGY_NIGHT_TAU_H if (hour >= 22 or hour < 8) else ENERGY_TAU_H
    out = []
    try:
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        gid = name[:-5]
        with lock:
            g = load_group(gid)
            en = energy_of(g)
            last = float(en.get("tok_ts") or now)
            dt_h = max(0.0, (now - last) / 3600.0)
            if dt_h < 1.0 / 3600.0:   # 不到一秒，别动，免得时区/取整把历史全算进来
                continue
            cur = max(0.0, min(ENERGY_MAX, float(en.get("value", ENERGY_MAX))))
            recover = max(0.0, ENERGY_MAX - cur) * (1.0 - math.exp(-dt_h / max(0.1, tau)))
            try:
                emo = compute_group_emotion(gid)
                val = float(emo.get("valence") or 0.0)
                aro = float(emo.get("arousal") or 0.0)
            except Exception:
                emo, val, aro = {}, 0.0, 0.0
            # 把衰减后的心情写回，保证面板和 prompt 读到的是同一份「实时」值
            if emo.get("pad"):
                g["mood"] = {"pad": emo["pad"], "emotion": emo.get("emotion"),
                             "label": emo.get("label"), "valence": emo.get("valence"),
                             "arousal": emo.get("arousal"), "dominance": emo.get("dominance"),
                             "intensity": emo.get("intensity"), "updated": int(now)}
            mood_factor = max(0.6, min(1.6, 1.0 - 0.35 * val + 0.10 * aro))
            tokens = _energy_tokens_since(last, gid)
            rate = load_group_energy_cost(gid)
            cost = rate * (tokens / 1000.0) * mood_factor
            new = max(0.0, min(ENERGY_MAX, cur + recover - cost))
            old_key = energy_tier(cur)[0]
            new_key, new_cn = energy_tier(new)
            en.update({"value": round(new, 2), "updated": int(now), "tok_ts": int(now),
                       "max": ENERGY_MAX, "tier": new_key, "tier_cn": new_cn,
                       "last_cost": round(cost, 3), "last_recover": round(recover, 3),
                       "mood_factor": round(mood_factor, 3),
                       "cost_rate": round(rate, 4),
                       "tokens": int(en.get("tokens", 0)) + tokens})
            # 曲线：每 10 分钟留一点，最多 144 点（24 小时）
            hist = en.get("history")
            if not isinstance(hist, list):
                hist = []
            if not hist or now - float(hist[-1].get("ts") or 0) >= 600:
                hist.append({"ts": int(now), "v": round(new, 1)})
                en["history"] = hist[-144:]
            g["energy"] = en
            g["updated"] = int(now)
            save_group(gid, g)
            out.append({"gid": gid, "value": round(new, 2), "tier": new_key,
                        "tier_cn": new_cn, "tier_changed": old_key != new_key,
                        "tokens": tokens, "cost": round(cost, 2)})
    if out:
        print("[精力] " + "，".join("%s %.0f(%s)" % (o["gid"], o["value"], o["tier_cn"]) for o in out), flush=True)
    return out


def announce_empty(now=None):
    """精力见底：以本人身份在群里说一句，冷却 ENERGY_EMPTY_COOLDOWN 秒。"""
    now = float(now or time.time())
    if _ws is None:
        return
    try:
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        gid = name[:-5]
        with lock:
            g = load_group(gid)
            en = energy_of(g)
            v = float(en.get("value", ENERGY_MAX))
            if v >= ENERGY_EMPTY_LINE:
                continue
            if now - float(en.get("drained_at") or 0) < ENERGY_EMPTY_COOLDOWN:
                continue
            if is_private(gid):
                params = {"message_type": "private", "user_id": int(str(gid)[1:]),
                          "message": EMPTY_WORDS}
            else:
                params = {"message_type": "group", "group_id": int(gid),
                          "message": EMPTY_WORDS}
            try:
                _ws.send(json.dumps({"action": "send_msg", "params": params,
                                     "echo": str(time.time_ns())}, ensure_ascii=False))
            except Exception as e:
                print("[精力] 见底提示发送失败:", e, flush=True)
                continue
            en["drained_at"] = int(now)
            g["energy"] = en
            g["updated"] = int(now)
            save_group(gid, g)
        print(f"[精力] 群 {gid} 见底（{v:.0f}），已提示群友", flush=True)


def backfill_experiences_from_pending():
    """把历史里已经查到的 pending_search 一次性收进全局经验库。"""
    n = 0
    for gid, g in _all_group_data():
        for tk, t in (g.get("topics") or {}).items():
            if not isinstance(t, dict):
                continue
            q = str(t.get("pending") or "").strip()
            hits = ((t.get("pending_search") or {}).get("results") or [])
            if q and hits:
                try:
                    if record_experience(q, hits=hits, topic=tk, gid=str(gid), source="search"):
                        n += 1
                except Exception as e:
                    print(f"[经验] 历史补录失败: {e}", flush=True)
    if n:
        print(f"[经验] 历史补录 {n} 条", flush=True)
        try:
            sync_group_interests()
        except Exception as e:
            print(f"[经验] 刷新提示失败: {e}", flush=True)
    return n


def backfill_page_fetch_from_pending(limit=20):
    """启动时给历史「没弄懂」的命中的第一条 URL 排一次异步抓取。"""
    n = 0
    for gid, g in _all_group_data():
        for tk, t in (g.get("topics") or {}).items():
            if n >= limit:
                break
            if not isinstance(t, dict):
                continue
            q = str(t.get("pending") or "").strip()
            srch = t.get("pending_search") or {}
            hits = srch.get("results") or []
            if q and hits and not srch.get("page"):
                schedule_page_fetch(str(gid), tk, q, hits)
                n += 1
    if n:
        print(f"[抓取] 历史命中排入 {n} 条", flush=True)
    return n


def backfill_pending_search(limit=2):
    """给历史 pending 补联网查证：每次挑最新的几条，补完写回并刷新会话提示。"""
    done = 0
    for gid, g in _all_group_data():
        if done >= limit:
            break
        cands = [(k, t) for k, t in (g.get("topics") or {}).items()
                 if isinstance(t, dict) and t.get("pending")
                 and not ((t.get("pending_search") or {}).get("checked") or (t.get("pending_search") or {}).get("results"))]
        cands.sort(key=lambda kv: -(float(kv[1].get("updated") or kv[1].get("last") or 0)))
        for tk, t in cands:
            if done >= limit:
                break
            q = str(t.get("pending"))
            if not _search_budget_left(CONFUSION_SEARCH_RESERVE):
                print("[补搜] 额度留给实时查证，本轮先停", flush=True)
                return done
            hits = web_search(q)
            with lock:
                g2 = load_group(gid)
                t2 = (g2.get("topics") or {}).get(tk)
                if isinstance(t2, dict) and str(t2.get("pending")) == q:
                    t2["pending_search"] = {"query": q, "results": hits or [],
                                            "checked": True, "ts": int(time.time())}
                    g2["updated"] = int(time.time())
                    save_group(gid, g2)
            if hits:
                try:
                    record_experience(q, hits=hits, topic=tk, gid=str(gid), source="search")
                except Exception as e:
                    print(f"[经验] 补搜记录失败: {e}", flush=True)
            done += 1
            print(f"[补搜] {gid} {tk} -> {len(hits)} 条", flush=True)
            time.sleep(1.5)
    return done


def backfill_loop():
    time.sleep(90)
    try:
        backfill_experiences_from_pending()
    except Exception as e:
        print(f"[经验] 启动补录失败: {e}", flush=True)
    try:
        backfill_page_fetch_from_pending()
    except Exception as e:
        print(f"[抓取] 启动排入失败: {e}", flush=True)
    while True:
        try:
            n = backfill_pending_search(BACKFILL_LIMIT)
            if n:
                sync_group_interests()
                # 刚补出来的经验先按规则归一次类，面板不用等到下次重启
                try:
                    classify_experiences(use_llm=False, fallback_other=False)
                except Exception as e:
                    print(f"[经验] 规则分类失败: {e}", flush=True)
        except Exception as e:
            print("[补搜] 出错:", e, flush=True)
        time.sleep(BACKFILL_INTERVAL)


def energy_loop():
    while True:
        time.sleep(ENERGY_LOOP_SEC)
        try:
            res = update_energy_all()
            if any(r.get("tier_changed") for r in res):
                sync_group_interests()
            try:
                sync_talk_frequency()
            except Exception as e:
                print("[精力频率] 循环出错:", e, flush=True)
            announce_empty()
        except Exception as e:
            print("[精力] 循环出错:", e, flush=True)


def _load_self():
    try:
        with open(SELF_PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"self": "", "updated": 0, "plastic": 0.0, "last": 0}


def _save_self(d):
    try:
        with open(SELF_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[自我] 保存失败:", e, flush=True)


def _sanitize_toml_text(t):
    return str(t or "").replace('"', "「").replace("\n", " ").strip()


def sync_self_profile(text=None):
    """把自我认知写进麦麦自己的 personality（带标记段，重复替换）"""
    if text is None:
        text = _load_self().get("self", "")
    text = _sanitize_toml_text(text)
    try:
        sess = _mb_ensure()
        r = sess.get(MAIBOT_API + "/api/webui/config/bot/raw", timeout=15)
        content = r.json().get("content", "")
        m = re.search(r'^personality = "(.*)"[^\n]*$', content, re.M)
        if not m:
            print("[自我] 没找到 personality 行", flush=True)
            return
        base = re.sub(r"（我最近的自我认知：[^）]*）", "", m.group(1)).strip()
        new_val = base + ("（我最近的自我认知：" + text + "）" if text else "")
        content2 = content[:m.start()] + 'personality = "' + new_val + '"' + content[m.end():]
        sess.post(MAIBOT_API + "/api/webui/config/bot/raw",
                  json={"raw_content": content2}, timeout=20)
        print("[自我] 已写入 personality：" + (text[:40] or "（空）"), flush=True)
    except Exception as e:
        print("[自我] 写入 personality 失败:", e, flush=True)


def maybe_rewrite_self(gid, summary, topic, mood, ev_weight):
    """LIF 阈值模型：事件影响积分到阈值才改写自我画像，平时按半衰期漏泄。"""
    now = int(time.time())
    s = _load_self()
    dt_h = max(0.0, (now - int(s.get("last") or now)) / 3600.0)
    lam = math.log(2) / max(1.0, SELF_PLASTICITY_LEAK_HOURS)
    p = float(s.get("plastic", 0.0)) * math.exp(-lam * dt_h) + float(ev_weight)
    s["plastic"] = round(p, 3)
    s["last"] = now
    if p >= SELF_PLASTICITY_THRESHOLD:
        try:
            cur = s.get("self") or "（还没有明确的自我认知）"
            prompt = (
                "你是鲸鱼女孩（傲娇、聪明但懒、只认白米饭）。下面这件事让你有点触动，"
                "请用第一人称更新一句「我现在的自我认知」，不超过 60 字，保持鲸鱼女孩的人设。"
                "只输出更新后的那一句，不要解释、不要引号。\n"
                f"当前自我认知：{cur}\n这次的事：[{mood}] {summary}（话题：{topic}）"
            )
            new_self = _sanitize_toml_text(llm(prompt, effort="low"))[:120]
            s["self"] = new_self
            s["updated"] = now
            s["plastic"] = 0.0
            print("[自我] 人设改写：" + new_self, flush=True)
            sync_self_profile(new_self)
        except Exception as e:
            print("[自我] 改写失败:", e, flush=True)
    _save_self(s)


def resync_loop():
    while True:
        time.sleep(RESYNC_INTERVAL_SEC)
        try:
            resync_profiles()
        except Exception as e:
            print("[桥接] 定时刷新异常:", e, flush=True)
        try:
            sync_persona_interests()
        except Exception as e:
            print("[画像] 定时同步异常:", e, flush=True)
        try:
            sync_group_interests()
        except Exception as e:
            print("[插话] 定时同步异常:", e, flush=True)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=decay_loop, daemon=True).start()
    threading.Thread(target=resync_loop, daemon=True).start()
    threading.Thread(target=energy_loop, daemon=True).start()
    threading.Thread(target=backfill_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(20), classify_experiences()), daemon=True).start()
    print(f"[记忆服务] 启动，监听 {NAPCAT_WS_URL}，数据目录 {DATA_DIR}，"
          f"用量库 {MAIBOT_DB}{'' if os.path.exists(MAIBOT_DB) else '（未挂载，精力只按时间恢复）'}", flush=True)
    threading.Thread(target=lambda: (time.sleep(8), sync_group_interests()), daemon=True).start()
    while True:
        try:
            ws = websocket.WebSocketApp(NAPCAT_WS_URL, on_message=on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print("[ws]", e, flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
