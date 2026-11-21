# Telegram skill

Send outbound text messages, photos, and files over Telegram. The helper reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env` or the environment, so every message goes to the single chat those credentials point at.

**Outbound only:** Use only `send`, `photo`, and `file`. Never call `getUpdates` or poll the Telegram API directly; `home_daemon.py` owns inbound polling and delivers incoming replies and attachments into `.inbox/`.

## Commands

Run these from the skill directory. From the workspace root, prefix each with `skills/telegram_skill/`.

```sh
# Short reply
python3 telegram.py send "Got it, added to notes.md."

# Longer or multi-line message (avoids shell quoting issues)
python3 telegram.py send - < reply.txt

# Send an image (plot, screenshot, CAD render)
python3 telegram.py photo plot.png "Weekly trend"

# Send any file (PDF, STL/3MF, CSV)
python3 telegram.py file bracket.3mf "Bracket v2"
```

## Credentials

Copy `.env.example` to `.env` and fill in both values. The helper takes them from the first `.env` that sets them, checking the working directory, then the skill directory, then the two directories above it, and falls back to environment variables. If either value is missing, it exits with an error.

## Formatting and chunking

- Aim for 1-3 short sentences per message unless sending a scannable bulleted list.
- Supported markup in `send`: `*bold*`, `[text](url)`, and `` `inline code` ``. Everything else is sent literally (`_italic_`, `> quote`, and `~strike~` arrive as typed; `&`, `<`, and `>` are escaped automatically).
- The helper splits long messages automatically at blank lines or line breaks to stay under Telegram's 4,096 UTF-16 code-unit limit. If a multi-part send fails partway through, the error states which parts were already delivered so you only resend the remaining text.
