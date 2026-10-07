import asyncio
import logging
import re
from collections import defaultdict

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession


log = logging.getLogger("forwarder")


# ---------------------------------------------------------------------------
# Telegram URL patterns
# ---------------------------------------------------------------------------

PUBLIC_POST_RE = re.compile(
    r"^https?://t\.me/(?P<username>[A-Za-z0-9_]+)/(?P<message_id>\d+)/?$",
    re.IGNORECASE,
)

PUBLIC_POST_S_RE = re.compile(
    r"^https?://t\.me/s/(?P<username>[A-Za-z0-9_]+)/(?P<message_id>\d+)/?$",
    re.IGNORECASE,
)

PRIVATE_POST_RE = re.compile(
    r"^https?://t\.me/c/(?P<channel_id>\d+)/(?P<message_id>\d+)/?$",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/)",
    re.IGNORECASE,
)


class UserForwarder:
    """
    One independent forwarder instance per controller user.

    Responsibilities:
    - Connect using the user's encrypted Telethon StringSession
    - Resolve configured sources/destinations
    - Forward new media
    - Preserve albums
    - Forward old media on request
    """

    def __init__(self, uid, cfg, db, box):
        self.uid = uid
        self.cfg = cfg
        self.db = db
        self.box = box

        self.client = None
        self.task = None

        self.running = False

        self.last_event = "Not started"
        self.last_error = ""

        # Resolved Telegram entities.
        self.sources = {}
        self.source_entities = []
        self.destinations = []

        # Album buffering.
        self.album_buf = defaultdict(list)
        self.album_tasks = {}

        # Event handler reference so it can be removed cleanly.
        self.handler = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self):
        if self.last_error:
            return "🔴 Error"

        if self.running:
            return "🟢 Running"

        return "⚪ Stopped"

    # ------------------------------------------------------------------
    # Telegram entity resolution
    # ------------------------------------------------------------------

    async def resolve(self, ref):
        """
        Resolve:
            @username
            username
            -1001234567890
            public Telegram post URL
        """

        if not self.client:
            raise RuntimeError("Telegram client is not connected.")

        return await self.resolve_ref_external(self.client, ref)

    async def resolve_ref_external(self, client, ref):
        """
        Resolve a configured source/destination reference.
        """

        ref = (ref or "").strip()

        if not ref:
            raise RuntimeError("Empty Telegram reference.")

        # --------------------------------------------------------------
        # Telegram public post URL
        # --------------------------------------------------------------

        parsed = self._parse_post_link(ref)

        if parsed:
            source_ref, _message_id = parsed
            ref = source_ref

        # --------------------------------------------------------------
        # @username / username
        # --------------------------------------------------------------

        if ref.startswith("@"):
            return await client.get_entity(ref)

        if not ref.lstrip("-").isdigit():
            return await client.get_entity(ref)

        # --------------------------------------------------------------
        # Numeric Telegram ID
        # --------------------------------------------------------------

        wanted = int(ref)

        # First try direct entity access.
        try:
            entity = await client.get_entity(wanted)

            if utils.get_peer_id(entity) == wanted:
                return entity

        except Exception:
            pass

        # If direct resolution fails, search dialogs.
        async for dialog in client.iter_dialogs():
            dialog_id = dialog.id

            try:
                peer_id = utils.get_peer_id(dialog.entity)
            except Exception:
                peer_id = None

            if dialog_id == wanted or peer_id == wanted:
                return dialog.entity

        raise RuntimeError(
            f"Cannot resolve Telegram chat {ref}. "
            "Make sure the connected Telegram account has access to it."
        )

    # ------------------------------------------------------------------
    # Telegram post-link parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_post_link(link):
        """
        Returns:

            (source_reference, message_id)

        Examples:

            https://t.me/channel/123
                -> ("@channel", 123)

            https://t.me/s/channel/123
                -> ("@channel", 123)

            https://t.me/c/1234567890/123
                -> ("-1001234567890", 123)

        Returns None when the supplied value isn't a Telegram post URL.
        """

        link = (link or "").strip()

        # Public channel/group post.
        match = PUBLIC_POST_RE.match(link)

        if match:
            return (
                f"@{match.group('username')}",
                int(match.group("message_id")),
            )

        # Public /s/ channel post.
        match = PUBLIC_POST_S_RE.match(link)

        if match:
            return (
                f"@{match.group('username')}",
                int(match.group("message_id")),
            )

        # Private channel/group post.
        match = PRIVATE_POST_RE.match(link)

        if match:
            channel_id = match.group("channel_id")
            message_id = match.group("message_id")

            return (
                f"-100{channel_id}",
                int(message_id),
            )

        return None

    # ------------------------------------------------------------------
    # Link filtering
    # ------------------------------------------------------------------

    def _is_link_filtered(self, msg):
        if not self.db.skip_links(self.uid):
            return False

        text = msg.raw_text or ""

        return bool(URL_RE.search(text))

    # ------------------------------------------------------------------
    # Send media/messages
    # ------------------------------------------------------------------

    async def _send(self, msgs):
        """
        Forward one message or one album to every configured destination.
        """

        if not msgs:
            return

        msgs = sorted(msgs, key=lambda message: message.id)

        for destination in self.destinations:

            try:
                first = msgs[0]

                # ------------------------------------------------------
                # Single message
                # ------------------------------------------------------

                if len(msgs) == 1:

                    if first.media:

                        await self.client.send_file(
                            destination,
                            first.media,
                            caption=first.raw_text or None,
                        )

                    elif first.raw_text:

                        await self.client.send_message(
                            destination,
                            first.raw_text,
                        )

                # ------------------------------------------------------
                # Album
                # ------------------------------------------------------

                else:

                    media = [
                        message.media
                        for message in msgs
                        if message.media
                    ]

                    if media:

                        # Telethon accepts a list of media for albums.
                        await self.client.send_file(
                            destination,
                            media,
                            caption=first.raw_text or None,
                        )

                    elif first.raw_text:

                        await self.client.send_message(
                            destination,
                            first.raw_text,
                        )

                self.db.inc(self.uid, "sent")

                destination_id = utils.get_peer_id(destination)

                self.last_event = (
                    f"Forwarded {len(msgs)} message(s) "
                    f"→ {destination_id}"
                )

                log.info(
                    "user=%s forwarded count=%s destination=%s",
                    self.uid,
                    len(msgs),
                    destination_id,
                )

            except FloodWaitError as exc:

                self.last_error = (
                    f"FloodWait: Telegram requires "
                    f"{exc.seconds}s wait."
                )

                log.warning(
                    "user=%s flood wait=%ss",
                    self.uid,
                    exc.seconds,
                )

                await asyncio.sleep(exc.seconds)

            except Exception as exc:

                self.db.inc(self.uid, "failed")

                self.last_error = (
                    f"{type(exc).__name__}: {exc}"
                )

                log.exception(
                    "user=%s send failed destination=%s",
                    self.uid,
                    utils.get_peer_id(destination),
                )

    # ------------------------------------------------------------------
    # Album handling
    # ------------------------------------------------------------------

    async def _flush_album(self, key):
        """
        Wait briefly for all messages belonging to an album,
        then forward the complete album.
        """

        await asyncio.sleep(1.0)

        messages = self.album_buf.pop(key, [])

        self.album_tasks.pop(key, None)

        if not messages:
            return

        messages.sort(key=lambda message: message.id)

        await self._send(messages)

    # ------------------------------------------------------------------
    # Live message handler
    # ------------------------------------------------------------------

    async def _handle_new_message(self, event):
        try:
            message = event.message
            chat_id = event.chat_id

            log.info(
                "user=%s EVENT received "
                "source_message_id=%s chat_id=%s media=%s grouped=%s",
                self.uid,
                message.id,
                chat_id,
                bool(message.media),
                message.grouped_id,
            )

            self.last_event = (
                f"Received message {message.id} "
                f"from {chat_id}"
            )

            # ----------------------------------------------------------
            # Link filter
            # ----------------------------------------------------------

            if self._is_link_filtered(message):

                self.last_event = (
                    f"Skipped {message.id}: link filter"
                )

                log.info(
                    "user=%s skipped message=%s because of link filter",
                    self.uid,
                    message.id,
                )

                return

            # ----------------------------------------------------------
            # Ignore text-only messages
            # ----------------------------------------------------------

            if not message.media:

                self.last_event = (
                    f"Skipped text-only message {message.id}"
                )

                log.info(
                    "user=%s skipped text-only message=%s",
                    self.uid,
                    message.id,
                )

                return

            # ----------------------------------------------------------
            # Album
            # ----------------------------------------------------------

            if message.grouped_id:

                key = (
                    chat_id,
                    message.grouped_id,
                )

                self.album_buf[key].append(message)

                if key not in self.album_tasks:

                    self.album_tasks[key] = asyncio.create_task(
                        self._flush_album(key)
                    )

                return

            # ----------------------------------------------------------
            # Normal media message
            # ----------------------------------------------------------

            await self._send([message])

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            self.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            log.exception(
                "user=%s live event handler failed",
                self.uid,
            )

    # ------------------------------------------------------------------
    # Start live forwarding
    # ------------------------------------------------------------------

    async def start(self):
        """
        Start the Telethon listener.
        """

        if self.running:
            return

        encrypted_session = self.db.get_session(self.uid)

        if not encrypted_session:
            raise RuntimeError(
                "Connect your Telegram account first."
            )

        self.last_error = ""

        # --------------------------------------------------------------
        # Create Telethon client
        # --------------------------------------------------------------

        self.client = TelegramClient(
            StringSession(
                self.box.decrypt(encrypted_session)
            ),
            self.cfg.api_id,
            self.cfg.api_hash,
        )

        log.info(
            "user=%s connecting Telethon client",
            self.uid,
        )

        await self.client.connect()

        try:

            # ----------------------------------------------------------
            # Verify authorization
            # ----------------------------------------------------------

            if not await self.client.is_user_authorized():

                raise RuntimeError(
                    "Telegram session is not authorized. "
                    "Reconnect your Telegram account."
                )

            me = await self.client.get_me()

            if getattr(me, "bot", False):

                raise RuntimeError(
                    "The connected Telegram account is a bot. "
                    "Connect a normal Telegram user account."
                )

            log.info(
                "user=%s Telegram connected account_id=%s",
                self.uid,
                getattr(me, "id", None),
            )

            # ----------------------------------------------------------
            # Load configured references
            # ----------------------------------------------------------

            source_refs = self.db.refs(
                "sources",
                self.uid,
            )

            destination_refs = self.db.refs(
                "destinations",
                self.uid,
            )

            if not source_refs:
                raise RuntimeError(
                    "Add at least one source."
                )

            if not destination_refs:
                raise RuntimeError(
                    "Add at least one destination."
                )

            # ----------------------------------------------------------
            # Resolve sources
            # ----------------------------------------------------------

            self.source_entities = []

            self.sources = {}

            for ref in source_refs:

                log.info(
                    "user=%s resolving source=%s",
                    self.uid,
                    ref,
                )

                entity = await self.resolve(ref)

                peer_id = utils.get_peer_id(entity)

                self.sources[peer_id] = ref

                self.source_entities.append(entity)

                log.info(
                    "user=%s source ready ref=%s peer_id=%s",
                    self.uid,
                    ref,
                    peer_id,
                )

            # ----------------------------------------------------------
            # Resolve destinations
            # ----------------------------------------------------------

            self.destinations = []

            for ref in destination_refs:

                log.info(
                    "user=%s resolving destination=%s",
                    self.uid,
                    ref,
                )

                entity = await self.resolve(ref)

                self.destinations.append(entity)

                log.info(
                    "user=%s destination ready ref=%s peer_id=%s",
                    self.uid,
                    ref,
                    utils.get_peer_id(entity),
                )

            # ----------------------------------------------------------
            # Register Telethon event handler.
            #
            # IMPORTANT:
            # Use Telethon's native chats filter instead of receiving
            # every Telegram message and manually comparing chat IDs.
            # ----------------------------------------------------------

            self.handler = self.client.add_event_handler(
                self._handle_new_message,
                events.NewMessage(
                    chats=self.source_entities,
                ),
            )

            # ----------------------------------------------------------
            # Mark running
            # ----------------------------------------------------------

            self.running = True

            self.db.running(
                self.uid,
                1,
            )

            self.last_event = (
                f"Listener active for "
                f"{len(self.source_entities)} source(s)"
            )

            log.info(
                "user=%s forwarder running sources=%s destinations=%s",
                self.uid,
                len(self.source_entities),
                len(self.destinations),
            )

            # ----------------------------------------------------------
            # Keep Telethon alive.
            #
            # This task does NOT block aiogram's bot polling.
            # ----------------------------------------------------------

            self.task = asyncio.create_task(
                self.client.run_until_disconnected()
            )

        except Exception:

            try:
                await self.client.disconnect()
            except Exception:
                pass

            self.client = None

            self.running = False

            self.db.running(
                self.uid,
                0,
            )

            raise

    # ------------------------------------------------------------------
    # Stop live forwarding
    # ------------------------------------------------------------------

    async def stop(self):
        """
        Stop Telethon listener and clean up tasks.
        """

        self.running = False

        # Cancel album timers.
        for task in list(self.album_tasks.values()):

            if not task.done():
                task.cancel()

        self.album_tasks.clear()
        self.album_buf.clear()

        # Remove event handler.
        if self.client and self.handler:

            try:
                self.client.remove_event_handler(
                    self._handle_new_message,
                    events.NewMessage,
                )
            except Exception:
                pass

        self.handler = None

        # Disconnect Telegram.
        if self.client:

            try:
                await self.client.disconnect()
            except Exception:
                pass

        # Cancel run_until_disconnected task.
        if self.task and not self.task.done():

            self.task.cancel()

            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        self.task = None
        self.client = None

        self.db.running(
            self.uid,
            0,
        )

        self.last_event = "Stopped"

        log.info(
            "user=%s forwarder stopped",
            self.uid,
        )

    # ------------------------------------------------------------------
    # OLD MEDIA
    # ------------------------------------------------------------------

    async def forward_old_media(
        self,
        post_link,
        media_count,
    ):
        """
        Forward old media starting from a supplied Telegram post.

        Example:

            https://t.me/channel/500

        media_count=50

        The function scans backward from message 500 and collects
        50 media messages.

        Text-only messages do NOT count.

        Returned messages are forwarded oldest -> newest.
        """

        if not self.client:

            raise RuntimeError(
                "Start the forwarder first."
            )

        try:
            media_count = int(media_count)

        except (TypeError, ValueError):

            raise ValueError(
                "Media count must be a number."
            )

        if media_count <= 0:

            raise ValueError(
                "Media count must be greater than zero."
            )

        if media_count > 1000:

            raise ValueError(
                "Maximum old-media count is 1000 per request."
            )

        # --------------------------------------------------------------
        # Parse URL
        # --------------------------------------------------------------

        parsed = self._parse_post_link(post_link)

        if not parsed:

            raise ValueError(
                "Invalid Telegram post link.\n\n"
                "Examples:\n"
                "https://t.me/channel/123\n"
                "https://t.me/s/channel/123\n"
                "https://t.me/c/1234567890/123"
            )

        source_ref, start_message_id = parsed

        # --------------------------------------------------------------
        # Resolve source
        # --------------------------------------------------------------

        source = await self.resolve(
            source_ref
        )

        log.info(
            "user=%s old media request source=%s start=%s count=%s",
            self.uid,
            source_ref,
            start_message_id,
            media_count,
        )

        self.last_event = (
            f"Scanning old media from message "
            f"{start_message_id}"
        )

        # --------------------------------------------------------------
        # Scan backwards.
        #
        # We collect individual media messages first.
        # Albums are grouped afterward.
        # --------------------------------------------------------------

        collected = []

        current_id = start_message_id

        while len(collected) < media_count:

            remaining = media_count - len(collected)

            # Fetch in chunks to avoid making hundreds of API calls.
            limit = min(
                max(remaining * 2, 20),
                100,
            )

            messages = await self.client.get_messages(
                source,
                limit=limit,
                max_id=current_id + 1,
            )

            if not messages:
                break

            for message in messages:

                # We only want messages at or before the requested post.
                if message.id > start_message_id:
                    continue

                # Skip deleted/empty messages.
                if not message:
                    continue

                # Skip text-only messages.
                if not message.media:
                    continue

                # Link filtering.
                if self._is_link_filtered(message):
                    continue

                collected.append(message)

                if len(collected) >= media_count:
                    break

            # Find the oldest message returned.
            oldest_id = min(
                message.id
                for message in messages
            )

            if oldest_id <= 1:
                break

            current_id = oldest_id - 1

            # If Telegram returned fewer messages than requested,
            # we've probably reached the beginning.
            if len(messages) < limit:
                break

        if not collected:

            self.last_event = "No old media found"

            return 0

        # --------------------------------------------------------------
        # Reverse chronological order.
        #
        # get_messages() returns newest -> oldest.
        # We want oldest -> newest.
        # --------------------------------------------------------------

        collected.sort(
            key=lambda message: message.id
        )

        # --------------------------------------------------------------
        # Group albums together.
        # --------------------------------------------------------------

        batches = []

        album_groups = defaultdict(list)

        for message in collected:

            if message.grouped_id:

                album_groups[
                    (
                        utils.get_peer_id(source),
                        message.grouped_id,
                    )
                ].append(message)

            else:

                batches.append([message])

        # Insert albums into correct chronological positions.
        for album in album_groups.values():

            album.sort(
                key=lambda message: message.id
            )

            batches.append(album)

        batches.sort(
            key=lambda batch: batch[0].id
        )

        # --------------------------------------------------------------
        # Forward batches.
        # --------------------------------------------------------------

        forwarded = 0

        for batch in batches:

            await self._send(batch)

            forwarded += len(batch)

            self.last_event = (
                f"Old media forwarded "
                f"{forwarded}/{len(collected)}"
            )

            # Small delay prevents unnecessary API pressure.
            await asyncio.sleep(0.25)

        log.info(
            "user=%s old media completed requested=%s found=%s",
            self.uid,
            media_count,
            forwarded,
        )

        self.last_event = (
            f"Old media completed: "
            f"{forwarded} media message(s) forwarded"
        )

        return forwarded


class Manager:
    """
    Maintains one UserForwarder per controller user.
    """

    def __init__(self, cfg, db, box):
        self.cfg = cfg
        self.db = db
        self.box = box

        self.workers = {}

        self.lock = asyncio.Lock()

    def get(self, uid):
        if uid not in self.workers:

            self.workers[uid] = UserForwarder(
                uid,
                self.cfg,
                self.db,
                self.box,
            )

        return self.workers[uid]

    async def start(self, uid):
        async with self.lock:
            return await self.get(uid).start()

    async def stop(self, uid):
        return await self.get(uid).stop()

    async def disconnect(self, uid):
        await self.get(uid).stop()

        self.workers.pop(
            uid,
            None,
        )