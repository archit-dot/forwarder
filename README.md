# Telegram Forwarder — Multi-User / Phone Login

A Telegram forwarding controller using an Aiogram bot and one isolated Telethon user session per customer.

## What it does

- Forwards **new media** from configured source chats to configured destinations.
- Keeps Telegram albums together.
- Skips text-only messages.
- Optional link filter.
- Supports forwarding **old/historical media** from an existing Telegram post link without stopping the live forwarder.

## Historical media forwarding

After connecting your Telegram user account and configuring destinations:

1. Open **🕘 Old Media**.
2. Tap **📌 Forward from post link**.
3. Send a Telegram post link, for example:
   - `https://t.me/mychannel/123`
   - `https://t.me/c/1234567890/123` for a private channel/group.
4. Enter how many **media posts** to forward, from that post backwards.
5. The bot scans backwards, skips text-only posts, keeps albums together, and sends the selected media from oldest to newest.

The linked post is included in the scan. If the link points to an album, the whole album is treated as one media post.

Historical forwarding is separate from the live `▶️ Start` listener, so it can be used while live forwarding is running.

## Authentication

**Phone-number login only. QR login is not implemented.** Users authenticate their own normal Telegram account through the controller:

1. Enter your phone number in international format.
2. Telegram sends the login code.
3. Use the private inline keypad to enter the newest code and tap Verify.
4. If Telegram 2FA is enabled, enter the 2FA password.
5. The resulting Telethon session is encrypted at rest.

## Setup

Copy `.env.example` to `.env`, fill in the controller bot token, Telegram API ID/hash, owner ID, and Fernet encryption key.

Generate a key with:

```powershell
.\\.venv\\Scripts\\python.exe scripts/generate_key.py
```

Install:

```powershell
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
```

Run:

```powershell
.\\.venv\\Scripts\\python.exe main.py
```

## Supported source/destination references

- `@username`
- `-100...` Telegram chat/channel ID
- `https://t.me/...` public links

For private groups/channels, the connected Telegram user must already have access.

## Important

This project requires a **normal Telegram user account** for forwarding. The controller bot account itself is not used as the forwarding identity.
