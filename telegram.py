#!/usr/bin/env python3
import os
import re
import shlex
import sys
import time
from pathlib import Path

import requests

SKILL_ROOT = Path(__file__).resolve().parent

_TG_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)\)")
_TG_CODE_RE = re.compile(r"`([^`\n]+)`")
_TG_HELD_RE = re.compile("\x00(\\d+)\x00")
_TG_BOLD_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*|(?<![\w*/])\*(?=[^*\s]*[A-Za-z0-9])([^*\n]+?)(?<=\S)\*(?![\w*/])")

TG_RENDER_LIMIT = 4096
TG_RAW_LIMIT = 3900
TG_CAPTION_LIMIT = 1024
TG_PART_PAUSE_SEC = 0.4


def _read_env_file(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        key, raw_val = s.split("=", 1)
        key = key.strip()
        raw_val = raw_val.strip()
        try:
            tokens = shlex.split(raw_val, comments=True)
            values[key] = tokens[0] if tokens else ""
        except ValueError:
            values[key] = raw_val.strip("\"'")
    return values


def _env_candidates():
    # Closest file wins: working directory, then the skill directory, then one or
    # two levels up, which covers both <repo>/skills/telegram_skill and a flat
    # <root>/telegram_skill with the .env at the repo root.
    return [Path.cwd() / ".env", SKILL_ROOT / ".env"] + [p / ".env" for p in list(SKILL_ROOT.parents)[:2]]


def _load_credentials():
    values = {}
    for candidate in _env_candidates():
        if candidate.is_file():
            values = _read_env_file(candidate)
            if values.get("TELEGRAM_BOT_TOKEN") and values.get("TELEGRAM_CHAT_ID"):
                break
    token = (values.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (values.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        sys.exit("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env or environment.")
    return token, int(chat_id)


def _tg_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_units(text):
    return len(text.encode("utf-16-le")) // 2


def _tg_html(body):
    held = []

    def hold(match):
        held.append(match.group(1))
        return "\x00" + str(len(held) - 1) + "\x00"

    out = _tg_escape(_TG_CODE_RE.sub(hold, body))
    out = _TG_LINK_RE.sub(lambda m: '<a href="' + m.group(2) + '">' + m.group(1) + "</a>", out)
    out = _TG_BOLD_RE.sub(lambda m: "<b>" + (m.group(1) if m.group(1) is not None else m.group(2)) + "</b>", out)
    if held:
        out = _TG_HELD_RE.sub(lambda m: "<code>" + _tg_escape(held[int(m.group(1))]) + "</code>", out)
    return out


def _tg_take(rest, limit):
    end = min(limit, len(rest))
    while end > 1:
        size = _tg_units(_tg_html(rest[:end]))
        if size <= TG_RENDER_LIMIT:
            break
        end = min(end - 1, int(end * TG_RENDER_LIMIT / float(size)))
    if end < len(rest):
        cut = rest.rfind("\n\n", 0, end)
        if cut < end // 2:
            cut = rest.rfind("\n", 0, end)
        if cut < end // 2:
            cut = rest.rfind(" ", 0, end)
        if cut < end // 2:
            cut = end
    else:
        cut = end
    return rest[:cut].rstrip(), rest[cut:].lstrip()


def _tg_chunks(body, limit=TG_RAW_LIMIT):
    parts, rest = [], body.strip()
    while rest:
        chunk, rest = _tg_take(rest, limit)
        if not chunk:
            chunk, rest = rest[0], rest[1:]
        parts.append(chunk)
    return parts


def _tg_retry_after(response):
    try:
        return max(1, min(10, int(response.json().get("parameters", {}).get("retry_after", 1))))
    except (ValueError, TypeError, AttributeError):
        return 1


def _tg_post(url, chat_id, text, parse_mode):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return requests.post(url, json=payload, timeout=30)


def _tg_send_one(url, chat_id, chunk):
    r = _tg_post(url, chat_id, _tg_html(chunk), "HTML")
    if r.status_code == 429:
        time.sleep(_tg_retry_after(r))
        r = _tg_post(url, chat_id, _tg_html(chunk), "HTML")
    if r.status_code == 400 and "can't parse entities" in r.text:
        r = _tg_post(url, chat_id, chunk, None)
    return r


def cmd_send(token, chat_id, args):
    if args and args != ["-"]:
        body = " ".join(args).strip()
    else:
        body = sys.stdin.read().strip()
    if not body:
        sys.exit("Error: empty message")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = _tg_chunks(body)
    sent = 0
    try:
        for index, chunk in enumerate(parts, 1):
            if index > 1:
                time.sleep(TG_PART_PAUSE_SEC)
            r = _tg_send_one(url, chat_id, chunk)
            ok = r.status_code == 200
            try:
                data = r.json() if ok else {}
            except ValueError:
                ok, data = False, {}
            if ok and not data.get("ok"):
                ok = False
            if not ok:
                if sent:
                    sys.exit(
                        f"Error: parts 1-{sent} of {len(parts)} were delivered and part {index} failed ({r.text[:200]}). " "Do not resend the whole message: send only the text that did not arrive."
                    )
                sys.exit(f"Error: Telegram part 1 of {len(parts)} failed: {r.text[:300]}")
            sent += 1
    except requests.RequestException as e:
        sys.exit(f"Error: send failed after {sent} part(s): {str(e)[:300]}")
    print(f"sent {len(body)} chars in {sent} message(s)")


def cmd_upload(token, chat_id, method, field, args):
    if not args:
        sys.exit(f"Usage: telegram.py {'photo' if field == 'photo' else 'file'} <path> [caption]")
    file_path = Path(args[0])
    if not file_path.is_file():
        sys.exit(f"Error: file not found: {file_path}")
    caption = " ".join(args[1:]).strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = {"chat_id": str(chat_id)}
    overflow = ""
    if caption:
        if _tg_units(caption) <= TG_CAPTION_LIMIT:
            data["caption"] = caption
        else:
            overflow = caption
    try:
        with file_path.open("rb") as fh:
            r = requests.post(url, data=data, files={field: (file_path.name, fh)}, timeout=120)
        if r.status_code == 429:
            time.sleep(_tg_retry_after(r))
            with file_path.open("rb") as fh:
                r = requests.post(url, data=data, files={field: (file_path.name, fh)}, timeout=120)
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            sys.exit(f"Error: Telegram {method} failed: {r.text[:300]}")
    except requests.RequestException as e:
        sys.exit(f"Error: Telegram {method} failed: {str(e)[:300]}")
    print(f"sent {field} {file_path.name} ({file_path.stat().st_size} bytes)")
    if overflow:
        cmd_send(token, chat_id, [overflow])


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print("Usage (run from this directory):")
        print('  python3 telegram.py send "message text"')
        print("  python3 telegram.py send -   # read message body from stdin")
        print("  python3 telegram.py photo <path> [caption]")
        print("  python3 telegram.py file <path> [caption]")
        return
    sub = sys.argv[1].lower()
    rest = sys.argv[2:]
    token, chat_id = _load_credentials()
    if sub == "send":
        cmd_send(token, chat_id, rest)
    elif sub == "photo":
        cmd_upload(token, chat_id, "sendPhoto", "photo", rest)
    elif sub in ("file", "document", "doc"):
        cmd_upload(token, chat_id, "sendDocument", "document", rest)
    else:
        sys.exit(f"Error: unknown subcommand '{sub}'. Expected send, photo, or file.")


if __name__ == "__main__":
    main()
