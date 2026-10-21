#!/usr/bin/env python3
"""webcli - 联网抓取命令行工具

用法:
    python webcli.py fetch https://example.com
    python webcli.py fetch https://example.com --json
    python webcli.py fetch https://example.com --out page.txt
    python webcli.py batch urls.txt --out-dir pages
    python webcli.py search "关键词"          # 可选：复用 DeepSeek tools/search

依赖: requests, trafilatura
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

try:
    import requests
except ImportError:
    print("缺少 requests，请先执行: python -m pip install requests", file=sys.stderr)
    raise SystemExit(2)

try:
    import trafilatura
except ImportError:
    trafilatura = None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 webcli/1.0"
)
DEFAULT_TIMEOUT = 25
DEFAULT_MAX_CHARS = 12000


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode(resp: requests.Response) -> str:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";", 1)[0].strip().strip("\"'")
        try:
            return resp.content.decode(charset, errors="replace")
        except LookupError:
            pass
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    return resp.text


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, max_chars: int = DEFAULT_MAX_CHARS,
          raw: bool = False) -> Dict[str, Any]:
    """抓取一个 URL，返回结构化结果。"""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("只支持 http/https URL")

    started = time.time()
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/json;q=0.8,text/plain;q=0.7,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    })
    resp = sess.get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    text = _decode(resp)
    title = author = date = sitename = ""
    content = text
    kind = "text"

    if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
        kind = "html"
        if raw:
            content = text
        elif trafilatura is not None:
            extracted = trafilatura.extract(
                text, url=url, include_comments=False, include_tables=True,
                favor_recall=True, output_format="txt",
            ) or ""
            content = extracted or text
            meta = {}
            try:
                meta = trafilatura.extract_metadata(text, default_url=url) or {}
            except Exception:
                meta = {}
            title = str(getattr(meta, "title", "") or "")
            author = str(getattr(meta, "author", "") or "")
            date = str(getattr(meta, "date", "") or "")
            sitename = str(getattr(meta, "sitename", "") or "")
        else:
            # 无 trafilatura 时的退底：去脚本/样式/标签
            content = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
            content = re.sub(r"(?s)<[^>]+>", " ", content)
            content = re.sub(r"&nbsp;?", " ", content)
            content = re.sub(r"&amp;", "&", content)
            content = re.sub(r"&lt;", "<", content)
            content = re.sub(r"&gt;", ">", content)
    elif "json" in ctype:
        kind = "json"
        try:
            content = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except Exception:
            content = text
    elif "pdf" in ctype:
        raise ValueError("PDF 暂不支持，请先下载后用 pdftotext 转换")

    content = _clean_text(content)
    if max_chars and len(content) > max_chars:
        content = content[:max_chars].rstrip() + "\n…（已截断）"
    return {
        "url": resp.url,
        "status": resp.status_code,
        "kind": kind,
        "title": title,
        "author": author,
        "date": date,
        "sitename": sitename,
        "content": content,
        "chars": len(content),
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _print_result(item: Dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return
    head = []
    if item.get("title"):
        head.append("标题: " + item["title"])
    head.append("URL: " + item["url"])
    if item.get("sitename"):
        head.append("站点: " + item["sitename"])
    if item.get("author"):
        head.append("作者: " + item["author"])
    if item.get("date"):
        head.append("日期: " + item["date"])
    head.append("状态: %s · %s 字 · %sms" % (item.get("status"), item.get("chars"), item.get("elapsed_ms")))
    print("\n".join(head))
    print("\n" + "-" * 60 + "\n")
    print(item.get("content") or "（无正文）")


def cmd_fetch(args: argparse.Namespace) -> int:
    try:
        item = fetch(args.url, timeout=args.timeout, max_chars=args.max_chars, raw=args.raw)
    except Exception as e:
        print("抓取失败: %s" % e, file=sys.stderr)
        return 1
    if args.out:
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        body = json.dumps(item, ensure_ascii=False, indent=2) if args.json else item["content"]
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        print("已保存: %s（%s 字）" % (out, item["chars"]))
        return 0
    _print_result(item, as_json=args.json)
    return 0


def _iter_urls(path: str) -> Iterable[str]:
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def cmd_batch(args: argparse.Namespace) -> int:
    urls = list(_iter_urls(args.file))
    if not urls:
        print("没有可抓取的 URL", file=sys.stderr)
        return 1
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    ok = 0
    results: List[Dict[str, Any]] = []
    for i, url in enumerate(urls, 1):
        try:
            item = fetch(url, timeout=args.timeout, max_chars=args.max_chars, raw=args.raw)
            results.append(item)
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", url)[:80] or ("page_%d" % i)
            if args.jsonl:
                with open(os.path.join(out_dir, "results.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            else:
                with open(os.path.join(out_dir, name + ".txt"), "w", encoding="utf-8") as f:
                    f.write(item["content"])
            print("[%d/%d] OK %s（%s 字）" % (i, len(urls), url, item["chars"]))
            ok += 1
        except Exception as e:
            print("[%d/%d] FAIL %s: %s" % (i, len(urls), url, e), file=sys.stderr)
    print("完成: %d/%d" % (ok, len(urls)))
    return 0 if ok else 1


def cmd_search(args: argparse.Namespace) -> int:
    """复用 DeepSeek tools/search；Key 从环境变量 DEEPSEEK_KEY 读。"""
    key = os.environ.get("DEEPSEEK_KEY", "").strip()
    if not key:
        print("缺少 DEEPSEEK_KEY 环境变量", file=sys.stderr)
        return 2
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/tools/search",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"query": args.query, "count": args.count, "need_content": False},
            timeout=args.timeout,
        )
        r.raise_for_status()
        hits = (r.json() or {}).get("results") or []
    except Exception as e:
        print("搜索失败: %s" % e, file=sys.stderr)
        return 1
    for i, h in enumerate(hits, 1):
        print("%d. %s" % (i, h.get("title") or ""))
        print("   %s" % (h.get("url") or ""))
        if h.get("snippet"):
            print("   %s" % h["snippet"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcli", description="联网抓取 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="抓取单个 URL")
    f.add_argument("url")
    f.add_argument("--json", action="store_true", help="输出 JSON")
    f.add_argument("--raw", action="store_true", help="保留原始 HTML/文本，不做正文提取")
    f.add_argument("--out", help="保存到文件")
    f.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    f.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("batch", help="批量抓取 URL 列表")
    b.add_argument("file")
    b.add_argument("--out-dir", default="webcli_out")
    b.add_argument("--jsonl", action="store_true", help="写入 results.jsonl")
    b.add_argument("--raw", action="store_true")
    b.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    b.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    b.set_defaults(func=cmd_batch)

    s = sub.add_parser("search", help="联网搜索（可选）")
    s.add_argument("query")
    s.add_argument("--count", type=int, default=5)
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    s.set_defaults(func=cmd_search)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
