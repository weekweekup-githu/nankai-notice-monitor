"""Monitor one public Nankai notice list. Python 3.11+, standard library only."""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = "https://economics.nankai.edu.cn/tzgg/list.htm"


class NoticeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.regions = 0
        self.in_title = False
        self.anchor = None
        self.items = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "ul":
            if self.depth:
                self.depth += 1
            elif "wp_article_list" in a.get("class", "").split():
                self.depth = 1
                self.regions += 1
        if not self.depth:
            return
        if tag == "span" and "Article_Title" in a.get("class", "").split():
            self.in_title = True
        if tag == "a" and self.in_title:
            self.anchor = {"href": a.get("href", ""), "title": a.get("title", ""), "text": []}

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            a, self.anchor = self.anchor, None
            link = urljoin(URL, a["href"])
            parts = urlsplit(link)
            title = " ".join((a["title"] or "".join(a["text"])).split())
            if (parts.scheme == "https" and parts.netloc == "economics.nankai.edu.cn"
                    and re.fullmatch(r"/\d{4}/\d{4}/c\d+a\d+/page\.htm", parts.path) and title):
                self.items[parts._replace(query="", fragment="").geturl()] = title
        if tag == "span":
            self.in_title = False
        if tag == "ul" and self.depth:
            self.depth -= 1


def parse_notices(html):
    p = NoticeParser()
    p.feed(html)
    if p.regions != 1 or not 1 <= len(p.items) <= 100:
        raise RuntimeError("未能识别唯一的公告列表，或公告数量异常；保留原记录，请检查页面结构。")
    return p.items


def fetch_html():
    for attempt in range(3):
        try:
            req = Request(URL, headers={"User-Agent": "NankaiNoticeMonitor/1.0 (4 checks/day)",
                                        "Cache-Control": "no-cache"})
            with urlopen(req, timeout=30) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise RuntimeError("页面过大，停止处理并保留原记录。")
                return raw.decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("连续三次无法获取学院网页；原记录未改变，下次检查将重试。") from None
            time.sleep(3 * (attempt + 1))


def send_push(title, description):
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not re.fullmatch(r"SCT[A-Za-z0-9]+", key):
        raise RuntimeError("请在仓库 Secrets 中设置有效的 Server酱 Turbo SERVERCHAN_SENDKEY（SCT 开头）。")
    req = Request("https://sctapi.ftqq.com/" + key + ".send",
                  data=urlencode({"title": title, "desp": description}).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read(100_000).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        # Never print exception URLs: the SendKey appears in the endpoint path.
        raise RuntimeError("推送请求失败或响应不明确；不更新已通知记录，下次检查重试（可能重复提醒）。") from None
    if not isinstance(result, dict) or result.get("code") != 0:
        raise RuntimeError("推送服务未接受消息；请到 Server酱后台检查密钥、通道和当天额度。")


def read_state(path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema") != 1 or data.get("url") != URL
                or not isinstance(data.get("seen"), dict)
                or not data["seen"]
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in data["seen"].items())):
            raise ValueError
        return data
    except (ValueError, AttributeError):
        raise RuntimeError("state.json 格式异常；停止检查，避免将旧通知误报为新增。") from None


def changes_for(current, previous):
    return [("新增" if link not in previous else "标题修改", title, link)
            for link, title in current.items()
            if link not in previous or previous[link] != title]


def run(current, state_path, always_notify=False, dry_run=False, sender=send_push):
    old = read_state(state_path)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    previous = old["seen"] if old else {}
    changes = changes_for(current, previous) if old else []
    if old is None:
        title = "南开经院公告监控已启用"
        body = (f"已保存当前 {len(current)} 条公告作为起点，旧公告不会逐条推送。\n\n"
                f"检查时间：{now}\n\n每天北京时间 06:00、13:00、19:00、23:00 检查；无更新时是否提醒由 ALWAYS_NOTIFY 控制。\n\n[通知公告]({URL})")
    elif changes:
        title = f"南开经院公告更新：{len(changes)}条"
        rows = []
        for kind, text, link in changes:
            # Escape Markdown in public titles; all links are validated above.
            safe = re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", text)
            rows.append(f"- {kind}：[{safe}]({link})")
        body = f"检查时间：{now}\n\n" + "\n\n".join(rows)
    elif always_notify:
        title = "南开经院公告：本次无更新"
        body = f"检查时间：{now}\n\n已检查公告列表，未发现新增或标题修改。\n\n[查看公告]({URL})"
    else:
        title = body = None
    print(f"读取 {len(current)} 条；首次运行：{old is None}；变化：{len(changes)} 条。")
    if dry_run:
        print(json.dumps({"push_title": title, "push_body": body,
                          "notices": current}, ensure_ascii=False, indent=2))
        return
    if title:
        sender(title, body)  # Advance state only after the provider accepts the push.
        print("推送服务已接受消息；请在手机确认收到。")
    new = {"schema": 1, "url": URL, "last_success": now, "seen": {**previous, **current}}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(state_path)
    print("本地检查记录已保存，工作流随后提交到仓库。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, help="Read a local HTML fixture instead of fetching")
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--dry-run", action="store_true", help="No push and no state writes")
    args = parser.parse_args()
    try:
        html = args.html.read_text(encoding="utf-8") if args.html else fetch_html()
        run(parse_notices(html), args.state,
            os.environ.get("ALWAYS_NOTIFY", "true").lower() == "true", args.dry_run)
    except (RuntimeError, OSError, UnicodeError) as error:
        # This script uses sanitized RuntimeErrors for all network operations.
        message = str(error) if isinstance(error, RuntimeError) else "本地文件或字符编码处理失败。"
        print("ERROR: " + message, file=sys.stderr)
        if not args.dry_run and os.environ.get("SERVERCHAN_SENDKEY"):
            try:
                send_push("南开经院公告监控异常", message + "\n\n请检查 GitHub Actions 日志。")
            except RuntimeError:
                print("异常提醒也未能确认送达，请查看 GitHub Actions。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
