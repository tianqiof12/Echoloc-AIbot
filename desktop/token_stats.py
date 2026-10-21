#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 统计：MaiBot 用量库 + memwatch 事件记忆，按群隔离。

消费不直接用 MaiBot 里记的 cost，而是按 DeepSeek 当前官方价、
结合每条用量的时间（高峰/空闲）和缓存命中情况重新计算。
SiliconFlow 的 token 单独统计；它的价格在 model_config.toml 里是 0，
所以不硬编一个价格，表里只显示 token 数，消费仍只算 DeepSeek。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

DB = os.environ.get("MAIBOT_DB", "/opt/maibot/data/MaiMBot/MaiBot.db")
MW_DIR = os.environ.get("MW_DIR", "/opt/memwatch/data/groups")

# (缓存命中输入, 未命中输入, 输出)，单位：元 / 百万 tokens
FLASH_OFF = (0.02, 1.0, 4.0)
FLASH_PEAK = (0.04, 2.0, 8.0)
PRO_OFF = (0.15, 4.5, 13.5)
PRO_PEAK = (0.30, 9.0, 27.0)

# 走 SiliconFlow 的模型前缀。SiliconFlow 上也可能挂 deepseek-ai/ 开头的模型，
# 所以要先判 SF，再判 DeepSeek 官方直连。
SF_PREFIXES = (
    "qwen/", "baai/", "thudm/", "zai-org/", "tencent/", "pro/",
    "deepseek-ai/", "internlm/", "minimaxai/", "meta-llama/",
    "google/", "nvidia/", "moonshotai/", "stepfun/", "01-ai/",
    "rednote-hilab/", "openbmb/", "thu/",
)

# 硅基流动官方价（元 / 百万 tokens）：(输入, 输出)。没列出的模型按 0 计。
# bge-m3 是免费向量模型；Qwen3-VL-30B 输入 0.7、输出 2.8（2026-10 官网价）。
SF_PRICES = {
    "qwen/qwen3-vl-30b-a3b-instruct": (0.7, 2.8),
    "qwen/qwen3-vl-30b-a3b-thinking": (0.7, 2.8),
}


def empty():
    return {"gid": "", "kind": "group", "name": "", "req": 0, "inp": 0, "out": 0, "tokens": 0,
            "hit": 0, "miss": 0, "cost": 0.0,
            "ds_req": 0, "ds_tokens": 0, "sf_req": 0, "sf_tokens": 0, "sf_cost": 0.0,
            "mw_req": 0, "mw_tokens": 0, "mw_cost": 0.0}


def provider_of(model):
    m = str(model or "").strip().lower()
    if m.startswith(SF_PREFIXES):
        return "sf"
    if "deepseek" in m:
        return "ds"
    return "other"


def price_of(model, peak):
    m = str(model or "").lower()
    if "pro" in m:
        return PRO_PEAK if peak else PRO_OFF
    return FLASH_PEAK if peak else FLASH_OFF


def fetch(con, since=None):
    where = ""
    params = ()
    if since:
        where = "WHERE u.timestamp >= ?"
        params = (since,)
    # 北京时间周一至周五 9-12、14-18 为高峰；时间戳本身按本地时间存
    sql = f"""
        SELECT COALESCE(cs.group_id, ''), COALESCE(cs.user_id, ''), COALESCE(cs.group_name, ''),
               u.model_name,
               CASE WHEN CAST(strftime('%w', u.timestamp) AS INTEGER) BETWEEN 1 AND 5
                     AND (CAST(strftime('%H', u.timestamp) AS INTEGER) BETWEEN 9 AND 11
                          OR CAST(strftime('%H', u.timestamp) AS INTEGER) BETWEEN 14 AND 17)
                    THEN 1 ELSE 0 END AS peak,
               COUNT(*),
               COALESCE(SUM(u.prompt_cache_hit_tokens), 0),
               COALESCE(SUM(u.prompt_cache_miss_tokens), 0),
               COALESCE(SUM(u.completion_tokens), 0),
               COALESCE(SUM(u.total_tokens), 0)
        FROM llm_usage u
        LEFT JOIN chat_sessions cs ON u.session_id = cs.session_id
        {where}
        GROUP BY 1, 2, 3, 4, 5
    """
    out = {}
    for gid, uid, gname, model, peak, req, hit, miss, outp, tot in con.execute(sql, params):
        gid = str(gid or "")
        uid = str(uid or "")
        if gid:
            key, kind = gid, "group"
        elif uid:
            key, kind = "p" + uid, "private"
        else:
            key, kind = "", "system"
        e = out.setdefault(key, empty())
        e["gid"] = key
        e["kind"] = kind
        if gname:
            e["name"] = gname
        e["req"] += int(req or 0)
        e["hit"] += int(hit or 0)
        e["miss"] += int(miss or 0)
        e["out"] += int(outp or 0)
        e["tokens"] += int(tot or 0)
        e["inp"] += int(hit or 0) + int(miss or 0)
        provider = provider_of(model)
        if provider == "sf":
            e["sf_req"] += int(req or 0)
            e["sf_tokens"] += int(tot or 0)
            price = SF_PRICES.get(str(model or "").strip().lower())
            if price:
                # 硅基流动部分模型不写 prompt_cache 字段，用 total - completion 兜底
                inp = int(hit or 0) + int(miss or 0)
                if inp <= 0:
                    inp = max(0, int(tot or 0) - int(outp or 0))
                e["sf_cost"] += (inp * price[0] + int(outp or 0) * price[1]) / 1_000_000
        elif provider == "ds":
            e["ds_req"] += int(req or 0)
            e["ds_tokens"] += int(tot or 0)
            p = price_of(model, bool(peak))
            e["cost"] += (int(hit or 0) * p[0] + int(miss or 0) * p[1] + int(outp or 0) * p[2]) / 1_000_000
    return out


def mw_usage(since=None):
    """memwatch 自己的事件记账。since=None 取全量，否则按 usage_daily 求和。"""
    out = {}
    cutoff = since.strftime("%Y-%m-%d") if since else None
    try:
        names = os.listdir(MW_DIR)
    except FileNotFoundError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(MW_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        gid = str(d.get("group_id") or fn[:-5])
        if cutoff is None:
            u = d.get("usage") or {}
            out[gid] = {
                "req": int(u.get("requests") or 0),
                "tokens": int(u.get("total_tokens") or 0),
                "cost": float(u.get("cost") or 0.0),
            }
            continue
        daily = d.get("usage_daily") or {}
        rq = tk = 0
        cs = 0.0
        for day, v in daily.items():
            if str(day) >= cutoff:
                rq += int(v.get("requests") or 0)
                tk += int(v.get("total_tokens") or 0)
                cs += float(v.get("cost") or 0.0)
        out[gid] = {"req": rq, "tokens": tk, "cost": cs}
    return out


def private_names():
    """私聊 QQ 号 → 昵称：先看 memwatch 的 p<uid>.json，再退回麦麦的人物库。"""
    names = {}
    try:
        files = os.listdir(MW_DIR)
    except FileNotFoundError:
        return names
    for fn in files:
        if not fn.endswith(".json") or not fn.startswith("p"):
            continue
        uid = fn[1:-5]
        try:
            d = json.load(open(os.path.join(MW_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        nm = ""
        for _u, ent in (d.get("affinity") or {}).items():
            if isinstance(ent, dict) and ent.get("name"):
                nm = ent["name"]
                break
        if not nm:
            prof = d.get("profiles") or {}
            if prof:
                nm = next(iter(prof))
        if nm:
            names[uid] = nm
    # 还没私聊过的，从麦麦的 person_info 里补
    try:
        con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
        for uid, nm, nick in con.execute(
                "SELECT user_id, person_name, user_nickname FROM person_info "
                "WHERE platform = 'qq' AND user_id IS NOT NULL AND user_id != ''"):
            uid = str(uid)
            if uid in names:
                continue
            nm = (nm or nick or "").strip()
            if nm:
                names[uid] = nm
        con.close()
    except Exception:
        pass
    return names


def merge(db, mw):
    for gid, u in mw.items():
        e = db.setdefault(gid, empty())
        e["gid"] = gid
        e["kind"] = e.get("kind") or ("private" if str(gid).startswith("p") else "group")
        e["mw_req"] += u["req"]
        e["mw_tokens"] += u["tokens"]
        e["mw_cost"] += u["cost"]
    names = private_names()
    for key, e in db.items():
        kind = e.get("kind") or ("private" if str(key).startswith("p") else "group")
        e["kind"] = kind
        if not e["name"]:
            if kind == "private":
                uid = str(key)[1:]
                e["name"] = "私聊 " + (names.get(uid) or ("QQ " + uid))
            elif not key:
                e["name"] = "未归属 / 系统任务"
                e["kind"] = "system"
            else:
                e["name"] = "群 " + key
        e["cost"] = round(e["cost"], 6)
        e["sf_cost"] = round(e.get("sf_cost", 0.0), 6)
        e["mw_cost"] = round(e["mw_cost"], 6)
        e["all_tokens"] = e["tokens"] + e["mw_tokens"]
        e["all_cost"] = round(e["cost"] + e["sf_cost"] + e["mw_cost"], 6)
    return db


def total(rows):
    t = {"req": 0, "tokens": 0, "cost": 0.0, "mw_req": 0, "mw_tokens": 0, "mw_cost": 0.0,
         "ds_req": 0, "ds_tokens": 0, "sf_req": 0, "sf_tokens": 0, "sf_cost": 0.0}
    for e in rows.values():
        t["req"] += e["req"]
        t["tokens"] += e["tokens"]
        t["cost"] += e["cost"]
        t["mw_req"] += e["mw_req"]
        t["mw_tokens"] += e["mw_tokens"]
        t["mw_cost"] += e["mw_cost"]
        t["ds_req"] += e.get("ds_req", 0)
        t["ds_tokens"] += e.get("ds_tokens", 0)
        t["sf_req"] += e.get("sf_req", 0)
        t["sf_tokens"] += e.get("sf_tokens", 0)
        t["sf_cost"] += e.get("sf_cost", 0.0)
    t["cost"] = round(t["cost"], 6)
    t["sf_cost"] = round(t["sf_cost"], 6)
    t["mw_cost"] = round(t["mw_cost"], 6)
    t["all_tokens"] = t["tokens"] + t["mw_tokens"]
    t["all_cost"] = round(t["cost"] + t["sf_cost"] + t["mw_cost"], 6)
    return t


def period_starts():
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "day": midnight,
        "week": midnight - timedelta(days=midnight.weekday()),
        "month": midnight.replace(day=1),
        "all": None,
    }


def main():
    now = datetime.now()
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    out = {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        for key, since in period_starts().items():
            db = fetch(con, since.strftime("%Y-%m-%d %H:%M:%S") if since else None)
            rows = merge(db, mw_usage(since))
            rows = {k: v for k, v in rows.items()
                    if v["all_tokens"] > 0 or v["all_cost"] > 0 or v.get("sf_tokens", 0) > 0}
            out[key] = {"groups": list(rows.values()), "total": total(rows)}
    finally:
        con.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
