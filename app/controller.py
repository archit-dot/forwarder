import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError,
)

log = logging.getLogger("controller")


class Form(StatesGroup):
    source = State()
    destination = State()

    phone = State()
    code = State()
    twofa = State()

    # Old media workflow
    old_media_link = State()
    old_media_count = State()


class Controller:
    def __init__(self, cfg, db, box, manager):
        self.cfg = cfg
        self.db = db
        self.box = box
        self.manager = manager

        self.router = Router()

        # uid -> {
        #     client,
        #     phone,
        #     phone_code_hash,
        #     needs_2fa,
        #     entered_code
        # }
        #
        # Login information is NEVER persisted.
        self.login = {}

        self._register()

    # ================================================================
    # MAIN KEYBOARD
    # ================================================================

    def kb(self, uid):
        s = self.db.stat(uid)

        b = InlineKeyboardBuilder()

        b.button(
            text=f"📱 Account {'✅' if s['connected'] else '❌'}",
            callback_data="account",
        )

        b.button(
            text=f"📥 Sources ({len(self.db.refs('sources', uid))})",
            callback_data="sources",
        )

        b.button(
            text=f"📤 Destinations ({len(self.db.refs('destinations', uid))})",
            callback_data="destinations",
        )

        b.button(
            text="🎯 Filters",
            callback_data="filters",
        )

        b.button(
            text="🔎 Test",
            callback_data="validate",
        )

        b.button(
            text="📊 Status",
            callback_data="status",
        )

        b.button(
            text="▶️ Start",
            callback_data="start",
        )

        b.button(
            text="⏹ Stop",
            callback_data="stop",
        )

        # NEW
        b.button(
            text="📜 Old Media",
            callback_data="old_media",
        )

        b.button(
            text="❓ Help",
            callback_data="help",
        )

        b.adjust(
            2,
            2,
            2,
            2,
            2,
            1,
        )

        return b.as_markup()

    # ================================================================
    # HOME
    # ================================================================

    async def home(self, t):
        uid = t.from_user.id

        self.db.ensure_user(
            uid,
            t.from_user.username or "",
            t.from_user.first_name or "",
        )

        s = self.db.stat(uid)

        text = (
            "<b>◆ Telegram Forwarder</b>\n\n"
            f"Account: {'🟢 Connected' if s['connected'] else '🔴 Not connected'}\n"
            f"📥 Sources: {len(self.db.refs('sources', uid))}\n"
            f"📤 Destinations: {len(self.db.refs('destinations', uid))}\n"
            f"Status: {'🟢 Running' if s['running'] else '⚪ Stopped'}\n\n"
            "Configure your Telegram forwarding below."
        )

        if isinstance(t, Message):
            await t.answer(
                text,
                reply_markup=self.kb(uid),
                parse_mode="HTML",
            )

        else:
            try:
                await t.message.edit_text(
                    text,
                    reply_markup=self.kb(uid),
                    parse_mode="HTML",
                )

            except Exception as e:
                # Telegram returns this when nothing changed.
                if "message is not modified" not in str(e).lower():
                    raise

    # ================================================================
    # HELP
    # ================================================================

    async def help(self, m):
        await m.answer(
            """<b>❓ Forwarder Help</b>

<b>1. Connect Telegram</b>
Tap 📱 Account → Connect.

Enter your phone number in international format.

Telegram sends a login code to your Telegram app. Use the keypad shown by the bot to enter the newest code.

If 2FA is enabled, the bot will ask for your Telegram 2FA password.

<b>2. Sources</b>
Add your source channel/group using:

<code>@username</code>

or:

<code>-1001234567890</code>

Your connected Telegram account must have access to the source.

<b>3. Destinations</b>
Add the destination channel/group using:

<code>@username</code>

or:

<code>-1001234567890</code>

Your connected account must be allowed to post there.

<b>4. Start</b>
Press ▶️ Start.

New media arriving in configured sources will be forwarded automatically.

Albums are kept together.

<b>5. Old Media</b>
Press 📜 Old Media.

Send a Telegram post link such as:

<code>https://t.me/channel/500</code>

Then enter how many media messages you want.

The bot scans backwards from that post, skips text-only messages, keeps albums together and forwards the selected media oldest → newest.

<b>6. Filters</b>
🎯 Filters can skip messages containing links.

<b>Privacy</b>
Each user's account, sources, destinations and statistics are isolated.

Telegram login codes and 2FA passwords are never stored.

<b>Commands</b>

/start — control panel

/help — this guide

/cancel — cancel an input step

/admin — owner-only service statistics
""",
            parse_mode="HTML",
        )

    # ================================================================
    # SOURCE / DESTINATION LIST
    # ================================================================

    def _list_markup(self, uid, kind):
        b = InlineKeyboardBuilder()

        b.button(
            text="➕ Add",
            callback_data=f"add_{kind}",
        )

        for ref in self.db.refs(kind, uid):
            b.button(
                text=f"🗑 {ref[:40]}",
                callback_data=f"del_{kind}|{ref}",
            )

        b.button(
            text="⬅️ Back",
            callback_data="home",
        )

        b.adjust(1)

        return b.as_markup()

    # ================================================================
    # LOGIN KEYBOARD
    # ================================================================

    def _login_markup(self, uid=None):
        b = InlineKeyboardBuilder()

        if uid is not None:

            code = self.login.get(
                uid,
                {},
            ).get(
                "entered_code",
                "",
            )

            masked = (
                " ".join("•" for _ in code)
                if code
                else "—"
            )

            b.button(
                text=f"Code: {masked}",
                callback_data="login_noop",
            )

        for row in (
            ("1", "2", "3"),
            ("4", "5", "6"),
            ("7", "8", "9"),
            ("0", "⌫", "Clear"),
        ):

            for digit in row:

                if digit.isdigit():

                    b.button(
                        text=digit,
                        callback_data=f"login_digit:{digit}",
                    )

                elif digit == "⌫":

                    b.button(
                        text=digit,
                        callback_data="login_back",
                    )

                else:

                    b.button(
                        text=digit,
                        callback_data="login_clear",
                    )

        b.button(
            text="✅ Verify",
            callback_data="login_submit",
        )

        b.button(
            text="🔄 New Code",
            callback_data="resend_code",
        )

        b.button(
            text="❌ Cancel",
            callback_data="cancel_login",
        )

        b.adjust(
            1,
            3,
            3,
            3,
            3,
            1,
            2,
        )

        return b.as_markup()

    # ================================================================
    # LOGIN CLEANUP
    # ================================================================

    async def _cleanup_login(self, uid):
        item = self.login.pop(
            uid,
            None,
        )

        if item:

            try:
                await item["client"].disconnect()

            except Exception:
                pass

    # ================================================================
    # REQUEST TELEGRAM LOGIN CODE
    # ================================================================

    async def _request_code(self, uid, phone):

        await self._cleanup_login(uid)

        client = TelegramClient(
            StringSession(),
            self.cfg.api_id,
            self.cfg.api_hash,
        )

        await client.connect()

        sent = await client.send_code_request(
            phone,
        )

        self.login[uid] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "needs_2fa": False,
            "entered_code": "",
        }

    # ================================================================
    # REGISTER HANDLERS
    # ================================================================

    def _register(self):

        r = self.router

        # ============================================================
        # /start
        # ============================================================

        @r.message(CommandStart())
        async def start(m, state: FSMContext):

            await state.clear()

            await self.home(m)

        # ============================================================
        # /help
        # ============================================================

        @r.message(Command("help"))
        async def helpcmd(m):

            await self.help(m)

        @r.callback_query(F.data == "help")
        async def helpcb(q):

            await q.answer()

            await self.help(
                q.message,
            )

        # ============================================================
        # /cancel
        # ============================================================

        @r.message(Command("cancel"))
        async def cancel(m, state: FSMContext):

            await self._cleanup_login(
                m.from_user.id,
            )

            await state.clear()

            await m.answer(
                "Cancelled.",
                reply_markup=self.kb(
                    m.from_user.id,
                ),
            )

        # ============================================================
        # HOME BUTTON
        # ============================================================

        @r.callback_query(F.data == "home")
        async def home(q):

            await q.answer()

            await self.home(q)

        # ============================================================
        # ACCOUNT
        # ============================================================

        @r.callback_query(F.data == "account")
        async def account(q):

            b = InlineKeyboardBuilder()

            b.button(
                text="📱 Connect / Reconnect",
                callback_data="connect",
            )

            b.button(
                text="🔌 Disconnect",
                callback_data="disconnect",
            )

            b.button(
                text="⬅️ Back",
                callback_data="home",
            )

            b.adjust(1)

            await q.answer()

            await q.message.edit_text(
                "<b>📱 Telegram Account</b>\n\n"
                "Connect your normal Telegram user account.\n\n"
                "<b>QR login is not used.</b>\n"
                "No StringSession copy/paste is required.",
                reply_markup=b.as_markup(),
                parse_mode="HTML",
            )

        # ============================================================
        # CONNECT
        # ============================================================

        @r.callback_query(F.data == "connect")
        async def connect(q, state: FSMContext):

            await q.answer()

            await self.start_phone_login(
                q,
                state,
            )

        # ============================================================
        # DISCONNECT
        # ============================================================

        @r.callback_query(F.data == "disconnect")
        async def disconnect(q):

            await self.manager.disconnect(
                q.from_user.id,
            )

            self.db.disconnect(
                q.from_user.id,
            )

            await q.answer(
                "Disconnected",
            )

            await self.home(q)

        # ============================================================
        # RESEND LOGIN CODE
        # ============================================================

        @r.callback_query(F.data == "resend_code")
        async def resend_code(q, state: FSMContext):

            uid = q.from_user.id

            item = self.login.get(uid)

            if not item or not item.get("phone"):

                await q.answer(
                    "No active login. Start Connect again.",
                    show_alert=True,
                )

                return

            try:

                await self._request_code(
                    uid,
                    item["phone"],
                )

                await state.set_state(
                    Form.code,
                )

                self.login[uid]["entered_code"] = ""

                await q.answer(
                    "New code requested",
                )

                await q.message.answer(
                    "🔢 <b>New Telegram login code sent.</b>\n\n"
                    "For security, do <b>not</b> type or paste "
                    "the code into a message.\n\n"
                    "Use the keypad below to enter the newest code.",
                    reply_markup=self._login_markup(uid),
                    parse_mode="HTML",
                )

            except FloodWaitError as e:

                await q.answer(
                    f"Telegram asks you to wait {e.seconds} seconds.",
                    show_alert=True,
                )

            except Exception as e:

                log.info(
                    "resend code failed for user %s: %s",
                    uid,
                    type(e).__name__,
                )

                await q.answer(
                    "Could not request a new code.",
                    show_alert=True,
                )

        # ============================================================
        # CANCEL LOGIN
        # ============================================================

        @r.callback_query(F.data == "cancel_login")
        async def cancel_login(q, state: FSMContext):

            await self._cleanup_login(
                q.from_user.id,
            )

            await state.clear()

            await q.answer(
                "Login cancelled",
            )

            await self.home(q)

        # ============================================================
        # SOURCES
        # ============================================================

        @r.callback_query(F.data == "sources")
        async def sources(q):

            await q.answer()

            refs = self.db.refs(
                "sources",
                q.from_user.id,
            )

            text = (
                "<b>📥 Sources</b>\n\n"
                +
                (
                    "\n".join(
                        f"{i + 1}. <code>{x}</code>"
                        for i, x in enumerate(refs)
                    )
                    if refs
                    else "No sources configured."
                )
            )

            await q.message.edit_text(
                text,
                reply_markup=self._list_markup(
                    q.from_user.id,
                    "sources",
                ),
                parse_mode="HTML",
            )

        # ============================================================
        # DESTINATIONS
        # ============================================================

        @r.callback_query(F.data == "destinations")
        async def dests(q):

            await q.answer()

            refs = self.db.refs(
                "destinations",
                q.from_user.id,
            )

            text = (
                "<b>📤 Destinations</b>\n\n"
                +
                (
                    "\n".join(
                        f"{i + 1}. <code>{x}</code>"
                        for i, x in enumerate(refs)
                    )
                    if refs
                    else "No destinations configured."
                )
            )

            await q.message.edit_text(
                text,
                reply_markup=self._list_markup(
                    q.from_user.id,
                    "destinations",
                ),
                parse_mode="HTML",
            )

        # ============================================================
        # ADD SOURCE
        # ============================================================

        @r.callback_query(F.data == "add_sources")
        async def adds(q, state: FSMContext):

            await state.set_state(
                Form.source,
            )

            await q.answer()

            await q.message.answer(
                "📥 Send source username or ID.\n\n"
                "Example:\n"
                "<code>@sourcechannel</code>\n"
                "<code>-1001234567890</code>",
                parse_mode="HTML",
            )

        # ============================================================
        # ADD DESTINATION
        # ============================================================

        @r.callback_query(F.data == "add_destinations")
        async def addd(q, state: FSMContext):

            await state.set_state(
                Form.destination,
            )

            await q.answer()

            await q.message.answer(
                "📤 Send destination username or ID.\n\n"
                "Example:\n"
                "<code>@destination</code>\n"
                "<code>-1001234567890</code>",
                parse_mode="HTML",
            )

        # ============================================================
        # SAVE SOURCE
        # ============================================================

        @r.message(Form.source)
        async def saves(m, state: FSMContext):

            ref = (m.text or "").strip()

            if not ref:

                await m.answer(
                    "❌ Please send a source username or ID.",
                )

                return

            self.db.ensure_user(
                m.from_user.id,
                m.from_user.username or "",
                m.from_user.first_name or "",
            )

            self.db.add_ref(
                "sources",
                m.from_user.id,
                ref,
            )

            await state.clear()

            await m.answer(
                "✅ Source added.",
                reply_markup=self.kb(
                    m.from_user.id,
                ),
            )

        # ============================================================
        # SAVE DESTINATION
        # ============================================================

        @r.message(Form.destination)
        async def saved(m, state: FSMContext):

            ref = (m.text or "").strip()

            if not ref:

                await m.answer(
                    "❌ Please send a destination username or ID.",
                )

                return

            self.db.ensure_user(
                m.from_user.id,
                m.from_user.username or "",
                m.from_user.first_name or "",
            )

            self.db.add_ref(
                "destinations",
                m.from_user.id,
                ref,
            )

            await state.clear()

            await m.answer(
                "✅ Destination added.",
                reply_markup=self.kb(
                    m.from_user.id,
                ),
            )

        # ============================================================
        # DELETE SOURCE
        # ============================================================

        @r.callback_query(F.data.startswith("del_sources|"))
        async def dels(q):

            ref = q.data.split(
                "|",
                1,
            )[1]

            self.db.remove_ref(
                "sources",
                q.from_user.id,
                ref,
            )

            await q.answer(
                "Removed",
            )

            await sources(q)

        # ============================================================
        # DELETE DESTINATION
        # ============================================================

        @r.callback_query(F.data.startswith("del_destinations|"))
        async def deld(q):

            ref = q.data.split(
                "|",
                1,
            )[1]

            self.db.remove_ref(
                "destinations",
                q.from_user.id,
                ref,
            )

            await q.answer(
                "Removed",
            )

            await dests(q)

        # ============================================================
        # FILTERS
        # ============================================================

        @r.callback_query(F.data == "filters")
        async def filters(q):

            on = self.db.skip_links(
                q.from_user.id,
            )

            b = InlineKeyboardBuilder()

            b.button(
                text=f"🔗 Skip Links: {'ON' if on else 'OFF'}",
                callback_data="toggle_links",
            )

            b.button(
                text="⬅️ Back",
                callback_data="home",
            )

            await q.answer()

            await q.message.edit_text(
                "<b>🎯 Filters</b>\n\n"
                "Skip link-containing messages when enabled.",
                reply_markup=b.as_markup(),
                parse_mode="HTML",
            )

        # ============================================================
        # TOGGLE LINKS
        # ============================================================

        @r.callback_query(F.data == "toggle_links")
        async def toggle(q):

            self.db.toggle_links(
                q.from_user.id,
            )

            await q.answer(
                "Updated",
            )

            await filters(q)

        # ============================================================
        # TEST
        # ============================================================

        @r.callback_query(F.data == "validate")
        async def validate(q):

            await q.answer(
                "Testing…",
            )

            c = None

            try:

                enc = self.db.get_session(
                    q.from_user.id,
                )

                if not enc:

                    raise RuntimeError(
                        "Connect your Telegram account first."
                    )

                c = TelegramClient(
                    StringSession(
                        self.box.decrypt(enc),
                    ),
                    self.cfg.api_id,
                    self.cfg.api_hash,
                )

                await c.connect()

                me = await c.get_me()

                if getattr(me, "bot", False):

                    raise RuntimeError(
                        "Connected account is a bot."
                    )

                w = self.manager.get(
                    q.from_user.id,
                )

                for ref in self.db.refs(
                    "sources",
                    q.from_user.id,
                ):

                    await w.resolve_ref_external(
                        c,
                        ref,
                    )

                for ref in self.db.refs(
                    "destinations",
                    q.from_user.id,
                ):

                    await w.resolve_ref_external(
                        c,
                        ref,
                    )

                await q.message.answer(
                    "✅ Account and "
                    f"{len(self.db.refs('sources', q.from_user.id))} source(s) / "
                    f"{len(self.db.refs('destinations', q.from_user.id))} destination(s) "
                    "are accessible."
                )

            except Exception as e:

                await q.message.answer(
                    "❌ Test failed:\n"
                    f"<code>{str(e)[:3000]}</code>",
                    parse_mode="HTML",
                )

            finally:

                if c:

                    try:
                        await c.disconnect()

                    except Exception:
                        pass

        # ============================================================
        # STATUS
        # ============================================================

        @r.callback_query(F.data == "status")
        async def status(q):

            uid = q.from_user.id

            s = self.db.stat(uid)

            w = self.manager.get(uid)

            text = (
                "<b>📊 Status</b>\n\n"
                f"Account: "
                f"{'🟢 Connected' if s['connected'] else '🔴 Not connected'}\n"
                f"Forwarder: {w.status()}\n\n"
                f"📤 Sent: {s['sent']}\n"
                f"❌ Failed: {s['failed']}\n\n"
                f"Last event: {w.last_event}"
            )

            if w.last_error:

                text += (
                    "\n\nError: "
                    f"<code>{w.last_error[:2000]}</code>"
                )

            await q.answer()

            try:

                await q.message.edit_text(
                    text,
                    reply_markup=self.kb(uid),
                    parse_mode="HTML",
                )

            except Exception as e:

                # Telegram throws this if text + keyboard are identical.
                if "message is not modified" not in str(e).lower():
                    raise

        # ============================================================
        # START FORWARDER
        # ============================================================

        @r.callback_query(F.data == "start")
        async def startf(q):

            uid = q.from_user.id

            await q.answer(
                "Starting…",
            )

            await q.message.answer(
                "⏳ <b>Starting forwarder...</b>\n\n"
                "Connecting to Telegram and checking sources/destinations.",
                parse_mode="HTML",
            )

            try:

                log.info(
                    "user=%s start requested",
                    uid,
                )

                await self.manager.start(
                    uid,
                )

                log.info(
                    "user=%s start completed",
                    uid,
                )

                await q.message.answer(
                    "🟢 <b>Forwarder started.</b>\n\n"
                    "New media from your configured sources "
                    "will now be forwarded.",
                    parse_mode="HTML",
                )

            except Exception as e:

                log.exception(
                    "user=%s start failed",
                    uid,
                )

                await q.message.answer(
                    "❌ <b>Could not start.</b>\n\n"
                    f"<code>{str(e)[:3000]}</code>",
                    parse_mode="HTML",
                )

        # ============================================================
        # STOP FORWARDER
        # ============================================================

        @r.callback_query(F.data == "stop")
        async def stopf(q):

            try:

                await self.manager.stop(
                    q.from_user.id,
                )

                await q.answer(
                    "Stopped",
                )

                await q.message.answer(
                    "⏹ <b>Forwarder stopped.</b>",
                    parse_mode="HTML",
                )

            except Exception as e:

                await q.answer(
                    "Stop failed",
                    show_alert=True,
                )

                await q.message.answer(
                    "❌ <b>Could not stop.</b>\n\n"
                    f"<code>{str(e)[:3000]}</code>",
                    parse_mode="HTML",
                )

        # ============================================================
        # OLD MEDIA - OPEN
        # ============================================================

        @r.callback_query(F.data == "old_media")
        async def old_media_start(q, state: FSMContext):

            uid = q.from_user.id

            await q.answer()

            if not self.db.get_session(uid):

                await q.message.answer(
                    "❌ <b>Telegram account is not connected.</b>\n\n"
                    "Go to 📱 Account → Connect first.",
                    parse_mode="HTML",
                )

                return

            await state.set_state(
                Form.old_media_link,
            )

            await q.message.answer(
                "📜 <b>Forward Old Media</b>\n\n"
                "Send the Telegram post link where the scan should start.\n\n"
                "<b>Public:</b>\n"
                "<code>https://t.me/channel/500</code>\n\n"
                "<b>Public /s/:</b>\n"
                "<code>https://t.me/s/channel/500</code>\n\n"
                "<b>Private:</b>\n"
                "<code>https://t.me/c/1234567890/500</code>\n\n"
                "The bot will scan backwards from that post.",
                parse_mode="HTML",
            )

        # ============================================================
        # OLD MEDIA - LINK
        # ============================================================

        @r.message(Form.old_media_link)
        async def old_media_link_input(
            m,
            state: FSMContext,
        ):

            uid = m.from_user.id

            link = (m.text or "").strip()

            if not link:

                await m.answer(
                    "❌ Please send a Telegram post link.",
                )

                return

            worker = self.manager.get(uid)

            try:

                parsed = worker._parse_post_link(
                    link,
                )

                if not parsed:

                    raise ValueError(
                        "Invalid Telegram post link."
                    )

            except Exception:

                await m.answer(
                    "❌ <b>Invalid Telegram post link.</b>\n\n"
                    "Use one of these formats:\n\n"
                    "<code>https://t.me/channel/123</code>\n"
                    "<code>https://t.me/s/channel/123</code>\n"
                    "<code>https://t.me/c/1234567890/123</code>",
                    parse_mode="HTML",
                )

                return

            await state.update_data(
                old_media_link=link,
            )

            await state.set_state(
                Form.old_media_count,
            )

            await m.answer(
                "🔢 <b>How many media messages?</b>\n\n"
                "Send the number of media messages you want.\n\n"
                "Example:\n"
                "<code>50</code>\n\n"
                "Text-only posts will be skipped automatically.",
                parse_mode="HTML",
            )

        # ============================================================
        # OLD MEDIA - COUNT
        # ============================================================

        @r.message(Form.old_media_count)
        async def old_media_count_input(
            m,
            state: FSMContext,
        ):

            uid = m.from_user.id

            raw_count = (m.text or "").strip()

            try:

                count = int(
                    raw_count,
                )

            except ValueError:

                await m.answer(
                    "❌ Please enter a valid number.\n\n"
                    "Example: <code>50</code>",
                    parse_mode="HTML",
                )

                return

            if count <= 0:

                await m.answer(
                    "❌ Number must be greater than 0.",
                )

                return

            if count > 1000:

                await m.answer(
                    "❌ Maximum is 1000 media messages per request.",
                )

                return

            data = await state.get_data()

            link = data.get(
                "old_media_link",
            )

            if not link:

                await state.clear()

                await m.answer(
                    "❌ Old-media request expired.\n\n"
                    "Press 📜 Old Media and try again.",
                    reply_markup=self.kb(uid),
                )

                return

            await state.clear()

            worker = self.manager.get(uid)

            # --------------------------------------------------------
            # Automatically connect if the forwarder isn't running.
            # --------------------------------------------------------

            if not worker.running:

                await m.answer(
                    "🔌 <b>Connecting to Telegram...</b>",
                    parse_mode="HTML",
                )

                try:

                    await worker.start()

                except Exception as e:

                    log.exception(
                        "user=%s old media could not start worker",
                        uid,
                    )

                    await m.answer(
                        "❌ <b>Could not connect to Telegram.</b>\n\n"
                        f"<code>{str(e)[:3000]}</code>",
                        parse_mode="HTML",
                    )

                    return

            # --------------------------------------------------------
            # Start background job.
            # --------------------------------------------------------

            await m.answer(
                "⏳ <b>Old-media forwarding started.</b>\n\n"
                f"📊 Requested: <b>{count}</b> media messages\n"
                "🔎 Scanning backwards from the supplied post...\n\n"
                "The bot will skip text-only posts automatically.",
                parse_mode="HTML",
            )

            asyncio.create_task(
                self._run_old_media(
                    m,
                    worker,
                    link,
                    count,
                )
            )

        # ============================================================
        # PHONE LOGIN INPUT
        # ============================================================

        @r.message(Form.phone)
        async def phone_input(
            m,
            state: FSMContext,
        ):

            uid = m.from_user.id

            phone = (
                m.text or ""
            ).strip()

            if not phone.startswith("+") or len(phone) < 7:

                await m.answer(
                    "❌ Please enter a valid phone number "
                    "in international format.\n\n"
                    "Example:\n"
                    "<code>+919876543210</code>",
                    parse_mode="HTML",
                )

                return

            try:

                await self._request_code(
                    uid,
                    phone,
                )

                await state.set_state(
                    Form.code,
                )

                await m.answer(
                    "🔢 <b>Telegram login code sent.</b>\n\n"
                    "For security, do <b>not</b> type or paste "
                    "the code into a message.\n\n"
                    "Use the keypad to enter the newest code.",
                    reply_markup=self._login_markup(uid),
                    parse_mode="HTML",
                )

            except PhoneNumberInvalidError:

                await self._cleanup_login(uid)

                await state.clear()

                await m.answer(
                    "❌ Telegram says that phone number is invalid.",
                )

            except FloodWaitError as e:

                await self._cleanup_login(uid)

                await state.clear()

                await m.answer(
                    f"⏳ Telegram asks you to wait "
                    f"{e.seconds} seconds before requesting another code."
                )

            except Exception as e:

                await self._cleanup_login(uid)

                await state.clear()

                log.info(
                    "phone login could not send code for user %s: %s",
                    uid,
                    type(e).__name__,
                )

                await m.answer(
                    "❌ Could not send the Telegram login code. "
                    "Please try again later."
                )

        # ============================================================
        # LOGIN NOOP
        # ============================================================

        @r.callback_query(F.data == "login_noop")
        async def login_noop(q):

            await q.answer(
                "Enter the newest code using the keypad.",
            )

        # ============================================================
        # LOGIN DIGIT
        # ============================================================

        @r.callback_query(F.data.startswith("login_digit:"))
        async def login_digit(q):

            uid = q.from_user.id

            item = self.login.get(uid)

            if not item:

                await q.answer(
                    "No active login. Start Connect again.",
                    show_alert=True,
                )

                return

            code = item.setdefault(
                "entered_code",
                "",
            )

            if len(code) >= 8:

                await q.answer(
                    "Code is full. Tap Verify or Clear.",
                )

                return

            digit = q.data.split(
                ":",
                1,
            )[1]

            item["entered_code"] = (
                code + digit
            )

            await q.answer()

            try:

                await q.message.edit_reply_markup(
                    reply_markup=self._login_markup(uid),
                )

            except Exception:
                pass

        # ============================================================
        # LOGIN BACKSPACE
        # ============================================================

        @r.callback_query(F.data == "login_back")
        async def login_back(q):

            uid = q.from_user.id

            item = self.login.get(uid)

            if item:

                item["entered_code"] = (
                    item.get("entered_code", "")[:-1]
                )

            await q.answer()

            try:

                await q.message.edit_reply_markup(
                    reply_markup=self._login_markup(uid),
                )

            except Exception:
                pass

        # ============================================================
        # LOGIN CLEAR
        # ============================================================

        @r.callback_query(F.data == "login_clear")
        async def login_clear(q):

            uid = q.from_user.id

            item = self.login.get(uid)

            if item:

                item["entered_code"] = ""

            await q.answer(
                "Cleared",
            )

            try:

                await q.message.edit_reply_markup(
                    reply_markup=self._login_markup(uid),
                )

            except Exception:
                pass

        # ============================================================
        # LOGIN VERIFY
        # ============================================================

        @r.callback_query(F.data == "login_submit")
        async def login_submit(
            q,
            state: FSMContext,
        ):

            uid = q.from_user.id

            item = self.login.get(uid)

            if not item:

                await q.answer(
                    "No active login. Start Connect again.",
                    show_alert=True,
                )

                return

            code = item.get(
                "entered_code",
                "",
            )

            if not code or len(code) < 4:

                await q.answer(
                    "Enter the complete Telegram code first.",
                    show_alert=True,
                )

                return

            await q.answer(
                "Verifying…",
            )

            client = item["client"]

            try:

                await client.sign_in(
                    phone=item["phone"],
                    code=code,
                    phone_code_hash=item["phone_code_hash"],
                )

                await self._finish_phone_login(
                    uid,
                    q,
                    state,
                    item,
                )

            except SessionPasswordNeededError:

                item["needs_2fa"] = True

                await state.set_state(
                    Form.twofa,
                )

                await q.message.answer(
                    "🔐 <b>2FA is enabled.</b>\n\n"
                    "Enter your Telegram 2FA password to finish connecting.\n\n"
                    "It is used only in memory and will not be stored or logged.\n\n"
                    "Use /cancel to stop.",
                    parse_mode="HTML",
                )

            except PhoneCodeExpiredError:

                log.info(
                    "phone code expired for user %s",
                    uid,
                )

                try:

                    await self._request_code(
                        uid,
                        item["phone"],
                    )

                    self.login[uid]["entered_code"] = ""

                    await state.set_state(
                        Form.code,
                    )

                    await q.message.answer(
                        "⌛ <b>The previous code expired.</b>\n\n"
                        "A new Telegram code has been requested.",
                        reply_markup=self._login_markup(uid),
                        parse_mode="HTML",
                    )

                except FloodWaitError as e:

                    await self._cleanup_login(uid)

                    await state.clear()

                    await q.message.answer(
                        f"⏳ Telegram asks you to wait "
                        f"{e.seconds} seconds before requesting another code."
                    )

                except Exception:

                    await self._cleanup_login(uid)

                    await state.clear()

                    await q.message.answer(
                        "❌ The code expired and Telegram could "
                        "not issue a new one."
                    )

            except PhoneCodeInvalidError:

                item["entered_code"] = ""

                await q.message.answer(
                    "❌ <b>Incorrect login code.</b>\n\n"
                    "Use the newest code and enter it with the keypad.",
                    reply_markup=self._login_markup(uid),
                    parse_mode="HTML",
                )

            except Exception as e:

                log.info(
                    "phone code login failed for user %s: %s",
                    uid,
                    type(e).__name__,
                )

                await self._cleanup_login(uid)

                await state.clear()

                await q.message.answer(
                    "❌ Telegram login failed.\n\n"
                    "Please start Connect again and use the newest code."
                )

        # ============================================================
        # BLOCK LOGIN CODE AS NORMAL MESSAGE
        # ============================================================

        @r.message(Form.code)
        async def code_text_blocked(
            m,
            state: FSMContext,
        ):

            await m.answer(
                "🔢 For security, please <b>do not send "
                "the login code as a message</b>.\n\n"
                "Use the keypad on the login-code message instead.",
                reply_markup=self._login_markup(
                    m.from_user.id,
                ),
                parse_mode="HTML",
            )

        # ============================================================
        # 2FA
        # ============================================================

        @r.message(Form.twofa)
        async def twofa(
            m,
            state: FSMContext,
        ):

            uid = m.from_user.id

            item = self.login.get(uid)

            if not item or not item.get("needs_2fa"):

                await state.clear()

                await m.answer(
                    "No active 2FA login. "
                    "Press 📱 Account → Connect again."
                )

                return

            try:

                # Password exists only during this call.
                # It is never stored or logged.

                await item["client"].sign_in(
                    password=m.text or "",
                )

                await self._finish_phone_login(
                    uid,
                    m,
                    state,
                    item,
                )

            except PasswordHashInvalidError:

                await m.answer(
                    "❌ Incorrect 2FA password. "
                    "Try again or /cancel."
                )

            except Exception as e:

                log.info(
                    "2FA completion failed for user %s: %s",
                    uid,
                    type(e).__name__,
                )

                await self._cleanup_login(uid)

                await state.clear()

                await m.answer(
                    "❌ 2FA login failed. "
                    "Please start Connect again."
                )

        # ============================================================
        # ADMIN
        # ============================================================

        @r.message(Command("admin"))
        async def admin(m):

            if m.from_user.id != self.cfg.owner_id:

                return await m.answer(
                    "⛔ Admin only.",
                )

            a = self.db.admin()

            await m.answer(
                "<b>👑 Admin</b>\n\n"
                f"Users: {a['total']}\n"
                f"Connected: {a['connected']}\n"
                f"Running: {a['running']}",
                parse_mode="HTML",
            )

        # ============================================================
        # FALLBACK
        # ============================================================

        @r.message()
        async def fallback(
            m,
            state: FSMContext,
        ):

            item = self.login.get(
                m.from_user.id,
            )

            if item and item.get("needs_2fa"):

                await state.set_state(
                    Form.twofa,
                )

                await m.answer(
                    "🔐 Enter your Telegram 2FA password, "
                    "or use /cancel."
                )

                return

            await m.answer(
                "Use /start or /help and the buttons.",
            )

    # ================================================================
    # OLD MEDIA BACKGROUND JOB
    # ================================================================

    async def _run_old_media(
        self,
        message,
        worker,
        link,
        count,
    ):

        uid = message.from_user.id

        log.info(
            "user=%s old media requested link=%s count=%s",
            uid,
            link,
            count,
        )

        try:

            forwarded = await worker.forward_old_media(
                link,
                count,
            )

            await message.answer(
                "✅ <b>Old-media forwarding completed.</b>\n\n"
                f"📤 Media forwarded: <b>{forwarded}</b>\n"
                f"🎯 Requested: <b>{count}</b>",
                parse_mode="HTML",
            )

            log.info(
                "user=%s old media completed forwarded=%s",
                uid,
                forwarded,
            )

        except asyncio.CancelledError:

            log.info(
                "user=%s old media task cancelled",
                uid,
            )

            raise

        except Exception as e:

            log.exception(
                "user=%s old media failed",
                uid,
            )

            try:

                await message.answer(
                    "❌ <b>Old-media forwarding failed.</b>\n\n"
                    f"<code>{str(e)[:3000]}</code>",
                    parse_mode="HTML",
                )

            except Exception:

                pass

    # ================================================================
    # FINISH TELEGRAM LOGIN
    # ================================================================

    async def _finish_phone_login(
        self,
        uid,
        m,
        state,
        item,
    ):

        client = item["client"]

        target = (
            m.message
            if isinstance(m, CallbackQuery)
            else m
        )

        try:

            me = await client.get_me()

            if getattr(me, "bot", False):

                raise RuntimeError(
                    "Bot accounts cannot be used. "
                    "Connect a normal Telegram user account."
                )

            session = client.session.save()

            username = (
                m.from_user.username or ""
            )

            first_name = (
                m.from_user.first_name or ""
            )

            self.db.ensure_user(
                uid,
                username,
                first_name,
            )

            self.db.set_session(
                uid,
                self.box.encrypt(session),
            )

            await client.disconnect()

            self.login.pop(
                uid,
                None,
            )

            await state.clear()

            log.info(
                "user %s connected Telegram account id=%s",
                uid,
                getattr(me, "id", None),
            )

            await target.answer(
                "✅ <b>Telegram account connected successfully.</b>\n\n"
                "Your encrypted session has been saved.\n"
                "The login code and 2FA password were not stored or logged.",
                reply_markup=self.kb(uid),
                parse_mode="HTML",
            )

        except Exception:

            self.login.pop(
                uid,
                None,
            )

            await state.clear()

            try:

                await client.disconnect()

            except Exception:

                pass

            log.info(
                "session save failed for user %s",
                uid,
            )

            await target.answer(
                "❌ Could not save the Telegram session. "
                "Please try Connect again."
            )

    # ================================================================
    # START PHONE LOGIN
    # ================================================================

    async def start_phone_login(
        self,
        q,
        state,
    ):

        await self._cleanup_login(
            q.from_user.id,
        )

        await state.set_state(
            Form.phone,
        )

        await q.message.answer(
            "📱 <b>Connect Telegram</b>\n\n"
            "Send your Telegram phone number "
            "in international format.\n\n"
            "Example:\n"
            "<code>+919876543210</code>\n\n"
            "Telegram will send a login code to your Telegram app.\n"
            "<b>QR login is not used.</b>\n\n"
            "Use the keypad to enter the newest code.\n\n"
            "Use /cancel if you want to stop.",
            parse_mode="HTML",
        )